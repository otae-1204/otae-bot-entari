"""Application answer protocol → HYw AST; presentation policy lives in render_protocol.

The Python answer protocol parser decides wrapper recovery/scoring visibility.
Only reviewed, supplied image bytes may render; other images remain links and HTML stays inert.
"""
import re
from dataclasses import replace
from urllib.parse import urlsplit

from md2png.hyw.document import HywDocument, Reference
from md2png.model import Block, Inline, Limits, Style
from md2png.model import RenderError as EngineError
from md2png.parser import parse

from .render_protocol import parse_answer as parse_protocol
from .source_titles import source_key
from .media_refs import is_display_url

__all__ = ["adapt_answer", "parse_protocol"]


def safe_url(value: str) -> str:
    try:
        url = urlsplit(value)
        _ = url.port
        return value if (url.scheme in ('http', 'https') and url.hostname
                         and not (url.username or url.password)
                         and not any(c.isspace() or ord(c) < 32 for c in value)) else ''
    except ValueError:
        return ''


def linked_spans(span: Inline) -> list[Inline]:
    """Recognize bare HTTP(S) URLs without parsing code or fetching resources."""
    if span.kind != 'text' or span.style.code or span.style.link:
        return [span]
    result, cursor = [], 0
    for match in re.finditer(r'''https?://[^\s<>"'`，。；！？、（）【】]+''', span.text):
        candidate = match[0].rstrip('.,;:!?')
        while candidate and candidate[-1] in ')]}' and candidate.count(candidate[-1]) > candidate.count({')': '(', ']': '[', '}': '{'}[candidate[-1]]):
            candidate = candidate[:-1]
        if not safe_url(candidate):
            continue
        result.append(replace(span, text=span.text[cursor:match.start()]))
        result.append(replace(span, text=candidate, style=replace(span.style, link=candidate)))
        cursor = match.start() + len(candidate)
    result.append(replace(span, text=span.text[cursor:]))
    return [part for part in result if part.text]


def adapt_answer(parsed: dict, metadata: dict, limits: Limits, *, reading: bool = False,
                 image_urls: set[str] | None = None) -> HywDocument:
    """Adapt semantics; the live worker opts into the reading profile, not legacy parity."""
    image_urls = image_urls or set()
    counter = 0
    inline_count = 0
    references: dict[str, Reference] = {}
    titles = metadata.get('source_titles') or {}

    def unique(block: Block) -> Block:
        nonlocal counter, inline_count
        counter += 1
        if counter > limits.max_nodes:
            raise EngineError('Combined answer node budget exceeded')
        identifier = f'answer:{counter}'
        inlines = []
        for span in block.inlines:
            internal = span.kind == 'image' and is_display_url(span.target)
            link = span.target if internal else safe_url(span.target if span.kind == 'image' else span.style.link)
            # Only standalone pictures with reviewed bytes may become image blocks.
            # Unknown/data/file URLs and inline pictures never trigger network requests.
            if span.kind == 'image':
                if block.kind == 'gallery' and link in image_urls:
                    span = replace(span, target=link, style=replace(span.style, link='' if internal else link))
                else:
                    span = Inline(span.text or '图片链接', Style(link='' if internal else link))
            elif span.style.link:
                span = replace(span, style=replace(span.style, link=link))
            inlines.extend(linked_spans(span))
        for span in inlines:
            url = span.style.link
            if url and span.kind in ('text', 'image') and not span.style.code and url not in references:
                title = titles.get(source_key(url)) or urlsplit(url).hostname or url
                references[url] = Reference(title=title, url=url, number=len(references) + 1)
        inline_count += len(inlines)
        if inline_count > limits.max_nodes:
            raise EngineError('Combined answer inline budget exceeded')
        kind = ('paragraph' if block.kind == 'gallery' and not any(s.kind == 'image' for s in inlines)
                else block.kind)
        return replace(block, id=identifier, kind=kind, inlines=tuple(inlines),
                       children=tuple(unique(child) for child in block.children))

    blocks = []
    for part in parsed['parts']:
        # Text mode is still authored Markdown: parsing keeps `[label](url)` links whole and
        # still records bare URLs, instead of matching a URL across the rest of the line.
        children = parse(part['text'], limits, soft_breaks=True).children
        if part['kind'] == 'summary':
            children = (Block('', 'summary', children=children),)
        blocks.extend(unique(b) for b in children)

    # Do not add a second generic title when an authored H1 already exists.
    # Preamble trimming is owned by the protocol parser, never by AST promotion.
    title = '' if any(block.kind == 'heading' and block.level == 1 for block in blocks) else '回答'
    title_inlines = ()
    if (blocks and blocks[0].kind == 'heading' and blocks[0].level == 1
            and not any(span.style.citation for span in blocks[0].inlines)):
        # Only promote a genuinely leading title. Never drop content preceding a heading.
        first = blocks.pop(0)
        title = ''.join(span.text for span in first.inlines)
        title_inlines = first.inlines
    sections, pending = [], []
    for block in blocks:
        if block.kind in {'summary', 'code', 'table'}:
            if pending:
                sections.append(tuple(pending)); pending = []
            sections.append((block,))
        else:
            pending.append(block)
    if pending:
        sections.append(tuple(pending))
    # Runtime diagnostics belong in the web status/trace, never the exported answer.
    warning = '模型输出未完整' if metadata.get('truncated') else ''
    # The renderer indexes authored links; the model need not write a separate source list.
    # Keep inline links unchanged and never fetch remote icons or invent missing references.
    return HywDocument(title, tuple(sections), tuple(references.values()), (), '', '#ef4444', warning,
                       title_inlines=title_inlines, reading=reading)


def referenced_assets(document: HywDocument, images: dict, icons: dict) -> tuple[dict, dict]:
    """Select by the adapted AST, preserving exact URLs and reviewed image bytes."""
    from md2png.hyw.document import source_origin

    def targets(block):
        for span in block.inlines:
            if span.kind == 'image':
                yield span.target
        for child in block.children:
            yield from targets(child)

    used = {url for section in document.sections for block in section for url in targets(block)}
    used.update(document.gallery)
    origins = {source_origin(ref.url) for ref in document.references}
    return ({url: raw for url, raw in images.items() if url in used},
            {origin: raw for origin, raw in icons.items() if origin in origins})
