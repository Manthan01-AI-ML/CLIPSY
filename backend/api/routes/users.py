"""
backend/api/routes/users.py

User profile endpoints:
  GET /users/me  — return current user's profile (requires auth)
"""
from fastapi import APIRouter, Depends

from backend.api.deps import get_current_user
from backend.models.user import User
from backend.schemas.auth import UserOut


router = APIRouter()


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get my profile (requires JWT)",
)
def read_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return current_user