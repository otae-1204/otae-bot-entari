"""Bounded, validated inline images for user messages (never fetch remote URLs)."""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import warnings

from PIL import Image
from .prompt_files import read_prompt

MIME_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 256 * 1024 * 1024
MAX_MESSAGE_BLOCKS = 32768
IMAGE_ONLY_TEXT = read_prompt('image_only.md')
IMAGE_INPUT_CONFIG = {"enabled": True, "paste_only": True, "mime_types": list(MIME_FORMATS),
                      "max_images": MAX_IMAGES, "max_image_bytes": MAX_IMAGE_BYTES,
                      "max_total_bytes": MAX_TOTAL_BYTES, "image_only_text": IMAGE_ONLY_TEXT}


class ImageInputError(ValueError):
    pass


def validate_images(images, *, max_image_bytes: int = MAX_IMAGE_BYTES,
                    max_total_bytes: int = MAX_TOTAL_BYTES) -> list[dict]:
    if (type(max_image_bytes) is not int or max_image_bytes < 1
            or type(max_total_bytes) is not int or max_total_bytes < 1):
        raise ValueError('Image byte limits must be positive integers')
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ImageInputError("每次最多粘贴4张图片。")
    result = []
    total = 0
    for item in images:
        if (not isinstance(item, dict) or not isinstance(item.get("mimeType"), str)
                or item["mimeType"] not in MIME_FORMATS):
            raise ImageInputError("图片仅支持 PNG、JPEG、WebP、GIF。")
        data = item.get("data")
        if not isinstance(data, str) or not data or len(data) > 4 * ((max_image_bytes + 2) // 3):
            raise ImageInputError(f"图片为空或超过单张{max_image_bytes // (1024 * 1024)} MiB上限。")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageInputError("图片编码无效。") from exc
        total += len(raw)
        if len(raw) > max_image_bytes or total > max_total_bytes:
            raise ImageInputError(f"图片超过单张{max_image_bytes // (1024 * 1024)} MiB或合计{max_total_bytes // (1024 * 1024)} MiB上限。")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as image:
                    if image.format != MIME_FORMATS[item["mimeType"]]:
                        raise ValueError("Image format invalid")
                    image.verify()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageInputError("图片触发解码器安全保护，请压缩后重新粘贴。") from exc
        except (OSError, ValueError, SyntaxError) as exc:
            raise ImageInputError("图片损坏或格式不匹配。") from exc
        result.append({"type": "image", "mimeType": item["mimeType"], "data": data})
    return result


def validate_message_content(content) -> list[dict]:
    """Explicit rich-input boundary; does not relax the ordinary four-image API.

    Count UTF-8 text and decoded image bytes, not Base64 transport overhead.
    Rebuild blocks so chat-supplied internal metadata cannot bypass validation.
    """
    if not isinstance(content, list) or len(content) > MAX_MESSAGE_BLOCKS:
        raise ImageInputError("结构化消息内容块超过安全上限。")
    result = []
    total = 0
    for block in content:
        if not isinstance(block, dict):
            raise ImageInputError("结构化消息内容块无效。")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            clean = {"type": "text", "text": block["text"]}
            total += len(clean["text"].encode("utf-8"))
        elif block.get("type") == "image":
            clean = validate_images([block])[0]
            total += len(base64.b64decode(clean["data"], validate=True))
        else:
            raise ImageInputError("结构化消息仅支持文字和图片。")
        if total > MAX_MESSAGE_BYTES:
            raise ImageInputError("结构化消息超过256 MiB上限，请按原顺序截断后提交。")
        result.append(clean)
    return result
