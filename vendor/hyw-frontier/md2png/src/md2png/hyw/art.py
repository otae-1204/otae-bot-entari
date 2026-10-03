"""Render original MDI vector paths and supplied image assets, never screenshot crops."""

import base64
import binascii
import json
from pathlib import Path
from xml.etree import ElementTree

import aggdraw
from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path
from PIL import Image

from ..model import RenderError
from ..resources import Raster, Resources


class IconPen(BasePen):
    def __init__(self):
        super().__init__(None)
        self.path = aggdraw.Path()

    def _moveTo(self, point):
        self.path.moveto(*point)

    def _lineTo(self, point):
        self.path.lineto(*point)

    def _curveToOne(self, one, two, three):
        self.path.curveto(*one, *two, *three)

    def _closePath(self):
        self.path.close()

    def _endPath(self):
        pass


class Art:
    def __init__(self, resources: Resources):
        self.resources = resources
        self.icons = json.loads((Path(__file__).parent / "assets/icons.json").read_text())
        self.icon_cache = {}

    def icon(self, name: str, color: str, size: float = 21) -> str:
        key = f"icon:{name}:{color}:{size}"
        if key in self.resources.images:
            return key
        scale = self.resources.scale
        pixels = round(size * scale)
        # Supersample small vectors so diagonal strokes remain clean at scale=1.
        canvas = Image.new("RGBA", (pixels * 3, pixels * 3))
        drawing = aggdraw.Draw(canvas)
        entry = (
            {
                "width": 24,
                "body": (
                    '<path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7'
                    "A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2zm9-6h-4v1.9h4a3.1 3.1 0 0 1 0 6.2h-4"
                    'V17h4a5 5 0 0 0 0-10z"/>'
                ),
            }
            if name == "link"
            else {"width": 24, "body": '<path d="M4 12l5 5L21 5l-2-2L9 13l-3-3z"/>'}
            if name == "check"
            else self.icons["icons"][name]
        )
        body = ElementTree.fromstring("<svg>" + entry["body"] + "</svg>")
        ratio = pixels * 3 / entry.get("width", self.icons["width"])
        for element in body:
            if element.tag != "path":
                raise RenderError(f"Unsupported icon primitive {element.tag}")
            pen = IconPen()
            parse_path(element.attrib["d"], TransformPen(pen, (ratio, 0, 0, ratio, 0, 0)))
            drawing.path(pen.path, aggdraw.Brush(color))
        drawing.flush()
        self.resources.images[key] = canvas.resize((pixels, pixels), Image.Resampling.LANCZOS)
        return key

    def image(self, source: str, component: str) -> Raster | None:
        if source in self.resources.data:
            return self.resources.image(source, component)
        if source.startswith(("http:", "https:", "//", "file:")):
            return self.resources.image(source, component)
        text = (
            source.split(",", 1)[1]
            if source.startswith("data:image/") and "," in source
            else source
        )
        if len(text) > self.resources.budget.limits.max_asset_bytes * 4 / 3 + 8:
            raise RenderError("Embedded image byte budget exceeded")
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RenderError("Invalid embedded image") from exc
        if len(self.resources.data) >= self.resources.budget.limits.max_assets:
            raise RenderError("Asset count budget exceeded")
        self.resources.data = {**self.resources.data, source: raw}
        return self.resources.image(source, component)
