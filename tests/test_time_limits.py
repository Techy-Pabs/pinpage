"""Time limits: reading a PDF in a separate process (too_complex), and
the deadline for the whole of one answer. FakeAI and FakeStorage only."""

import functools
import multiprocessing
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection, Message, QuestionUsage
from app.models.document import STATUS_FAILED
from app.services import pdf_text, processing
from app.services.ai import AnswerTimeUp, answer_deadline, request_timeout
from app.services.pdf_text import ProcessingFailed, extract_pages, read_pdf_with_time_limit
from tests import slow_pdf_child
from tests.pdfs import LONG_TEXT, damaged_pdf, outlined_pdf
from tests.test_ai import chat, embeddings, make_ai
from tests.test_answers import (  # noqa: F401 (fixtures)
    ask,
    conversation_for,
    leave_conversation,
    make_ready_document,
)

TOO_COMPLEX = "This PDF took too long to read. Try saving it again as a new PDF, or upload a shorter file."


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


# --- Reading a PDF -------------------------------------------------------------------------------

def test_a_pdf_read_in_a_child_process_gives_the_same_pages_and_outline():
    data = outlined_pdf([f"1 Welcome\n{LONG_TEXT}", f"2 Leave\n{LONG_TEXT}"], [("1 Welcome", 1, 1), ("2 Leave", 1, 2)])

    assert read_pdf_with_time_limit(data, max_pages=100, seconds=30) == extract_pages(data, max_pages=100)


def test_failures_in_the_child_keep_their_code():
    with pytest.raises(ProcessingFailed) as failure:
        read_pdf_with_time_limit(damaged_pdf(), max_pages=100, seconds=30)
    assert failure.value.code == "unreadable"


def test_reading_over_the_time_limit_stops_the_child_with_too_complex():
    with pytest.raises(ProcessingFailed) as failure:
        read_pdf_with_time_limit(b"%PDF-", max_pages=100, seconds=0.5, target=slow_pdf_child.never_answers)

    assert failure.value.code == "too_complex"
    assert multiprocessing.active_children() == []  # stopped, not left running


def test_a_child_that_dies_without_an_answer_means_unreadable():
    with pytest.raises(ProcessingFailed) as failure:
        read_pdf_with_time_limit(b"%PDF-", max_pages=100, seconds=30, target=slow_pdf_child.dies)
    assert failure.value.code == "unreadable"


def test_an_upload_over_the_time_limit_fails_as_too_complex_keeps_nothing_and_cannot_be_retried(
    client, db, alice, monkeypatch
):
    monkeypatch.setattr(settings, "pdf_parse_time_limit_seconds", 0.5)
    monkeypatch.setattr(
        processing, "read_pdf_with_time_limit",
        functools.partial(pdf_text.read_pdf_with_time_limit, target=slow_pdf_child.never_answers),
    )
    data = outlined_pdf([f"1 Welcome\n{LONG_TEXT}"], [("1 Welcome", 1, 1)])

    response = client.post("/documents", headers=alice, files={"file": ("slow.pdf", data, "application/pdf")})

    document_id = uuid.UUID(response.json()["id"])
    db.expire_all()
    document = db.get(Document, document_id)
    assert (document.status, document.error_code, document.error_message) == (STATUS_FAILED, "too_complex", TOO_COMPLEX)
    for model in (DocumentPage, DocumentSection, DocumentChunk):
        assert db.scalar(select(func.count()).select_from(model).where(model.document_id == document_id)) == 0
    retry = client.post(f"/documents/{document_id}/retry", headers=alice)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "retry_not_allowed"
    shown = client.get(f"/documents/{document_id}", headers=alice).json()
    assert shown["error_code"] == "too_complex" and shown["error_message"] == TOO_COMPLEX


# --- The deadline for one answer --------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_without_a_deadline_a_request_gets_its_usual_timeout():
    assert request_timeout(45) == 45


def test_the_timeout_passed_to_each_request_shrinks_with_the_time_left():
    ai, calls = make_ai([embeddings([1.0, 0, 0, 0]), chat("Ready"), chat("unused")])
    clock = Clock()

    with answer_deadline(90, clock=clock):
        ai.embed_batch(["first"], "query")
        clock.now += 60
        ai.generate("System", [])
        clock.now += 31
        with pytest.raises(AnswerTimeUp) as error:
            ai.generate("System", [])

    assert [call["timeout"] for call in calls] == [45, 30]  # the usual 45, then the 30 seconds left
    assert error.value.retryable is False  # and no request was sent once time was up
    assert request_timeout(45) == 45  # the deadline ends with the answer


def test_every_request_of_one_answer_shares_its_deadline(alice, ask, leave_conversation, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "answer_time_limit_seconds", 5)
    fake_ai.delay = 0.2
    fake_ai.reply = "You can carry over five days [S1]."

    response = ask(alice, leave_conversation)

    assert response.status_code == 201
    embedding_timeout, reply_timeout = fake_ai.timeouts
    assert embedding_timeout <= 5 and reply_timeout <= embedding_timeout - 0.2


def test_a_slow_ai_past_the_answer_deadline_gives_503_with_nothing_saved_or_counted(
    db, alice, ask, leave_conversation, fake_ai, monkeypatch
):
    monkeypatch.setattr(settings, "answer_time_limit_seconds", 0.5)
    fake_ai.delay = 0.3  # the embedding fits, the reply runs past the deadline
    fake_ai.reply = "You can carry over five days [S1]."

    response = ask(alice, leave_conversation)

    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert len(fake_ai.timeouts) == 2 and fake_ai.reply_calls == []  # the reply timed out
    assert count(db, Message) == 0
    assert count(db, QuestionUsage) == 0


def test_once_the_deadline_has_passed_no_further_request_is_sent(alice, ask, leave_conversation, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "answer_time_limit_seconds", 0.2)
    fake_ai.delay = 0.1
    original_embed = fake_ai.embed_batch

    def slow_embed(texts, mode):
        vectors = original_embed(texts, mode)
        time.sleep(0.2)  # the time left is used up after the embedding
        return vectors

    fake_ai.embed_batch = slow_embed

    response = ask(alice, leave_conversation)

    assert response.status_code == 503
    assert len(fake_ai.timeouts) == 1 and fake_ai.reply_calls == []  # the chat request was never sent
