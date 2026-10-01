"""HYw compatibility semantics, separate from the general Markdown parser."""

import re
from dataclasses import dataclass, replace
from typing import Mapping
from urllib.parse import urlsplit

from ..model import Block, Document, Inline, Limits, RenderError
from ..parser import parse


def source_origin(url: str) -> str:
    """Stable key for supplied site icons; this function performs no network access."""
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return ""
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parts.port
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += ":" + str(port)
        return parts.scheme + "://" + host
    except (ValueError, UnicodeError):
        return ""


@dataclass(frozen=True)
class Reference:
    title: str
    url: str
    snippet: str = ""
    screenshot: str = ""
    thumbnail: bool = False
    cache_id: str = ""
    fetched: bool = False
    page: bool = False
    images: tuple[str, ...] = ()
    number: int = 0


@dataclass(frozen=True)
class HywDocument:
    title: str
    sections: tuple[tuple[Block, ...], ...]
    references: tuple[Reference, ...]
    gallery: tuple[str, ...]
    runtime: str
    accent: str
    metadata: str = ""
    title_inlines: tuple[Inline, ...] = ()
    reading: bool = False


def rich(spans: tuple[Inline, ...]) -> tuple[Inline, ...]:
    result = []
    for span in spans:
        if span.kind != "text":
            result.append(span)
            continue
        parts = re.split(r"(<u>[^<]*</u>)", span.text)
        for part in parts:
            style = replace(span.style, underline=True) if part.startswith("<u>") else span.style
            text = part[3:-4] if part.startswith("<u>") else part
            cursor = 0
            for match in re.finditer(r"\s*\[(\d+(?:\s*,\s*\d+)*)\]", text):
                if match.start() > cursor:
                    result.append(Inline(text[cursor : match.start()], style))
                for number in match[1].split(","):
                    result.append(Inline(str(int(number.strip())), replace(style, citation=True)))
                cursor = match.end()
            if text[cursor:]:
                result.append(Inline(text[cursor:], style))
    return tuple(result)


def adapted(block: Block) -> Block:
    return replace(
        block, inlines=rich(block.inlines), children=tuple(adapted(c) for c in block.children)
    )


def markdown(text: str, limits: Limits) -> Document:
    text = re.sub(
        r"(?:^|\n)\s*(?:#{1,3}|\*\*)\s*(?:References|Citations|Sources)[\s\S]*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"<summary>([\s\S]*?)</summary>",
        lambda m: "\n```summary\n" + m[1].strip() + "\n```\n",
        text,
        flags=re.I,
    )
    return Document(
        tuple(adapted(b) for b in parse(text.strip(), limits, soft_breaks=True).children)
    )


def from_payload(payload: Mapping, limits: Limits) -> HywDocument:
    text = payload.get("markdown", "")
    if not isinstance(text, str) or len(text) > limits.max_chars:
        raise RenderError("HYw markdown must be a bounded string")
    accent = payload.get("theme_color") or "#ef4444"
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        raise RenderError("HYw theme_color must be #RRGGBB")
    starts = [
        m.start()
        for pattern in (r"^#\s+", r"(?:^|\n)```summary\b|<summary>")
        if (m := re.search(pattern, text, flags=re.M | re.I))
    ]
    if starts:
        text = text[min(starts) :]
    refs = []
    for page, key in ((False, "references"), (True, "page_references")):
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > limits.max_assets:
            raise RenderError(f"Invalid/too many {key}")
        for item in values:
            if not isinstance(item, dict):
                raise RenderError("Reference must be an object")
            for field in ("title", "url", "snippet", "raw_screenshot_b64", "screenshot_cache_id"):
                if not isinstance(item.get(field, ""), str):
                    raise RenderError(f"Reference {field} must be text")
            images = item.get("images", [])
            if not isinstance(images, list) or any(not isinstance(i, str) for i in images):
                raise RenderError("Reference images must be a string list")
            refs.append(
                Reference(
                    item.get("title", ""),
                    item.get("url", ""),
                    item.get("snippet", ""),
                    item.get("raw_screenshot_b64", ""),
                    bool(item.get("is_thumbnail")),
                    item.get("screenshot_cache_id", ""),
                    bool(item.get("is_fetched")),
                    page,
                    tuple(images),
                )
            )
    order = list(dict.fromkeys(int(m[1]) for m in re.finditer(r"\[(\d+)\]", text)))
    mapping = {old: new for new, old in enumerate(order, 1)}
    selected = tuple(
        replace(refs[old - 1], number=mapping[old]) for old in order if 0 < old <= len(refs)
    )
    text = re.sub(r"\[(\d+)\]", lambda m: f"[{mapping[int(m[1])]}]", text)
    title_match = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = title_match[1].strip() if title_match else ""
    if title_match:
        text = text[: title_match.start()] + text[title_match.end() :]
    blocks = markdown(text, limits).children
    sections = []
    pending = []
    for block in blocks:
        if block.kind in {"code", "table", "summary"}:
            if pending:
                sections.append(tuple(pending))
                pending = []
            sections.append((block,))
        else:
            pending.append(block)
    if pending:
        sections.append(tuple(pending))
    gallery, seen = [], set()

    def add(image):
        if not image or (not image.startswith(("http", "//")) and len(image) < 20):
            return False
        key = (image[:100], len(image))
        if key in seen:
            return False
        seen.add(key)
        gallery.append(image)
        return True

    for ref in refs:
        if ref.page:
            continue
        count = 0
        for image in ref.images:
            count += int(add(image))
            if count >= 2:
                break
    if len(gallery) < 8:
        for ref in refs:
            if not ref.page:
                for image in ref.images:
                    add(image)
                    if len(gallery) >= 12:
                        break
            if len(gallery) >= 12:
                break
    stats = payload.get("stats") or {}
    usage = stats.get("usage") or {}
    if not isinstance(usage, dict):
        raise RenderError("stats.usage must be an object")
    numbers = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key, 0) or 0
        if type(value) not in (int, float) or value < 0 or value > 1e15:
            raise RenderError("Token counts must be bounded nonnegative numbers")
        numbers[key] = value
    runtime = ""
    if any(numbers.values()):

        def fmt(key):
            return f"{numbers[key] / 10000:.2f}w"

        runtime = (
            "| Metric | Value |\n| --- | --- |\n| Tokens | "
            + fmt("total_tokens")
            + " (Input: "
            + fmt("input_tokens")
            + " / Cached: "
            + fmt("cached_input_tokens")
            + " / Output: "
            + fmt("output_tokens")
            + ") |"
        )
    return HywDocument(title, tuple(sections), selected, tuple(gallery[:12]), runtime, accent)
