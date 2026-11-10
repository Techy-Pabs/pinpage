"""Keep text from documents from posing as part of the prompt. Adapted from
ShopAssist.

The prompts mark the excerpts with <excerpts> and <excerpt ...> tags, and
part summaries with <part_summaries> and <part ...>. A document's text is
untrusted: if it could contain those tags, it could "close" its excerpt and
write its own instructions.
"""

import html
import re

REMOVED_TAG = "[removed tag]"

# Anything that looks like one of the prompts' tags, opening or closing,
# whatever the case or spacing: </excerpts>, < / EXCERPT >, <excerpt marker="S9">,
# </part>, <part_summaries>.
# An "<excerpt" with no closing ">" on its line loses only those characters,
# never the text after it. Ordinary text such as "excerpt" or "<br>" is untouched.
_PROMPT_TAG = re.compile(r"<\s*/?\s*(?:excerpts?|part(?:[\s_-]*summaries)?)\b(?:[^<>\n]*>)?", re.IGNORECASE)


def neutralize_tags(text: str) -> str:
    """Replace anything imitating the prompt's tags with "[removed tag]"."""
    return _PROMPT_TAG.sub(REMOVED_TAG, text)


def escape_attribute(value: str) -> str:
    """A value made safe to place inside a tag attribute: tags neutralized,
    quotes and angle brackets escaped, kept on one line."""
    return html.escape(" ".join(neutralize_tags(value).split()), quote=True)
