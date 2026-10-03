"""Coordinate-only user-image crops; preserve originals and return one image per call."""
from __future__ import annotations

import base64
import hashlib
import json
from contextlib import ExitStack, closing
from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps

from .image_input import validate_images

CROP_CONFIG = {
    "enabled": True, "tool": "crop_user_image", "mode": "coordinates",
    "images_per_call": 1, "preserve_original": True,
    "coordinate_space": "exif_oriented_original_pixels",
    "bbox_format": ["left", "top", "right", "bottom"], "right_bottom_exclusive": True,
    "max_crop_edge": 1536, "max_crop_bytes": 768 * 1024,
    "resize_filter": "bilinear", "resize_reducing_gap": 2.0,
    "decoded_source_cache_size": 1,
    "reverse_search_source": "crop_id",
}


def _marker(kind: str, metadata: dict) -> dict:
    return {"type": "text", "text": kind + " " + json.dumps(metadata, ensure_ascii=False)}


def _crop_id(source_id: str, bbox: list[int], data: str) -> str:
    identity = json.dumps([source_id, bbox, data], separators=(",", ":"))
    return "crop_" + hashlib.sha256(identity.encode()).hexdigest()


def _encode(image: Image.Image) -> dict:
    # Keep small text at native size where possible; no artificial enlargement.
    for edge, quality in ((CROP_CONFIG["max_crop_edge"], 90), (1024, 80), (768, 70)):
        image.thumbnail((edge, edge), Image.Resampling.BILINEAR, reducing_gap=2.0)
        # Convert only the resized tile, never allocate RGBA + RGB for the full source.
        with ExitStack() as resources:
            output = image
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = resources.enter_context(closing(image.convert("RGBA")))
                output = resources.enter_context(closing(Image.new("RGB", image.size, "white")))
                mask = resources.enter_context(closing(rgba.getchannel("A")))
                output.paste(rgba, mask=mask)
            elif image.mode not in ("RGB", "L"):
                output = resources.enter_context(closing(image.convert("RGB")))
            buffer = resources.enter_context(BytesIO())
            output.save(buffer, "JPEG", quality=quality, subsampling=0)
            raw = buffer.getvalue()
        if len(raw) <= CROP_CONFIG["max_crop_bytes"]:
            return {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(raw).decode()}
    raise ValueError("crop_size_limit")


class UserImageCrops:
    """Expose originals from the latest image-bearing user turn, never generate regions.

    A single decoded source is reused across calls within a task. The lock bounds
    pixel memory during concurrent tool calls; close() releases it on every exit.
    """

    def __init__(self, messages: list[dict]):
        self.originals: list[dict] = []
        self._crops: dict[str, dict] = {}
        self.lock = Lock()
        self._source: Image.Image | None = None
        self._source_id: str | None = None
        users = [(index, message) for index, message in enumerate(messages)
                 if message.get("role") == "user" and isinstance(message.get("content"), list)
                 and any(block.get("type") == "image" and "_crop_source" not in block
                         for block in message["content"])]
        if not users:
            return
        index, message = users[-1]
        self.originals = [block for block in message["content"]
                          if block.get("type") == "image" and "_crop_source" not in block]
        unprepared = [block for block in self.originals if "_user_image" not in block]
        # The rich-input boundary owns aggregate limits; do not reapply the
        # ordinary four-upload limit to parsed chat-record images here.
        for block in unprepared:
            validate_images([block])
        content = []
        for block in message["content"]:
            if block.get("type") != "image" or "_crop_source" in block or "_user_image" in block:
                content.append(block)
                continue
            number = 1 + next(i for i, original in enumerate(self.originals) if original is block)
            with Image.open(BytesIO(base64.b64decode(block["data"]))) as source:
                width, height = source.size
                if source.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
            metadata = {"source_id": f"user_{index + 1}_image_{number}", "image_number": number,
                        "width": width, "height": height,
                        "coordinates": "EXIF-oriented original pixels; origin top-left; x right, y down; "
                                       "bbox=[left, top, right, bottom), first frame"}
            block["_user_image"] = metadata
            content.extend([_marker("user_image_original", metadata), block])
        message["content"] = content
        original_ids = {block["_user_image"]["source_id"] for block in self.originals}
        # Retain exact crop bytes and upload metadata across follow-up turns, but
        # never reactivate crops belonging to an older image-bearing user turn.
        for previous in messages[index + 1:]:
            if previous.get("role") != "toolResult" or previous.get("toolName") != "crop_user_image" or previous.get("isError"):
                continue
            for block in previous.get("content", []):
                crop = block.get("_crop_image")
                if (block.get("type") == "image" and isinstance(crop, dict)
                        and crop.get("source_id") in original_ids
                        and crop.get("crop_id") == _crop_id(crop["source_id"], crop.get("bbox"), block.get("data", ""))):
                    self._crops[crop["crop_id"]] = block

    def resolve_search_image(self, source_id: str) -> dict | None:
        with self.lock:
            return self._crops.get(source_id) or next((block for block in self.originals
                if block["_user_image"]["source_id"] == source_id), None)

    def _load_source(self, original: dict) -> Image.Image:
        source_id = original["_user_image"]["source_id"]
        if self._source is not None and self._source_id == source_id:
            return self._source
        if self._source is not None:
            self._source.close()
            self._source = None
            self._source_id = None
        with BytesIO(base64.b64decode(original["data"], validate=True)) as buffer:
            source = Image.open(buffer)
            try:
                # Loads the first frame and avoids an unconditional full-image copy.
                ImageOps.exif_transpose(source, in_place=True)
            except BaseException:
                source.close()
                raise
        self._source, self._source_id = source, source_id
        return source

    def close(self):
        with self.lock:
            if self._source is not None:
                self._source.close()
                self._source = None
            self._source_id = None
            self.originals.clear()
            self._crops.clear()

    def crop(self, source_id: str, bbox: list[int]) -> tuple[dict, list[dict]]:
        with self.lock:
            original = next((block for block in self.originals
                             if block["_user_image"]["source_id"] == source_id), None)
            if original is None:
                return {"ok": False, "code": "unknown_image",
                        "error": "source_id 不可用，请使用最近含图用户消息的 user_image_original 标记中的原图编号",
                        "available_images": [{key: block["_user_image"][key] for key in ("source_id", "width", "height")}
                                             for block in self.originals]}, []
            metadata = original["_user_image"]
            width, height = metadata["width"], metadata["height"]
            if (not isinstance(bbox, list) or len(bbox) != 4 or any(type(value) is not int for value in bbox)
                    or not (0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height)):
                return {"ok": False, "code": "invalid_bbox", "source_id": source_id,
                        "source_width": width, "source_height": height,
                        "error": "bbox 须为四个原图像素整数 [left, top, right, bottom]，满足 "
                                 "0≤left<right≤source_width、0≤top<bottom≤source_height；右下边界不包含，"
                                 "不是宽高或百分比。请修正坐标；未裁剪或自动调整区域"}, []
            try:
                source = self._load_source(original)
                if source.size != (width, height):
                    raise ValueError("source_dimensions_changed")
                with source.crop(tuple(bbox)) as crop:
                    block = _encode(crop)
                    crop_id = _crop_id(source_id, bbox, block["data"])
                    marker = {"source_id": source_id, "crop_id": crop_id, "bbox": list(bbox),
                              "width": crop.width, "height": crop.height}
                block["_crop_source"] = source_id
                block["_crop_image"] = marker
                block = self._crops.setdefault(crop_id, block)
                report = {"ok": True, "source_id": source_id, "crop_id": crop_id, "bbox": bbox,
                          "source_width": width, "source_height": height,
                          "crop_width": bbox[2] - bbox[0], "crop_height": bbox[3] - bbox[1],
                          "width": marker["width"], "height": marker["height"],
                          "crop_count": 1, "original_preserved": True}
                return report, [_marker("user_image_crop", marker), block]
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                return {"ok": False, "code": "crop_failed", "source_id": source_id,
                        "error": "裁剪或编码失败，原图保留；可缩小区域重试，或结合原图说明辨认限制"}, []
