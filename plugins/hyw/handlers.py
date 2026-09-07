from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import AsyncExitStack
from io import BytesIO

import httpx
from arclet.alconna import Alconna, AllParam, Args, Arparma
from arclet.entari import MessageChain, Session, command
from loguru import logger
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from satori import Image, Text

from otae_bot.adapters.entari import make_image
from otae_bot.infrastructure.rendering.executor import run_image_render

from .agent import ask
from .config import HywConfig, HywError
from .history import HistoryStore, Scope
from .rendering import render_answer, source_links, wants_card
from .web import download

HELP = """HYW / 何意味
/q 问题：搜索问答，也可附带图片（最多 3 张，需要视觉模型）。
引用一条消息后发送 /q 问题：分析引用内容。
引用自己的 HYW 回答后发送 /q 追问：继续对话，有效期 1 小时。
/q 清空：清除自己在当前会话中的历史。
别名：/hyw、/何意味。每次不引用回答时会开始新对话。
输入和引用内容会发送给配置的模型服务；搜索词会发送给 DuckDuckGo。"""
history_store = HistoryStore()
_active: set[Scope] = set()


def scope_for(session: Session) -> Scope:
    event = session.event
    return (
        str(session.account.platform), str(session.account.self_id),
        str(getattr(getattr(event, "guild", None), "id", "")),
        str(getattr(getattr(event, "channel", None), "id", "")),
        str(event.user.id),
    )


def parts_from(elements) -> tuple[str, list[str]]:
    text, images = [], []
    for element in elements:
        if isinstance(element, (Text, str)):
            text.append(element.text if isinstance(element, Text) else element)
        elif isinstance(element, Image):
            images.append(element.src)
    return " ".join(text).strip(), images


def encode_image(data: bytes) -> str:
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
            background.save(buffer, format="JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError):
        raise HywError("无法读取图片，请使用 PNG、JPEG 或 WebP 图片。") from None


async def model_content(client: httpx.AsyncClient, text: str, images: list[str]) -> str | list[dict]:
    if len(text) > 6000:
        raise HywError("问题和引用内容合计不能超过 6000 字。")
    if len(images) > 3:
        raise HywError("每次最多分析 3 张图片。")
    if not images:
        return text
    result = [{"type": "text", "text": text or "请解释这些图片的含义。"}]
    for url in images:
        if url.startswith("data:image/"):
            try:
                header, encoded = url.split(",", 1)
                if not header.endswith(";base64") or len(encoded) > 7_000_000:
                    raise ValueError("invalid data URI")
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise HywError("图片编码无效或大小超过 5 MB。") from None
        else:
            data, _, _ = await download(client, url, max_bytes=5_000_000)
        if len(data) > 5_000_000:
            raise HywError("单张图片不能超过 5 MB。")
        encoded = await run_image_render(encode_image, data)
        result.append({"type": "image_url", "image_url": {"url": encoded}})
    return result


async def send_text(session: Session, text: str) -> list:
    receipts = []
    for start in range(0, len(text), 2000):
        receipts.extend(await session.send(MessageChain([Text(text[start:start + 2000])])) or [])
    return receipts


async def run_request(session: Session, config: HywConfig, scope: Scope, text: str, images: list[str], prior: list[dict]):
    # Credentials are attached only to POST /chat/completions, never to search/image GETs.
    transport = httpx.AsyncHTTPTransport(proxy=config.proxy or None)
    async with (
        httpx.AsyncClient(transport=transport, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client,
        AsyncExitStack() as stack,
    ):
        search_proxy = config.proxy if config.search_proxy is None else config.search_proxy
        tool_client = client
        if search_proxy != config.proxy:
            tool_client = await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(proxy=search_proxy or None),
                trust_env=False, headers={"User-Agent": "Mozilla/5.0"},
            ))
        logger.info("[hyw] request routes: model={} search={}", "proxy" if config.proxy else "direct", "proxy" if search_proxy else "direct")
        content = await model_content(client, text, images)

        async def progress(message: str):
            await send_text(session, message)

        answer = await ask(client, config, content, history=prior, progress=progress, tool_client=tool_client)
    receipts = []
    if config.render and wants_card(answer.text):
        try:
            card = await render_answer(answer)
        except Exception as error:  # noqa: BLE001 - Rendering must fall back without logging user content.
            logger.warning("[hyw] card render failed: {}", type(error).__name__)
        else:
            receipts = await session.send(MessageChain([make_image(raw=card)])) or []
    if not receipts:
        receipts = await send_text(session, answer.text)
    # Save as each successfully delivered answer becomes available, including multipart replies.
    for receipt in receipts:
        history_store.put(scope, str(receipt.id), answer.history)
    links = source_links(answer)
    if links:
        for receipt in await send_text(session, links):
            history_store.put(scope, str(receipt.id), answer.history)


async def handle_hyw(session: Session, result: Arparma):
    text, images = parts_from(result.all_matched_args.get("content", []) or [])
    scope = scope_for(session)
    if text.lower() in {"帮助", "help", "--help"} or (not text and not images and not session.reply):
        await send_text(session, HELP)
        return
    if text.lower() in {"清空", "重置", "clear", "reset"} and not images:
        if scope in _active:
            await send_text(session, "当前问题还在处理中，请完成后再清空。")
            return
        history_store.clear(scope)
        await send_text(session, "已清除你在当前会话中的 HYW 历史。")
        return
    if scope in _active:
        await send_text(session, "你的上一条问题还在处理中，请稍等。")
        return
    if len(_active) >= 4:
        await send_text(session, "HYW 当前较忙，请稍后重试。")
        return
    try:
        config = HywConfig.from_env()
    except ValueError as error:
        await send_text(session, str(error))
        return
    if not config.api_key:
        await send_text(session, "HYW 尚未配置模型密钥。请管理员填写 HYW_API_KEY、HYW_BASE_URL、HYW_MODEL，或设置 HYW_CONFIG_SOURCE 复用现有模型配置。")
        return
    prior = []
    if session.reply and session.reply.origin:
        origin = session.reply.origin
        prior = history_store.get(scope, str(origin.id))
        if not prior:
            quoted_text, quoted_images = parts_from(MessageChain(origin.message))
            text = f"{text}\n\n[引用消息]\n{quoted_text}".strip()
            images = [*images, *quoted_images]
    _active.add(scope)
    try:
        await asyncio.wait_for(run_request(session, config, scope, text, images, prior), timeout=config.timeout)
    except HywError as error:
        await send_text(session, str(error))
    except asyncio.TimeoutError:
        await send_text(session, "本次问答超过 120 秒，请稍后重试或缩小问题范围。")
    except Exception as error:  # noqa: BLE001 - Chat boundary: always report failure, redact upstream payloads.
        # Avoid logging credentials, user images, prompts or provider response bodies.
        logger.warning("[hyw] request failed: {}", type(error).__name__)
        await send_text(session, "HYW 处理失败，请稍后重试。")
    finally:
        _active.discard(scope)


hyw_command = Alconna(["q", "hyw", "何意味"], Args["content;?", AllParam])
command.on(hyw_command)(handle_hyw)
