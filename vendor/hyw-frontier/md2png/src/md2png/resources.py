"""Offline, bounded resources. Network and arbitrary path loading do not exist here."""

import io
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from .model import Cancelled, Diagnostic, Limits, RenderError


class Budget:
    def __init__(self, limits: Limits, cancelled: Callable[[], bool] | None = None):
        self.limits = limits
        self.cancelled = cancelled
        self.deadline = time.monotonic() + limits.timeout

    def check(self, height: float = 0, width: int = 0, scale: int = 1) -> None:
        if self.cancelled and self.cancelled():
            raise Cancelled("Rendering cancelled")
        if time.monotonic() > self.deadline:
            raise RenderError("Rendering deadline exceeded")
        if height > self.limits.max_height or height * width * scale**2 > self.limits.max_pixels:
            raise RenderError("Canvas pixel/height budget exceeded")


@dataclass(frozen=True)
class Raster:
    key: str
    width: float
    height: float
    descent: float = 0


class Resources:
    def __init__(
        self,
        data: Mapping[str, bytes],
        budget: Budget,
        diagnostics: list[Diagnostic],
        scale: int,
        *,
        font_set=None,
    ):
        if len(data) > budget.limits.max_assets:
            raise RenderError("Asset count budget exceeded")
        self.data, self.budget, self.diagnostics, self.scale = data, budget, diagnostics, scale
        self.images: dict[str, Image.Image] = {}
        self._math: dict[tuple[str, float, str, bool], Raster | None] = {}
        self.font_set = font_set
        self._missing: set[str] = set()
        self._math_parser = None
        self._decoded_pixels = 0

    def reserve_pixels(self, pixels: int) -> None:
        if self._decoded_pixels + pixels > self.budget.limits.max_asset_pixels:
            raise RenderError("Combined decoded asset pixel budget exceeded")
        self._decoded_pixels += pixels

    def image(self, key: str, component: str) -> Raster | None:
        self.budget.check()
        internal_key = "image:" + key
        if internal_key in self.images:
            image = self.images[internal_key]
            return Raster(internal_key, image.width, image.height)
        if key not in self.data:
            if key not in self._missing:
                self._missing.add(key)
                self.diagnostics.append(
                    Diagnostic("asset-unavailable", f"Asset not supplied: {key}", component)
                )
            return None
        data = self.data[key]
        if len(data) > self.budget.limits.max_asset_bytes:
            raise RenderError(f"Asset byte budget exceeded: {key}")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    self.reserve_pixels(image.width * image.height)
                    self.images[internal_key] = ImageOps.exif_transpose(image).convert("RGBA")
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise RenderError(f"Invalid image {key}: {exc}") from exc
        return self.image(key, component)

    def math(
        self, text: str, size: float, color: str, component: str, *, display: bool = False
    ) -> Raster | None:
        self.budget.check()
        if len(text) > self.budget.limits.max_math_chars:
            raise RenderError("Formula character budget exceeded")
        cache_key = (text, size, color, display)
        if cache_key in self._math:
            return self._math[cache_key]
        if len(self._math) >= self.budget.limits.max_assets:
            raise RenderError("Formula count budget exceeded")
        try:
            from .math_raster import MathRasterizer

            if self._math_parser is None:
                self._math_parser = MathRasterizer(self.budget, self.scale, self.font_set)
        except ImportError:
            self.diagnostics.append(
                Diagnostic("math-unavailable", "Install md2png[math] for Ziamath", component)
            )
            self._math[cache_key] = None
            return None
        try:
            result = self._math_parser.render(text, size, color, display=display)
        except ValueError as exc:
            self.diagnostics.append(Diagnostic("math-unsupported", str(exc), component))
            self._math[cache_key] = None
            return None
        image = result.image
        self.reserve_pixels(image.width * image.height)
        key = f"math:{len(self._math)}"
        self.images[key] = image
        raster = Raster(
            key, image.width / self.scale, image.height / self.scale, result.descent / self.scale
        )
        self._math[cache_key] = raster
        return raster
