"""The documents table's own rules, and the workspace lookup it relies on."""

import hashlib
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.models import Document, User, Workspace, WorkspaceMember
from app.models.document import STATUS_PROCESSING
from app.models.workspace import KIND_ORGANISATION, ROLE_OWNER
from app.services.storage import document_key
from app.services.workspaces import get_personal_workspace


def user_and_workspace(db, email="alice@example.com") -> tuple[User, Workspace]:
    user = db.scalars(select(User).where(User.email == email)).one()
    return user, get_personal_workspace(db, user.id)


def new_document(workspace: Workspace, user: User, **changes) -> Document:
    document_id = uuid.uuid4()
    values = dict(
        id=document_id,
        workspace_id=workspace.id,
        uploaded_by=user.id,
        title="Handbook",
        original_filename="Handbook.pdf",
        storage_key=document_key(workspace.id, document_id),
        size_bytes=100,
        sha256=hashlib.sha256(document_id.bytes).hexdigest(),
        status=STATUS_PROCESSING,
    )
    return Document(**{**values, **changes})


# --- Finding the personal workspace -------------------------------------------------------

def test_personal_workspace_is_found_through_the_membership(db, register):
    register("alice@example.com")
    register("bob@example.com")

    alice, alice_workspace = user_and_workspace(db, "alice@example.com")
    bob, bob_workspace = user_and_workspace(db, "bob@example.com")

    assert alice_workspace is not None and bob_workspace is not None
    assert alice_workspace.id != bob_workspace.id
    membership = db.get(WorkspaceMember, (alice_workspace.id, alice.id))
    assert membership.role == ROLE_OWNER


def test_no_membership_means_no_workspace(db, register):
    register("alice@example.com")
    user = db.scalars(select(User)).one()
    db.query(WorkspaceMember).delete()
    db.commit()

    # The workspace still exists, but without a membership the query finds nothing
    assert db.scalars(select(Workspace)).one() is not None
    assert get_personal_workspace(db, user.id) is None


def test_an_organisation_workspace_is_not_the_personal_one(db, register):
    register("alice@example.com")
    user, personal = user_and_workspace(db)
    other = Workspace(name="School", kind=KIND_ORGANISATION)
    db.add(other)
    db.flush()
    db.add(WorkspaceMember(workspace_id=other.id, user_id=user.id, role=ROLE_OWNER))
    db.commit()

    assert get_personal_workspace(db, user.id).id == personal.id


def test_lock_uses_select_for_update_on_the_workspace_only(db, register):
    register("alice@example.com")
    user = db.scalars(select(User)).one()
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        get_personal_workspace(db, user.id, lock=True)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
        db.rollback()

    assert any("FOR UPDATE OF workspaces" in s for s in statements)


# --- Rules enforced by the database --------------------------------------------------------

def test_same_file_twice_in_one_workspace_is_refused_by_the_database(db, register):
    register("alice@example.com")
    user, workspace = user_and_workspace(db)
    first = new_document(workspace, user)
    db.add(first)
    db.commit()

    db.add(new_document(workspace, user, sha256=first.sha256))
    with pytest.raises(IntegrityError, match="uq_documents_workspace_id_sha256"):
        db.commit()


@pytest.mark.parametrize(
    "changes, constraint",
    [
        ({"status": "deleted"}, "ck_documents_status_valid"),
        ({"size_bytes": 0}, "ck_documents_size_positive"),
        ({"page_count": 0}, "ck_documents_page_count_positive"),
        ({"sha256": "NOT-A-HASH"}, "ck_documents_sha256_hex"),
    ],
)
def test_database_refuses_impossible_values(db, register, changes, constraint):
    register("alice@example.com")
    user, workspace = user_and_workspace(db)

    db.add(new_document(workspace, user, **changes))
    with pytest.raises(IntegrityError, match=constraint):
        db.commit()


def test_a_workspace_with_documents_cannot_be_deleted_directly(db, register):
    # Its files in storage must be deleted first, so the database refuses
    register("alice@example.com")
    user, workspace = user_and_workspace(db)
    db.add(new_document(workspace, user))
    db.commit()

    db.delete(workspace)
    with pytest.raises(IntegrityError, match="fk_documents_workspace_id_workspaces"):
        db.commit()


def test_deleting_the_uploader_keeps_the_document(db, register):
    register("alice@example.com")
    user, workspace = user_and_workspace(db)
    document = new_document(workspace, user)
    db.add(document)
    db.commit()

    db.query(WorkspaceMember).delete()
    db.delete(user)
    db.commit()
    db.expire_all()

    assert db.get(Document, document.id).uploaded_by is None
