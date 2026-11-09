import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies import get_ai, get_current_user, get_db, get_session_factory, get_storage
from app.models import Document, DocumentSection, User, Workspace
from app.models.document import RETRYABLE_CODES, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.schemas.document import DocumentResponse, FileLinkResponse, PageResponse, SectionResponse
from app.services import documents as docs
from app.services.ai import AIClient
from app.services.processing import process_document
from app.services.storage import UNAVAILABLE_MESSAGE, Storage, StorageError, document_key
from app.services.uploads import Upload, UploadInvalid, UploadTooLarge, read_upload
from app.services.workspaces import get_personal_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# The body is read by hand (see app.services.uploads), so FastAPI can't
# describe the form itself; this tells Swagger what to send
UPLOAD_FORM = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary", "description": "A PDF file"},
                        "title": {"type": "string", "maxLength": 200, "description": "Defaults to the file name"},
                    },
                }
            }
        },
    }
}


# --- Errors -------------------------------------------------------------------

def file_too_large() -> ApiError:
    return ApiError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        "file_too_large",
        f"The file is larger than {settings.max_upload_mb} MB. Upload a smaller file.",
    )


def invalid_upload() -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_error",
        'Send one PDF as multipart/form-data in a field named "file", with an optional "title".',
    )


def workspace_not_found() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "workspace_not_found", "Your account has no workspace.")


def document_limit_reached() -> ApiError:
    limit = settings.max_documents_per_workspace
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        "document_limit_reached",
        f"This workspace already has {limit} documents. Delete one to upload another.",
    )


def not_pdf() -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, "not_pdf", "This file is not a PDF.")


def duplicate_document(existing: Document | None) -> ApiError:
    # Only the id and status of a document in the user's own workspace
    extra = {"existing_document": {"id": existing.id, "status": existing.status}} if existing else None
    return ApiError(
        status.HTTP_409_CONFLICT,
        "duplicate_document",
        "This file is already in your workspace.",
        extra=extra,
    )


def storage_error() -> ApiError:
    return ApiError(status.HTTP_502_BAD_GATEWAY, "storage_error", UNAVAILABLE_MESSAGE)


def document_not_found() -> ApiError:
    # The same answer for a missing document and someone else's: a 403 would
    # confirm that the document exists
    return ApiError(status.HTTP_404_NOT_FOUND, "document_not_found", "Document not found.")


def document_not_ready() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "document_not_ready",
        "This document isn't ready yet. Check its status and try again once it is ready.",
    )


def retry_not_allowed() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "retry_not_allowed",
        "Only documents that failed because processing was interrupted or the AI was unavailable "
        "can be retried. Upload the file again in a fixed form instead.",
    )


def page_not_found() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "page_not_found", "This document has no such page.")


# --- Reading the body -----------------------------------------------------------

async def pdf_upload(request: Request, _: User = Depends(get_current_user)) -> Upload:
    """Step 1: the file, read within the size limit.

    Depends on get_current_user, so a request without a valid login is
    refused before any of its body is read. Async, so a slow upload waits
    on the event loop instead of holding a worker thread.
    """
    try:
        return await read_upload(request, settings.max_upload_bytes)
    except UploadTooLarge:
        raise file_too_large() from None
    except UploadInvalid:
        raise invalid_upload() from None


def remove_file(storage: Storage, key: str, document_id: uuid.UUID) -> None:
    """Best effort: delete a file whose upload failed, so none is left
    behind in storage. A failure here is logged, never shown."""
    try:
        storage.delete(key)
    except StorageError:
        logger.error("Could not remove the file of failed upload %s", document_id)


# --- The route ----------------------------------------------------------------------

@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra=UPLOAD_FORM,
    summary="Upload a PDF",
)
def upload_document(
    background_tasks: BackgroundTasks,
    upload: Upload = Depends(pdf_upload),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
    ai: AIClient = Depends(get_ai),
    session_factory: sessionmaker = Depends(get_session_factory),
):
    """Upload one PDF as a multipart form: file, and an optional title (the
    file name without .pdf by default). Answers 202 at once with status
    processing; the text is extracted in the background, so check
    GET /documents/{id} until it is ready or failed.

    Limits: MAX_UPLOAD_MB per file (413 file_too_large) and
    MAX_DOCUMENTS_PER_WORKSPACE processing or ready documents (403
    document_limit_reached). The file must really be a PDF (400 not_pdf), and
    the same file can be in a workspace only once (409 duplicate_document,
    with the existing document's id and status).
    """
    # Step 2: lock the workspace row until this transaction ends, so a second
    # upload to the same workspace waits here and then sees this one's row
    workspace = get_personal_workspace(db, current_user.id, lock=True)
    if workspace is None:
        raise workspace_not_found()
    if docs.count_counted_documents(db, workspace.id) >= settings.max_documents_per_workspace:
        raise document_limit_reached()

    # Step 3: the file's own first bytes decide, not its name or claimed type
    if not docs.is_pdf(upload.data):
        raise not_pdf()

    # Step 4: the same file once per workspace, whatever the earlier one's status
    sha256 = docs.sha256_hex(upload.data)
    existing = docs.find_duplicate(db, workspace.id, sha256)
    if existing is not None:
        raise duplicate_document(existing)

    # Step 5: row (not committed yet), then the file, then the commit
    filename = docs.clean_filename(upload.filename)
    document_id = uuid.uuid4()
    key = document_key(workspace.id, document_id)
    document = Document(
        id=document_id,
        workspace_id=workspace.id,
        uploaded_by=current_user.id,
        title=docs.clean_title(upload.title, filename),
        original_filename=filename,
        storage_key=key,
        size_bytes=len(upload.data),
        sha256=sha256,
        status=STATUS_PROCESSING,
    )
    try:
        db.add(document)
        db.flush()  # the INSERT runs now; nothing is saved until the commit
    except IntegrityError as error:
        db.rollback()
        if docs.is_duplicate_violation(error):
            raise duplicate_document(docs.find_duplicate(db, workspace.id, sha256))
        raise

    try:
        storage.save(key, upload.data, "application/pdf")
    except StorageError:
        db.rollback()  # the row goes, and the workspace lock is released
        remove_file(storage, key, document_id)  # in case the save got through before failing
        raise storage_error() from None

    try:
        db.commit()
    except Exception as error:
        # The file is saved but the row isn't: remove the file, so storage
        # never holds a file no document points to
        db.rollback()
        remove_file(storage, key, document_id)
        if isinstance(error, IntegrityError) and docs.is_duplicate_violation(error):
            raise duplicate_document(docs.find_duplicate(db, workspace.id, sha256))
        raise

    db.refresh(document)  # created_at and updated_at come from the database

    # Step 6: runs after the response is sent, with its own sessions
    background_tasks.add_task(process_document, document.id, storage, ai, session_factory)
    return document


# --- The library ------------------------------------------------------------------------

def visible_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Document:
    """The document in the path, if the current user may reach it.

    The id is read as text, so a malformed id gets the same 404 as a
    missing document instead of a 422 that reveals what ids look like.
    """
    try:
        parsed = uuid.UUID(document_id)
    except ValueError:
        raise document_not_found() from None
    document = docs.find_visible_document(db, current_user.id, parsed)
    if document is None:
        raise document_not_found()
    return document


@router.get("", response_model=list[DocumentResponse], summary="List your documents")
def list_documents(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Every document in your workspace, newest first, in any status."""
    return docs.list_visible_documents(db, current_user.id)


@router.get("/{document_id}", response_model=DocumentResponse, summary="Get one document")
def get_document(document: Document = Depends(visible_document)):
    """One document with its status. Check this after uploading until the
    status is ready or failed; a failed document has an error_code and a
    message to show."""
    return document


@router.get("/{document_id}/pages/{page_number}", response_model=PageResponse, summary="Read one page")
def get_page(
    page_number: str,
    document: Document = Depends(visible_document),
    db: Session = Depends(get_db),
):
    """The extracted text of one page. Pages start at 1. Only ready
    documents have pages (409 document_not_ready otherwise)."""
    if document.status != STATUS_READY:
        raise document_not_ready()
    if not page_number.isdigit() or int(page_number) < 1:
        raise page_not_found()
    page = docs.find_page(db, document.id, int(page_number))
    if page is None:
        raise page_not_found()
    return PageResponse(
        page_number=page.page_number,
        page_count=document.page_count,
        text=page.text,
        char_count=page.char_count,
    )


@router.get("/{document_id}/sections", response_model=list[SectionResponse], summary="List the sections")
def list_sections(document: Document = Depends(visible_document), db: Session = Depends(get_db)):
    """The sections found in a ready document, in order: from the PDF's
    outline, else its numbered headings, else one section named after the
    document. Each covers start_page to end_page (409 document_not_ready
    before the document is ready)."""
    if document.status != STATUS_READY:
        raise document_not_ready()
    return db.scalars(
        select(DocumentSection).where(DocumentSection.document_id == document.id).order_by(DocumentSection.position)
    ).all()


@router.post(
    "/{document_id}/retry",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry processing",
)
def retry_document(
    background_tasks: BackgroundTasks,
    document: Document = Depends(visible_document),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
    ai: AIClient = Depends(get_ai),
    session_factory: sessionmaker = Depends(get_session_factory),
):
    """Restarts processing for a document that failed with interrupted or
    ai_unavailable, from the first unfinished stage: a document whose
    embeddings failed only redoes the embeddings. Any other status or code
    gets 409 retry_not_allowed, because the same file would fail the same
    way. Answers 202 with status processing; check GET /documents/{id}."""
    # The workspace lock first, as in upload: the retried document counts
    # toward the limit again, and two requests must not both pass the check
    db.execute(select(Workspace.id).where(Workspace.id == document.workspace_id).with_for_update())
    db.refresh(document, with_for_update=True)  # read its status again, locked
    if document.status != STATUS_FAILED or document.error_code not in RETRYABLE_CODES:
        raise retry_not_allowed()
    if docs.count_counted_documents(db, document.workspace_id) >= settings.max_documents_per_workspace:
        raise document_limit_reached()

    document.status = STATUS_PROCESSING
    document.error_code = None
    document.error_message = None
    document.processed_at = None
    db.commit()
    db.refresh(document)

    background_tasks.add_task(process_document, document.id, storage, ai, session_factory)
    return document


@router.get("/{document_id}/file", response_model=FileLinkResponse, summary="Get a link to the original PDF")
def get_file_link(
    document: Document = Depends(visible_document),
    storage: Storage = Depends(get_storage),
):
    """A signed link to the original file, valid for SIGNED_URL_SECONDS
    (5 minutes by default). Only for ready documents. The link is made
    only after the membership check, and the bucket itself stays private."""
    if document.status != STATUS_READY:
        raise document_not_ready()
    try:
        url = storage.signed_url(document.storage_key, settings.signed_url_seconds)
    except StorageError:
        raise storage_error() from None
    return FileLinkResponse(url=url, expires_in=settings.signed_url_seconds)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a document",
)
def delete_document(
    document: Document = Depends(visible_document),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
):
    """Deletes the original file, the document and its pages, in any status.

    The file goes first: if storage fails, the answer is 502 storage_error
    and the document stays, so you can try again. A file already missing
    from storage counts as deleted.
    """
    try:
        storage.delete(document.storage_key)
    except StorageError:
        db.rollback()
        raise storage_error() from None

    # The database deletes the pages with it (ON DELETE CASCADE). If a
    # background task is still processing this document, it finds the row
    # gone and drops its result.
    db.execute(delete(Document).where(Document.id == document.id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
