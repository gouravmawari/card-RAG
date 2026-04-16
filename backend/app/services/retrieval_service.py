import os
import requests
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, Range
from rank_bm25 import BM25Okapi
from groq import Groq
from google import genai

from app.core.config import settings

class RetrievalService:
    def __init__(self):
        # Initialize all clients using our centralized settings
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.qdrant = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
        )

    def generate_hyde_text(self, topic: str, chapter: str) -> str:
        """Ask Groq to write a fake NCERT-style explanation of the topic."""
        prompt = (
            f"Write a Class 10 NCERT-style explanation of '{topic}' "
            f"from the chapter '{chapter}' in about 150 words. "
            f"Be factual, use simple English, include key terms."
        )
        response = self.groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()

    def dense_search(
        self,
        hyde_text: str,
        board: str,
        subject: str,
        page_range: Optional[Tuple[int, int]] = None,
        top_k: int = 20,
    ) -> List[Dict]:
        """Search Qdrant using the HyDE embedding via Gemini."""
        result = self.client_genai.models.embed_content(
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

        search_results = self.qdrant.query_points(
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

    def sparse_search(
        self,
        topic: str,
        board: str,
        subject: str,
        page_range: Optional[Tuple[int, int]] = None,
        top_k: int = 20,
    ) -> List[Dict]:
        """Fetch a broad set of chunks from Qdrant, then run BM25 locally."""
        must_conditions = [
            FieldCondition(key="board", match={"value": board}),
            FieldCondition(key="subject", match={"value": subject}),
        ]

        if page_range:
            page_from, page_to = page_range
            must_conditions.append(FieldCondition(key="page_start", range=Range(lte=page_to)))
            must_conditions.append(FieldCondition(key="page_end", range=Range(gte=page_from)))

        scroll_results, _ = self.qdrant.scroll(
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

    def reciprocal_rank_fusion(
        self,
        dense_results: List[Dict],
        sparse_results: List[Dict],
        k: int = 60,
        top_n: int = 20,
    ) -> List[Dict]:
        rrf_scores: Dict[str, float] = {}
        chunk_map: Dict[str, Dict] = {}

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

    def rerank(self, topic: str, candidates: List[Dict], top_n: int = 5) -> List[Dict]:
        """Uses LangSearch (langsearch-reranker-v1) to rerank candidates."""
        if not candidates:
            return []

        print(f"[Reranker] Calling LangSearch for {len(candidates)} candidates...")

        try:
            api_key = settings.OPENROUTER_API_KEY # Note: Using OpenRouter key as per user's earlier setup if LangSearch uses it, or update to LANGSEARCH_API_KEY
            # Based on user's request, we use the LangSearch endpoint
            # If the user has a specific LANGSEARCH_API_KEY in .env, we should use that.
            # For now, let's assume they use the one provided in the prompt.

            langsearch_key = os.getenv("LANGSEARCH_API_KEY", settings.OPENROUTER_API_KEY)

            response = requests.post(
                url="https://api.langsearch.com/v1/rerank",
                headers={
                    "Authorization": f"Bearer {langsearch_key}",
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

    def get_relevant_chunks(
        self,
        topic: str,
        chapter: str,
        board: str,
        subject: str,
        page_range: Optional[Tuple[int, int]] = None,
    ) -> List[str]:
        """The main retrieval orchestrator."""
        print(f"[Retrieval] topic='{topic}' chapter='{chapter}' board='{board}' pages={page_range}")

        # 1. HyDE
        hyde_text = self.generate_hyde_text(topic, chapter)
        print(f"[HyDE] Generated: {hyde_text[:100]}...")

        # 2. Dense + sparse search
        dense = self.dense_search(hyde_text, board, subject, page_range, top_k=20)
        sparse = self.sparse_search(topic, board, subject, page_range, top_k=20)
        print(f"[Search] Dense: {len(dense)} | Sparse: {len(sparse)}")

        # 3. Fusion
        fused = self.reciprocal_rank_fusion(dense, sparse, top_n=20)
        print(f"[RRF] Fused to {len(fused)} unique chunks")

        # 4. Rerank
        top_chunks = self.rerank(topic, fused, top_n=5)
        print(f"[Rerank] Final top {len(top_chunks)} selected")

        return [chunk["text"] for chunk in top_chunks]
