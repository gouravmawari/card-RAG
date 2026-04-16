from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from app.services.scheduler_service import SchedulerService
import datetime

router = APIRouter()
scheduler_service = SchedulerService()

class ReviewRequest(BaseModel):
    card_id: str
    rating: int  # 1 (Again), 2 (Hard), 3 (Good), 4 (Easy)
    user_id: str
    metadata: Optional[Dict[str, Any]] = None

class DueCardsResponse(BaseModel):
    card_id: str
    question: str
    answer: str
    hint: str
    card_type: str
    difficulty: str
    topic: str
    chapter: str
    board: str
    subject: str

@router.get("/due", response_model=List[DueCardsResponse])
async def get_due_cards(user_id: str, limit: int = 50):
    """
    Fetches all cards due for review for a specific user.
    """
    # For now, we assume cards are associated with a user via a 'user_id' column.
    # If your schema doesn't have 'user_id' in 'cards', we might need to adjust this.
    # For this implementation, we'll assume 'user_id' is a column in 'cards' or we filter by it.

    # Note: In a real app, you'd filter by user_id.
    # Since 'cards' might not have 'user_id' yet, let's just get due cards for now.
    cards = await scheduler_service.get_due_cards(user_id, limit=limit)

    if not cards:
        return []

    return cards

@router.post("/submit")
async def submit_review(request: ReviewRequest):
    """
    Submits a review for a card and updates the next review date.
    """
    try:
        result = await scheduler_service.process_review(
            card_id=request.card_id,
            rating=request.rating,
            user_id=request.user_id,
            review_data=request.metadata
        )
        return {
            "status": "success",
            "data": result
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Review processing failed: {str(e)}")
