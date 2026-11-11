"""Page questions and section summaries. FakeAI and FakeStorage only."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import DocumentChunk, DocumentPage, DocumentSection, Message, QuestionUsage
from app.models.document import STATUS_READY
from app.services import modes
from app.services.ai import ChatReply
from app.services.citations import trim_truncated
from app.services.prompts import NOT_FOUND_MESSAGE
from app.services.requests import SectionInfo

FILL = "Staff should read this carefully and ask their manager about anything unclear."

# A small handbook: front matter on page 1, section 4 with two subsections,
# then section 5. (number, title, level, start, end, kind)
SECTIONS = [
    (None, "Front matter", 1, 1, 1, "front_matter"),
    ("4", "Leave", 1, 2, 4, "body"),
    ("4.1", "Annual Leave", 2, 2, 3, "body"),
    ("4.2", "Sick Leave", 2, 4, 4, "body"),
    ("5", "Recruitment and Induction", 1, 5, 5, "body"),
]
PAGES = [
    "Contents: 4 Leave ... 2, 5 Recruitment ... 5",
    f"4 Leave. All leave rules. 4.1 Annual Leave. Staff get 25 days. {FILL}",
    f"Carry over five days into the next year. {FILL}",
    f"4.2 Sick Leave. A note is needed after three days. {FILL}",
    f"5 Recruitment and Induction. New staff have a 6 month probation. {FILL}",
]
# (section index, content, page_start, page_end)
CHUNKS = [
    (0, "Contents: 4 Leave ... 2, 5 Recruitment ... 5", 1, 1),
    (1, "4 Leave. All leave rules.", 2, 2),
    (2, "4.1 Annual Leave. Staff get 25 days.", 2, 2),
    (2, "Carry over five days into the next year.", 3, 3),
    (3, "4.2 Sick Leave. A note is needed after three days.", 4, 4),
    (4, "5 Recruitment and Induction. New staff have a 6 month probation.", 5, 5),
]


@pytest.fixture(autouse=True)
def mode_settings(monkeypatch):
    monkeypatch.setattr(settings, "max_pages_per_question", 3)
    monkeypatch.setattr(settings, "summary_part_chars", 12000)
    monkeypatch.setattr(settings, "summary_max_parts", 4)
    monkeypatch.setattr(settings, "daily_questions_per_user", 20)
    monkeypatch.setattr(settings, "chunk_overlap", 150)


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


@pytest.fixture
def structured_document(make_structured_document):
    """This file's handbook unless told otherwise."""

    def _make(pages=PAGES, sections=SECTIONS, chunks=CHUNKS, email="alice@example.com"):
        return make_structured_document(pages, sections, chunks, email)

    return _make


@pytest.fixture
def ask_about(client, alice, structured_document):
    """ask_about(question, **document) -> the answer, in a new conversation."""

    def _ask(question, **document):
        doc = structured_document(**document)
        conversation = client.post(f"/documents/{doc.id}/conversations", headers=alice).json()["id"]
        response = client.post(f"/conversations/{conversation}/messages", headers=alice, json={"content": question})
        assert response.status_code == 201, response.text
        return response.json()

    return _ask


def excerpt_order(prompt: str) -> list[str]:
    """The (marker, pages) of each excerpt in a prompt, in order."""
    import re

    return re.findall(r'<excerpt marker="(S\d+)" pages="([\d-]+)"', prompt)


def usage(db) -> int:
    db.expire_all()
    return db.scalar(select(func.sum(QuestionUsage.count))) or 0


# --- Page answers ---------------------------------------------------------------------------------

def test_a_page_question_sends_that_page_and_cites_it(ask_about, fake_ai):
    fake_ai.reply = "Staff can carry over five days [S1]."

    answer = ask_about("What does page 3 say?")

    [call] = fake_ai.reply_calls
    assert excerpt_order(call["system_prompt"]) == [("S1", "3")]
    assert PAGES[2] in call["system_prompt"]
    assert "give a short summary of the page in up to 5 lines" in call["system_prompt"]
    assert fake_ai.embed_calls == []  # no retrieval, so no embedding request
    assert answer["mode"] == "page" and answer["outcome"] == "answered"
    assert answer["citations"] == [{
        "marker": "S1", "page_start": 3, "page_end": 3, "section_number": "4.1",
        "section_title": "Annual Leave", "snippet": PAGES[2],
    }]


def test_pages_are_sent_in_order_one_excerpt_each(ask_about, fake_ai):
    fake_ai.reply = "Leave rules [S1]. Sick notes [S3]."

    answer = ask_about("Summarise pages 4 to 2")

    prompt = fake_ai.reply_calls[0]["system_prompt"]
    assert excerpt_order(prompt) == [("S1", "2"), ("S2", "3"), ("S3", "4")]
    assert [(c["marker"], c["page_start"], c["section_title"]) for c in answer["citations"]] == [
        ("S1", 2, "Annual Leave"), ("S3", 4, "Sick Leave"),
    ]


def test_more_than_three_pages_uses_the_first_three_and_says_so(ask_about, fake_ai):
    fake_ai.reply = "Several rules [S1]."

    answer = ask_about("What do pages 1 to 5 say?")

    assert [p for _, p in excerpt_order(fake_ai.reply_calls[0]["system_prompt"])] == ["1", "2", "3"]
    assert answer["content"].endswith("I looked at pages 1 to 3 only. Ask about later pages separately.")


def test_front_matter_pages_can_be_read(ask_about, fake_ai):
    fake_ai.reply = "It lists the sections [S1]."

    answer = ask_about("What does page 1 say?")

    assert answer["citations"][0]["section_title"] == "Front matter"


def test_out_of_range_page_costs_no_ai_request(db, ask_about, fake_ai):
    answer = ask_about("What does page 40 say?")

    assert answer["content"] == "This document has 5 pages. Ask about a page between 1 and 5."
    assert (answer["mode"], answer["outcome"], answer["citations"]) == ("page", "not_found", [])
    assert fake_ai.reply_calls == [] and fake_ai.embed_calls == []
    assert usage(db) == 1  # still a question


def test_an_empty_page_costs_no_ai_request(ask_about, fake_ai):
    pages = [*PAGES[:3], "   ", PAGES[4]]

    answer = ask_about("What does page 4 say?", pages=pages)

    assert answer["content"] == "Page 4 has no readable text."
    assert answer["outcome"] == "not_found" and answer["mode"] == "page"
    assert fake_ai.reply_calls == []


def test_an_empty_page_among_readable_ones_is_noted(ask_about, fake_ai):
    pages = [*PAGES[:3], "", PAGES[4]]
    fake_ai.reply = "Carry over five days [S1]."

    answer = ask_about("pages 3 and 4", pages=pages)

    assert excerpt_order(fake_ai.reply_calls[0]["system_prompt"]) == [("S1", "3")]
    assert answer["content"].endswith("Page 4 has no readable text.")


def test_a_page_answer_with_no_valid_marker_is_not_found(ask_about, fake_ai):
    fake_ai.reply = "It is about leave [S7]."

    answer = ask_about("What does page 2 say?")

    assert answer["content"] == NOT_FOUND_MESSAGE and answer["citations"] == []


def test_another_users_document_still_gives_404(client, alice, auth_headers, structured_document):
    document = structured_document()
    bob = auth_headers("bob@example.com")

    response = client.post(f"/documents/{document.id}/conversations", headers=bob)

    assert response.json()["error"]["code"] == "document_not_found"


def test_pages_are_read_through_the_membership_query(db, structured_document, alice, register):
    document = structured_document()
    register("bob@example.com")
    from app.models import User

    bob_id = db.scalars(select(User.id).where(User.email == "bob@example.com")).one()
    request = modes.ParsedRequest(mode="page", pages=[2])

    answer = modes.answer_pages(db, None, bob_id, document, request, [], "page 2")

    # Bob's id gets no page text back, so nothing could be sent
    assert answer.content == "Page 2 has no readable text."


# --- Section summaries --------------------------------------------------------------------------

def test_a_section_summary_includes_its_subsections_in_order(ask_about, fake_ai):
    fake_ai.reply = "Section 4 covers leave [S1].\nStaff get 25 days [S2]."

    answer = ask_about("Summarise section 4")

    [call] = fake_ai.reply_calls  # a short section: one request
    prompt = call["system_prompt"]
    assert excerpt_order(prompt) == [("S1", "2"), ("S2", "2"), ("S3", "3"), ("S4", "4")]
    assert 'section="4.1 Annual Leave"' in prompt and 'section="4.2 Sick Leave"' in prompt
    assert "Contents" not in prompt and "Recruitment" not in prompt
    assert "up to 8 short lines" in prompt
    assert answer["mode"] == "section" and answer["outcome"] == "answered"
    assert [c["section_title"] for c in answer["citations"]] == ["Leave", "Annual Leave"]


def test_a_summary_by_title(ask_about, fake_ai):
    fake_ai.reply = "Probation is 6 months [S1]."

    answer = ask_about("Give me an overview of recruitment and induction")

    assert excerpt_order(fake_ai.reply_calls[0]["system_prompt"]) == [("S1", "5")]
    assert answer["mode"] == "section"


def test_front_matter_is_never_summarised(db, alice, structured_document):
    # Even if front matter sat inside a section's range, it is left out
    sections = [("4", "Leave", 1, 2, 4, "body"), (None, "Front matter", 2, 2, 2, "front_matter")]
    document = structured_document(sections=sections, chunks=[(0, "Leave rules.", 2, 2), (1, "Contents.", 2, 2)])
    infos = [SectionInfo(0, "4", "Leave", 1), SectionInfo(1, None, "Front matter", 2, "front_matter")]

    excerpts, _ = modes.section_excerpts(db, document.uploaded_by, document.id, infos[0], infos)

    assert [e.content for e in excerpts] == ["Leave rules."]


def test_overlap_between_chunks_is_removed(db, alice, structured_document):
    overlap = "the next year, and"
    chunks = [(1, f"Staff get 25 days and can carry five over into {overlap}", 2, 2),
              (1, f"{overlap} must use them by March.", 3, 3)]
    document = structured_document(chunks=chunks)
    infos = [SectionInfo(i, *s[:3], s[5]) for i, s in enumerate(SECTIONS)]

    excerpts, _ = modes.section_excerpts(db, document.uploaded_by, document.id, infos[1], infos)

    assert [e.content for e in excerpts] == [chunks[0][1], "must use them by March."]


def test_a_long_section_uses_one_request_per_part_plus_one(db, ask_about, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "summary_part_chars", 2000)
    long = [(1 + i % 3, f"Point {i}. " + "x" * 900, 2 + i % 3, 2 + i % 3) for i in range(6)]
    fake_ai.reply = "Section 4 covers leave [S1]."

    answer = ask_about("Summarise section 4", chunks=sorted(long, key=lambda c: c[0]))

    parts, combine = fake_ai.reply_calls[:-1], fake_ai.reply_calls[-1]
    assert len(parts) == 3  # 6 excerpts of about 910 characters, 2 per part
    # Each part keeps the markers the excerpts have in the whole section
    assert [m for m, _ in excerpt_order(parts[1]["system_prompt"])] == ["S3", "S4"]
    assert "part 2 of 3" in parts[1]["system_prompt"]
    assert "<part_summaries" in combine["system_prompt"] and combine["system_prompt"].count("<part number=") == 3
    assert answer["outcome"] == "answered" and answer["citations"][0]["marker"] == "S1"
    assert usage(db) == 1  # four requests, one question


def test_too_many_parts_costs_no_request_and_names_the_subsections(ask_about, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "summary_part_chars", 2000)
    monkeypatch.setattr(settings, "summary_max_parts", 2)
    long = [(1 + i % 3, f"Point {i}. " + "x" * 900, 2, 2) for i in range(6)]

    answer = ask_about("Summarise section 4", chunks=sorted(long, key=lambda c: c[0]))

    assert answer["content"] == "Section 4 is too long to summarise in one go. Ask about one of its subsections: 4.1, 4.2."
    assert answer["outcome"] == "not_found" and answer["mode"] == "section"
    assert fake_ai.reply_calls == []


def test_invalid_markers_in_a_summary_are_dropped(ask_about, fake_ai):
    fake_ai.reply = "Section 4 covers leave [S1].\nSomething else [S9]."

    answer = ask_about("Summarise section 4")

    assert answer["content"] == "Section 4 covers leave [S1].\nSomething else."
    assert [c["marker"] for c in answer["citations"]] == ["S1"]


def test_a_summary_with_no_valid_marker_is_not_found(ask_about, fake_ai):
    fake_ai.reply = "Section 4 covers leave."

    assert ask_about("Summarise section 4")["content"] == NOT_FOUND_MESSAGE


def test_an_unknown_section_costs_no_ai_request(db, ask_about, fake_ai):
    answer = ask_about("Summarise section 15")

    assert answer["content"] == "I couldn't find section 15 in this document. The sections are listed under Sections."
    assert answer["mode"] == "section" and answer["outcome"] == "not_found"
    assert fake_ai.reply_calls == [] and fake_ai.embed_calls == []


def test_a_summary_counts_once_toward_the_daily_limit(db, client, alice, ask_about, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "summary_part_chars", 2000)
    monkeypatch.setattr(settings, "daily_questions_per_user", 1)
    long = [(1 + i % 3, f"Point {i}. " + "x" * 900, 2, 2) for i in range(6)]
    fake_ai.reply = "Section 4 covers leave [S1]."

    ask_about("Summarise section 4", chunks=sorted(long, key=lambda c: c[0]))

    assert len(fake_ai.reply_calls) == 4 and usage(db) == 1


@pytest.mark.parametrize("question", ["Summarise section 4", "What does page 2 say?"])
def test_ai_failure_in_a_page_or_summary_gives_503_and_saves_nothing(db, client, alice, structured_document, fake_ai, question):
    document = structured_document()
    conversation = client.post(f"/documents/{document.id}/conversations", headers=alice).json()["id"]
    fake_ai.fail_on = {"reply"}

    response = client.post(f"/conversations/{conversation}/messages", headers=alice, json={"content": question})

    assert response.status_code == 503
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Message)) == 0 and usage(db) == 0


def test_ordinary_questions_keep_mode_question(ask_about, fake_ai):
    fake_ai.vectors["How many days do staff get?"] = [1.0] * 2048
    fake_ai.reply = "25 days [S1]."

    answer = ask_about("How many days do staff get?")

    assert answer["mode"] == "question"
    assert all("Contents" not in e for e in [fake_ai.reply_calls[0]["system_prompt"]])  # front matter excluded


# --- Replies cut off at the output limit -------------------------------------------------------

def test_each_kind_of_answer_has_its_own_output_limit(ask_about, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "ai_max_tokens_page", 111)
    monkeypatch.setattr(settings, "ai_max_tokens_summary", 222)
    fake_ai.reply = "Leave rules [S1]."

    ask_about("What does page 2 say?")
    ask_about("Summarise section 4")

    assert [call["max_tokens"] for call in fake_ai.reply_calls] == [111, 222]


def test_a_cut_off_page_answer_keeps_its_complete_cited_lines(db, ask_about, fake_ai):
    fake_ai.replies = [ChatReply(
        "Page 3 is about carrying over leave [S1].\nStaff can carry over five days [S1].\nThey should ask their man [",
        "length",
    )]

    answer = ask_about("What does page 3 say?")

    assert answer["content"] == "Page 3 is about carrying over leave [S1].\nStaff can carry over five days [S1]."
    assert answer["outcome"] == "answered" and [c["marker"] for c in answer["citations"]] == ["S1"]
    assert len(fake_ai.reply_calls) == 1  # no second attempt with the same limit
    assert usage(db) == 1


def test_a_cut_off_summary_with_no_complete_cited_line_is_not_found(ask_about, fake_ai):
    fake_ai.replies = [ChatReply("Section 4 covers leave, including annual leave and sick", "length")]

    answer = ask_about("Summarise section 4")

    assert answer["content"] == NOT_FOUND_MESSAGE
    assert answer["outcome"] == "not_found" and answer["citations"] == []


def test_an_empty_cut_off_reply_is_not_found_not_a_503(db, ask_about, fake_ai):
    # A reasoning model that spent the whole limit thinking writes nothing
    fake_ai.replies = [ChatReply("", "length")]

    answer = ask_about("Summarise section 4")

    assert answer["content"] == NOT_FOUND_MESSAGE and answer["outcome"] == "not_found"
    assert len(fake_ai.reply_calls) == 1
    assert usage(db) == 1


def test_a_cut_off_part_summary_keeps_its_cited_lines_for_the_combine_request(ask_about, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "summary_part_chars", 2000)
    long = [(1 + i % 3, f"Point {i}. " + "x" * 900, 2 + i % 3, 2 + i % 3) for i in range(6)]
    fake_ai.replies = [
        "Part one [S1].",
        ChatReply("Part two [S3].\nAnd then som", "length"),
        ChatReply("", "length"),  # nothing left: not sent to the combine request
        "Section 4 covers leave [S1][S3].",
    ]

    answer = ask_about("Summarise section 4", chunks=sorted(long, key=lambda c: c[0]))

    combine = fake_ai.reply_calls[-1]["system_prompt"]
    assert combine.count("<part number=") == 2
    assert "Part two [S3]." in combine and "And then som" not in combine
    assert answer["outcome"] == "answered"


@pytest.mark.parametrize(("reply", "expected"), [
    ("One [S1].\nTwo [S2]\nThr", "One [S1].\nTwo [S2]"),
    ("One [S1].\nTwo [S2", "One [S1]."),  # a marker cut in half doesn't count
    ("One [S1].\nTwo [S1][S", "One [S1]."),
    ("One [S1, S2].\nTwo, with no marker", "One [S1, S2]."),
    ("One [S1].\nTwo [S9].", "One [S1]."),  # only valid markers count
    ("Intro line\nOne [S2]).\nTwo", "Intro line\nOne [S2])."),
    ("Nothing cited here", ""),
    ("", ""),
])
def test_trim_truncated(reply, expected):
    assert trim_truncated(reply, chunk_count=2) == expected
