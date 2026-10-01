"""Render geometry, font provenance, and visual layout diagnostics."""

import json
import platform
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from PIL import Image, ImageDraw, features
from PIL import __version__ as pillow_version

from .fonts import Fonts
from .scene import Scene


def write_layout(scene: Scene, fonts: Fonts, path: Path) -> None:
    payload = {
        "schema": "md2png.scene.v1",
        "pillow": pillow_version,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "freetype": features.version("freetype2"),
            "raqm": features.version("raqm"),
            "packages": {
                name: version(name)
                for name in (
                    "markdown-it-py",
                    "mdit-py-plugins",
                    "Pygments",
                    "fonttools",
                    "regex",
                    "uniseg",
                )
            },
        },
        "fonts": fonts.manifest(),
        **asdict(scene),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def overlay(image: Image.Image, scene: Scene, path: Path) -> None:
    canvas = image.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    for component in scene.components:
        b = component.box
        draw.rectangle((b.x, b.y, b.x + b.width, b.y + b.height), outline="#e34b68", width=1)
    for line in scene.lines:
        b = line.box
        draw.line((b.x, line.baseline, b.x + b.width, line.baseline), fill="#26a38a", width=1)
    canvas.save(path)
