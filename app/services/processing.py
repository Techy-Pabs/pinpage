"""Background processing of an uploaded document: read the PDF from storage,
extract its text page by page, and mark the document ready or failed.

Logs here never contain document text or exception messages (a database
error's message can include the page text it was saving); only ids, codes
and exception type names.
"""

import io
import logging
import uuid
from datetime import UTC, datetime

from pypdf import PasswordType, PdfReader
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models import Document, DocumentPage
from app.models.document import FAILURE_MESSAGES, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.services.storage import Storage, StorageError

logger = logging.getLogger(__name__)

# A page with fewer characters than this (ignoring spaces) has no real text
MIN_CHARS_PER_PAGE = 20
# If at least this share of pages have no real text, it's a scan
SCANNED_SHARE = 0.8


class ProcessingFailed(Exception):
    """Processing failed for a reason the user is told about by its code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def failure_message(code: str) -> str:
    return FAILURE_MESSAGES[code].format(max_pages=settings.max_pages)


# --- Reading the PDF (no database, no storage) -------------------------------------

def looks_scanned(texts: list[str]) -> bool:
    empty = sum(1 for text in texts if len(text.strip()) < MIN_CHARS_PER_PAGE)
    return empty >= SCANNED_SHARE * len(texts)


def extract_pages(data: bytes, max_pages: int) -> list[str]:
    """The text of each page, in order. Raises ProcessingFailed."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many PDFs are encrypted only to restrict printing or copying,
            # with an empty password for opening; those are readable
            try:
                unlocked = reader.decrypt("") != PasswordType.NOT_DECRYPTED
            except Exception:
                unlocked = False
            if not unlocked:
                raise ProcessingFailed("encrypted")

        page_count = len(reader.pages)
        if page_count == 0:
            raise ProcessingFailed("unreadable")
        if page_count > max_pages:
            raise ProcessingFailed("too_many_pages")

        # PostgreSQL can't store NUL characters in text
        texts = [(page.extract_text() or "").replace("\x00", "") for page in reader.pages]
    except ProcessingFailed:
        raise
    except Exception:
        # Anything pypdf trips over: damaged or unsupported files
        raise ProcessingFailed("unreadable") from None

    if looks_scanned(texts):
        raise ProcessingFailed("no_text_layer")
    return texts


# --- Recording the result -------------------------------------------------------------

def _lock_if_processing(db: Session, document_id: uuid.UUID) -> Document | None:
    """The document, locked until commit, but only if it is still processing.
    None if it was deleted (or finished) meanwhile."""
    return db.scalar(
        select(Document)
        .where(Document.id == document_id, Document.status == STATUS_PROCESSING)
        .with_for_update()
    )


def _finish_ready(session_factory: sessionmaker, document_id: uuid.UUID, texts: list[str]) -> None:
    # One transaction: the pages and the ready status are saved together
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            logger.info("Document %s was deleted during processing; result dropped", document_id)
            return
        db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
        db.add_all(
            DocumentPage(document_id=document_id, page_number=number, text=text, char_count=len(text))
            for number, text in enumerate(texts, start=1)
        )
        document.status = STATUS_READY
        document.page_count = len(texts)
        document.error_code = None
        document.error_message = None
        document.processed_at = datetime.now(UTC)
    logger.info("Document %s is ready (%d pages)", document_id, len(texts))


def _finish_failed(session_factory: sessionmaker, document_id: uuid.UUID, code: str) -> None:
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            return
        # No pages are kept for a failed document
        db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
        document.status = STATUS_FAILED
        document.page_count = None
        document.error_code = code
        document.error_message = failure_message(code)
    logger.info("Document %s failed: %s", document_id, code)


# --- The background task ------------------------------------------------------------------

def process_document(document_id: uuid.UUID, storage: Storage, session_factory: sessionmaker) -> None:
    """Runs after the upload response is sent.

    Opens its own sessions (never the request's, which is closed by now)
    and reads the file from storage by its key, not from memory, so a job
    queue could run it later without changes.
    """
    try:
        with session_factory() as db:
            key = db.scalar(
                select(Document.storage_key).where(
                    Document.id == document_id, Document.status == STATUS_PROCESSING
                )
            )
        # The session is closed here: nothing is held during the slow part
        if key is None:
            logger.info("Document %s is not waiting for processing; skipped", document_id)
            return

        try:
            data = storage.read(key)
        except StorageError:
            # Storage is down, or the file is gone (the user may have just
            # deleted the document; then the row is gone too and this is a no-op)
            logger.warning("Could not read the file of document %s", document_id)
            # interrupted, not unreadable: the file itself may be fine
            _finish_failed(session_factory, document_id, "interrupted")
            return

        try:
            texts = extract_pages(data, settings.max_pages)
        except ProcessingFailed as failure:
            _finish_failed(session_factory, document_id, failure.code)
            return
        _finish_ready(session_factory, document_id, texts)
    except Exception as error:
        # Storage or the database failed. Only the type is logged: the
        # message could contain page text from a failed INSERT.
        logger.error("Processing document %s failed unexpectedly (%s)", document_id, type(error).__name__)
        try:
            _finish_failed(session_factory, document_id, "unreadable")
        except Exception as second:
            # Still processing; the startup check will mark it interrupted
            logger.error("Could not mark document %s as failed (%s)", document_id, type(second).__name__)


# --- At startup ------------------------------------------------------------------------------

def mark_stuck_documents(session_factory: sessionmaker, timeout_minutes: int) -> int:
    """Documents still processing after the timeout can't finish any more
    (the server restarted mid task): mark them failed with interrupted.

    Uses the database's clock, so the app's clock can't make it wrong.
    """
    cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, timeout_minutes)
    with session_factory() as db, db.begin():
        stuck = db.scalars(
            update(Document)
            .where(Document.status == STATUS_PROCESSING, Document.created_at < cutoff)
            .values(
                status=STATUS_FAILED,
                page_count=None,
                error_code="interrupted",
                error_message=failure_message("interrupted"),
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).all()
        if stuck:
            db.execute(delete(DocumentPage).where(DocumentPage.document_id.in_(stuck)))
    if stuck:
        logger.warning("Marked %d stuck document(s) as interrupted", len(stuck))
    return len(stuck)
