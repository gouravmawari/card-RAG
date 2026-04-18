"""
Admin CLI to add a PDF to the system library.

Usage (run from project root):
    .venv/bin/python scripts/seed_library.py <pdf_path> \
        --subject Biology \
        --board CBSE \
        --chapter "Photosynthesis" \
        --klass 10

The PDF is:
  1. Copied to backend/data/uploads/<source_id>_<name>.pdf
  2. Registered in `public.sources` with source_type='system_library' (user_id=null)
  3. Ingested via the same pipeline as user uploads (extract → chunk → embed → Qdrant)

After this completes, the book is visible to every authenticated user via
GET /api/v1/library and usable as a session source_id.
"""
import sys
import os
import uuid
import asyncio
import argparse
import shutil
from pathlib import Path

# Make `app.*` importable regardless of where we're run from.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings
from app.db.supabase import get_supabase
from app.services.ingestion_service import IngestionService


async def seed(pdf_path: Path, subject: str, board: str, chapter: str, klass: str) -> None:
    if not pdf_path.exists():
        raise SystemExit(f"File not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise SystemExit("Only .pdf files are supported.")

    supabase = get_supabase()
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    source_id = f"lib_{uuid.uuid4().hex[:12]}"
    safe_name = "".join(c for c in pdf_path.name if c.isalnum() or c in (".", "-", "_"))[:120]
    dest = settings.UPLOAD_DIR / f"{source_id}_{safe_name}"
    shutil.copy2(pdf_path, dest)
    print(f"📚 copied to {dest}")

    supabase.table("sources").insert({
        "source_id": source_id,
        "user_id": None,
        "file_url": str(dest),
        "file_name": safe_name,
        "source_type": "system_library",
        "subject": subject,
        "board": board,
        "chapter": chapter,
        "status": "pending",
    }).execute()
    print(f"✅ registered source_id={source_id} as system_library")

    svc = IngestionService()
    try:
        result = await svc.ingest_pdf(
            source_id=source_id,
            pdf_path=str(dest),
            metadata={"subject": subject, "board": board, "chapter": chapter, "class": klass},
        )
        print(f"\n🎉 ingestion complete: {result}")
        print(f"\nBook is now live as source_id={source_id}")
    except Exception as e:
        print(f"\n❌ ingestion failed: {e}")
        print("The source row was marked status='failed'; you can retry with a different source_id.")
        raise


def main() -> None:
    p = argparse.ArgumentParser(description="Add a PDF to the system library.")
    p.add_argument("pdf", type=Path, help="Path to the PDF file.")
    p.add_argument("--subject", required=True, help="e.g. Biology, Physics, Math")
    p.add_argument("--board", required=True, help="e.g. CBSE, ICSE")
    p.add_argument("--chapter", required=True, help="Chapter name, e.g. 'Photosynthesis'")
    p.add_argument("--klass", default="NA", help="Class/grade, e.g. '10'")
    args = p.parse_args()

    asyncio.run(seed(args.pdf, args.subject, args.board, args.chapter, args.klass))


if __name__ == "__main__":
    main()
