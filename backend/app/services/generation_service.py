import os
import json
import uuid
from pathlib import Path
from typing import List, Dict, Optional

from google import genai
from app.core.config import settings
from app.db.supabase import get_supabase
from app.services.retrieval_service import RetrievalService

class GenerationService:
    def __init__(self):
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.supabase = get_supabase()
        self.retrieval_service = RetrievalService()
        self.exam_pattern_dir = Path("data/exam_patterns")

    def _get_exam_pattern(self, subject: str, board: str) -> str:
        """Reads the exam pattern markdown file."""
        pattern_file = self.exam_pattern_dir / f"{subject}_{board}.md"
        if pattern_file.exists():
            return pattern_file.read_text(encoding="utf-8")
        return "No specific exam pattern provided. Use standard educational style."

    def _generate_cards_from_chunks(
        self,
        topic: str,
        chapter: str,
        board: str,
        subject: str,
        chunks: List[str],
        exam_pattern: str
    ) -> List[Dict]:
        """Sends chunks and pattern to Gemini to generate 5 types of cards."""
        context_text = "\n\n".join(chunks)

        prompt = f"""
You are an expert exam paper setter for {board} board, {subject} subject.
Based on the following textbook chunks, generate 5 high-quality flashcards for the topic: '{topic}'.

### TEXTBOOK CONTEXT:
{context_text}

### EXAM PATTERN & STYLE:
{exam_pattern}

### INSTRUCTIONS:
For this topic, you MUST generate exactly one card of each of these 5 types:
1. Q&A: A standard definition or recall question.
2. Fill in the blank: A sentence with a key term missing.
3. True/False: A statement that requires reasoning to explain.
4. Worked example: A step-by-step calculation or application problem.
5. Spot the error: A common misconception or a wrong solution that the student must identify.

For EACH card, provide:
- question: The question text.
- answer: The correct answer or explanation.
- hint: A one-line hint to help if the student is stuck.
- card_type: The type (Q&A, Fill-in-the-blank, True/False, Worked example, Spot the error).
- difficulty: One of [Easy, Medium, Hard].

OUTPUT FORMAT:
Return ONLY a valid JSON list of objects. Do not include markdown formatting or extra text.
Example format:
[
  {{"question": "...", "answer": "...", "hint": "...", "card_type": "...", "difficulty": "..."}},
  ...
]
"""
        response = self.client_genai.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt
        )

        clean_json = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean_json)

    def _quality_check(self, cards: List[Dict], subject: str) -> List[Dict]:
        """Second pass: Gemini scores the cards for clarity, correctness, and exam alignment."""
        if not cards:
            return []

        prompt = f"""
Review these generated flashcards for a {subject} exam.
Score each card from 1 to 5 on:
1. clarity (is the question unambiguous?)
2. correctness (is the answer factually right?)
3. exam_alignment (would this appear in a real exam?)

Return ONLY a JSON list where each object contains the original card and a 'score' field.
If a card's average score is below 3, mark it as 'reject: true'.

Example format:
[
  {{"card": {{"question": "...", ...}}, "score": 5, "reject": false}},
  ...
]

CARDS TO REVIEW:
{json.dumps(cards)}
"""
        response = self.client_genai.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt
        )

        clean_json = response.text.strip().replace("```json", "").replace("```", "")
        reviewed_cards = json.loads(clean_json)

        return [item["card"] for item in reviewed_cards if not item.get("reject", False)]

    async def generate_flashcards(
        self,
        topic: str,
        chapter: str,
        board: str,
        subject: str,
        page_range: Optional[tuple[int, int]] = None
    ) -> List[Dict]:
        """The main orchestration function."""
        print(f"Starting generation for topic: {topic}")

        # 1. Retrieve
        chunks = self.retrieval_service.get_relevant_chunks(
            topic=topic,
            chapter=chapter,
            board=board,
            subject=subject,
            page_range=page_range
        )

        if not chunks:
            print(f"No relevant chunks found. Cannot generate cards.")
            return []

        print(f"Retrieved {len(chunks)} chunks. Generating cards...")

        # 2. Generate
        exam_pattern = self._get_exam_pattern(subject, board)
        raw_cards = self._generate_cards_from_chunks(
            topic, chapter, board, subject, chunks, exam_pattern
        )
        print(f"Generated {len(raw_cards)} raw cards.")

        # 3. Quality Check
        print(f"Running quality check pass...")
        approved_cards = self._quality_check(raw_cards, subject)
        print(f"Quality check passed. {len(approved_cards)} cards approved.")

        if not approved_cards:
            print(f"No cards passed the quality check.")
            return []

        # 4. Save to Supabase
        print(f"Saving {len(approved_cards)} cards to Supabase...")

        saved_cards = []
        for card in approved_cards:
            card_id = str(uuid.uuid4())
            payload = {
                "card_id": card_id,
                "topic": topic,
                "chapter": chapter,
                "board": board,
                "subject": subject,
                "question": card["question"],
                "answer": card["answer"],
                "hint": card["hint"],
                "card_type": card["card_type"],
                "difficulty": card["difficulty"],
            }

            try:
                self.supabase.table("cards").insert(payload).execute()
                print(f"   Saved: {card['card_type']} card")
                # Add metadata to the returned object
                card_with_id = card.copy()
                card_with_id["id"] = card_id
                saved_cards.append(card_with_id)
            except Exception as e:
                print(f"   Supabase Error: {e}")

        print("Card generation complete!")
        return saved_cards
