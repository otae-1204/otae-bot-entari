"""Explicit font cascade; selection never depends on unrelated document text."""

import hashlib
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, features

from .model import Diagnostic, RenderError, Style


@dataclass(frozen=True)
class Face:
    path: str
    index: int = 0
    variations: tuple[float, ...] = ()
    slant: float = 0
    tabular: bool = False


@dataclass(frozen=True)
class FontSet:
    regular: Face
    bold: Face
    italic: Face
    bold_italic: Face
    mono: Face
    mono_bold: Face
    fallback: tuple[Face, ...] = ()
    fallback_bold: tuple[Face, ...] = ()
    mono_italic: Face | None = None
    mono_bold_italic: Face | None = None

    @classmethod
    def load(cls, path: Path) -> "FontSet":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")), base=path.parent)

    @classmethod
    def from_dict(cls, data: dict, *, base: Path | None = None) -> "FontSet":
        """Decode the same manifest shape from an explicit in-memory configuration."""
        base = Path.cwd() if base is None else base

        def face(value: dict) -> Face:
            p = Path(value["path"]).expanduser()
            return Face(
                str((base / p).resolve()),
                int(value.get("index", 0)),
                tuple(value.get("variations", ())),
                float(value.get("slant", 0)),
                bool(value.get("tabular", False)),
            )

        return cls(
            **{
                key: (
                    tuple(face(x) for x in value)
                    if key.startswith("fallback")
                    else face(value)
                    if value is not None
                    else None
                )
                for key, value in data.items()
            }
        )

    @classmethod
    def bundled(cls) -> "FontSet":
        """Offline, redistributable fonts; identical files on all supported platforms."""
        root = Path(__file__).with_name("font_assets")
        sans, mono = str(root / "NotoSansSC.ttf"), str(root / "NotoSansMono.ttf")
        regular = Face(sans, variations=(400,))
        bold = Face(sans, variations=(700,))
        return cls(
            regular,
            bold,
            replace(regular, slant=0.2),
            replace(bold, slant=0.2),
            # fvar order is wght, wdth (not the alphabetical order in the upstream filename).
            Face(mono, variations=(400, 100)),
            Face(mono, variations=(700, 100)),
            (
                regular,
                Face(str(root / "NotoSansKR.ttf"), variations=(400,)),
                Face(str(root / "NotoEmoji.ttf"), variations=(400,)),
                Face(str(root / "BabelStoneHan.ttf")),
            ),
            (
                bold,
                Face(str(root / "NotoSansKR.ttf"), variations=(700,)),
                Face(str(root / "NotoEmoji.ttf"), variations=(700,)),
                Face(str(root / "BabelStoneHan.ttf")),
            ),
            Face(mono, variations=(400, 100), slant=0.2),
            Face(mono, variations=(700, 100), slant=0.2),
        )

    @classmethod
    def system(cls) -> "FontSet":
        # Small documented platform presets, not a scan/scoring heuristic.
        if sys.platform == "darwin":
            base = "/System/Library/Fonts/Supplemental/"
            cjk = "/System/Library/Fonts/Hiragino Sans GB.ttc"
            return cls(
                Face(base + "Arial.ttf"),
                Face(base + "Arial Bold.ttf"),
                Face(base + "Arial Italic.ttf"),
                Face(base + "Arial Bold Italic.ttf"),
                Face("/System/Library/Fonts/Menlo.ttc"),
                Face("/System/Library/Fonts/Menlo.ttc", 1),
                (Face(cjk),),
                (Face(cjk, 2),),
                Face("/System/Library/Fonts/Menlo.ttc", 2),
                Face("/System/Library/Fonts/Menlo.ttc", 3),
            )
        if sys.platform == "win32":
            base = "C:/Windows/Fonts/"
            return cls(
                Face(base + "arial.ttf"),
                Face(base + "arialbd.ttf"),
                Face(base + "ariali.ttf"),
                Face(base + "arialbi.ttf"),
                Face(base + "consola.ttf"),
                Face(base + "consolab.ttf"),
                (Face(base + "msyh.ttc"),),
                (Face(base + "msyhbd.ttc"),),
                Face(base + "consolai.ttf"),
                Face(base + "consolaz.ttf"),
            )
        base = "/usr/share/fonts/truetype/dejavu/"
        cjk = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
        bold = cjk.with_name("NotoSansCJK-Bold.ttc")
        return cls(
            Face(base + "DejaVuSans.ttf"),
            Face(base + "DejaVuSans-Bold.ttf"),
            Face(base + "DejaVuSans-Oblique.ttf"),
            Face(base + "DejaVuSans-BoldOblique.ttf"),
            Face(base + "DejaVuSansMono.ttf"),
            Face(base + "DejaVuSansMono-Bold.ttf"),
            (Face(str(cjk), 2),) if cjk.exists() else (),
            (Face(str(bold), 2),) if bold.exists() else (),
            Face(base + "DejaVuSansMono-Oblique.ttf"),
            Face(base + "DejaVuSansMono-BoldOblique.ttf"),
        )


class Fonts:
    def __init__(
        self,
        spec: FontSet,
        scale: int,
        diagnostics: list[Diagnostic],
        *,
        coverage: dict[Face, frozenset[int]] | None = None,
    ):
        self.spec, self.scale, self.diagnostics = spec, scale, diagnostics
        self._fonts: dict[tuple[Face, int], ImageFont.FreeTypeFont] = {}
        self._coverage: dict[Face, frozenset[int]] = {}
        self._selected: dict[tuple[str, Style], Face] = {}
        self.used: set[Face] = set()
        self._warned: set[str] = set()
        self.engine = (
            ImageFont.Layout.RAQM if features.check_feature("raqm") else ImageFont.Layout.BASIC
        )
        for face in self.faces:
            if not Path(face.path).is_file():
                raise RenderError(f"Missing font {face.path}; supply --fonts manifest.json")
            if coverage is not None and face in coverage:
                self._coverage[face] = coverage[face]
            else:
                with TTFont(face.path, fontNumber=face.index, lazy=True) as font:
                    self._coverage[face] = frozenset((font.getBestCmap() or {}).keys())

    @property
    def faces(self) -> tuple[Face, ...]:
        s = self.spec
        return tuple(
            dict.fromkeys(
                (
                    s.regular,
                    s.bold,
                    s.italic,
                    s.bold_italic,
                    s.mono,
                    s.mono_bold,
                    s.mono_italic or s.mono,
                    s.mono_bold_italic or s.mono_bold,
                    *s.fallback,
                    *s.fallback_bold,
                )
            )
        )

    def select(self, cluster: str, style: Style) -> Face:
        key = (cluster, style)
        if key in self._selected:
            return self._selected[key]
        s = self.spec
        primary = (
            (s.mono_bold if style.bold else s.mono)
            if style.code
            else (
                s.bold_italic
                if style.bold and style.italic
                else s.bold
                if style.bold
                else s.italic
                if style.italic
                else s.regular
            )
        )
        if style.code and style.italic:
            italic_face = s.mono_bold_italic if style.bold else s.mono_italic
            if italic_face is not None:
                primary = italic_face
            elif "mono-italic" not in self._warned:
                self._warned.add("mono-italic")
                self.diagnostics.append(
                    Diagnostic("mono-italic", "No italic monospace face configured")
                )
        cascade = (primary, *(s.fallback_bold if style.bold else s.fallback), *s.fallback)
        needed = {ord(c) for c in cluster if c not in "\u200d\ufe0f\ufe0e"}
        face = next((f for f in cascade if needed <= self._coverage[f]), primary)
        if not needed <= self._coverage[face] and cluster not in self._warned:
            self._warned.add(cluster)
            self.diagnostics.append(
                Diagnostic("missing-glyph", f"No configured face covers {cluster!r}")
            )
        if style.italic and face != primary and "fallback-italic" not in self._warned:
            self._warned.add("fallback-italic")
            self.diagnostics.append(
                Diagnostic("fallback-italic", "Fallback has no italic face; not synthesizing slant")
            )
        self.used.add(face)
        self._selected[key] = face
        return face

    def font(self, face: Face, size: int) -> ImageFont.FreeTypeFont:
        key = (face, size)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(
                face.path, size * self.scale, index=face.index, layout_engine=self.engine
            )
            if face.variations:
                self._fonts[key].set_variation_by_axes(list(face.variations))
        return self._fonts[key]

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
        if tracking:
            for char in text:
                ImageDraw.Draw(canvas).text(
                    (x * self.scale, baseline * self.scale),
                    char,
                    anchor="ls",
                    font=self.font(face, size),
                    fill=fill,
                )
                x += self.width(char, face, size) + tracking
        else:
            ImageDraw.Draw(canvas).text(
                (x * self.scale, baseline * self.scale),
                text,
                anchor="ls",
                font=self.font(face, size),
                fill=fill,
            )

    def width(self, text: str, face: Face, size: int) -> float:
        return self.font(face, size).getlength(text) / self.scale

    def metrics(self, face: Face, size: int) -> tuple[float, float]:
        a, d = self.font(face, size).getmetrics()
        return a / self.scale, d / self.scale

    def manifest(self) -> list[dict]:
        return [
            {
                **asdict(f),
                "sha256": hashlib.sha256(Path(f.path).read_bytes()).hexdigest(),
                "family": self.font(f, 16).getname(),
                "engine": self.engine.name,
            }
            for f in sorted(self.used, key=lambda f: (f.path, f.index))
        ]
