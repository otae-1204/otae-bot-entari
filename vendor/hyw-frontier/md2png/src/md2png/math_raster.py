"""Offline Python formula pipeline: latex2mathml → Ziamath → aggdraw/Pillow.

No TeX process, browser, JavaScript, macros executing code, file commands or
network access. Unsupported syntax remains an explicit caller-visible failure.
"""

import math
import re
from dataclasses import dataclass
from threading import RLock
from xml.etree import ElementTree as ET

from latex2mathml import exceptions as tex_errors
from latex2mathml.converter import convert
from PIL import Image
from ziamath.nodes import Mnode
from ziamath.styles import MathVariant, parse_style
from ziamath.zmath import Math, apply_mstyle, denamespace

from .fonts import FontSet
from .math_fonts import ReadingMathFont
from .math_svg import rasterize
from .model import RenderError

# FontTools lazy font tables and upstream font internals are mutable. The lock
# protects bounded source reuse; formula documents/fonts themselves are local.
_MATH_LOCK = RLock()
_TEX_ERRORS = (
    tex_errors.NumeratorNotFoundError,
    tex_errors.DenominatorNotFoundError,
    tex_errors.ExtraLeftOrMissingRightError,
    tex_errors.MissingSuperScriptOrSubscriptError,
    tex_errors.DoubleSubscriptsError,
    tex_errors.DoubleSuperscriptsError,
    tex_errors.NoAvailableTokensError,
    tex_errors.InvalidStyleForGenfracError,
    tex_errors.MissingEndError,
    tex_errors.InvalidAlignmentError,
    tex_errors.InvalidWidthError,
    tex_errors.LimitsMustFollowMathOperatorError,
)
_TAGS = frozenset(
    "math mrow mi mn mo mtext ms mspace mfrac msqrt mroot msub msup "
    "msubsup mover munder munderover mfenced mtable mtr mtd mstyle "
    "mphantom mpadded menclose mmultiscripts mprescripts none".split()
)
_ATTRIBUTES = frozenset(
    "display displaystyle mathvariant mathcolor mathbackground mathsize "
    "scriptlevel stretchy symmetric fence separator form lspace rspace "
    "minsize maxsize movablelimits largeop accent accentunder "
    "linethickness width height depth "
    "lspace voffset open close separators columnalign columnspacing "
    "rowspacing equalrows equalcolumns linebreak notation".split()
)
_MACRO_DEFINITIONS = frozenset(
    r"\newcommand \renewcommand \providecommand \def \gdef \edef \xdef \let \futurelet "
    r"\newenvironment \renewenvironment \DeclareMathOperator".split()
)
_ARITY = {
    "mfrac": 2,
    "mroot": 2,
    "msub": 2,
    "msup": 2,
    "mover": 2,
    "munder": 2,
    "msubsup": 3,
    "munderover": 3,
}
_DIMENSIONS = frozenset(
    "mathsize scriptlevel minsize maxsize width height depth lspace rspace "
    "voffset linethickness columnspacing rowspacing".split()
)


@dataclass
class MathImage:
    image: Image.Image
    ascent: float

    @property
    def descent(self):
        return self.image.height - self.ascent


class Formula(Math):
    """Pinned Ziamath 0.13 parent contract, with an explicitly owned font.

    Its public constructor selects fonts from a mutable module registry. Build
    the same parent fields here instead: no registry injection/monkeypatching,
    persistent equation numbering, or request data in global state.
    """

    def __init__(self, root, size, font):
        self.size, self.font, self.margin = size, font, 2
        self.title = self.eqnumber = None
        self.mathml = self.element = apply_mstyle(root)
        self.style = parse_style(self.element)
        self.mtag = "math"
        self.node = Mnode.fromelement(self.element, parent=self)


class MathRasterizer:
    def __init__(self, budget, scale, font_set=None):
        self.budget, self.scale = budget, scale
        self.font_set = font_set or FontSet.bundled()
        self.font = None

    def mathml(self, text, display):
        limits = self.budget.limits
        depth = 0
        # Count groups before recursive conversion, respecting escaped braces.
        for match in re.finditer(r"\\(?:[a-zA-Z]+|.)|[{}]", text):
            # The converter can expand user macros; its depth cap is not an
            # expansion-size cap. Do not run that path on model-generated input.
            if match[0] in _MACRO_DEFINITIONS:
                raise ValueError("User-defined TeX macros are not enabled")
            if match[0] == "{":
                depth += 1
            elif match[0] == "}":
                depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced formula group")
            if depth > limits.max_depth:
                raise RenderError("Formula nesting budget exceeded")
        if depth:
            raise ValueError("Unbalanced formula group")
        # latex2mathml calls the unnumbered alignment environment align*.
        text = re.sub(
            r"\\(begin|end)\{(aligned|gathered)\}",
            lambda m: "\\" + m[1] + "{" + {"aligned": "align*", "gathered": "gather*"}[m[2]] + "}",
            text,
        )
        markup = convert(text, display="block" if display else "inline")
        if len(markup) > limits.max_math_chars * 300:
            raise RenderError("Formula MathML byte budget exceeded")
        root = denamespace(ET.fromstring(markup))
        pending, nodes, cells = [(root, 0)], 0, 0
        while pending:
            node, level = pending.pop()
            self.budget.check()
            nodes += 1
            cells += node.tag == "mtd"
            if nodes > limits.max_nodes or level > limits.max_depth * 2 or cells > 128:
                raise RenderError("Formula MathML complexity budget exceeded")
            if node.tag not in _TAGS:
                raise ValueError(f"Unsupported MathML element: {node.tag}")
            if node.tag in _ARITY and len(node) != _ARITY[node.tag]:
                raise ValueError(
                    f"Incomplete formula: {node.tag} requires {_ARITY[node.tag]} children"
                )
            if node.tag in {"msqrt", "menclose", "mmultiscripts"} and not len(node):
                raise ValueError(f"Incomplete formula: empty {node.tag}")
            if node.tag == "menclose" and not set(node.get("notation", "box").split()) <= {
                "box",
                "circle",
                "roundedbox",
                "top",
                "bottom",
                "left",
                "right",
                "longdiv",
                "actuarial",
                "madruwb",
                "phasorangle",
                "verticalstrike",
                "horizontalstrike",
                "updiagonalstrike",
                "downdiagonalstrike",
                "updiagonalarrow",
            }:
                raise ValueError("Unsupported formula enclosure notation")
            for key, value in node.attrib.items():
                if key not in _ATTRIBUTES:
                    raise ValueError(f"Unsupported MathML attribute: {key}")
                if key in _DIMENSIONS:
                    for number in re.findall(r"[+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?", value):
                        amount = float(number)
                        if not math.isfinite(amount) or abs(amount) > 1024:
                            raise RenderError("Formula dimension budget exceeded")
            if node.tag in {"mi", "mo"} and re.search(r"\\[a-zA-Z]+", node.text or ""):
                raise ValueError(f"Unsupported TeX command: {node.text}")
            # Match Ziamath's recommended accent codepoints, without regex
            # rewriting nested \binom / \mathrm groups in the source expression.
            if node.tag == "mo" and node.text == "^":
                node.text = "\u02c6" if node.get("stretchy") == "false" else "\u0302"
            elif node.tag == "mo" and node.text == "~":
                node.text = "\u0303"
            pending.extend((child, level + 1) for child in node)
        return root

    def render(self, text, size, color, *, display=False):
        self.budget.check()
        if len(text) > self.budget.limits.max_math_chars:
            raise RenderError("Formula character budget exceeded")
        with _MATH_LOCK:
            try:
                root = self.mathml(text, display)
                root.set("mathcolor", color)
                if self.font is None:
                    self.font = ReadingMathFont(size, self.font_set, self.budget)
                self.font.basesize = size
                # Ziamath strips token-edge whitespace. Preserve explicit \text{}
                # spaces as measured MathML boxes, including all-space labels.
                for token in list(root.iter("mtext")):
                    text_value = token.text or ""
                    left = len(text_value) - len(text_value.lstrip())
                    end = max(left, len(text_value.rstrip()))
                    if left or end < len(text_value):
                        token.tag, token.text = "mrow", None
                        for part, space in (
                            (text_value[:left], True),
                            (text_value[left:end], False),
                            (text_value[end:], True),
                        ):
                            if space and part:
                                advance = (
                                    sum(
                                        self.font.findglyph(c, MathVariant()).advance()
                                        for c in part
                                    )
                                    / self.font.info.layout.unitsperem
                                )
                                ET.SubElement(token, "mspace", {"width": f"{advance}em"})
                            elif part:
                                ET.SubElement(token, "mtext").text = part
                formula = Formula(root, size, self.font)
                self.budget.check()
                bbox = formula.node.bbox
                x = min(0, bbox.xmin) - 2
                y = -max(0, bbox.ymax) - 2
                width = max(0, bbox.xmax) - x + 2
                height = -min(0, bbox.ymin) - y + 2
                if not all(math.isfinite(v) for v in (x, y, width, height)):
                    raise ValueError("Non-finite formula layout")
                if width * height * self.scale**2 * 4 > self.budget.limits.max_asset_pixels:
                    raise RenderError("Formula pixel budget exceeded")
                svg = ET.Element("svg", {"viewBox": f"{x} {y} {width} {height}"})
                formula.node.draw(0, 0, svg)
                image, ascent = rasterize(svg, self.budget, self.scale)
            except _TEX_ERRORS as exc:
                raise ValueError(f"Invalid TeX: {type(exc).__name__}") from exc
            except (
                ET.ParseError,
                IndexError,
                KeyError,
                ZeroDivisionError,
                NotImplementedError,
            ) as exc:
                raise ValueError(
                    f"Unsupported formula layout: {type(exc).__name__}: {exc}"
                ) from exc
            except RecursionError as exc:
                raise RenderError("Formula recursion budget exceeded") from exc
        return MathImage(image, ascent)
