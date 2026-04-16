from fastapi import FastAPI
from app.api.endpoints import ingestion, generation, review

app = FastAPI(
    title="CurMath Flashcard Engine API",
    description="API for managing NCERT flashcard ingestion, generation, and spaced repetition learning.",
    version="1.0.0"
)

# Include routers from our modules
app.include_router(ingestion.router, prefix="/api/v1/ingest", tags=["Ingestion"])
app.include_router(generation.router, prefix="/api/v1/generate", tags=["Generation"])
app.include_router(review.router, prefix="/api/v1/review", tags=["Review"])

@app.get("/")
async def root():
    return {"message": "Welcome to the CurMath Flashcard Engine API. Visit /docs for API documentation."}
