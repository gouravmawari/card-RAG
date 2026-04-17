"""Security headers + payload size middleware."""
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


# 25 MB is plenty for NCERT-style chapter PDFs. Anything bigger is almost certainly abuse.
MAX_REQUEST_BYTES = 25 * 1024 * 1024


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add a standard set of hardening headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Hide server software version
        response.headers["Server"] = "api"
        return response


class PayloadSizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_REQUEST_BYTES."""

    async def dispatch(self, request: Request, call_next) -> Response:
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Payload too large (max {MAX_REQUEST_BYTES // (1024*1024)} MB)."},
                    )
            except ValueError:
                pass
        return await call_next(request)
