"""One request owns its fonts, budget, assets and scene; no mutable renderer singleton."""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from .fonts import Fonts, FontSet
from .inspection import overlay, write_layout
from .layout import Layout
from .model import Document, Limits, RenderError
from .painter import paint
from .parser import parse
from .resources import Budget, Resources
from .scene import Scene, Theme
from .typography import Typography


@dataclass
class RenderResult:
    image: Image.Image
    scene: Scene
    document: Document


def render(
    markdown: str,
    *,
    width: int = 840,
    scale: int = 2,
    theme: Theme = Theme(),
    fonts: FontSet | None = None,
    assets: Mapping[str, bytes] | None = None,
    limits: Limits = Limits(),
    cancelled: Callable[[], bool] | None = None,
    debug: Path | None = None,
) -> RenderResult:
    if type(width) is not int or not 240 <= width <= 4096:
        raise RenderError("width must be an integer in 240..4096")
    if type(scale) is not int or scale not in {1, 2, 3}:
        raise RenderError("scale must be 1, 2 or 3; output width is unchanged")
    budget = Budget(limits, cancelled)
    budget.check()
    document = parse(markdown, limits)
    scene = Scene(width=width, scale=scale)
    font_engine = Fonts(fonts or FontSet.system(), scale, scene.diagnostics)
    resources = Resources(assets or {}, budget, scene.diagnostics, scale, font_set=fonts)
    typography = Typography(font_engine, resources, theme, scene)
    Layout(typography, scene, theme, budget).document(document)
    image = paint(scene, font_engine, resources, theme, budget, debug / "frames" if debug else None)
    if debug:
        (debug / "ast.json").write_text(
            json.dumps(asdict(document), ensure_ascii=False, indent=2) + "\n"
        )
        write_layout(scene, font_engine, debug / "layout.json")
        overlay(image, scene, debug / "layout-overlay.png")
    return RenderResult(image, scene, document)
