"""Background processing of an uploaded document, in three stages:

1. Extract pages: read the PDF from storage and extract each page's text,
   in a separate process stopped after PDF_PARSE_TIME_LIMIT_SECONDS.
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

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection
from app.models.document import (
    FAILURE_MESSAGES,
    RETRYABLE_CODES,
    STATUS_FAILED,
    STAGE_EMBEDDING,
    STAGE_EXTRACTING,
    STAGE_FINDING_SECTIONS,
    STATUS_PROCESSING,
    STATUS_READY,
)
from app.services.ai import AIClient, AIServiceError
from app.services.chunking import ChunkDraft, build_chunks, embedding_input, section_label
from app.services.pdf_text import (  # noqa: F401 (re-exported for callers of processing)
    MIN_CHARS_PER_PAGE,
    ExtractedPdf,
    ProcessingFailed,
    extract_pages,
    read_outline,
    read_pdf_with_time_limit,
)
from app.services.sections import DocumentText, Section, detect_sections
from app.services.storage import Storage, StorageError

logger = logging.getLogger(__name__)


def failure_message(code: str) -> str:
    return FAILURE_MESSAGES[code].format(max_pages=settings.max_pages)


# --- Sections and chunks (stage 2) ----------------------------------------------------------
# Stage 1, reading the PDF, is in app.services.pdf_text: it runs in a
# separate process with a time limit.

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


def _set_stage(session_factory: sessionmaker, document_id: uuid.UUID, stage: str) -> bool:
    """Records the stage a processing document has moved to. False if it was
    deleted (or finished) meanwhile."""
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            return False
        document.stage = stage
    return True


def _save_prepared(session_factory: sessionmaker, document_id: uuid.UUID, prepared: Prepared) -> bool:
    """Stages 1 and 2: pages, sections and chunks, saved together or not at
    all, with the move to the embedding stage. False if the document was
    deleted meanwhile."""
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            logger.info("Document %s was deleted during processing; result dropped", document_id)
            return False
        _delete_prepared(db, document_id)
        _add_prepared(db, document_id, prepared)
        document.stage = STAGE_EMBEDDING
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
    Raises AIServiceError if a batch can't be embedded. A batch embedded by
    a fake AI marks the document embedded_with_fake_ai."""
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
            document = _lock_if_processing(db, document_id)
            if document is None:
                logger.info("Document %s was deleted during embedding; stopped", document_id)
                return False
            db.execute(
                update(DocumentChunk),
                [{"id": chunk_id, "embedding": vector} for (chunk_id, *_), vector in zip(batch, vectors)],
            )
            if ai.is_fake:
                # Saved with the batch: the real AI must never use these vectors
                document.embedded_with_fake_ai = True


def _finish_ready(session_factory: sessionmaker, document_id: uuid.UUID) -> None:
    with session_factory() as db, db.begin():
        document = _lock_if_processing(db, document_id)
        if document is None:
            return
        document.status = STATUS_READY
        document.stage = None
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
        document.stage = None
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
                extracted = read_pdf_with_time_limit(data, settings.max_pages, settings.pdf_parse_time_limit_seconds)
            except ProcessingFailed as failure:
                _finish_failed(session_factory, document_id, failure.code)
                return
            if not _set_stage(session_factory, document_id, STAGE_FINDING_SECTIONS):
                return
            # Saved with the move to the embedding stage
            if not _save_prepared(session_factory, document_id, prepare(extracted, title)):
                return
        elif not _set_stage(session_factory, document_id, STAGE_EMBEDDING):
            return  # deleted meanwhile

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

def forget_fake_embeddings(db: Session, document: Document) -> None:
    """Every chunk loses its embedding, so stage 3 embeds them all again,
    and the fake AI mark is cleared. In the caller's transaction."""
    db.execute(update(DocumentChunk).where(DocumentChunk.document_id == document.id).values(embedding=None))
    document.embedded_with_fake_ai = False


def restart_processing(db: Session, document: Document) -> None:
    """A retry: the document is processing again, from its first unfinished
    stage. A document marked embedded_with_fake_ai, even partly (a fake run
    that failed with ai_unavailable), first loses every embedding and the
    mark, so the whole document is embedded again by whichever AI processes
    it. In the caller's transaction."""
    if document.embedded_with_fake_ai:
        forget_fake_embeddings(db, document)
    document.status = STATUS_PROCESSING
    document.stage = STAGE_EXTRACTING  # moves on at once past the stages already done
    document.error_code = None
    document.error_message = None
    document.processed_at = None


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
                stage=None,
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
