# AI Flashcard Engine: Architecture & Design

## Overview
A sophisticated, resource-efficient AI-powered pipeline designed to transform Indian NCERT textbook PDFs into high-quality, exam-aligned flashcards.

## The Pipeline

### 1. Data Ingestion & Processing
- **Extraction:** `pymupdf` (fitz) extracts text and mathematical notation from PDFs.
- **Chunking:** Text is split into ~300-word semantic chunks with overlapping windows to preserve context.

### 2. The Retrieval Engine (RAG Pipeline)
To ensure the most relevant textbook context is used for card generation, we use a multi-stage retrieval process:
1. **HyDE (Hypothetical Document Embeddings):** 
   - **Model:** Groq (Llama-3.3-70b).
   - **Why:** We use Groq to quickly generate a "fake" textbook explanation of the topic. This hypothetical text is then used to perform the vector search, significantly improving semantic accuracy compared to searching with the raw query.
2. **Hybrid Search:**
   - **Dense Search:** Gemini Embeddings (`gemini-embedding-001`) + Qdrant. Captures semantic meaning.
   - **Sparse Search:** BM25 (local). Captures exact keyword matches.
3. **Reciprocal Rank Fusion (RRF):** Combines the results from Dense and Sparse searches into a single, ranked list.
4. **Cloud Reranking:**
   - **Model:** LangSearch (`langsearch-reranker-v1`).
   - **Why:** Reranking the top candidates using a specialized model ensures that the most contextually relevant chunks are passed to the generator, reducing "hallucinations."

### 3. The Generation Engine
1. **Flashcard Creation:**
   - **Model:** Gemini (`gemini-1.5-flash`).
   - **Why:** Gemini provides the perfect balance of speed, reasoning capabilities, and cost-effectiveness. It generates 5 distinct card types (Q&A, Fill-in-the-blank, True/False, Worked Example, Spot the Error) to ensure deep learning.
2. **Quality Assurance (Double-Pass):**
   - **Model:** Gemini (`gemini-1.5-flash`).
   - **Process:** A second AI agent reviews the generated cards for clarity, correctness, and exam alignment, rejecting any that fall below a quality threshold.

### 4. Storage & Persistence
- **Vector Database:** Qdrant (Cloud). Stores embeddings and metadata (subject, board, chapter) for lightning-fast retrieval.
- **Relational Database:** Supabase. Stores the finalized, high-quality flashcards and will eventually handle user progress (FSRS).

## Technology Summary

| Component | Technology | Purpose |
| :--- | :--- | :--- |
| **LLM (Generation/QA)** | Google Gemini 1.5 Flash | High-speed, intelligent reasoning & generation |
| **LLM (HyDE)** | Groq (Llama 3.3 70B) | Ultra-low latency hypothetical text generation |
| **Embeddings** | Google Gemini | High-dimensional (3072) semantic representation |
| **Reranker** | LangSearch | Precision refinement of search results |
| **Vector DB** | Qdrant | Semantic search and metadata filtering |
| **Database** | Supabase | Permanent storage of cards and user data |

## Design Philosophy
**"Cloud-First, Low-Spec Friendly"**
Instead of downloading massive, heavy models locally (which requires high-end GPUs), this engine offloads all heavy computation to specialized cloud APIs. This allows the system to run flawlessly on any hardware while maintaining state-of-the-art AI performance.
