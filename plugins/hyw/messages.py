"""Expand QQ share cards, XML cards and merged forwards into text and image URLs.

Only the triggering message and its quote are walked. Forward ids are fetched
for that message; the channel history is not scanned beyond locating the quoted
message itself. A Satori ``<message forward>`` with child messages is read in
place; one that only carries an id, and a OneBot ``forward`` segment, go through
``get_forward_msg``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from xml.etree import ElementTree

from otae_bot.adapters import onebot

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
    elif tag in {"message", "author"}:
        # Elements built in code keep these as fields; only parsed ones fill attrs.
        attrs = {**attrs, **{key: getattr(element, key) for key in ("id", "forward", "name")
                             if getattr(element, key, None) is not None}}
    children = list(getattr(element, "children", None) or [])
    return tag, attrs, children


def _flag(value) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _note(failures: list[str] | None, step: str, error: BaseException):
    if failures is not None:
        detail = " ".join(str(error).split())[:300]
        failures.append(f"{step}: {type(error).__name__}" + (f": {detail}" if detail else ""))


class QuoteUnavailable(Exception):
    """The quoted message could not be read; ``reason`` is the one-line cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _call(session, action: str, **params):
    """Run a OneBot action as the account the event came in on."""
    account = getattr(session, "account", None)
    if account is None:
        raise onebot.OneBotUnavailable("事件没有账号")
    return await asyncio.wait_for(onebot.call_account_action(account, action, **params), 20)


async def _load(session, action: str, failures: list[str] | None = None, **params):
    try:
        return await _call(session, action, **params)
    except Exception as error:
        _note(failures, action, error)
        return None


async def _load_forward(session, forward_id: str, failures: list[str] | None = None):
    # LLBot reads a forward by its resid only; message ids answer "unexpected end of file".
    return await _load(session, "get_forward_msg", failures, id=forward_id)


def _payload_segments(payload):
    """Segments of a OneBot message, unwrapped from ``data`` if needed."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}] if message else None
    return message if isinstance(message, list) and message else None


def _payload_messages(payload):
    """``messages`` of a forward or history answer; ``data`` may also be the list itself."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    if isinstance(payload.get("messages"), list):
        return payload["messages"]
    return None


_SATORI_ID = re.compile(r"([0-9]{1,3})\|([^|\s]{1,64})\|([0-9]{1,20})")
_HISTORY_COUNT = 10
_GROUP, _FRIEND = 2, 1


def parse_satori_id(value) -> tuple[int, str, int] | None:
    """(chatType, peerUid, msgSeq) of an LLBot Satori message id such as ``2|875241970|3969610``.

    chatType 2 is a group (peerUid is the group number), 1 a private chat. Anything else
    in that shape is returned as is; a different shape gives None.
    """
    match = _SATORI_ID.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return int(match[1]), match[2], int(match[3])


def _digits(value) -> str:
    value = str(value or "")
    return value if value.isascii() and value.isdigit() else ""


def _friend_of(session, peer: str) -> str:
    """QQ number of the other side of a private chat; LLBot's peerUid there is usually a ``u_`` uid."""
    if _digits(peer):
        return peer
    event = getattr(session, "event", None)
    channel = str(getattr(getattr(event, "channel", None), "id", "") or "")
    if _digits(channel.removeprefix("private:")):
        return channel.removeprefix("private:")
    return _digits(getattr(getattr(event, "user", None), "id", ""))


async def _locate(session, chat_type: int, peer: str, seq: int, failures: list[str] | None) -> dict:
    """The quoted message from the receiving account's own history, matched on ``message_seq``.

    OneBot message ids hash a random number and differ per account, so they cannot be
    derived from the Satori id; the history around ``seq`` has the message with its id.
    """
    if chat_type == _GROUP and _digits(peer):
        action, target, where = "get_group_msg_history", {"group_id": int(peer)}, f"收到事件的号可能不在群 {peer}"
    elif chat_type == _FRIEND and (friend := _friend_of(session, peer)):
        action, target, where = "get_friend_msg_history", {"user_id": int(friend)}, f"读不到与 {friend} 的私聊历史"
    else:
        raise QuoteUnavailable(f"引用 id 的会话无法识别：chatType={chat_type}")
    try:
        payload = await _call(session, action, **target, message_seq=seq, count=_HISTORY_COUNT)
    except onebot.OneBotUnavailable as error:
        _note(failures, action, error)
        if error.answered:
            raise QuoteUnavailable(f"{action} 报错，{where}") from None
        raise QuoteUnavailable(f"通道不可用，{action} 没有送达") from None
    except Exception as error:
        _note(failures, action, error)
        raise QuoteUnavailable(f"{action} 调用失败：{type(error).__name__}") from None
    messages = _payload_messages(payload)
    if messages is None:
        raise QuoteUnavailable(f"{action} 返回格式无效")
    for message in messages:
        if isinstance(message, dict) and str(message.get("message_seq")) == str(seq):
            return message
    raise QuoteUnavailable(f"{action} 返回 {len(messages)} 条，没有 message_seq={seq}")


class _Parser:
    def __init__(self, session, failures: list[str] | None = None):
        self.session = session
        self.output = Expanded()
        self.failures = failures

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
            elif tag == "message" and _flag(data.get("forward")):
                if children:
                    await self._inline_forward(children, depth)
                else:
                    await self._forward(str(data.get("id") or ""), depth)
            elif tag == "node" and depth < _MAX_DEPTH:
                name = str(data.get("name") or "未知")[:200]
                self.output.add_text(f"\n【转发节点】发送者：{name}\n")
                await self.feed(data.get("content") or children, depth + 1, nested=True)
            elif children and depth < _MAX_DEPTH and tag not in {"at", "author", "quote", "reply"}:
                await self.feed(children, depth + 1, nested=nested)

    async def _inline_forward(self, nodes, depth: int):
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
            await self.feed(content, depth + 1, nested=True)
        self.output.add_text("\n【聊天记录 结束】\n")

    async def _forward(self, forward_id: str, depth: int, payload=None):
        self.output.rich = True
        if depth >= _MAX_DEPTH or not forward_id or forward_id in self.output._active or len(forward_id) > 256:
            self.output.add_text("[聊天记录嵌套过深、循环引用或缺少标识，未展开]\n")
            return
        self.output.add_text("\n【聊天记录 开始；按原始顺序】\n")
        self.output._active.add(forward_id)
        try:
            if payload is None:
                payload = await _load_forward(self.session, forward_id, self.failures)
            messages = _payload_messages(payload)
            if not isinstance(messages, list):
                if self.failures is not None:
                    self.failures.append(f"get_forward_msg {forward_id[:40]}: no message list")
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


async def expand_quote(session, message_id: str, failures: list[str] | None = None) -> tuple[str, list[str], bool]:
    """Read a quoted message by its Satori id without Satori ``message.get``.

    LLBot's Satori decoder drops merged forwards, so ``message.get`` answers 500 "消息为空"
    and the quote carries no resid. The id is ``chatType|peerUid|msgSeq``: the receiving
    account's OneBot history finds the message by seq, a ``forward`` segment is expanded
    with ``get_forward_msg`` by its resid, anything else is read from the message itself.
    Raises ``QuoteUnavailable`` when nothing could be read; failed calls are appended to
    ``failures``.
    """
    parsed = parse_satori_id(message_id)
    if parsed is None:
        raise QuoteUnavailable("引用 id 不是 chatType|peerUid|msgSeq 格式")
    message = await _locate(session, *parsed, failures)
    segments = _payload_segments(message) or []
    parser = _Parser(session, failures)
    forward = next((data for tag, data, _ in map(_segment, segments) if tag == "forward"), None)
    if forward is not None:
        resid = str(forward.get("id") or "")
        payload = await _load_forward(session, resid, failures) if resid else None
        if _payload_messages(payload) is None:
            raise QuoteUnavailable(f"合并转发展开失败（get_forward_msg {resid[:40] or '缺少 resid'}）")
        await parser._forward(resid, 0, payload)
    else:
        await parser.feed(segments, nested=True)
    text = "".join(parser.output.lines).strip()
    if not text and not parser.output.images:
        raise QuoteUnavailable("引用的消息里没有可读的文字、图片或转发")
    return text, parser.output.images, parser.output.rich
