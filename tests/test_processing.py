"""Background processing: from an uploaded PDF to pages, or a clear failure.

Files are read from FakeStorage; nothing contacts R2.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.dependencies import get_session_factory
from app.main import app
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection
from app.models.document import STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.services import processing
from app.services.processing import ProcessingFailed, extract_pages, mark_stuck_documents, process_document
from tests.pdfs import LONG_TEXT, damaged_pdf, encrypted_pdf, image_only_pdf, make_pdf, outlined_pdf, text_pdf

MESSAGES = {
    "too_many_pages": "This document has more than 100 pages. Upload a shorter file.",
    "encrypted": "This PDF is password protected. Remove the password and upload it again.",
    "no_text_layer": "This looks like a scanned document. Pinpage can't read scans yet.",
    "unreadable": "We couldn't read this PDF. It may be damaged.",
    "interrupted": "Processing was interrupted. Try again.",
    "ai_unavailable": "Pinpage couldn't prepare this document for questions right now. Try again later.",
}


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 100)
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    monkeypatch.setattr(settings, "processing_timeout_minutes", 15)


@pytest.fixture
def upload(client, auth_headers):
    """upload(data) -> the stored Document after its background task ran
    (TestClient runs background tasks before returning)."""
    headers = auth_headers()

    def _upload(data: bytes, filename="file.pdf"):
        response = client.post("/documents", headers=headers, files={"file": (filename, data, "application/pdf")})
        assert response.status_code == 202, response.text
        return uuid.UUID(response.json()["id"])

    return _upload


def reload(db, document_id) -> Document:
    db.expire_all()
    return db.get(Document, document_id)


def pages_of(db, document_id) -> list[DocumentPage]:
    db.expire_all()
    return db.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    ).all()


# --- Success ---------------------------------------------------------------------------------

def test_text_pdf_becomes_ready_with_one_row_per_page(db, upload):
    texts = [f"Page {n}: {LONG_TEXT}" for n in (1, 2, 3)]

    document_id = upload(text_pdf(texts))

    document = reload(db, document_id)
    assert document.status == STATUS_READY
    assert document.page_count == 3
    assert document.processed_at is not None
    assert document.error_code is None and document.error_message is None
    pages = pages_of(db, document_id)
    assert [p.page_number for p in pages] == [1, 2, 3]
    for page, text in zip(pages, texts):
        assert page.text.strip() == text
        assert page.char_count == len(page.text)


@pytest.mark.parametrize("algorithm", ["AES-256", "AES-128", "RC4-128"])
def test_pdf_encrypted_with_an_empty_password_is_readable(db, upload, algorithm):
    # Encrypted only to restrict editing: anyone can open it (decision 15)
    document_id = upload(encrypted_pdf("", algorithm))

    assert reload(db, document_id).status == STATUS_READY
    assert len(pages_of(db, document_id)) == 2


def test_exactly_max_pages_is_accepted(db, upload, monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 3)

    document_id = upload(text_pdf([LONG_TEXT] * 3))

    assert reload(db, document_id).page_count == 3


# --- Failures -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "make, code",
    [
        (lambda: text_pdf([f"{n} {LONG_TEXT}" for n in range(101)]), "too_many_pages"),
        (lambda: encrypted_pdf("open-sesame", "AES-256"), "encrypted"),
        (lambda: encrypted_pdf("open-sesame", "RC4-128"), "encrypted"),
        (lambda: image_only_pdf(3), "no_text_layer"),
        (damaged_pdf, "unreadable"),
        (lambda: make_pdf("Too short"), "no_text_layer"),  # under 20 characters
    ],
    ids=["101-pages", "encrypted-aes", "encrypted-rc4", "image-only", "damaged", "almost-no-text"],
)
def test_bad_files_fail_with_the_right_code_and_keep_no_pages(db, upload, make, code):
    document_id = upload(make())

    document = reload(db, document_id)
    assert document.status == STATUS_FAILED
    assert document.error_code == code
    assert document.error_message == MESSAGES[code]  # our sentence, never exception text
    assert document.page_count is None
    assert pages_of(db, document_id) == []


def test_page_limit_message_follows_the_setting(db, upload, monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 3)

    document_id = upload(text_pdf([LONG_TEXT] * 4))

    assert reload(db, document_id).error_message == "This document has more than 3 pages. Upload a shorter file."


@pytest.mark.parametrize(
    "texts, scanned",
    [
        ([LONG_TEXT] * 4 + [""], False),  # 20% empty: a real document with a blank page
        ([LONG_TEXT] + [""] * 4, True),  # 80% empty: a scan
        ([LONG_TEXT] * 2 + ["   short   "] * 8, True),  # spaces don't count as text
        ([""] * 5, True),
    ],
)
def test_scan_detection(texts, scanned):
    assert processing.looks_scanned(texts) is scanned


def test_nul_characters_are_removed_before_saving(monkeypatch):
    class Page:
        def extract_text(self):
            return f"Before\x00after. {LONG_TEXT}"

    class Reader:
        is_encrypted = False
        pages = [Page(), Page()]

        def __init__(self, stream):
            pass

    monkeypatch.setattr(processing, "PdfReader", Reader)

    texts = extract_pages(b"%PDF-", max_pages=100).texts

    assert all("\x00" not in text for text in texts)
    assert texts[0].startswith("Beforeafter.")


def test_pdf_with_no_pages_is_unreadable(monkeypatch):
    class Reader:
        is_encrypted = False
        pages = []

        def __init__(self, stream):
            pass

    monkeypatch.setattr(processing, "PdfReader", Reader)

    with pytest.raises(ProcessingFailed) as failure:
        extract_pages(b"%PDF-", max_pages=100)
    assert failure.value.code == "unreadable"


def test_failure_deletes_pages_already_saved(db, register, make_document, session_factory, fake_storage, fake_ai):
    register()
    document = make_document(data=damaged_pdf())
    # Pages left over from somewhere: a failed document must not keep any
    db.add(DocumentPage(document_id=document.id, page_number=1, text="old", char_count=3))
    db.commit()

    process_document(document.id, fake_storage, fake_ai, session_factory)

    assert reload(db, document.id).error_code == "unreadable"
    assert pages_of(db, document.id) == []


@pytest.mark.parametrize("problem", ["missing-file", "storage-down"])
def test_storage_problems_fail_as_interrupted(db, register, make_document, session_factory, fake_storage, problem, fake_ai):
    register()
    document = make_document(data=text_pdf([LONG_TEXT]))
    if problem == "missing-file":
        fake_storage.files.clear()
    else:
        fake_storage.fail_on = {"read"}

    process_document(document.id, fake_storage, fake_ai, session_factory)

    document = reload(db, document.id)
    assert document.status == STATUS_FAILED and document.error_code == "interrupted"
    assert document.error_message == MESSAGES["interrupted"]


def test_database_failure_while_saving_ends_as_interrupted_and_logs_no_text(
    db, register, make_document, session_factory, fake_storage, fake_ai, monkeypatch, caplog
):
    register()
    secret_text = "Confidential salary table for every employee"
    document = make_document(data=text_pdf([secret_text]))

    def broken_save(session_factory, document_id, prepared):
        # A real database error's message includes the parameters it was sending
        text = prepared.texts[0]
        raise IntegrityError(f"INSERT ... ({text})", {"text": text}, Exception(text))

    monkeypatch.setattr(processing, "_save_prepared", broken_save)

    with caplog.at_level(logging.INFO):
        process_document(document.id, fake_storage, fake_ai, session_factory)

    document = reload(db, document.id)
    assert document.status == STATUS_FAILED and document.error_code == "interrupted"
    assert "IntegrityError" in caplog.text
    assert secret_text not in caplog.text
    assert secret_text not in document.error_message


def test_logs_never_contain_document_text(db, upload, caplog):
    secret_text = "Disciplinary notes about a named employee"

    with caplog.at_level(logging.DEBUG):
        upload(text_pdf([secret_text]))

    assert secret_text not in caplog.text


# --- Documents that changed meanwhile ---------------------------------------------------------

def test_deleted_document_is_skipped_quietly(db, register, make_document, session_factory, fake_storage, caplog, fake_ai):
    register()
    document = make_document(data=text_pdf([LONG_TEXT]))
    db.execute(delete(Document).where(Document.id == document.id))
    db.commit()

    with caplog.at_level(logging.INFO):
        process_document(document.id, fake_storage, fake_ai, session_factory)  # no error

    assert "skipped" in caplog.text
    assert db.scalar(select(func.count()).select_from(DocumentPage)) == 0


def test_document_deleted_during_processing_keeps_no_pages(db, register, make_document, session_factory, fake_storage, monkeypatch, fake_ai):
    register()
    document = make_document(data=text_pdf([LONG_TEXT]))
    real_read = fake_storage.read

    def read_then_user_deletes(key):
        data = real_read(key)
        with session_factory() as other:
            other.execute(delete(Document).where(Document.id == document.id))
            other.commit()
        return data

    monkeypatch.setattr(fake_storage, "read", read_then_user_deletes)

    process_document(document.id, fake_storage, fake_ai, session_factory)  # no error

    assert db.scalar(select(func.count()).select_from(DocumentPage)) == 0
    assert db.scalar(select(func.count()).select_from(Document)) == 0


@pytest.mark.parametrize("status", [STATUS_READY, STATUS_FAILED])
def test_only_documents_still_processing_are_processed(db, register, make_document, session_factory, fake_storage, status, fake_ai):
    register()
    document = make_document(data=text_pdf([LONG_TEXT]), status=status, error_code="unreadable" if status == STATUS_FAILED else None)

    process_document(document.id, fake_storage, fake_ai, session_factory)

    assert reload(db, document.id).status == status  # untouched
    assert pages_of(db, document.id) == []


# --- Stuck documents ----------------------------------------------------------------------------

def make_aged(db, make_document, minutes_ago: int, status=STATUS_PROCESSING) -> Document:
    document = make_document(status=status)
    db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
    )
    db.commit()
    return document


def test_stuck_documents_are_marked_interrupted(db, register, make_document, session_factory):
    register()
    stuck = make_aged(db, make_document, minutes_ago=16)
    recent = make_aged(db, make_document, minutes_ago=5)
    old_ready = make_aged(db, make_document, minutes_ago=60, status=STATUS_READY)
    db.add(DocumentPage(document_id=stuck.id, page_number=1, text="partial", char_count=7))
    db.commit()

    assert mark_stuck_documents(session_factory, timeout_minutes=15) == 1

    stuck = reload(db, stuck.id)
    assert stuck.status == STATUS_FAILED
    assert stuck.error_code == "interrupted"
    assert stuck.error_message == MESSAGES["interrupted"]
    assert len(pages_of(db, stuck.id)) == 1  # kept, so a retry resumes from there
    assert reload(db, recent.id).status == STATUS_PROCESSING  # may still finish
    assert reload(db, old_ready.id).status == STATUS_READY


def test_stuck_check_runs_when_the_app_starts(db, client, register, make_document):
    register()
    stuck = make_aged(db, make_document, minutes_ago=30)
    assert app.dependency_overrides.get(get_session_factory) is None  # app.state is the only switch

    with TestClient(app):  # starting the app runs the check, with app.state's factory
        pass

    assert reload(db, stuck.id).error_code == "interrupted"


def test_app_makes_its_sessions_from_session_local_by_default():
    # Outside the client fixture, app.state holds the real factory
    from app.db.session import SessionLocal

    assert app.state.session_factory is SessionLocal


# --- Sections and chunks (stage 2) ---------------------------------------------------------------

def sections_of(db, document_id) -> list[DocumentSection]:
    db.expire_all()
    return db.scalars(
        select(DocumentSection).where(DocumentSection.document_id == document_id).order_by(DocumentSection.position)
    ).all()


def chunks_of(db, document_id) -> list[DocumentChunk]:
    db.expire_all()
    return db.scalars(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.position)
    ).all()


def test_pdf_with_an_outline_gets_sections_from_the_outline(db, upload):
    pdf = outlined_pdf(
        [f"Welcome. {LONG_TEXT}", f"Leave rules. {LONG_TEXT}", f"Annual leave. {LONG_TEXT}"],
        [("1 Welcome", 1, 1), ("2 Leave", 1, 2), ("2.1 Annual Leave", 2, 3)],
    )

    document_id = upload(pdf, filename="Staff Handbook.pdf")

    sections = sections_of(db, document_id)
    assert [(s.position, s.number, s.title, s.level, s.start_page, s.end_page) for s in sections] == [
        (0, "1", "Welcome", 1, 1, 1),
        (1, "2", "Leave", 1, 2, 3),
        (2, "2.1", "Annual Leave", 2, 3, 3),
    ]
    chunks = chunks_of(db, document_id)
    by_section = {s.id: s.title for s in sections}
    assert [(by_section[c.section_id], c.page_start, c.page_end) for c in chunks] == [
        ("Welcome", 1, 1), ("Leave", 2, 2), ("Annual Leave", 3, 3),
    ]
    assert all(c.embedding is not None for c in chunks)  # stage 3 embedded them


def test_pdf_without_an_outline_gets_sections_from_numbered_headings(db, upload):
    pdf = text_pdf([
        f"1. Welcome\n{LONG_TEXT}\n2. Pay\n{LONG_TEXT}",
        f"2.1 Overtime\n{LONG_TEXT}",
        f"3. Conduct\n{LONG_TEXT}",
    ])

    document_id = upload(pdf)

    assert [(s.number, s.title, s.level, s.start_page, s.end_page) for s in sections_of(db, document_id)] == [
        ("1", "Welcome", 1, 1, 1),
        ("2", "Pay", 1, 1, 2),
        ("2.1", "Overtime", 2, 2, 2),
        ("3", "Conduct", 1, 3, 3),
    ]


def test_pdf_with_neither_gets_one_section_named_after_the_document(db, upload):
    document_id = upload(text_pdf([f"Plain text. {LONG_TEXT}", f"More plain text. {LONG_TEXT}"]), filename="Visitor Policy.pdf")

    sections = sections_of(db, document_id)
    assert [(s.number, s.title, s.level, s.start_page, s.end_page) for s in sections] == [
        (None, "Visitor Policy", 1, 1, 2)
    ]
    chunks = chunks_of(db, document_id)
    assert len(chunks) == 1 and (chunks[0].page_start, chunks[0].page_end) == (1, 2)  # crosses the page break
    assert "Plain text." in chunks[0].content and "More plain text." in chunks[0].content


def test_failed_document_keeps_no_sections_or_chunks(db, register, make_document, session_factory, fake_storage, fake_ai):
    register()
    document = make_document(data=damaged_pdf())
    section = DocumentSection(document_id=document.id, position=0, title="Old", level=1, start_page=1, end_page=1)
    db.add(section)
    db.flush()
    db.add(DocumentChunk(document_id=document.id, section_id=section.id, position=0, page_start=1, page_end=1, content="old"))
    db.commit()

    process_document(document.id, fake_storage, fake_ai, session_factory)

    assert reload(db, document.id).error_code == "unreadable"
    assert sections_of(db, document.id) == [] and chunks_of(db, document.id) == []


def test_deleting_a_document_deletes_its_sections_and_chunks(db, upload):
    document_id = upload(text_pdf([f"1. Welcome\n{LONG_TEXT}"]))
    assert sections_of(db, document_id) and chunks_of(db, document_id)

    db.execute(delete(Document).where(Document.id == document_id))
    db.commit()

    assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0
