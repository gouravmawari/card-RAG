import os
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from supabase import create_client, Client

# Import our retrieval engine
from retrieval import get_relevant_chunks

load_dotenv()

# Configuration
EXAM_PATTERN_DIR = Path("data/exam_patterns")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Initialize clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Create the client once and reuse it
client_genai = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

def get_exam_pattern(subject: str, board: str) -> str:
    """Reads the exam pattern markdown file."""
    pattern_file = EXAM_PATTERN_DIR / f"{subject}_{board}.md"
    if pattern_file.exists():
        return pattern_file.read_text(encoding="utf-8")
    return "No specific exam pattern provided. Use standard educational style."

def generate_cards_from_chunks(topic: str, chapter: str, board: str, subject: str, chunks: list[str], exam_pattern: str) -> list[dict]:
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

    # Correct new syntax: client.models.generate_content
    response = client_genai.models.generate_content(
        model='gemini-2.5-flash-lite',
        contents=prompt
    )

    clean_json = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(clean_json)

def quality_check(cards: list[dict], subject: str) -> list[dict]:
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
    # Correct new syntax: client.models.generate_content
    response = client_genai.models.generate_content(
        model='gemini-2.5-flash-lite',
        contents=prompt
    )

    clean_json = response.text.strip().replace("```json", "").replace("```", "")
    reviewed_cards = json.loads(clean_json)

    return [item["card"] for item in reviewed_cards if not item.get("reject", False)]

def generate_and_save_cards(topic: str, chapter: str, board: str, subject: str):
    """The main orchestration function."""
    print(f"🚀 Starting generation for topic: {topic}")

    # 1. Retrieve
    chunks = get_relevant_chunks(topic, chapter, board, subject)
    if not chunks:
        print("❌ No relevant chunks found. Cannot generate cards.")
        return

    print(f"✅ Retrieved {len(chunks)} chunks. Generating cards...")

    # 2. Generate
    exam_pattern = get_exam_pattern(subject, board)
    raw_cards = generate_cards_from_chunks(topic, chapter, board, subject, chunks, exam_pattern)
    print(f"✨ Generated {len(raw_cards)} raw cards.")

    # 3. Quality Check
    print("🔍 Running quality check pass...")
    approved_cards = quality_check(raw_cards, subject)
    print(f"✅ Quality check passed. {len(approved_cards)} cards approved.")

    if not approved_cards:
        print("⚠️ No cards passed the quality check. Try again later.")
        return

    # 4. Save to Supabase (or fallback to file)
    print(f"💾 Saving {len(approved_cards)} cards to Supabase...")

    results_file = Path("data/result.txt")

    for card in approved_cards:
        payload = {
            "topic": topic,
            "chapter": chapter,
            "board": board,
            "subject": subject,
            "question": card["question"],
            "answer": card["answer"],
            "hint": card["hint"],
            "card_type": card["card_type"],
            "difficulty": card["difficulty"],
            "created_at": "now()"
        }

        try:
            res = supabase.table("cards").insert(payload).execute()
            # The new supabase-py returns an APIResponse object.
            # We check for success by looking at the response, not .error
            print(f"   Saved: {card['card_type']} card")
        except Exception as e:
            print(f"   Supabase Error: {e}. Writing to {results_file} instead.")
            with open(results_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")

    print("\n🎉 Card generation complete!")

if __name__ == "__main__":
    generate_and_save_cards(
        topic="pH scale",
        chapter="acids_bases_and_salts",
        board="cbse",
        subject="jesc102-1-10"
    )
