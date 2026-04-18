from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from app.services.session_service import SessionService
from app.core.security import get_current_user_id
from app.core.rate_limit import limiter

router = APIRouter()
session_service = SessionService()


class CreateSessionRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    num_cards: int = Field(ge=1, le=15)
    scheduled_for: Optional[str] = Field(default=None, max_length=40)
    focus_topics: Optional[List[str]] = Field(default=None, max_length=10)
    page_range: Optional[List[int]] = Field(default=None, min_length=2, max_length=2)


class AnswerRequest(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)
    user_answer: Optional[str] = Field(default=None, max_length=5000)
    used_hint: bool = False
    is_skipped: bool = False


@router.post("/create")
@limiter.limit("30/day")
async def create_session(request: Request, req: CreateSessionRequest, user_id: str = Depends(get_current_user_id)):
    try:
        s = await session_service.create_session(
            user_id=user_id,
            source_id=req.source_id,
            num_cards=req.num_cards,
            scheduled_for=req.scheduled_for,
            focus_topics=req.focus_topics,
            page_range=req.page_range,
        )
        return {"status": "created", "session": s}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/start")
@limiter.limit("30/day")
async def start_session(request: Request, session_id: str, user_id: str = Depends(get_current_user_id)):
    if len(session_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid session_id.")
    try:
        result = await session_service.start_session(session_id, user_id)
        return {"status": "started", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/answer")
@limiter.limit("120/minute")
async def answer_card(request: Request, session_id: str, req: AnswerRequest, user_id: str = Depends(get_current_user_id)):
    if len(session_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid session_id.")
    try:
        return await session_service.record_answer(
            session_id=session_id,
            user_id=user_id,
            card_id=req.card_id,
            user_answer=req.user_answer,
            used_hint=req.used_hint,
            is_skipped=req.is_skipped,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/finalize")
@limiter.limit("30/day")
async def finalize_session(request: Request, session_id: str, user_id: str = Depends(get_current_user_id)):
    if len(session_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid session_id.")
    try:
        result = await session_service.finalize_session(session_id, user_id)
        return {"status": "completed", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{session_id}")
@limiter.limit("120/minute")
async def get_session(request: Request, session_id: str, user_id: str = Depends(get_current_user_id)):
    if len(session_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid session_id.")
    try:
        return await session_service.get_session(session_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("")
@limiter.limit("60/minute")
async def list_sessions(request: Request, status: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    if status and status not in {"scheduled", "in_progress", "completed", "abandoned"}:
        raise HTTPException(status_code=400, detail="Invalid status filter.")
    sessions = await session_service.list_sessions(user_id, status=status)
    return {"sessions": sessions}


@router.get("/history/hinted-cards")
@limiter.limit("60/minute")
async def hinted_cards(request: Request, user_id: str = Depends(get_current_user_id)):
    cards = await session_service.get_hinted_cards(user_id)
    return {"count": len(cards), "cards": cards}


@router.get("/history/skipped-cards")
@limiter.limit("60/minute")
async def skipped_cards(request: Request, user_id: str = Depends(get_current_user_id)):
    cards = await session_service.get_skipped_cards(user_id)
    return {"count": len(cards), "cards": cards}


@router.get("/history/weak-topics")
@limiter.limit("60/minute")
async def weak_topics(request: Request, limit: int = 10, user_id: str = Depends(get_current_user_id)):
    limit = max(1, min(limit, 100))
    topics = await session_service.get_weak_topics(user_id, limit=limit)
    return {"topics": topics}
