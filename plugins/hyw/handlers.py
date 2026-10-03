from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
from io import BytesIO
from pathlib import Path

import httpx
from arclet.alconna import Alconna, AllParam, Args, Arparma
from arclet.entari import MessageChain, Session, command
from loguru import logger
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from otae_bot.adapters.entari import make_image
from otae_bot.infrastructure.http.tls import shared_ssl_context

from .config import HywConfig, HywError
from .history import HistoryStore, Scope, SourceBook
from .messages import expand_quote, expand_special

HELP = """HYW / 何意味
/q 问题：搜索问答，也可附带图片（最多 4 张）。
引用一条消息后发送 /q 问题：分析引用内容。
引用自己的 HYW 回答后发送 /q 追问：继续对话，有效期 1 小时。
/q 清空：清除自己在当前会话中的历史和来源记录。
/qstop：取消自己在当前会话中正在进行的问答。
/link：回复一条 HYW 回答后再发送，才会取回该次来源。
别名：/hyw、/何意味、/qlink。
输入和引用内容会发送给配置的模型；搜索词会发送给所选搜索服务；读网页时网址会发给 Jina。"""
history_store = HistoryStore()
sources = SourceBook()
_active: dict[Scope, int] = {}
_inflight: dict[Scope, set[asyncio.Task]] = {}


def scope_for(session: Session) -> Scope:
    event = session.event
    return (
        str(session.account.platform), str(session.account.self_id),
        str(getattr(getattr(event, "guild", None), "id", "")),
        str(getattr(getattr(event, "channel", None), "id", "")),
        str(event.user.id),
    )


def channel_for(scope: Scope) -> tuple:
    return scope[:4]


def parts_from(elements) -> tuple[str, list[str]]:
    from satori import Image, Text

    text, images = [], []
    for element in elements:
        if isinstance(element, (Text, str)):
            text.append(element.text if isinstance(element, Text) else element)
        elif isinstance(element, Image):
            images.append(element.src)
        else:
            snippet = _element_text(element)
            if snippet:
                text.append(snippet)
    return " ".join(text).strip(), images


_CARD_TEXT = {"title": "标题", "desc": "描述", "description": "描述", "summary": "摘要", "prompt": "卡片摘要", "name": "名称"}
_CARD_LINK = {"qqdocurl": "内容链接", "jumpurl": "内容链接", "url": "链接", "href": "链接"}


def _card_lines(value, seen: set[str]) -> list[str]:
    lines = []
    if isinstance(value, list):
        for child in value[:32]:
            lines.extend(_card_lines(child, seen))
        return lines
    if not isinstance(value, dict):
        return lines
    for key, item in value.items():
        name = str(key).lower()
        if name in {"config", "extra", "host", "token", "secret", "password"}:
            continue
        if name in _CARD_TEXT and isinstance(item, str) and item.strip():
            line = f"{_CARD_TEXT[name]}：{item.strip()[:300]}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        elif name in _CARD_LINK and isinstance(item, str) and item.startswith(("http://", "https://")):
            line = f"{_CARD_LINK[name]}：{item.strip()[:500]}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        elif isinstance(item, (dict, list)):
            lines.extend(_card_lines(item, seen))
    return lines


def _element_text(element) -> str:
    tag = str(getattr(element, "tag", "") or "").removeprefix("onebot:")
    if tag not in {"json", "xml"}:
        return ""
    raw = getattr(element, "attrs", None) or {}
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, str) or len(data) > 256 * 1024:
        return ""
    if tag == "json":
        try:
            card = json.loads(data)
        except (ValueError, RecursionError):
            return ""
        lines = _card_lines(card, set())
        return "\n".join(lines[:40])
    return ""


_SECRET_NAMES = (
    r"authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-goog-api-key|api[-_]?key|key"
    r"|access[-_]?token|refresh[-_]?token|id[-_]?token|token|client[-_]?secret|secret|password"
    r"|private[-_]?key|signature|sig"
)
_REDACTIONS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "<private key>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/=]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)(https?://)[^\s/@'\"]+@"), r"\1<redacted>@"),
    (re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s#'\"<>]*"), r"\1?<redacted>"),
    (re.compile(rf"(?i)([\"']?\b(?:{_SECRET_NAMES})\b[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}}]+"), r"\1<redacted>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "<redacted>"),
    (re.compile(r"\bya29\.[0-9A-Za-z._-]+"), "<redacted>"),
    (re.compile(r"\beyJ[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+"), "<redacted>"),
)


def _redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<redacted>")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _message(error: BaseException) -> str:
    try:
        return " ".join(str(error).split())
    except Exception:  # noqa: BLE001 - a broken __str__ must not hide the original failure
        return "<unprintable>"


def _status_value(value) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def failure_summary(error: BaseException, secrets: tuple[str, ...] = ()) -> str:
    """One log line: error code, HTTP status and the redacted exception chain.

    ``raise ... from None`` hides the cause from tracebacks but keeps it in
    ``__context__``; that hidden SDK error is usually the useful part.
    """
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain) and len(chain) < 6:
        chain.append(current)
        current = current.__cause__ or current.__context__
    code = status = retryable = None
    for item in chain:
        diagnostics = getattr(item, "diagnostics", None)
        if isinstance(diagnostics, dict):
            code = code or diagnostics.get("code")
            status = status or _status_value(diagnostics.get("http_status"))
            if retryable is None and isinstance(diagnostics.get("retryable"), bool):
                retryable = diagnostics["retryable"]
        value = getattr(item, "code", None)
        if code is None and isinstance(value, (str, int)) and not isinstance(value, bool):
            code = value
        status = status or _status_value(getattr(item, "status_code", None))
        status = status or _status_value(getattr(getattr(item, "response", None), "status_code", None))
        status = status or _status_value(value)
    links = " <- ".join(
        f"{type(item).__name__}: {_redact(_message(item), secrets)[:300]}".rstrip(": ") for item in chain
    )
    parts = [f"code={code if code is not None else '-'}", f"http_status={status or '-'}"]
    if retryable is not None:
        parts.append(f"retryable={retryable}")
    return " ".join(parts) + f" chain=[{links}]"


def _config_secrets(config: HywConfig | None) -> tuple[str, ...]:
    if config is None:
        return ()
    return tuple(value for value in (config.api_key, config.proxy, config.search_proxy) if value)


def _jpeg(data: bytes) -> str:
    try:
        with PILImage.open(BytesIO(data)) as original:
            if original.width * original.height > 20_000_000:
                raise HywError("图片像素过大，请压缩后再发送。")
            image = ImageOps.exif_transpose(original)
            image.thumbnail((1280, 1280))
            rgba = image.convert("RGBA")
            background = PILImage.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            buffer = BytesIO()
            background.save(buffer, format="JPEG", quality=85, subsampling=0)
            encoded = base64.b64encode(buffer.getvalue()).decode()
    except HywError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError):
        raise HywError("无法读取图片，请使用 PNG、JPEG 或 WebP 图片。") from None
    if len(encoded) > 7_000_000:
        raise HywError("压缩后的图片仍然过大。")
    return encoded


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    if url.startswith("data:image/"):
        try:
            header, encoded = url.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError("invalid data URI")
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise HywError("图片编码无效。") from None
    else:
        async with client.stream("GET", url, follow_redirects=True, timeout=20) as response:
            if response.status_code != 200:
                raise HywError(f"图片下载失败（HTTP {response.status_code}）。")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 20_000_000:
                    raise HywError("单张原图不能超过 20 MB。")
    if len(data) > 20_000_000:
        raise HywError("单张原图不能超过 20 MB。")
    return bytes(data)


async def _model_images(config: HywConfig, images: list[str]) -> list[dict]:
    if len(images) > 20:
        raise HywError("展开后的图片不能超过 20 张。")
    if not images:
        return []
    prepared = []
    async with httpx.AsyncClient(
        proxy=config.proxy or None, trust_env=False, verify=shared_ssl_context(),
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for url in images:
            prepared.append({"mimeType": "image/jpeg", "data": _jpeg(await _download(client, url))})
    return prepared


def _continuable(messages: list[dict]) -> list[dict]:
    kept = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        kept.append({"role": role, "content": str(content)[:6000]})
    return kept[-12:]


def _listed_sources(answer) -> list[dict]:
    from hyw_frontier.source_titles import source_key, source_titles

    titles = source_titles(answer.messages)
    return [{"title": titles.get(source_key(url)) or url, "url": url} for url in dict.fromkeys(answer.links)]


def _png_jpeg(png: bytes) -> bytes:
    with PILImage.open(BytesIO(png)) as image:
        frame = image.convert("RGB")
        buffer = BytesIO()
        frame.save(buffer, format="JPEG", quality=85, subsampling=0)
        return buffer.getvalue()


async def send_text(session: Session, text: str) -> list:
    from satori import Text

    receipts = []
    for start in range(0, len(text), 2000):
        receipts.extend(await session.send(MessageChain([Text(text[start:start + 2000])]), reply_to=True) or [])
    return receipts


def _remember(scope: Scope, receipts: list, history: list[dict], listed: list[dict]):
    channel, sender = channel_for(scope), scope[4]
    for receipt in receipts:
        message_id = str(receipt.id)
        history_store.put(scope, message_id, history)
        if listed:
            sources.put(channel, sender, message_id, listed)


def _google_home(config: HywConfig) -> Path:
    home = Path(config.home).expanduser()
    if not home.is_absolute():
        home = Path.cwd() / home
    home.mkdir(parents=True, exist_ok=True)
    try:
        account = json.loads(Path(config.credentials_file).expanduser().read_text(encoding="utf-8"))
        if not isinstance(account, dict) or account.get("type") != "service_account":
            raise ValueError("type")
        (home / "auth.json").write_text(json.dumps({"google": account}, ensure_ascii=False), encoding="utf-8")
        (home / "models.json").write_text(json.dumps({
            "google": {"model": config.model, "label": "Gemini", "location": config.vertex_location},
        }, ensure_ascii=False), encoding="utf-8")
    except HywError:
        raise
    except Exception:
        raise HywError("服务账号 JSON 无法交给 Vertex。请管理员检查 HYW_CREDENTIALS_FILE。") from None
    return home


async def _prepared_blocks(config: HywConfig, text: str, images: list[str]) -> list[dict]:
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    encoded = await _model_images(config, images)
    blocks.extend({"type": "image", "mimeType": item["mimeType"], "data": item["data"]} for item in encoded)
    return blocks


async def run_request(session: Session, config: HywConfig, scope: Scope, text: str, images: list[str], prior: list[dict], *, rich: bool = False):
    try:
        from hyw_frontier import answer
        from hyw_frontier.errors import FrontierError
        from hyw_frontier.rendering import RenderError
    except ImportError:
        raise HywError("HYW 核心库未安装，请管理员安装 hyw-frontier 与 md2png。") from None
    if len(text) > 32_000:
        raise HywError("问题和引用内容合计不能超过 32000 字。")
    if not rich and len(images) > 4:
        raise HywError("每次最多分析 4 张图片。")
    model_images = [] if rich else await _model_images(config, images)
    message_content = await _prepared_blocks(config, text, images) if rich else None
    previous_search_proxy = os.environ.get("DDGS_PROXY")
    if config.search_proxy:
        os.environ["DDGS_PROXY"] = config.search_proxy
    elif previous_search_proxy is not None:
        os.environ.pop("DDGS_PROXY", None)
    home = _google_home(config) if config.auth_mode == "service_account" else Path(config.home)

    async def send(message: str):
        await send_text(session, message)

    try:
        try:
            result = await answer(
                "请结合下面已展开的消息回答。" if rich else (text or ""),
                send=send,
                provider=config.provider,
                model=config.model,
                api=config.api,
                base_url=None if config.auth_mode == "service_account" else config.base_url,
                api_key="" if config.auth_mode == "service_account" else config.api_key,
                home=home,
                search_provider=config.search_provider,
                search_mode=config.search_mode,
                max_tool_images=config.max_tool_images,
                max_reader_images=config.max_reader_images,
                reader_engine=config.reader_engine,
                timeout=config.request_timeout,
                history=prior or None,
                images=None if rich else (model_images or None),
                message_content=message_content,
            )
        except RenderError as error:
            logger.warning("[hyw] card render failed: {}", failure_summary(error, _config_secrets(config)))
            raise HywError("回答已生成，但出图失败。请稍后重试。") from None
        except FrontierError as error:
            logger.warning("[hyw] answer failed: {}", failure_summary(error, _config_secrets(config)))
            raise HywError(str(error)) from None
    finally:
        if previous_search_proxy is None:
            os.environ.pop("DDGS_PROXY", None)
        else:
            os.environ["DDGS_PROXY"] = previous_search_proxy
    history = _continuable(result.messages)
    listed = _listed_sources(result)
    receipts = []
    if config.render and result.kind == "image" and result.png:
        try:
            jpeg = _png_jpeg(result.png)
        except OSError:
            jpeg = b""
        if jpeg:
            receipts = await session.send(MessageChain([make_image(raw=jpeg)]), reply_to=True) or []
    if not receipts:
        receipts = await send_text(session, result.display_text or result.text)
    _remember(scope, receipts, history, listed)


async def _compose(session: Session, elements) -> tuple[str, list[str], bool]:
    text, images = parts_from(elements)
    extra_text, extra_images, rich = await expand_special(session, elements)
    if extra_text:
        text = f"{text}\n{extra_text}".strip() if text else extra_text
    return text, [*images, *[url for url in extra_images if url not in images]], rich or bool(extra_text or extra_images)


def _unfetched_quote(session: Session):
    """The event's quote when Entari could not turn it into ``session.reply``.

    The Entari adapter keeps ``event.quote`` when ``message.get`` fails (LLBot answers
    500 "消息为空" for merged forwards), so the quoted id is still available here.
    """
    from satori import Quote

    if session.reply:
        return None
    quote = getattr(getattr(session, "event", None), "quote", None)
    return quote if isinstance(quote, Quote) and quote.id else None


async def _quoted_without_reply(session: Session, quote) -> tuple[str, list[str], bool]:
    if quote.children:
        return await _compose(session, MessageChain(quote.children))
    failures: list[str] = []
    text, images, rich = await expand_quote(session, str(quote.id), failures)
    if not text and not images:
        logger.warning(
            "[hyw] quoted message unavailable, answering without it: channel={} quote={} failures={}",
            getattr(getattr(session.event, "channel", None), "id", None), quote.id, failures,
        )
    elif failures:
        logger.debug("[hyw] quoted message read after fallbacks: quote={} failures={}", quote.id, failures)
    return text, images, rich


async def handle_hyw(session: Session, result: Arparma):
    text, images, rich = await _compose(session, result.all_matched_args.get("content", []) or [])
    scope = scope_for(session)
    quote = _unfetched_quote(session)
    if text.lower() in {"帮助", "help", "--help"} or (not text and not images and not session.reply and not quote):
        await send_text(session, HELP)
        return
    if text.lower() in {"清空", "重置", "clear", "reset"} and not images:
        if scope in _active:
            await send_text(session, "当前仍有问题在处理中，请全部完成后再清空。")
            return
        history_store.clear(scope)
        sources.clear_sender(channel_for(scope), scope[4])
        await send_text(session, "已清除你在当前会话中的 HYW 历史。")
        return
    try:
        config = HywConfig.from_env()
    except ValueError as error:
        await send_text(session, str(error))
        return
    if sum(_active.values()) >= config.max_concurrent:
        await send_text(session, "HYW 当前较忙，请稍后重试。")
        return
    if not config.configured:
        if config.auth_mode == "none" and config.auth_error:
            await send_text(session, f"{config.auth_error}请管理员检查 HYW_CREDENTIALS_FILE 指向的服务账号 JSON。")
        elif config.credentials_file:
            await send_text(session, "HYW 尚未配置模型密钥。请管理员填写 HYW_API_KEY、HYW_BASE_URL、HYW_MODEL，或设置 HYW_CONFIG_SOURCE 复用现有模型配置。")
        else:
            await send_text(session, "HYW 尚未配置模型密钥。请管理员填写 HYW_API_KEY，或把 Google 服务账号 JSON 放到 HYW_CREDENTIALS_FILE 指向的路径。")
        return
    prior = []
    if session.reply and session.reply.origin:
        origin = session.reply.origin
        prior = history_store.get(scope, str(origin.id))
        if not prior:
            quoted_text, quoted_images, quoted_rich = await _compose(session, MessageChain(origin.message))
            text = f"{text}\n\n[引用消息]\n{quoted_text}".strip()
            images = [*images, *[url for url in quoted_images if url not in images]]
            rich = rich or quoted_rich
    elif quote:
        prior = history_store.get(scope, str(quote.id))
        if not prior:
            quoted_text, quoted_images, quoted_rich = await _quoted_without_reply(session, quote)
            if quoted_text or quoted_images:
                text = f"{text}\n\n[引用消息]\n{quoted_text}".strip()
                images = [*images, *[url for url in quoted_images if url not in images]]
                rich = rich or quoted_rich
            elif not text and not images:
                await send_text(session, "没能读取引用的消息，请在 /q 后直接写出问题。")
                return
    _active[scope] = _active.get(scope, 0) + 1
    task = asyncio.current_task()
    if task is not None:
        _inflight.setdefault(scope, set()).add(task)
    try:
        await asyncio.wait_for(run_request(session, config, scope, text, images, prior, rich=rich), timeout=config.timeout)
    except HywError as error:
        await send_text(session, str(error))
    except asyncio.TimeoutError:
        await send_text(session, f"本次问答超过 {int(config.timeout)} 秒，请稍后重试或缩小问题范围。")
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - Chat boundary: always report failure, redact upstream payloads.
        logger.warning("[hyw] request failed: {}", failure_summary(error, _config_secrets(config)))
        await send_text(session, "HYW 处理失败，请稍后重试。")
    finally:
        if task is not None:
            _inflight.get(scope, set()).discard(task)
            if not _inflight.get(scope):
                _inflight.pop(scope, None)
        if _active.get(scope, 0) <= 1:
            _active.pop(scope, None)
        else:
            _active[scope] -= 1


async def handle_stop(session: Session, result: Arparma):
    scope = scope_for(session)
    tasks = [
        task for owner, group in _inflight.items()
        if owner[:4] == scope[:4] and owner[4] == scope[4]
        for task in group
    ]
    if not tasks:
        await send_text(session, "你在当前会话中没有正在进行的 HYW 问答。")
        return
    for task in tasks:
        task.cancel()
    await send_text(session, "已取消你在当前会话中的 HYW 问答。")


async def handle_link(session: Session, result: Arparma):
    if session.reply and session.reply.origin:
        quoted_id = str(session.reply.origin.id)
    elif quote := _unfetched_quote(session):
        quoted_id = str(quote.id)
    else:
        await send_text(session, "请回复一条 HYW 回答后再发送 /link。")
        return
    listed = sources.get(channel_for(scope_for(session)), quoted_id)
    if not listed:
        await send_text(session, "这条消息没有可查询的 HYW 来源。请回复 HYW 的回答后再发 /link。")
        return
    lines = ["参考资料："]
    for index, item in enumerate(listed, 1):
        lines.append(f"[{index}] {item['title']}\n{item['url']}")
    await send_text(session, "\n".join(lines))


hyw_command = Alconna(["q", "hyw", "何意味"], Args["content;?", AllParam])
command.on(hyw_command)(handle_hyw)
command.on(Alconna("qstop"))(handle_stop)
command.on(Alconna(["link", "qlink"]))(handle_link)
