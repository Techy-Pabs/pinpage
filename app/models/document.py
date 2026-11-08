import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Why a document failed: a short fixed code, and the sentence shown to the
# user. Never exception text.
FAILURE_MESSAGES = {
    "too_many_pages": "This document has more than {max_pages} pages. Upload a shorter file.",
    "encrypted": "This PDF is password protected. Remove the password and upload it again.",
    "no_text_layer": "This looks like a scanned document. Pinpage can't read scans yet.",
    "unreadable": "We couldn't read this PDF. It may be damaged.",
    "interrupted": "Processing was interrupted. Delete this document and upload it again.",
}

# Documents that count toward the per-workspace limit (failed ones don't)
COUNTED_STATUSES = (STATUS_PROCESSING, STATUS_READY)

# Named by the convention in app.db.base; the upload route looks for it to
# recognise a duplicate that slipped past the early check
WORKSPACE_SHA256_UNIQUE = "uq_documents_workspace_id_sha256"


class Document(Base):
    """One uploaded PDF. It belongs to a workspace, never directly to a user."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'ready', 'failed')", name="status_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        # The same file only once per workspace, whatever its status
        UniqueConstraint("workspace_id", "sha256"),
        # The library lists a workspace's documents newest first
        Index("ix_documents_workspace_id_created_at", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # RESTRICT: a workspace can't be deleted while it still has documents,
    # because their files in R2 must be deleted first
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT")
    )
    # SET NULL: the document stays in its workspace if the uploader's
    # account goes (it matters once workspaces are shared, in Milestone 7)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    original_filename: Mapped[str] = mapped_column(String(255))
    # Built only from UUIDs (see app.services.storage.document_key); unique,
    # so deleting one document can never delete another document's file
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)  # empty until processing finishes
    status: Mapped[str] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentPage(Base):
    """The extracted text of one page. Deleted with its document by the
    database itself (ON DELETE CASCADE)."""

    __tablename__ = "document_pages"
    __table_args__ = (
        CheckConstraint("page_number > 0", name="page_number_positive"),
        CheckConstraint("char_count >= 0", name="char_count_not_negative"),
        # One row per page; it also serves "the pages of this document"
        UniqueConstraint("document_id", "page_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_number: Mapped[int] = mapped_column(Integer)  # starts at 1
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
