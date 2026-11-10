"""Find the chunks of one document most relevant to a question."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentSection, WorkspaceMember
from app.services.sections import KIND_BODY

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    content: str
    page_start: int
    page_end: int
    section_number: str | None
    section_title: str
    similarity: float


def similar_chunks_query(user_id: uuid.UUID, document_id: uuid.UUID, vector: Sequence[float], top_k: int):
    """The search, most similar first. See similar_chunks."""
    similarity = (1 - DocumentChunk.embedding.cosine_distance(vector)).label("similarity")
    return (
        select(
            DocumentChunk.id,
            DocumentChunk.content,
            DocumentChunk.page_start,
            DocumentChunk.page_end,
            DocumentSection.number,
            DocumentSection.title,
            similarity,
        )
        .join(DocumentSection, DocumentSection.id == DocumentChunk.section_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id)
        .where(
            WorkspaceMember.user_id == user_id,
            DocumentChunk.document_id == document_id,
            DocumentChunk.embedding.is_not(None),
            # A contents page matches many questions weakly: never cite front matter
            DocumentSection.kind == KIND_BODY,
        )
        # By the similarity expression, not the raw distance: see similar_chunks
        .order_by(similarity.desc(), DocumentChunk.position)
        .limit(top_k)
    )


def similar_chunks(
    db: Session,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    vector: Sequence[float],
    *,
    top_k: int,
    min_similarity: float,
) -> list[RetrievedChunk]:
    """At most top_k chunks of this one document, each at least
    min_similarity (cosine similarity, -1 to 1), most similar first.

    Two filters sit inside the query: the document itself, and the user's
    membership of its workspace. Chunks of any other document, the user's
    own or someone else's, can never come back, however similar.

    The search is exact. Ordering by the similarity expression (rather than
    by the raw distance) keeps PostgreSQL from using the HNSW index, which
    finds the nearest chunks of all documents and only then applies the
    document filter, so it could return fewer than top_k, or none. One
    document has at most a few hundred chunks, so the exact search is quick.
    """
    rows = db.execute(similar_chunks_query(user_id, document_id, vector, top_k)).all()
    chunks = [RetrievedChunk(*row[:6], similarity=float(row[6])) for row in rows]
    kept = [c for c in chunks if c.similarity >= min_similarity]
    # Pages and scores only, never the question or the text
    logger.info(
        "Retrieval for document %s: %d of %d above %.2f (best %s)",
        document_id, len(kept), len(chunks), min_similarity,
        f"{chunks[0].similarity:.3f}" if chunks else "none",
    )
    return kept
