import os
import uuid
from pathlib import Path
from typing import List, Dict, Optional

from google import genai
from app.core.config import settings
from app.db.supabase import get_supabase
from app.services.retrieval_service import RetrievalService

# We will import the logic from the existing scripts to avoid duplication
# Note: In a real production environment, we would refactor these scripts
# into a shared library, but for now, we'll adapt the logic.
import fitz  # PyMuPDF
import pdfplumber
import re
from rank_bm25 import BM25Okapi
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

class IngestionService:
    def __init__(self):
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.supabase = get_supabase()
        self.qdrant = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
        )
        self.MARGIN_PERCENT = 0.07

    def _extract_text_and_tables(self, pdf_path: str) -> str:
        """Ported logic from scripts/parse_pdf.py"""
        doc = fitz.open(pdf_path)
        full_text = []

        for page_num, page in enumerate(doc, start=1):
            page_height = page.rect.height
            content_top = page_height * self.MARGIN_PERCENT
            content_bottom = page_height * (1 - self.MARGIN_PERCENT)

            # 1. Extract Tables
            table_text = ""
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    pdf_page = pdf.pages[page_num - 1]
                    tables = pdf_page.extract_tables()
                    for table in tables:
                        table_text += "\n[TABLE START]\n"
                        for row in table:
                            clean_row = [str(cell).replace('\n', ' ') if cell else "" for cell in row]
                            table_text += "| " + " | ".join(clean_row) + " |\n"
                        table_text += "[TABLE END]\n"
            except Exception as e:
                print(f"      Warning: Table extraction failed on page {page_num}: {e}")

            # 2. Extract Text
            page_dict = page.get_text("dict")
            page_content = []
            all_sizes = []
            for block in page_dict["blocks"]:
                if "lines" in block:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            all_sizes.append(span["size"])

            avg_size = sum(all_sizes) / len(all_sizes) if all_sizes else 12
            heading_threshold = avg_size * 1.2

            for block in page_dict["blocks"]:
                if block["type"] != 0: continue
                bbox = block["bbox"]
                block_center_y = (bbox[1] + bbox[3]) / 2
                if block_center_y < content_top or block_center_y > content_bottom:
                    continue

                block_parts = []
                is_heading = False
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text: continue
                        if span["size"] > heading_threshold:
                            is_heading = True
                        if re.fullmatch(r"[-_=\s]+", text): continue
                        block_parts.append(text)

                if block_parts:
                    combined = " ".join(block_parts)
                    if is_heading:
                        page_content.append(f"\n### {combined} ###\n")
                    else:
                        page_content.append(combined)

            page_output = "\n".join(page_content)
            if table_text:
                page_output += f"\n{table_text}"

            if page_output.strip():
                full_text.append(f"[PAGE:{page_num}]\n{page_output}")

        doc.close()
        return "\n\n".join(full_text)

    def _chunk_text(self, text: str, metadata: dict) -> List[Dict]:
        """Ported logic from scripts/chunk.py"""
        # Simplified chunker for the service
        # We split by [PAGE:N] and then by paragraphs
        blocks = re.split(r"\[PAGE:(\d+)\]", text)

        chunks = []
        chunk_id_counter = 0

        # blocks will look like ['', '1', 'text...', '2', 'text...']
        for i in range(1, len(blocks), 2):
            page_num = int(blocks[i])
            content = blocks[i+1].strip()

            if not content:
                continue

            # Split into paragraphs
            paragraphs = content.split("\n\n")

            current_chunk_words = []
            current_chunk_page = page_num

            for para in paragraphs:
                para_words = para.split()

                # If adding this para exceeds ~300 words, flush the chunk
                if len(current_chunk_words) + len(para_words) > 300 and current_chunk_words:
                    chunk_text = " ".join(current_chunk_words)
                    chunks.append({
                        "chunk_id": f"{metadata['subject']}_{metadata['board']}_{metadata['class']}_chunk{chunk_id_counter:04d}",
                        "text": chunk_text,
                        "page_num": current_chunk_page,
                        "topic": "General", # Default, can be improved via heading detection
                        "chapter": metadata.get("chapter", ""),
                        "subject": metadata["subject"],
                        "board": metadata["board"],
                        "class": metadata["class"]
                    })
                    chunk_id_counter += 1
                    current_chunk_words = para_words[:]
                    current_chunk_page = page_num
                else:
                    current_chunk_words.extend(para_words)
                    current_chunk_page = page_num

            # Flush final chunk of the page
            if current_chunk_words:
                chunks.append({
                    "chunk_id": f"{metadata['subject']}_{metadata['board']}_{metadata['class']}_chunk{chunk_id_counter:04d}",
                    "text": " ".join(current_chunk_words),
                    "page_num": current_chunk_page,
                    "topic": "General",
                    "chapter": metadata.get("chapter", ""),
                    "subject": metadata["subject"],
                    "board": metadata["board"],
                    "class": metadata["class"]
                })
                chunk_id_counter += 1

        return chunks

    async def ingest_pdf(self, pdf_path: str, metadata: Dict) -> Dict:
        """The main orchestration function for PDF ingestion."""
        print(f"🚀 Starting ingestion for: {pdf_path}")

        # 1. Extract Text
        print("   [1/4] Extracting text and tables...")
        raw_text = self._extract_text_and_tables(pdf_path)

        # 2. Chunk Text
        print("   [2/4] Chunking text...")
        chunks = self._chunk_text(raw_text, metadata)
        print(f"      Created {len(chunks)} semantic chunks.")

        # 3. Embed Chunks
        print("   [3/4] Generating embeddings via Gemini...")
        chunk_texts = [c["text"] for c in chunks]

        # Gemini embedding call
        result = self.client_genai.models.embed_content(
            model="gemini-embedding-001",
            contents=chunk_texts,
        )
        embeddings = [e.values for e in result.embeddings]

        # 4. Store in Qdrant & Supabase
        print("   [4/4] Storing in Qdrant and Supabase...")

        # Prepare Qdrant points
        points = []
        for i, chunk in enumerate(chunks):
            # Combine metadata for Qdrant payload
            payload = {
                **chunk,
                "board": metadata["board"],
                "subject": metadata["subject"],
                "class": metadata["class"],
            }

            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=embeddings[i],
                payload=payload
            ))

        # Bulk upload to Qdrant
        self.qdrant.upsert(
            collection_name="ncert_chunks",
            points=points
        )

        # Bulk upload to Supabase (using the 'cards' table as a temporary storage for chunks if needed,
        # but ideally we should have a 'chunks' table. For now, let's just ensure we log it.)
        # Note: We'll assume the user might want these chunks to be searchable via the RetrievalService.
        # Since we don't have a dedicated 'chunks' table in Supabase yet, we will log success.
        print(f"✅ Ingestion complete. {len(chunks)} chunks indexed in Qdrant.")

        return {
            "status": "success",
            "chunks_processed": len(chunks),
            "file": os.path.basename(pdf_path)
        }

