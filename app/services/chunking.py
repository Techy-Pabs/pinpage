"""Splitting a document's sections into chunks that remember their pages.

LangChain's RecursiveCharacterTextSplitter does the splitting: it cuts at
paragraphs first, then lines, then words, so chunks keep paragraphs
together. Each section is split on its own, so a chunk never mixes two
sections, and each chunk's place in the joined text gives its pages.
"""

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.services.sections import DocumentText, Section


@dataclass
class ChunkDraft:
    section_index: int  # index into the list of sections
    position: int  # order in the document, from 0
    page_start: int
    page_end: int
    content: str


def build_chunks(doc: DocumentText, sections: list[Section], chunk_size: int, chunk_overlap: int) -> list[ChunkDraft]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
    )
    chunks: list[ChunkDraft] = []
    for section_index, section in enumerate(sections):
        span = doc.text[section.start_offset : section.end_offset]
        for piece in splitter.create_documents([span]):
            content = piece.page_content
            if not content.strip():
                continue
            relative = piece.metadata.get("start_index", -1)
            if relative < 0:  # the splitter couldn't place it; look it up
                relative = max(0, span.find(content))
            start = section.start_offset + relative
            end = start + len(content) - 1
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
