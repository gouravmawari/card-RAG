from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from app.services.auth_service import AuthService
from app.core.security import get_current_user_id
from app.core.rate_limit import limiter

router = APIRouter()
auth_service = AuthService()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=80)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class SyncProfileRequest(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=80)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DeleteAccountRequest(BaseModel):
    # Current password for email users. OAuth users can omit (JWT auth alone is trusted).
    password: str | None = Field(default=None, max_length=128)
    # Literal "DELETE" to guard against accidental calls.
    confirm: str = Field(min_length=6, max_length=16)


@router.post("/register")
@limiter.limit("10/hour")
async def register(request: Request, body: RegisterRequest):
    try:
        user_data = await auth_service.register_user(
            email=body.email,
            password=body.password,
            name=body.name,
        )
        return {"status": "success", "user": user_data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Registration failed.") from e


@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest):
    try:
        user_data = await auth_service.login_user(email=body.email, password=body.password)
        return {"status": "success", "user": user_data}
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid credentials.") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Login failed.") from e


@router.get("/me")
@limiter.limit("60/minute")
async def whoami(request: Request, user_id: str = Depends(get_current_user_id)):
    return {"user_id": user_id}


@router.post("/sync-profile")
@limiter.limit("10/minute")
async def sync_profile(request: Request, body: SyncProfileRequest, user_id: str = Depends(get_current_user_id)):
    try:
        result = await auth_service.sync_oauth_profile(user_id=user_id, email=body.email, name=body.name or "")
        return {"status": "success", "profile": result}
    except Exception:
        raise HTTPException(status_code=500, detail="Profile sync failed.")


@router.post("/delete-account")
@limiter.limit("3/hour")
async def delete_account(request: Request, body: DeleteAccountRequest, user_id: str = Depends(get_current_user_id)):
    """Permanently delete the calling user. Requires `confirm: "DELETE"`;
    email-password users must also include their current password."""
    if body.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='Set "confirm" to the literal string "DELETE".')
    try:
        result = await auth_service.delete_account(user_id=user_id, password=body.password)
        return {"status": "deleted", **result}
    except ValueError:
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    except Exception:
        raise HTTPException(status_code=500, detail="Account deletion failed.")


@router.post("/change-password")
@limiter.limit("5/hour")
async def change_password(request: Request, body: ChangePasswordRequest, user_id: str = Depends(get_current_user_id)):
    if body.old_password == body.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current password.")
    try:
        await auth_service.change_password(user_id=user_id, old_password=body.old_password, new_password=body.new_password)
        return {"status": "success", "message": "Password updated."}
    except ValueError:
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    except Exception:
        raise HTTPException(status_code=500, detail="Password change failed.")
