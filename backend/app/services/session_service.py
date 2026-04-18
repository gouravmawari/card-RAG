import datetime
import json
from typing import Dict, List, Optional
from app.db.supabase import get_supabase
from app.services.generation_service import GenerationService
from google import genai
from app.core.config import settings


class SessionService:
    """
    Owns the lifecycle of a study session:
      create -> start (generate cards) -> answer (per card) -> finalize (LLM report)

    Retests re-use `create(source_id, focus_topics=...)` with weak topics
    pulled from user_topic_stats.
    """

    def __init__(self):
        self.supabase = get_supabase()
        self.generation_service = GenerationService()
        self.genai = genai.Client(api_key=settings.GOOGLE_API_KEY)

    # ---------- lifecycle ----------

    async def create_session(
        self,
        user_id: str,
        source_id: str,
        num_cards: int,
        scheduled_for: Optional[str] = None,
        focus_topics: Optional[List[str]] = None,
        page_range: Optional[List[int]] = None,
    ) -> Dict:
        if not (1 <= num_cards <= 15):
            raise ValueError("num_cards must be between 1 and 15.")
        if page_range is not None:
            if len(page_range) != 2 or page_range[0] < 1 or page_range[1] < page_range[0]:
                raise ValueError("page_range must be [from, to] with from>=1 and to>=from.")

        # Source must be completed. Caller must own it OR it's a system library book.
        src = self.supabase.table("sources").select("source_id, status, user_id, source_type").eq("source_id", source_id).limit(1).execute()
        if not src.data:
            raise ValueError("Source not found.")
        source = src.data[0]
        if source["status"] != "completed":
            raise ValueError(f"Source not ready (status={source['status']}).")
        is_library = source.get("source_type") == "system_library"
        if not is_library and source["user_id"] != user_id:
            raise ValueError("Source does not belong to this user.")

        creation_params: Dict = {}
        if focus_topics:
            creation_params["_focus_topics"] = focus_topics
        if page_range:
            creation_params["_page_range"] = page_range

        payload = {
            "user_id": user_id,
            "source_id": source_id,
            "num_cards": num_cards,
            "status": "scheduled",
            "scheduled_for": scheduled_for,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        if creation_params:
            payload["final_report_json"] = creation_params

        res = self.supabase.table("sessions").insert(payload).execute()
        return res.data[0]

    async def start_session(self, session_id: str, user_id: str) -> Dict:
        session = self._get_owned_session(session_id, user_id)
        if session["status"] not in ("scheduled", "in_progress"):
            raise ValueError(f"Session cannot be started (status={session['status']}).")

        # If already started and has cards, just return them.
        existing = self.supabase.table("cards").select("*").eq("session_id", session_id).execute()
        if existing.data:
            self._update_session(session_id, {"status": "in_progress"})
            return {"session_id": session_id, "cards": existing.data}

        # Pull creation params stashed at create time (focus_topics, page_range)
        frj = session.get("final_report_json") or {}
        focus_topics = frj.get("_focus_topics") if isinstance(frj, dict) else None
        page_range = frj.get("_page_range") if isinstance(frj, dict) else None

        # If no explicit focus, try weak topics for this source
        if not focus_topics:
            focus_topics = await self._suggest_focus_topics(user_id, session["source_id"])

        cards = await self.generation_service.generate_for_session(
            session_id=session_id,
            user_id=user_id,
            source_id=session["source_id"],
            num_cards=session["num_cards"],
            focus_topics=focus_topics,
            page_range=tuple(page_range) if page_range else None,
        )

        now = datetime.datetime.utcnow().isoformat()
        self._update_session(session_id, {"status": "in_progress", "started_at": now})
        return {"session_id": session_id, "cards": cards}

    async def record_answer(
        self,
        session_id: str,
        user_id: str,
        card_id: str,
        user_answer: Optional[str],
        used_hint: bool,
        is_skipped: bool,
    ) -> Dict:
        session = self._get_owned_session(session_id, user_id)
        if session["status"] != "in_progress":
            raise ValueError(f"Session is not in_progress (status={session['status']}).")

        card = self.supabase.table("cards").select("*").eq("card_id", card_id).eq("session_id", session_id).limit(1).execute()
        if not card.data:
            raise ValueError("Card not found in this session.")
        card = card.data[0]

        is_correct = None
        if not is_skipped and user_answer is not None:
            if card["card_type"] == "mcq":
                opts = card.get("options") or {}
                is_correct = (str(user_answer).strip().lower() == str(opts.get("correct", "")).strip().lower())
            elif card["card_type"] == "true_false":
                expected = str(card.get("answer", "")).strip().lower()
                is_correct = (str(user_answer).strip().lower() == expected)

        review_payload = {
            "user_id": user_id,
            "card_id": card_id,
            "session_id": session_id,
            "user_answer": user_answer,
            "used_hint": used_hint,
            "is_skipped": is_skipped,
            "is_correct": is_correct,
        }
        # Upsert-by-unique-index: try insert; if conflict, update
        try:
            self.supabase.table("user_reviews").insert(review_payload).execute()
        except Exception:
            self.supabase.table("user_reviews").update(review_payload).eq("user_id", user_id).eq("card_id", card_id).execute()

        return {"recorded": True, "is_correct": is_correct}

    async def finalize_session(self, session_id: str, user_id: str) -> Dict:
        session = self._get_owned_session(session_id, user_id)
        if session["status"] == "completed":
            return {"session_id": session_id, "report": session.get("final_report_json")}

        cards = self.supabase.table("cards").select("*").eq("session_id", session_id).execute().data or []
        reviews = self.supabase.table("user_reviews").select("*").eq("session_id", session_id).execute().data or []
        reviews_by_card = {r["card_id"]: r for r in reviews}

        # Build per-card analysis. Only long-answer + spot_the_error get LLM-graded.
        long_answer_items = []
        simple_results = []
        for c in cards:
            r = reviews_by_card.get(c["card_id"])
            if c["card_type"] in ("long_answer", "spot_the_error"):
                long_answer_items.append((c, r))
            else:
                simple_results.append({
                    "card_id": c["card_id"],
                    "question": c["question"],
                    "correct_answer": c["answer"],
                    "user_answer": (r or {}).get("user_answer"),
                    "is_skipped": (r or {}).get("is_skipped", True),
                    "used_hint": (r or {}).get("used_hint", False),
                    "is_correct": (r or {}).get("is_correct"),
                    "card_type": c["card_type"],
                    "topic": c.get("topic"),
                })

        graded_long = self._grade_long_answers(long_answer_items) if long_answer_items else []

        # Assemble report
        report = {
            "simple_results": simple_results,
            "long_answer_results": graded_long,
            "summary": self._build_summary_text(simple_results, graded_long),
            "generated_at": datetime.datetime.utcnow().isoformat(),
        }

        # Update user_topic_stats counters
        self._bump_topic_stats(user_id, cards, reviews_by_card, graded_long)

        self._update_session(session_id, {
            "status": "completed",
            "completed_at": datetime.datetime.utcnow().isoformat(),
            "final_report_json": report,
        })

        return {"session_id": session_id, "report": report}

    # ---------- reads ----------

    async def get_session(self, session_id: str, user_id: str) -> Dict:
        session = self._get_owned_session(session_id, user_id)
        cards = self.supabase.table("cards").select("*").eq("session_id", session_id).execute().data
        reviews = self.supabase.table("user_reviews").select("*").eq("session_id", session_id).execute().data
        return {"session": session, "cards": cards, "reviews": reviews}

    async def list_sessions(self, user_id: str, status: Optional[str] = None) -> List[Dict]:
        q = self.supabase.table("sessions").select("*").eq("user_id", user_id).order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        return q.execute().data or []

    async def get_hinted_cards(self, user_id: str) -> List[Dict]:
        """All cards the user ever clicked hint on — one indexed query."""
        reviews = self.supabase.table("user_reviews").select("card_id").eq("user_id", user_id).eq("used_hint", True).execute().data or []
        ids = [r["card_id"] for r in reviews]
        if not ids:
            return []
        return self.supabase.table("cards").select("*").in_("card_id", ids).execute().data or []

    async def get_skipped_cards(self, user_id: str) -> List[Dict]:
        reviews = self.supabase.table("user_reviews").select("card_id").eq("user_id", user_id).eq("is_skipped", True).execute().data or []
        ids = [r["card_id"] for r in reviews]
        if not ids:
            return []
        return self.supabase.table("cards").select("*").in_("card_id", ids).execute().data or []

    async def get_weak_topics(self, user_id: str, limit: int = 10) -> List[Dict]:
        rows = self.supabase.table("user_topic_stats").select("*").eq("user_id", user_id).order("incorrect_count", desc=True).limit(limit).execute().data or []
        return rows

    # ---------- internals ----------

    def _get_owned_session(self, session_id: str, user_id: str) -> Dict:
        res = self.supabase.table("sessions").select("*").eq("session_id", session_id).limit(1).execute()
        if not res.data:
            raise ValueError("Session not found.")
        s = res.data[0]
        if s["user_id"] != user_id:
            raise ValueError("Session does not belong to this user.")
        return s

    def _update_session(self, session_id: str, fields: Dict) -> None:
        self.supabase.table("sessions").update(fields).eq("session_id", session_id).execute()

    async def _suggest_focus_topics(self, user_id: str, source_id: str) -> Optional[List[str]]:
        """Pick weak topics that also appear in this PDF. Returns None if none found."""
        src = self.supabase.table("sources").select("topics").eq("source_id", source_id).limit(1).execute()
        if not src.data:
            return None
        pdf_topics = set(src.data[0].get("topics") or [])
        if not pdf_topics:
            return None
        weak = await self.get_weak_topics(user_id, limit=20)
        overlap = [w["topic"] for w in weak if w["topic"] in pdf_topics][:3]
        return overlap or None

    def _grade_long_answers(self, items: List) -> List[Dict]:
        """One LLM call to grade all long-answer/spot_the_error cards."""
        payload = []
        for c, r in items:
            payload.append({
                "card_id": c["card_id"],
                "question": c["question"],
                "correct_answer": c["answer"],
                "card_type": c["card_type"],
                "topic": c.get("topic"),
                "user_answer": (r or {}).get("user_answer"),
                "is_skipped": (r or {}).get("is_skipped", True),
                "used_hint": (r or {}).get("used_hint", False),
            })

        prompt = f"""You are a kind, encouraging teacher grading student answers.
For EACH item below, produce a JSON object with:
  - card_id: echo the card_id
  - depth: integer 1-10 (how thoroughly did the student explore the idea?)
  - thinking: integer 1-10 (quality of reasoning, logic, structure)
  - correctness: integer 1-10 (factual accuracy vs. correct_answer)
  - feedback_text: 1-2 sentences, warm and specific, point out a strength + one thing to work on
If is_skipped is true, set depth/thinking/correctness to 0 and give gentle encouragement.
If used_hint is true, DO NOT deduct — just note they used a hint.

Return ONLY a JSON array, no prose or code fences.

ITEMS:
{json.dumps(payload, indent=2)}
"""
        response = self.genai.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Fallback: return empty ratings so session can still complete
            return [{"card_id": it[0]["card_id"], "depth": 0, "thinking": 0, "correctness": 0,
                     "feedback_text": "Automatic grading failed; please try again."} for it in items]

    def _build_summary_text(self, simple_results: List[Dict], long_results: List[Dict]) -> str:
        total = len(simple_results) + len(long_results)
        if total == 0:
            return "No answers recorded."
        correct_simple = sum(1 for r in simple_results if r.get("is_correct"))
        avg_long = 0.0
        if long_results:
            avg_long = sum(
                (r.get("depth", 0) + r.get("thinking", 0) + r.get("correctness", 0)) / 3
                for r in long_results
            ) / len(long_results)
        hints_used = sum(1 for r in simple_results if r.get("used_hint")) + sum(
            1 for r in long_results if r.get("used_hint")
        )
        parts = []
        if simple_results:
            parts.append(f"You got {correct_simple}/{len(simple_results)} objective questions right.")
        if long_results:
            parts.append(f"Your long-answer score averaged {avg_long:.1f}/10 across depth/thinking/correctness.")
        if hints_used:
            parts.append(f"You used a hint on {hints_used} question(s) — that's a healthy way to learn.")
        parts.append("Great job showing up — keep going!")
        return " ".join(parts)

    def _bump_topic_stats(self, user_id: str, cards: List[Dict], reviews_by_card: Dict, graded_long: List[Dict]) -> None:
        """Increment counters in user_topic_stats per topic."""
        grade_by_id = {g["card_id"]: g for g in graded_long}
        deltas: Dict[str, Dict[str, int]] = {}

        for c in cards:
            topic = (c.get("topic") or "General").strip()
            if topic not in deltas:
                deltas[topic] = {"correct": 0, "incorrect": 0, "skipped": 0, "hinted": 0}

            r = reviews_by_card.get(c["card_id"])
            if not r:
                deltas[topic]["skipped"] += 1
                continue

            if r.get("used_hint"):
                deltas[topic]["hinted"] += 1

            if r.get("is_skipped"):
                deltas[topic]["skipped"] += 1
                continue

            if c["card_type"] in ("mcq", "true_false"):
                if r.get("is_correct"):
                    deltas[topic]["correct"] += 1
                else:
                    deltas[topic]["incorrect"] += 1
            else:
                g = grade_by_id.get(c["card_id"], {})
                avg = (g.get("depth", 0) + g.get("thinking", 0) + g.get("correctness", 0)) / 3.0
                if avg >= 6:
                    deltas[topic]["correct"] += 1
                else:
                    deltas[topic]["incorrect"] += 1

        now = datetime.datetime.utcnow().isoformat()
        for topic, d in deltas.items():
            existing = self.supabase.table("user_topic_stats").select("*").eq("user_id", user_id).eq("topic", topic).limit(1).execute()
            if existing.data:
                row = existing.data[0]
                self.supabase.table("user_topic_stats").update({
                    "correct_count": row["correct_count"] + d["correct"],
                    "incorrect_count": row["incorrect_count"] + d["incorrect"],
                    "skipped_count": row["skipped_count"] + d["skipped"],
                    "hinted_count": row["hinted_count"] + d["hinted"],
                    "last_seen_at": now,
                }).eq("user_id", user_id).eq("topic", topic).execute()
            else:
                self.supabase.table("user_topic_stats").insert({
                    "user_id": user_id,
                    "topic": topic,
                    "correct_count": d["correct"],
                    "incorrect_count": d["incorrect"],
                    "skipped_count": d["skipped"],
                    "hinted_count": d["hinted"],
                    "last_seen_at": now,
                }).execute()
