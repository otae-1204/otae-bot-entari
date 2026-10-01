"""Explicit reusable HYw font assets; never stores request text or mutable FT faces.

Owned by one sequential render worker. Each render creates fresh diagnostics,
shaping results and FreeType/HarfBuzz handles. A changed font invalidates the whole
asset set at the next request boundary, including lazy font bytes and metrics.
"""

import sys
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont

from ..fonts import Face, Fonts, FontSet
from ..model import RenderError


class HywFontAssets:
    def __init__(self, pingfang: Path | None = None, font_set: FontSet | None = None):
        self.portable = font_set is not None or sys.platform != "darwin"
        self.pingfang = ""
        if self.portable:
            if pingfang is not None:
                raise ValueError(
                    "pingfang is only supported by the macOS compatibility font profile"
                )
            self.spec = font_set or FontSet.bundled()
            self.sf, self.sf_italic, self.menlo = (
                self.spec.regular.path,
                self.spec.italic.path,
                self.spec.mono.path,
            )
        else:
            root = Path("/System/Library/Fonts")
            if pingfang is None:
                candidates = sorted(
                    Path("/System/Library/AssetsV2").glob(
                        "com_apple_MobileAsset_Font*/*.asset/AssetData/PingFang.ttc"
                    )
                )
                if not candidates:
                    raise RenderError("HYw parity needs PingFang SC; provide pingfang=Path(...)")
                pingfang = candidates[-1]
            self.sf, self.sf_italic, self.menlo = (
                str(root / name) for name in ("SFNS.ttf", "SFNSItalic.ttf", "Menlo.ttc")
            )
            self.pingfang = str(pingfang)
            self.spec = FontSet(
                Face(self.sf),
                Face(self.sf),
                Face(self.sf_italic),
                Face(self.sf_italic),
                Face(self.menlo),
                Face(self.menlo, 1),
                (Face(self.pingfang, 3), Face(self.pingfang, 7), *FontSet.bundled().fallback),
                (Face(self.pingfang, 11), *FontSet.bundled().fallback_bold),
            )
        spec = self.spec
        paths = {
            face.path
            for face in (
                spec.regular,
                spec.bold,
                spec.italic,
                spec.bold_italic,
                spec.mono,
                spec.mono_bold,
                spec.mono_italic,
                spec.mono_bold_italic,
                *spec.fallback,
                *spec.fallback_bold,
            )
            if face is not None
        }
        self.signatures = {path: self._signature(path) for path in paths}
        # The base constructor owns coverage decoding; reuse exactly the same cmap policy.
        self.coverage = Fonts(self.spec, 1, [])._coverage
        self.trak = {}
        if not self.portable:
            with TTFont(self.sf, lazy=True) as source:
                self.trak = dict(source["trak"].horizData[0.0])
        self._blobs: dict[str, hb.Blob] = {}
        self._axes: dict[tuple[str, int], tuple[str, ...]] = {}
        self._metrics: dict[tuple[str, int], tuple[int, int, int]] = {}
        # Immutable glyph metrics only; no answer strings or raster masks survive requests.
        self.glyph_extents: dict[tuple[Face, int], hb.GlyphExtents | None] = {}
        if not self.unchanged():
            raise RenderError("Font files changed during preparation")

    @staticmethod
    def _signature(path: str) -> tuple:
        info = Path(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def unchanged(self) -> bool:
        try:
            return all(
                self._signature(path) == signature for path, signature in self.signatures.items()
            )
        except OSError:
            return False

    def coverage_for(self, face: Face) -> frozenset[int]:
        if face not in self.coverage:
            with TTFont(face.path, fontNumber=face.index, lazy=True) as font:
                self.coverage[face] = frozenset((font.getBestCmap() or {}).keys())
        return self.coverage[face]

    def font_blob(self, path: str) -> hb.Blob:
        if path not in self._blobs:
            # One owned blob per FILE, not a 75MB TTC copy per face/weight/request.
            # Do not mmap replaceable files: the blob must remain an immutable snapshot.
            self._blobs[path] = hb.Blob(Path(path).read_bytes())
        return self._blobs[path]

    def axes(self, face: Face) -> tuple[str, ...]:
        key = face.path, face.index
        if key not in self._axes:
            with TTFont(face.path, fontNumber=face.index, lazy=True) as source:
                self._axes[key] = tuple(axis.axisTag for axis in source["fvar"].axes)
        return self._axes[key]

    def metrics(self, mono: bool) -> tuple[int, int, int]:
        face = (
            (self.spec.mono if mono else self.spec.regular)
            if self.portable
            else Face(self.menlo if mono else self.sf)
        )
        key = face.path, face.index
        if key not in self._metrics:
            with TTFont(face.path, fontNumber=face.index, lazy=True) as font:
                self._metrics[key] = (
                    font["head"].unitsPerEm,
                    font["hhea"].ascent,
                    font["hhea"].descent,
                )
        return self._metrics[key]
