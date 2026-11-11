"""Answering in each mode: whole pages, a section summary, or an ordinary
question (retrieval). Each returns an Answer; the route saves it.

Every database read here goes through the membership query, and the read
transaction is ended before any AI request.
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document, DocumentChunk, DocumentPage, DocumentSection, WorkspaceMember
from app.models.conversation import OUTCOME_ANSWERED, OUTCOME_NOT_FOUND
from app.services.ai import AIClient, AIServiceError, ChatMessage
from app.services.citations import cite, trim_at_last_marker, trim_to_cited_lines
from app.services.prompts import (
    NOT_FOUND_MESSAGE,
    build_combine_prompt,
    build_grounding_prompt,
    build_page_prompt,
    build_summary_prompt,
)
from app.services.requests import MODE_PAGE, MODE_QUESTION, MODE_SECTION, ParsedRequest, SectionInfo
from app.services.processing import MIN_CHARS_PER_PAGE
from app.services.retrieval import RetrievedChunk, similar_chunks
from app.services.sections import KIND_BODY

logger = logging.getLogger(__name__)


@dataclass
class Answer:
    content: str
    outcome: str
    mode: str
    citations: list[dict] = field(default_factory=list)
    retrieval_query: str | None = None


def cited_or_not_found(reply: str, excerpts: list[RetrievedChunk], mode: str) -> Answer:
    """Only valid markers become citations; none means the not found answer."""
    cited = cite(reply, excerpts)
    if cited.citations:
        return Answer(cited.text, OUTCOME_ANSWERED, mode, cited.citations)
    return Answer(NOT_FOUND_MESSAGE, OUTCOME_NOT_FOUND, mode)


def complete_reply(
    ai: AIClient, system_prompt: str, messages: list[ChatMessage], max_tokens: int, excerpt_count: int, kind: str,
    *, part: bool = False,
) -> str:
    """The reply's text, never cut off mid sentence. An answer cut off at
    max_tokens is trimmed to its last valid marker; one with nothing left is
    an AIServiceError (503, nothing saved or counted), since "not found"
    would be a false statement about the document. A part summary is
    trimmed to its last cited line instead, and may come back empty."""
    reply = ai.generate(system_prompt, messages, max_tokens)
    if not reply.truncated:
        return reply.text
    trimmed = (trim_to_cited_lines if part else trim_at_last_marker)(reply.text, excerpt_count)
    # Lengths only, never the text
    logger.warning(
        "%s reply cut off at max_tokens=%d: kept %d of %d characters",
        kind, max_tokens, len(trimmed), len(reply.text),
    )
    if not trimmed and not part:
        raise AIServiceError("reply cut off with nothing cited", retryable=False)
    return trimmed


def visible_document_query(user_id: uuid.UUID, document_id: uuid.UUID):
    """The membership condition every query in this module joins on."""
    return (
        select(Document.id)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id)
        .where(Document.id == document_id, WorkspaceMember.user_id == user_id)
    )


def section_infos(db: Session, user_id: uuid.UUID, document_id: uuid.UUID) -> list[SectionInfo]:
    """The document's sections, in order, for the request parser."""
    rows = db.execute(
        select(
            DocumentSection.position, DocumentSection.number, DocumentSection.title,
            DocumentSection.level, DocumentSection.kind,
        )
        .where(DocumentSection.document_id.in_(visible_document_query(user_id, document_id)))
        .order_by(DocumentSection.position)
    ).all()
    return [SectionInfo(*row) for row in rows]


# --- Pages ---------------------------------------------------------------------------------

def _join_pages(pages: list[int]) -> str:
    """"pages 10 to 12" for a run, "pages 3, 7 and 9" otherwise, "page 4" for one."""
    if len(pages) == 1:
        return f"page {pages[0]}"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"pages {pages[0]} to {pages[-1]}"
    return "pages " + ", ".join(map(str, pages[:-1])) + f" and {pages[-1]}"


def _page_sections(
    db: Session, user_id: uuid.UUID, document_id: uuid.UUID, pages: list[int]
) -> dict[int, tuple[str | None, str]]:
    """For each page, the section that covers most of it: the one whose
    chunks on that page hold the most text. Front matter counts here."""
    rows = db.execute(
        select(
            DocumentChunk.page_start, DocumentChunk.page_end, func.length(DocumentChunk.content),
            DocumentSection.position, DocumentSection.number, DocumentSection.title,
        )
        .join(DocumentSection, DocumentSection.id == DocumentChunk.section_id)
        .where(
            DocumentChunk.document_id.in_(visible_document_query(user_id, document_id)),
            DocumentChunk.page_start <= max(pages),
            DocumentChunk.page_end >= min(pages),
        )
    ).all()
    best: dict[int, tuple[str | None, str]] = {}
    for page in pages:
        weights: dict[tuple[int, str | None, str], int] = {}
        for page_start, page_end, length, position, number, title in rows:
            if page_start <= page <= page_end:
                key = (position, number, title)
                weights[key] = weights.get(key, 0) + length
        if weights:
            (_, number, title), _ = max(weights.items(), key=lambda item: (item[1], -item[0][0]))
            best[page] = (number, title)
    return best


def answer_pages(
    db: Session, ai: AIClient, user_id: uuid.UUID, document: Document, request: ParsedRequest,
    history: list[ChatMessage], question: str,
) -> Answer:
    page_count = document.page_count
    if request.out_of_range:
        return Answer(
            f"This document has {page_count} pages. Ask about a page between 1 and {page_count}.",
            OUTCOME_NOT_FOUND, MODE_PAGE,
        )

    limit = settings.max_pages_per_question
    pages = request.pages[:limit]
    texts = dict(db.execute(
        select(DocumentPage.page_number, DocumentPage.text)
        .where(
            DocumentPage.document_id.in_(visible_document_query(user_id, document.id)),
            DocumentPage.page_number.in_(pages),
        )
    ).all())
    sections = _page_sections(db, user_id, document.id, pages)
    db.commit()  # no transaction stays open while the AI works

    # Fewer than 20 characters is "no readable text", as for scans
    unreadable = [p for p in pages if len(texts.get(p, "").strip()) < MIN_CHARS_PER_PAGE]
    readable = [p for p in pages if p not in unreadable]
    notes = []
    if unreadable and readable:
        notes.append(f"{_join_pages(unreadable).capitalize()} {'has' if len(unreadable) == 1 else 'have'} no readable text.")
    if len(request.pages) > limit:
        notes.append(f"I looked at {_join_pages(pages)} only. Ask about later pages separately.")

    if not readable:
        # No AI request for pages with nothing to read
        verb = "has" if len(unreadable) == 1 else "have"
        answer = Answer(f"{_join_pages(unreadable).capitalize()} {verb} no readable text.", OUTCOME_NOT_FOUND, MODE_PAGE)
    else:
        excerpts = [
            RetrievedChunk(
                chunk_id=uuid.uuid4(), content=texts[p], page_start=p, page_end=p,
                section_number=sections.get(p, (None, document.title))[0],
                section_title=sections.get(p, (None, document.title))[1],
                similarity=1.0,
            )
            for p in readable
        ]
        reply = complete_reply(
            ai, build_page_prompt(document.title, excerpts), [*history, {"role": "user", "content": question}],
            settings.ai_max_tokens_page, len(excerpts), "Page",
        )
        answer = cited_or_not_found(reply, excerpts, MODE_PAGE)

    if notes:
        answer.content = answer.content + "\n\n" + " ".join(notes)
    return answer


# --- Sections --------------------------------------------------------------------------------

def _section_name(section: SectionInfo) -> str:
    """How messages name a section: "section 4", "Part II", or its title."""
    if section.number:
        return section.number if not section.number[0].isdigit() else f"section {section.number}"
    return f'"{section.title}"'


def _without_overlap(previous: str, current: str, longest: int) -> str:
    """current without the text it repeats from the end of previous (the
    splitter's overlap), so the model doesn't read it twice."""
    for size in range(min(len(previous), len(current), longest), 9, -1):
        if previous.endswith(current[:size]):
            return current[size:].lstrip()
    return current


def section_excerpts(
    db: Session, user_id: uuid.UUID, document_id: uuid.UUID, section: SectionInfo, all_sections: list[SectionInfo]
) -> tuple[list[RetrievedChunk], list[SectionInfo]]:
    """The section's chunks with its subsections', in document order, front
    matter excluded and overlaps removed; and its direct subsections."""
    family = [section]
    for later in all_sections:
        if later.position <= section.position:
            continue
        if later.level <= section.level:
            break
        family.append(later)
    positions = [s.position for s in family if s.kind == KIND_BODY]
    children = [s for s in family[1:] if s.level == section.level + 1]

    rows = db.execute(
        select(
            DocumentChunk.id, DocumentChunk.content, DocumentChunk.page_start, DocumentChunk.page_end,
            DocumentSection.number, DocumentSection.title, DocumentSection.id,
        )
        .join(DocumentSection, DocumentSection.id == DocumentChunk.section_id)
        .where(
            DocumentChunk.document_id.in_(visible_document_query(user_id, document_id)),
            DocumentSection.position.in_(positions),
            DocumentSection.kind == KIND_BODY,
        )
        .order_by(DocumentChunk.position)
    ).all()

    excerpts: list[RetrievedChunk] = []
    previous_text, previous_section = "", None
    for chunk_id, content, page_start, page_end, number, title, section_id in rows:
        # The splitter only overlaps chunks within one section
        text = _without_overlap(previous_text, content, 2 * settings.chunk_overlap) if section_id == previous_section else content
        if text.strip():
            excerpts.append(RetrievedChunk(chunk_id, text, page_start, page_end, number, title, 1.0))
        previous_text, previous_section = content, section_id
    return excerpts, children


def _parts(excerpts: list[RetrievedChunk], part_chars: int) -> list[list[int]]:
    """Excerpt indexes grouped into parts of at most part_chars characters."""
    parts: list[list[int]] = [[]]
    size = 0
    for index, excerpt in enumerate(excerpts):
        if parts[-1] and size + len(excerpt.content) > part_chars:
            parts.append([])
            size = 0
        parts[-1].append(index)
        size += len(excerpt.content)
    return parts


def answer_section(
    db: Session, ai: AIClient, user_id: uuid.UUID, document: Document, request: ParsedRequest,
    all_sections: list[SectionInfo], history: list[ChatMessage], question: str,
) -> Answer:
    if request.section is None:
        return Answer(
            f"I couldn't find {request.unknown_section} in this document. The sections are listed under Sections.",
            OUTCOME_NOT_FOUND, MODE_SECTION,
        )
    section = request.section
    excerpts, children = section_excerpts(db, user_id, document.id, section, all_sections)
    db.commit()  # no transaction stays open while the AI works
    if not excerpts:
        return Answer(NOT_FOUND_MESSAGE, OUTCOME_NOT_FOUND, MODE_SECTION)

    parts = _parts(excerpts, settings.summary_part_chars)
    if len(parts) > settings.summary_max_parts:
        # No AI request: point to smaller pieces instead
        name = _section_name(section)
        if children:
            pieces = ", ".join(c.number or c.title for c in children)
            hint = f"Ask about one of its subsections: {pieces}."
        else:
            first, last = excerpts[0].page_start, excerpts[-1].page_end
            hint = f"Ask about its pages instead, up to {settings.max_pages_per_question} at a time (it covers pages {first} to {last})."
        return Answer(
            f"{name[0].upper()}{name[1:]} is too long to summarise in one go. {hint}", OUTCOME_NOT_FOUND, MODE_SECTION
        )

    request_messages = [*history, {"role": "user", "content": question}]
    limit = settings.ai_max_tokens_summary
    if len(parts) == 1:
        reply = complete_reply(
            ai, build_summary_prompt(document.title, list(enumerate(excerpts))), request_messages,
            limit, len(excerpts), "Summary",
        )
    else:
        # One request per part, each keeping the markers of the whole section,
        # then one request that combines the part summaries
        summaries = [
            complete_reply(
                ai,
                build_summary_prompt(
                    document.title, [(i, excerpts[i]) for i in part], part=number, parts=len(parts)
                ),
                [{"role": "user", "content": question}],
                limit, len(excerpts), "Summary part", part=True,
            )
            for number, part in enumerate(parts, start=1)
        ]
        # A part cut off with nothing left adds nothing to combine
        summaries = [summary for summary in summaries if summary]
        if not summaries:
            raise AIServiceError("every summary part was cut off with nothing cited", retryable=False)
        reply = complete_reply(
            ai, build_combine_prompt(document.title, summaries), request_messages, limit, len(excerpts), "Summary",
        )
    logger.info("Section summary for document %s: %d excerpts in %d part(s)", document.id, len(excerpts), len(parts))
    return cited_or_not_found(reply, excerpts, MODE_SECTION)


# --- Ordinary questions -----------------------------------------------------------------------

def answer_question(
    db: Session, ai: AIClient, user_id: uuid.UUID, document: Document, history: list[ChatMessage],
    question: str, retrieval_text: str,
) -> Answer:
    """Retrieval with retrieval_text (the question, or its rewrite); the answer
    prompt always gets the user's own question."""
    [vector] = ai.embed_batch([retrieval_text], "query")
    chunks = similar_chunks(
        db, user_id, document.id, vector,
        top_k=settings.retrieval_top_k, min_similarity=settings.retrieval_min_similarity,
    )
    db.commit()
    if not chunks:
        # Nothing close enough: no chat request at all
        return Answer(NOT_FOUND_MESSAGE, OUTCOME_NOT_FOUND, MODE_QUESTION)
    reply = complete_reply(
        ai, build_grounding_prompt(document.title, chunks), [*history, {"role": "user", "content": question}],
        settings.ai_max_tokens_question, len(chunks), "Question",
    )
    return cited_or_not_found(reply, chunks, MODE_QUESTION)
