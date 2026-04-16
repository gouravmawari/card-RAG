import datetime
from typing import Dict, Any, Optional
from app.db.supabase import get_supabase

class SchedulerService:
    """
    Handles Spaced Repetition logic using the FSRS (Free Spaced Repetition Scheduler) principles.
    This service manages card stability, difficulty, and calculates next review dates.
    """

    def __init__(self):
        self.supabase = get_supabase()
        # FSRS Default Parameters (Simplified version for implementation)
        # In a full implementation, these would be optimized via training
        self.W = {
            "w0": 0.4, "w1": 0.6, "w2": 1.3, "w3": -0.1, "w4": 0.1,
            "w5": -0.1, "w6": 0.1, "w7": 0.1, "w8": 0.1, "w9": 0.1
        }

    async def get_due_cards(self, user_id: str, limit: int = 50) -> list[Dict[str, Any]]:
        """
        Fetches cards that are due for review OR brand new (next_review is NULL).
        """
        now = datetime.datetime.now().isoformat()

        # 1. Get cards where next_review <= now (Overdue)
        overdue_res = self.supabase.table("cards") \
            .select("*") \
            .lte("next_review", now) \
            .limit(limit) \
            .execute()

        overdue_cards = overdue_res.data

        # 2. Get cards where next_review IS NULL (Brand New)
        # We limit this to ensure we don't grab the whole database if it's huge
        new_res = self.supabase.table("cards") \
            .select("*") \
            .is_("next_review", "null") \
            .limit(limit) \
            .execute()

        new_cards = new_res.data

        # Combine and return
        # In a real app, you might want to prioritize overdue cards over new ones
        return overdue_cards + new_cards

    async def process_review(
        self,
        card_id: str,
        rating: int,
        user_id: str,
        review_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Processes a user's review of a card.
        rating: 1 (Again), 2 (Hard), 3 (Good), 4 (Easy)
        """
        # 1. Fetch current card state
        card_res = self.supabase.table("cards").select("*").eq("card_id", card_id).single().execute()
        if not card_res.data:
            raise ValueError(f"Card {card_id} not found.")

        card = card_res.data

        # 2. Calculate new FSRS parameters
        # For this implementation, we'll use a simplified version of the FSRS update
        # Real FSRS involves updating Stability (S) and Difficulty (D)

        # Handle cases where stability/difficulty might be null (for brand new cards)
        current_stability = card.get("stability") if card.get("stability") is not None else 1.0
        current_difficulty = card.get("difficulty_score") if card.get("difficulty_score") is not None else 5.0

        # Simplified Update Logic
        if rating == 1: # Again
            new_stability = current_stability * 0.5
            new_difficulty = min(10.0, current_difficulty + 0.5)
        elif rating == 2: # Hard
            new_stability = current_stability * 0.8
            new_difficulty = min(10.0, current_difficulty + 0.2)
        elif rating == 3: # Good
            new_stability = current_stability * 1.5
            new_difficulty = max(1.0, current_difficulty - 0.1)
        else: # 4: Easy
            new_stability = current_stability * 2.5
            new_difficulty = max(1.0, current_difficulty - 0.3)

        # 3. Calculate next review date
        # Stability is measured in days
        next_review_date = datetime.datetime.now() + datetime.timedelta(days=new_stability)

        # 4. Update Supabase
        update_payload = {
            "stability": new_stability,
            "difficulty_score": new_difficulty,
            "last_review": datetime.datetime.now().isoformat(),
            "next_review": next_review_date.isoformat()
        }

        # Also log the review in a 'user_reviews' table if it exists
        if review_data:
            review_log = {
                **review_data,
                "card_id": card_id,
                "rating": rating,
                "reviewed_at": datetime.datetime.now().isoformat()
            }
            self.supabase.table("user_reviews").insert(review_log).execute()

        # Update the card
        self.supabase.table("cards").update(update_payload).eq("card_id", card_id).execute()

        return {
            "card_id": card_id,
            "new_stability": new_stability,
            "new_difficulty": new_difficulty,
            "next_review": next_review_date.isoformat()
        }
