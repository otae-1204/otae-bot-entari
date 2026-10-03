"""The measured macOS HYw font cascade. HarfBuzz advances, FreeType glyph masks.

Pillow's wheel on this host has no RAQM; BASIC integer advances cannot reproduce
Chrome. Shaping is explicit instead of 'correcting' text widths from golden PNGs.
"""

import ctypes
from pathlib import Path

import freetype
import uharfbuzz as hb
from PIL import Image, ImageColor

from ..fonts import Face, Fonts, FontSet
from ..model import Diagnostic, RenderError, Style
from .font_assets import HywFontAssets


class HywFonts(Fonts):
    def __init__(
        self,
        scale: int,
        diagnostics: list[Diagnostic],
        pingfang: Path | None = None,
        font_set: FontSet | None = None,
        *,
        assets: HywFontAssets | None = None,
    ):
        if assets is not None and (
            (font_set is not None and font_set != assets.spec)
            or (pingfang is not None and str(pingfang) != assets.pingfang)
        ):
            raise ValueError("Prepared font assets do not match the requested font profile")
        self.assets = assets or HywFontAssets(pingfang, font_set)
        if not self.assets.unchanged():
            raise RenderError("Prepared font assets changed; rebuild before rendering")
        self.portable = self.assets.portable
        self.sf, self.sf_italic = self.assets.sf, self.assets.sf_italic
        self.menlo, self.pingfang = self.assets.menlo, self.assets.pingfang
        self.trak = self.assets.trak
        super().__init__(self.assets.spec, scale, diagnostics, coverage=self.assets.coverage)
        self._init_shaping()

    def _init_shaping(self):
        self._hb: dict[Face, hb.Font] = {}
        self._ft: dict[Face, freetype.Face] = {}
        self._shapes: dict[tuple[str, Face, float], tuple] = {}
        self._choices: dict[tuple, Face] = {}
        self._ink_bounds: dict[tuple, tuple[float, float, float, float]] = {}
        self._numeral_ink: dict[tuple, tuple[Image.Image, int, int]] = {}

    def choose(
        self,
        text: str,
        css_size: float,
        weight: int = 400,
        italic: bool = False,
        mono: bool = False,
    ) -> Face:
        if self.portable:
            return self.select(text, Style(bold=weight >= 600, italic=italic, code=mono))
        key = text, css_size, weight, italic, mono
        if key in self._choices:
            return self._choices[key]
        if mono:
            face = Face(self.menlo, (1 if weight >= 600 else 0) + (2 if italic else 0))
        elif italic:
            face = Face(self.sf_italic, variations=(min(28, max(17, css_size)), 400, weight))
        else:
            face = Face(self.sf, variations=(100, min(96, max(17, css_size)), 400, weight))
        primary_cmap = self._coverage.get(Face(face.path, face.index))
        if primary_cmap is None:
            primary_cmap = self.assets.coverage_for(Face(face.path, face.index))
            self._coverage[Face(face.path, face.index)] = primary_cmap
        needed = {ord(c) for c in text if c not in "\u200d\ufe0f\ufe0e"}
        if not needed <= primary_cmap:
            index = 11 if weight >= 600 else 7 if weight >= 500 else 3
            face = Face(self.pingfang, index, slant=0.21256 if italic else 0)
            cmap = self._coverage[Face(self.pingfang, index)]
            if not needed <= cmap:
                cascade = self.spec.fallback_bold if weight >= 600 else self.spec.fallback
                face = next((f for f in cascade if needed <= self._coverage[f]), face)
                cmap = self._coverage.get(face, cmap)
            if not needed <= cmap and text not in self._warned:
                self._warned.add(text)
                self.diagnostics.append(
                    Diagnostic("missing-glyph", f"HYw cascade does not cover {text!r}")
                )
        self.used.add(face)
        self._choices[key] = face
        return face

    def covers(self, text: str, face: Face) -> bool:
        needed = {ord(c) for c in text if c not in "\u200d\ufe0f\ufe0e"}
        return needed <= self.assets.coverage_for(Face(face.path, face.index))

    def hb_font(self, face: Face) -> hb.Font:
        if face not in self._hb:
            font = hb.Font(hb.Face(self.assets.font_blob(face.path), face.index))
            font.scale = (font.face.upem * 64, font.face.upem * 64)
            if face.variations:
                font.set_variations(dict(zip(self.assets.axes(face), face.variations, strict=True)))
            self._hb[face] = font
        return self._hb[face]

    def shape(self, text: str, face: Face, size: float) -> tuple:
        key = (text, face, size)
        if key not in self._shapes:
            font = self.hb_font(face)
            buffer = hb.Buffer()
            buffer.add_str(text)
            buffer.guess_segment_properties()
            features = {"kern": True, "chws": True, "tnum": face.tabular}
            if not self.portable and face.path == self.pingfang:
                # Blink's text-spacing-trim fallback for fonts without `chws`.
                # `halt` comes from PingFang's GPOS table, including glyph offsets.
                opening = set("（［｛〈《「『【〔〖〘〚")
                closing = set("）］｝〉》」』】〕〗〙〛、。，．：；")
                ranges = [
                    (i, i + 1, True)
                    for i in range(len(text) - 1)
                    if (text[i] in closing and text[i + 1] in closing)
                    or (text[i] in opening and text[i + 1] in opening | closing)
                ]
                if ranges:
                    features["halt"] = ranges
            hb.shape(font, buffer, features)
            ratio = size / (font.face.upem * 64)
            tracking = 0
            if not self.portable and face.path in {self.sf, self.sf_italic}:
                # CoreText automatically applies the font's AAT `trak` table. HarfBuzz's
                # OT shaper does not. Read the table, never fit per-string corrections.
                css_size = face.variations[0 if face.path == self.sf_italic else 1]
                sizes = sorted(self.trak)
                lo = max((s for s in sizes if s <= css_size), default=sizes[0])
                hi = min((s for s in sizes if s >= css_size), default=sizes[-1])
                value = (
                    self.trak[lo]
                    if lo == hi
                    else (
                        self.trak[lo]
                        + (self.trak[hi] - self.trak[lo]) * (css_size - lo) / (hi - lo)
                    )
                )
                tracking = value * size / font.face.upem
            self._shapes[key] = tuple(
                (
                    info.codepoint,
                    info.cluster,
                    pos.x_advance * ratio + tracking,
                    pos.x_offset * ratio,
                    pos.y_offset * ratio,
                )
                for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions, strict=True)
            )
        return self._shapes[key]

    def width(self, text: str, face: Face, size: float) -> float:
        return sum(glyph[2] for glyph in self.shape(text, face, size))

    def ink_bounds(self, text: str, face: Face, size: float) -> tuple[float, float, float, float]:
        """Visible outline bounds relative to the baseline, without rasterizing glyphs."""
        key = text, face, size
        if key not in self._ink_bounds:
            font = self.hb_font(face)
            ratio = size / (font.face.upem * 64)
            cursor = 0.0
            bounds = []
            for glyph, _, advance, offset_x, offset_y in self.shape(text, face, size):
                metric_key = face, glyph
                if metric_key not in self.assets.glyph_extents:
                    self.assets.glyph_extents[metric_key] = font.get_glyph_extents(glyph)
                extent = self.assets.glyph_extents[metric_key]
                if extent is not None and extent.width and extent.height:
                    left = cursor + offset_x + extent.x_bearing * ratio
                    top = -offset_y - extent.y_bearing * ratio
                    bounds.append(
                        (left, top, left + extent.width * ratio, top - extent.height * ratio)
                    )
                cursor += advance
            self._ink_bounds[key] = (
                (
                    min(b[0] for b in bounds),
                    min(b[1] for b in bounds),
                    max(b[2] for b in bounds),
                    max(b[3] for b in bounds),
                )
                if bounds
                else (0, -size, cursor, 0)
            )
        return self._ink_bounds[key]

    def css_metrics(self, size: float, mono: bool = False) -> tuple[float, float]:
        # Font-only metrics survive requests; no lru_cache retaining old self/diagnostics.
        em, ascent, descent = self.assets.metrics(mono)
        return round(ascent * size / em), round(-descent * size / em)

    def draw_text(
        self,
        canvas: Image.Image,
        x: float,
        baseline: float,
        text: str,
        face: Face,
        size: float,
        fill: str,
        tracking: float = 0,
    ) -> None:
        if face not in self._ft:
            ft = freetype.Face(face.path, index=face.index)
            if face.variations:
                ft.set_var_design_coords(face.variations)
            self._ft[face] = ft
        ft = self._ft[face]
        scale = self.scale
        ft.set_char_size(0, round(size * scale * 64), 72, 72)
        color = ImageColor.getrgb(fill)
        cache_numerals = size <= 14 and text.isdecimal()
        for glyph_id, cluster, advance, offset_x, offset_y in self.shape(text, face, size):
            px, py = (x + offset_x) * scale, (baseline - offset_y) * scale
            ix, iy = int(px // 1), int(py // 1)
            dx, dy = round((px - ix) * 64), -round((py - iy) * 64)
            key = (face, size, color, glyph_id, dx, dy) if cache_numerals else None
            cached = self._numeral_ink.get(key) if cache_numerals else None
            if cached is not None:
                ink, left, top = cached
                canvas.alpha_composite(ink, (ix + left, iy - top))
                x += advance + tracking
                continue
            ft.set_transform(
                freetype.Matrix(65536, round(face.slant * 65536), 0, 65536),
                freetype.Vector(dx, dy),
            )
            ft.load_glyph(
                glyph_id,
                freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_BITMAP,
            )
            bitmap = ft.glyph.bitmap
            if bitmap.width and bitmap.rows:
                # bitmap.buffer builds a Python int list for EVERY pixel. Copy in C
                # while the FT glyph slot is alive, before the next load_glyph mutates it.
                if (
                    bitmap.pixel_mode != freetype.FT_PIXEL_MODE_GRAY
                    or bitmap.num_grays != 256
                    or bitmap.pitch < bitmap.width
                    or not bitmap._FT_Bitmap.buffer
                ):
                    raise RenderError("Unsupported FreeType glyph bitmap layout")
                pixels = ctypes.string_at(bitmap._FT_Bitmap.buffer, bitmap.rows * bitmap.pitch)
                mask = Image.frombytes(
                    "L", (bitmap.width, bitmap.rows), pixels, "raw", "L", bitmap.pitch
                )
                ink = Image.new("RGBA", mask.size, color)
                ink.putalpha(mask)
                # Repeated pixel-aligned citation digits reuse the already drawn glyph.
                # Request-local and phase-aware: no stale masks or extra raster passes.
                if cache_numerals:
                    self._numeral_ink[key] = (ink, ft.glyph.bitmap_left, ft.glyph.bitmap_top)
                canvas.alpha_composite(ink, (ix + ft.glyph.bitmap_left, iy - ft.glyph.bitmap_top))
            x += advance + tracking
