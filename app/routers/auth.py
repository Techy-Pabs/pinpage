from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.security import (
    DUMMY_HASH,
    create_access_token,
    hash_password,
    verify_password,
)
from app.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def email_taken() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "email_taken", "Email already registered")


def invalid_credentials() -> ApiError:
    # The same response for a wrong password and an unknown email
    return ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Incorrect email or password")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == body.email)):
        raise email_taken()

    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two sign-ups with the same email at the same moment: the unique
        # index in the database catches the second one.
        db.rollback()
        raise email_taken()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))

    if user is None:
        verify_password(body.password, DUMMY_HASH)
        raise invalid_credentials()
    if not verify_password(body.password, user.password_hash):
        raise invalid_credentials()

    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user
