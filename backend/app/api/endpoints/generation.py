from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from app.services.generation_service import GenerationService

router = APIRouter()
generation_service = GenerationService()

class GenerationRequest(BaseModel):
    topic: str
    chapter: str
    board: str
    subject: str
    page_range: Optional[tuple[int, int]] = None

@router.post("/cards")
async def generate_cards(request: GenerationRequest):
    """
    Generates new flashcards for a specific topic and chapter.
    """
    cards = await generation_service.generate_flashcards(
        topic=request.topic,
        chapter=request.chapter,
        board=request.board,
        subject=request.subject,
        page_range=request.page_range
    )

    if not cards:
        raise HTTPException(status_code=404, detail="No cards could be generated for the given topic.")

    return {
        "status": "success",
        "count": len(cards),
        "cards": cards
    }
