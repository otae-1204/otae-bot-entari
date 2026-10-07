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

from otae_bot.infrastructure.http.tls import ashared_ssl_context

from .config import GrokError

MAX_INPUT_IMAGES = 3
MAX_IMAGE_BYTES = 5_000_000
# Upload copies: longest edge, JPEG qualities tried in turn, encoded size.
UPLOAD_EDGE = 1600
UPLOAD_QUALITIES = (85, 75, 65)
UPLOAD_IMAGE_BYTES = 2_000_000
ANIMATED_FORMATS = frozenset({"GIF", "PNG", "WEBP"})
UPLOAD_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                  "GIF": ("gif", "image/gif"), "WEBP": ("webp", "image/webp")}
MAX_FILE_BYTES = 20_000_000
MAX_REPLY_BYTES = 50_000_000
MAX_REPLY_FILES = 10


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
        raise GrokError("云端附件路径格式异常，已终止访问本地文件系统。") from None
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
        raise GrokError("附件内容编码格式有误，或已超出体积上限。") from None


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
        raise GrokError("无法请求该附件地址，仅支持公网可达的 HTTP/HTTPS 链接与适配器内置图片。") from None
    return target.copy_with(host=addresses[0][4][0]), {"Host": target.netloc.decode()}, {"sni_hostname": target.host}


async def download_url(url: str, *, limit: int, account=None) -> tuple[bytes, str]:
    trusted = None
    proxies = getattr(account, "proxy_urls", ()) if account is not None else ()
    via_bridge = isinstance(proxies, (list, tuple)) and any(isinstance(prefix, str) and prefix and url.startswith(prefix) for prefix in proxies)
    if url.startswith("internal:") or via_bridge:
        if account is None or not callable(getattr(account, "ensure_url", None)):
            raise GrokError("适配器内部图片解析失败，请尝试重新发送该图片。")
        trusted = str(account.ensure_url(url))
        url = trusted
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20, verify=await ashared_ssl_context(trust_env=False)) as client:
            for _ in range(4):
                if url == trusted:
                    target, headers, extensions = httpx.URL(url), {}, {}
                    if target.scheme not in {"http", "https"} or target.username or target.password:
                        raise GrokError("适配器提供的内部图片地址无效。")
                else:
                    target, headers, extensions = await public_request(url)
                async with client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise GrokError("附件请求发生异常重定向，无法获取目标资源。")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise GrokError(f"附件获取未成功（HTTP {response.status_code}），请重新上传或稍候重试。")
                    size = response.headers.get("content-length", "")
                    if size.isdigit() and int(size) > limit:
                        raise GrokError("附件文件体积已超出下载上限，未执行拉取。")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > limit:
                            raise GrokError("附件下载中超出体积限制，已中止传输。")
                        data.extend(chunk)
                    return bytes(data), response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    except (httpx.HTTPError, httpx.InvalidURL):
        raise GrokError("附件拉取超时或网络连接异常，请重新上传图片或稍后重试。") from None
    raise GrokError("附件链接重定向层级过多，已中止请求。")


def encoded(image: PILImage.Image, **options) -> bytes:
    stream = BytesIO()
    image.save(stream, **options)
    return stream.getvalue()


def has_alpha(image: PILImage.Image) -> bool:
    if image.mode not in {"RGBA", "LA", "PA", "RGBa", "La"} and "transparency" not in image.info:
        return False
    return image.convert("RGBA").getchannel("A").getextrema()[0] < 255


def encodings(image: PILImage.Image, alpha: bool):
    """Smaller and smaller encodings at this size; transparency stays PNG."""
    if alpha:
        yield encoded(image, format="PNG")
        # A 256-colour palette keeps the alpha channel.
        yield encoded(image.quantize(256, method=PILImage.Quantize.FASTOCTREE), format="PNG")
    else:
        for quality in UPLOAD_QUALITIES:
            yield encoded(image, format="JPEG", quality=quality, optimize=True)


def compress_image(data: bytes) -> tuple[bytes, str]:
    """An upload copy within UPLOAD_EDGE and UPLOAD_IMAGE_BYTES, and its format."""
    try:
        with PILImage.open(BytesIO(data)) as original:
            if original.width * original.height > 20_000_000:
                raise GrokError("图片分辨率超出限制，请调整尺寸或压缩后再试。")
            # Re-encoding keeps only the first frame, so an animation within the limit goes as sent.
            if original.format in ANIMATED_FORMATS and getattr(original, "n_frames", 1) > 1 and len(data) <= UPLOAD_IMAGE_BYTES:
                return data, original.format
            original.seek(0)
            image = ImageOps.exif_transpose(original)
            image.thumbnail((UPLOAD_EDGE, UPLOAD_EDGE))
            alpha = has_alpha(image)
            image = image.convert("RGBA" if alpha else "RGB")
            for _ in range(8):
                for result in encodings(image, alpha):
                    if len(result) <= UPLOAD_IMAGE_BYTES:
                        return result, "PNG" if alpha else "JPEG"
                image = image.resize((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)), PILImage.Resampling.LANCZOS)
            raise GrokError("图片处理后体积依然超出限制，请压缩文件后重新上传。")
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError):
        raise GrokError("图像格式无法识别，请使用合规的 PNG、JPEG、WebP 或 GIF 文件。") from None


async def input_images(sources: tuple[str, ...], account=None) -> tuple[Attachment, ...]:
    if len(sources) > MAX_INPUT_IMAGES:
        raise GrokError(f"消息内输入与引用的图片累计上限为 {MAX_INPUT_IMAGES} 张。")
    result = []
    for index, source in enumerate(sources, 1):
        if source.startswith("data:"):
            data, _ = decode_data_url(source, MAX_IMAGE_BYTES)
        else:
            data, _ = await download_url(source, limit=MAX_IMAGE_BYTES, account=account)
        compressed, kind = await asyncio.to_thread(compress_image, data)
        extension, mime = UPLOAD_FORMATS[kind]
        result.append(Attachment(f"qq-image-{index}.{extension}", mime=mime, image=True, data=compressed))
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
        raise GrokError("未能获取当前会话接收目标，附件尚未转交发送。")
    try:
        result = await asyncio.wait_for(session.account.protocol.internal(
            "onebot11/" + action, **{key: int(peer), "name": name,
                                   "file": "base64://" + base64.b64encode(attachment.data).decode("ascii")},
        ), 60)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - Do not retry an upload with an unknown outcome.
        raise GrokError("LLOneBot 端未返回文件发送确认，请检查会话与适配器状态；本次未重复推送。") from None
    if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
        raise GrokError("LLOneBot 上传或发送文件失败，请确认适配器是否已开启群文件与私聊文件能力。")


async def send_attachment(session, attachment: Attachment, *, reply_to=True) -> None:
    if attachment.data is None:
        raise GrokError(attachment.error or "附件数据不可用，请前往 Grok Bot 客户端查阅。")
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
            raise GrokError("协议端未返回附件发送确认，请前往 Grok Bot 客户端查阅。")
    except asyncio.CancelledError:
        raise
    except GrokError:
        raise
    except Exception:  # noqa: BLE001 - Never expose base64 or bridge credentials.
        raise GrokError("附件投递失败，请排查适配器是否具备图片或文件发送功能。") from None
