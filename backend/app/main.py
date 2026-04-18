import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.api.endpoints import ingestion, auth, sessions, library
from app.core.config import settings
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.middleware import SecurityHeadersMiddleware, PayloadSizeMiddleware


logger = logging.getLogger("curmath")

app = FastAPI(
    title="CurMath Flashcard Engine API",
    description="API for PDF ingestion, AI-driven flashcard sessions, and progress analytics.",
    version="2.0.0",
    # Hide internal schema details in production docs
    docs_url="/docs",
    redoc_url=None,
)

# Rate limiting — MUST be installed on the state so slowapi can find it
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Middleware stack (outermost last). Security headers → payload size cap → CORS.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(PayloadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all so we never leak Python tracebacks to clients.
    Real detail goes to the server log for debugging.
    """
    logger.exception("unhandled %s %s -> %r", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.include_router(auth.router,      prefix="/api/v1/auth",     tags=["Auth"])
app.include_router(ingestion.router, prefix="/api/v1/ingest",   tags=["Ingestion"])
app.include_router(sessions.router,  prefix="/api/v1/sessions", tags=["Sessions"])
app.include_router(library.router,   prefix="/api/v1/library",  tags=["Library"])


@app.get("/")
async def root():
    return {"message": "CurMath Flashcard Engine API v2. Visit /docs for API documentation."}
