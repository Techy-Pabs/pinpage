import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace import KIND_PERSONAL, ROLE_OWNER
from app.routers import auth as auth_router
from app.services import workspaces as workspaces_service


def count(db, model):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def assert_nothing_saved(db):
    assert count(db, User) == 0
    assert count(db, Workspace) == 0
    assert count(db, WorkspaceMember) == 0


@pytest.fixture
def server_error_client(client):
    """Like client, but a crash comes back as the 500 response a real caller
    would get, instead of being raised inside the test."""
    return TestClient(app, raise_server_exceptions=False)


def test_register_creates_one_personal_workspace_with_the_user_as_owner(db, register):
    user_id = register("alice@example.com").json()["id"]

    [workspace] = db.scalars(select(Workspace)).all()
    [membership] = db.scalars(select(WorkspaceMember)).all()
    assert workspace.name == "Personal"
    assert workspace.kind == KIND_PERSONAL
    assert membership.workspace_id == workspace.id
    assert str(membership.user_id) == user_id
    assert membership.role == ROLE_OWNER


def test_each_user_gets_their_own_workspace(db, register):
    register("alice@example.com")
    register("bob@example.com")

    members = db.scalars(select(WorkspaceMember)).all()
    assert count(db, Workspace) == 2
    assert len({m.workspace_id for m in members}) == 2  # no shared workspace
    assert len({m.user_id for m in members}) == 2


def test_register_response_does_not_expose_the_workspace(register):
    body = register("alice@example.com").json()

    assert set(body) == {"id", "email", "is_admin", "created_at"}


def test_if_creating_the_workspace_crashes_the_user_is_not_saved(db, server_error_client, monkeypatch):
    def broken(db, user):
        raise RuntimeError("simulated failure at /secret/path.py")

    monkeypatch.setattr(auth_router, "create_personal_workspace", broken)

    response = server_error_client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "correct-horse"}
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Something went wrong on our side. Please try again."}
    }
    assert "simulated" not in response.text and "/secret/path.py" not in response.text
    assert_nothing_saved(db)


@pytest.mark.parametrize(
    "constant, bad_value",
    [
        ("KIND_PERSONAL", "not-a-kind"),  # the workspace INSERT is rejected
        ("ROLE_OWNER", "not-a-role"),  # the membership INSERT is rejected
    ],
)
def test_if_the_database_rejects_the_workspace_or_membership_the_user_is_not_saved(
    db, server_error_client, monkeypatch, constant, bad_value
):
    # A value the check constraint refuses, so the database itself fails the step
    monkeypatch.setattr(workspaces_service, constant, bad_value)

    response = server_error_client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "correct-horse"}
    )

    assert response.status_code == 500  # a real failure, not "email taken"
    assert response.json()["error"]["code"] == "internal_error"
    assert_nothing_saved(db)


def test_duplicate_email_returns_409_and_creates_no_second_workspace(db, register):
    assert register("alice@example.com").status_code == 201

    response = register("ALICE@example.com")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"
    assert count(db, User) == 1
    assert count(db, Workspace) == 1
    assert count(db, WorkspaceMember) == 1


def test_simultaneous_duplicate_is_caught_by_the_database_and_creates_no_workspace(db, register, monkeypatch):
    assert register("alice@example.com").status_code == 201
    # Pretend the second sign-up checked before the first one committed:
    # the early check passes, so only the unique index can stop it
    monkeypatch.setattr(auth_router, "email_registered", lambda db, email: False)

    response = register("alice@example.com")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"
    assert count(db, User) == 1
    assert count(db, Workspace) == 1
    assert count(db, WorkspaceMember) == 1


def test_database_refuses_unknown_kind_and_role(db, register):
    register("alice@example.com")
    user = db.scalars(select(User)).one()
    workspace = db.scalars(select(Workspace)).one()

    db.add(Workspace(name="Odd", kind="team"))
    with pytest.raises(IntegrityError, match="ck_workspaces_kind_valid"):
        db.flush()
    db.rollback()

    other = Workspace(name="Second", kind=KIND_PERSONAL)
    db.add(other)
    db.flush()
    db.add(WorkspaceMember(workspace_id=other.id, user_id=user.id, role="admin"))
    with pytest.raises(IntegrityError, match="ck_workspace_members_role_valid"):
        db.flush()
    db.rollback()

    # The same person can't be added to the same workspace twice
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=ROLE_OWNER))
    with pytest.raises(IntegrityError, match="pk_workspace_members"):
        db.flush()
    db.rollback()
