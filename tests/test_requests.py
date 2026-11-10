"""The request parser: a pure function, so every case is tested directly."""

import pytest

from app.services.requests import MODE_PAGE, MODE_QUESTION, MODE_SECTION, SectionInfo, parse_request

# Like the Lantern Hill handbook
SECTIONS = [
    SectionInfo(0, None, "Front matter", 1, "front_matter"),
    SectionInfo(1, "1", "Introduction", 1),
    SectionInfo(2, "4", "Leave", 1),
    SectionInfo(3, "4.1", "Annual Leave", 2),
    SectionInfo(4, "4.2", "Sick Leave", 2),
    SectionInfo(5, "4.6", "Public Holidays", 2),
    SectionInfo(6, "5", "Recruitment and Induction", 1),
    SectionInfo(7, "5.4", "Probation", 2),
    SectionInfo(8, "Part II", "Conduct", 1),
    SectionInfo(9, "Chapter 7", "Safeguarding", 1),
]


def parse(question, page_count=20, sections=SECTIONS):
    return parse_request(question, page_count, sections)


# --- Pages ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question, pages",
    [
        ("What does page 12 say?", [12]),
        ("what does PAGE 3 cover", [3]),
        ("Summarise pages 10 to 11", [10, 11]),
        ("pages 10-11 please", [10, 11]),
        ("pages 10 – 12", [10, 11, 12]),  # an en dash
        ("Compare pages 4 and 9", [4, 9]),
        ("pages 3, 5 and 7", [3, 5, 7]),
        ("What is on p. 5?", [5]),
        ("see p.6", [6]),
        ("pages 12 to 10", [10, 11, 12]),  # a reversed range
        ("pages 7 to 7", [7]),
        ("page 2 and page 9", [2, 9]),
        ("pages 5", [5]),  # a plural with one number
        ("page 4 then pages 4-5", [4, 5]),  # no repeats
    ],
)
def test_page_requests(question, pages):
    request = parse(question)

    assert request.mode == MODE_PAGE
    assert request.pages == pages
    assert request.out_of_range is False


@pytest.mark.parametrize("question", ["What does page 40 say?", "pages 19 to 21", "page 0"])
def test_out_of_range_pages(question):
    request = parse(question)

    assert request.mode == MODE_PAGE and request.out_of_range is True


def test_a_huge_range_is_capped_and_out_of_range():
    request = parse("pages 1 to 999999")

    assert request.out_of_range is True
    assert len(request.pages) <= 1001


def test_pages_come_before_sections():
    assert parse("What does page 10 of section 4 say?").mode == MODE_PAGE


# --- Sections by number ------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question, title",
    [
        ("Summarise section 4", "Leave"),
        ("What does section 4.6 say?", "Public Holidays"),
        ("SECTION 5.4?", "Probation"),
        ("Tell me about chapter 7", "Safeguarding"),
        ("Summarise part II", "Conduct"),
        ("What is in Part ii?", "Conduct"),
        ("section 4. What does it cover?", "Leave"),  # a full stop after the number
        ("And chapter 4?", "Leave"),  # the keyword needn't match the document's
    ],
)
def test_sections_by_number(question, title):
    request = parse(question)

    assert request.mode == MODE_SECTION
    assert request.section.title == title


@pytest.mark.parametrize("question, reference", [("Summarise section 15", "section 15"), ("chapter 4.9", "chapter 4.9")])
def test_unknown_section_numbers(question, reference):
    request = parse(question)

    assert request.mode == MODE_SECTION
    assert request.section is None
    assert request.unknown_section == reference


def test_same_keyword_wins_when_two_sections_share_a_number():
    sections = [SectionInfo(0, "4", "Pay", 1), SectionInfo(1, "Part 4", "Annexes", 1)]

    assert parse("Summarise part 4", sections=sections).section.title == "Annexes"
    assert parse("Summarise section 4", sections=sections).section.title == "Pay"


@pytest.mark.parametrize(
    "question",
    [
        "Is there a part I don't understand?",  # "part I": a pronoun, no such section
        "Do part time staff get leave?",  # "part" without a number
    ],
)
def test_words_that_only_look_like_references(question):
    assert parse(question, sections=SECTIONS[:8]).mode == MODE_QUESTION


# --- Sections by title ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question, title",
    [
        ("Summarise the leave policy", "Leave"),
        ("Give me an overview of probation", "Probation"),
        ("summarize the sick leave section", "Sick Leave"),  # longest title wins over "Leave"
        ("A summary of public holidays", "Public Holidays"),
        ("Explain the public holiday rules", "Public Holidays"),  # plural s ignored
        ("Summarise recruitment and induction", "Recruitment and Induction"),
        ("Overview of the annual leaves", "Annual Leave"),
    ],
)
def test_summary_verb_and_a_title(question, title):
    request = parse(question)

    assert request.mode == MODE_SECTION
    assert request.section.title == title


def test_a_title_tie_goes_to_the_higher_level():
    sections = [SectionInfo(0, "4.1", "Leave Rules", 2), SectionInfo(1, "4", "Rules Leave", 1)]

    assert parse("Summarise the leave rules", sections=sections).section.number == "4"


def test_every_meaningful_title_word_must_be_there():
    # "Recruitment and Induction" needs both words
    assert parse("Summarise recruitment").mode == MODE_QUESTION


def test_a_title_without_a_summary_verb_is_a_question():
    assert parse("How long is probation?").mode == MODE_QUESTION


def test_front_matter_is_never_matched():
    assert parse("Summarise the front matter").mode == MODE_QUESTION


# --- Ordinary questions ------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "How many days after 5 years?",
        "How long is paternity leave?",
        "Can I carry over 10 days?",
        "Is the 2026 rate different?",
        "What happens at 3 pm?",
        "Who do I page in an emergency?",  # "page" as a verb, no number
    ],
)
def test_ordinary_questions(question):
    request = parse(question)

    assert request.mode == MODE_QUESTION
    assert request.pages == [] and request.section is None and request.unknown_section is None
