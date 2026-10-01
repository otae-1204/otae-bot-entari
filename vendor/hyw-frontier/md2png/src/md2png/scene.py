"""Measured display-list contract, reusable by card composers and diagnostics."""

import math
from dataclasses import dataclass, field
from typing import Literal

from PIL import ImageColor

from .fonts import Face
from .model import Diagnostic, RenderError


@dataclass(frozen=True)
class Theme:
    name: str = "modern-light"
    page: str = "#eef0f3"
    paper: str = "#ffffff"
    ink: str = "#242b38"
    muted: str = "#748091"
    accent: str = "#3566c9"
    border: str = "#e2e7ee"
    subtle: str = "#f5f7fa"
    quote: str = "#f1f5fc"
    code_bg: str = "#171f2e"
    code_ink: str = "#dae2ef"
    code_muted: str = "#8996ad"
    margin: int = 24
    padding: int = 44
    body_size: int = 20
    code_size: int = 16
    line_height: float = 1.65
    gap: int = 18
    radius: int = 10
    headings: tuple[int, ...] = (38, 29, 24, 22, 20, 18)

    def __post_init__(self) -> None:
        if len(self.headings) != 6 or any(
            type(size) is not int or not 8 <= size <= 128
            for size in (self.body_size, self.code_size, *self.headings)
        ):
            raise RenderError("Theme requires six heading sizes and font sizes in 8..128")
        if not math.isfinite(self.line_height) or not 1 <= self.line_height <= 3:
            raise RenderError("Theme line_height must be in 1..3")
        if any(
            type(n) is not int or not 0 <= n <= 512
            for n in (self.margin, self.padding, self.gap, self.radius)
        ):
            raise RenderError("Theme spacing must be integer pixels in 0..512")
        for color in (
            self.page,
            self.paper,
            self.ink,
            self.muted,
            self.accent,
            self.border,
            self.subtle,
            self.quote,
            self.code_bg,
            self.code_ink,
            self.code_muted,
        ):
            try:
                ImageColor.getrgb(color)
            except ValueError as exc:
                raise RenderError(f"Invalid theme color: {color}") from exc


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Op:
    kind: Literal["rect", "text", "line", "image", "shadow"]
    box: Box
    component: str
    layer: Literal["background", "panels", "content", "decoration"] = "content"
    fill: str = ""
    radius: float = 0
    stroke: str = ""
    stroke_width: float = 1
    text: str = ""
    face: Face | None = None
    size: float = 20
    baseline: float = 0
    asset: str = ""
    tracking: float = 0
    blur: float = 7


@dataclass(frozen=True)
class LineRecord:
    component: str
    box: Box
    baseline: float
    text: str
    hard_break: bool = False
    logical_line: int | None = None


@dataclass(frozen=True)
class Component:
    id: str
    kind: str
    box: Box
    source: tuple[int, int] | None = None


@dataclass
class Scene:
    width: int
    height: int = 0
    scale: int = 2
    ops: list[Op] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    lines: list[LineRecord] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
