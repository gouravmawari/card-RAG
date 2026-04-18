from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Request, Query

from app.core.security import get_current_user_id
from app.core.rate_limit import limiter
from app.db.supabase import get_supabase

router = APIRouter()


@router.get("")
@limiter.limit("60/minute")
async def list_library(
    request: Request,
    board: Optional[str] = Query(default=None, max_length=40),
    subject: Optional[str] = Query(default=None, max_length=40),
    klass: Optional[str] = Query(default=None, max_length=10),
    _: str = Depends(get_current_user_id),
):
    """List all pre-loaded system library books. Available to any authenticated user."""
    q = (
        get_supabase()
        .table("sources")
        .select("source_id, file_name, subject, board, chapter, status, chunk_count, topics, created_at")
        .eq("source_type", "system_library")
        .eq("status", "completed")
    )
    if board:
        q = q.eq("board", board)
    if subject:
        q = q.eq("subject", subject)

    result = q.order("created_at", desc=True).execute()
    books = result.data or []
    return {"books": books}


@router.get("/{source_id}")
@limiter.limit("120/minute")
async def get_library_book(
    request: Request,
    source_id: str,
    _: str = Depends(get_current_user_id),
):
    """Details for one library book — includes the full extracted topics list
    so the frontend can populate a topic/chapter dropdown."""
    if len(source_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid source_id.")
    result = (
        get_supabase()
        .table("sources")
        .select("source_id, file_name, subject, board, chapter, status, chunk_count, topics, created_at")
        .eq("source_id", source_id)
        .eq("source_type", "system_library")
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Library book not found.")
    return result.data[0]
