"""Chunking: pieces of each section that remember their pages."""

from app.services.chunking import build_chunks, embedding_input, section_label
from app.services.sections import DocumentText, detect_sections

SENTENCE = "Staff may carry over five days of annual leave into the next year. "


def test_chunks_stay_within_size_and_overlap():
    doc = DocumentText([SENTENCE * 40])
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=300, chunk_overlap=60)

    assert len(chunks) > 5
    assert all(len(c.content) <= 300 for c in chunks)
    assert [c.position for c in chunks] == list(range(len(chunks)))
    # Neighbours share some text, so a sentence cut at a boundary isn't lost
    assert chunks[0].content[-30:] in chunks[1].content


def test_chunk_records_its_pages_and_section():
    doc = DocumentText([
        f"1. Welcome\n{SENTENCE * 3}",
        f"2. Leave\n{SENTENCE * 3}",
        f"{SENTENCE * 3}",
    ])
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=1000, chunk_overlap=150)

    assert [(sections[c.section_index].title, c.page_start, c.page_end) for c in chunks] == [
        ("Welcome", 1, 1),
        ("Leave", 2, 3),  # one chunk crossing the page break
    ]
    assert chunks[1].content.startswith("2. Leave")


def test_a_chunk_never_mixes_two_sections():
    doc = DocumentText([f"1. Short\nTiny.\n2. Next\nAlso tiny."])
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=1000, chunk_overlap=150)

    assert [c.content for c in chunks] == ["1. Short\nTiny.", "2. Next\nAlso tiny."]


def test_long_section_over_many_pages_gets_the_right_pages_for_each_chunk():
    pages = [f"Page {n} text. " + SENTENCE * 12 for n in range(1, 5)]
    doc = DocumentText(pages)
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=500, chunk_overlap=0)

    for chunk in chunks:
        # Each chunk's text really is on the pages it claims
        joined = "\n\n".join(pages[chunk.page_start - 1 : chunk.page_end])
        assert chunk.content in joined
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 4


def test_label_for_embedding():
    assert section_label("Staff Handbook", "4.2", "Annual Leave") == "Staff Handbook > 4.2 Annual Leave"
    assert section_label("Staff Handbook", None, "CODE OF CONDUCT") == "Staff Handbook > CODE OF CONDUCT"
    assert section_label("Staff Handbook", "Section 4", "Section 4") == "Staff Handbook > Section 4"
    assert section_label("Staff Handbook", None, "Staff Handbook") == "Staff Handbook"
    assert embedding_input("Handbook > 1 Pay", "Paid monthly.") == "Handbook > 1 Pay\n\nPaid monthly."


HEADER = "Lantern Hill College Staff Handbook"
EDITION = "Edition 4, September 2026"


def handbook_page(number: int, body: str) -> str:
    """A page with a two line running header and a page number at the top."""
    return f"{HEADER}\n{EDITION}\nPage {number}\n{body}"


def test_running_headers_are_left_out_of_chunks_and_their_pages():
    # Section 5.4 is all on page 2; page 3 starts with the running header,
    # then section 5.5. The header must not stretch 5.4 onto page 3.
    # "Ask your manager." repeats on every page, but inside the body text.
    doc = DocumentText([
        handbook_page(1, "5.3 Induction\nAsk your manager.\nNew staff meet their mentor in week one."),
        handbook_page(2, "5.4 Probation\nAsk your manager.\nProbation lasts six months."),
        handbook_page(3, "5.5 Training\nAsk your manager.\nTraining days are in August."),
        handbook_page(4, "Book a place online.\nAsk your manager.\nEnd of training."),
    ])
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=1000, chunk_overlap=150)

    by_section = {sections[c.section_index].number: c for c in chunks}
    assert (by_section["5.4"].page_start, by_section["5.4"].page_end) == (2, 2)
    assert by_section["5.4"].content == "5.4 Probation\nAsk your manager.\nProbation lasts six months."
    # Across a page break the text joins up without the header in between
    assert (by_section["5.5"].page_start, by_section["5.5"].page_end) == (3, 4)
    assert by_section["5.5"].content == (
        "5.5 Training\nAsk your manager.\nTraining days are in August.\n\n"
        "Book a place online.\nAsk your manager.\nEnd of training."
    )
    for chunk in chunks:
        assert HEADER not in chunk.content and EDITION not in chunk.content and "Page " not in chunk.content


def test_footers_and_page_numbers_at_the_bottom_are_left_out_too():
    parts = [("Welcome", "Welcome to the college."), ("Leave", "Leave is booked online."),
             ("Pay", "Pay day is the 25th."), ("Pensions", "Ask HR about pensions.")]
    pages = [f"{n}. {title}\n{body}\nConfidential - internal use\nPage {n} of 4" for n, (title, body) in enumerate(parts, 1)]
    doc = DocumentText(pages)
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=1000, chunk_overlap=150)

    assert [c.content for c in chunks] == [f"{n}. {title}\n{body}" for n, (title, body) in enumerate(parts, 1)]
    assert [(c.page_start, c.page_end) for c in chunks] == [(n, n) for n in range(1, 5)]


def test_long_or_numeric_body_lines_are_never_taken_for_headers():
    # The same long line ending every page, and a bare number in a document
    # whose pages aren't numbered, are body text
    doc = DocumentText([f"1. Fees\nThe fee is:\n250\n{SENTENCE * 2}", f"2. Dates\n{SENTENCE * 2}", f"3. More\n{SENTENCE * 2}"])
    sections = detect_sections(doc, [], "Handbook")

    chunks = build_chunks(doc, sections, chunk_size=1000, chunk_overlap=150)

    assert chunks[0].content == f"1. Fees\nThe fee is:\n250\n{(SENTENCE * 2).strip()}"
    assert all(c.content.endswith(SENTENCE.strip()) for c in chunks)
