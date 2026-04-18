import os
import uuid
import datetime
import gc
from pathlib import Path
from typing import List, Dict

from google import genai
from app.core.config import settings
from app.db.supabase import get_supabase

import fitz  # PyMuPDF
import pdfplumber
import re
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct


class IngestionService:
    def __init__(self):
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.supabase = get_supabase()
        self.qdrant = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
        self.MARGIN_PERCENT = 0.07

    def _extract_text_and_tables(self, pdf_path: str) -> str:
        doc = fitz.open(pdf_path)
        full_text = []

        for page_num, page in enumerate(doc, start=1):
            page_height = page.rect.height
            content_top = page_height * self.MARGIN_PERCENT
            content_bottom = page_height * (1 - self.MARGIN_PERCENT)

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
                if block["type"] != 0:
                    continue
                bbox = block["bbox"]
                block_center_y = (bbox[1] + bbox[3]) / 2
                if block_center_y < content_top or block_center_y > content_bottom:
                    continue

                block_parts = []
                is_heading = False
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text:
                            continue
                        if span["size"] > heading_threshold:
                            is_heading = True
                        if re.fullmatch(r"[-_=\s]+", text):
                            continue
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
        blocks = re.split(r"\[PAGE:(\d+)\]", text)
        chunks = []
        chunk_id_counter = 0
        klass = metadata.get("class", "NA")

        for i in range(1, len(blocks), 2):
            page_num = int(blocks[i])
            content = blocks[i + 1].strip()
            if not content:
                continue

            paragraphs = content.split("\n\n")
            current_chunk_words: List[str] = []
            current_chunk_page = page_num

            for para in paragraphs:
                para_words = para.split()
                if len(current_chunk_words) + len(para_words) > 300 and current_chunk_words:
                    chunks.append(self._make_chunk(
                        chunk_id_counter, current_chunk_words, current_chunk_page, metadata, klass,
                    ))
                    chunk_id_counter += 1
                    current_chunk_words = para_words[:]
                    current_chunk_page = page_num
                else:
                    current_chunk_words.extend(para_words)
                    current_chunk_page = page_num

            if current_chunk_words:
                chunks.append(self._make_chunk(
                    chunk_id_counter, current_chunk_words, current_chunk_page, metadata, klass,
                ))
                chunk_id_counter += 1

        return chunks

    def _make_chunk(self, idx: int, words: List[str], page_num: int, metadata: dict, klass: str) -> Dict:
        return {
            "chunk_id": f"{metadata['subject']}_{metadata['board']}_{klass}_chunk{idx:04d}",
            "text": " ".join(words),
            "page_num": page_num,
            "topic": self._detect_topic(words),
            "chapter": metadata.get("chapter", ""),
            "subject": metadata["subject"],
            "board": metadata["board"],
            "class": klass,
        }

    def _detect_topic(self, words: List[str]) -> str:
        joined = " ".join(words[:25])
        m = re.search(r"###\s*(.+?)\s*###", joined)
        return m.group(1).strip() if m else "General"

    async def ingest_pdf(self, source_id: str, pdf_path: str, metadata: Dict) -> Dict:
        try:
            self._update_source(source_id, {"status": "processing"})
            print(f"🚀 Starting ingestion for source_id={source_id}: {pdf_path}")

            print("   [1/4] Extracting text and tables...")
            raw_text = self._extract_text_and_tables(pdf_path)

            print("   [2/4] Chunking text...")
            chunks = self._chunk_text(raw_text, metadata)
            print(f"      Created {len(chunks)} semantic chunks.")

            # Free raw text immediately after chunking
            del raw_text
            gc.collect()

            if not chunks:
                raise RuntimeError("No chunks produced — PDF may be empty or unreadable.")

            print("   [3/4] Generating embeddings via Gemini...")
            chunk_texts = [c["text"] for c in chunks]
            embeddings = []
            BATCH_SIZE = 10

            for i in range(0, len(chunk_texts), BATCH_SIZE):
                batch = chunk_texts[i:i + BATCH_SIZE]
                result = self.client_genai.models.embed_content(
                    model="gemini-embedding-001",
                    contents=batch,
                )
                embeddings.extend([e.values for e in result.embeddings])
                del result
                gc.collect()

            del chunk_texts
            gc.collect()

            print("   [4/4] Storing in Qdrant...")
            points = []
            for i, chunk in enumerate(chunks):
                payload = {**chunk, "source_id": source_id}
                points.append(PointStruct(
                    id=str(uuid.uuid4()),
                    vector=embeddings[i],
                    payload=payload,
                ))

            # Upsert in batches to avoid large payloads
            QDRANT_BATCH = 50
            for i in range(0, len(points), QDRANT_BATCH):
                self.qdrant.upsert(collection_name="ncert_chunks", points=points[i:i + QDRANT_BATCH])

            del embeddings, points
            gc.collect()

            topics = sorted({c["topic"] for c in chunks if c["topic"] and c["topic"] != "General"})

            self._update_source(source_id, {
                "status": "completed",
                "chunk_count": len(chunks),
                "topics": topics,
                "processed_at": datetime.datetime.utcnow().isoformat(),
            })

            print(f"✅ Ingestion complete. {len(chunks)} chunks indexed.")
            return {"status": "success", "chunks_processed": len(chunks), "source_id": source_id}

        except Exception as e:
            print(f"❌ Ingestion failed for {source_id}: {e}")
            self._update_source(source_id, {"status": "failed", "error_message": str(e)[:1000]})
            raise

    def _update_source(self, source_id: str, fields: Dict) -> None:
        self.supabase.table("sources").update(fields).eq("source_id", source_id).execute()
