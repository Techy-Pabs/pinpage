import uuid
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

JWT_ALGORITHM = "HS256"

# Argon2 is the recommended password hashing algorithm
password_hash = PasswordHash.recommended()

# A real hash of a throwaway password. Login checks against this when the
# email doesn't exist, so that case takes as long as a wrong password and
# the response time doesn't reveal which emails have accounts.
DUMMY_HASH = password_hash.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def create_access_token(user_id: uuid.UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    # The JWT standard says "sub" must be a string
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    """Return the user id from a valid token.

    Raises jwt.InvalidTokenError if the signature is wrong, the token has
    expired, it wasn't signed with HS256, or "sub" is missing or not a UUID.
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        # Only accept HS256, so a token can't pick a weaker algorithm (e.g. "none")
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    try:
        return uuid.UUID(payload["sub"])
    except (ValueError, TypeError, AttributeError):
        raise jwt.InvalidTokenError("sub is not a user id")
