"""Splitting a document's sections into chunks that remember their pages.

LangChain's RecursiveCharacterTextSplitter does the splitting: it cuts at
paragraphs first, then lines, then words, so chunks keep paragraphs
together. Each section is split on its own, so a chunk never mixes two
sections, and each chunk's place in the joined text gives its pages.

Running headers and footers (and page numbers) are left out before
splitting: they aren't the section's text, and a header at the top of the
next page would otherwise stretch a chunk's pages by one.
"""

import bisect
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.services.sections import DocumentText, Section, running_line_spans


@dataclass
class ChunkDraft:
    section_index: int  # index into the list of sections
    position: int  # order in the document, from 0
    page_start: int
    page_end: int
    content: str


class _Kept:
    """A stretch of the joined text with some spans left out, remembering
    where each kept piece came from."""

    def __init__(self, text: str, start: int, end: int, left_out: list[tuple[int, int]]):
        self.text = ""
        self.starts: list[int] = []  # where each kept piece starts in self.text
        self.origins: list[int] = []  # ...and in the joined text
        offset = start
        for out_start, out_end in left_out:
            if out_end <= start or out_start >= end:
                continue
            # A left out line goes with the line break after it
            if out_end < len(text) and text[out_end] == "\n":
                out_end += 1
            self._keep(text, offset, max(offset, out_start))
            offset = max(offset, min(out_end, end))
        self._keep(text, offset, end)

    def _keep(self, text: str, start: int, end: int) -> None:
        if end > start:
            self.starts.append(len(self.text))
            self.origins.append(start)
            self.text += text[start:end]

    def origin(self, offset: int) -> int:
        """The joined text offset of this offset in the kept text."""
        piece = max(0, bisect.bisect_right(self.starts, offset) - 1)
        return self.origins[piece] + offset - self.starts[piece] if self.starts else offset


def build_chunks(doc: DocumentText, sections: list[Section], chunk_size: int, chunk_overlap: int) -> list[ChunkDraft]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
    )
    left_out = running_line_spans(doc)
    chunks: list[ChunkDraft] = []
    for section_index, section in enumerate(sections):
        kept = _Kept(doc.text, section.start_offset, section.end_offset, left_out)
        for piece in splitter.create_documents([kept.text]):
            content = piece.page_content
            if not content.strip():
                continue
            relative = piece.metadata.get("start_index", -1)
            if relative < 0:  # the splitter couldn't place it; look it up
                relative = max(0, kept.text.find(content))
            start = kept.origin(relative)
            end = kept.origin(relative + len(content) - 1)
            chunks.append(ChunkDraft(
                section_index=section_index,
                position=len(chunks),
                page_start=doc.page_at(start),
                page_end=doc.page_at(end),
                content=content,
            ))
    return chunks


def section_label(document_title: str, number: str | None, section_title: str) -> str:
    """"Staff Handbook > 4.2 Annual Leave"; just the title for the section
    named after the document."""
    heading = f"{number} {section_title}" if number and number != section_title else section_title
    if heading == document_title:
        return document_title
    return f"{document_title} > {heading}"


def embedding_input(label: str, content: str) -> str:
    """The text sent for embedding: the label helps retrieval find the right
    section. The stored chunk content stays the original text."""
    return f"{label}\n\n{content}"
