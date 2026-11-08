"""Checks and helpers for documents. Every query here is limited to one
workspace inside the query itself."""

import hashlib
import ntpath
import posixpath
import unicodedata
import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Document, DocumentPage, WorkspaceMember
from app.models.document import COUNTED_STATUSES, WORKSPACE_SHA256_UNIQUE

PDF_SIGNATURE = b"%PDF-"
MAX_TITLE_LENGTH = 200
MAX_FILENAME_LENGTH = 255
FALLBACK_FILENAME = "document.pdf"
FALLBACK_TITLE = "Untitled document"


def is_pdf(data: bytes) -> bool:
    # Only the file's own first bytes are trusted, never its name or the
    # content type the client claims
    return data.startswith(PDF_SIGNATURE)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _visible(text: str) -> str:
    # Drops control characters (Cc, e.g. newlines and NUL) and invisible
    # format characters (Cf, e.g. the right-to-left override that can make
    # "evil\u202efdp.exe" display as "evilexe.pdf")
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))


def clean_filename(name: str | None) -> str:
    """The file name to keep for display: no folders, no control
    characters, at most 255 characters. It never goes into a storage key."""
    name = _visible(name or "")
    # Keep only the last part of "C:\\Users\\x\\a.pdf" or "../../a.pdf"
    name = posixpath.basename(ntpath.basename(name)).strip()
    if name in ("", ".", ".."):
        return FALLBACK_FILENAME
    return name[:MAX_FILENAME_LENGTH]


def clean_title(title: str | None, filename: str) -> str:
    """The title the user gave, or the file name without .pdf; at most
    200 characters either way."""
    title = _visible(title or "").strip()
    if not title:
        title = filename
        if title.lower().endswith(".pdf"):
            title = title[: -len(".pdf")]
        title = title.strip()
    return title[:MAX_TITLE_LENGTH].strip() or FALLBACK_TITLE


def count_counted_documents(db: Session, workspace_id: uuid.UUID) -> int:
    """Documents that count toward the limit: processing and ready, not failed."""
    return db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.workspace_id == workspace_id, Document.status.in_(COUNTED_STATUSES))
    )


def find_duplicate(db: Session, workspace_id: uuid.UUID, sha256: str) -> Document | None:
    """The document with the same file in this workspace, whatever its status."""
    return db.scalar(
        select(Document).where(Document.workspace_id == workspace_id, Document.sha256 == sha256)
    )


def is_duplicate_violation(error: IntegrityError) -> bool:
    # psycopg reports which constraint rejected the row
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == WORKSPACE_SHA256_UNIQUE


def documents_visible_to(user_id: uuid.UUID) -> Select[tuple[Document]]:
    """Every document in a workspace the user is a member of.

    The membership check is a join inside the query, so a document the user
    can't reach is never loaded at all; there is no separate ownership
    check that could be forgotten. Every library route starts from here.
    """
    return select(Document).join(
        WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id
    ).where(WorkspaceMember.user_id == user_id)


def find_visible_document(db: Session, user_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    return db.scalar(documents_visible_to(user_id).where(Document.id == document_id))


def list_visible_documents(db: Session, user_id: uuid.UUID) -> list[Document]:
    # Newest first; the id breaks ties between documents uploaded in the same instant
    return db.scalars(
        documents_visible_to(user_id).order_by(Document.created_at.desc(), Document.id.desc())
    ).all()


def find_page(db: Session, document_id: uuid.UUID, page_number: int) -> DocumentPage | None:
    return db.scalar(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id, DocumentPage.page_number == page_number
        )
    )
