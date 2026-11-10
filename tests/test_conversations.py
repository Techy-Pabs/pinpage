"""Conversations and ownership. FakeAI and FakeStorage only."""

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models import Conversation, Message, WorkspaceMember
from app.models.document import STATUS_FAILED, STATUS_PROCESSING
from tests.pdfs import LONG_TEXT, text_pdf

CONVERSATION_FIELDS = {"id", "document_id", "title", "created_at", "updated_at"}
NOT_FOUND = {"error": {"code": "conversation_not_found", "message": "Conversation not found."}}
DOCUMENT_NOT_FOUND = {"error": {"code": "document_not_found", "message": "Document not found."}}


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    monkeypatch.setattr(settings, "max_documents_per_workspace", 10)


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(auth_headers):
    return auth_headers("bob@example.com")


@pytest.fixture
def ready_document(client):
    """ready_document(headers, text) -> the id of a processed, ready document."""

    def _ready_document(headers, text=LONG_TEXT):
        response = client.post(
            "/documents", headers=headers, files={"file": ("Handbook.pdf", text_pdf([f"1. Leave\n{text}"]), "application/pdf")}
        )
        assert response.status_code == 202
        document_id = response.json()["id"]
        assert client.get(f"/documents/{document_id}", headers=headers).json()["status"] == "ready"
        return document_id

    return _ready_document


@pytest.fixture
def start(client):
    def _start(headers, document_id):
        response = client.post(f"/documents/{document_id}/conversations", headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _start


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def add_pair(db, conversation_id, question="How much leave?", answer="Five days [S1]."):
    """A question and its answer, saved in one transaction as the API does."""
    db.add(Message(conversation_id=uuid.UUID(conversation_id), role="user", content=question))
    db.flush()
    db.add(Message(conversation_id=uuid.UUID(conversation_id), role="assistant", content=answer,
                   outcome="answered", citations=[], mode="question"))
    db.commit()


# --- Start, list, get, delete -----------------------------------------------------------------

def test_start_a_conversation_with_a_ready_document(alice, ready_document, start):
    document_id = ready_document(alice)

    conversation = start(alice, document_id)

    assert set(conversation) == CONVERSATION_FIELDS
    assert conversation["document_id"] == document_id
    assert conversation["title"] == "New conversation"


@pytest.mark.parametrize("status", [STATUS_PROCESSING, STATUS_FAILED])
def test_cannot_start_a_conversation_with_a_document_that_is_not_ready(client, alice, make_document, status):
    document = make_document(status=status, error_code="interrupted" if status == STATUS_FAILED else None)

    response = client.post(f"/documents/{document.id}/conversations", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


def test_list_your_conversations_with_a_document_newest_first(client, alice, ready_document, start):
    document_id = ready_document(alice)
    other_document = ready_document(alice, text=f"Something else. {LONG_TEXT}")
    first = start(alice, document_id)
    second = start(alice, document_id)
    start(alice, other_document)

    response = client.get(f"/documents/{document_id}/conversations", headers=alice)

    assert response.status_code == 200
    assert [c["id"] for c in response.json()] == [second["id"], first["id"]]


def test_get_a_conversation_with_its_messages_oldest_first(client, db, alice, ready_document, start):
    conversation = start(alice, ready_document(alice))
    add_pair(db, conversation["id"], "First question?", "First answer [S1].")
    add_pair(db, conversation["id"], "Second question?", "Second answer [S1].")

    response = client.get(f"/conversations/{conversation['id']}", headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CONVERSATION_FIELDS | {"messages"}
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "First question?"),
        ("assistant", "First answer [S1]."),
        ("user", "Second question?"),
        ("assistant", "Second answer [S1]."),
    ]
    assert body["messages"][0]["outcome"] is None and body["messages"][0]["citations"] is None
    assert body["messages"][1]["outcome"] == "answered"
    assert body["messages"][1]["mode"] == "question" and body["messages"][1]["retrieval_query"] is None
    assert body["messages"][0]["mode"] is None


def test_delete_a_conversation_and_its_messages(client, db, alice, ready_document, start):
    conversation = start(alice, ready_document(alice))
    add_pair(db, conversation["id"])

    response = client.delete(f"/conversations/{conversation['id']}", headers=alice)

    assert response.status_code == 204
    assert count(db, Conversation) == 0 and count(db, Message) == 0
    assert client.get(f"/conversations/{conversation['id']}", headers=alice).json() == NOT_FOUND


def test_deleting_a_document_deletes_its_conversations(client, db, alice, ready_document, start):
    document_id = ready_document(alice)
    conversation = start(alice, document_id)
    add_pair(db, conversation["id"])

    assert client.delete(f"/documents/{document_id}", headers=alice).status_code == 204

    assert count(db, Conversation) == 0 and count(db, Message) == 0


# --- Ownership ---------------------------------------------------------------------------------

def test_other_users_get_404_on_your_conversations_for_every_endpoint(client, db, alice, bob, ready_document, start):
    document_id = ready_document(alice)
    conversation = start(alice, document_id)
    add_pair(db, conversation["id"])

    attempts = {
        "get": client.get(f"/conversations/{conversation['id']}", headers=bob),
        "delete": client.delete(f"/conversations/{conversation['id']}", headers=bob),
        "ask": client.post(f"/conversations/{conversation['id']}/messages", headers=bob, json={"content": "Hi?"}),
    }
    for name, response in attempts.items():
        assert response.status_code == 404, name
        assert response.json() == NOT_FOUND, name

    # Nor through the document
    assert client.get(f"/documents/{document_id}/conversations", headers=bob).json() == DOCUMENT_NOT_FOUND
    # Bob's delete changed nothing
    assert count(db, Conversation) == 1 and count(db, Message) == 2


def test_other_users_cannot_start_a_conversation_with_your_document(client, db, bob, alice, ready_document):
    document_id = ready_document(alice)

    response = client.post(f"/documents/{document_id}/conversations", headers=bob)

    assert response.json() == DOCUMENT_NOT_FOUND
    assert count(db, Conversation) == 0


def test_conversation_is_unreachable_once_its_document_is(client, db, alice, ready_document, start):
    # Alice started it, but has lost her membership: the query checks both
    conversation = start(alice, ready_document(alice))
    db.execute(delete(WorkspaceMember))
    db.commit()

    assert client.get(f"/conversations/{conversation['id']}", headers=alice).json() == NOT_FOUND
    assert client.delete(f"/conversations/{conversation['id']}", headers=alice).status_code == 404


def test_a_conversation_is_private_even_to_someone_who_can_reach_the_document(client, db, alice, bob, ready_document, start):
    # As in a shared workspace (Milestone 7): Bob is made a member of
    # Alice's workspace, so he reaches her document but not her conversation
    document_id = ready_document(alice)
    conversation = start(alice, document_id)
    workspace_id = db.scalars(select(WorkspaceMember.workspace_id)).first()
    bob_id = uuid.UUID(client.get("/auth/me", headers=bob).json()["id"])
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=bob_id, role="member"))
    db.commit()

    assert client.get(f"/documents/{document_id}", headers=bob).status_code == 200
    assert client.get(f"/conversations/{conversation['id']}", headers=bob).json() == NOT_FOUND
    assert client.get(f"/documents/{document_id}/conversations", headers=bob).json() == []


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", str(uuid.uuid4())], ids=["text", "number", "unknown"])
@pytest.mark.parametrize("method, path", [("get", "/conversations/{id}"), ("delete", "/conversations/{id}")])
def test_missing_or_malformed_conversation_ids_get_404(client, alice, bad_id, method, path):
    response = getattr(client, method)(path.format(id=bad_id), headers=alice)

    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/documents/{id}/conversations"),
        ("get", "/documents/{id}/conversations"),
        ("get", "/conversations/{id}"),
        ("delete", "/conversations/{id}"),
        ("post", "/conversations/{id}/messages"),
    ],
)
def test_conversation_routes_need_a_login(client, method, path):
    kwargs = {"json": {"content": "Hi?"}} if method == "post" else {}
    assert getattr(client, method)(path.format(id=uuid.uuid4()), **kwargs).status_code == 401


# --- Database rules -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "values",
    [
        {"role": "system", "outcome": None, "citations": None},
        {"role": "user", "outcome": "answered", "citations": None},  # questions have no outcome
        {"role": "assistant", "outcome": None, "citations": []},  # answers always have one
        {"role": "assistant", "outcome": "maybe", "citations": []},
        {"role": "assistant", "outcome": "answered", "citations": None},
        {"role": "assistant", "outcome": "answered", "citations": [], "mode": None},  # answers have a mode
        {"role": "assistant", "outcome": "answered", "citations": [], "mode": "summary"},
        {"role": "user", "outcome": None, "citations": None, "mode": "question"},  # questions don't
        {"role": "user", "outcome": None, "citations": None, "retrieval_query": "x"},
    ],
)
def test_database_refuses_impossible_messages(db, alice, ready_document, start, values):
    conversation = start(alice, ready_document(alice))

    db.add(Message(conversation_id=uuid.UUID(conversation["id"]), content="x", **values))
    with pytest.raises(IntegrityError):
        db.commit()
