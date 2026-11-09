"""PDFs made inside the tests, so no real documents live in the repo.

make_pdf builds a tiny PDF by hand; the others use reportlab (a test-only
package) for real text, and pypdf for encryption.
"""

import io


def make_pdf(*page_texts: str) -> bytes:
    """A valid PDF with one page per text (one page saying "Hello" by default).

    Different texts give different files, and so different fingerprints.
    """
    texts = page_texts or ("Hello",)
    page_count = len(texts)
    # Objects: 1 catalog, 2 page list, 3 font, then a page and its content per text
    page_ids = [4 + 2 * i for i in range(page_count)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % pid for pid in page_ids), page_count),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, text in zip(page_ids, texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % escaped.encode("latin-1")
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (page_id + 1)
        )
        objects[page_id + 1] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)

    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref_at = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at)
    return bytes(out)


# --- PDFs made with reportlab (real text, as a word processor would make) ---------

def text_pdf(page_texts: list[str]) -> bytes:
    """One page per text, each written as real, extractable text."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)  # invariant: the same input gives the same bytes
    for text in page_texts:
        y = 750
        for line in text.split("\n"):
            pdf.drawString(72, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def image_only_pdf(pages: int = 3) -> bytes:
    """Pages with only drawings and no text, like a scan."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for i in range(pages):
        pdf.rect(72, 300 + i * 10, 400, 300, fill=1)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def encrypted_pdf(user_password: str, algorithm: str = "AES-256") -> bytes:
    """A text PDF encrypted with pypdf. An empty user_password means anyone
    can open it; only the owner password (for editing) is set."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf([LONG_TEXT, LONG_TEXT]))))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm=algorithm)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def damaged_pdf() -> bytes:
    """Starts like a PDF, so the upload accepts it, but isn't one."""
    return b"%PDF-1.7\n" + b"\x8f garbage that no PDF reader can make sense of \x00\xff" * 50


# Long enough to count as a real text page (20 characters or more)
LONG_TEXT = "This page has a real text layer with plenty of words."


def outlined_pdf(page_texts: list[str], outline: list[tuple[str, int, int]]) -> bytes:
    """A text PDF with bookmarks. outline: (title, level from 1, page from 1)."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for page_number, text in enumerate(page_texts, start=1):
        y = 750
        for line in text.split("\n"):
            pdf.drawString(72, y, line)
            y -= 16
        for index, (title, level, page) in enumerate(outline):
            if page == page_number:
                key = f"bookmark-{index}"
                pdf.bookmarkPage(key)
                pdf.addOutlineEntry(title, key, level=level - 1)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
