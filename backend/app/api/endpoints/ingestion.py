import uuid
import shutil
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form, Depends, Request
from app.services.ingestion_service import IngestionService
from app.core.security import get_current_user_id
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.supabase import get_supabase

router = APIRouter()
ingestion_service = IngestionService()


# 25 MB — NCERT chapter PDFs are typically 2–15 MB.
MAX_PDF_BYTES = 25 * 1024 * 1024


def _clean_text(value: str, field: str, max_len: int = 120) -> str:
    v = (value or "").strip()
    if not v:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    if len(v) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} too long (max {max_len} chars).")
    # Defensive: strip control chars.
    return "".join(c for c in v if c.isprintable())


@router.post("/pdf")
@limiter.limit("10/day")
async def ingest_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subject: str = Form(...),
    board: str = Form(...),
    chapter: str = Form(...),
    klass: str = Form("NA"),
    user_id: str = Depends(get_current_user_id),
):
    """Upload a PDF and queue it for ingestion."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are supported.")

    # Magic-byte check: real PDFs start with %PDF-
    head = await file.read(5)
    if head != b"%PDF-":
        raise HTTPException(status_code=400, detail="File is not a valid PDF.")

    subject = _clean_text(subject, "subject")
    board = _clean_text(board, "board")
    chapter = _clean_text(chapter, "chapter")
    klass = _clean_text(klass, "klass", max_len=10)

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    source_id = f"src_{uuid.uuid4().hex[:12]}"
    safe_name = "".join(c for c in file.filename if c.isalnum() or c in (".", "-", "_"))[:120] or "upload.pdf"
    saved_path = settings.UPLOAD_DIR / f"{source_id}_{safe_name}"

    # Stream to disk with a running size cap.
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
        "user_id": user_id,
        "file_url": str(saved_path),
        "file_name": safe_name,
        "source_type": "user_upload",
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
        "message": "PDF uploaded. Processing will run in the background.",
    }


@router.get("/sources/{source_id}")
@limiter.limit("120/minute")
async def get_source_status(request: Request, source_id: str, user_id: str = Depends(get_current_user_id)):
    if len(source_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid source_id.")
    supabase = get_supabase()
    result = supabase.table("sources").select("*").eq("source_id", source_id).eq("user_id", user_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Source not found.")
    return result.data[0]


@router.get("/sources")
@limiter.limit("60/minute")
async def list_user_sources(request: Request, user_id: str = Depends(get_current_user_id)):
    supabase = get_supabase()
    result = (
        supabase.table("sources")
        .select("source_id, file_name, subject, board, chapter, status, chunk_count, topics, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"sources": result.data}
