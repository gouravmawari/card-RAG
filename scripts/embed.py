import json
import os
import uuid
import time
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai
from tqdm import tqdm

load_dotenv()

CHUNK_DIR = Path("data/chunks")
COLLECTION_NAME = "ncert_chunks"
VECTOR_SIZE = 3072        # UPDATED: Gemini-001 is returning 3072 dimensions
BATCH_SIZE = 50

# Initialise clients
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

# Configure Gemini
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

print("Using Google Gemini Embedding API...")

def create_collection_if_not_exists():
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME in existing:
        # Check if the dimension matches
        info = qdrant.get_collection(COLLECTION_NAME)
        current_dim = info.config.params.vectors.size
        if current_dim == VECTOR_SIZE:
            print(f"Collection '{COLLECTION_NAME}' already exists with correct dimension. Skipping creation.")
            return
        else:
            print(f"CRITICAL: Collection '{COLLECTION_NAME}' exists but has dimension {current_dim}. Expected {VECTOR_SIZE}.")
            print(f"Please DELETE the collection in your Qdrant dashboard and run this script again.")
            exit(1)

    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=VECTOR_SIZE,
            distance=Distance.COSINE,
        ),
    )
    print(f"Created Qdrant collection: {COLLECTION_NAME}")

def get_gemini_embeddings(texts: list[str]) -> list[list[float]]:
    """Fetches embeddings from Gemini API."""
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=texts,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        if "429" in str(e):
            print("Rate limit hit. Sleeping for 10 seconds...")
            time.sleep(10)
            return get_gemini_embeddings(texts)
        return []

def embed_and_upload(chunks: list[dict]):
    """Embed a list of chunks via Gemini and upload to Qdrant."""
    for i in tqdm(range(0, len(chunks), BATCH_SIZE), desc="Uploading batches"):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["text"] for c in batch]

        embeddings = get_gemini_embeddings(texts)

        if not embeddings:
            continue

        points = []
        for chunk, vector in zip(batch, embeddings):
            points.append(
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload={
                        "chunk_id": chunk["chunk_id"],
                        "text": chunk["text"],
                        "chapter": chunk.get("chapter", ""),
                        "topic": chunk.get("topic", ""),
                        "page_num": chunk.get("page_num", 0),
                        "board": chunk.get("board", "cbse"),
                        "subject": chunk.get("subject", ""),
                        "class": chunk.get("class", "class10"),
                        "word_count": chunk.get("word_count", 0),
                    },
                )
            )

        if points:
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points)

def verify_upload(expected_count: int):
    """Quick sanity check."""
    info = qdrant.get_collection(COLLECTION_NAME)
    actual = info.points_count
    print(f"\nVerification: {actual} vectors in Qdrant (expected ~{expected_count})")
    if actual < expected_count * 0.95:
        print("WARNING: Fewer vectors than expected.")
    else:
        print("All chunks uploaded successfully.")

def embed_all():
    create_collection_if_not_exists()

    chunk_files = list(CHUNK_DIR.glob("*_chunks.json"))
    if not chunk_files:
        print(f"No chunk files in {CHUNK_DIR}. Run chunk.py first.")
        return

    all_chunks = []
    for path in chunk_files:
        with open(path, encoding="utf-8") as f:
            all_chunks.extend(json.load(f))

    print(f"\nTotal chunks to embed and upload: {len(all_chunks)}")

    embed_and_upload(all_chunks)
    verify_upload(len(all_chunks))

    print("\nStep 1 complete. Qdrant is ready for retrieval.")

if __name__ == "__main__":
    embed_all()
