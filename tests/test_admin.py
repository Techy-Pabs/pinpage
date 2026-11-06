"""require_admin, tested through a route that exists only in these tests.

Milestone 1 has no admin endpoints yet, so the test adds one to the app.
"""

import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import update

from app.dependencies import require_admin
from app.main import app
from app.models import User

ADMIN_TEST_PATH = "/test-only/admin"


@pytest.fixture(scope="module", autouse=True)
def admin_test_route():
    router = APIRouter()

    @router.get(ADMIN_TEST_PATH)
    def admin_only(user: User = Depends(require_admin)):
        return {"ok": True}

    app.include_router(router)
    yield
    # Remove it again, so no other test (or the OpenAPI schema) sees it
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != ADMIN_TEST_PATH]
    app.openapi_schema = None


def set_admin(db, email, is_admin):
    db.execute(update(User).where(User.email == email).values(is_admin=is_admin))
    db.commit()


@pytest.fixture
def normal_user(auth_headers):
    return auth_headers("user@example.com")


@pytest.fixture
def admin(auth_headers, db):
    headers = auth_headers("admin@example.com")
    set_admin(db, "admin@example.com", True)
    return headers


def test_no_token_gets_401(client):
    response = client.get(ADMIN_TEST_PATH)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_normal_user_gets_403(client, normal_user):
    response = client.get(ADMIN_TEST_PATH, headers=normal_user)

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "admin_required", "message": "Admin access required"}}
    assert "WWW-Authenticate" not in response.headers  # logging in again won't help


def test_admin_gets_200(client, admin):
    assert client.get(ADMIN_TEST_PATH, headers=admin).status_code == 200


def test_promote_and_demote_take_effect_without_a_new_token(client, db, normal_user):
    assert client.get(ADMIN_TEST_PATH, headers=normal_user).status_code == 403

    set_admin(db, "user@example.com", True)
    assert client.get(ADMIN_TEST_PATH, headers=normal_user).status_code == 200  # same token

    set_admin(db, "user@example.com", False)
    assert client.get(ADMIN_TEST_PATH, headers=normal_user).status_code == 403  # same token


def test_register_ignores_is_admin_in_the_request(client, db):
    response = client.post(
        "/auth/register", json={"email": "sneaky@example.com", "password": "long-enough", "is_admin": True}
    )

    assert response.status_code == 201
    assert response.json()["is_admin"] is False
    assert db.query(User).filter_by(email="sneaky@example.com").one().is_admin is False


def test_me_returns_is_admin(client, admin):
    assert client.get("/auth/me", headers=admin).json()["is_admin"] is True


def test_admin_requests_are_logged_without_content(client, admin, caplog):
    caplog.set_level("INFO", logger="app.dependencies")

    client.get(ADMIN_TEST_PATH, headers=admin)

    [line] = [r.getMessage() for r in caplog.records if "Admin request" in r.getMessage()]
    assert line.endswith(f"GET {ADMIN_TEST_PATH}")
    assert "user_id=" in line
