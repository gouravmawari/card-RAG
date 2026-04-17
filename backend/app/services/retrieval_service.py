import requests
from typing import List, Dict, Tuple, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range, PayloadSchemaType
from rank_bm25 import BM25Okapi
from groq import Groq
from google import genai

from app.core.config import settings

_INDEXES_ENSURED = False


class RetrievalService:
    def __init__(self):
        self.client_genai = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.qdrant = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        """Create Qdrant payload indexes once per process. Idempotent."""
        global _INDEXES_ENSURED
        if _INDEXES_ENSURED:
            return
        keyword_fields = ["board", "subject", "chapter", "source_id", "topic"]
        for field in keyword_fields:
            try:
                self.qdrant.create_payload_index(
                    collection_name="ncert_chunks",
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception:
                pass  # already exists or collection missing — ignore
        try:
            self.qdrant.create_payload_index(
                collection_name="ncert_chunks",
                field_name="page_num",
                field_schema=PayloadSchemaType.INTEGER,
            )
        except Exception:
            pass
        _INDEXES_ENSURED = True

    # ---------- components ----------

    def generate_hyde_text(self, topic: str, chapter: str) -> str:
        prompt = (
            f"Write a short NCERT-style explanation of '{topic}' "
            f"from the chapter '{chapter}' in about 120 words. "
            f"Be factual, use simple English, include key terms."
        )
        response = self.groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        return (response.choices[0].message.content or "").strip()

    def _build_filter(
        self,
        board: str,
        subject: str,
        source_id: Optional[str] = None,
        page_range: Optional[Tuple[int, int]] = None,
    ) -> Filter:
        must = [
            FieldCondition(key="board", match=MatchValue(value=board)),
            FieldCondition(key="subject", match=MatchValue(value=subject)),
        ]
        if source_id:
            must.append(FieldCondition(key="source_id", match=MatchValue(value=source_id)))
        if page_range:
            p_from, p_to = page_range
            must.append(FieldCondition(key="page_num", range=Range(gte=p_from, lte=p_to)))
        return Filter(must=must)

    def dense_search(
        self,
        hyde_text: str,
        board: str,
        subject: str,
        source_id: Optional[str] = None,
        page_range: Optional[Tuple[int, int]] = None,
        top_k: int = 20,
    ) -> List[Dict]:
        result = self.client_genai.models.embed_content(
            model="gemini-embedding-001",
            contents=hyde_text,
        )
        vector = result.embeddings[0].values

        search = self.qdrant.query_points(
            collection_name="ncert_chunks",
            query=vector,
            query_filter=self._build_filter(board, subject, source_id, page_range),
            limit=top_k,
            with_payload=True,
        ).points

        return [
            {
                "chunk_id": r.payload.get("chunk_id"),
                "text": r.payload.get("text", ""),
                "topic": r.payload.get("topic", ""),
                "page_num": r.payload.get("page_num"),
                "score": r.score,
                "source": "dense",
            }
            for r in search
        ]

    def sparse_search(
        self,
        topic: str,
        board: str,
        subject: str,
        source_id: Optional[str] = None,
        page_range: Optional[Tuple[int, int]] = None,
        top_k: int = 20,
    ) -> List[Dict]:
        scroll_results, _ = self.qdrant.scroll(
            collection_name="ncert_chunks",
            scroll_filter=self._build_filter(board, subject, source_id, page_range),
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        if not scroll_results:
            return []

        corpus = [r.payload.get("text", "") for r in scroll_results]
        tokenized = [doc.lower().split() for doc in corpus]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(topic.lower().split())
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        return [
            {
                "chunk_id": scroll_results[i].payload.get("chunk_id"),
                "text": scroll_results[i].payload.get("text", ""),
                "topic": scroll_results[i].payload.get("topic", ""),
                "page_num": scroll_results[i].payload.get("page_num"),
                "score": scores[i],
                "source": "sparse",
            }
            for i in top_idx
        ]

    @staticmethod
    def reciprocal_rank_fusion(dense: List[Dict], sparse: List[Dict], k: int = 60, top_n: int = 20) -> List[Dict]:
        rrf: Dict[str, float] = {}
        chunk_map: Dict[str, Dict] = {}
        for rank, c in enumerate(dense):
            cid = c["chunk_id"]
            rrf[cid] = rrf.get(cid, 0.0) + 1 / (k + rank + 1)
            chunk_map[cid] = c
        for rank, c in enumerate(sparse):
            cid = c["chunk_id"]
            rrf[cid] = rrf.get(cid, 0.0) + 1 / (k + rank + 1)
            chunk_map[cid] = c

        ordered = sorted(rrf, key=lambda cid: rrf[cid], reverse=True)
        out = []
        for cid in ordered[:top_n]:
            ch = chunk_map[cid]
            ch["rrf_score"] = rrf[cid]
            out.append(ch)
        return out

    def rerank(self, topic: str, candidates: List[Dict], top_n: int = 5) -> List[Dict]:
        if not candidates:
            return []
        if not settings.LANGSEARCH_API_KEY:
            print("[Rerank] No LANGSEARCH_API_KEY set — skipping rerank.")
            return candidates[:top_n]

        try:
            response = requests.post(
                url="https://api.langsearch.com/v1/rerank",
                headers={
                    "Authorization": f"Bearer {settings.LANGSEARCH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "langsearch-reranker-v1",
                    "query": topic,
                    "top_n": top_n,
                    "return_documents": True,
                    "documents": [c["text"] for c in candidates],
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results")
            if not isinstance(results, list):
                return candidates[:top_n]

            out = []
            for res in results:
                idx = res.get("index")
                if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                    continue
                c = candidates[idx]
                c["rerank_score"] = res.get("relevance_score", 0)
                out.append(c)
            return out
        except Exception as e:
            print(f"[Rerank] error: {e}. Falling back to unranked candidates.")
            return candidates[:top_n]

    # ---------- orchestrator ----------

    def get_relevant_chunks(
        self,
        topic: str,
        chapter: str,
        board: str,
        subject: str,
        source_id: Optional[str] = None,
        page_range: Optional[Tuple[int, int]] = None,
    ) -> List[str]:
        print(f"[Retrieval] topic='{topic}' board='{board}' subject='{subject}' source_id={source_id}")

        hyde_text = self.generate_hyde_text(topic, chapter)
        dense = self.dense_search(hyde_text, board, subject, source_id, page_range, top_k=20)
        sparse = self.sparse_search(topic, board, subject, source_id, page_range, top_k=20)
        print(f"[Retrieval] dense={len(dense)} sparse={len(sparse)}")

        fused = self.reciprocal_rank_fusion(dense, sparse, top_n=20)
        top = self.rerank(topic, fused, top_n=5)
        print(f"[Retrieval] returning top {len(top)} chunks")
        return [c["text"] for c in top]
