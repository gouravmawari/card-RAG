from pathlib import Path
from typing import Dict
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

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

    async def sync_oauth_profile(self, user_id: str, email: str, name: str) -> Dict:
        """
        Ensures a public.users row exists for a Supabase Auth user.
        Called by the frontend after Google OAuth (or any non-password sign-in)
        to create the profile if missing. Idempotent.
        """
        existing = self.supabase.table("users").select("user_id, name, email").eq("user_id", user_id).limit(1).execute()
        if existing.data:
            return {"user_id": user_id, "created": False, **existing.data[0]}

        self.supabase.table("users").insert({
            "user_id": user_id,
            "email": email,
            "name": name or email.split("@")[0],
            "total_mastery_score": 0,
            "streak_days": 0,
        }).execute()
        return {"user_id": user_id, "email": email, "name": name, "created": True}

    async def delete_account(self, user_id: str, password: str | None = None) -> Dict:
        """
        Permanently wipe a user: their Qdrant vectors, their uploaded PDFs on
        disk, their DB rows (sessions/cards/reviews/topic_stats/sources/profile),
        and finally their Supabase Auth user.

        If `password` is supplied, verify it first via a password grant — this
        blocks a stolen access token from deleting an account silently. OAuth
        users can pass None.
        """
        # Look up email for optional password re-verification.
        profile = self.supabase.table("users").select("email").eq("user_id", user_id).limit(1).execute()
        email = (profile.data[0].get("email") if profile.data else None) or None

        if password:
            if not email:
                raise ValueError("Cannot verify password: user profile missing email.")
            _password_grant(email, password)  # raises ValueError on bad creds

        # 1) Pull every source this user owns (so we can clean up storage + Qdrant).
        sources = (
            self.supabase.table("sources")
            .select("source_id, file_url")
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
        source_ids = [s["source_id"] for s in sources if s.get("source_id")]

        # 2) Remove Qdrant points for those sources (best-effort).
        if source_ids:
            try:
                qdrant = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
                for sid in source_ids:
                    try:
                        qdrant.delete(
                            collection_name="ncert_chunks",
                            points_selector=FilterSelector(
                                filter=Filter(
                                    must=[FieldCondition(
                                        key="source_id",
                                        match=MatchValue(value=sid),
                                    )]
                                )
                            ),
                        )
                    except Exception as e:
                        print(f"[delete_account] Qdrant delete failed for {sid}: {e}")
            except Exception as e:
                print(f"[delete_account] Qdrant client init failed: {e}")

        # 3) Delete PDF files on disk (best-effort).
        for s in sources:
            fp = s.get("file_url")
            if not fp:
                continue
            try:
                Path(fp).unlink(missing_ok=True)
            except Exception as e:
                print(f"[delete_account] File unlink failed for {fp}: {e}")

        # 4) Delete DB rows in dependency order. ON DELETE CASCADE on sessions/cards
        #    takes care of most; we also wipe anything keyed directly off user_id
        #    so we don't rely on cascade chains we can't verify.
        try:
            self.supabase.table("user_reviews").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] user_reviews delete failed: {e}")
        try:
            self.supabase.table("user_topic_stats").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] user_topic_stats delete failed: {e}")
        try:
            self.supabase.table("sessions").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] sessions delete failed: {e}")
        try:
            self.supabase.table("cards").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] cards delete failed: {e}")
        try:
            self.supabase.table("sources").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] sources delete failed: {e}")
        try:
            self.supabase.table("users").delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[delete_account] users delete failed: {e}")

        # 5) Finally, delete the Supabase Auth user. Without this the user could
        #    still log in (and we'd re-create a bare profile on sync).
        try:
            self.supabase.auth.admin.delete_user(user_id)
        except Exception as e:
            raise Exception(f"Supabase Auth delete failed: {e}")

        return {"user_id": user_id, "sources_removed": len(source_ids)}

    async def change_password(self, user_id: str, old_password: str, new_password: str) -> None:
        """
        Verify the user's current password by attempting a password grant, then
        set the new password via the Supabase admin API. Raises ValueError on
        bad old password, and generic Exception on Supabase-side failures.
        """
        # Look up email so we can attempt the password grant.
        profile = self.supabase.table("users").select("email").eq("user_id", user_id).limit(1).execute()
        if not profile.data or not profile.data[0].get("email"):
            raise ValueError("User profile not found.")
        email = profile.data[0]["email"]

        # Verify the old password. _password_grant raises ValueError on bad creds.
        _password_grant(email, old_password)

        # Update via admin API.
        try:
            self.supabase.auth.admin.update_user_by_id(user_id, {"password": new_password})
        except Exception as e:
            raise Exception(f"Password update failed: {e}")

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
