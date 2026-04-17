"""
Rate limiting.

Uses slowapi (FastAPI-compatible). Keyed on user_id from JWT when present,
otherwise the caller's IP. Prevents both brute-force auth attacks and
third-party-API abuse (Gemini/Groq/LangSearch spend).

Storage is in-memory (fine for a single VPS instance). If you scale to
multiple instances behind a load balancer, switch to Redis via
`Limiter(..., storage_uri="redis://host:6379")`.
"""
import jwt
from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


def _rate_limit_key(request: Request) -> str:
    """Prefer user_id from a valid-looking JWT; fall back to client IP."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        try:
            # Unverified decode is fine HERE — this key is only for rate-limiting bucketing,
            # not for authentication. The actual JWT verification still happens in the
            # `get_current_user_id` dependency before any sensitive work.
            payload = jwt.decode(token, options={"verify_signature": False})
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_rate_limit_key,
    # Sensible defaults applied to endpoints that don't specify their own.
    default_limits=["300/minute"],
    headers_enabled=False,
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Standard 429 response; avoids leaking internal detail."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many requests. Slow down — limit: {exc.detail}"},
        headers={"Retry-After": "60"},
    )
