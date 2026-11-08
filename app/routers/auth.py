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
from app.services.workspaces import create_personal_workspace

router = APIRouter(prefix="/auth", tags=["auth"])


def email_taken() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "email_taken", "Email already registered")


def invalid_credentials() -> ApiError:
    # The same response for a wrong password and an unknown email
    return ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Incorrect email or password")


# The unique index on users.email (named by the convention in app.db.base)
EMAIL_UNIQUE_INDEX = "ix_users_email"


def email_registered(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email)) is not None


def violates_unique_email(error: IntegrityError) -> bool:
    # psycopg reports which constraint or index rejected the row
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == EMAIL_UNIQUE_INDEX


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED, summary="Create an account"
)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Creates the account, its personal workspace and the owner membership
    together, or none of them. 409 email_taken if the email is registered."""
    if email_registered(db, body.email):
        raise email_taken()

    # One transaction: the user, their personal workspace and the owner
    # membership are saved together by the single commit below, or not at all
    try:
        user = User(email=body.email, password_hash=hash_password(body.password))
        db.add(user)
        db.flush()  # gives user.id, and hits the unique email index now
        create_personal_workspace(db, user)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if violates_unique_email(error):
            # Two sign-ups with the same email at the same moment: the unique
            # index in the database catches the second one
            raise email_taken()
        raise  # any other database error is a real failure, not a taken email
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse, summary="Log in")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Returns a bearer token (a JWT) valid for JWT_EXPIRE_MINUTES. Use the
    Authorize button with it. A wrong email or password gets the same 401."""
    user = db.scalar(select(User).where(User.email == body.email))

    if user is None:
        verify_password(body.password, DUMMY_HASH)
        raise invalid_credentials()
    if not verify_password(body.password, user.password_hash):
        raise invalid_credentials()

    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserResponse, summary="Your account")
def me(current_user: User = Depends(get_current_user)):
    """The logged in user."""
    return current_user
