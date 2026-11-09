"""Finding a document's sections. No AI: the PDF's own outline when it has
one, otherwise heading lines in the text, otherwise one section.

Section detection never fails a document. At worst, everything sits in
one section named after the document.
"""

import bisect
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Pages are joined with a blank line, so the text splitter prefers to cut
# chunks at a page break
PAGE_SEPARATOR = "\n\n"

MAX_TITLE_LENGTH = 500
MAX_NUMBER_LENGTH = 50
# More candidates than this means the "headings" are really list items
MAX_SECTIONS = 300
MAX_HEADING_LINE = 100
MAX_HEADING_WORDS = 12

# "4", "4.2", "4.2.1" then a title: "4. Leave", "4.2 Annual Leave"
_NUMBERED = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,4})\.?\s+(\S.*)$")
# "Section 4", "Chapter 3: Pay", "Part II - Conduct", "Article 7.1"
_KEYWORD = re.compile(
    r"^(part|chapter|section|article)\s+(\d{1,3}(?:\.\d{1,3})*|[ivxlcdm]{1,7})\b\.?(?:\s*[:.\-–—]\s*|\s+|$)(.*)$",
    re.IGNORECASE,
)
_KEYWORD_RANK = {"part": 10, "chapter": 20, "section": 30, "article": 30}
_CAPS_RANK = 0  # a short line in capitals reads as a top-level heading
_NUMBERED_RANK = 30  # "4." sits where "Section 4" would

_TRAILING_PAGE_NUMBER = re.compile(r"\s\d+$")  # a contents page: "4.2 Annual Leave  12"
_DOT_LEADER = re.compile(r"\.{3,}|(?:\. ){3,}")


@dataclass
class OutlineEntry:
    title: str
    level: int  # 1 is the top level
    page: int  # starts at 1


@dataclass
class Section:
    number: str | None
    title: str
    level: int
    start_page: int
    end_page: int
    # Where the section's own text starts and ends in DocumentText.text (its
    # text stops where the next section of any level starts)
    start_offset: int
    end_offset: int


class DocumentText:
    """All pages joined into one text, remembering where each page starts."""

    def __init__(self, pages: list[str]):
        self.pages = pages
        self.page_starts: list[int] = []
        offset = 0
        for page in pages:
            self.page_starts.append(offset)
            offset += len(page) + len(PAGE_SEPARATOR)
        self.text = PAGE_SEPARATOR.join(pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_at(self, offset: int) -> int:
        """The page (from 1) holding this character of the joined text."""
        return max(1, bisect.bisect_right(self.page_starts, offset))

    def starts_its_page(self, offset: int) -> bool:
        """True if only whitespace comes before this point on its page."""
        page_start = self.page_starts[self.page_at(offset) - 1]
        return not self.text[page_start:offset].strip()


@dataclass
class _Heading:
    offset: int
    number: str | None
    title: str
    rank: float  # a raw level; turned into 1, 2, 3... afterwards


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit].strip()


# --- Headings from the text -----------------------------------------------------------

def _looks_like_title(title: str) -> bool:
    return (
        bool(title)
        and title[0].isupper()
        and len(title.split()) <= MAX_HEADING_WORDS
        and title[-1] not in ".,;:"
        and not _TRAILING_PAGE_NUMBER.search(title)
        and not _DOT_LEADER.search(title)
    )


def _is_caps_heading(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    return (
        3 <= len(line) <= 80
        and len(letters) >= 3
        and line == line.upper()
        and len(line.split()) <= 10
        and line[-1] not in ".,;:"
        and not _DOT_LEADER.search(line)
        and not _TRAILING_PAGE_NUMBER.search(line)
    )


def parse_heading(line: str) -> tuple[str | None, str, float] | None:
    """(number, title, raw level) if the line looks like a heading."""
    line = line.strip()
    if not line or len(line) > MAX_HEADING_LINE:
        return None

    match = _KEYWORD.match(line)
    if match:
        keyword, number, rest = match.groups()
        number_text = f"{keyword.capitalize()} {number}"
        title = rest.strip() or number_text
        if rest.strip() and not _looks_like_title(title):
            return None
        depth = number.count(".") if number[0].isdigit() else 0
        return number_text, title, _KEYWORD_RANK[keyword.lower()] + depth

    match = _NUMBERED.match(line)
    if match:
        number, title = match.groups()
        if not _looks_like_title(title.strip()):
            return None
        return number, title.strip(), _NUMBERED_RANK + number.count(".")

    if _is_caps_heading(line):
        return None, line, _CAPS_RANK
    return None


def _repeated_lines(pages: list[str]) -> set[str]:
    """Lines that repeat on many pages: running headers and footers, not headings."""
    if len(pages) < 3:
        return set()
    seen: dict[str, int] = {}
    for page in pages:
        for key in {_repeat_key(line) for line in page.splitlines() if line.strip()}:
            seen[key] = seen.get(key, 0) + 1
    threshold = max(3, int(0.3 * len(pages)))
    return {key for key, count in seen.items() if count >= threshold}


def _repeat_key(line: str) -> str:
    # Exact apart from spacing and case. Digits count: "Chapter 1" and
    # "Chapter 2" are different headings, while a running header that looks
    # like a heading ("STAFF HANDBOOK 2026") repeats exactly.
    return " ".join(line.lower().split())


def headings_from_text(doc: DocumentText, repeated: set[str]) -> list[_Heading]:
    headings = []
    for page_number, page in enumerate(doc.pages, start=1):
        position = doc.page_starts[page_number - 1]
        for line in page.splitlines(keepends=True):
            parsed = parse_heading(line)
            if parsed and _repeat_key(line) not in repeated:
                number, title, rank = parsed
                offset = position + (len(line) - len(line.lstrip()))
                headings.append(_Heading(offset, number, _clean(title, MAX_TITLE_LENGTH), rank))
            position += len(line)
    return headings


# --- Headings from the outline -------------------------------------------------------

def headings_from_outline(doc: DocumentText, outline: list[OutlineEntry]) -> list[_Heading]:
    headings = []
    for entry in outline:
        if not 1 <= entry.page <= doc.page_count:
            continue
        title = _clean(entry.title, MAX_TITLE_LENGTH)
        if not title:
            continue
        number = None
        parsed = parse_heading(title)
        if parsed and parsed[0]:
            number, title = parsed[0], parsed[1]
        headings.append(_Heading(_find_on_page(doc, entry.page, entry.title), number, title, entry.level))
    return headings


def _find_on_page(doc: DocumentText, page: int, title: str) -> int:
    """Where the outline entry's title appears on its page, or the page start."""
    start = doc.page_starts[page - 1]
    page_text = doc.pages[page - 1]
    needle = " ".join(title.split())[:40].lower()
    found = page_text.lower().find(needle) if needle else -1
    return start + found if found >= 0 else start


# --- Building the sections ------------------------------------------------------------

def _levels(headings: list[_Heading]) -> dict[float, int]:
    """Raw levels in use, turned into 1, 2, 3... from the highest down."""
    return {rank: level for level, rank in enumerate(sorted({h.rank for h in headings}), start=1)}


def _has_own_text(text: str, repeated: set[str]) -> bool:
    """True if the text has lines other than running headers and footers."""
    return any(line.strip() and _repeat_key(line) not in repeated for line in text.splitlines())


def _build(doc: DocumentText, headings: list[_Heading], title: str, repeated: set[str]) -> list[Section]:
    headings = sorted(headings, key=lambda h: h.offset)
    levels = _levels(headings)
    items = [(h.offset, h.number, h.title, levels[h.rank]) for h in headings]

    # Text before the first heading (a cover page, an introduction) gets a
    # section of its own, named after the document; a running header alone doesn't
    if _has_own_text(doc.text[: items[0][0]], repeated):
        items.insert(0, (0, None, title, 1))

    sections = []
    for i, (offset, number, heading_title, level) in enumerate(items):
        end_offset = items[i + 1][0] if i + 1 < len(items) else len(doc.text)
        end_page = doc.page_count
        for later_offset, _, _, later_level in items[i + 1 :]:
            if later_level <= level:
                # It ends where the next section at its level or higher starts:
                # on the page before, if that section starts at the top of a page
                end_page = doc.page_at(later_offset)
                if doc.starts_its_page(later_offset):
                    end_page -= 1
                break
        start_page = doc.page_at(offset)
        sections.append(Section(
            number=number,
            title=heading_title,
            level=level,
            start_page=start_page,
            end_page=max(start_page, end_page),
            start_offset=offset,
            end_offset=end_offset,
        ))
    return sections


def single_section(doc: DocumentText, title: str) -> list[Section]:
    return [Section(None, title, 1, 1, doc.page_count, 0, len(doc.text))]


def detect_sections(doc: DocumentText, outline: list[OutlineEntry], title: str) -> list[Section]:
    """Sections from the outline, else from headings in the text, else one."""
    title = _clean(title, MAX_TITLE_LENGTH) or "Document"
    try:
        repeated = _repeated_lines(doc.pages)
        for find in (lambda: headings_from_outline(doc, outline), lambda: headings_from_text(doc, repeated)):
            # Several outline entries or headings pointing at the same place
            # would make empty sections: keep the first at each point
            unique = list({h.offset: h for h in reversed(find())}.values())
            if 0 < len(unique) <= MAX_SECTIONS:
                return _build(doc, unique, title, repeated)
    except Exception as error:
        # Never fails the document: fall back to one section
        logger.warning("Section detection failed (%s); using one section", type(error).__name__)
    return single_section(doc, title)
