"""What the frontend reads besides documents themselves: each processing
document's stage, and GET /limits. FakeAI and FakeStorage only; nothing
contacts OpenRouter or R2."""

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models import Document
from app.models.document import STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.services import processing
from tests.pdfs import LONG_TEXT, image_only_pdf, text_pdf

HANDBOOK = text_pdf([f"{n}. Topic {name}\n{LONG_TEXT}" for n, name in enumerate(["Welcome", "Pay", "Leave"], start=1)])


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


def stage_of(session_factory, document_id) -> str | None:
    with session_factory() as db:
        return db.scalar(select(Document.stage).where(Document.id == document_id))


@pytest.fixture
def stages_seen(session_factory, fake_ai, monkeypatch):
    """The document's stage, read from the database as each stage's work
    starts: reading the PDF, finding sections, and each embedding batch."""
    seen: list[str | None] = []
    current: dict[str, uuid.UUID] = {}

    def remember(document_id):
        current["id"] = document_id

    real_read = processing.read_pdf_with_time_limit
    real_prepare = processing.prepare
    real_embed = fake_ai.embed_batch

    def read(*args, **kwargs):
        seen.append(stage_of(session_factory, current["id"]))
        return real_read(*args, **kwargs)

    def prepare(*args, **kwargs):
        seen.append(stage_of(session_factory, current["id"]))
        return real_prepare(*args, **kwargs)

    def embed(texts, mode):
        if mode == "document":
            seen.append(stage_of(session_factory, current["id"]))
        return real_embed(texts, mode)

    monkeypatch.setattr(processing, "read_pdf_with_time_limit", read)
    monkeypatch.setattr(processing, "prepare", prepare)
    monkeypatch.setattr(fake_ai, "embed_batch", embed)

    real_process = processing.process_document

    def process(document_id, *args, **kwargs):
        remember(document_id)
        return real_process(document_id, *args, **kwargs)

    monkeypatch.setattr("app.routers.documents.process_document", process)
    return seen


def upload(client, headers, data=HANDBOOK):
    response = client.post("/documents", headers=headers, files={"file": ("handbook.pdf", data, "application/pdf")})
    assert response.status_code == 202, response.text
    return response.json()


# --- The processing stage ------------------------------------------------------------------------------

def test_an_upload_starts_at_extracting_and_moves_through_each_stage(client, session_factory, alice, stages_seen):
    body = upload(client, alice)

    # The 202 answer is sent before processing runs
    assert (body["status"], body["stage"]) == ("processing", "extracting")
    assert stages_seen == ["extracting", "finding_sections", "embedding"]
    finished = client.get(f"/documents/{body['id']}", headers=alice).json()
    assert (finished["status"], finished["stage"]) == ("ready", None)


def test_a_failed_document_has_no_stage(client, alice):
    body = upload(client, alice, image_only_pdf())

    failed = client.get(f"/documents/{body['id']}", headers=alice).json()
    assert (failed["status"], failed["error_code"], failed["stage"]) == ("failed", "no_text_layer", None)


def test_a_retry_starts_again_from_extracting(client, session_factory, alice, fake_ai, stages_seen):
    fake_ai.fail_on = {"embed"}
    body = upload(client, alice)
    assert client.get(f"/documents/{body['id']}", headers=alice).json()["error_code"] == "ai_unavailable"
    fake_ai.fail_on = set()
    stages_seen.clear()

    retried = client.post(f"/documents/{body['id']}/retry", headers=alice).json()

    assert (retried["status"], retried["stage"]) == ("processing", "extracting")
    # Pages and sections were kept, so it moves straight on to embedding
    assert stages_seen == ["embedding"]
    assert client.get(f"/documents/{body['id']}", headers=alice).json()["stage"] is None


def test_the_startup_check_clears_the_stage_of_a_stuck_document(db, session_factory, alice, make_document):
    document = make_document(status=STATUS_PROCESSING)
    assert document.stage == "extracting"  # set by the insert itself
    db.execute(update(Document).where(Document.id == document.id).values(updated_at=text("now() - interval '1 day'")))
    db.commit()

    processing.mark_stuck_documents(session_factory, timeout_minutes=1)

    db.expire_all()
    stuck = db.get(Document, document.id)
    assert (stuck.status, stuck.error_code, stuck.stage) == (STATUS_FAILED, "interrupted", None)


@pytest.mark.parametrize("status", [STATUS_READY, STATUS_FAILED])
def test_only_a_processing_document_can_have_a_stage(db, alice, make_document, status):
    document = make_document(status=status)
    assert document.stage is None

    with pytest.raises(IntegrityError, match="ck_documents_stage_valid"):
        db.execute(update(Document).where(Document.id == document.id).values(stage="embedding"))
    db.rollback()


def test_a_stage_must_be_a_known_one(db, alice, make_document):
    document = make_document(status=STATUS_PROCESSING)

    with pytest.raises(IntegrityError, match="ck_documents_stage_valid"):
        db.execute(update(Document).where(Document.id == document.id).values(stage="thinking"))
    db.rollback()


# --- GET /limits ---------------------------------------------------------------------------------------------

def test_limits_need_a_login(client):
    response = client.get("/limits")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_limits_are_read_from_the_settings(client, alice, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 7)
    monkeypatch.setattr(settings, "max_pages", 42)
    monkeypatch.setattr(settings, "max_documents_per_workspace", 3)
    monkeypatch.setattr(settings, "daily_questions_per_user", 11)

    assert client.get("/limits", headers=alice).json() == {
        "max_upload_mb": 7,
        "max_pages": 42,
        "max_documents_per_workspace": 3,
        "questions_per_day": 11,
    }
