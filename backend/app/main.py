from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.endpoints import ingestion, auth, sessions
from app.core.config import settings

app = FastAPI(
    title="CurMath Flashcard Engine API",
    description="API for PDF ingestion, AI-driven flashcard sessions, and progress analytics.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,      prefix="/api/v1/auth",     tags=["Auth"])
app.include_router(ingestion.router, prefix="/api/v1/ingest",   tags=["Ingestion"])
app.include_router(sessions.router,  prefix="/api/v1/sessions", tags=["Sessions"])


@app.get("/")
async def root():
    return {"message": "CurMath Flashcard Engine API v2. Visit /docs for API documentation."}
