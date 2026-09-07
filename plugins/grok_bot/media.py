"""Bounded image input and QQ delivery; gateway credentials never reach media URLs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from satori import ChannelType, File, Image
from satori.model import Upload

from otae_bot.infrastructure.rendering.executor import run_image_render

from .config import GrokError

MAX_INPUT_IMAGES = 3
MAX_IMAGE_BYTES = 5_000_000
MAX_FILE_BYTES = 20_000_000
MAX_REPLY_BYTES = 50_000_000
MAX_REPLY_FILES = 10
REPLY_DOWNLOAD_TIMEOUT = 60


def remote_path(source: str) -> str:
    """A cloud path is sent to the gateway, never opened on the QQ machine."""
    try:
        parsed = urlsplit(source)
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"} and not parsed.query and not parsed.fragment:
            source = unquote(parsed.path, errors="strict")
        elif parsed.scheme:
            raise ValueError
        if not source.startswith("/") or source.startswith("//") or "\\" in source or any(ord(c) < 32 for c in source):
            raise ValueError
    except (ValueError, UnicodeError):
        raise GrokError("云端附件路径无效，未读取本地文件。") from None
    return source


def mime_type(value: object) -> str:
    if isinstance(value, str) and "/" in value and not any(c.isspace() or ord(c) < 32 for c in value):
        return value.lower()
    return "application/octet-stream"


@dataclass(frozen=True)
class Attachment:
    name: str
    source: str = field(default="", repr=False)
    mime: str = "application/octet-stream"
    image: bool = False
    data: bytes | None = field(default=None, repr=False)
    error: str = ""


@dataclass(frozen=True)
class Reply:
    text: str = ""
    attachments: tuple[Attachment, ...] = ()


def filename(name: str, fallback: str = "attachment.bin") -> str:
    # Windows reserved characters and path separators must never become paths.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if character in '<>:"/\\|?*' or ord(character) < 32 else character for character in name)
    name = name[:160].strip(" .")
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name or fallback


def decode_data_url(source: str, limit: int) -> tuple[bytes, str]:
    try:
        header, encoded = source.split(",", 1)
        if not header.startswith("data:") or not header.endswith(";base64") or len(encoded) > ((limit + 2) // 3) * 4:
            raise ValueError
        data = base64.b64decode(encoded, validate=True)
        if len(data) > limit:
            raise ValueError
        return data, header[5:-7]
    except (ValueError, binascii.Error):
        raise GrokError("附件编码无效或超过大小限制。") from None


async def public_request(url: str) -> tuple[httpx.URL, dict, dict]:
    """Pin a public IP for this request, retaining Host and TLS SNI."""
    try:
        parsed = urlsplit(url)
        if len(url) > 8192 or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise ValueError
        target = httpx.URL(url)
        addresses = await asyncio.get_running_loop().getaddrinfo(target.host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
    except (ValueError, OSError, httpx.InvalidURL):
        raise GrokError("附件地址不可访问，仅支持公开的 HTTP/HTTPS 地址和当前适配器的内部图片。") from None
    return target.copy_with(host=addresses[0][4][0]), {"Host": target.netloc.decode()}, {"sni_hostname": target.host}


async def download_url(url: str, *, limit: int, account=None) -> tuple[bytes, str]:
    trusted = None
    proxies = getattr(account, "proxy_urls", ()) if account is not None else ()
    via_bridge = isinstance(proxies, (list, tuple)) and any(isinstance(prefix, str) and prefix and url.startswith(prefix) for prefix in proxies)
    if url.startswith("internal:") or via_bridge:
        if account is None or not callable(getattr(account, "ensure_url", None)):
            raise GrokError("无法读取适配器内部图片，请重新发送图片。")
        trusted = str(account.ensure_url(url))
        url = trusted
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as client:
            for _ in range(4):
                if url == trusted:
                    target, headers, extensions = httpx.URL(url), {}, {}
                    if target.scheme not in {"http", "https"} or target.username or target.password:
                        raise GrokError("适配器内部图片地址无效。")
                else:
                    target, headers, extensions = await public_request(url)
                async with client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise GrokError("附件地址重定向无效。")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise GrokError(f"附件下载失败（HTTP {response.status_code}），请重新发送或稍后重试。")
                    size = response.headers.get("content-length", "")
                    if size.isdigit() and int(size) > limit:
                        raise GrokError("附件超过大小限制，未下载。")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > limit:
                            raise GrokError("附件超过大小限制，已停止下载。")
                        data.extend(chunk)
                    return bytes(data), response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    except (httpx.HTTPError, httpx.InvalidURL):
        raise GrokError("附件下载失败或超时，请重新发送图片或稍后重试。") from None
    raise GrokError("附件地址重定向次数过多。")


def normalize_image(data: bytes) -> bytes:
    try:
        with PILImage.open(BytesIO(data)) as original:
            if original.width * original.height > 20_000_000:
                raise GrokError("图片像素过大，请压缩后再发送。")
            original.seek(0)
            normalized = ImageOps.exif_transpose(original)
            normalized.thumbnail((2048, 2048))
            rgba = normalized.convert("RGBA")
            rgb = PILImage.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            stream = BytesIO()
            rgb.save(stream, format="JPEG", quality=90)
            result = stream.getvalue()
            if len(result) > MAX_IMAGE_BYTES:
                raise GrokError("图片转换后超过大小限制，请压缩后重新发送。")
            return result
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError):
        raise GrokError("无法读取图片，请使用有效的 PNG、JPEG、WebP 或 GIF 图片。") from None


async def input_images(sources: tuple[str, ...], account=None) -> tuple[Attachment, ...]:
    if len(sources) > MAX_INPUT_IMAGES:
        raise GrokError(f"发送和引用的图片合计不能超过 {MAX_INPUT_IMAGES} 张。")
    result = []
    for index, source in enumerate(sources, 1):
        if source.startswith("data:"):
            data, _ = decode_data_url(source, MAX_IMAGE_BYTES)
        else:
            data, _ = await download_url(source, limit=MAX_IMAGE_BYTES, account=account)
        normalized = await run_image_render(normalize_image, data)
        result.append(Attachment(f"qq-image-{index}.jpg", mime="image/jpeg", image=True, data=normalized))
    return tuple(result)


async def send_llonebot_file(session, attachment: Attachment, name: str) -> None:
    # LLOneBot's Satori <file> branch is a TODO. Its internal OneBot upload
    # actions both upload and send the file, on this same adapter/account.
    event = session.event
    channel = getattr(event, "channel", None)
    if getattr(channel, "type", None) in {ChannelType.DIRECT, "direct"}:
        action, key = "upload_private_file", "user_id"
        peer = getattr(getattr(event, "user", None), "id", "")
    else:
        action, key = "upload_group_file", "group_id"
        peer = getattr(getattr(event, "guild", None), "id", "") or getattr(channel, "id", "")
    if not str(peer).isascii() or not str(peer).isdigit() or int(peer) <= 0:
        raise GrokError("无法确定 QQ 文件接收者，附件尚未发送。")
    try:
        result = await asyncio.wait_for(session.account.protocol.internal(
            "onebot11/" + action, **{key: int(peer), "name": name,
                                   "file": "base64://" + base64.b64encode(attachment.data).decode("ascii")},
        ), 60)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - Do not retry an upload with an unknown outcome.
        raise GrokError("LLOneBot 文件发送未确认，请检查当前会话和适配器；本次未重复发送。") from None
    if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
        raise GrokError("LLOneBot 文件上传或发送失败，请检查适配器的群文件或私聊文件支持。")


async def send_attachment(session, attachment: Attachment, *, reply_to=True) -> None:
    if attachment.data is None:
        raise GrokError(attachment.error or "附件内容不可用，请在 Grok Bot 应用中查看。")
    name = filename(attachment.name, "image.jpg" if attachment.image else "attachment.bin")
    if not attachment.image and str(getattr(session.account, "adapter", "") or "").casefold() == "llonebot":
        await send_llonebot_file(session, attachment, name)
        return
    element_type = Image if attachment.image else File
    try:
        urls = await asyncio.wait_for(session.account.protocol.upload_create(
            Upload(attachment.data, attachment.mime, name),
        ), 60)
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str) or not urls[0]:
            raise ValueError("invalid upload receipt")
        element = element_type.of(url=urls[0], name=name)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - Some QQ bridges support data URLs but not upload.create.
        element = element_type.of(raw=attachment.data, mime=attachment.mime, name=name)
    try:
        # File-only messages work with QQ bridges that reject mixed file/text.
        from arclet.entari import MessageChain

        quote = (reply_to() if callable(reply_to) else reply_to) and attachment.image
        receipts = await asyncio.wait_for(session.send(MessageChain([element]), reply_to=quote), 60)
        if not receipts:
            raise GrokError("适配器未确认附件发送，请在 Grok Bot 应用中查看。")
    except asyncio.CancelledError:
        raise
    except GrokError:
        raise
    except Exception:  # noqa: BLE001 - Never expose base64 or bridge credentials.
        raise GrokError("QQ 附件发送失败，请检查适配器的图片或文件发送支持。") from None
