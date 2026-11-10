"""Background processing of an uploaded document, in three stages:

1. Extract pages: read the PDF from storage and extract each page's text.
2. Find sections and build chunks (no AI requests).
3. Create embeddings for the chunks (AI requests, batched and paced).

The document stays processing until all three finish, then becomes ready,
or failed with a code. Stages 1 and 2 are saved together; stage 3 saves
each batch as it goes. A retry resumes from the first unfinished stage:
if chunks exist, only the chunks still missing an embedding are embedded.

Logs here never contain document text or exception messages (a database
error's message can include the page text it was saving); only ids, codes
and exception type names.
"""

import io
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pypdf import PasswordType, PdfReader
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection
from app.models.document import (
    FAILURE_MESSAGES,
    RETRYABLE_CODES,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
)
from app.services.ai import AIClient, AIServiceError
from app.services.chunking import ChunkDraft, build_chunks, embedding_input, section_label
from app.services.sections import DocumentText, OutlineEntry, Section, detect_sections
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


# Outlines deeper or longer than this are cut short, so a hostile file
# can't make reading the outline expensive
MAX_OUTLINE_DEPTH = 10
MAX_OUTLINE_ENTRIES = 1000


@dataclass
class ExtractedPdf:
    texts: list[str]  # one per page, in order
    outline: list[OutlineEntry] = field(default_factory=list)


def read_outline(reader: PdfReader) -> list[OutlineEntry]:
    """The PDF's bookmarks, if it has any. Never fails: a broken outline
    just means sections come from the text instead."""
    entries: list[OutlineEntry] = []

    def walk(items, level: int) -> None:
        if level > MAX_OUTLINE_DEPTH:
            return
        for item in items:
            if len(entries) >= MAX_OUTLINE_ENTRIES:
                return
            if isinstance(item, list):
                walk(item, level + 1)  # the children of the entry before it
                continue
            page = reader.get_destination_page_number(item)
            title = str(getattr(item, "title", "") or "").replace("\x00", "")
            if title.strip() and page is not None and page >= 0:
                entries.append(OutlineEntry(title=title, level=level, page=page + 1))

    try:
        walk(reader.outline, 1)
    except Exception as error:
        logger.info("Ignoring an unreadable outline (%s)", type(error).__name__)
        return []
    return entries


def extract_pages(data: bytes, max_pages: int) -> ExtractedPdf:
    """The text of each page, in order, and the outline. Raises ProcessingFailed."""
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
    return ExtractedPdf(texts=texts, outline=read_outline(reader))


@dataclass
class Prepared:
    """Everything stages 1 and 2 produce, saved together."""

    texts: list[str]
    sections: list[Section]
    chunks: list[ChunkDraft]


def prepare(extracted: ExtractedPdf, title: str) -> Prepared:
    """Stage 2: sections and chunks, from the pages alone (no AI)."""
    doc = DocumentText(extracted.texts)
    sections = detect_sections(doc, extracted.outline, title)
    chunks = build_chunks(doc, sections, settings.chunk_size, settings.chunk_overlap)
    return Prepared(texts=extracted.texts, sections=sections, chunks=chunks)


def _delete_prepared(db: Session, document_id: uuid.UUID) -> None:
    """Remove pages, sections and chunks (chunks go with their sections)."""
    db.execute(delete(DocumentSection).where(DocumentSection.document_id == document_id))
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))


def _add_prepared(db: Session, document_id: uuid.UUID, prepared: Prepared) -> None:
    db.add_all(
        DocumentPage(document_id=document_id, page_number=number, text=text, char_count=len(text))
        for number, text in enumerate(prepared.texts, start=1)
    )
    section_rows = [
        DocumentSection(
            id=uuid.uuid4(),
            document_id=document_id,
            position=position,
            number=section.number,
            title=section.title,
            level=section.level,
            start_page=section.start_page,
            end_page=section.end_page,
            kind=section.kind,
        )
        for position, section in enumerate(prepared.sections)
    ]
    db.add_all(section_rows)
    db.flush()  # sections first: chunks point at them
    db.add_all(
        DocumentChunk(
            document_id=document_id,
            section_id=section_rows[chunk.section_index].id,
            position=chunk.position,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            content=chunk.content,
        )
        for chunk in prepared.chunks
    )


# --- Recording the results ------------------------------------------------------------

def _lock_if_processing(db: Session, document_id: uuid.UUID) -> Document | None:
    """The document, locked until commit, but only if it is still processing.
    None if it was deleted (or finished) meanwhile."""
    return db.scalar(
        select(Document)
        .where(Document.id == document_id, Document.status == STATUS_PROCESSING)
        .with_for_update()
    )


def _save_prepared(session_factory: sessionmaker, document_id: uuid.UUID, prepared: Prepared) -> bool:
    """Stages 1 and 2: pages, sections and chunks, saved together or not at
    all. False if the document was deleted meanwhile."""
    with session_factory() as db, db.begin():
        if _lock_if_processing(db, document_id) is None:
            logger.info("Document %s was deleted during processing; result dropped", document_id)
            return False
        _delete_prepared(db, document_id)
        _add_prepared(db, document_id, prepared)
    logger.info(
        "Document %s: %d pages, %d sections, %d chunks",
        document_id, len(prepared.texts), len(prepared.sections), len(prepared.chunks),
    )
    return True


def _prepared_exists(session_factory: sessionmaker, document_id: uuid.UUID) -> bool:
    """True if stages 1 and 2 finished: they save pages and chunks together,
    so both exist. Anything else (e.g. leftovers) means starting over."""
    with session_factory() as db:
        return bool(db.scalar(select(
            exists().where(DocumentChunk.document_id == document_id)
            & exists().where(DocumentPage.document_id == document_id)
        )))


def _embed_missing(session_factory: sessionmaker, ai: AIClient, document_id: uuid.UUID, title: str) -> bool:
    """Stage 3: embed every chunk still without an embedding, a batch at a
    time, saving each batch. False if the document was deleted meanwhile.
    Raises AIServiceError if a batch can't be embedded."""
    batch_size = settings.embedding_batch_size
    while True:
        with session_factory() as db:
            batch = db.execute(
                select(DocumentChunk.id, DocumentChunk.content, DocumentSection.number, DocumentSection.title)
                .join(DocumentSection, DocumentSection.id == DocumentChunk.section_id)
                .where(DocumentChunk.document_id == document_id, DocumentChunk.embedding.is_(None))
                .order_by(DocumentChunk.position)
                .limit(batch_size)
            ).all()
        # No session is open while waiting for the AI
        if not batch:
            return True

        texts = [
            embedding_input(section_label(title, number, section_title), content)
            for _, content, number, section_title in batch
        ]
        vectors = ai.embed_batch(texts, "document")
        if len(vectors) != len(batch):
            raise AIServiceError("missing embeddings")

        with session_factory() as db, db.begin():
            if _lock_if_processing(db, document_id) is None:
                logger.info("Document %s was deleted during embedding; stopped", document_id)
                return False
            db.execute(
                update(DocumentChunk),
                [{"id": chunk_id, "embedding": vector} for (chunk_id, *_), vector in zip(batch, vectors)],
            )


def _finish_ready(session_factory: sessionmaker, document_id: uuid.UUID) -> None:
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            return
        document.status = STATUS_READY
        document.page_count = db.scalar(
            select(func.count()).select_from(DocumentPage).where(DocumentPage.document_id == document_id)
        )
        document.error_code = None
        document.error_message = None
        document.processed_at = datetime.now(UTC)
    logger.info("Document %s is ready", document_id)


def _finish_failed(session_factory: sessionmaker, document_id: uuid.UUID, code: str) -> None:
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            return
        if code not in RETRYABLE_CODES:
            # The file itself is the problem: no pages, sections or chunks
            # are kept. Retryable failures keep what's done, so a retry
            # resumes from the first unfinished stage.
            _delete_prepared(db, document_id)
        document.status = STATUS_FAILED
        document.page_count = None
        document.error_code = code
        document.error_message = failure_message(code)
    logger.info("Document %s failed: %s", document_id, code)


# --- The background task ------------------------------------------------------------------

def process_document(
    document_id: uuid.UUID, storage: Storage, ai: AIClient, session_factory: sessionmaker
) -> None:
    """Runs after the upload (or retry) response is sent.

    Opens its own sessions (never the request's, which is closed by now)
    and reads the file from storage by its key, not from memory, so a job
    queue could run it later without changes.
    """
    try:
        with session_factory() as db:
            row = db.execute(
                select(Document.storage_key, Document.title).where(
                    Document.id == document_id, Document.status == STATUS_PROCESSING
                )
            ).first()
        # The session is closed here: nothing is held during the slow parts
        if row is None:
            logger.info("Document %s is not waiting for processing; skipped", document_id)
            return
        key, title = row

        # Stages 1 and 2, unless a previous run finished them
        if not _prepared_exists(session_factory, document_id):
            try:
                data = storage.read(key)
            except StorageError:
                # Storage is down, or the file is gone (the user may have just
                # deleted the document; then the row is gone too and this is a no-op)
                logger.warning("Could not read the file of document %s", document_id)
                _finish_failed(session_factory, document_id, "interrupted")
                return
            try:
                extracted = extract_pages(data, settings.max_pages)
            except ProcessingFailed as failure:
                _finish_failed(session_factory, document_id, failure.code)
                return
            if not _save_prepared(session_factory, document_id, prepare(extracted, title)):
                return

        # Stage 3
        try:
            if not _embed_missing(session_factory, ai, document_id, title):
                return
        except AIServiceError:
            # Pages, sections, chunks and any finished batches are kept, so a
            # retry only embeds what's missing
            _finish_failed(session_factory, document_id, "ai_unavailable")
            return

        _finish_ready(session_factory, document_id)
    except Exception as error:
        # The database (or something unplanned) failed. Only the type is
        # logged: the message could contain page text from a failed INSERT.
        logger.error("Processing document %s failed unexpectedly (%s)", document_id, type(error).__name__)
        try:
            _finish_failed(session_factory, document_id, "interrupted")
        except Exception as second:
            # Still processing; the startup check will mark it interrupted
            logger.error("Could not mark document %s as failed (%s)", document_id, type(second).__name__)


# --- At startup ------------------------------------------------------------------------------

def mark_stuck_documents(session_factory: sessionmaker, timeout_minutes: int) -> int:
    """Documents still processing after the timeout can't finish any more
    (the server restarted mid task): mark them failed with interrupted.

    Counted from updated_at, which an upload or a retry sets. What they had
    finished is kept, so a retry resumes from there. Uses the database's
    clock, so the app's clock can't make it wrong.
    """
    cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, timeout_minutes)
    with session_factory() as db, db.begin():
        stuck = db.scalars(
            update(Document)
            .where(Document.status == STATUS_PROCESSING, Document.updated_at < cutoff)
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
        logger.warning("Marked %d stuck document(s) as interrupted", len(stuck))
    return len(stuck)
