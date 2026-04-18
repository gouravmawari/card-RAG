import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException, status
from app.core.config import settings

_jwks_url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwks_client = PyJWKClient(_jwks_url, cache_keys=True, lifespan=3600)


def _decode_token(authorization: str) -> dict:
    """Verify a Supabase JWT via JWKS and return the decoded payload."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty token")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256", "HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")

    return payload


async def get_current_user_id(authorization: str = Header(...)) -> str:
    """
    FastAPI dependency: verifies a Supabase JWT locally using JWKS.
    Supabase issues ES256-signed tokens with rotating keys; we fetch & cache the JWKS.
    """
    payload = _decode_token(authorization)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim")
    return user_id


async def get_current_admin(authorization: str = Header(...)) -> str:
    """
    FastAPI dependency: same JWT verification as get_current_user_id, but
    additionally requires the token's email claim to be in settings.ADMIN_EMAILS.
    Returns the admin's user_id.
    """
    payload = _decode_token(authorization)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim")

    email = (payload.get("email") or "").lower()
    allowlist = [e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()]
    if not allowlist:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Admin access is not configured.")
    if not email or email not in allowlist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")
    return user_id
