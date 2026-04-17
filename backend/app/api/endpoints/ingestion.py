import uuid
import shutil
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form, Depends
from app.services.ingestion_service import IngestionService
from app.core.security import get_current_user_id
from app.core.config import settings
from app.db.supabase import get_supabase

router = APIRouter()
ingestion_service = IngestionService()


@router.post("/pdf")
async def ingest_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subject: str = Form(...),
    board: str = Form(...),
    chapter: str = Form(...),
    klass: str = Form("NA"),
    user_id: str = Depends(get_current_user_id),
):
    """
    Upload a PDF (multipart/form-data) and queue it for ingestion.
    Returns the source_id immediately; poll sources.status to check progress.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are supported.")

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    source_id = f"src_{uuid.uuid4().hex[:12]}"
    saved_path = settings.UPLOAD_DIR / f"{source_id}_{file.filename}"

    try:
        with saved_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        file.file.close()

    # Create source row immediately so the client gets a handle; background task updates it.
    supabase = get_supabase()
    supabase.table("sources").insert({
        "source_id": source_id,
        "user_id": user_id,
        "file_url": str(saved_path),
        "file_name": file.filename,
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
        "file_name": file.filename,
        "message": "PDF uploaded. Processing will run in the background.",
    }


@router.get("/sources/{source_id}")
async def get_source_status(source_id: str, user_id: str = Depends(get_current_user_id)):
    """Check ingestion progress for a source."""
    supabase = get_supabase()
    result = supabase.table("sources").select("*").eq("source_id", source_id).eq("user_id", user_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Source not found.")
    return result.data[0]


@router.get("/sources")
async def list_user_sources(user_id: str = Depends(get_current_user_id)):
    """List all PDFs the caller has uploaded (for the 'past uploads' scroll)."""
    supabase = get_supabase()
    result = (
        supabase.table("sources")
        .select("source_id, file_name, subject, board, chapter, status, chunk_count, topics, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"sources": result.data}
