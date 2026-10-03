"""Jina Reader full-page screenshots with optional crop-for-display support."""
from __future__ import annotations

import base64
from contextlib import ExitStack, closing
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock
from typing import Callable
import warnings

from PIL import Image, ImageOps

from .jina import JinaClient, JinaError
from .media_refs import display_url as image_display_url
from .prompt_files import read_prompt
from .telemetry import stage

MAX_STORED_PAGESHOTS = 4
MAX_SOURCE_PIXELS = 80_000_000
DOWNLOAD_TIMEOUT = 15.0
MAX_MODEL_BYTES = 4 * 1024 * 1024
MAX_MODEL_WIDTH = 1536
MAX_MODEL_HEIGHT = 12000
MAX_RENDER_BYTES = 256 * 1024
MAX_RENDER_EDGE = 1280


def _marker(kind: str, metadata: dict) -> dict:
    return {"type": "text", "text": kind + " " + json.dumps(metadata, ensure_ascii=False)}


def _download(url: str) -> bytes:
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("media_fetch.py")), str(DOWNLOAD_TIMEOUT)],
            input=url.encode(), capture_output=True, timeout=DOWNLOAD_TIMEOUT,
            env={k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise JinaError("pageshot_download_failed", "整页截图已生成，但图片下载失败或超时；未自动重试") from None
    if result.returncode != 0 or not result.stdout:
        raise JinaError("pageshot_download_failed", "整页截图已生成，但图片下载失败；未自动重试")
    return result.stdout


def _oriented_image(raw: bytes, resources: ExitStack) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        source = resources.enter_context(closing(Image.open(resources.enter_context(BytesIO(raw)))))
        if source.format not in ("PNG", "JPEG", "WEBP"):
            raise JinaError("invalid_pageshot", "Reader pageshot 返回的不是受支持图片")
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise JinaError("pageshot_too_large", "整页截图像素过大，无法安全送入模型；请改用 Reader 正文或更具体的页面")
        source.seek(0)
        image = resources.enter_context(closing(ImageOps.exif_transpose(source)))
        image.load()
        return image


def _rgb(image: Image.Image, resources: ExitStack) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = resources.enter_context(closing(image.convert("RGBA")))
        output = resources.enter_context(closing(Image.new("RGB", rgba.size, "white")))
        alpha = resources.enter_context(closing(rgba.getchannel("A")))
        output.paste(rgba, mask=alpha)
        return output
    if image.mode != "RGB":
        return resources.enter_context(closing(image.convert("RGB")))
    return image


def _jpeg(image: Image.Image, *, quality: int) -> bytes:
    with BytesIO() as output:
        image.save(output, "JPEG", quality=quality, subsampling=2, optimize=False, progressive=False)
        return output.getvalue()


def _model_preview(raw: bytes) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    with ExitStack() as resources:
        image = _oriented_image(raw, resources)
        source_size = image.size
        scale = min(1.0, MAX_MODEL_WIDTH / image.width, MAX_MODEL_HEIGHT / image.height)
        if scale < 1:
            image = resources.enter_context(closing(image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.BILINEAR,
            )))
        image = _rgb(image, resources)
        encoded = _jpeg(image, quality=82)
        if len(encoded) > MAX_MODEL_BYTES:
            encoded = _jpeg(image, quality=62)
        if len(encoded) > MAX_MODEL_BYTES:
            scale = min(0.95, math.sqrt(MAX_MODEL_BYTES / len(encoded)) * 0.9)
            image = resources.enter_context(closing(image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.BILINEAR,
            )))
            encoded = _jpeg(image, quality=60)
        if len(encoded) > MAX_MODEL_BYTES:
            raise JinaError("pageshot_too_large", "整页截图压缩后仍过大，无法安全送入模型；请改用 Reader 正文")
        return encoded, image.size, source_size


def _render_crop(image: Image.Image) -> tuple[bytes, tuple[int, int]]:
    with ExitStack() as resources:
        if max(image.size) > MAX_RENDER_EDGE:
            image = resources.enter_context(closing(image.copy()))
            image.thumbnail((MAX_RENDER_EDGE, MAX_RENDER_EDGE), Image.Resampling.BILINEAR, reducing_gap=2.0)
        image = _rgb(image, resources)
        encoded = _jpeg(image, quality=82)
        if len(encoded) > MAX_RENDER_BYTES:
            image = resources.enter_context(closing(image.copy()))
            image.thumbnail((960, 960), Image.Resampling.BILINEAR, reducing_gap=2.0)
            encoded = _jpeg(image, quality=65)
        if len(encoded) > MAX_RENDER_BYTES:
            raise JinaError("pageshot_crop_too_large", "截图裁剪后仍超过展示图片上限，请缩小裁剪区域")
        return encoded, image.size


class PageshotStore:
    """Task-local full-page screenshots. Full shots feed the model; crops may feed final rendering."""

    def __init__(self, jina: JinaClient, *, fetch_image: Callable[[str], bytes] | None = None):
        self.jina = jina
        self.fetch_image = fetch_image or _download
        self._shots: dict[str, dict] = {}
        self._lock = Lock()
        self._asset_sink: Callable[[str, bytes], None] | None = None
        self._reserve_image: Callable[[], bool] | None = None

    def set_asset_sink(self, sink: Callable[[str, bytes], None] | None):
        self._asset_sink = sink

    def set_image_budget(self, reserve: Callable[[], bool] | None):
        self._reserve_image = reserve

    def close(self):
        with self._lock:
            self._shots.clear()
        self._asset_sink = None
        self._reserve_image = None

    def capture(self, url: str, *, on_event=None) -> tuple[dict, list[dict]]:
        with stage(on_event, 'pageshot_generate') as metrics:
            shot = self.jina.pageshot_url({"url": url})
            metrics['cached'] = shot.get('cached', False)
        with stage(on_event, 'pageshot_download') as metrics:
            raw = self.fetch_image(shot["image_url"])
            metrics['download_bytes'] = len(raw)
        try:
            with stage(on_event, 'pageshot_compression') as metrics:
                preview, (width, height), (source_width, source_height) = _model_preview(raw)
                metrics.update(input_bytes=len(raw), output_bytes=len(preview), width=width, height=height)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise JinaError("invalid_pageshot", "Reader pageshot 图片无法解析") from exc
        digest = hashlib.sha256(shot["url"].encode() + raw).hexdigest()[:20]
        pageshot_id = "pageshot_" + digest
        record = {
            "url": shot["url"], "raw": raw,
            "width": width, "height": height,
            "source_width": source_width, "source_height": source_height,
        }
        with self._lock:
            self._shots[pageshot_id] = record
            while len(self._shots) > MAX_STORED_PAGESHOTS:
                self._shots.pop(next(iter(self._shots)))
        marker = {
            "pageshot_id": pageshot_id, "url": shot["url"],
            "width": width, "height": height,
            "source_width": source_width, "source_height": source_height,
            "coordinates": "bbox uses the attached pageshot pixel coordinates [left, top, right, bottom)",
        }
        return {
            "ok": True, **marker, "full_page": True,
            "display_hint": "需要在最终回复展示局部时，用同一工具传 pageshot_id 与 bbox 裁剪；使用裁剪结果的 display_url。",
        }, [
            _marker("pageshot_attachment", marker),
            {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(preview).decode("ascii")},
        ]

    def crop(self, pageshot_id: str, bbox: list[int], *, on_event=None) -> tuple[dict, list[dict]]:
        with self._lock:
            record = self._shots.get(pageshot_id)
        if record is None:
            return {
                "ok": False, "code": "unknown_pageshot",
                "error": "pageshot_id 不可用；请先在当前任务调用 jina_pageshot 获取整页截图",
            }, []
        width, height = record["width"], record["height"]
        if (not isinstance(bbox, list) or len(bbox) != 4 or any(type(v) is not int for v in bbox)
                or not (0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height)):
            return {
                "ok": False, "code": "invalid_bbox", "pageshot_id": pageshot_id,
                "width": width, "height": height,
                "error": "bbox 须使用 pageshot_attachment 的像素坐标 [left, top, right, bottom]，且位于截图范围内",
            }, []
        try:
            with stage(on_event, 'pageshot_crop_compression', pageshot_id=pageshot_id) as metrics, ExitStack() as resources:
                image = _oriented_image(record["raw"], resources)
                source_width, source_height = image.size
                left = math.floor(bbox[0] * source_width / width)
                top = math.floor(bbox[1] * source_height / height)
                right = math.ceil(bbox[2] * source_width / width)
                bottom = math.ceil(bbox[3] * source_height / height)
                right, bottom = min(source_width, right), min(source_height, bottom)
                crop = resources.enter_context(closing(image.crop((left, top, right, bottom))))
                encoded, (out_width, out_height) = _render_crop(crop)
                metrics.update(output_bytes=len(encoded), width=out_width, height=out_height)
        except JinaError:
            raise
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise JinaError("pageshot_crop_failed", "整页截图裁剪失败；请缩小或修正裁剪区域") from exc

        digest = hashlib.sha256((pageshot_id + json.dumps(bbox)).encode() + encoded).hexdigest()[:16]
        display_url = image_display_url(pageshot_id + json.dumps(bbox), encoded)
        sink = self._asset_sink
        if sink is not None:
            sink(display_url, encoded)
        image_id = "pageshot_crop_" + digest
        row = {
            "image_id": image_id, "display_url": display_url, "source_url": record["url"],
            "status": "ready", "width": out_width, "height": out_height,
            "mimeType": "image/jpeg", "bytes": len(encoded),
            "orientation": "landscape" if out_width > out_height else "portrait" if out_width < out_height else "square",
            "pageshot_id": pageshot_id, "bbox": bbox,
        }
        marker = {"media_attachment": image_id, **row}
        return {
            "ok": True, "pageshot_id": pageshot_id, "bbox": bbox,
            "display_url": display_url, "image_id": image_id,
            "width": out_width, "height": out_height,
            "display_ready": sink is not None,
            "media_images": [row], "media_notice": read_prompt('media_notice.md'),
        }, [
            {"type": "text", "text": json.dumps(marker, ensure_ascii=False)},
            {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(encoded).decode("ascii")},
        ]

    def run(self, args: dict, *, on_event=None) -> tuple[dict, list[dict]]:
        try:
            if self._reserve_image is not None and not self._reserve_image():
                return {"ok": False, "code": "image_budget_exhausted",
                        "error": "工具图片预算已用尽，未获取或裁剪截图；请使用已有资料完成回答"}, []
            if "url" in args:
                return self.capture(args["url"], on_event=on_event)
            return self.crop(args["pageshot_id"], args["bbox"], on_event=on_event)
        except JinaError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}, []
