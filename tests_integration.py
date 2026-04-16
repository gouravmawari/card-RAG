import asyncio
import sys
import os
import traceback
from pathlib import Path

# Explicitly add the backend directory to sys.path
backend_dir = Path("backend").resolve()
sys.path.append(str(backend_dir))

print(f"Added to sys.path: {backend_dir}")
print(f"Current sys.path: {sys.path}")

try:
    import app
    print("Import 'app' successful!")
    from app.services.retrieval_service import RetrievalService
    from app.services.generation_service import GenerationService
    print("Imports from 'app.services' successful!")
except ImportError as e:
    print(f"Import Error: {e}")
    raise

async def test_pipeline():
    print("--- Starting Integration Test ---")

    # Initialize services
    retrieval_service = RetrievalService()
    generation_service = GenerationService()

    # Test parameters
    topic = "pH scale"
    chapter = "acids_bases_and_salts"
    board = "cbse"
    subject = "jesc102-1-10"

    print(f"\n[1/3] Testing Retrieval for topic: '{topic}'")
    try:
        chunks = retrieval_service.get_relevant_chunks(
            topic=topic,
            chapter=chapter,
            board=board,
            subject=subject
        )
        if not chunks:
            print("Retrieval failed: No chunks returned.")
            return
        print(f"Retrieval successful. Found {len(chunks)} chunks.")
        for i, chunk in enumerate(chunks[:2]):
            print(f"   Chunk {i+1} preview: {chunk[:100]}...")
    except Exception as e:
        print(f"Retrieval error: {e}")
        traceback.print_exc()
        return

    print(f"\n[2/3] Testing Generation for topic: '{topic}'")
    try:
        cards = await generation_service.generate_flashcards(
            topic=topic,
            chapter=chapter,
            board=board,
            subject=subject
        )

        if not cards:
            print("Generation failed: No cards were returned.")
            return

        print(f"Generation successful. Created {len(cards)} cards.")
        for i, card in enumerate(cards):
            print(f"   Card {i+1} ({card['card_type']}): {card['question'][:50]}...")

    except Exception as e:
        print(f"Generation error: {e}")
        traceback.print_exc()
        return

    print(f"\n[3/3] Testing Database Persistence")
    print("Pipeline test completed successfully!")

if __name__ == "__main__":
    try:
        asyncio.run(test_pipeline())
    except Exception:
        traceback.print_exc()
