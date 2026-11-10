"""Stage 3 (embeddings), ai_unavailable, retry, and the sections endpoint.
The AI is FakeAI and files live in FakeStorage: nothing contacts OpenRouter or R2."""

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from app.core.config import settings
from app.dependencies import get_ai
from app.main import app
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection
from app.models.document import STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.routers import documents as documents_router
from app.services import processing
from app.services.ai import OpenRouterAI, RequestPacer
from tests.pdfs import LONG_TEXT, damaged_pdf, text_pdf

AI_UNAVAILABLE = "Pinpage couldn't prepare this document for questions right now. Try again later."
INTERRUPTED = "Processing was interrupted. Try again."

# Five sections on five pages, so five chunks
HANDBOOK = text_pdf([f"{n}. Topic {name}\n{LONG_TEXT}" for n, name in enumerate(
    ["Welcome", "Pay", "Leave", "Conduct", "Safety"], start=1
)])


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    monkeypatch.setattr(settings, "max_documents_per_workspace", 10)
    monkeypatch.setattr(settings, "max_pages", 100)
    monkeypatch.setattr(settings, "chunk_size", 1000)
    monkeypatch.setattr(settings, "chunk_overlap", 150)
    monkeypatch.setattr(settings, "embedding_batch_size", 64)


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


@pytest.fixture
def upload(client, alice):
    """upload(data) -> the document id, after processing ran."""

    def _upload(data=HANDBOOK, filename="Staff Handbook.pdf", headers=None):
        response = client.post(
            "/documents", headers=headers or alice, files={"file": (filename, data, "application/pdf")}
        )
        assert response.status_code == 202, response.text
        return uuid.UUID(response.json()["id"])

    return _upload


def reload(db, document_id) -> Document:
    db.expire_all()
    return db.get(Document, document_id)


def count(db, model, document_id) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model).where(model.document_id == document_id))


def embedded(db, document_id) -> int:
    db.expire_all()
    return db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id, DocumentChunk.embedding.is_not(None))
    )


class ReadCounter:
    """Wraps FakeStorage.read to count how often the file is read."""

    def __init__(self, storage, monkeypatch):
        self.calls = 0
        real = storage.read

        def counted(key):
            self.calls += 1
            return real(key)

        monkeypatch.setattr(storage, "read", counted)


# --- Stage 3: embeddings ----------------------------------------------------------------------------

def test_ready_document_has_every_chunk_embedded(db, upload, fake_ai):
    document_id = upload()

    document = reload(db, document_id)
    assert document.status == STATUS_READY and document.page_count == 5
    chunks = count(db, DocumentChunk, document_id)
    assert chunks == 5 and embedded(db, document_id) == 5
    assert [call["mode"] for call in fake_ai.embed_calls] == ["document"]


def test_embedding_text_starts_with_the_document_and_section_label(db, upload, fake_ai):
    upload()

    texts = fake_ai.embed_calls[0]["texts"]
    assert texts[0].startswith("Staff Handbook > 1 Topic Welcome\n\n1. Topic Welcome")
    assert texts[2].startswith("Staff Handbook > 3 Topic Leave\n\n")
    # The stored chunk keeps the original text, without the label
    first = db.scalars(select(DocumentChunk).order_by(DocumentChunk.position)).first()
    assert first.content.startswith("1. Topic Welcome")


def test_embeddings_are_requested_in_batches(db, upload, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "embedding_batch_size", 2)

    document_id = upload()

    assert [len(call["texts"]) for call in fake_ai.embed_calls] == [2, 2, 1]
    assert reload(db, document_id).status == STATUS_READY


def test_embedding_failure_ends_as_ai_unavailable_and_keeps_pages_sections_and_chunks(db, upload, fake_ai):
    fake_ai.fail_on = {"embed"}

    document_id = upload()

    document = reload(db, document_id)
    assert document.status == STATUS_FAILED
    assert document.error_code == "ai_unavailable"
    assert document.error_message == AI_UNAVAILABLE
    assert document.page_count is None
    assert count(db, DocumentPage, document_id) == 5
    assert count(db, DocumentSection, document_id) == 5
    assert count(db, DocumentChunk, document_id) == 5
    assert embedded(db, document_id) == 0


def test_missing_openrouter_key_ends_as_ai_unavailable(db, upload):
    no_key = OpenRouterAI(
        api_key=None, base_url="https://openrouter.ai/api/v1", chat_models=["m"], embedding_model="e",
        dimensions=2048, timeout_seconds=1, max_tokens=10, pacer=RequestPacer(15),
    )
    app.dependency_overrides[get_ai] = lambda: no_key

    document_id = upload()

    assert reload(db, document_id).error_code == "ai_unavailable"


def test_unexpected_database_error_ends_as_interrupted(db, upload, monkeypatch):
    def broken(*args):
        raise RuntimeError("database went away")

    monkeypatch.setattr(processing, "_finish_ready", broken)

    document_id = upload()

    document = reload(db, document_id)
    assert document.error_code == "interrupted"
    assert document.error_message == INTERRUPTED
    assert count(db, DocumentChunk, document_id) == 5  # kept for the retry


# --- Retry ------------------------------------------------------------------------------------------

def test_retry_after_ai_unavailable_only_redoes_the_embeddings(client, db, alice, upload, fake_ai, fake_storage, monkeypatch):
    fake_ai.fail_on = {"embed"}
    document_id = upload()
    chunk_ids = set(db.scalars(select(DocumentChunk.id)))
    reads = ReadCounter(fake_storage, monkeypatch)
    fake_ai.fail_on = set()
    fake_ai.embed_calls.clear()

    response = client.post(f"/documents/{document_id}/retry", headers=alice)

    assert response.status_code == 202
    assert response.json()["status"] == STATUS_PROCESSING  # the answer is sent before processing runs
    document = reload(db, document_id)
    assert document.status == STATUS_READY and document.page_count == 5
    assert reads.calls == 0  # stages 1 and 2 were not redone
    assert set(db.scalars(select(DocumentChunk.id))) == chunk_ids  # the same chunks
    assert embedded(db, document_id) == 5


def test_retry_embeds_only_the_chunks_still_missing(db, client, alice, upload, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "embedding_batch_size", 2)
    fake_ai.fail_after_batches = 1  # the first batch works, then the AI goes down
    document_id = upload()
    assert reload(db, document_id).error_code == "ai_unavailable"
    assert embedded(db, document_id) == 2
    fake_ai.fail_after_batches = None
    fake_ai.embed_calls.clear()

    client.post(f"/documents/{document_id}/retry", headers=alice)

    assert [len(call["texts"]) for call in fake_ai.embed_calls] == [2, 1]  # 3 missing, not 5
    assert reload(db, document_id).status == STATUS_READY


def test_retry_after_interrupted_redoes_the_unfinished_stages(client, db, alice, upload, fake_storage, monkeypatch):
    fake_storage.fail_on = {"read"}  # storage down: nothing was extracted
    document_id = upload()
    assert reload(db, document_id).error_code == "interrupted"
    assert count(db, DocumentPage, document_id) == 0
    fake_storage.fail_on = set()

    assert client.post(f"/documents/{document_id}/retry", headers=alice).status_code == 202

    document = reload(db, document_id)
    assert document.status == STATUS_READY
    assert count(db, DocumentPage, document_id) == 5 and embedded(db, document_id) == 5


def test_retry_after_an_interruption_during_embedding_resumes_there(client, db, alice, upload, fake_ai, fake_storage, monkeypatch):
    fake_ai.fail_on = {"embed"}
    document_id = upload()
    # As if the server had restarted while embedding: the startup check
    # marks it interrupted, keeping what was done
    db.execute(update(Document).values(error_code="interrupted", error_message=INTERRUPTED))
    db.commit()
    reads = ReadCounter(fake_storage, monkeypatch)
    fake_ai.fail_on = set()

    client.post(f"/documents/{document_id}/retry", headers=alice)

    assert reload(db, document_id).status == STATUS_READY
    assert reads.calls == 0


@pytest.mark.parametrize("code", ["unreadable", "encrypted", "no_text_layer", "too_many_pages"])
def test_retry_is_refused_for_failures_the_file_causes(client, alice, register, make_document, code):
    document = make_document(status=STATUS_FAILED, error_code=code, error_message="x")

    response = client.post(f"/documents/{document.id}/retry", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retry_not_allowed"


@pytest.mark.parametrize("status", [STATUS_READY, STATUS_PROCESSING])
def test_retry_is_refused_unless_failed(client, alice, make_document, status):
    document = make_document(status=status)

    response = client.post(f"/documents/{document.id}/retry", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retry_not_allowed"


def test_retry_of_a_damaged_file_upload_is_refused(client, db, alice, upload):
    document_id = upload(damaged_pdf(), filename="broken.pdf")

    assert client.post(f"/documents/{document_id}/retry", headers=alice).status_code == 409
    assert reload(db, document_id).error_code == "unreadable"


def test_retry_respects_the_document_limit(client, db, alice, make_document, monkeypatch):
    monkeypatch.setattr(settings, "max_documents_per_workspace", 2)
    make_document(status=STATUS_READY)
    make_document(status=STATUS_READY)
    failed = make_document(status=STATUS_FAILED, error_code="ai_unavailable", error_message=AI_UNAVAILABLE)

    response = client.post(f"/documents/{failed.id}/retry", headers=alice)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "document_limit_reached"
    assert reload(db, failed.id).status == STATUS_FAILED


def test_retry_schedules_processing_with_the_requests_storage_and_ai(client, alice, make_document, fake_storage, fake_ai, monkeypatch):
    scheduled = []
    monkeypatch.setattr(documents_router, "process_document", lambda *args: scheduled.append(args))
    failed = make_document(status=STATUS_FAILED, error_code="interrupted", error_message=INTERRUPTED)

    client.post(f"/documents/{failed.id}/retry", headers=alice)

    [(document_id, storage, ai, _)] = scheduled
    assert (document_id, storage, ai) == (failed.id, fake_storage, fake_ai)


def test_other_users_get_404_on_retry_and_sections(client, auth_headers, alice, upload, make_document):
    document_id = upload()
    failed = make_document(status=STATUS_FAILED, error_code="interrupted", error_message=INTERRUPTED)
    bob = auth_headers("bob@example.com")

    for path in (f"/documents/{failed.id}/retry",):
        response = client.post(path, headers=bob)
        assert response.status_code == 404 and response.json()["error"]["code"] == "document_not_found"
    response = client.get(f"/documents/{document_id}/sections", headers=bob)
    assert response.status_code == 404 and response.json()["error"]["code"] == "document_not_found"


@pytest.mark.parametrize("method, path", [("post", "/documents/{id}/retry"), ("get", "/documents/{id}/sections")])
def test_new_routes_need_a_login_and_a_real_id(client, alice, method, path):
    assert getattr(client, method)(path.format(id=uuid.uuid4())).status_code == 401
    assert getattr(client, method)(path.format(id="not-a-uuid"), headers=alice).status_code == 404


# --- Sections endpoint ----------------------------------------------------------------------------

def test_sections_of_a_ready_document_in_order(client, alice, upload):
    document_id = upload()

    response = client.get(f"/documents/{document_id}/sections", headers=alice)

    assert response.status_code == 200
    assert response.json() == [
        {"number": str(n), "title": f"Topic {name}", "level": 1, "start_page": n, "end_page": n, "kind": "body"}
        for n, name in enumerate(["Welcome", "Pay", "Leave", "Conduct", "Safety"], start=1)
    ]


def test_sections_need_a_ready_document(client, alice, upload, fake_ai):
    fake_ai.fail_on = {"embed"}
    document_id = upload()  # failed with ai_unavailable, sections kept

    response = client.get(f"/documents/{document_id}/sections", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


def test_pages_of_a_failed_document_with_kept_pages_are_not_ready(client, alice, upload, fake_ai):
    fake_ai.fail_on = {"embed"}
    document_id = upload()

    assert client.get(f"/documents/{document_id}/pages/1", headers=alice).status_code == 409


def test_deleting_a_failed_document_removes_what_it_kept(client, db, alice, upload, fake_ai):
    fake_ai.fail_on = {"embed"}
    document_id = upload()

    assert client.delete(f"/documents/{document_id}", headers=alice).status_code == 204

    for model in (DocumentPage, DocumentSection, DocumentChunk):
        assert count(db, model, document_id) == 0
