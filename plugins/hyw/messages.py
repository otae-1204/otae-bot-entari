"""Expand QQ share cards, XML cards and merged forwards into text and image URLs.

Only the triggering message and its quote are walked. Forward ids are fetched
for that message; the channel history is not scanned.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
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
        self._active: set[str] = set()

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


def _segment(element):
    if isinstance(element, dict):
        data = element.get("data") if isinstance(element.get("data"), dict) else {}
        return str(element.get("type", "")).removeprefix("onebot:"), data, element.get("children") or []
    tag = str(getattr(element, "tag", "") or "").removeprefix("onebot:")
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
    children = list(getattr(element, "children", None) or [])
    return tag, attrs, children


async def _load_forward(session, forward_id: str):
    async def call(method):
        return await asyncio.wait_for(method, 20)

    if session is not None and hasattr(session, "internal"):
        try:
            return await call(session.internal("get_forward_msg", message_id=forward_id))
        except Exception:
            pass
    account = getattr(session, "account", None)
    if account is None:
        return None
    try:
        from otae_bot.adapters.onebot import call_onebot_action
        return await call(call_onebot_action(account, "get_forward_msg", message_id=forward_id))
    except Exception:
        return None


def _payload_messages(payload):
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    if isinstance(payload.get("messages"), list):
        return payload["messages"]
    return None


class _Parser:
    def __init__(self, session):
        self.session = session
        self.output = Expanded()

    async def feed(self, elements, depth: int = 0, *, nested: bool = False):
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
            elif tag == "json":
                _json_card(str(data.get("data", "")), self.output)
            elif tag == "xml":
                _xml_card(str(data.get("data", "")), self.output)
            elif tag == "forward":
                await self._forward(str(data.get("id", "")), depth)
            elif tag == "node" and depth < _MAX_DEPTH:
                name = str(data.get("name") or "未知")[:200]
                self.output.add_text(f"\n【转发节点】发送者：{name}\n")
                await self.feed(data.get("content") or children, depth + 1, nested=True)
            elif children and depth < _MAX_DEPTH and tag not in {"at", "author", "quote", "reply"}:
                await self.feed(children, depth + 1, nested=nested)

    async def _forward(self, forward_id: str, depth: int):
        self.output.rich = True
        if depth >= _MAX_DEPTH or not forward_id or forward_id in self.output._active or len(forward_id) > 256:
            self.output.add_text("[聊天记录嵌套过深、循环引用或缺少标识，未展开]\n")
            return
        self.output.add_text("\n【聊天记录 开始；按原始顺序】\n")
        self.output._active.add(forward_id)
        try:
            payload = await _load_forward(self.session, forward_id)
            messages = _payload_messages(payload)
            if not isinstance(messages, list):
                self.output.add_text("[聊天记录读取失败或格式无效，未展开]\n")
                return
            for index, message in enumerate(messages, 1):
                if self.output._truncated:
                    return
                if self.output._messages >= _MAX_MESSAGES:
                    self.output._stop("展开消息数量达到 2000 条上限")
                    return
                self.output._messages += 1
                if not isinstance(message, dict):
                    self.output.add_text(f"【消息 {index}】[消息格式无效]\n")
                    continue
                sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
                name = str(sender.get("nickname") or message.get("name") or "未知发送者")[:200]
                uid = str(sender.get("user_id", message.get("uin", "未知")))[:100]
                stamp = message.get("time")
                try:
                    when = datetime.fromtimestamp(float(stamp), timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    when = "未知时间"
                self.output.add_text(f"\n【消息 {index}】\n发送者：{name}（{uid}）\n时间：{when}\n")
                await self.feed(message.get("message", message.get("content", [])), depth + 1, nested=True)
            self.output.add_text("\n【聊天记录 结束】\n")
        finally:
            self.output._active.discard(forward_id)


async def expand_special(session, elements) -> tuple[str, list[str], bool]:
    """Return extra text, image URLs, and whether a card or forward was expanded."""
    parser = _Parser(session)
    await parser.feed(elements)
    return "".join(parser.output.lines).strip(), parser.output.images, parser.output.rich
