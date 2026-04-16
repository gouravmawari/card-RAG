from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Dict, Optional
from app.services.ingestion_service import IngestionService
import os

router = APIRouter()
ingestion_service = IngestionService()

class IngestionRequest(BaseModel):
    file_path: str
    metadata: Dict

@router.post("/pdf")
async def ingest_pdf(request: IngestionRequest, background_tasks: BackgroundTasks):
    """
    Triggers PDF ingestion in the background.
    """
    if not os.path.exists(request.file_path):
        raise HTTPException(status_code=404, detail="File not found.")

    # We run this in the background so the API responds immediately
    background_tasks.add_task(ingestion_service.ingest_pdf, request.file_path, request.metadata)

    return {
        "status": "processing",
        "message": f"Ingestion for {os.path.basename(request.file_path)} has been queued."
    }
