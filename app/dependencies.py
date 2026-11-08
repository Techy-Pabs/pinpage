import logging
from collections.abc import Generator

import jwt
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models import User
from app.services.storage import Storage, default_storage

logger = logging.getLogger(__name__)

# Reads "Authorization: Bearer <token>" and adds the Authorize button to /docs.
# auto_error=False lets us send our own 401 instead of FastAPI's default.
bearer_scheme = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    # One session per request, closed afterwards even if the request fails
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_factory() -> sessionmaker:
    """Makes new database sessions, for work that outlives the request
    (background tasks) or runs outside one (the startup check).

    Tests replace it with a factory for the test database.
    """
    return SessionLocal


def get_storage() -> Storage:
    """Where original PDFs are kept: R2 in the app.

    Tests replace it with FakeStorage through app.dependency_overrides,
    so there is no test mode switch in the settings.
    """
    return default_storage()


def unauthorized() -> ApiError:
    # Every failure gets the same response, so callers can't tell
    # a forged token from an expired one or a deleted user.
    return ApiError(
        status.HTTP_401_UNAUTHORIZED,
        "not_authenticated",
        "Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise unauthorized()
    try:
        user_id = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise unauthorized()

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized()
    return user


def require_admin(request: Request, current_user: User = Depends(get_current_user)) -> User:
    """The current user, but only if they are an admin.

    is_admin is read from the database on every request (not from the
    token), so promoting or demoting someone takes effect immediately.
    """
    if not current_user.is_admin:
        # 403, not 401: they are logged in, just not allowed. No
        # WWW-Authenticate header, since logging in again won't help.
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required", "Admin access required")
    # Who did what, never request bodies or document content
    logger.info("Admin request: user_id=%s %s %s", current_user.id, request.method, request.url.path)
    return current_user
