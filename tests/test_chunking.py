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
