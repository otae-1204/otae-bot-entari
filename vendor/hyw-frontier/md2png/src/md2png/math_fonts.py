"""Request-local Ziamath font with explicit, normalized Unicode fallbacks.

Only immutable font sources are cached. No global font registry is patched and
no formula, glyph selection or synthetic glyph ID survives a render request.
"""

import io
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from ziafont.fonttypes import BBox
from ziafont.glyph import SimpleGlyph
from ziafont.svgpath import Cubic, Lineto, Moveto, Point, Quad
from ziamath.mathfont import MathFont

from .fonts import Face, FontSet


@lru_cache(maxsize=12)
def _source(path: str, signature: tuple) -> TTFont:
    # BytesIO keeps the cache from retaining open file descriptors; the signature
    # makes font replacement observable. Callers serialize access to lazy tables.
    return TTFont(io.BytesIO(Path(path).read_bytes()), fontNumber=signature[-1], lazy=True)


def source(face: Face) -> TTFont:
    stat = Path(face.path).stat()
    return _source(
        face.path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, face.index)
    )


class OutlinePen(BasePen):
    """Convert FontTools outlines to Ziafont's geometry in math-font units."""

    def __init__(self, glyphset, factor):
        super().__init__(glyphset)
        self.factor = factor
        self.operators = []

    def point(self, xy):
        return Point(xy[0] * self.factor, xy[1] * self.factor)

    def _moveTo(self, point):
        self.operators.append(Moveto(self.point(point)))

    def _lineTo(self, point):
        self.operators.append(Lineto(self.point(point)))

    def _qCurveToOne(self, control, end):
        self.operators.append(Quad(self.point(control), self.point(end)))

    def _curveToOne(self, one, two, end):
        self.operators.append(Cubic(self.point(one), self.point(two), self.point(end)))

    def _closePath(self):
        pass  # SimpleGlyph closes each contour at the next move, and the last one.

    def _endPath(self):
        pass


class ReadingMathFont(MathFont):
    def __init__(self, size: float, spec: FontSet, budget):
        super().__init__(files("ziamath.fonts").joinpath("STIXTwoMath-Regular.ttf"), size)
        self.spec, self.budget = spec, budget
        self.fallback_glyphs = {}
        self.fallback_advances = {}

    def findglyph(self, char, variant):
        self.budget.check()
        glyph = super().findglyph(char, variant)
        if glyph.index:
            return glyph
        key = char, variant.bold, variant.italic
        if key in self.fallback_glyphs:
            return self.fallback_glyphs[key]
        faces = (
            (self.spec.bold, *self.spec.fallback_bold)
            if variant.bold
            else (self.spec.regular, *self.spec.fallback)
        )
        for face in dict.fromkeys(faces):
            font = source(face)
            name = (font.getBestCmap() or {}).get(ord(char))
            if name is None:
                continue
            location = (
                dict(zip((a.axisTag for a in font["fvar"].axes), face.variations, strict=True))
                if face.variations
                else None
            )
            glyphset = font.getGlyphSet(location=location)
            outline = glyphset[name]
            factor = self.info.layout.unitsperem / font["head"].unitsPerEm
            pen = OutlinePen(glyphset, factor)
            outline.draw(pen)
            bounds = BoundsPen(glyphset)
            outline.draw(bounds)
            x0, y0, x1, y1 = bounds.bounds or (0, 0, 0, 0)
            # IDs outside the base font cannot be mistaken for its GSUB variants.
            index = 0x10000 + len(self.fallback_advances)
            glyph = SimpleGlyph(
                index, pen.operators, BBox(x0 * factor, x1 * factor, y0 * factor, y1 * factor), self
            )
            self._glyphs[index] = glyph
            self.fallback_advances[index] = outline.width * factor
            self.fallback_glyphs[key] = glyph
            return glyph
        raise ValueError(f"Missing math glyph: U+{ord(char):04X}")

    def advance(self, glyph, glyph2=None):
        if glyph in self.fallback_advances:
            return self.fallback_advances[glyph]
        if glyph2 in self.fallback_advances:
            glyph2 = None
        return super().advance(glyph, glyph2)
