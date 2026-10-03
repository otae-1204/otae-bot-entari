"""Pure Python final-answer envelope parsing; no process, network or retained state.

Mask code examples, separate scoring, infer Markdown from block structure,
and discard preambles before the first heading.
"""
import re


def _mask_code(text: str) -> str:
    fence = None
    lines = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        masked = False
        if fence:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not line[match.end():].strip()):
                fence = None
            masked = True
        elif match:
            fence = match[1]
            masked = True
        elif re.match(r"^(?: {4}|\t| {0,3}>)", line):
            masked = True
        lines.append(re.sub(r"[^\n]", " ", line) if masked else line)
    return re.sub(r"(`+)([\s\S]*?)\1(?!`)|<!--[\s\S]*?(?:-->|$)",
                  lambda m: re.sub(r"[^\n]", " ", m[0]), "".join(lines))


def _strict(raw: str) -> dict:
    fallback = {"mode": "text", "valid": False, "scoring": None, "parts": [{"kind": "text", "text": raw}]}
    envelope = re.fullmatch(r"\s*<final_response>([\s\S]*)</final_response>\s*", raw, re.I)
    if not envelope:
        return fallback
    body = envelope[1].strip()
    masked = _mask_code(body)
    if re.search(r"</?final_response>", masked, re.I):
        return fallback
    spans, seen, pending = [], set(), None
    for match in re.finditer(r"<(/?)(summary|scoring)>", masked, re.I):
        name = match[2].lower()
        if not match[1]:
            if pending or name in seen:
                return fallback
            pending = (name, match.start(), match.end())
            seen.add(name)
        else:
            if pending is None or pending[0] != name:
                return fallback
            spans.append((*pending, match.start(), match.end()))
            pending = None
    if pending:
        return fallback
    mode = "markdown" if "summary" in seen else "text"
    parts, previous, scoring = [], 0, None
    for name, start, content_start, content_end, end in spans:
        text = body[previous:start].strip()
        if text:
            parts.append({"kind": mode, "text": text})
        content = body[content_start:content_end].strip()
        if name == "scoring":
            scoring = content
        else:
            parts.append({"kind": "summary", "text": content})
        previous = end
    tail = body[previous:].strip()
    if tail:
        parts.append({"kind": mode, "text": tail})
    return {"mode": mode, "valid": True, "scoring": scoring, "parts": parts}


def _markdown(text: str) -> bool:
    visible = _mask_code(text)
    return any(re.search(pattern, visible, re.M) for pattern in (
        r"^ {0,3}#{1,6}[ \t]+\S",
        r"(?:^|\n) {0,3}(?:[-+*]|\d+[.)])[ \t]+\S[^\n]*\n {0,3}(?:[-+*]|\d+[.)])[ \t]+\S",
        r"^[^\n]*\|[^\n]*\n[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*\|[| :\t-]*$",
    ))


def _tokens(text: str):
    return [{"start": m.start(), "end": m.end(), "name": m[2].lower(), "closing": bool(m[1])}
            for m in re.finditer(r"<(/?)(final_response|summary|scoring)>", _mask_code(text), re.I)
            if not m.start() or text[m.start() - 1] != "\\"]


def _parse_answer(raw: str) -> dict:
    exact = _strict(raw)

    def missing_summary(answer):
        return (answer["valid"] and answer["mode"] == "text"
                and _markdown("\n\n".join(p["text"] for p in answer["parts"])))

    if exact["valid"] and not missing_summary(exact):
        return {**exact, "recovered": False, "repairs": []}
    repairs = dict()
    text = re.sub(r"\r\n?", "\n", raw)
    fenced = re.fullmatch(r"\s*(`{3,}|~{3,})(?:xml|html|markdown|md)?[ \t]*\n([\s\S]*?)\n\1\s*", text, re.I)
    if (fenced and re.search(r"^\s*(?:<|&lt;)\s*final_response\s*(?:>|&gt;)", fenced[2], re.I)
            and re.search(r"(?:<|&lt;)\s*/\s*final_response\s*(?:>|&gt;)\s*$", fenced[2], re.I)):
        text = fenced[2]
        repairs["outer_fence_removed"] = None
    mask = _mask_code(text)

    def normalize(match):
        if not mask[match.start():match.end()].strip() or (match.start() and text[match.start() - 1] == "\\"):
            return match[0]
        tag = f"<{match[1]}{match[2].lower()}>"
        if tag != match[0]:
            repairs["tag_normalized"] = None
        return tag

    text = re.sub(r"(?:<|&lt;)[ \t]*(/?)[ \t]*(final_response|summary|scoring)[ \t]*(?:>|&gt;)", normalize, text, flags=re.I)
    normalized = _strict(text)
    if normalized["valid"] and not missing_summary(normalized):
        if not repairs:
            repairs["line_endings_normalized"] = None
        return {**normalized, "valid": False, "recovered": True, "repairs": list(repairs)}
    tokens = _tokens(text)
    envelope = any(t["name"] == "final_response" and (
        not text[:t["start"]].strip() or not text[t["end"]:].strip()
        or not text[text.rfind("\n", 0, t["start"]) + 1:t["start"]].strip()) for t in tokens)
    summary = any(t["name"] == "summary" and not t["closing"]
                  and not text[text.rfind("\n", 0, t["start"]) + 1:t["start"]].strip() for t in tokens)
    if not envelope and not summary and not _markdown(text):
        return {**exact, "recovered": False, "repairs": []}
    repairs["envelope_recovered"] = None
    previous, fragments = 0, []
    for tag in tokens:
        if tag["name"] == "summary":
            continue
        fragments.extend((text[previous:tag["start"]], "\n\n"))
        previous = tag["end"]
    fragments.append(text[previous:])
    text = "".join(fragments)
    summaries = [t for t in _tokens(text) if t["name"] == "summary"]
    mode = "markdown" if summaries or _markdown(text) else "text"
    if not summaries and mode == "markdown":
        repairs["markdown_inferred"] = None
    if summaries:
        repairs["summary_recovered"] = None
    parts, in_summary, bounded = [], False, False

    def push(kind, content):
        if content.strip():
            parts.append({"kind": kind, "text": content.strip()})

    def append(content):
        nonlocal in_summary
        if not in_summary:
            return push(mode, content)
        boundary = None if bounded else re.search(r"\n[ \t]*\n|\n(?= {0,3}#{1,6}[ \t])", content)
        if boundary is None:
            return push("summary", content)
        push("summary", content[:boundary.start()])
        push(mode, content[boundary.start():])
        in_summary = False

    previous = 0
    for index, tag in enumerate(summaries):
        append(text[previous:tag["start"]])
        in_summary = not tag["closing"]
        bounded = in_summary and index + 1 < len(summaries) and summaries[index + 1]["closing"]
        previous = tag["end"]
    append(text[previous:])
    return {"mode": mode, "valid": False, "recovered": True, "repairs": list(repairs), "scoring": None, "parts": parts}


def parse_answer(raw: str) -> dict:
    """Trim presentation only; callers retain raw output for history and diagnostics."""
    answer = _parse_answer(raw)
    if answer['mode'] != 'markdown':
        return answer
    for index, part in enumerate(answer['parts']):
        if part['kind'] != 'markdown':
            continue
        heading = re.search(r'^ {0,3}#{1,6}[ \t]+\S', _mask_code(part['text']), re.M)
        if heading is None:
            continue
        if index == 0 and not part['text'][:heading.start()].strip():
            return answer
        parts = [{**part, 'text': part['text'][heading.start():]}, *answer['parts'][index + 1:]]
        return {**answer, 'parts': parts, 'valid': False, 'recovered': True,
                'repairs': [*answer['repairs'], 'preamble_removed']}
    return answer


def answer_text(parsed: dict) -> str:
    """Display body without protocol envelopes/scoring; keep literal text literal."""
    return '\n\n'.join(part['text'] for part in parsed['parts'])


class AnswerParser:
    """Compatibility facade for the renderer; parsing is now stateless and in-process."""

    def parse(self, answer: str) -> dict:
        return parse_answer(answer)

    def close(self):
        pass
