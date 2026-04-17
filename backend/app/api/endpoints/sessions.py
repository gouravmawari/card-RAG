from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.services.session_service import SessionService
from app.core.security import get_current_user_id

router = APIRouter()
session_service = SessionService()


class CreateSessionRequest(BaseModel):
    source_id: str
    num_cards: int = Field(ge=1, le=15)
    scheduled_for: Optional[str] = None  # ISO 8601 string; null = immediately
    focus_topics: Optional[List[str]] = None


class AnswerRequest(BaseModel):
    card_id: str
    user_answer: Optional[str] = None
    used_hint: bool = False
    is_skipped: bool = False


# ---------- lifecycle ----------


@router.post("/create")
async def create_session(req: CreateSessionRequest, user_id: str = Depends(get_current_user_id)):
    try:
        s = await session_service.create_session(
            user_id=user_id,
            source_id=req.source_id,
            num_cards=req.num_cards,
            scheduled_for=req.scheduled_for,
            focus_topics=req.focus_topics,
        )
        return {"status": "created", "session": s}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/start")
async def start_session(session_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        result = await session_service.start_session(session_id, user_id)
        return {"status": "started", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/answer")
async def answer_card(session_id: str, req: AnswerRequest, user_id: str = Depends(get_current_user_id)):
    try:
        result = await session_service.record_answer(
            session_id=session_id,
            user_id=user_id,
            card_id=req.card_id,
            user_answer=req.user_answer,
            used_hint=req.used_hint,
            is_skipped=req.is_skipped,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/finalize")
async def finalize_session(session_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        result = await session_service.finalize_session(session_id, user_id)
        return {"status": "completed", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- reads ----------


@router.get("/{session_id}")
async def get_session(session_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        return await session_service.get_session(session_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("")
async def list_sessions(status: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    sessions = await session_service.list_sessions(user_id, status=status)
    return {"sessions": sessions}


@router.get("/history/hinted-cards")
async def hinted_cards(user_id: str = Depends(get_current_user_id)):
    cards = await session_service.get_hinted_cards(user_id)
    return {"count": len(cards), "cards": cards}


@router.get("/history/skipped-cards")
async def skipped_cards(user_id: str = Depends(get_current_user_id)):
    cards = await session_service.get_skipped_cards(user_id)
    return {"count": len(cards), "cards": cards}


@router.get("/history/weak-topics")
async def weak_topics(limit: int = 10, user_id: str = Depends(get_current_user_id)):
    topics = await session_service.get_weak_topics(user_id, limit=limit)
    return {"topics": topics}
