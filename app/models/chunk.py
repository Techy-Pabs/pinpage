import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The embedding model's output size, as in ShopAssist (nemotron-3-embed-1b
# returns 2,048 numbers). Fixed by the migration: a model with another size
# needs a new migration, so the setting is checked against this at startup.
EMBEDDING_COLUMN_DIMENSIONS = 2048


class DocumentSection(Base):
    """A heading in a document and the pages it covers. Deleted with its
    document (ON DELETE CASCADE)."""

    __tablename__ = "document_sections"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_not_negative"),
        CheckConstraint("level > 0", name="level_positive"),
        CheckConstraint("start_page > 0 AND end_page >= start_page", name="pages_valid"),
        UniqueConstraint("document_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)  # order in the document, from 0
    # "4.2", "Section 4", "Part II"; empty for a heading without one
    number: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(500))
    level: Mapped[int] = mapped_column(Integer)  # 1 is the top level
    start_page: Mapped[int] = mapped_column(Integer)
    # Up to where the next section at the same or a higher level starts
    end_page: Mapped[int] = mapped_column(Integer)


class DocumentChunk(Base):
    """A piece of a section's text, with the pages it came from and, once
    embedded, its embedding for similarity search."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_not_negative"),
        CheckConstraint("page_start > 0 AND page_end >= page_start", name="pages_valid"),
        UniqueConstraint("document_id", "position"),
        # HNSW index for cosine similarity. halfvec (half precision), as in
        # ShopAssist: pgvector can only index plain vectors up to 2,000
        # dimensions, and the embedding model returns 2,048.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_sections.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)  # order in the document, from 0
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)  # a chunk can cross a page break
    content: Mapped[str] = mapped_column(Text)  # the original text, without the label
    # Empty until the embedding stage has run for this chunk
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_COLUMN_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
