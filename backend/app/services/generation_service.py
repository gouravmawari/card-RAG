import json
import uuid
from pathlib import Path
from typing import List, Dict, Optional

from google import genai
from app.core.config import settings
from app.db.supabase import get_supabase
from app.services.retrieval_service import RetrievalService


VALID_CARD_TYPES = {"long_answer", "mcq", "true_false", "spot_the_error"}


class GenerationService:
    def __init__(self):
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.supabase = get_supabase()
        self.retrieval_service = RetrievalService()
        self.exam_pattern_dir = settings.BASE_DIR / "data" / "exam_patterns"

    # ---------- public: session-aware entry point ----------

    async def generate_for_session(
        self,
        session_id: str,
        user_id: str,
        source_id: str,
        num_cards: int,
        focus_topics: Optional[List[str]] = None,
    ) -> List[Dict]:
        src = self.supabase.table("sources").select("*").eq("source_id", source_id).limit(1).execute()
        if not src.data:
            raise ValueError("Source not found.")
        source = src.data[0]
        subject = source.get("subject") or "General"
        board = source.get("board") or "CBSE"
        chapter = source.get("chapter") or ""
        pdf_topics = source.get("topics") or []

        # Pick topics: focus_topics first, then fill with random pdf topics, fallback to chapter
        chosen_topics: List[str] = []
        if focus_topics:
            chosen_topics.extend([t for t in focus_topics if t])
        remaining_pdf_topics = [t for t in pdf_topics if t not in chosen_topics]
        chosen_topics.extend(remaining_pdf_topics[: max(0, 3 - len(chosen_topics))])
        if not chosen_topics:
            chosen_topics = [chapter or subject]

        # Retrieve chunks using the combined topics as one query
        topic_query = ", ".join(chosen_topics)
        chunks = self.retrieval_service.get_relevant_chunks(
            topic=topic_query,
            chapter=chapter,
            board=board,
            subject=subject,
            source_id=source_id,
        )
        if not chunks:
            print("[Generation] No chunks retrieved — cannot generate cards.")
            return []

        exam_pattern = self._get_exam_pattern(subject, board)
        distribution = self._type_distribution(num_cards)
        raw_cards = self._generate_cards(
            topics=chosen_topics,
            chapter=chapter,
            board=board,
            subject=subject,
            chunks=chunks,
            exam_pattern=exam_pattern,
            distribution=distribution,
        )
        if not raw_cards:
            return []

        approved_cards = self._quality_check(raw_cards, subject)
        if not approved_cards:
            print("[Generation] Quality check rejected all cards; using raw cards as fallback.")
            approved_cards = raw_cards

        # Persist to Supabase
        saved: List[Dict] = []
        for c in approved_cards:
            ct = c.get("card_type")
            if ct not in VALID_CARD_TYPES:
                continue

            card_id = f"card_{uuid.uuid4().hex[:16]}"
            payload = {
                "card_id": card_id,
                "session_id": session_id,
                "user_id": user_id,
                "source_id": source_id,
                "question": c.get("question", ""),
                "answer": c.get("answer", ""),
                "hint": c.get("hint"),
                "card_type": ct,
                "difficulty": c.get("difficulty"),
                "topic": c.get("topic") or chosen_topics[0],
                "chapter": chapter,
                "board": board,
                "subject": subject,
                "options": c.get("options") if ct == "mcq" else None,
            }
            try:
                self.supabase.table("cards").insert(payload).execute()
                saved.append({**payload})
            except Exception as e:
                print(f"[Generation] Supabase insert failed for a card: {e}")

        print(f"[Generation] Saved {len(saved)} cards for session {session_id}.")
        return saved

    # ---------- internals ----------

    def _type_distribution(self, n: int) -> Dict[str, int]:
        """Target ~40% long_answer, 20% each of mcq/true_false/spot_the_error."""
        long_n = max(1, round(n * 0.4))
        remaining = n - long_n
        mcq_n = max(1, round(remaining / 3)) if remaining >= 3 else max(0, remaining)
        tf_n = max(1, round(remaining / 3)) if remaining >= 2 else max(0, remaining - mcq_n)
        spot_n = max(0, n - long_n - mcq_n - tf_n)
        # Normalize to exactly n
        total = long_n + mcq_n + tf_n + spot_n
        if total != n:
            long_n += (n - total)
        return {"long_answer": long_n, "mcq": mcq_n, "true_false": tf_n, "spot_the_error": spot_n}

    def _get_exam_pattern(self, subject: str, board: str) -> str:
        f = self.exam_pattern_dir / f"{subject.lower()}_{board.lower()}.md"
        if f.exists():
            return f.read_text(encoding="utf-8")
        return "No specific exam pattern provided. Use clear, standard educational phrasing."

    def _generate_cards(
        self,
        topics: List[str],
        chapter: str,
        board: str,
        subject: str,
        chunks: List[str],
        exam_pattern: str,
        distribution: Dict[str, int],
    ) -> List[Dict]:
        context_text = "\n\n".join(chunks)
        dist_lines = [f"- {k}: {v}" for k, v in distribution.items() if v > 0]
        topics_str = ", ".join(topics)

        prompt = f"""You are an expert {board} {subject} question-setter.
Use ONLY the textbook context below to generate flashcards covering these topics: {topics_str}.
Chapter: {chapter}

### TEXTBOOK CONTEXT
{context_text}

### EXAM STYLE NOTES
{exam_pattern}

### CARD TYPE BUDGET (generate EXACTLY these counts)
{chr(10).join(dist_lines)}

### RULES
- long_answer: descriptive question requiring 2-5 sentences to answer.
- spot_the_error: give a short paragraph or statement with one factual error; user must identify and correct it.
- mcq: provide 4 options (A-D), exactly one correct.
- true_false: a single factual statement; answer is "True" or "False".
- hint: one short sentence. Always include.
- difficulty: one of Easy, Medium, Hard.
- topic: set to one of the requested topics.

### OUTPUT FORMAT (JSON array; no prose, no code fences)
Each element must match one of these shapes:

long_answer / spot_the_error:
{{"card_type": "long_answer"|"spot_the_error", "question": "...", "answer": "...", "hint": "...", "difficulty": "...", "topic": "..."}}

mcq:
{{"card_type": "mcq", "question": "...", "options": {{"choices": ["A: ...", "B: ...", "C: ...", "D: ..."], "correct": "B"}}, "answer": "B: ...", "hint": "...", "difficulty": "...", "topic": "..."}}

true_false:
{{"card_type": "true_false", "question": "...", "answer": "True"|"False", "hint": "...", "difficulty": "...", "topic": "..."}}
"""
        response = self.client_genai.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        text = (response.text or "").strip().replace("```json", "").replace("```", "").strip()
        try:
            cards = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"[Generation] Failed to parse JSON from LLM: {e}. First 200 chars: {text[:200]}")
            return []

        # Filter / sanitize
        cleaned = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            ct = c.get("card_type")
            if ct not in VALID_CARD_TYPES:
                continue
            if not c.get("question") or not c.get("answer"):
                continue
            cleaned.append(c)
        return cleaned

    def _quality_check(self, cards: List[Dict], subject: str) -> List[Dict]:
        if not cards:
            return []

        prompt = f"""You are QA-checking {subject} flashcards. For EACH card below, give it a 1-5 score on overall
quality (clarity, correctness, exam-alignment). Return ONLY a JSON array where each item has:
  {{"index": <int>, "score": <1-5>, "reject": <bool>}}

Set reject=true if score < 3.

CARDS:
{json.dumps(cards, indent=2)}
"""
        try:
            response = self.client_genai.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
            )
            text = (response.text or "").strip().replace("```json", "").replace("```", "").strip()
            reviews = json.loads(text)
        except Exception as e:
            print(f"[Generation] QA pass failed; keeping all cards. err={e}")
            return cards

        approved = []
        for r in reviews:
            idx = r.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(cards):
                continue
            if r.get("reject"):
                continue
            approved.append(cards[idx])
        return approved
