"""CSS-like inline flow for the HYw profile; all coordinates are output pixels."""

import math
from dataclasses import dataclass, replace

import regex

from ..fonts import Face
from ..model import Diagnostic, Inline, RenderError, Style
from ..resources import Resources
from ..scene import Box, LineRecord, Op, Scene
from ..typography import Atom, Typography
from .fonts import HywFonts


@dataclass(frozen=True)
class TextSpec:
    size: float = 20  # source CSS px, before zoom
    line_height: float = 28
    weight: int = 400
    italic: bool = False
    mono: bool = False
    tracking: float = 0
    uppercase: bool = False
    tabular: bool = False
    color: str = "#3a3a3c"
    wrap: str = "normal"


@dataclass(frozen=True)
class HRun:
    text: str
    face: Face | None
    style: Style
    size: float
    width: float
    ascent: float
    descent: float
    padding: float = 0
    asset: str = ""


@dataclass(frozen=True)
class HLine:
    runs: tuple[HRun, ...]
    width: float
    ascent: float
    height: float
    hard: bool = False


def q(value: float) -> float:
    return math.floor(value * 64 + 1e-7) / 64


def url_break_units(atoms: list[Atom]) -> list[list[Atom]]:
    """Prefer URL path/query boundaries, including slashes between numeric segments."""
    text = "".join(atom.text for atom in atoms)
    prefix = regex.match(r"https?://[^/?#]+/?", text)
    if prefix is None:
        return Typography.break_units(atoms)
    units, unit = [], []
    offset = index = 0
    while index < len(atoms):
        atom = atoms[index]
        # Keep each encoded byte intact even during emergency wrapping of long paths.
        escape = "".join(a.text for a in atoms[index:index + 3])
        if atom.text == "%" and regex.fullmatch(r"%[0-9a-fA-F]{2}", escape):
            atom = replace(atom, text=escape)
            index += 3
        else:
            index += 1
        unit.append(atom)
        offset += len(atom.text)
        if offset >= prefix.end() and (offset == prefix.end() or atom.text in "/?&#"):
            units.append(unit)
            unit = []
    if unit:
        units.append(unit)
    return units


class Flow:
    def __init__(
        self,
        fonts: HywFonts,
        resources: Resources,
        scene: Scene,
        accent: str,
        *,
        wrap_long_words: bool = False,
    ):
        self.fonts, self.resources, self.scene, self.accent = fonts, resources, scene, accent
        self.wrap_long_words = wrap_long_words
        self.header_color = "#ffffff"
        self._line_metrics: dict[tuple, tuple[float, float]] = {}

    def line_metrics(self, size: float, height: float, mono: bool) -> tuple[float, float]:
        key = size, height, mono
        if key not in self._line_metrics:
            a, d = self.fonts.css_metrics(size, mono)
            before = math.floor((height - a - d) / 2) + a
            self._line_metrics[key] = before, height - before
        return self._line_metrics[key]

    def atoms(
        self,
        spans: tuple[Inline, ...],
        spec: TextSpec,
        component: str,
        width: float,
        code: bool = False,
    ) -> list[Atom]:
        atoms = []
        for span in spans:
            self.resources.budget.check()
            style = span.style
            if span.kind == "break":
                atoms.append(Atom("\n", style, None, spec.size * 1.5))
                continue
            if span.kind == "math":
                raster = self.resources.math(
                    span.text, spec.size * 1.5 * 1.21, spec.color, component, display=span.display
                )
                if raster:
                    ratio = min(1, width / raster.width)
                    atoms.append(
                        Atom(
                            "\ufffc",
                            style,
                            None,
                            spec.size * 1.5,
                            raster.key,
                            raster.width * ratio,
                            (raster.height - raster.descent) * ratio,
                            raster.descent * ratio,
                        )
                    )
                    continue
            text = span.text.upper() if spec.uppercase else span.text
            if span.kind == "math":
                text = "[公式未渲染] " + span.text
            weight = 600 if style.bold else spec.weight
            color = style.color or ("#2c2c2e" if style.bold else spec.color)
            style = replace(style, color=color)
            css_size = spec.size * 0.85 if style.code and not code else spec.size
            mono = spec.mono or style.code
            if style.citation:
                size = max(10, min(14, spec.size * 0.7))
                face = self.fonts.choose(text, size / 1.5, 600)
                badge_width = max(size + 4, self.fonts.width(text, face, size) + 6)
                atoms.append(Atom(text, style, face, size, width=badge_width + 3))
                continue
            for char in regex.findall(r"\X", text):
                if char == "\n":
                    atoms.append(Atom("\n", style, None, css_size * 1.5))
                    continue
                face = self.fonts.choose(char, css_size, weight, spec.italic or style.italic, mono)
                if not self.fonts.covers(char, face):
                    # The original code point remains recoverable even without a glyph.
                    label = "[" + " ".join(f"U+{ord(c):04X}" for c in char) + "]"
                    for letter in label:
                        fallback = self.fonts.choose(letter, css_size, weight, mono=mono)
                        atoms.append(Atom(letter, style, fallback, css_size * 1.5))
                    continue
                if spec.tabular:
                    face = replace(face, tabular=True)
                atoms.append(Atom(char, style, face, css_size * 1.5))
        return atoms

    def runs(self, atoms: list[Atom], spec: TextSpec, code: bool) -> tuple[HRun, ...]:
        groups: list[list[Atom]] = []
        for atom in atoms:
            if (
                groups
                and not atom.asset
                and not atom.style.citation
                and not groups[-1][0].asset
                and not groups[-1][0].style.citation
                and (atom.face, atom.style, atom.size)
                == (groups[-1][0].face, groups[-1][0].style, groups[-1][0].size)
            ):
                groups[-1].append(atom)
            else:
                groups.append([atom])
        output = []
        for group in groups:
            a = group[0]
            text = "".join(g.text for g in group)
            if a.asset:
                output.append(
                    HRun(text, None, a.style, a.size, a.width, a.ascent, a.descent, asset=a.asset)
                )
            elif a.style.citation:
                output.append(HRun(text, a.face, a.style, a.size, a.width, a.size + 4, 0))
            else:
                pad = a.size * 0.5 + 1 if a.style.code and not code else 0
                width = self.fonts.width(text, a.face, a.size) + len(text) * spec.tracking * 1.5
                lh = spec.line_height * 1.5
                if a.style.code and not code and spec.size != 20:
                    lh *= 0.85
                ascent, descent = self.line_metrics(a.size, q(lh), spec.mono or a.style.code)
                output.append(
                    HRun(text, a.face, a.style, a.size, width + pad * 2, ascent, descent, pad)
                )
        return tuple(output)

    def measure(
        self,
        spans: tuple[Inline, ...],
        width: float,
        component: str,
        spec: TextSpec = TextSpec(),
        *,
        code: bool = False,
    ) -> tuple[HLine, ...]:
        if width < 12:
            raise RenderError(f"HYw content too narrow: {component}")
        atoms = self.atoms(spans, spec, component, width, code)
        units: list[list[Atom]] = []
        break_units = (
            url_break_units(atoms) if spec.wrap == "url" else Typography.break_units(atoms)
        )
        for unit in break_units:
            # A citation belongs to the preceding word, never the next line on its own.
            if unit[0].style.citation and units and units[-1][0].text != "\n":
                units[-1].extend(unit)
            else:
                units.append(unit)
        output: list[HLine] = []
        current: list[Atom] = []
        da, dd = self.line_metrics(spec.size * 1.5, q(spec.line_height * 1.5), spec.mono)

        def trimmed(items):
            end = len(items)
            if not code:
                while end and items[end - 1].text in {" ", "\t"}:
                    end -= 1
            return items[:end]

        def width_of(items):
            return sum(run.width for run in self.runs(trimmed(items), spec, code))

        def flush(hard=False):
            nonlocal current
            runs = self.runs(trimmed(current), spec, code)
            ascent = max([da, *(r.ascent for r in runs if not r.style.citation)])
            descent = max([dd, *(r.descent for r in runs if not r.style.citation)])
            output.append(
                HLine(runs, sum(r.width for r in runs), ascent, q(ascent + descent), hard)
            )
            current = []

        if spec.wrap == "ellipsis":
            # Work in grapheme atoms and measured font widths, not character counts.
            atoms = [replace(atom, text=" ") if atom.text in {"\n", "\t"} else atom
                     for atom in atoms]
            current = atoms
            if width_of(current) > width:
                suffix = self.atoms((Inline("…"),), spec, component, width, code)
                if width_of(suffix) > width:
                    raise RenderError(f"Ellipsis wider than HYw line: {component}")
                low, high = 0, len(atoms)
                while low < high:
                    self.resources.budget.check()
                    middle = (low + high + 1) // 2
                    if width_of(trimmed(atoms[:middle]) + suffix) <= width:
                        low = middle
                    else:
                        high = middle - 1
                current = trimmed(atoms[:low]) + suffix
            flush()
            return tuple(output)

        for unit in units:
            self.resources.budget.check()
            if unit[0].text == "\n":
                flush(True)
                continue
            if not code and not current:
                while unit and unit[0].text in {" ", "\t"}:
                    unit = unit[1:]
            if not unit:
                continue
            if width_of(current + unit) <= width + 0.02:
                current.extend(unit)
            elif width_of(unit) <= width + 0.02:
                if current:
                    flush()
                current = unit
            else:
                if current:
                    flush()
                if spec.wrap == "normal" and not code and not self.wrap_long_words:
                    current = unit
                    diagnostic = Diagnostic(
                        "source-overflow",
                        "Original HYw CSS leaves this unbreakable word wider than the canvas; "
                        "intentionally reproduced",
                        component,
                    )
                    if diagnostic not in self.scene.diagnostics:
                        self.scene.diagnostics.append(diagnostic)
                    continue
                # CSS break-word first tries the word on a fresh line, then splits it.
                chunks: list[list[Atom]] = []
                for atom in unit:
                    if atom.style.citation and chunks:
                        chunks[-1].append(atom)
                    else:
                        chunks.append([atom])
                for chunk in chunks:
                    if width_of(current + chunk) > width + 0.02:
                        if current:
                            flush()
                        if width_of(chunk) > width + 0.02:
                            raise RenderError(f"Grapheme/citation wider than HYw line: {component}")
                    current.extend(chunk)
        if current or not output or (atoms and atoms[-1].text == "\n"):
            flush()
        return tuple(output)

    def place(
        self,
        lines: tuple[HLine, ...],
        x: float,
        y: float,
        width: float,
        component: str,
        spec: TextSpec = TextSpec(),
        align: str = "left",
        logical_line: int | None = None,
    ) -> float:
        for line in lines:
            cx = x + (
                (width - line.width) / 2
                if align == "center"
                else width - line.width
                if align == "right"
                else 0
            )
            baseline = y + line.ascent
            self.scene.lines.append(
                LineRecord(
                    component,
                    Box(cx, y, line.width, line.height),
                    baseline,
                    "".join(r.text for r in line.runs),
                    line.hard,
                    logical_line,
                )
            )
            previous_link, link_start = "", round(cx)
            for run in line.runs:
                if run.style.link != previous_link:
                    link_start = round(cx)
                previous_link = run.style.link
                if run.style.citation:
                    scale = self.scene.scale
                    height = round((run.size + 4) * scale) / scale
                    top = round(max(y, baseline - spec.size * 1.5 * 0.95) * scale) / scale
                    left = round((cx + 2) * scale) / scale
                    badge_width = round((run.width - 3) * scale) / scale
                    self.scene.ops.append(
                        Op(
                            "rect",
                            Box(left, top, badge_width, height),
                            component,
                            "panels",
                            fill=self.accent,
                        )
                    )
                    ink_left, ink_top, ink_right, ink_bottom = self.fonts.ink_bounds(
                        run.text, run.face, run.size
                    )
                    # Center the visible numeral, not its asymmetric side bearings.
                    text_x = left + (badge_width - ink_right - ink_left) / 2
                    text_x = round(text_x * scale) / scale
                    text_baseline = top + (height - ink_bottom - ink_top) / 2
                    text_baseline = round(text_baseline * scale) / scale
                    self.scene.ops.append(
                        Op(
                            "text",
                            Box(text_x, top, badge_width, height),
                            component,
                            fill=self.header_color,
                            text=run.text,
                            face=run.face,
                            size=run.size,
                            baseline=text_baseline,
                        )
                    )
                elif run.asset:
                    self.scene.ops.append(
                        Op(
                            "image",
                            Box(cx, baseline - run.ascent, run.width, run.ascent + run.descent),
                            component,
                            asset=run.asset,
                        )
                    )
                else:
                    if run.padding:
                        a, d = self.fonts.css_metrics(run.size, True)
                        pad_y = run.size * 0.2 + 1
                        self.scene.ops.append(
                            Op(
                                "rect",
                                Box(cx, baseline - a - pad_y, run.width, a + d + pad_y * 2),
                                component,
                                "panels",
                                fill="#f8f8f8",
                                radius=12,
                                stroke="#eeeeee",
                            )
                        )
                    self.scene.ops.append(
                        Op(
                            "text",
                            Box(
                                cx + run.padding,
                                baseline - run.ascent,
                                run.width - 2 * run.padding,
                                run.ascent + run.descent,
                            ),
                            component,
                            fill=run.style.color,
                            text=run.text,
                            face=run.face,
                            size=run.size,
                            baseline=baseline,
                            tracking=spec.tracking * 1.5,
                        )
                    )
                    if run.style.link:
                        # Pixel-aligned 4px dashes / 3px gaps, 3px thick. Keep the
                        # phase across font/weight runs belonging to the same link.
                        left, right = round(cx), round(cx + run.width)
                        first_dash = link_start + ((left - link_start) // 7) * 7
                        for dash in range(first_dash, right, 7):
                            start, end = max(left, dash), min(right, dash + 4)
                            if end > start:
                                self.scene.ops.append(
                                    Op(
                                        "rect",
                                        Box(start, round(baseline + 6), end - start, 3),
                                        component,
                                        "decoration",
                                        fill="#45454b",
                                    )
                                )
                    elif run.style.underline:
                        self.scene.ops.append(
                            Op(
                                "line",
                                Box(cx, baseline + 9, run.width, 0),
                                component,
                                "decoration",
                                fill=self.accent,
                                stroke_width=4.5,
                            )
                        )
                    if run.style.strike:
                        self.scene.ops.append(
                            Op(
                                "line",
                                Box(cx, baseline - run.size * 0.28, run.width, 0),
                                component,
                                "decoration",
                                fill=run.style.color,
                                stroke_width=1.5,
                            )
                        )
                cx += run.width
            y += line.height
        return q(y)
