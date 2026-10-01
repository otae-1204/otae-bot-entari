"""Semantic document types. No Pillow objects, filesystem IO or theme decisions."""

import math
from dataclasses import dataclass, fields
from typing import Literal


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: str = ""
    color: str = ""
    underline: bool = False
    citation: bool = False


@dataclass(frozen=True)
class Inline:
    text: str = ""
    style: Style = Style()
    kind: Literal["text", "break", "math", "image"] = "text"
    target: str = ""
    display: bool = False  # Math display style; inline formulas use compact fractions/limits.


BlockKind = Literal[
    "paragraph",
    "heading",
    "quote",
    "list",
    "item",
    "code",
    "summary",
    "rule",
    "table",
    "row",
    "cell",
    "math",
    "gallery",
]


@dataclass(frozen=True)
class Block:
    id: str
    kind: BlockKind
    children: tuple["Block", ...] = ()
    inlines: tuple[Inline, ...] = ()
    text: str = ""
    level: int = 0
    language: str = ""
    ordered: bool = False
    start: int = 1
    tight: bool = False
    header: bool = False
    align: str = "left"
    source: tuple[int, int] | None = None
    checked: bool | None = None


@dataclass(frozen=True)
class Document:
    children: tuple[Block, ...]


class RenderError(ValueError):
    """Visible, actionable failure; callers must not silently replace the result."""


class Cancelled(RenderError):
    pass


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    component: str = ""


@dataclass(frozen=True)
class Limits:
    max_chars: int = 100_000
    max_nodes: int = 12_000
    max_depth: int = 24
    max_height: int = 24_000
    max_pixels: int = 48_000_000  # includes supersampling
    max_asset_bytes: int = 8_000_000
    max_asset_pixels: int = 12_000_000
    max_assets: int = 64
    max_math_chars: int = 2_000
    timeout: float = 20.0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "timeout":
                if not math.isfinite(value) or value <= 0:
                    raise RenderError("timeout must be positive and finite")
            elif type(value) is not int or value <= 0:
                raise RenderError(f"{item.name} must be a positive integer")
        if self.max_depth > 64:
            raise RenderError("max_depth must not exceed 64")
