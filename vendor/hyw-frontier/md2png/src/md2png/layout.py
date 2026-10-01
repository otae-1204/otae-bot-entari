"""Block layout owns geometry. It emits a display list; it never paints pixels."""

import math
from dataclasses import replace

from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound

from .model import Block, Diagnostic, Document, Inline, RenderError, Style
from .resources import Budget
from .scene import Box, Component, Op, Scene, Theme
from .typography import Typography


class Layout:
    def __init__(self, typography: Typography, scene: Scene, theme: Theme, budget: Budget):
        self.text, self.scene, self.theme, self.budget = typography, scene, theme, budget

    def panel(
        self,
        component: str,
        x: float,
        y: float,
        width: float,
        fill: str,
        radius: float = 0,
        stroke: str = "",
    ) -> int:
        index = len(self.scene.ops)
        self.scene.ops.append(
            Op(
                "rect",
                Box(x, y, width, 0),
                component,
                "panels",
                fill=fill,
                radius=radius,
                stroke=stroke,
            )
        )
        return index

    def finish_panel(self, index: int, height: float) -> None:
        op = self.scene.ops[index]
        self.scene.ops[index] = replace(op, box=replace(op.box, height=height))

    def rich(
        self, spans: tuple[Inline, ...], x: float, y: float, width: float, component: str, **options
    ) -> float:
        lines = self.text.measure(spans, width, component, **options)
        return self.text.place(lines, x, y, width, component)

    def document(self, document: Document) -> Scene:
        t, s = self.theme, self.scene
        inset = t.margin + t.padding
        content_width = s.width - 2 * inset
        if content_width < 96:
            raise RenderError("Page too narrow for configured margin/padding")
        shadow = len(s.ops)
        s.ops.append(
            Op(
                "shadow",
                Box(t.margin, t.margin + 4, s.width - 2 * t.margin, 0),
                "page",
                "background",
                fill="#15243b18",
                radius=t.radius,
            )
        )
        panel = self.panel("page", t.margin, t.margin, s.width - 2 * t.margin, t.paper, t.radius)
        bottom = self.blocks(document.children, inset, inset, content_width)
        if document.children:
            bottom -= self.margin_after(document.children[-1])
        s.height = math.ceil(max(bottom + inset, inset * 2 + t.body_size * t.line_height))
        self.budget.check(s.height, s.width, s.scale)
        self.finish_panel(panel, s.height - 2 * t.margin)
        s.ops[shadow] = replace(
            s.ops[shadow], box=replace(s.ops[shadow].box, height=s.height - 2 * t.margin)
        )
        return s

    def margin_after(self, block: Block) -> int:
        if block.kind == "heading":
            return 12
        if block.kind == "paragraph" and block.tight:
            return 6
        return self.theme.gap

    def blocks(self, blocks: tuple[Block, ...], x: float, y: float, width: float) -> float:
        for index, block in enumerate(blocks):
            if block.kind == "heading" and index:
                y += 12
            y = self.block(block, x, y, width)
            self.budget.check(y, self.scene.width, self.scene.scale)
        return y

    def block(self, block: Block, x: float, y: float, width: float) -> float:
        if width < 32:
            raise RenderError(f"Nesting leaves no content width: {block.id}")
        top, t, kind = y, self.theme, block.kind
        if kind in {"paragraph", "heading"}:
            size = t.headings[block.level - 1] if kind == "heading" else t.body_size
            y = self.rich(block.inlines, x, y, width, block.id, size=size, bold=kind == "heading")
            if kind == "heading" and block.level == 1:
                y += 12
                self.scene.ops.append(
                    Op("line", Box(x, y, width, 0), block.id, "decoration", fill=t.border)
                )
                y += 4
        elif kind in {"quote", "summary"}:
            panel = self.panel(
                block.id,
                x,
                y,
                width,
                t.quote if kind == "quote" else t.subtle,
                t.radius,
                t.border if kind == "summary" else "",
            )
            inner_y = y + 18
            if kind == "summary":
                inner_y = (
                    self.rich(
                        (Inline("SUMMARY", Style(bold=True)),),
                        x + 20,
                        inner_y,
                        width - 40,
                        block.id + ":label",
                        size=12,
                        color=t.accent,
                    )
                    + 8
                )
            end = self.blocks(block.children, x + 20, inner_y, width - 40)
            if block.children:
                end -= self.margin_after(block.children[-1])
            y = end + 18
            self.finish_panel(panel, y - top)
            if kind == "quote":
                self.scene.ops.append(
                    Op(
                        "rect",
                        Box(x, top + 12, 3, y - top - 24),
                        block.id,
                        "decoration",
                        fill=t.accent,
                        radius=1,
                    )
                )
        elif kind == "list":
            marker_spans = [
                Inline(
                    ("[x]" if item.checked else "[ ]")
                    if item.checked is not None
                    else f"{block.start + i}."
                    if block.ordered
                    else "•"
                )
                for i, item in enumerate(block.children)
            ]
            marker_lines = [
                self.text.measure((span,), width, block.id, color=t.muted) for span in marker_spans
            ]
            gutter = max([16, *(line[0].width for line in marker_lines)]) + 14
            for item, marker in zip(block.children, marker_lines, strict=True):
                item_top = y
                line_index = len(self.scene.lines)
                y = self.blocks(item.children, x + gutter, y, width - gutter)
                if item.children:
                    y -= self.margin_after(item.children[-1])
                # Align the marker with the actual first child's baseline, including CJK fallback.
                baseline = (
                    self.scene.lines[line_index].baseline
                    if len(self.scene.lines) > line_index
                    else item_top + marker[0].ascent
                )
                self.text.place(
                    marker,
                    x,
                    baseline - marker[0].ascent,
                    gutter - 14,
                    item.id + ":marker",
                    align="right",
                )
                self.scene.components.append(
                    Component(item.id, "item", Box(x, item_top, width, y - item_top), item.source)
                )
                y += 6
            y -= 6 if block.children else 0
        elif kind == "code":
            y = self.code(block, x, y, width)
        elif kind == "table":
            y = self.table(block, x, y, width)
        elif kind == "rule":
            self.scene.ops.append(
                Op("line", Box(x, y + 8, width, 0), block.id, "decoration", fill=t.border)
            )
            y += 18
        elif kind == "math":
            lines = self.text.measure(
                (Inline(block.text, kind="math", display=True),),
                width - 24,
                block.id,
                size=t.body_size + 4,
            )
            y = self.text.place(lines, x + 12, y + 12, width - 24, block.id, align="center") + 12
        elif kind == "gallery":
            y = self.gallery(block, x, y, width)
        else:
            raise RenderError(f"Block cannot be laid out directly: {kind}")
        self.scene.components.append(
            Component(block.id, kind, Box(x, top, width, y - top), block.source)
        )
        return y + self.margin_after(block)

    def code(self, block: Block, x: float, y: float, width: float) -> float:
        t, top = self.theme, y
        panel = self.panel(block.id, x, y, width, t.code_bg, t.radius)
        language = block.language or "text"
        try:
            lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
        except ClassNotFound:
            lexer = TextLexer(stripnl=False, ensurenl=False)
            self.scene.diagnostics.append(Diagnostic("lexer-unavailable", language, block.id))
        syntax = get_style_by_name("github-dark")
        logical: list[list[Inline]] = [[]]
        for token, value in lex(block.text.expandtabs(4), lexer):
            palette = syntax.style_for_token(token)
            style = Style(
                code=True,
                bold=palette["bold"],
                italic=palette["italic"],
                color="#" + palette["color"] if palette["color"] else t.code_ink,
            )
            for index, fragment in enumerate(value.split("\n")):
                if index:
                    logical.append([])
                if fragment:
                    logical[-1].append(Inline(fragment, style))
        # Header and separator have their own components for inspection.
        label_end = self.rich(
            (Inline(language.upper(), Style(code=True)),),
            x + 18,
            y + 9,
            width - 36,
            block.id + ":label",
            size=12,
            color=t.code_muted,
            code=True,
        )
        y = max(y + 38, label_end + 9)
        self.scene.ops.append(
            Op("line", Box(x + 1, y, width - 2, 0), block.id, "decoration", fill="#2b3547")
        )
        y += 14
        marker_face = self.text.fonts.select("0", Style(code=True))
        gutter = self.text.fonts.width(str(len(logical)), marker_face, 12) + 16
        code_width = width - 36 - gutter
        for number, spans in enumerate(logical, 1):
            self.budget.check(y, self.scene.width, self.scene.scale)
            lines = self.text.measure(
                tuple(spans), code_width, block.id, size=t.code_size, color=t.code_ink, code=True
            )
            marks = self.text.measure(
                (Inline(str(number), Style(code=True)),),
                gutter,
                block.id,
                size=12,
                color=t.code_muted,
                code=True,
            )
            self.text.place(
                marks,
                x + 14,
                y + lines[0].ascent - marks[0].ascent,
                gutter - 10,
                block.id + ":gutter",
                align="right",
                logical_line=number,
            )
            y = self.text.place(
                lines, x + 18 + gutter, y, code_width, block.id, logical_line=number
            )
        y += 16
        self.finish_panel(panel, y - top)
        return y

    def table(self, block: Block, x: float, y: float, width: float) -> float:
        rows, t = block.children, self.theme
        columns = max((len(row.children) for row in rows), default=0)
        if not columns:
            return y
        pad, size = 12, t.body_size - 2
        minimum = size * 2 + pad * 2
        if columns * minimum > width:
            raise RenderError(
                f"Table {block.id} has too many columns for {width:.0f}px; increase width"
            )
        natural = [minimum] * columns
        preferred_min = [minimum] * columns
        for row in rows:
            for index, cell in enumerate(row.children):
                atoms = self.text.atoms(
                    cell.inlines, size, t.ink, cell.id, cell.header, False, width
                )
                min_content, max_content = self.text.intrinsic(atoms)
                natural[index] = max(natural[index], min(width, max_content + 2 * pad))
                # Long identifiers may wrap; normal words get space before spare width is shared.
                preferred_min[index] = max(
                    preferred_min[index], min(width * 0.4, min_content + 2 * pad)
                )
        if sum(preferred_min) > width:
            excess = [n - minimum for n in preferred_min]
            remainder = width - minimum * columns
            widths = [minimum + remainder * weight / sum(excess) for weight in excess]
        else:
            excess = [max(1, n - m) for n, m in zip(natural, preferred_min, strict=True)]
            remainder = width - sum(preferred_min)
            widths = [
                m + remainder * weight / sum(excess)
                for m, weight in zip(preferred_min, excess, strict=True)
            ]
        widths[-1] = width - sum(widths[:-1])
        for row_index, row in enumerate(rows):
            measured = [
                self.text.measure(
                    cell.inlines, widths[i] - pad * 2, cell.id, size=size, bold=cell.header
                )
                for i, cell in enumerate(row.children)
            ]
            height = max(sum(line.height for line in lines) for lines in measured) + pad * 2
            fill = t.subtle if row_index == 0 else t.paper if row_index % 2 else "#fafbfd"
            self.scene.ops.append(Op("rect", Box(x, y, width, height), row.id, "panels", fill=fill))
            cursor = x
            for index, (cell, lines) in enumerate(zip(row.children, measured, strict=True)):
                self.text.place(
                    lines, cursor + pad, y + pad, widths[index] - 2 * pad, cell.id, align=cell.align
                )
                self.scene.components.append(
                    Component(cell.id, "cell", Box(cursor, y, widths[index], height), cell.source)
                )
                cursor += widths[index]
            self.scene.ops.append(
                Op("line", Box(x, y + height, width, 0), row.id, "decoration", fill=t.border)
            )
            self.scene.components.append(
                Component(row.id, "row", Box(x, y, width, height), row.source)
            )
            y += height
            self.budget.check(y, self.scene.width, self.scene.scale)
        return y

    def gallery(self, block: Block, x: float, y: float, width: float) -> float:
        columns = min(2, len(block.inlines))
        gap = 14
        cell_width = (width - gap * (columns - 1)) / columns
        for start in range(0, len(block.inlines), columns):
            row_bottom = y
            for index, span in enumerate(block.inlines[start : start + columns]):
                left = x + index * (cell_width + gap)
                identifier = f"{block.id}:image{start + index}"
                raster = self.text.resources.image(span.target, identifier)
                if raster:
                    ratio = min(cell_width / raster.width, 260 / raster.height, 1)
                    w, h = raster.width * ratio, raster.height * ratio
                else:
                    w, h = cell_width, 120
                self.scene.ops.append(
                    Op(
                        "rect",
                        Box(left, y, cell_width, h),
                        identifier,
                        "panels",
                        fill=self.theme.subtle,
                        radius=6,
                    )
                )
                if raster:
                    self.scene.ops.append(
                        Op(
                            "image",
                            Box(left + (cell_width - w) / 2, y, w, h),
                            identifier,
                            asset=raster.key,
                        )
                    )
                else:
                    self.rich(
                        (Inline("Image not supplied"),),
                        left + 12,
                        y + 38,
                        cell_width - 24,
                        identifier,
                        size=14,
                        color=self.theme.muted,
                    )
                bottom = y + h
                if span.text:
                    bottom = self.rich(
                        (Inline(span.text),),
                        left,
                        bottom + 8,
                        cell_width,
                        identifier + ":caption",
                        size=14,
                        color=self.theme.muted,
                    )
                self.scene.components.append(
                    Component(identifier, "image", Box(left, y, cell_width, bottom - y))
                )
                row_bottom = max(row_bottom, bottom)
            y = row_bottom + gap
        return y - gap
