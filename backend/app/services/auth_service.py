import uuid
from typing import Dict, Optional
from app.db.supabase import get_supabase

class AuthService:
    def __init__(self):
        self.supabase = get_supabase()

    async def register_user(self, email: str, password: str, name: str) -> Dict:
        """
        Registers a new user in the public.users table.
        """
        # 1. Check if user already exists
        existing_user = self.supabase.table("users").select("user_id").eq("email", email).execute()

        if existing_user.data:
            raise ValueError("A user with this email already exists.")

        # 2. Create new user
        new_user_id = str(uuid.uuid4())
        payload = {
            "user_id": new_user_id,
            "email": email,
            "password": password,
            "name": name,
            "total_mastery_score": 0,
            "streak_days": 0
        }

        try:
            self.supabase.table("users").insert(payload).execute()
            return {
                "user_id": new_user_id,
                "email": email,
                "name": name
            }
        except Exception as e:
            print(f"Registration error: {e}")
            raise Exception("Failed to create user in database.")
