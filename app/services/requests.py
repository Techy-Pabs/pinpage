"""Recognising what a question asks for, without an AI request.

parse_request is a pure function: the question, the page count and the
document's sections in, a decision out. No database, no AI.

Modes:
- page: the question names pages ("page 12", "pages 10 to 11", "p. 5")
- section: it names a section by number ("section 4.6", "chapter 3"), or
  asks for a summary of one by title ("summarise the leave policy")
- question: anything else, answered by retrieval as before
"""

import re
from dataclasses import dataclass, field

MODE_QUESTION = "question"
MODE_PAGE = "page"
MODE_SECTION = "section"

# Words that don't make a title match ("the leave policy" is "leave")
TITLE_STOPWORDS = {"the", "and", "of", "policy", "section"}

# "explain" is deliberately not one: "Explain how sick pay works" is a
# question for retrieval, not a request to summarise the Sick Leave section
SUMMARY_VERBS = re.compile(r"\b(?:summari[sz]e|summary\s+of|overview\s+of)\b", re.IGNORECASE)

_NUMBER = r"(\d{1,6})"
# "pages 10 to 11", "pages 10-11", "pages 10 – 11", "pp. 10-11"
_PAGE_RANGE = re.compile(
    rf"\b(?:pages?|pp\.?)\s*{_NUMBER}\s*(?:-|–|—|to|through)\s*{_NUMBER}\b", re.IGNORECASE
)
# "pages 10 and 11", "pages 3, 5 and 7"
_PAGE_LIST = re.compile(
    rf"\bpages?\s*{_NUMBER}((?:\s*,\s*\d{{1,6}})*\s*(?:,\s*)?and\s*\d{{1,6}})", re.IGNORECASE
)
# "page 12", "p. 5", "p.5"
_PAGE_SINGLE = re.compile(rf"(?:\bpages?\s*|\bp\.\s*){_NUMBER}\b", re.IGNORECASE)

# "section 4", "section 4.6", "chapter 3", "part II"
_SECTION_REF = re.compile(
    r"\b(section|chapter|part)\s+(\d{1,3}(?:\.\d{1,3})*|[ivxlc]{1,6})\b(?!\.\d)", re.IGNORECASE
)


@dataclass
class SectionInfo:
    """What the parser needs to know about a section."""

    position: int
    number: str | None
    title: str
    level: int
    kind: str = "body"


@dataclass
class ParsedRequest:
    mode: str
    # page mode: the pages asked for, in order, without repeats. Empty for
    # other modes. Not capped here: the caller uses the first few.
    pages: list[int] = field(default_factory=list)
    # page mode: True if any page is outside 1..page_count
    out_of_range: bool = False
    # section mode: the matched section, or None with unknown_section set
    section: SectionInfo | None = None
    # section mode: the reference that matched nothing, e.g. "section 15"
    unknown_section: str | None = None


# --- Pages -------------------------------------------------------------------------------

def _pages_in(question: str) -> list[int]:
    """Every page the question names, in order of mention, without repeats."""
    found: list[tuple[int, list[int]]] = []  # (position in the text, pages)
    taken: list[tuple[int, int]] = []

    def free(match: re.Match) -> bool:
        return all(match.end() <= start or match.start() >= end for start, end in taken)

    for match in _PAGE_RANGE.finditer(question):
        first, last = int(match.group(1)), int(match.group(2))
        low, high = sorted((first, last))  # "pages 12 to 10" means 10 to 12
        # A range is never longer than a document can be; cap it so a
        # "pages 1 to 9999" can't build a huge list
        found.append((match.start(), list(range(low, min(high, low + 1000) + 1))))
        taken.append(match.span())
    for match in _PAGE_LIST.finditer(question):
        if free(match):
            numbers = [int(match.group(1)), *map(int, re.findall(r"\d{1,6}", match.group(2)))]
            found.append((match.start(), numbers))
            taken.append(match.span())
    for match in _PAGE_SINGLE.finditer(question):
        if free(match):
            found.append((match.start(), [int(match.group(1))]))
            taken.append(match.span())

    pages: list[int] = []
    for _, numbers in sorted(found):
        for number in numbers:
            if number not in pages:
                pages.append(number)
    return pages


# --- Sections ------------------------------------------------------------------------------

def _number_token(number: str) -> str:
    """"Section 4.2" -> "4.2", "Part II" -> "ii", "4" -> "4"."""
    parts = number.split()
    return parts[-1].rstrip(".").lower() if parts else ""


def _keyword(number: str) -> str | None:
    parts = number.split()
    return parts[0].lower() if len(parts) > 1 else None


def _by_reference(keyword: str, token: str, sections: list[SectionInfo]) -> SectionInfo | None:
    """The section numbered token. If two match (a "Part 4" and a "4"),
    prefer the one using the same keyword, then the higher level."""
    matches = [s for s in sections if s.number and _number_token(s.number) == token.lower()]
    if not matches:
        return None
    return min(matches, key=lambda s: (_keyword(s.number) != keyword.lower(), s.level, s.position))


def _stem(word: str) -> str:
    """Ignore a simple plural s: "holidays" and "holiday" match."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def _by_title(question: str, sections: list[SectionInfo]) -> SectionInfo | None:
    """The section whose every meaningful title word is in the question. If
    several match, the longest title wins, then the higher level."""
    asked = _words(question)
    matches = []
    for section in sections:
        meaningful = {w for w in _words(section.title) if w not in TITLE_STOPWORDS}
        if meaningful and meaningful <= asked:
            matches.append(section)
    if not matches:
        return None
    return min(matches, key=lambda s: (-len(s.title), s.level, s.position))


# --- The parser ----------------------------------------------------------------------------

def parse_request(question: str, page_count: int, sections: list[SectionInfo]) -> ParsedRequest:
    pages = _pages_in(question)
    if pages:
        return ParsedRequest(
            mode=MODE_PAGE, pages=pages, out_of_range=any(not 1 <= p <= page_count for p in pages)
        )

    # Front matter has no number or real title to ask for, and is never summarised
    body = [s for s in sections if s.kind == "body"]

    unknown = None
    for reference in _SECTION_REF.finditer(question):
        keyword, token = reference.group(1), reference.group(2)
        # "part" only counts with a number or a roman numeral, never "part time"
        section = _by_reference(keyword, token, body)
        if section is not None:
            return ParsedRequest(mode=MODE_SECTION, section=section)
        # A roman numeral that matches nothing is ignored: "the part I don't
        # understand" is not a reference. A digit is: "section 15" is unknown.
        if unknown is None and token[0].isdigit():
            unknown = f"{keyword.lower()} {token}"
    if unknown is not None:
        return ParsedRequest(mode=MODE_SECTION, unknown_section=unknown)

    if SUMMARY_VERBS.search(question):
        section = _by_title(question, body)
        if section is not None:
            return ParsedRequest(mode=MODE_SECTION, section=section)

    return ParsedRequest(mode=MODE_QUESTION)
