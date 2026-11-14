"""Reading a PDF: the text of each page, and its bookmarks. No database,
no storage, no settings, so it can run in a separate process.

process_document reads every upload through read_pdf_with_time_limit: the
PDF is opened in a child process that is stopped if it runs over
PDF_PARSE_TIME_LIMIT_SECONDS, so a hostile or pathological file can't hold
the server. The document then fails with too_complex.
"""

import io
import logging
import multiprocessing
from dataclasses import dataclass, field

from pypdf import PasswordType, PdfReader

from app.services.sections import OutlineEntry

logger = logging.getLogger(__name__)

# A page with fewer characters than this (ignoring spaces) has no real text
MIN_CHARS_PER_PAGE = 20
# If at least this share of pages have no real text, it's a scan
SCANNED_SHARE = 0.8


class ProcessingFailed(Exception):
    """Processing failed for a reason the user is told about by its code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def looks_scanned(texts: list[str]) -> bool:
    empty = sum(1 for text in texts if len(text.strip()) < MIN_CHARS_PER_PAGE)
    return empty >= SCANNED_SHARE * len(texts)


# Outlines deeper or longer than this are cut short, so a hostile file
# can't make reading the outline expensive
MAX_OUTLINE_DEPTH = 10
MAX_OUTLINE_ENTRIES = 1000


@dataclass
class ExtractedPdf:
    texts: list[str]  # one per page, in order
    outline: list[OutlineEntry] = field(default_factory=list)


def read_outline(reader: PdfReader) -> list[OutlineEntry]:
    """The PDF's bookmarks, if it has any. Never fails: a broken outline
    just means sections come from the text instead."""
    entries: list[OutlineEntry] = []

    def walk(items, level: int) -> None:
        if level > MAX_OUTLINE_DEPTH:
            return
        for item in items:
            if len(entries) >= MAX_OUTLINE_ENTRIES:
                return
            if isinstance(item, list):
                walk(item, level + 1)  # the children of the entry before it
                continue
            page = reader.get_destination_page_number(item)
            title = str(getattr(item, "title", "") or "").replace("\x00", "")
            if title.strip() and page is not None and page >= 0:
                entries.append(OutlineEntry(title=title, level=level, page=page + 1))

    try:
        walk(reader.outline, 1)
    except Exception as error:
        logger.info("Ignoring an unreadable outline (%s)", type(error).__name__)
        return []
    return entries


def extract_pages(data: bytes, max_pages: int) -> ExtractedPdf:
    """The text of each page, in order, and the outline. Raises ProcessingFailed."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many PDFs are encrypted only to restrict printing or copying,
            # with an empty password for opening; those are readable
            try:
                unlocked = reader.decrypt("") != PasswordType.NOT_DECRYPTED
            except Exception:
                unlocked = False
            if not unlocked:
                raise ProcessingFailed("encrypted")

        page_count = len(reader.pages)
        if page_count == 0:
            raise ProcessingFailed("unreadable")
        if page_count > max_pages:
            raise ProcessingFailed("too_many_pages")

        # PostgreSQL can't store NUL characters in text
        texts = [(page.extract_text() or "").replace("\x00", "") for page in reader.pages]
    except ProcessingFailed:
        raise
    except Exception:
        # Anything pypdf trips over: damaged or unsupported files
        raise ProcessingFailed("unreadable") from None

    if looks_scanned(texts):
        raise ProcessingFailed("no_text_layer")
    return ExtractedPdf(texts=texts, outline=read_outline(reader))




# --- In a separate process ------------------------------------------------------------------

def _read_in_child(sender, data: bytes, max_pages: int) -> None:
    """Runs in the child process: sends ("ok", ExtractedPdf) or ("failed", code)."""
    try:
        result = ("ok", extract_pages(data, max_pages))
    except ProcessingFailed as failure:
        result = ("failed", failure.code)
    except Exception:
        result = ("failed", "unreadable")
    sender.send(result)
    sender.close()


def _context():
    """forkserver where there is one (Linux): children start quickly from a
    small, single threaded server process, never forked from the app's
    threads. spawn elsewhere."""
    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")
    if "forkserver" in methods:
        context.set_forkserver_preload(["app.services.pdf_text"])
    return context


def read_pdf_with_time_limit(data: bytes, max_pages: int, seconds: float, target=_read_in_child) -> ExtractedPdf:
    """extract_pages in a child process, stopped after seconds: then
    ProcessingFailed("too_complex"). A child that dies without an answer
    means the file is unreadable. target is for tests."""
    context = _context()
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=target, args=(sender, data, max_pages), daemon=True)
    child.start()
    sender.close()  # the child holds the only sending end now
    try:
        if not receiver.poll(seconds):
            logger.warning("Reading a PDF took over %.0f seconds; stopped", seconds)
            raise ProcessingFailed("too_complex")
        kind, value = receiver.recv()
    except EOFError:
        logger.warning("The PDF reading process ended without a result")
        raise ProcessingFailed("unreadable") from None
    finally:
        if child.is_alive():
            child.kill()
        child.join(timeout=5)
        receiver.close()
    if kind == "failed":
        raise ProcessingFailed(value)
    return value
