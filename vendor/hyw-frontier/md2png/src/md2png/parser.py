"""CommonMark tokens are consumed once, preserving nesting and source ranges."""

import re
from dataclasses import replace

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.dollarmath.index import math_inline_dollar
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.texmath import texmath_plugin

from .cjk_emphasis import cjk_emphasis_plugin
from .model import Block, Document, Inline, Limits, RenderError, Style


def _parenthesized_currency(source: str, pos: int) -> bool:
    """A standalone ($) or （$） is a currency label, not a math delimiter."""
    left, right = pos - 1, pos + 1
    while left >= 0 and source[left] in " \t":
        left -= 1
    while right < len(source) and source[right] in " \t":
        right += 1
    return (left >= 0 and right < len(source)
            and (source[left], source[right]) in {("(", ")"), ("（", "）")})


_dollar_math = math_inline_dollar(allow_space=False, allow_digits=False)


def _currency_safe_dollar_math(state, silent: bool) -> bool:
    start = state.pos
    if state.src[start] != "$" or _parenthesized_currency(state.src, start):
        return False
    # Probe without emitting tokens: a currency symbol must not close an earlier
    # dollar either. Restore the cursor on rejection, including silent link scans.
    if not _dollar_math(state, True):
        return False
    end = state.pos
    state.pos = start
    if _parenthesized_currency(state.src, end - 1):
        return False
    return _dollar_math(state, silent)


def parse(markdown: str, limits: Limits = Limits(), *, soft_breaks: bool = False) -> Document:
    if len(markdown) > limits.max_chars:
        raise RenderError(f"Markdown exceeds {limits.max_chars} characters")
    md = MarkdownIt("commonmark", {"html": False, "maxNesting": limits.max_depth + 1})
    md.use(cjk_emphasis_plugin)
    md.enable(["table", "strikethrough"]).use(
        dollarmath_plugin, allow_space=False, allow_digits=False
    )
    md.inline.ruler.at("math_inline", _currency_safe_dollar_math)
    md.use(texmath_plugin, delimiters="brackets")
    # Keep definitions even when unreferenced, and labels stable across summary parts.
    md.use(footnote_plugin, inline=False, move_to_end=False, always_match_refs=True)
    count = 0

    def tree(text: str, offset: int = 0, depth: int = 0) -> SyntaxTreeNode:
        nonlocal count
        tokens = md.parse(text)
        # Count nested summary tokens too, before constructing their syntax tree.
        count += sum(1 + len(t.children or []) for t in tokens)
        if count > limits.max_nodes:
            raise RenderError("Markdown node budget exceeded")
        if any(t.level + depth >= limits.max_depth for t in tokens) or any(
            child.level + depth >= limits.max_depth for t in tokens for child in (t.children or [])
        ):
            raise RenderError("Markdown nesting budget exceeded")
        for token in tokens:
            if token.map:
                token.map = [line + offset for line in token.map]
        return SyntaxTreeNode(tokens)

    root = tree(markdown)
    serial = 0

    def inlines(node: SyntaxTreeNode, style: Style = Style()) -> tuple[Inline, ...]:
        result: list[Inline] = []
        for child in node.children:
            kind = child.type
            if kind == "strong":
                result.extend(inlines(child, replace(style, bold=True)))
            elif kind == "em":
                result.extend(inlines(child, replace(style, italic=True)))
            elif kind == "s":
                result.extend(inlines(child, replace(style, strike=True)))
            elif kind == "link":
                result.extend(inlines(child, replace(style, link=str(child.attrs.get("href", "")))))
            elif kind == "inline":
                result.extend(inlines(child, style))
            elif kind == "code_inline":
                result.append(Inline(child.content, replace(style, code=True)))
            elif kind == "hardbreak":
                result.append(Inline(kind="break"))
            elif kind == "softbreak":
                result.append(Inline(kind="break") if soft_breaks else Inline(" ", style))
            elif kind == "math_inline":
                result.append(Inline(child.content, style, "math"))
            elif kind == "footnote_ref":
                result.append(Inline("[^" + child.meta["label"] + "]", style))
            elif kind == "image":
                result.append(
                    Inline(child.content, style, "image", str(child.attrs.get("src", "")))
                )
            elif kind == "text":
                result.append(Inline(child.content, style))
            else:
                raise RenderError(f"Unsupported inline token: {kind}")
        return tuple(result)

    def block(node: SyntaxTreeNode, depth: int = 0) -> Block:
        nonlocal serial
        serial += 1
        if serial > limits.max_nodes or depth > limits.max_depth:
            raise RenderError("Markdown structural budget exceeded")
        identifier = f"b{serial:04d}"
        source = tuple(node.map) if node.map else None
        kind = node.type
        base = {"id": identifier, "source": source}
        if kind in {"paragraph", "heading", "th", "td"}:
            spans = inlines(node)
            if kind in {"th", "td"}:
                align = str(node.attrs.get("style", "text-align:left")).split(":")[-1]
                return Block(**base, kind="cell", inlines=spans, header=kind == "th", align=align)
            if kind == "heading":
                return Block(**base, kind="heading", inlines=spans, level=int(node.tag[1:]))
            images = [s for s in spans if s.kind == "image"]
            if images and all(s.kind == "image" or not s.text.strip() for s in spans):
                return Block(**base, kind="gallery", inlines=tuple(images))
            return Block(**base, kind="paragraph", inlines=spans, tight=node.hidden)
        if kind in {"fence", "code_block"}:
            language = node.info.strip().split()[0] if node.info.strip() else "text"
            if language in {"summary", "摘要"}:
                # A summary is a semantic container, not code with a special paint path.
                nested = tree(node.content, (source[0] + 1) if source else 0, depth + 1)
                return Block(
                    **base,
                    kind="summary",
                    children=tuple(block(n, depth + 1) for n in nested.children),
                )
            return Block(
                **base, kind="code", text=node.content.removesuffix("\n"), language=language
            )
        if kind == "math_block":
            return Block(**base, kind="math", text=node.content.strip())
        if kind == "hr":
            return Block(**base, kind="rule")
        if kind == "table":
            rows = [row for section in node.children for row in section.children]
            return Block(**base, kind="table", children=tuple(block(n, depth + 1) for n in rows))
        if kind == "footnote_reference":
            children = tuple(block(n, depth + 1) for n in node.children)
            label = Inline("[^" + node.meta["label"] + "]: ", Style(bold=True))
            if children and children[0].kind == "paragraph":
                children = (
                    replace(children[0], inlines=(label,) + children[0].inlines),
                    *children[1:],
                )
            else:
                children = (Block(identifier + ":label", "paragraph", inlines=(label,)), *children)
            return Block(**base, kind="quote", children=children, language="footnote")
        containers = {"blockquote": "quote", "list_item": "item", "tr": "row"}
        if kind in {"bullet_list", "ordered_list"}:
            return Block(
                **base,
                kind="list",
                ordered=kind == "ordered_list",
                start=int(node.attrs.get("start", 1)),
                children=tuple(block(n, depth + 1) for n in node.children),
            )
        if kind in containers:
            children = tuple(block(n, depth + 1) for n in node.children)
            language = ""
            checked = None
            if children and children[0].kind == "paragraph" and children[0].inlines:
                first = children[0].inlines[0]
                if kind == "list_item" and first.kind == "text":
                    task = re.match(r"^\[([ xX])\]\s+", first.text)
                    if task:
                        checked = task[1].lower() == "x"
                        children = (
                            replace(
                                children[0],
                                inlines=(
                                    replace(first, text=first.text[task.end() :]),
                                    *children[0].inlines[1:],
                                ),
                            ),
                            *children[1:],
                        )
                elif kind == "blockquote" and first.kind == "text":
                    note = re.fullmatch(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", first.text)
                    if note:
                        language = "admonition"
                        children = (
                            replace(
                                children[0],
                                inlines=(
                                    Inline(note[1], Style(bold=True)),
                                    *children[0].inlines[1:],
                                ),
                            ),
                            *children[1:],
                        )
            return Block(
                **base,
                kind=containers[kind],
                children=children,
                language=language,
                checked=checked,
            )
        raise RenderError(f"Unsupported block token: {kind}")

    return Document(tuple(block(n) for n in root.children))
