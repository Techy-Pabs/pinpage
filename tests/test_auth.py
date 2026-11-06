import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from app.core.security import JWT_ALGORITHM, create_access_token

USER_FIELDS = {"id", "email", "is_admin", "created_at"}


def assert_unauthorized(response):
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "not_authenticated", "message": "Could not validate credentials"}
    }


def test_register_succeeds(register):
    response = register("Alice@Example.com", "correct-horse")

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["is_admin"] is False
    assert set(body) == USER_FIELDS
    assert uuid.UUID(body["id"])  # a UUID, not a counter


def test_register_duplicate_email_returns_409(register):
    assert register("alice@example.com").status_code == 201

    response = register("ALICE@example.com")  # same email, different case

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "email_taken", "message": "Email already registered"}}


def test_register_short_password_returns_422_without_echoing_it(register):
    response = register("alice@example.com", "pw7Qz")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "pw7Qz" not in response.text  # the password isn't echoed back


def test_register_invalid_email_returns_422(register):
    response = register("not-an-email", "correct-horse")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_login_succeeds(client, register):
    register("alice@example.com", "correct-horse")

    response = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct-horse"}
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


def test_login_wrong_password_and_unknown_email_return_same_401(client, register):
    register("alice@example.com", "correct-horse")

    wrong_password = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
    )

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"


def test_unknown_email_still_checks_a_password_hash(client, monkeypatch):
    # Timing protection: an unknown email does the same Argon2 work as a wrong password
    from app.routers import auth

    checked = []
    real_verify = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, h: checked.append(h) or real_verify(p, h))

    client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever"})

    assert checked == [auth.DUMMY_HASH]


def test_me_without_token_returns_401(client):
    assert_unauthorized(client.get("/auth/me"))


def expired_token(user_id):
    payload = {"sub": str(user_id), "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


@pytest.mark.parametrize(
    "make_token",
    [
        lambda user_id: "not-a-real-token",
        # Signed with the wrong secret
        lambda user_id: jwt.encode(
            {"sub": str(user_id), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            "some-other-secret", algorithm=JWT_ALGORITHM,
        ),
        expired_token,
        # A valid token for a user who doesn't exist
        lambda user_id: create_access_token(uuid.uuid4()),
        # "sub" isn't a UUID
        lambda user_id: jwt.encode(
            {"sub": "42", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            settings.jwt_secret, algorithm=JWT_ALGORITHM,
        ),
        # No algorithm at all
        lambda user_id: jwt.encode({"sub": str(user_id)}, None, algorithm="none"),
    ],
    ids=["garbage", "wrong-secret", "expired", "unknown-user", "sub-not-uuid", "alg-none"],
)
def test_every_bad_token_gets_the_same_401(client, register, make_token):
    user_id = register().json()["id"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {make_token(user_id)}"})

    assert_unauthorized(response)


def test_me_returns_current_user_without_password_hash(client, auth_headers):
    headers = auth_headers("alice@example.com", "correct-horse")

    response = client.get("/auth/me", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert set(body) == USER_FIELDS
    assert "argon2" not in response.text
    assert "password" not in response.text


def test_token_sub_is_the_user_uuid(client, register):
    user_id = register("alice@example.com", "correct-horse").json()["id"]
    token = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct-horse"}
    ).json()["access_token"]

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])

    assert payload["sub"] == user_id


def test_password_is_stored_as_an_argon2_hash(db, register):
    from app.models import User

    register("alice@example.com", "correct-horse")

    stored = db.query(User).filter_by(email="alice@example.com").one().password_hash
    assert stored.startswith("$argon2")
    assert "correct-horse" not in stored
