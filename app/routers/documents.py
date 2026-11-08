import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies import get_current_user, get_db, get_session_factory, get_storage
from app.models import Document, User
from app.models.document import STATUS_PROCESSING
from app.schemas.document import DocumentResponse
from app.services import documents as docs
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
    session_factory: sessionmaker = Depends(get_session_factory),
):
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
    background_tasks.add_task(process_document, document.id, storage, session_factory)
    return document
