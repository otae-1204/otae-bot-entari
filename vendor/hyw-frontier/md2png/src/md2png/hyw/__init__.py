"""HYw working-tree style profile; no browser, golden, or DOM input at runtime."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from ..fonts import FontSet
from ..inspection import overlay, write_layout
from ..model import Limits
from ..painter import paint
from ..resources import Budget, Resources
from ..scene import Scene, Theme
from .art import Art
from .document import HywDocument, from_payload
from .font_assets import HywFontAssets
from .fonts import HywFonts
from .layout import HywLayout
from .text import Flow


@dataclass
class HywResult:
    image: Image.Image
    scene: Scene
    document: HywDocument


def render(
    payload: Mapping,
    *,
    assets: Mapping[str, bytes] | None = None,
    scale: int = 2,
    limits: Limits = Limits(timeout=60),
    debug: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
    pingfang: Path | None = None,
    font_set: FontSet | None = None,
) -> HywResult:
    if type(scale) is not int or scale not in (1, 2, 3):
        raise ValueError("scale must be 1, 2 or 3")
    return render_document(
        from_payload(payload, limits),
        assets=assets,
        scale=scale,
        limits=limits,
        debug=debug,
        cancelled=cancelled,
        pingfang=pingfang,
        font_set=font_set,
    )


def render_document(
    document: HywDocument,
    *,
    assets: Mapping[str, bytes] | None = None,
    scale: int = 1,
    limits: Limits = Limits(timeout=60),
    debug: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
    pingfang: Path | None = None,
    font_set: FontSet | None = None,
    reject_overflow: bool = False,
    font_assets: HywFontAssets | None = None,
    timings: dict[str, float] | None = None,
) -> HywResult:
    """Paint an already adapted document without legacy prefix/reference stripping.

    reject_overflow enables emergency grapheme wrapping before enforcing bounds;
    the default retains legacy CSS overflow for visual comparison only.
    """
    if type(scale) is not int or scale not in (1, 2, 3):
        raise ValueError("scale must be 1, 2 or 3")
    budget = Budget(limits, cancelled)
    budget.check()
    scene = Scene(width=840, scale=scale)
    started = time.perf_counter()
    fonts = HywFonts(scale, scene.diagnostics, pingfang, font_set, assets=font_assets)
    if timings is not None:
        timings["font_ms"] = round((time.perf_counter() - started) * 1000, 2)
    resources = Resources(assets or {}, budget, scene.diagnostics, scale, font_set=font_set)
    flow = Flow(fonts, resources, scene, document.accent, wrap_long_words=reject_overflow)
    channels = [int(document.accent[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    if sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)) > 0.4:
        flow.header_color = "#1f2937"
    started = time.perf_counter()
    HywLayout(flow, Art(resources)).document(document)
    if timings is not None:
        timings["layout_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if reject_overflow and any(d.code == "source-overflow" for d in scene.diagnostics):
        from ..model import RenderError

        raise RenderError("Unbreakable content exceeds canvas width budget")
    theme = Theme(page="#f2f2f2")
    started = time.perf_counter()
    image = paint(scene, fonts, resources, theme, budget, debug / "frames" if debug else None)
    if timings is not None:
        timings["paint_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if debug:
        (debug / "ast.json").write_text(
            json.dumps(asdict(document), ensure_ascii=False, indent=2) + "\n"
        )
        write_layout(scene, fonts, debug / "layout.json")
        overlay(image, scene, debug / "layout-overlay.png")
    return HywResult(image, scene, document)


__all__ = ["HywResult", "render", "render_document"]
