from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from app.services.auth_service import AuthService
from app.core.security import get_current_user_id

router = APIRouter()
auth_service = AuthService()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/register")
async def register(request: RegisterRequest):
    try:
        user_data = await auth_service.register_user(
            email=request.email,
            password=request.password,
            name=request.name,
        )
        return {"status": "success", "user": user_data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/login")
async def login(request: LoginRequest):
    try:
        user_data = await auth_service.login_user(email=request.email, password=request.password)
        return {"status": "success", "user": user_data}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/me")
async def whoami(user_id: str = Depends(get_current_user_id)):
    """Verifies the Bearer token and returns the caller's user_id. Handy for Postman testing."""
    return {"user_id": user_id}
