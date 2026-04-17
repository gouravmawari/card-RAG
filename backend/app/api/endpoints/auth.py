from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from app.services.auth_service import AuthService

router = APIRouter()
auth_service = AuthService()

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str

@router.post("/register")
async def register(request: RegisterRequest):
    """
    Registers a new user.
    """
    try:
        user_data = await auth_service.register_user(
            email=request.email,
            password=request.password,
            name=request.name
        )
        return {
            "status": "success",
            "user": user_data
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
