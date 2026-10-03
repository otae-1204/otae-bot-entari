"""CJK-friendly asterisk boundaries, without rewriting authored Markdown.

Based on the boundary relaxation described at:
https://github.com/tats-u/markdown-cjk-friendly/blob/main/implementers-tips.md
Underscores and strikethrough intentionally keep their CommonMark/GFM rules.
"""

import regex
from markdown_it import MarkdownIt
from markdown_it.common.utils import isWhiteSpace
from markdown_it.rules_inline import StateInline, emphasis


# Use the existing regex dependency's Unicode tables; wide emoji are not CJK.
_CJK = regex.compile(
    r"(?:\p{Script=Hangul}|(?!\p{Emoji_Presentation})"
    r"[\p{East_Asian_Width=Wide}\p{East_Asian_Width=Fullwidth}"
    r"\p{East_Asian_Width=Halfwidth}])\Z"
)


def _cjk_before(source: str, start: int) -> bool:
    if start == 0:
        return False
    char = source[start - 1]
    if "\U000e0100" <= char <= "\U000e01ef":
        return True  # Ideographic variation selectors follow ideographs.
    if "\ufe00" <= char <= "\ufe0e" and start > 1:
        char = source[start - 2]
    return _CJK.fullmatch(char) is not None


def _tokenize(state: StateInline, silent: bool) -> bool:
    start = state.pos
    first_delimiter = len(state.delimiters)
    # Let markdown-it retain escaping, nesting, delimiter runs and rule-of-three
    # pairing. Only change eligibility, before its balance_pairs pass runs.
    if not emphasis.tokenize(state, silent):
        return False
    if state.src[start] != "*":
        return True
    end = state.pos
    before = state.src[start - 1] if start else " "
    after = state.src[end] if end < state.posMax else " "
    # Spaces inside emphasis remain invalid, including fullwidth spaces/newlines.
    if isWhiteSpace(ord(before)) or isWhiteSpace(ord(after)):
        return True
    if _cjk_before(state.src, start) or _CJK.fullmatch(after):
        for delimiter in state.delimiters[first_delimiter:]:
            delimiter.open = delimiter.close = True
    return True


def cjk_emphasis_plugin(md: MarkdownIt) -> None:
    """Apply the extension to this parser only; never patch global library state."""
    md.inline.ruler.at("emphasis", _tokenize)
