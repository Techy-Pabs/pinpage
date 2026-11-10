"""Rebuild a document's sections from its stored pages, without re-embedding.

Usage:
    python -m app.cli.documents rebuild-sections <document_id>
    python -m app.cli.documents rebuild-sections <document_id> --yes   # skip the confirmation

For documents processed before a change to section detection. The pages
come from the database; the PDF is read from storage only for its outline
(bookmarks), which isn't stored. Chunk text and embeddings never change:
chunks are only moved to the new sections. If the new sections would cut
the text into different chunks, the command refuses, and the document must
be uploaded again.

It prints counts only, never document text.
"""

import argparse
import io
import sys
import uuid
from dataclasses import dataclass

from pypdf import PasswordType, PdfReader
from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

from app.cli.users import database_label
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection
from app.services.chunking import build_chunks
from app.services.processing import read_outline
from app.services.sections import KIND_FRONT_MATTER, DocumentText, OutlineEntry, detect_sections
from app.services.storage import Storage, StorageError, default_storage

# Old sections are moved out of the way of the new positions first, since
# (document_id, position) is unique
POSITION_SHIFT = 1_000_000


class RebuildRefused(Exception):
    """The rebuild can't be done; the message says why."""


@dataclass
class RebuildResult:
    old_sections: int
    new_sections: int
    front_matter: int
    chunks_moved: int


def outline_of(data: bytes) -> list[OutlineEntry]:
    """The PDF's bookmarks. A PDF that won't open, or opens only with a
    password, gives none (sections then come from the text)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            return []
        return read_outline(reader)
    except Exception:
        return []


def rebuild_sections(session_factory: sessionmaker, storage: Storage, document_id: uuid.UUID) -> RebuildResult:
    with session_factory() as db:
        document = db.get(Document, document_id)
        if document is None:
            raise RebuildRefused("No document with that id.")
        texts = db.scalars(
            select(DocumentPage.text).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
        ).all()
        stored = db.execute(
            select(DocumentChunk.id, DocumentChunk.content, DocumentChunk.page_start, DocumentChunk.page_end)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.position)
        ).all()
        old_sections = len(db.scalars(select(DocumentSection.id).where(DocumentSection.document_id == document_id)).all())
        key, title = document.storage_key, document.title
    if not texts or not stored:
        raise RebuildRefused("This document has no stored pages or chunks yet. Wait until it is ready, or retry it.")

    try:
        outline = outline_of(storage.read(key))
    except StorageError as error:
        raise RebuildRefused(f"Couldn't read the PDF for its bookmarks: {error}") from None

    doc = DocumentText(list(texts))
    sections = detect_sections(doc, outline, title)
    drafts = build_chunks(doc, sections, settings.chunk_size, settings.chunk_overlap)
    if [(d.content, d.page_start, d.page_end) for d in drafts] != [tuple(row[1:]) for row in stored]:
        raise RebuildRefused(
            "Rebuilding would change this document's chunks, so it can't be done without "
            "re-embedding. Upload the document again instead."
        )

    with session_factory() as db, db.begin():
        db.execute(select(Document.id).where(Document.id == document_id).with_for_update())
        db.execute(
            update(DocumentSection)
            .where(DocumentSection.document_id == document_id)
            .values(position=DocumentSection.position + POSITION_SHIFT)
        )
        old_ids = db.scalars(select(DocumentSection.id).where(DocumentSection.document_id == document_id)).all()
        rows = [
            DocumentSection(
                id=uuid.uuid4(), document_id=document_id, position=position, number=section.number,
                title=section.title, level=section.level, start_page=section.start_page,
                end_page=section.end_page, kind=section.kind,
            )
            for position, section in enumerate(sections)
        ]
        db.add_all(rows)
        db.flush()
        # Same chunks, same text, same embeddings: only their section changes
        db.execute(
            update(DocumentChunk),
            [{"id": row[0], "section_id": rows[draft.section_index].id} for row, draft in zip(stored, drafts)],
        )
        if old_ids:
            db.execute(delete(DocumentSection).where(DocumentSection.id.in_(old_ids)))

    return RebuildResult(
        old_sections=old_sections,
        new_sections=len(sections),
        front_matter=sum(1 for s in sections if s.kind == KIND_FRONT_MATTER),
        chunks_moved=len(stored),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild a Pinpage document's sections without re-embedding.")
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser("rebuild-sections", help="Rebuild sections from the stored pages")
    rebuild.add_argument("document_id")
    rebuild.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    args = parser.parse_args(argv)

    try:
        document_id = uuid.UUID(args.document_id)
    except ValueError:
        print("That is not a document id (a UUID). Nothing was changed.")
        return 1

    print(f"Database: {database_label()}")
    if not args.yes:
        answer = input(f"Rebuild the sections of document {document_id}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1

    try:
        result = rebuild_sections(SessionLocal, default_storage(), document_id)
    except RebuildRefused as refusal:
        print(f"{refusal} Nothing was changed.")
        return 1
    print(
        f"Rebuilt the sections of document {document_id}: {result.new_sections} sections "
        f"(was {result.old_sections}), {result.front_matter} front matter, "
        f"{result.chunks_moved} chunks moved. No text or embedding changed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
