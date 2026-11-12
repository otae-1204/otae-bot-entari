"""Expand QQ share cards, XML cards and merged forwards into text and image URLs.

Only the triggering message and its quote are walked, and nothing is fetched: a
merged forward is read from the ``<message forward>`` children the event carries
(the boundary expands them only inside a quote). A forward without children stays
unread. Cards are the extension elements ``onebot:json`` / ``onebot:xml`` (and
LLBot's ``llonebot:ark``); none of them has to be present.
"""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree

_TEXT = {
    "title": "标题", "desc": "描述", "description": "描述", "summary": "摘要",
    "content": "内容", "prompt": "卡片摘要", "tag": "来源", "name": "名称",
    "artist": "歌手", "singer": "歌手", "author": "作者",
}
_LINKS = {
    "qqdocurl": "内容链接", "jumpurl": "内容链接", "url": "链接", "href": "链接",
    "musicurl": "音频链接（未下载/播放）", "audiourl": "音频链接（未下载/播放）",
    "videourl": "视频链接（未下载/播放）",
}
_IMAGES = {"preview", "cover", "coverurl", "image", "imageurl", "pic", "picurl", "thumb", "thumbnail"}
_SKIP = {"config", "extra", "host", "token", "secret", "password"}
_MAX_CARD = 256 * 1024
_MAX_DEPTH = 8
_MAX_MESSAGES = 2000
_MAX_PARTS = 16000
_MAX_TEXT = 32_000
_MAX_IMAGES = 20


class Expanded:
    def __init__(self):
        self.lines: list[str] = []
        self.images: list[str] = []
        self.rich = False
        self._text_bytes = 0
        self._parts = 0
        self._messages = 0
        self._truncated = False
        self._seen: set[str] = set()

    def add_text(self, value: str):
        if self._truncated or not value:
            return
        if self._parts >= _MAX_PARTS:
            self._stop("内容块数量达到上限")
            return
        remaining = _MAX_TEXT - self._text_bytes
        if remaining <= 0:
            self._stop("展开文本达到上限")
            return
        raw = value.encode("utf-8", errors="replace")
        if len(raw) > remaining:
            value = raw[:remaining].decode("utf-8", errors="ignore")
            self._stop("展开文本达到上限")
        if value:
            self.lines.append(value)
            self._text_bytes += len(value.encode("utf-8"))
            self._parts += 1

    def add_image(self, url: str):
        if self._truncated or not url or url in self.images:
            return
        if len(self.images) >= _MAX_IMAGES:
            self._stop("展开图片达到 20 张上限")
            return
        self.images.append(url)
        self.rich = True

    def _stop(self, reason: str):
        if not self._truncated:
            self._truncated = True
            self.lines.append(f"[{reason}，其余未展开]")


def _public_http(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("m.q.qq.com/"):
        value = "https://" + value
    if not value.startswith(("http://", "https://")) or len(value) > 2000:
        return None
    return value


def _walk_card(value, output: Expanded, depth: int = 0):
    if output._truncated or depth > 6:
        return
    if isinstance(value, list):
        for child in value[:128]:
            _walk_card(child, output, depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        name = str(key).lower()
        if name in _SKIP:
            continue
        if name in _TEXT and isinstance(item, str) and item.strip():
            line = f"{_TEXT[name]}：{item.strip()[:300]}"
            if line not in output._seen:
                output._seen.add(line)
                output.add_text(line + "\n")
        elif name in _LINKS:
            url = _public_http(item)
            if url and url not in output._seen:
                output._seen.add(url)
                output.add_text(f"{_LINKS[name]}：{url}\n")
        elif name in _IMAGES:
            url = _public_http(item)
            if url and url not in output._seen:
                output._seen.add(url)
                output.add_text(f"封面/图片链接：{url}\n")
                output.add_image(url)
        elif isinstance(item, (dict, list)):
            _walk_card(item, output, depth + 1)


def _json_card(raw: str, output: Expanded):
    output.rich = True
    output.add_text("\n【分享组件】\n")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CARD:
        output.add_text("[卡片数据为空或超过 256 KiB，未展开]\n")
        return
    try:
        card = json.loads(raw)
    except (ValueError, RecursionError):
        output.add_text("[卡片 JSON 格式无效，未展开]\n")
        return
    if not isinstance(card, dict):
        output.add_text("[卡片结构无效，未展开]\n")
        return
    before = len(output.lines)
    _walk_card(card, output)
    if len(output.lines) == before:
        output.add_text("[未发现可读标题、描述、公开链接或图片；不执行小程序]\n")
    output.add_text("【分享组件结束】\n")


def _xml_card(raw: str, output: Expanded):
    output.rich = True
    output.add_text("\n【XML 分享组件】\n")
    if (not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CARD
            or re.search(r"<\s*!\s*(?:DOCTYPE|ENTITY)", raw, re.I)):
        output.add_text("[XML 过大或包含不允许的实体声明，未展开]\n")
        return
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError, RecursionError):
        output.add_text("[XML 格式无效，未展开]\n")
        return
    for index, node in enumerate(root.iter(), 1):
        if index > 512:
            output.add_text("[XML 节点超过 512 个，剩余未展开]\n")
            break
        if node.tag.lower() in {"image", "img", "picture"}:
            _walk_card({"preview": node.get("src") or node.get("url") or node.get("cover")}, output)
        _walk_card(dict(node.attrib), output)
        if node.tag.lower() in _TEXT and node.text and node.text.strip():
            _walk_card({node.tag: node.text}, output)
    output.add_text("【XML 分享组件结束】\n")


# Extension elements carrying a QQ card in ``data``: tag -> card kind.
CARD_TAGS = {"onebot:json": "json", "llonebot:ark": "json", "onebot:xml": "xml"}


def _segment(element):
    tag = str(getattr(element, "tag", "") or "")
    attrs = {}
    raw = getattr(element, "raw", None)
    if callable(raw):
        try:
            attrs = dict(raw().attrs)
        except Exception:
            attrs = {}
    if not attrs:
        extra = getattr(element, "attrs", None)
        if isinstance(extra, dict):
            attrs = dict(extra)
    if tag == "text":
        attrs = {"text": getattr(element, "text", None) or attrs.get("text") or ""}
    elif tag in {"img", "image"}:
        tag = "image"
        attrs = {"url": getattr(element, "src", None) or attrs.get("url") or attrs.get("src") or ""}
    elif tag in {"message", "author"}:
        # Elements built in code keep these as fields; only parsed ones fill attrs.
        attrs = {**attrs, **{key: getattr(element, key) for key in ("id", "forward", "name")
                             if getattr(element, key, None) is not None}}
    children = list(getattr(element, "children", None) or [])
    return tag, attrs, children


def _flag(value) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def unread_forward(elements) -> bool:
    """Whether these elements are a merged forward that arrived without its messages."""
    for element in elements or ():
        tag, attrs, children = _segment(element)
        if tag == "message" and _flag(attrs.get("forward")) and not children:
            return True
    return False


class QuoteUnavailable(Exception):
    """The quoted message could not be read; ``reason`` is the one-line cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Parser:
    def __init__(self):
        self.output = Expanded()

    def feed(self, elements, depth: int = 0, *, nested: bool = False):
        if isinstance(elements, str):
            self.output.add_text(elements)
            return
        if elements is None:
            return
        try:
            items = list(elements)
        except TypeError:
            items = [elements]
        for segment in items:
            if self.output._truncated:
                return
            tag, data, children = _segment(segment)
            if tag == "text":
                if nested:
                    self.output.add_text(str(data.get("text", "")))
            elif tag == "image":
                if nested:
                    url = str(data.get("url") or data.get("src") or data.get("file") or "")
                    if url:
                        self.output.add_image(url)
            elif CARD_TAGS.get(tag) == "json":
                _json_card(str(data.get("data", "")), self.output)
            elif CARD_TAGS.get(tag) == "xml":
                _xml_card(str(data.get("data", "")), self.output)
            elif tag == "message" and _flag(data.get("forward")):
                if children:
                    self._inline_forward(children, depth)
                else:
                    self.output.rich = True
                    self.output.add_text("[聊天记录读取失败或格式无效，未展开]\n")
            elif children and depth < _MAX_DEPTH and tag not in {"at", "author", "quote", "reply"}:
                self.feed(children, depth + 1, nested=nested)

    def _inline_forward(self, nodes, depth: int):
        """Satori ``<message forward>``: each child ``<message>`` is one record, ``<author>`` its sender."""
        self.output.rich = True
        if depth >= _MAX_DEPTH:
            self.output.add_text("[聊天记录嵌套过深，未展开]\n")
            return
        self.output.add_text("\n【聊天记录 开始；按原始顺序】\n")
        for index, node in enumerate(nodes, 1):
            if self.output._truncated:
                return
            if self.output._messages >= _MAX_MESSAGES:
                self.output._stop("展开消息数量达到 2000 条上限")
                return
            self.output._messages += 1
            tag, _, children = _segment(node)
            content = children if tag == "message" else [node]
            author = next((attrs for kind, attrs, _ in map(_segment, content) if kind == "author"), {})
            name = str(author.get("name") or "未知发送者")[:200]
            uid = str(author.get("id") or "未知")[:100]
            self.output.add_text(f"\n【消息 {index}】\n发送者：{name}（{uid}）\n")
            self.feed(content, depth + 1, nested=True)
        self.output.add_text("\n【聊天记录 结束】\n")


def expand_special(elements) -> tuple[str, list[str], bool]:
    """Return extra text, image URLs, and whether a card or forward was expanded."""
    parser = _Parser()
    parser.feed(elements)
    return "".join(parser.output.lines).strip(), parser.output.images, parser.output.rich
