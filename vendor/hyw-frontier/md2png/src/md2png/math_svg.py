"""Rasterize only Ziamath's local vector vocabulary, not arbitrary model SVG.

No CSS, text, images, external references, filters or file/network resolution.
Unknown primitives fail visibly rather than being silently omitted.
"""

import math

import aggdraw
from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path
from PIL import Image, ImageColor

from .model import RenderError


class PathPen(BasePen):
    def __init__(self):
        super().__init__(None)
        self.path = aggdraw.Path()

    def _moveTo(self, xy):
        self.path.moveto(*xy)

    def _lineTo(self, xy):
        self.path.lineto(*xy)

    def _curveToOne(self, one, two, end):
        self.path.curveto(*one, *two, *end)

    def _closePath(self):
        self.path.close()

    def _endPath(self):
        pass


def number(value):
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1_000_000:
        raise ValueError("Invalid formula vector coordinate")
    return result


def rasterize(svg, budget, scale):
    """Return an RGBA image and baseline ascent in physical pixels."""
    vx, vy, width, height = (number(v) for v in svg.get("viewBox").split())
    if width <= 0 or height <= 0:
        raise ValueError("Invalid formula bounds")
    size = math.ceil(width * scale), math.ceil(height * scale)
    # Include antialiasing workspace in the bound, before any Pillow allocation.
    if size[0] * size[1] * 4 > budget.limits.max_asset_pixels:
        raise RenderError("Formula pixel budget exceeded")
    image = Image.new("RGBA", (size[0] * 2, size[1] * 2))
    draw = aggdraw.Draw(image)
    symbols = {e.get("id"): e for e in svg.iter("symbol")}
    factor = scale * 2
    count = 0

    def visit(element, sx, sy, tx, ty, fill="black", stroke="none", depth=0):
        nonlocal count
        count += 1
        budget.check()
        if count > budget.limits.max_nodes * 8 or depth > budget.limits.max_depth:
            raise RenderError("Formula vector complexity budget exceeded")
        tag = element.tag
        if tag in {"symbol", "title"}:
            return
        if any(
            key
            not in {
                "x",
                "y",
                "width",
                "height",
                "cx",
                "cy",
                "rx",
                "ry",
                "d",
                "fill",
                "stroke",
                "stroke-width",
                "href",
                "id",
                "viewBox",
                "xmlns",
                "xmlns:xlink",
            }
            for key in element.attrib
        ):
            raise ValueError(f"Unsupported formula SVG attribute on {tag}")
        fill, stroke = element.get("fill", fill), element.get("stroke", stroke)
        if tag in {"svg", "g"}:
            for child in element:
                visit(child, sx, sy, tx, ty, fill, stroke, depth + 1)
            return
        if tag == "use":
            href = element.get("href", "")
            if not href.startswith("#") or href[1:] not in symbols:
                raise ValueError("Only local formula glyph references are allowed")
            symbol = symbols[href[1:]]
            if any(child.tag != "path" for child in symbol):
                raise ValueError("Formula glyph must contain only paths")
            bx, by, bw, bh = (number(v) for v in symbol.get("viewBox").split())
            w, h = number(element.get("width")), number(element.get("height"))
            if w == 0 or h == 0:  # A space has an advance but no painted outline.
                return
            if min(w, h, bw, bh) <= 0:
                raise ValueError("Invalid formula glyph bounds")
            # Ziafont emits equal-aspect viewports; implement SVG's default meet.
            zoom = min(w / bw, h / bh)
            x = number(element.get("x", 0)) + (w - bw * zoom) / 2 - bx * zoom
            y = number(element.get("y", 0)) + (h - bh * zoom) / 2 - by * zoom
            for child in symbol:
                visit(
                    child, sx * zoom, sy * zoom, tx + sx * x, ty + sy * y, fill, stroke, depth + 1
                )
            return
        brush = None if fill == "none" else aggdraw.Brush(ImageColor.getrgb(fill))
        pen = (
            None
            if stroke == "none"
            else aggdraw.Pen(
                ImageColor.getrgb(stroke),
                number(element.get("stroke-width", 1)) * max(abs(sx), abs(sy)),
            )
        )
        if tag == "path":
            path = element.get("d", "")
            if len(path) > budget.limits.max_math_chars * 2000:
                raise RenderError("Formula outline byte budget exceeded")
            target = PathPen()
            parse_path(path, TransformPen(target, (sx, 0, 0, sy, tx, ty)))
            draw.path(target.path, brush, pen)
        elif tag in {"rect", "ellipse"}:
            if tag == "ellipse":
                x, y = number(element.get("cx", 0)), number(element.get("cy", 0))
                rx, ry = number(element.get("rx", 0)), number(element.get("ry", 0))
                box = (
                    tx + sx * (x - rx),
                    ty + sy * (y - ry),
                    tx + sx * (x + rx),
                    ty + sy * (y + ry),
                )
                if min(rx, ry) < 0:
                    raise ValueError("Negative formula ellipse")
                draw.ellipse(box, brush, pen)
            else:
                x, y = number(element.get("x", 0)), number(element.get("y", 0))
                w, h = number(element.get("width", 0)), number(element.get("height", 0))
                if min(w, h) < 0:
                    raise ValueError("Negative formula rectangle")
                if number(element.get("rx", 0)) or number(element.get("ry", 0)):
                    raise ValueError("Rounded formula boxes are not supported")
                draw.rectangle(
                    (tx + sx * x, ty + sy * y, tx + sx * (x + w), ty + sy * (y + h)), brush, pen
                )
        else:
            raise ValueError(f"Unsupported formula SVG primitive: {tag}")

    visit(svg, factor, factor, -vx * factor, -vy * factor)
    draw.flush()
    budget.check()
    return image.resize(size, Image.Resampling.LANCZOS), -vy * scale
