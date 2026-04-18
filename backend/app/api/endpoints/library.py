import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import get_current_admin, get_current_user_id
from app.db.supabase import get_supabase
from app.services.ingestion_service import IngestionService

router = APIRouter()
ingestion_service = IngestionService()

MAX_PDF_BYTES = 25 * 1024 * 1024
QDRANT_COLLECTION = "ncert_chunks"


def _clean_text(value: str, field: str, max_len: int = 120) -> str:
    v = (value or "").strip()
    if not v:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    if len(v) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} too long (max {max_len} chars).")
    return "".join(c for c in v if c.isprintable())


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


@router.post("")
@limiter.limit("30/hour")
async def upload_library_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subject: str = Form(...),
    board: str = Form(...),
    chapter: str = Form(...),
    klass: str = Form("NA"),
    admin_user_id: str = Depends(get_current_admin),
):
    """Admin-only: upload a PDF into the shared system library.
    Runs ingestion in the background; the book appears in GET /library once status='completed'."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are supported.")

    head = await file.read(5)
    if head != b"%PDF-":
        raise HTTPException(status_code=400, detail="File is not a valid PDF.")

    subject = _clean_text(subject, "subject")
    board = _clean_text(board, "board")
    chapter = _clean_text(chapter, "chapter")
    klass = _clean_text(klass, "klass", max_len=10)

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    source_id = f"lib_{uuid.uuid4().hex[:12]}"
    safe_name = "".join(c for c in file.filename if c.isalnum() or c in (".", "-", "_"))[:120] or "upload.pdf"
    saved_path = settings.UPLOAD_DIR / f"{source_id}_{safe_name}"

    total = len(head)
    try:
        with saved_path.open("wb") as out:
            out.write(head)
            while True:
                chunk = await file.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    out.close()
                    saved_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_PDF_BYTES // (1024*1024)} MB.")
                out.write(chunk)
    finally:
        await file.close()

    supabase = get_supabase()
    supabase.table("sources").insert({
        "source_id": source_id,
        "user_id": None,
        "file_url": str(saved_path),
        "file_name": safe_name,
        "source_type": "system_library",
        "subject": subject,
        "board": board,
        "chapter": chapter,
        "status": "pending",
    }).execute()

    metadata = {"subject": subject, "board": board, "chapter": chapter, "class": klass}
    background_tasks.add_task(ingestion_service.ingest_pdf, source_id, str(saved_path), metadata)

    return {
        "status": "queued",
        "source_id": source_id,
        "file_name": safe_name,
        "message": "Library PDF uploaded. Ingestion running in background.",
    }


@router.delete("/{source_id}")
@limiter.limit("30/hour")
async def delete_library_book(
    request: Request,
    source_id: str,
    admin_user_id: str = Depends(get_current_admin),
):
    """Admin-only: remove a system library book — deletes Qdrant vectors,
    the PDF file on disk, and the sources row."""
    if not source_id or len(source_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid source_id.")

    supabase = get_supabase()
    row = (
        supabase.table("sources")
        .select("source_id, file_url, source_type")
        .eq("source_id", source_id)
        .eq("source_type", "system_library")
        .limit(1)
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="Library book not found.")
    file_url = row.data[0].get("file_url")

    qdrant = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
    try:
        qdrant.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(
                        key="source_id",
                        match=MatchValue(value=source_id),
                    )]
                )
            ),
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Vector store deletion failed.")

    if file_url:
        try:
            Path(file_url).unlink(missing_ok=True)
        except Exception:
            pass

    supabase.table("sources").delete().eq("source_id", source_id).eq("source_type", "system_library").execute()

    return {"status": "deleted", "source_id": source_id}
