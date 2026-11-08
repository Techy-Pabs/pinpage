"""The document library: list, details, pages, file links and delete, and
the ownership sweep. Files live in FakeStorage; nothing contacts R2."""

import uuid

import pytest
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.models import Document, DocumentPage, WorkspaceMember
from app.models.document import STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.routers import documents as documents_router
from app.services.processing import process_document
from tests.pdfs import LONG_TEXT, text_pdf

RESPONSE_FIELDS = {
    "id", "title", "original_filename", "size_bytes", "page_count", "status",
    "error_code", "error_message", "created_at", "processed_at",
}
NOT_FOUND = {"error": {"code": "document_not_found", "message": "Document not found."}}
PAGE_ONE = f"First page. {LONG_TEXT}"
PAGE_TWO = f"Second page. {LONG_TEXT}"


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    monkeypatch.setattr(settings, "max_documents_per_workspace", 10)
    monkeypatch.setattr(settings, "max_pages", 100)
    monkeypatch.setattr(settings, "signed_url_seconds", 300)


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(auth_headers):
    return auth_headers("bob@example.com")


@pytest.fixture
def upload(client):
    """upload(headers, pages, filename) -> the response body, after
    processing ran (TestClient runs background tasks before returning)."""

    def _upload(headers, pages=(PAGE_ONE, PAGE_TWO), filename="Handbook.pdf"):
        response = client.post(
            "/documents", headers=headers, files={"file": (filename, text_pdf(list(pages)), "application/pdf")}
        )
        assert response.status_code == 202, response.text
        return response.json()

    return _upload


@pytest.fixture
def ready_document(alice, upload):
    """Alice's two page document, processed and ready."""
    return upload(alice)


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


# --- List ---------------------------------------------------------------------------------

def test_list_shows_your_documents_newest_first(client, alice, upload):
    first = upload(alice, [f"One {LONG_TEXT}"], "one.pdf")
    second = upload(alice, [f"Two {LONG_TEXT}"], "two.pdf")
    third = upload(alice, [f"Three {LONG_TEXT}"], "three.pdf")

    response = client.get("/documents", headers=alice)

    assert response.status_code == 200
    assert [d["id"] for d in response.json()] == [third["id"], second["id"], first["id"]]
    assert all(set(d) == RESPONSE_FIELDS for d in response.json())
    assert {d["status"] for d in response.json()} == {STATUS_READY}


def test_list_includes_every_status(client, alice, make_document):
    make_document(status=STATUS_PROCESSING)
    make_document(status=STATUS_FAILED, error_code="unreadable", error_message="We couldn't read this PDF.")

    statuses = sorted(d["status"] for d in client.get("/documents", headers=alice).json())

    assert statuses == [STATUS_FAILED, STATUS_PROCESSING]


def test_new_user_has_an_empty_list(client, alice):
    assert client.get("/documents", headers=alice).json() == []


# --- Details ------------------------------------------------------------------------------

def test_details_of_your_document(client, alice, ready_document):
    response = client.get(f"/documents/{ready_document['id']}", headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["status"] == STATUS_READY
    assert body["page_count"] == 2
    assert body["processed_at"] is not None


def test_failed_document_shows_its_code_and_message(client, alice, upload):
    document = upload(alice, [""] * 3)  # no text: fails as a scan

    body = client.get(f"/documents/{document['id']}", headers=alice).json()

    assert body["status"] == STATUS_FAILED
    assert body["error_code"] == "no_text_layer"
    assert body["error_message"] == "This looks like a scanned document. Pinpage can't read scans yet."


# --- Pages --------------------------------------------------------------------------------

def test_read_each_page(client, alice, ready_document):
    for number, text in [(1, PAGE_ONE), (2, PAGE_TWO)]:
        response = client.get(f"/documents/{ready_document['id']}/pages/{number}", headers=alice)

        assert response.status_code == 200
        body = response.json()
        assert body["page_number"] == number
        assert body["page_count"] == 2
        assert body["text"].strip() == text
        assert body["char_count"] == len(body["text"])


@pytest.mark.parametrize("page", ["3", "99", "0", "-1", "abc", "1.5"])
def test_page_past_the_end_or_invalid_is_not_found(client, alice, ready_document, page):
    response = client.get(f"/documents/{ready_document['id']}/pages/{page}", headers=alice)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "page_not_found"


@pytest.mark.parametrize("status", [STATUS_PROCESSING, STATUS_FAILED])
def test_page_of_a_document_that_is_not_ready(client, alice, make_document, status):
    document = make_document(status=status)

    response = client.get(f"/documents/{document.id}/pages/1", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


# --- File links ----------------------------------------------------------------------------

def test_file_link_for_a_ready_document(client, db, alice, ready_document, monkeypatch):
    monkeypatch.setattr(settings, "signed_url_seconds", 120)
    storage_key = db.get(Document, uuid.UUID(ready_document["id"])).storage_key

    response = client.get(f"/documents/{ready_document['id']}/file", headers=alice)

    assert response.status_code == 200
    assert response.json() == {
        "url": f"https://fake-storage.invalid/{storage_key}?expires=120",
        "expires_in": 120,
    }


@pytest.mark.parametrize("status", [STATUS_PROCESSING, STATUS_FAILED])
def test_no_file_link_for_a_document_that_is_not_ready(client, alice, make_document, status):
    document = make_document(status=status)

    response = client.get(f"/documents/{document.id}/file", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


def test_file_link_storage_failure_is_502(client, alice, ready_document, fake_storage):
    fake_storage.fail_on = {"signed_url"}

    response = client.get(f"/documents/{ready_document['id']}/file", headers=alice)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "storage_error"


# --- Delete ---------------------------------------------------------------------------------

def test_delete_removes_the_file_the_document_and_all_pages(client, db, alice, ready_document, fake_storage):
    assert count(db, DocumentPage) == 2 and len(fake_storage.files) == 1

    response = client.delete(f"/documents/{ready_document['id']}", headers=alice)

    assert response.status_code == 204
    assert response.content == b""
    assert fake_storage.files == {}
    assert count(db, Document) == 0
    assert count(db, DocumentPage) == 0
    assert client.get(f"/documents/{ready_document['id']}", headers=alice).status_code == 404


def test_storage_failure_on_delete_returns_502_and_keeps_everything(client, db, alice, ready_document, fake_storage):
    fake_storage.fail_on = {"delete"}

    response = client.delete(f"/documents/{ready_document['id']}", headers=alice)

    assert response.status_code == 502
    assert response.json() == {
        "error": {"code": "storage_error", "message": "File storage is unavailable. Please try again."}
    }
    assert count(db, Document) == 1 and count(db, DocumentPage) == 2
    assert len(fake_storage.files) == 1

    # Storage is back: trying again works
    fake_storage.fail_on = set()
    assert client.delete(f"/documents/{ready_document['id']}", headers=alice).status_code == 204
    assert count(db, Document) == 0 and fake_storage.files == {}


def test_delete_works_when_the_file_is_already_gone(client, db, alice, ready_document, fake_storage):
    fake_storage.files.clear()

    assert client.delete(f"/documents/{ready_document['id']}", headers=alice).status_code == 204
    assert count(db, Document) == 0


@pytest.mark.parametrize("status", [STATUS_PROCESSING, STATUS_FAILED])
def test_delete_works_in_any_status(client, db, alice, make_document, fake_storage, status):
    document = make_document(status=status)

    assert client.delete(f"/documents/{document.id}", headers=alice).status_code == 204
    assert count(db, Document) == 0 and fake_storage.files == {}


def test_deleting_while_processing_is_safe(client, db, alice, session_factory, fake_storage, monkeypatch):
    # Upload, but hold the background task back so the document stays processing
    held = []
    monkeypatch.setattr(documents_router, "process_document", lambda *args: held.append(args))
    response = client.post(
        "/documents", headers=alice, files={"file": ("a.pdf", text_pdf([LONG_TEXT]), "application/pdf")}
    )
    document_id = uuid.UUID(response.json()["id"])

    assert client.delete(f"/documents/{document_id}", headers=alice).status_code == 204

    # Now the held task runs: the row and file are gone, and it stops quietly
    [(task_document_id, storage, factory)] = held
    process_document(task_document_id, storage, factory)

    assert count(db, Document) == 0
    assert count(db, DocumentPage) == 0
    assert fake_storage.files == {}


# --- Ownership sweep --------------------------------------------------------------------------

def test_other_users_get_404_on_your_document_for_every_route(client, db, alice, bob, ready_document, fake_storage):
    document_id = ready_document["id"]
    attempts = {
        "details": client.get(f"/documents/{document_id}", headers=bob),
        "page": client.get(f"/documents/{document_id}/pages/1", headers=bob),
        "file": client.get(f"/documents/{document_id}/file", headers=bob),
        "delete": client.delete(f"/documents/{document_id}", headers=bob),
    }

    for name, response in attempts.items():
        assert response.status_code == 404, name
        # Exactly what a document that doesn't exist gets
        assert response.json() == NOT_FOUND, name

    # Bob's delete attempt changed nothing
    assert count(db, Document) == 1 and count(db, DocumentPage) == 2
    assert len(fake_storage.files) == 1
    assert client.get(f"/documents/{document_id}", headers=alice).status_code == 200
    assert client.get(f"/documents/{document_id}/pages/1", headers=alice).status_code == 200


def test_other_users_never_see_your_documents_in_their_list(client, alice, bob, upload):
    alice_document = upload(alice, [f"Alice's {LONG_TEXT}"])
    bob_document = upload(bob, [f"Bob's {LONG_TEXT}"])

    assert [d["id"] for d in client.get("/documents", headers=bob).json()] == [bob_document["id"]]
    assert [d["id"] for d in client.get("/documents", headers=alice).json()] == [alice_document["id"]]


def test_the_same_pdf_is_accepted_for_two_users(client, alice, bob):
    pdf = text_pdf([LONG_TEXT])
    for headers in (alice, bob):
        response = client.post("/documents", headers=headers, files={"file": ("same.pdf", pdf, "application/pdf")})
        assert response.status_code == 202


def test_user_without_a_membership_gets_nothing_back(client, db, alice, ready_document):
    # A valid token, but the membership is gone: every query filters it out
    db.execute(delete(WorkspaceMember))
    db.commit()

    assert client.get("/documents", headers=alice).json() == []
    assert client.get(f"/documents/{ready_document['id']}", headers=alice).json() == NOT_FOUND
    assert client.get(f"/documents/{ready_document['id']}/pages/1", headers=alice).status_code == 404
    assert client.get(f"/documents/{ready_document['id']}/file", headers=alice).status_code == 404


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/documents/{id}"),
        ("get", "/documents/{id}/pages/1"),
        ("get", "/documents/{id}/file"),
        ("delete", "/documents/{id}"),
    ],
)
@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", str(uuid.uuid4())], ids=["text", "number", "unknown-uuid"])
def test_missing_or_malformed_ids_get_404_document_not_found(client, alice, method, path, bad_id):
    response = getattr(client, method)(path.format(id=bad_id), headers=alice)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/documents"),
        ("get", "/documents/{id}"),
        ("get", "/documents/{id}/pages/1"),
        ("get", "/documents/{id}/file"),
        ("delete", "/documents/{id}"),
    ],
)
def test_every_library_route_needs_a_login(client, db, ready_document, method, path):
    response = getattr(client, method)(path.format(id=ready_document["id"]))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert count(db, Document) == 1


def test_responses_never_contain_storage_key_sha256_or_workspace_ids(client, db, alice, ready_document):
    document = db.get(Document, uuid.UUID(ready_document["id"]))
    responses = [
        client.get("/documents", headers=alice),
        client.get(f"/documents/{document.id}", headers=alice),
        client.get(f"/documents/{document.id}/pages/1", headers=alice),
    ]

    for response in responses:
        for value in (document.storage_key, document.sha256, str(document.workspace_id), "sha256", "workspace_id", "storage_key"):
            assert value not in response.text
