import os
import requests
import json
from pathlib import Path
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, Range
from rank_bm25 import BM25Okapi
from groq import Groq
# import google.generativeai as genai
from google import genai
load_dotenv()
client_genai = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


# --- Initialise all clients ---
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Configure Gemini (For Embedding)
# genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

print("Using Google Gemini Embedding API...")

# ---------------------------------------------------------------
# STEP 1: HyDE — generate a hypothetical answer to embed
# ---------------------------------------------------------------
def generate_hyde_text(topic: str, chapter: str) -> str:
    """Ask Groq to write a fake NCERT-style explanation of the topic."""
    prompt = (
        f"Write a Class 10 NCERT-style explanation of '{topic}' "
        f"from the chapter '{chapter}' in about 150 words. "
        f"Be factual, use simple English, include key terms."
    )
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------
# STEP 2: Dense vector search using Gemini Embedding
# ---------------------------------------------------------------
def dense_search(
    hyde_text: str,
    board: str,
    subject: str,
    page_range: tuple[int, int] | None = None,
    top_k: int = 20,
) -> list[dict]:
    """Search Qdrant using the HyDE embedding via Gemini."""

    # Use Gemini instead of local BGE-M3
    # result = genai.embed_content(
    #     model="models/gemini-embedding-001",
    #     content=hyde_text,
    #     task_type="retrieval_document"
    # )
    # vector = result['embedding']

    result = client_genai.models.embed_content(
        model="gemini-embedding-001",
        contents=hyde_text,
    )
    vector = result.embeddings[0].values

    must_conditions = [
        FieldCondition(key="board", match={"value": board}),
        FieldCondition(key="subject", match={"value": subject}),
    ]

    if page_range:
        page_from, page_to = page_range
        must_conditions.append(FieldCondition(key="page_start", range=Range(lte=page_to)))
        must_conditions.append(FieldCondition(key="page_end", range=Range(gte=page_from)))

    # Use the standard search method
    # search_results = qdrant.search(
    #     collection_name="ncert_chunks",
    #     query_vector=vector,
    #     query_filter=Filter(must=must_conditions),
    #     limit=top_k,
    #     with_payload=True,
    # )
    search_results = qdrant.query_points(
        collection_name="ncert_chunks",
        query=vector,
        query_filter=Filter(must=must_conditions),
        limit=top_k,
        with_payload=True,
    ).points

    return [
        {
            "chunk_id": r.payload["chunk_id"],
            "text": r.payload["text"],
            "topic": r.payload.get("topic", ""),
            "page_start": r.payload.get("page_start", 0),
            "page_end": r.payload.get("page_end", 0),
            "score": r.score,
            "source": "dense",
        }
        for r in search_results
    ]


# ---------------------------------------------------------------
# STEP 3: Sparse BM25 keyword search
# ---------------------------------------------------------------
def sparse_search(
    topic: str,
    board: str,
    subject: str,
    page_range: tuple[int, int] | None = None,
    top_k: int = 20,
) -> list[dict]:
    """Fetch a broad set of chunks from Qdrant, then run BM25 locally."""
    must_conditions = [
        FieldCondition(key="board", match={"value": board}),
        FieldCondition(key="subject", match={"value": subject}),
    ]

    if page_range:
        page_from, page_to = page_range
        must_conditions.append(FieldCondition(key="page_start", range=Range(lte=page_to)))
        must_conditions.append(FieldCondition(key="page_end", range=Range(gte=page_from)))

    scroll_results, _ = qdrant.scroll(
        collection_name="ncert_chunks",
        scroll_filter=Filter(must=must_conditions),
        limit=200,
        with_payload=True,
        with_vectors=False,
    )

    if not scroll_results:
        return []

    corpus = [r.payload["text"] for r in scroll_results]
    tokenized_corpus = [doc.lower().split() for doc in corpus]
    bm25 = BM25Okapi(tokenized_corpus)

    tokenized_query = topic.lower().split()
    scores = bm25.get_scores(tokenized_query)

    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    return [
        {
            "chunk_id": scroll_results[i].payload["chunk_id"],
            "text": scroll_results[i].payload["text"],
            "topic": scroll_results[i].payload.get("topic", ""),
            "page_start": scroll_results[i].payload.get("page_start", 0),
            "page_end": scroll_results[i].payload.get("page_end", 0),
            "score": scores[i],
            "source": "sparse",
        }
        for i in top_indices
    ]


# ---------------------------------------------------------------
# STEP 4: Reciprocal Rank Fusion
# ---------------------------------------------------------------
def reciprocal_rank_fusion(
    dense_results: list[dict],
    sparse_results: list[dict],
    k: int = 60,
    top_n: int = 20,
) -> list[dict]:
    rrf_scores: dict[str, float] = {}
    chunk_map: dict[str, dict] = {}

    for rank, chunk in enumerate(dense_results):
        cid = chunk["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (k + rank + 1)
        chunk_map[cid] = chunk

    for rank, chunk in enumerate(sparse_results):
        cid = chunk["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (k + rank + 1)
        chunk_map[cid] = chunk

    sorted_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)

    results = []
    for cid in sorted_ids[:top_n]:
        chunk = chunk_map[cid]
        chunk["rrf_score"] = rrf_scores[cid]
        results.append(chunk)

    return results


# ---------------------------------------------------------------
# STEP 5: Cloud Reranking (OpenRouter)
# ---------------------------------------------------------------
def rerank(topic: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    """Uses LangSearch (langsearch-reranker-v1) to rerank candidates."""
    if not candidates:
        return []

    print(f"[Reranker] Calling LangSearch for {len(candidates)} candidates...")

    try:
        # Note: Assuming LANGSEARCH_API_KEY is in your .env
        api_key = os.getenv("LANGSEARCH_API_KEY")
        if not api_key:
             print("❌ LangSearch API Key not found in environment. Falling back.")
             return candidates[:top_n]

        response = requests.post(
            url="https://api.langsearch.com/v1/rerank",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "langsearch-reranker-v1",
                "query": topic,
                "top_n": top_n,
                "return_documents": True,
                "documents": [c["text"] for c in candidates],
            }
        )
        response.raise_for_status()
        results = response.json()

        # Based on the provided example, LangSearch returns a JSON structure.
        # We need to map its results back to our candidate objects.
        # The example doesn't show the exact response structure,
        # but typically rerankers return a list of indices or similar.
        # Assuming results['results'] contains objects with 'index' and 'relevance_score'
        # (similar to Cohere/OpenRouter) or similar.

        # If LangSearch returns something different, this logic might need adjustment.
        # For now, let's try to handle the most common pattern.

        ranked_candidates = []
        if "results" in results and isinstance(results["results"], list):
            for res in results["results"]:
                index = res["index"]
                score = res.get("relevance_score", 0)
                candidate = candidates[index]
                candidate["rerank_score"] = score
                ranked_candidates.append(candidate)
            return ranked_candidates
        else:
            print("⚠️ LangSearch response format unexpected. Falling back.")
            return candidates[:top_n]

    except Exception as e:
        print(f"❌ Reranker Error: {e}. Falling back to unranked candidates.")
        return candidates[:top_n]


# ---------------------------------------------------------------
# MAIN FUNCTION
# ---------------------------------------------------------------
def get_relevant_chunks(
    topic: str,
    chapter: str,
    board: str,
    subject: str,
    page_range: tuple[int, int] | None = None,
) -> list[str]:
    print(f"[Retrieval] topic='{topic}' chapter='{chapter}' board='{board}' pages={page_range}")

    # 1. HyDE
    hyde_text = generate_hyde_text(topic, chapter)
    print(f"[HyDE] Generated: {hyde_text[:100]}...")

    # 2. Dense + sparse search
    dense = dense_search(hyde_text, board, subject, page_range, top_k=20)
    sparse = sparse_search(topic, board, subject, page_range, top_k=20)
    print(f"[Search] Dense: {len(dense)} | Sparse: {len(sparse)}")

    # 3. Fusion
    fused = reciprocal_rank_fusion(dense, sparse, top_n=20)
    print(f"[RRF] Fused to {len(fused)} unique chunks")

    # 4. Rerank
    top_chunks = rerank(topic, fused, top_n=5)
    print(f"[Rerank] Final top {len(top_chunks)} selected")

    return [chunk["text"] for chunk in top_chunks]
