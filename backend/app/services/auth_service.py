from typing import Dict
import requests
from app.db.supabase import get_supabase
from app.core.config import settings


def _password_grant(email: str, password: str) -> Dict:
    """Hit Supabase's token endpoint directly — does not mutate shared client state."""
    url = f"{settings.SUPABASE_URL}/auth/v1/token?grant_type=password"
    resp = requests.post(
        url,
        headers={
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ValueError(f"Invalid credentials: {resp.json().get('error_description') or resp.text}")
    return resp.json()


class AuthService:
    """
    Uses Supabase Auth (auth.users) as the source of truth for credentials.
    public.users holds the application-level profile, keyed by the same UUID
    via the users.user_id FK to auth.users(id).
    """

    def __init__(self):
        self.supabase = get_supabase()

    async def register_user(self, email: str, password: str, name: str) -> Dict:
        # 1. Create the Supabase Auth user via admin API (bypasses email confirmation).
        try:
            auth_result = self.supabase.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {"name": name},
            })
        except Exception as e:
            msg = str(e)
            if "already" in msg.lower() and "registered" in msg.lower():
                raise ValueError("A user with this email already exists.")
            if "already been registered" in msg.lower() or "already exists" in msg.lower():
                raise ValueError("A user with this email already exists.")
            raise Exception(f"Supabase Auth create user failed: {msg}")

        if not auth_result or not auth_result.user:
            raise Exception("Supabase Auth returned no user.")

        user_id = auth_result.user.id

        # 2. Create the profile row in public.users with the same UUID.
        try:
            self.supabase.table("users").insert({
                "user_id": user_id,
                "email": email,
                "name": name,
                "total_mastery_score": 0,
                "streak_days": 0,
            }).execute()
        except Exception as e:
            raise Exception(f"Failed to create user profile: {e}")

        # 3. Sign in via direct HTTP to return a usable access_token.
        try:
            token_data = _password_grant(email, password)
        except Exception:
            token_data = {}

        return {
            "user_id": user_id,
            "email": email,
            "name": name,
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
        }

    async def login_user(self, email: str, password: str) -> Dict:
        token_data = _password_grant(email, password)  # raises ValueError on bad creds

        user = token_data.get("user") or {}
        user_id = user.get("id")

        profile_row = {}
        if user_id:
            profile = self.supabase.table("users").select("name, email").eq("user_id", user_id).limit(1).execute()
            profile_row = profile.data[0] if profile.data else {}

        return {
            "user_id": user_id,
            "email": user.get("email") or email,
            "name": profile_row.get("name"),
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
        }
