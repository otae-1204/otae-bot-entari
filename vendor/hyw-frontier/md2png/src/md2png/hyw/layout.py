"""Independent HYw component geometry derived from the working-tree CSS cascade.

No fixture names, DOM recordings, or reference pixels are read by this module.
"""

from dataclasses import replace
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps
from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.token import Comment, Keyword, Literal, Name, Number, String
from pygments.util import ClassNotFound

from ..model import Block, Diagnostic, Inline, Style
from ..scene import Box, Component, Op
from .art import Art
from .document import HywDocument, markdown, rich, source_origin
from .text import Flow, TextSpec, q

# Original card-ui: bg-white / bg-gray-50/30 over white, gray-100 columns.
TABLE_ROW_COLORS = ("#ffffff", "#fdfefe")
TABLE_COLUMN_RULE = "#f3f4f6"


class HywLayout:
    def __init__(self, flow: Flow, art: Art):
        self.flow, self.art, self.scene = flow, art, flow.scene
        self.budget = flow.resources.budget
        self.reading = False

    def rect(self, box: Box, component: str, fill: str, stroke: str = "", radius: float = 0):
        self.scene.ops.append(
            Op("rect", box, component, "panels", fill=fill, stroke=stroke, radius=radius)
        )

    def component(
        self, identifier: str, kind: str, x: float, y: float, w: float, h: float, source=None
    ):
        self.scene.components.append(Component(identifier, kind, Box(x, y, w, h), source))
        self.budget.check(y + h, self.scene.width, self.scene.scale)

    def text(self, spans, x, y, width, identifier, spec=TextSpec(), align="left"):
        lines = self.flow.measure(spans, width, identifier, spec)
        return self.flow.place(lines, x, y, width, identifier, spec, align)

    def card_start(self, identifier, label, icon, x, y, width):
        shadow = len(self.scene.ops)
        self.scene.ops.append(
            Op(
                "shadow",
                Box(x, y + 1.5, width, 0),
                identifier,
                "panels",
                fill="#0000000d",
                blur=1.5,
            )
        )
        panel = len(self.scene.ops)
        self.rect(Box(x, y, width, 0), identifier, "#ffffff")
        large_source = self.reading and identifier == "sources"
        spec = TextSpec(14 if large_source else 12, 18 if large_source else 16,
                        700, tracking=0.6, color=self.flow.header_color)
        lines = self.flow.measure((Inline(label.upper()),), width, identifier + ":badge", spec)
        badge_width = round((lines[0].width + (66 if large_source else 60)) * 64) / 64
        badge_box = Box(x - 12, y - 12, badge_width, 48 if large_source else 42)
        self.scene.ops.append(
            Op(
                "shadow",
                replace(badge_box, y=y - 9),
                identifier + ":badge",
                "panels",
                fill="#00000026",
                blur=3,
            )
        )
        self.rect(badge_box, identifier + ":badge", self.flow.accent)
        icon_size = 24 if large_source else 21
        key = self.art.icon(icon, self.flow.header_color, icon_size)
        self.scene.ops.append(
            Op("image", Box(x + 3, y if large_source else y - 1.5, icon_size, icon_size),
               identifier + ":icon", asset=key)
        )
        self.flow.place(lines, x + (36 if large_source else 33), y - (1.5 if large_source else 3),
                        lines[0].width, identifier + ":badge", spec)
        self.component(identifier + ":badge", "badge", badge_box.x, badge_box.y,
                       badge_width, badge_box.height)
        return shadow, panel

    def card_end(self, indexes, identifier, kind, x, top, width, bottom):
        for index in indexes:
            op = self.scene.ops[index]
            self.scene.ops[index] = replace(op, box=replace(op.box, height=bottom - top))
        self.component(identifier, kind, x, top, width, bottom - top)
        return bottom

    def margins(self, block: Block, compact=False):
        if compact:
            return (0, 0) if block.kind == "paragraph" else (12, 6)
        return {
            "heading": (48, 24 if block.level == 2 else 18),
            "paragraph": (24, 24),
            "list": (30, 30) if block.ordered else (24, 24),
            "quote": (45, 45),
            "rule": (54, 54),
            "math": (24, 24),
        }.get(block.kind, (24, 24))

    def prose(self, blocks, x, y, width, *, compact=False, spec=TextSpec()):
        previous = 0
        for index, block in enumerate(blocks):
            mt, mb = self.margins(block, compact)
            if index:
                y += max(previous, mt)
            y = self.block(block, x, y, width, compact=compact, spec=spec)
            previous = mb
        return y, previous

    def block(self, block: Block, x, y, width, *, compact=False, spec=TextSpec()):
        top = y
        if block.kind == "paragraph":
            use = replace(spec, size=14, line_height=24, wrap="break-word") if compact else spec
            if block.language == "literal":
                lines = self.flow.measure(block.inlines, width, block.id, use, code=True)
                y = self.flow.place(lines, x, y, width, block.id, use)
            else:
                y = self.text(block.inlines, x, y, width, block.id, use)
        elif block.kind == "heading":
            if self.reading:
                size = {1: 32, 2: 24, 3: 21, 4: 19, 5: 17, 6: 16}[block.level]
                use = TextSpec(size, size * 1.4, 700, color="#2c2c2e")
            elif block.level == 2:
                use = TextSpec(22, 29.333333, 900, tracking=-0.55, uppercase=True, color="#2c2c2e")
            else:
                size, lh = {1: (40.5, 45), 3: (24, 36), 4: (18, 28), 5: (18, 28), 6: (18, 28)}[
                    block.level
                ]
                use = TextSpec(size, lh, 700, tracking=-size * 0.025, color="#2c2c2e")
            y = self.text(block.inlines, x, y, width, block.id, use)
        elif block.kind == "list":
            y = self.list(block, x, y, width, spec)
        elif block.kind == "quote":
            children = list(block.children)
            paragraph_indexes = [i for i, child in enumerate(children) if child.kind == "paragraph"]
            if paragraph_indexes and not block.language:
                first, last = paragraph_indexes[0], paragraph_indexes[-1]
                children[first] = replace(
                    children[first], inlines=(Inline("“"),) + children[first].inlines
                )
                children[last] = replace(
                    children[last], inlines=children[last].inlines + (Inline("”"),)
                )
            y, _ = self.prose(
                tuple(children),
                x + 24,
                y,
                width - 24,
                spec=replace(
                    spec, weight=400 if block.language else 500, italic=not bool(block.language)
                ),
            )
            self.rect(Box(x, top, 7.5, y - top), block.id + ":line", self.flow.accent)
        elif block.kind == "rule":
            self.rect(Box(x, y, width, 1), block.id, "#e5e7eb")
            y += 1
        elif block.kind == "code":
            y = self.code(block, x, y, width)
        elif block.kind == "table":
            y = self.table(block, x, y, width)
        elif block.kind == "summary":
            y = self.summary(block, x, y, width)
        elif block.kind == "math":
            # KaTeX display wrapper adds 1em margins inside an overflow flow root.
            use = TextSpec(18, 32)
            lines = self.flow.measure(
                (Inline(block.text, kind="math", display=True),), width, block.id, use
            )
            y = self.flow.place(lines, x, y + 27, width, block.id, use, "center") + 27
        elif block.kind == "gallery":
            for index, span in enumerate(block.inlines):
                if index:
                    y += 18
                if span.kind != "image":
                    y = self.text((span,), x, y, width, block.id + ":link", spec)
                    continue
                raster = self.art.image(span.target, block.id)
                if raster:
                    # Reading cards fill the content column without cropping or
                    # distortion, including portraits and low-resolution images.
                    ratio = (width / raster.width if self.reading else
                             min(1.5, width / raster.width, 600 / raster.height))
                    self.scene.ops.append(
                        Op(
                            "image",
                            Box(x, y, raster.width * ratio, raster.height * ratio),
                            block.id,
                            asset=raster.key,
                        )
                    )
                    y += raster.height * ratio
                    if self.reading and span.text.strip():
                        y = self.text((Inline(span.text),), x, y + 9, width,
                                      block.id + ":caption", TextSpec(12, 18, color="#737378"))
        else:
            raise ValueError(f"Unsupported HYw block: {block.kind}")
        if block.kind not in {"code", "table", "summary"}:
            self.component(block.id, block.kind, x, top, width, y - top, block.source)
        return q(y)

    def list(self, block, x, y, width, spec, depth=0, card_bounds=None):
        # Lists indent text, not full-width panels. Preserve the enclosing content
        # bounds through nested lists; quotes/table cells still own their own bounds.
        card_x, card_width = card_bounds if card_bounds is not None else (x, width)
        ordered = block.ordered
        outer_pad = 42 if ordered else 24 if depth else 6
        inner_pad = 13.333333 if ordered else 30 if depth else 36
        gap = 15 if ordered else 6 if depth else 18
        use = replace(spec, line_height=28 if ordered else 32)
        for index, item in enumerate(block.children):
            if index:
                y += gap
            top = y
            tx = x + outer_pad + inner_pad
            for child_index, child in enumerate(item.children):
                if child.kind == "list":
                    y = self.list(
                        child,
                        tx,
                        y + 6,
                        width - outer_pad - inner_pad,
                        use,
                        depth + 1,
                        (card_x, card_width),
                    )
                elif child.kind == "paragraph":
                    if child_index:
                        y += 24
                    y = self.text(
                        child.inlines, tx, y, width - outer_pad - inner_pad, child.id, use
                    )
                elif child.kind in {"code", "table", "summary"}:
                    # A leading card needs a marker line; subsequent cards keep the
                    # normal block gap so their floating badges do not cover text.
                    y += self.margins(child)[0] if child_index else use.line_height * 1.5
                    y = self.block(child, card_x, y, card_width, spec=use)
                else:
                    y = self.block(child, tx, y, width - outer_pad - inner_pad, spec=use)
            if item.checked is not None:
                left, upper = x + outer_pad, top + 12
                self.rect(
                    Box(left, upper, 21, 21), item.id + ":marker", "#ffffff", self.flow.accent, 2
                )
                if item.checked:
                    self.scene.ops.append(
                        Op(
                            "image",
                            Box(left + 1, upper + 1, 19, 19),
                            item.id + ":check",
                            asset=self.art.icon("check", self.flow.accent, 19),
                        )
                    )
            elif ordered:
                label = f"{block.start + index}."
                lines = self.flow.measure((Inline(label),), outer_pad, item.id + ":marker", use)
                self.flow.place(
                    lines, x - 24, top, outer_pad + 15, item.id + ":marker", use, "right"
                )
            else:
                size = 9 if depth else 12
                self.rect(
                    Box(x + outer_pad, top + (19.5 if depth else 18), size, size),
                    item.id + ":marker",
                    self.flow.accent,
                )
            self.component(item.id, "item", card_x, top, card_width, y - top, item.source)
        return y

    def summary(self, block, x, y, width):
        top = y
        panel = self.card_start(block.id, "Summary", "text-box-outline", x, y, width)
        y, margin = self.prose(block.children, x + 30, y + 48, width - 60)
        return self.card_end(panel, block.id, "summary", x, top, width, y + margin + 24)

    def code(self, block, x, y, width):
        top = y
        language = block.language or "text"
        panel = self.card_start(
            block.id,
            language.capitalize() if language != "text" else "Code",
            "code-braces",
            x,
            y,
            width,
        )
        try:
            lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
        except ClassNotFound:
            lexer = TextLexer(stripnl=False, ensurenl=False)
            self.scene.diagnostics.append(
                Diagnostic("highlight-approximation", f"No lexer for {language}", block.id)
            )
        logical = [[]]
        for token, value in lex(block.text, lexer):
            color = "#3a3a3c"
            if token in Comment:
                color = "#6a737d"
            elif token in Keyword or token in Literal:
                color = "#d73a49"
            if token in Name.Function:
                color = "#6f42c1"
            elif token in Name.Builtin:
                color = "#e36209"
            elif token in String:
                color = "#032f62"
            elif token in Number:
                color = "#005cc5"
            if language == "json" and token in Name.Tag:
                color = "#005cc5"
            for i, fragment in enumerate(value.split("\n")):
                if i:
                    logical.append([])
                if fragment:
                    logical[-1].append(Inline(fragment.expandtabs(8), Style(color=color)))
        y += 42
        use = TextSpec(13.6, 18.7, mono=True)
        pad = q(2.04)
        for number, spans in enumerate(logical, 1):
            lines = self.flow.measure(
                tuple(spans), width - 54 - 20.4 - 25.5, block.id, use, code=True
            )
            height = max(sum(line.height for line in lines) + pad * 2 if spans else 0, 25.96875)
            if spans:
                self.flow.place(
                    lines, x + 74.4, y + pad, width - 99.9, block.id, use, logical_line=number
                )
            self.rect(Box(x + 53, y, 1, height), block.id + ":gutter", "#e5e7eb")
            marker = TextSpec(11, 15.125, mono=True, color="#86868b")
            marks = self.flow.measure((Inline(str(number)),), 41, block.id + ":gutter", marker)
            self.flow.place(
                marks, x + 6, y + q(1.65), 35, block.id + ":gutter", marker, "right", number
            )
            y += height
        return self.card_end(panel, block.id, "code", x, top, width, y + 12)

    def reading_table(self, block, x, y, width):
        """Keep wide records readable instead of squeezing every column equally."""
        top = y
        panel = self.card_start(block.id, "Table", "table", x, y, width)
        y += 36
        rows = block.children
        body = TextSpec(16, 23, wrap="break-word")
        header = replace(body, weight=700, color="#1e2939")
        pad = 14
        labels = rows[0].children
        label_width = min(190, (width - 4 * pad) * 0.32)
        value_width = width - 4 * pad - label_width
        for index, row in enumerate(rows[1:], 1):
            y += 12
            y = (
                self.text(
                    (Inline(f"记录 {index}"),),
                    x + pad,
                    y,
                    width - 2 * pad,
                    row.id + ":record",
                    header,
                )
                + 12
            )
            for column, cell in enumerate(row.children):
                self.budget.check()
                label = labels[column].inlines if column < len(labels) else ()
                left = self.flow.measure(label, label_width, cell.id + ":label", header)
                right = self.flow.measure(cell.inlines, value_width, cell.id, body)
                height = (
                    max(sum(line.height for line in left), sum(line.height for line in right))
                    + 20
                )
                self.flow.place(left, x + pad, y + 10, label_width, cell.id + ":label", header)
                self.flow.place(
                    right, x + 3 * pad + label_width, y + 10, value_width, cell.id, body
                )
                self.rect(
                    Box(x + pad, y + height, width - 2 * pad, 1), cell.id + ":rule", "#e5e7eb"
                )
                self.component(cell.id, "cell", x, y, width, height, cell.source)
                y += height + 1
        # A header-only table still has authored content to preserve.
        if len(rows) == 1:
            for cell in labels:
                y = self.text(cell.inlines, x + pad, y, width - 2 * pad, cell.id, header) + 12
        return self.card_end(panel, block.id, "table", x, top, width, y + 12)

    def table(self, block, x, y, width, bare=False):
        count = max(len(row.children) for row in block.children)
        if self.reading and not bare and (count >= 6 or width / count < 95):
            return self.reading_table(block, x, y, width)
        top = y
        panel = None if bare else self.card_start(block.id, "Table", "table", x, y, width)
        if not bare:
            y += 30
        rows = block.children
        n = max(len(row.children) for row in rows)
        edges = (
            [0] + [round(((width - (n - 1)) * i / n + i) * 64) / 64 for i in range(1, n)] + [width]
        )
        for ri, row in enumerate(rows):
            specs = [
                TextSpec(
                    16.5,
                    20.625,
                    900 if ri == 0 else 400,
                    tracking=-0.4125 if ri == 0 else 0,
                    uppercase=ri == 0,
                    color="#1e2939" if ri == 0 else "#3a3a3c",
                    wrap="break-word",
                )
                for _ in row.children
            ]
            measured = [
                self.flow.measure(
                    cell.inlines,
                    edges[i + 1] - edges[i] - 48 - (1 if i < n - 1 else 0),
                    cell.id,
                    specs[i],
                )
                for i, cell in enumerate(row.children)
            ]
            height = max(sum(line.height for line in lines) for lines in measured) + 42
            self.rect(Box(x, y, width, height + 1), row.id, TABLE_ROW_COLORS[ri % 2])
            for i, (cell, lines) in enumerate(zip(row.children, measured, strict=True)):
                cw = edges[i + 1] - edges[i]
                inner = cw - 48 - (1 if i < n - 1 else 0)
                cy = y + (height - sum(line.height for line in lines)) / 2
                self.flow.place(lines, x + edges[i] + 24, cy, inner, cell.id, specs[i], cell.align)
                if i < n - 1:
                    self.rect(
                        Box(x + edges[i + 1] - 1, y, 1, height + 1),
                        cell.id + ":border",
                        TABLE_COLUMN_RULE,
                    )
                self.component(cell.id, "cell", x + edges[i], y, cw, height, cell.source)
            self.rect(Box(x, y + height, width, 1), row.id + ":border", "#e5e7eb")
            self.component(row.id, "row", x, y, width, height + 1, row.source)
            y += height + 1
        if panel:
            return self.card_end(panel, block.id, "table", x, top, width, y)
        self.component(block.id, "table", x, top, width, y - top)
        return y

    def sources(self, refs, x, y, width):
        top = y
        identifier = "sources"
        panel = self.card_start(
            identifier, "Sources", "book-open-page-variant-outline", x, y, width
        )
        y += 66 if self.reading else 60
        title_spec = TextSpec(18 if self.reading else 16, 24 if self.reading else 20,
                              600, color="#2c2c2e", wrap="ellipsis")
        url_spec = TextSpec(12 if self.reading else 10, 18 if self.reading else 15,
                            color="#55555b" if self.reading else "#737378", wrap="url")
        number_spec = TextSpec(12, 20, 600, color=self.flow.accent)
        icon_size, icon_inset = (21, 38) if self.reading else (18, 27)
        icon_color = "#65656b" if self.reading else "#86868b"
        # Reading cards use one site icon, no numbered gutter; legacy parity stays unchanged.
        gutter = 0 if self.reading else max(30, len(str(max(ref.number for ref in refs))) * 12)
        tx = x + 30 + (0 if self.reading else gutter + 21)
        tw = width - (tx - x) - 30
        for index, ref in enumerate(refs):
            self.budget.check()
            if index:
                self.rect(Box(tx, y + 12, tw, 1), identifier + ":divider", "#eeeeef")
                y += 42 if self.reading else 36
            item_top = y
            item = f"source:{index + 1}"
            if not self.reading:
                self.text(
                    (Inline(str(ref.number)),),
                    x + 30,
                    y,
                    gutter,
                    item + ":number",
                    number_spec,
                    "center",
                )
            # Favicons are supplied bytes only. Both text rows share the same inset;
            # 21px icons leave a 17px visual gap before the reading card's text.
            favicon_key = "favicon:" + ref.url
            if favicon_key not in self.flow.resources.data:
                favicon_key = "favicon:" + source_origin(ref.url)
            favicon = (
                self.flow.resources.image(favicon_key, item)
                if favicon_key in self.flow.resources.data
                else None
            )
            title_inset = icon_inset if self.reading or favicon else 0
            if title_inset:
                icon = (favicon.key if favicon
                        else self.art.icon("text-box-outline", icon_color, icon_size))
                self.scene.ops.append(
                    Op("image", Box(tx, y + (title_spec.line_height * 1.5 - icon_size) / 2,
                                    icon_size, icon_size), item + ":site-icon", asset=icon)
                )
            y = (
                self.text(
                    (Inline(ref.title or urlsplit(ref.url).hostname or ref.url),),
                    tx + title_inset,
                    y,
                    tw - title_inset,
                    item + ":title",
                    title_spec,
                )
                + 3
            )
            if not self.reading:
                icon = self.art.icon("link", icon_color, icon_size)
                self.scene.ops.append(
                    Op("image", Box(tx, y + (url_spec.line_height * 1.5 - icon_size) / 2,
                                    icon_size, icon_size), item + ":link-icon", asset=icon)
                )
            y = self.text(
                (Inline(unquote(ref.url)),),
                tx + icon_inset,
                y,
                tw - icon_inset,
                item + ":domain",
                url_spec,
            )
            if ref.snippet or ref.screenshot:
                y += 12
                snippet_top = y
                y += 3
                border = 4 if ref.page or ref.fetched else 3
                sx = tx + 18 + border
                sw = tw - 18 - border
                if ref.screenshot:
                    raster = self.art.image(ref.screenshot, item + ":preview")
                    if raster:
                        iw = min(raster.width * 1.5 + 2, sw)
                        ih = iw if ref.thumbnail else (iw - 2) * raster.height / raster.width + 2
                        self.rect(Box(sx, y, iw, ih), item + ":preview", "#ffffff", "#e5e7eb", 6)
                        image_key = raster.key
                        if ref.thumbnail:
                            image = self.flow.resources.images[raster.key]
                            image_key = raster.key + ":square"
                            self.flow.resources.images[image_key] = ImageOps.fit(
                                image, (min(image.size),) * 2, Image.Resampling.LANCZOS
                            )
                        self.scene.ops.append(
                            Op(
                                "image",
                                Box(sx + 1, y + 1, iw - 2, ih - 2),
                                item + ":preview",
                                asset=image_key,
                            )
                        )
                        y += ih
                        if ref.thumbnail and ref.cache_id:
                            y = self.text(
                                (Inline(f"/w {ref.number} 查看完整页面"),),
                                sx,
                                y + 6,
                                sw,
                                item + ":hint",
                                TextSpec(10, 15, mono=True, color="#c3c3c5"),
                            )
                else:
                    children = markdown(ref.snippet, self.budget.limits).children
                    y, _ = self.prose(children, sx, y, sw, compact=True)
                y += 3
                if ref.page or ref.fetched:
                    self.rect(
                        Box(tx, snippet_top, border, y - snippet_top),
                        item + ":rule",
                        self.flow.accent,
                    )
            self.component(item, "source", x + 30, item_top, width - 60, y - item_top)
        return self.card_end(panel, identifier, "sources", x, top, width, y + 36)

    def gallery(self, images, x, y, width):
        top = y
        identifier = "gallery"
        panel = self.card_start(identifier, "Gallery", "image-multiple-outline", x, y, width)
        cw = (width - 72 - 24) / 2
        rasters = [self.art.image(image, identifier) for image in images]
        rasters = [r for r in rasters if r]
        heights = [q((cw - 2) * r.height / r.width) + 2 for r in rasters]
        if len(heights) <= 1:
            split = len(heights)
        else:
            split = min(
                range(1, len(heights)),
                key=lambda i: max(
                    sum(heights[:i]) + 24 * (i - 1), sum(heights[i:]) + 24 * (len(heights) - i - 1)
                ),
            )
        bottom = y + 60
        for column, (start, end) in enumerate(((0, split), (split, len(rasters)))):
            cy = y + 60
            for i in range(start, end):
                left = x + 36 + column * (cw + 24)
                self.rect(Box(left, cy, cw, heights[i]), f"gallery:{i}", "#f9fafb", "#f3f4f6", 6)
                self.scene.ops.append(
                    Op(
                        "image",
                        Box(left + 1, cy + 1, cw - 2, heights[i] - 2),
                        f"gallery:{i}",
                        asset=rasters[i].key,
                    )
                )
                self.component(f"gallery:{i}", "image", left, cy, cw, heights[i])
                cy += heights[i] + 24
            bottom = max(bottom, cy - 24 if end > start else cy)
        return self.card_end(panel, identifier, "gallery", x, top, width, bottom + 36)

    def document(self, document: HywDocument):
        self.reading = document.reading
        y = 60
        x = 48
        width = self.scene.width - 96
        trailing = 0
        has_content = False
        if document.title:
            spec = TextSpec(
                32,
                40,
                900,
                tracking=-1.6,
                uppercase=not self.reading,
                tabular=True,
                color="#2c2c2e",
            )
            top = y
            spans = document.title_inlines or rich((Inline(document.title),))
            y = self.text(spans, x, y, width, "title", spec)
            self.component("title", "title", x, top, width, y - top)
            has_content = True
        for index, section in enumerate(document.sections):
            if has_content:
                y += max(36, trailing)
            top = y
            if len(section) == 1 and section[0].kind in {"summary", "code", "table"}:
                y = self.block(section[0], x, y, width)
                trailing = 0
            else:
                y, trailing = self.prose(section, x, y, width)
                self.component(f"section:{index}", "markdown", x, top, width, y - top)
            has_content = True
        if document.runtime:
            if has_content:
                y += max(36, trailing)
            top = y
            panel = self.card_start("runtime", "Runtime", "counter", x, y, width)
            table = markdown(document.runtime, self.budget.limits).children[0]
            table = replace(
                table,
                id="runtime:table",
                children=tuple(
                    replace(
                        row,
                        id="runtime:" + row.id,
                        children=tuple(replace(c, id="runtime:" + c.id) for c in row.children),
                    )
                    for row in table.children
                ),
            )
            y = self.table(table, x + 30, y + 60, width - 60, True) + 36
            self.card_end(panel, "runtime", "runtime", x, top, width, y)
            has_content = True
            trailing = 0
        if document.references:
            if has_content:
                y += max(36, trailing)
            y = self.sources(document.references, x, y, width)
            has_content = True
            trailing = 0
        if document.gallery:
            if has_content:
                y += max(36, trailing)
            y = self.gallery(document.gallery, x, y, width)
            trailing = 48
        if document.metadata:
            y += max(36, trailing)
            y = self.text(
                (Inline(document.metadata),),
                x,
                y,
                width,
                "metadata",
                TextSpec(11, 18, color="#86868b", wrap="break-word"),
            )
            trailing = 0
        # CDP's clip dimensions are truncated, not rounded, to whole device pixels.
        self.scene.height = int(y + trailing + 60)
        self.budget.check(self.scene.height, self.scene.width, self.scene.scale)
        return self.scene
