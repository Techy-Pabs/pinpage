"""The prompt leak check: an answer that repeats the prompt's rules is
never shown.

Before an answer is saved, it is checked for any run of LEAK_RUN_CHARS or
more characters copied from the fixed text of the answering prompts,
ignoring case and spacing (all whitespace is removed before comparing, so
the run is 40 characters of text, not counting spaces). If one is found,
the answer is replaced with PROMPT_LEAK_MESSAGE.

Only the prompts' own words count. Everything filled in at request time is
left out, so a normal answer can never match it: the document's excerpts
and title, the part summaries, and the not found message (which the rules
ask the model to repeat word for word).
"""

import re
import string
from functools import lru_cache

from app.services.prompts import COMBINE_PROMPT, GROUNDING_PROMPT, PAGE_PROMPT, PART_NOTE, SUMMARY_PROMPT

LEAK_RUN_CHARS = 40

PROMPT_LEAK_MESSAGE = "I can't share that. Ask me about the document."

# Every prompt the answering model sees
_TEMPLATES = (GROUNDING_PROMPT, PAGE_PROMPT, SUMMARY_PROMPT, PART_NOTE, COMBINE_PROMPT)

_WHITESPACE = re.compile(r"\s+")


def _normalized(text: str) -> str:
    return _WHITESPACE.sub("", text).lower()


def _fixed_parts(template: str) -> list[str]:
    """The template's literal text, split wherever a value is filled in
    ({excerpts}, {document_title}, {not_found}, ...), so that no run can
    span the document's own text."""
    return [literal for literal, _, _, _ in string.Formatter().parse(template) if literal]


@lru_cache(maxsize=1)
def _prompt_runs() -> frozenset[str]:
    """Every LEAK_RUN_CHARS long run of the prompts' fixed text, normalized."""
    runs = set()
    for template in _TEMPLATES:
        for part in _fixed_parts(template):
            text = _normalized(part)
            runs.update(text[i:i + LEAK_RUN_CHARS] for i in range(len(text) - LEAK_RUN_CHARS + 1))
    return frozenset(runs)


def repeats_prompt(answer: str) -> bool:
    """True if the answer copies LEAK_RUN_CHARS or more characters of the
    prompts' rules, ignoring case and spacing."""
    text = _normalized(answer)
    runs = _prompt_runs()
    return any(text[i:i + LEAK_RUN_CHARS] in runs for i in range(len(text) - LEAK_RUN_CHARS + 1))
