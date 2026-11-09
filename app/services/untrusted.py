"""Keep text from documents from posing as part of the prompt. Adapted from
ShopAssist.

The grounding prompt marks the excerpts with <excerpts> and <excerpt ...>
tags. A document's text is untrusted: if it could contain those tags, it
could "close" its excerpt and write its own instructions.
"""

import html
import re

REMOVED_TAG = "[removed tag]"

# Anything that looks like one of the prompt's tags, opening or closing,
# whatever the case or spacing: </excerpts>, < / EXCERPT >, <excerpt marker="S9">.
# An "<excerpt" with no closing ">" on its line loses only those characters,
# never the text after it. Ordinary text such as "excerpt" or "<br>" is untouched.
_PROMPT_TAG = re.compile(r"<\s*/?\s*excerpts?\b(?:[^<>\n]*>)?", re.IGNORECASE)


def neutralize_tags(text: str) -> str:
    """Replace anything imitating the prompt's tags with "[removed tag]"."""
    return _PROMPT_TAG.sub(REMOVED_TAG, text)


def escape_attribute(value: str) -> str:
    """A value made safe to place inside a tag attribute: tags neutralized,
    quotes and angle brackets escaped, kept on one line."""
    return html.escape(" ".join(neutralize_tags(value).split()), quote=True)
