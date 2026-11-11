"""Turning an answer's [S1] style markers into citations.

Only markers that match a real excerpt count. Invalid ones are removed from
the text, and an answer left with no valid citation is replaced by the not
found message, so every answer Pinpage gives points to a page.
"""

import re
from dataclasses import dataclass

from app.services.prompts import marker as marker_for
from app.services.retrieval import RetrievedChunk

SNIPPET_LENGTH = 200

# One bracket holding one or more markers: [S1], [ s2 ], [S1, S3], [S1; S2]
_MARKER_GROUP = re.compile(r"\s*\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]", re.IGNORECASE)
_MARKER = re.compile(r"S(\d+)", re.IGNORECASE)


@dataclass
class CitedAnswer:
    text: str
    citations: list[dict]


def snippet(text: str) -> str:
    """Up to 200 characters from the start of the text, cut at a word."""
    text = " ".join(text.split())
    if len(text) <= SNIPPET_LENGTH:
        return text
    cut = text[: SNIPPET_LENGTH - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"


def citation(index: int, chunk: RetrievedChunk) -> dict:
    return {
        "marker": marker_for(index),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "section_number": chunk.section_number,
        "section_title": chunk.section_title,
        "snippet": snippet(chunk.content),
    }


def cite(answer: str, chunks: list[RetrievedChunk]) -> CitedAnswer:
    """The answer with only valid markers kept, written as [S1][S3], and
    one citation per excerpt cited, in the order first cited."""
    cited: list[int] = []

    def keep_valid(match: re.Match) -> str:
        valid = []
        for number in _MARKER.findall(match.group(1)):
            index = int(number) - 1
            if 0 <= index < len(chunks) and index not in valid:
                valid.append(index)
        for index in valid:
            if index not in cited:
                cited.append(index)
        # Invalid markers vanish, with the space before them
        return "".join(f"[{marker_for(i)}]" for i in valid)

    text = _MARKER_GROUP.sub(lambda match: _with_space(match, keep_valid), answer)
    return CitedAnswer(text=text.strip(), citations=[citation(i, chunks[i]) for i in cited])


def _with_space(match: re.Match, keep_valid) -> str:
    kept = keep_valid(match)
    if not kept:
        return ""
    # Keep a space before the marker if the model wrote one
    return (" " if match.group(0)[0].isspace() else "") + kept


# A line ending in a marker group, maybe followed by closing punctuation
_ENDS_WITH_MARKERS = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]\W*$", re.IGNORECASE)
# Punctuation that closes the sentence a marker belongs to: "... days [S1]."
_CLOSING = re.compile(r"[.!?)]*")


def _has_valid(group: str, chunk_count: int) -> bool:
    return any(0 < int(n) <= chunk_count for n in _MARKER.findall(group))


def trim_at_last_marker(answer: str, chunk_count: int) -> str:
    """A reply cut off at the output limit, up to the end of its last valid
    marker (and the punctuation closing that sentence); "" if it has none.
    Everything cited is kept, wherever the markers are on a line; what
    follows the last one is unfinished and is dropped."""
    end = 0
    for match in _MARKER_GROUP.finditer(answer):
        if _has_valid(match.group(1), chunk_count):
            end = match.end()
    if not end:
        return ""
    end = _CLOSING.match(answer, end).end()
    return answer[:end].strip()


def trim_to_cited_lines(answer: str, chunk_count: int) -> str:
    """For a cut off part summary, whose lines each end with markers: up to
    its last line that ends with a valid marker; "" if there is none."""
    lines = answer.splitlines()
    for end in range(len(lines) - 1, -1, -1):
        match = _ENDS_WITH_MARKERS.search(lines[end].rstrip())
        if match and _has_valid(match.group(1), chunk_count):
            return "\n".join(lines[: end + 1]).strip()
    return ""


def strip_markers(text: str) -> str:
    """An earlier answer without its markers, for the conversation history:
    they pointed at that question's excerpts, not this one's."""
    return _MARKER_GROUP.sub("", text).strip()
