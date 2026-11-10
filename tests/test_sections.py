"""Section detection: outline first, then headings in the text, then one section."""

import pytest

from app.services.sections import (
    DocumentText,
    OutlineEntry,
    detect_sections,
    parse_heading,
)

FILLER = "Employees should read this part carefully before they apply."


def summary(sections):
    return [(s.number, s.title, s.level, s.start_page, s.end_page) for s in sections]


# --- Which lines are headings --------------------------------------------------------------

@pytest.mark.parametrize(
    "line, expected",
    [
        ("4. Leave", ("4", "Leave", 30)),
        ("4.2 Annual Leave", ("4.2", "Annual Leave", 31)),
        ("4.2.1 Carrying Over Days", ("4.2.1", "Carrying Over Days", 32)),
        ("Section 4", ("Section 4", "Section 4", 30)),
        ("Section 4.2: Annual Leave", ("Section 4.2", "Annual Leave", 31)),
        ("Chapter 3 - Pay", ("Chapter 3", "Pay", 20)),
        ("Part II", ("Part II", "Part II", 10)),
        ("PART II — Conduct", ("Part II", "Conduct", 10)),
        ("CODE OF CONDUCT", (None, "CODE OF CONDUCT", 0)),
    ],
)
def test_heading_lines(line, expected):
    assert parse_heading(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "4. employees must give two weeks' notice.",  # a sentence in a numbered list
        "1. Submit the form.",  # ends like a sentence
        "4.2 Annual Leave ........ 12",  # a contents page line
        "4.2 Annual Leave 12",  # a contents line without dots
        "2026 Staff Handbook",  # a year, not a section number
        "Chapter 3 explains how pay is calculated",  # a sentence starting with a keyword
        "OK",  # too short to be a capitals heading
        "This is an ordinary sentence in the body of the text.",
        "",
        "x" * 150,
    ],
)
def test_lines_that_are_not_headings(line):
    assert parse_heading(line) is None


# --- Outline first -------------------------------------------------------------------------------

def test_outline_gives_titles_levels_and_pages():
    doc = DocumentText([
        f"Introduction\n{FILLER}",
        f"Leave\n{FILLER}",
        f"Annual leave\n{FILLER}",
        f"Conduct\n{FILLER}",
    ])
    outline = [
        OutlineEntry("Introduction", 1, 1),
        OutlineEntry("4 Leave", 1, 2),
        OutlineEntry("4.2 Annual Leave", 2, 3),
        OutlineEntry("Conduct", 1, 4),
    ]

    sections = detect_sections(doc, outline, "Staff Handbook")

    assert summary(sections) == [
        (None, "Introduction", 1, 1, 1),
        ("4", "Leave", 1, 2, 3),  # runs until the next top-level section
        ("4.2", "Annual Leave", 2, 3, 3),
        (None, "Conduct", 1, 4, 4),
    ]


def test_outline_wins_over_headings_in_the_text():
    doc = DocumentText([f"1. Welcome\n{FILLER}", f"2. Pay\n{FILLER}"])

    sections = detect_sections(doc, [OutlineEntry("Everything", 1, 1)], "Handbook")

    assert [s.title for s in sections] == ["Everything"]


def test_outline_entries_pointing_outside_the_document_are_ignored():
    doc = DocumentText([f"1. Welcome\n{FILLER}"])

    sections = detect_sections(doc, [OutlineEntry("Ghost", 1, 9)], "Handbook")

    assert [s.title for s in sections] == ["Welcome"]  # fell back to the text


# --- Then headings in the text ----------------------------------------------------------------

def test_numbered_headings_give_sections_and_levels():
    doc = DocumentText([
        f"1. Welcome\n{FILLER}\n2. Leave\n{FILLER}",
        f"2.1 Annual Leave\n{FILLER}\n2.2 Sick Leave\n{FILLER}",
        f"3. Conduct\n{FILLER}",
    ])

    sections = detect_sections(doc, [], "Staff Handbook")

    assert summary(sections) == [
        ("1", "Welcome", 1, 1, 1),
        ("2", "Leave", 1, 1, 2),  # 3. Conduct starts at the top of page 3
        ("2.1", "Annual Leave", 2, 2, 2),
        ("2.2", "Sick Leave", 2, 2, 2),
        ("3", "Conduct", 1, 3, 3),
    ]


def test_section_ending_mid_page_includes_that_page():
    doc = DocumentText([f"1. Welcome\n{FILLER}", f"{FILLER}\n2. Pay\n{FILLER}"])

    sections = detect_sections(doc, [], "Handbook")

    assert summary(sections) == [("1", "Welcome", 1, 1, 2), ("2", "Pay", 1, 2, 2)]


def test_keyword_and_capitals_headings():
    doc = DocumentText([
        f"CODE OF CONDUCT\n{FILLER}\nSection 1: Dress\n{FILLER}\nSection 2: Phones\n{FILLER}",
    ])

    sections = detect_sections(doc, [], "Handbook")

    assert summary(sections) == [
        (None, "CODE OF CONDUCT", 1, 1, 1),
        ("Section 1", "Dress", 2, 1, 1),
        ("Section 2", "Phones", 2, 1, 1),
    ]


def test_text_before_a_first_heading_on_page_1_gets_a_section_named_after_the_document():
    doc = DocumentText([f"Welcome to the school. {FILLER}\n1. Admissions\n{FILLER}"])

    sections = detect_sections(doc, [], "Parent Guide")

    assert summary(sections) == [(None, "Parent Guide", 1, 1, 1), ("1", "Admissions", 1, 1, 1)]
    assert [s.kind for s in sections] == ["body", "body"]


TOPICS = ["Welcome", "Pay", "Leave", "Conduct", "Safety"]


def test_running_headers_repeated_on_every_page_are_not_headings():
    pages = [f"STAFF HANDBOOK 2026\n{n}. {topic}\n{FILLER}" for n, topic in enumerate(TOPICS, start=1)]

    sections = detect_sections(DocumentText(pages), [], "Handbook")

    # No section for the header, and no preamble section made of it alone
    assert [s.title for s in sections] == TOPICS


def test_titles_ending_in_a_number_look_like_a_contents_page():
    # A known limit: "Phase 2" as a numbered heading's title is skipped,
    # because contents pages end their lines with page numbers
    assert parse_heading("3. Phase 2") is None


# --- Otherwise one section ------------------------------------------------------------------

def test_no_headings_gives_one_section_named_after_the_document():
    doc = DocumentText([FILLER, FILLER, FILLER])

    sections = detect_sections(doc, [], "Visitor Policy")

    assert summary(sections) == [(None, "Visitor Policy", 1, 1, 3)]


def test_section_detection_never_fails(monkeypatch):
    from app.services import sections as sections_module

    def broken(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(sections_module, "headings_from_text", broken)

    sections = detect_sections(DocumentText([f"1. Welcome\n{FILLER}"]), [], "Handbook")

    assert summary(sections) == [(None, "Handbook", 1, 1, 1)]


def test_page_lookup():
    doc = DocumentText(["abc", "defg", "hi"])

    # "abc" + "\n\n" + "defg" + "\n\n" + "hi"
    assert [doc.page_at(i) for i in (0, 2, 5, 8, 11, 12)] == [1, 1, 2, 2, 3, 3]


def test_numbered_keyword_headings_on_every_page_are_kept():
    pages = [f"Chapter {n}: Topic\n{FILLER}" for n in range(1, 6)]

    sections = detect_sections(DocumentText(pages), [], "Handbook")

    assert [s.number for s in sections] == [f"Chapter {n}" for n in range(1, 6)]


# --- Milestone 3: outline section ends and front matter --------------------------------------

def kinds(sections):
    return [(s.title, s.kind, s.start_page, s.end_page) for s in sections]


def test_outline_section_ends_on_the_page_before_when_the_next_starts_at_the_top():
    # As in the Lantern Hill handbook: a running header comes first on every
    # page, so the heading is the second line, not the very first text
    pages = [
        f"STAFF HANDBOOK\n1. Introduction\n{FILLER}",
        f"STAFF HANDBOOK\n{FILLER}",
        f"STAFF HANDBOOK\n2. Pay\n{FILLER}",
    ]
    outline = [OutlineEntry("1 Introduction", 1, 1), OutlineEntry("2 Pay", 1, 3)]

    sections = detect_sections(DocumentText(pages), outline, "Handbook")

    assert summary(sections) == [("1", "Introduction", 1, 1, 2), ("2", "Pay", 1, 3, 3)]


def test_outline_heading_found_mid_page_keeps_the_same_page():
    pages = [
        f"1. Introduction\n{FILLER}",
        f"{FILLER}\n{FILLER}\n{FILLER}\n2. Pay\n{FILLER}",  # the heading is the 4th line
    ]
    outline = [OutlineEntry("1 Introduction", 1, 1), OutlineEntry("2 Pay", 1, 2)]

    sections = detect_sections(DocumentText(pages), outline, "Handbook")

    assert summary(sections) == [("1", "Introduction", 1, 1, 2), ("2", "Pay", 1, 2, 2)]


def test_outline_heading_not_found_on_its_page_keeps_the_old_behaviour():
    pages = [f"1. Introduction\n{FILLER}", f"{FILLER}"]  # page 2 never shows "Pay"
    outline = [OutlineEntry("1 Introduction", 1, 1), OutlineEntry("2 Pay", 1, 2)]

    sections = detect_sections(DocumentText(pages), outline, "Handbook")

    assert summary(sections)[0] == ("1", "Introduction", 1, 1, 2)  # ends on the next one's start page


def test_a_subsection_starting_at_the_top_of_a_page_ends_its_sibling_on_the_page_before():
    pages = [f"4. Leave\n{FILLER}\n4.1 Annual\n{FILLER}", f"4.2 Sick\n{FILLER}", f"5. Pay\n{FILLER}"]
    outline = [OutlineEntry("4 Leave", 1, 1), OutlineEntry("4.1 Annual", 2, 1), OutlineEntry("4.2 Sick", 2, 2),
               OutlineEntry("5 Pay", 1, 3)]

    sections = detect_sections(DocumentText(pages), outline, "Handbook")

    assert summary(sections) == [
        ("4", "Leave", 1, 1, 2), ("4.1", "Annual", 2, 1, 1), ("4.2", "Sick", 2, 2, 2), ("5", "Pay", 1, 3, 3),
    ]


def test_front_matter_when_the_first_section_starts_after_page_1():
    pages = ["Staff Handbook\nEdition 4", f"Contents\n1 Introduction ..... 3\n2 Pay ..... 4",
             f"1. Introduction\n{FILLER}", f"2. Pay\n{FILLER}"]
    outline = [OutlineEntry("1 Introduction", 1, 3), OutlineEntry("2 Pay", 1, 4)]

    sections = detect_sections(DocumentText(pages), outline, "Handbook")

    assert kinds(sections) == [
        ("Front matter", "front_matter", 1, 2),
        ("Introduction", "body", 3, 3),
        ("Pay", "body", 4, 4),
    ]
    assert sections[0].number is None and sections[0].level == 1


def test_front_matter_from_headings_in_the_text_too():
    pages = ["A cover page with only a title on it", f"1. Introduction\n{FILLER}"]

    sections = detect_sections(DocumentText(pages), [], "Handbook")

    assert kinds(sections) == [("Front matter", "front_matter", 1, 1), ("Introduction", "body", 2, 2)]


def test_no_front_matter_when_the_first_section_starts_on_page_1():
    pages = [f"1. Introduction\n{FILLER}", f"2. Pay\n{FILLER}"]

    assert all(s.kind == "body" for s in detect_sections(DocumentText(pages), [], "Handbook"))


def test_no_headings_means_no_front_matter():
    sections = detect_sections(DocumentText([FILLER, FILLER]), [], "Visitor Policy")

    assert kinds(sections) == [("Visitor Policy", "body", 1, 2)]
