"""Unicode line breaking and baseline layout, independent of block geometry."""

from dataclasses import dataclass, replace

import regex
from uniseg.linebreak import line_break_boundaries

from .fonts import Face, Fonts
from .model import Diagnostic, Inline, RenderError, Style
from .resources import Resources
from .scene import Box, LineRecord, Op, Scene, Theme


@dataclass(frozen=True)
class Atom:
    text: str
    style: Style
    face: Face | None
    size: int
    asset: str = ""
    width: float = 0
    ascent: float = 0
    descent: float = 0


@dataclass(frozen=True)
class Run:
    text: str
    style: Style
    face: Face | None
    size: int
    width: float
    ascent: float
    descent: float
    asset: str = ""
    padding: float = 0


@dataclass(frozen=True)
class MeasuredLine:
    runs: tuple[Run, ...]
    width: float
    height: float
    ascent: float
    hard_break: bool = False


class Typography:
    def __init__(self, fonts: Fonts, resources: Resources, theme: Theme, scene: Scene):
        self.fonts, self.resources, self.theme, self.scene = fonts, resources, theme, scene

    def atoms(
        self,
        spans: tuple[Inline, ...],
        size: int,
        color: str,
        component: str,
        bold: bool,
        code: bool,
        width: float,
    ) -> list[Atom]:
        result = []
        for span in spans:
            self.resources.budget.check()
            style = replace(
                span.style,
                bold=bold or span.style.bold,
                color=span.style.color or (self.theme.accent if span.style.link else color),
            )
            font_size = round(size * 0.88) if style.code and not code else size
            if span.kind == "break":
                result.append(Atom("\n", style, None, size))
                continue
            raster = None
            if span.kind == "math":
                raster = self.resources.math(
                    span.text, size, style.color, component, display=span.display
                )
            elif span.kind == "image":
                raster = self.resources.image(span.target, component)
            if raster:
                ratio = min(1, width / raster.width)
                if span.kind == "image":
                    ratio = min(ratio, size * 1.4 / raster.height)
                result.append(
                    Atom(
                        "\ufffc",
                        style,
                        None,
                        size,
                        raster.key,
                        raster.width * ratio,
                        (raster.height - raster.descent) * ratio,
                        raster.descent * ratio,
                    )
                )
                continue
            text = (
                span.text
                if span.kind == "text"
                else (f"${span.text}$" if span.kind == "math" else f"[image: {span.text}]")
            )
            if regex.search(r"\p{Bidi_Class=R}|\p{Bidi_Class=AL}", text):
                diagnostic = Diagnostic(
                    "bidi-limited",
                    "Mixed-direction paragraph reordering is not implemented",
                    component,
                )
                if diagnostic not in self.scene.diagnostics:
                    self.scene.diagnostics.append(diagnostic)
            for cluster in regex.findall(r"\X", text):
                face = self.fonts.select(cluster, style)
                result.append(Atom(cluster, style, face, font_size))
        return result

    def runs(self, atoms: list[Atom], code: bool) -> tuple[Run, ...]:
        groups: list[list[Atom]] = []
        for atom in atoms:
            if (
                groups
                and not atom.asset
                and not groups[-1][0].asset
                and (atom.face, atom.style, atom.size)
                == (groups[-1][0].face, groups[-1][0].style, groups[-1][0].size)
            ):
                groups[-1].append(atom)
            else:
                groups.append([atom])
        result = []
        for group in groups:
            first = group[0]
            if first.asset:
                result.append(
                    Run(
                        first.text,
                        first.style,
                        None,
                        first.size,
                        first.width,
                        first.ascent,
                        first.descent,
                        first.asset,
                    )
                )
            else:
                text = "".join(a.text for a in group)
                if first.face is None:
                    raise RenderError("Text run has no font")
                ascent, descent = self.fonts.metrics(first.face, first.size)
                padding = 4 if first.style.code and not code else 0
                result.append(
                    Run(
                        text,
                        first.style,
                        first.face,
                        first.size,
                        self.fonts.width(text, first.face, first.size) + padding * 2,
                        ascent + (2 if padding else 0),
                        descent + (2 if padding else 0),
                        padding=padding,
                    )
                )
        return tuple(result)

    @staticmethod
    def break_units(atoms: list[Atom]) -> list[list[Atom]]:
        text = "".join(a.text for a in atoms)
        boundaries = set(line_break_boundaries(text))
        units: list[list[Atom]] = []
        unit: list[Atom] = []
        offset = 0
        for atom in atoms:
            if atom.text == "\n":
                if unit:
                    units.append(unit)
                units.append([atom])
                unit = []
            else:
                unit.append(atom)
            offset += len(atom.text)
            if unit and offset in boundaries:
                units.append(unit)
                unit = []
        if unit:
            units.append(unit)
        return units

    def intrinsic(self, atoms: list[Atom]) -> tuple[float, float]:
        """Min-content prefers unbroken words; max-content prefers a single line."""
        chunks = [unit for unit in self.break_units(atoms) if unit[0].text != "\n"]
        minimum = max(
            (sum(run.width for run in self.runs(unit, False)) for unit in chunks), default=0
        )
        maximum = sum(run.width for run in self.runs(atoms, False))
        return minimum, maximum

    def measure(
        self,
        spans: tuple[Inline, ...],
        width: float,
        component: str,
        *,
        size: int | None = None,
        color: str | None = None,
        bold: bool = False,
        code: bool = False,
    ) -> tuple[MeasuredLine, ...]:
        if width < 16:
            raise RenderError(f"Content width too small in {component}: {width:.1f}px")
        size = size or self.theme.body_size
        atoms = self.atoms(spans, size, color or self.theme.ink, component, bold, code, width)
        units = self.break_units(atoms)
        lines: list[MeasuredLine] = []
        current: list[Atom] = []
        default_face = self.fonts.select("M", Style(code=code, bold=bold))
        da, dd = self.fonts.metrics(default_face, size)

        def trimmed(items: list[Atom]) -> list[Atom]:
            if code:
                return items
            end = len(items)
            while end and items[end - 1].text in {" ", "\t"}:
                end -= 1
            return items[:end]

        def fits(items: list[Atom]) -> bool:
            return sum(r.width for r in self.runs(trimmed(items), code)) <= width + 0.001

        def flush(hard: bool = False) -> None:
            nonlocal current
            runs = self.runs(trimmed(current), code)
            ascent = max([da, *(r.ascent for r in runs)])
            descent = max([dd, *(r.descent for r in runs)])
            height = max(size * (1.55 if code else self.theme.line_height), ascent + descent)
            lines.append(
                MeasuredLine(
                    runs,
                    sum(r.width for r in runs),
                    height,
                    ascent + (height - ascent - descent) / 2,
                    hard,
                )
            )
            current = []

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
            if fits(current + unit):
                current.extend(unit)
                continue
            if current:
                flush()
            if not code:
                while unit and unit[0].text in {" ", "\t"}:
                    unit = unit[1:]
            if fits(unit):
                current = list(unit)
                continue
            # Only an overlong unbreakable unit may use emergency grapheme breaks.
            # Combining marks/ZWJ sequences are never split into codepoints.
            for atom in unit:
                if not fits(current + [atom]):
                    if current:
                        flush()
                    if not fits([atom]):
                        raise RenderError(
                            f"Single grapheme wider than available line in {component}"
                        )
                current.append(atom)
        if current or not lines or (atoms and atoms[-1].text == "\n"):
            flush()
        return tuple(lines)

    def place(
        self,
        lines: tuple[MeasuredLine, ...],
        x: float,
        y: float,
        width: float,
        component: str,
        *,
        align: str = "left",
        logical_line: int | None = None,
    ) -> float:
        for line in lines:
            cursor = x + (
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
                    Box(cursor, y, line.width, line.height),
                    baseline,
                    "".join(r.text for r in line.runs),
                    line.hard_break,
                    logical_line,
                )
            )
            for run in line.runs:
                top = baseline - run.ascent
                box = Box(cursor, top, run.width, run.ascent + run.descent)
                if run.asset:
                    self.scene.ops.append(Op("image", box, component, asset=run.asset))
                else:
                    if run.padding:
                        self.scene.ops.append(
                            Op("rect", box, component, "panels", fill=self.theme.subtle, radius=4)
                        )
                    self.scene.ops.append(
                        Op(
                            "text",
                            Box(cursor + run.padding, top, run.width - 2 * run.padding, box.height),
                            component,
                            fill=run.style.color,
                            text=run.text,
                            face=run.face,
                            size=run.size,
                            baseline=baseline,
                        )
                    )
                    if run.style.link or run.style.strike:
                        line_y = baseline + 2 if run.style.link else baseline - run.size * 0.32
                        self.scene.ops.append(
                            Op(
                                "line",
                                Box(cursor, line_y, run.width, 0),
                                component,
                                "decoration",
                                fill=run.style.color,
                            )
                        )
                cursor += run.width
            y += line.height
        return y
