"""Help cards: page specs + an art gallery -> themed PNGs shipped under ``assets/image/help``.

Page text lives in ``scripts/help_pages.json``; artworks and their crop focus live in
``assets/image/help/art/gallery.json``. Adding art or pages is a data change: edit the two
JSON files, then run ``scripts/render_help_cards.py --write``. See ``docs/help_cards.md``.

Layout: blue title banner, then a card whose backdrop is the artwork (blurred), a frosted
panel with the commands, and a sharp "standee" window showing the same artwork. Artwork is
only ever scaled uniformly and cropped around its focus point, never stretched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from otae_bot.help_images import VARIANT_DIR_NAME, cover_visibility, image_ratio

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HELP_IMAGE_DIR = PROJECT_ROOT / "assets/image/help"
ART_DIR = HELP_IMAGE_DIR / "art"
GALLERY_PATH = ART_DIR / "gallery.json"
PAGES_PATH = PROJECT_ROOT / "scripts/help_pages.json"

DIGEST_KEY = "otae-help-digest"
# Bump when the HTML/CSS below changes the output, so shipped images are reported stale.
TEMPLATE_VERSION = 4
FONT_FAMILY = "OtaeHelpSans"


class HelpSpecError(ValueError):
    pass


@dataclass(frozen=True)
class HelpTheme:
    page_width: int = 1325
    min_card_height: int = 661
    window_width: int = 240
    max_height: int = 4000
    accent: str = "#527fbe"
    accent_soft: str = "#a5bcde"
    heading_color: str = "#434343"
    body_color: str = "#5b5b5b"
    border_color: str = "#251e19"
    panel_alpha: float = 0.78
    backdrop_blur: int = 8
    font_dir: Path = PROJECT_ROOT / "plugins/endfield/assets/fonts"
    font_files: tuple[tuple[int, str], ...] = (
        (500, "HarmonyOS_Sans_SC_Medium.ttf"),
        (700, "HarmonyOS_Sans_SC_Bold.ttf"),
    )

    def digest_fields(self) -> dict[str, Any]:
        values = dataclasses.asdict(self)
        values.pop("font_dir")
        return values


@dataclass(frozen=True)
class HelpItem:
    command: str
    description: str = ""
    badge: str = ""
    example: str = ""


@dataclass(frozen=True)
class HelpSection:
    heading: str
    items: tuple[HelpItem, ...]
    access: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelpPage:
    id: str
    file: str
    title: str
    columns: tuple[tuple[HelpSection, ...], ...]
    subtitle: str = ""
    footnote: str = ""
    art: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Artwork:
    id: str
    path: Path
    focus: tuple[float, float] = (0.5, 0.3)
    backdrop_focus: tuple[float, float] | None = None
    credit: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Gallery:
    artworks: dict[str, Artwork]
    default: tuple[str, ...]

    def resolve(self, page: HelpPage) -> tuple[Artwork, ...]:
        ids = page.art or self.default
        missing = [art_id for art_id in ids if art_id not in self.artworks]
        if missing:
            raise HelpSpecError(f"page {page.id!r} uses unknown art {missing}; add them to gallery.json")
        if not ids:
            raise HelpSpecError(f"page {page.id!r} has no art and gallery.json has no default")
        return tuple(self.artworks[art_id] for art_id in ids)


@dataclass(frozen=True)
class RenderTarget:
    page: HelpPage
    art: Artwork
    path: Path
    digest: str
    primary: bool


# ---------------------------------------------------------------------------
# Spec loading


def _text(value: Any) -> str:
    return str(value or "").strip()


def parse_item(raw: Any) -> HelpItem:
    """Accept ``"命令  说明"`` strings (two spaces) or ``{"cmd", "desc", "badge"}`` objects."""
    if isinstance(raw, str):
        command, _, description = raw.partition("  ")
        item = HelpItem(command.strip(), description.strip())
    elif isinstance(raw, dict):
        item = HelpItem(
            _text(raw.get("cmd") or raw.get("command")),
            _text(raw.get("desc") or raw.get("description")),
            _text(raw.get("badge")),
            _text(raw.get("example") or raw.get("eg")),
        )
    else:
        raise HelpSpecError(f"help item must be a string or object: {raw!r}")
    if not item.command:
        raise HelpSpecError(f"help item has no command: {raw!r}")
    return item


def parse_section(raw: dict[str, Any]) -> HelpSection:
    heading = _text(raw.get("heading"))
    if not heading:
        raise HelpSpecError(f"help section has no heading: {raw!r}")
    return HelpSection(
        heading=heading,
        items=tuple(parse_item(item) for item in raw.get("items") or ()),
        access=_text(raw.get("access")),
        notes=tuple(_text(note) for note in raw.get("notes") or () if _text(note)),
    )


def parse_page(raw: dict[str, Any]) -> HelpPage:
    page_id, file = _text(raw.get("id")), _text(raw.get("file"))
    if not page_id or not file.endswith(".png"):
        raise HelpSpecError(f"help page needs an id and a .png file: {raw!r}")
    columns = tuple(tuple(parse_section(section) for section in column) for column in raw.get("columns") or ())
    if not any(columns):
        raise HelpSpecError(f"help page {page_id!r} has no sections")
    art = raw.get("art") or ()
    return HelpPage(
        id=page_id,
        file=file,
        title=_text(raw.get("title")),
        columns=columns,
        subtitle=_text(raw.get("subtitle")),
        footnote=_text(raw.get("footnote")),
        art=(art,) if isinstance(art, str) else tuple(art),
        raw=raw,
    )


def load_pages(path: Path = PAGES_PATH) -> list[HelpPage]:
    pages = [parse_page(raw) for raw in json.loads(path.read_text(encoding="utf-8"))["pages"]]
    for key in ("id", "file"):
        seen: set[str] = set()
        for page in pages:
            value = getattr(page, key)
            if value in seen:
                raise HelpSpecError(f"duplicate help page {key}: {value!r}")
            seen.add(value)
    return pages


def _focus(value: Any, fallback: tuple[float, float] | None) -> tuple[float, float] | None:
    if value is None:
        return fallback
    x, y = (float(part) for part in value)
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise HelpSpecError(f"art focus must be fractions between 0 and 1: {value!r}")
    return x, y


def load_gallery(path: Path = GALLERY_PATH) -> Gallery:
    data = json.loads(path.read_text(encoding="utf-8"))
    artworks: dict[str, Artwork] = {}
    for raw in data.get("art") or ():
        art_id = _text(raw.get("id"))
        if not art_id or not re.fullmatch(r"[A-Za-z0-9_-]+", art_id):
            raise HelpSpecError(f"art id must be letters, digits, '-' or '_': {raw!r}")
        if art_id in artworks:
            raise HelpSpecError(f"duplicate art id: {art_id!r}")
        art_path = path.parent / _text(raw.get("file"))
        if not art_path.is_file():
            raise HelpSpecError(f"art {art_id!r} file not found: {art_path}")
        artworks[art_id] = Artwork(
            id=art_id,
            path=art_path,
            focus=_focus(raw.get("focus"), (0.5, 0.3)),
            backdrop_focus=_focus(raw.get("backdrop_focus"), None),
            credit=_text(raw.get("credit")),
            raw=raw,
        )
    default = data.get("default") or ()
    return Gallery(artworks, (default,) if isinstance(default, str) else tuple(default))


# ---------------------------------------------------------------------------
# Artwork choice by aspect ratio

#: The standee window is the sharp part of the card, so it is the only gate: an artwork
#: stays a candidate while at least this much of it survives into the window. The blurred
#: backdrop sits behind a frosted panel and tolerates heavy cropping, so it never filters -
#: it only orders the pool, best match first.
WINDOW_VISIBILITY_FLOOR = 0.15

#: Card metrics from the stylesheet below: `.card` is inset from `.page`, and `.panel`
#: is inset from `.card` again. Keep in sync with the CSS.
CARD_WIDTH_INSET = 30
CARD_HEIGHT_INSET = 100
PANEL_HEIGHT_INSET = 72


def page_height(page: HelpPage, theme: HelpTheme = HelpTheme()) -> int:
    """Rendered height of a page, read from its shipped PNG.

    The layout does not depend on which artwork is used, so the shipped primary is a
    reliable proxy for the height of any artwork on the same page.
    """
    path = HELP_IMAGE_DIR / page.file
    if path.is_file():
        from PIL import Image

        with Image.open(path) as image:
            return image.height
    return theme.min_card_height + CARD_HEIGHT_INSET


def backdrop_ratio(height: int, theme: HelpTheme = HelpTheme()) -> float:
    """Aspect ratio of the blurred backdrop box."""
    return (theme.page_width - CARD_WIDTH_INSET) / max(1, height - CARD_HEIGHT_INSET)


def window_ratio(height: int, theme: HelpTheme = HelpTheme()) -> float:
    """Aspect ratio of the sharp standee window."""
    return theme.window_width / max(1, height - CARD_HEIGHT_INSET - PANEL_HEIGHT_INSET)


def ratio_candidates(
    page: HelpPage,
    gallery: Gallery,
    theme: HelpTheme = HelpTheme(),
) -> tuple[Artwork, ...]:
    """Every gallery artwork this page may use, closest backdrop match first.

    The whole gallery is eligible: the backdrop is blurred and sits behind a frosted
    panel, so cropping it hard costs nothing, while the standee window keeps the subject
    in frame because every crop is centred on the artwork's focus point.
    `WINDOW_VISIBILITY_FLOOR` is the single gate - it drops only artwork that would leave
    the window showing almost nothing, and falls back to the whole gallery when a page is
    so extreme that nothing clears it, so a send can always produce an image.

    The order is by backdrop match so the result is deterministic and the best-fitting
    artwork comes first; the caller draws uniformly from the pool.
    """
    height = page_height(page, theme)
    backdrop = backdrop_ratio(height, theme)
    window = window_ratio(height, theme)
    scored = [
        (
            cover_visibility(image_ratio(str(art.path)), backdrop),
            cover_visibility(image_ratio(str(art.path)), window),
            art,
        )
        for art in gallery.artworks.values()
    ]
    scored.sort(key=lambda row: (-row[0], row[2].id))
    usable = [row for row in scored if row[1] >= WINDOW_VISIBILITY_FLOOR] or scored
    return tuple(row[2] for row in usable)


# ---------------------------------------------------------------------------
# Targets and staleness


def spec_digest(page: HelpPage, art: Artwork, theme: HelpTheme = HelpTheme()) -> str:
    page_fields = {key: value for key, value in page.raw.items() if key != "art"}
    payload = {
        "template": TEMPLATE_VERSION,
        "theme": theme.digest_fields(),
        "page": page_fields,
        "art": art.raw,
        "art_sha256": art.sha256(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_targets(
    pages: Iterable[HelpPage],
    gallery: Gallery,
    help_dir: Path = HELP_IMAGE_DIR,
    theme: HelpTheme = HelpTheme(),
) -> list[RenderTarget]:
    """One target per page: the first artwork ships as ``<file>``.

    Variants are no longer pre-rendered. The artwork actually sent is picked by
    :func:`ratio_candidates` and rendered on demand by
    ``otae_bot.infrastructure.rendering.help_runtime``, so the shipped PNG only has to
    cover the case where no browser is available.
    """
    targets = []
    for page in pages:
        art = gallery.resolve(page)[0]
        path = help_dir / page.file
        targets.append(RenderTarget(page, art, path, spec_digest(page, art, theme), True))
    return targets


def read_digest(path: Path) -> str | None:
    from PIL import Image

    if not path.is_file():
        return None
    with Image.open(path) as image:
        return (getattr(image, "text", None) or {}).get(DIGEST_KEY)


def stale_targets(targets: Iterable[RenderTarget]) -> list[RenderTarget]:
    return [target for target in targets if read_digest(target.path) != target.digest]


def orphan_variants(targets: Sequence[RenderTarget], help_dir: Path = HELP_IMAGE_DIR) -> list[Path]:
    """Variant PNGs on disk that no page asks for any more."""
    wanted = {target.path.resolve() for target in targets}
    return sorted(
        path for path in (help_dir / VARIANT_DIR_NAME).glob("*/*.png") if path.resolve() not in wanted
    )


# ---------------------------------------------------------------------------
# HTML


def _object_position(focus: tuple[float, float]) -> str:
    return f"{focus[0] * 100:.1f}% {focus[1] * 100:.1f}%"


def _dots(color: str) -> str:
    circles = "".join(
        f'<circle cx="{column * 16 + 3 + (3 - row) * 8}" cy="{row * 12 + 3}" r="3"/>'
        for row in range(4) for column in range(10)
    )
    return f'<svg class="dots" width="174" height="42" viewBox="0 0 174 42" fill="{color}">{circles}</svg>'


def _item_html(item: HelpItem) -> str:
    badge = f'<span class="badge">{escape(item.badge)}</span>' if item.badge else ""
    # If example is set separately, don't duplicate raw example string inside the inline description
    desc_text = item.description
    if item.example and desc_text == item.example:
        desc_text = ""
    elif item.example and desc_text.startswith("例:"):
        desc_text = desc_text[3:].strip()
    elif item.example and desc_text.startswith("例："):
        desc_text = desc_text[3:].strip()
    elif item.example and desc_text.strip() == "2腐蚀 200":
        desc_text = ""
    description = f'<span class="desc">{escape(desc_text)}</span>' if desc_text else ""
    eg = (
        f'<div class="item-example" data-fit>'
        f'<span class="eg-tag">例</span><span class="eg-code">{escape(item.example)}</span>'
        f'</div>'
        if item.example else ""
    )
    return f'<div class="item" data-fit><span class="cmd">{escape(item.command)}</span>{badge}{description}</div>{eg}'


def _section_html(section: HelpSection) -> str:
    access = f'<span class="access">{escape(section.access)}</span>' if section.access else ""
    notes = "".join(f'<div class="note" data-fit>{escape(note)}</div>' for note in section.notes)
    return (
        f'<section><div class="heading" data-fit>{escape(section.heading)}{access}</div>'
        + "".join(_item_html(item) for item in section.items)
        + notes
        + "</section>"
    )


def font_face_css(theme: HelpTheme) -> str:
    return "".join(
        f'@font-face{{font-family:"{FONT_FAMILY}";font-weight:{weight};'
        f'src:url("{(theme.font_dir / name).resolve().as_uri()}") format("truetype")}}'
        for weight, name in theme.font_files
    )


def render_html(page: HelpPage, art: Artwork, theme: HelpTheme = HelpTheme()) -> str:
    card_width = theme.page_width - 30
    columns = "".join(
        f'<div class="col">{"".join(_section_html(section) for section in column)}</div>'
        for column in page.columns
    )
    art_uri = art.path.resolve().as_uri()
    subtitle = f'<span class="subtitle" data-fit>{escape(page.subtitle)}</span>' if page.subtitle else ""
    footnote = f'<p class="foot" data-fit>{escape(page.footnote)}</p>' if page.footnote else ""
    t = theme
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{escape(page.title)}</title><style>
{font_face_css(t)}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:#fff}}
body{{font-family:"{FONT_FAMILY}",sans-serif;font-synthesis:none}}
.page{{width:{t.page_width}px;padding:17px 13px 16px 17px;background:#fff}}
.banner{{position:relative;height:52px;border-radius:21px;background:{t.accent}}}
.titles{{position:absolute;left:32px;top:0;height:52px;display:flex;align-items:baseline;gap:16px;
  padding-top:9px;white-space:nowrap}}
.titles h1{{font-size:30px;line-height:34px;font-weight:700;letter-spacing:.5px;color:#fff}}
.subtitle{{font-size:18px;font-weight:500;color:rgba(255,255,255,.86)}}
.dots{{position:absolute;right:166px;top:5px}}
.pill{{position:absolute;right:23px;top:7px;width:143px;height:36px;border-radius:18px;background:#fff}}
.card{{position:relative;margin-top:15px;width:{card_width}px;min-height:{t.min_card_height}px;display:flex;gap:22px;
  border:2px solid {t.border_color};border-radius:24px;overflow:hidden;background:#b9c3c0;padding:38px 34px 30px 37px}}
.backdrop{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  object-position:{_object_position(art.backdrop_focus or art.focus)};
  filter:blur({t.backdrop_blur}px);transform:scale(1.06)}}
.panel{{position:relative;flex:1;min-width:0;display:flex;flex-direction:column;min-height:{t.min_card_height - 72}px;
  border-radius:24px;background:rgba(255,255,255,{t.panel_alpha});padding:34px 32px 18px 36px}}
.window{{position:relative;flex:0 0 {t.window_width}px;border-radius:24px;overflow:hidden;
  border:3px solid rgba(255,255,255,.9);box-shadow:0 6px 18px rgba(37,30,25,.28)}}
.window img{{display:block;width:100%;height:100%;object-fit:cover;object-position:{_object_position(art.focus)}}}
.cols{{display:grid;grid-template-columns:repeat({max(1, len(page.columns))},minmax(0,1fr));column-gap:28px}}
.col{{min-width:0}}
section+section{{margin-top:20px}}
.heading{{font-size:22px;line-height:30px;font-weight:700;color:{t.heading_color};margin-bottom:4px}}
.access{{margin-left:8px;font-size:14px;font-weight:500;color:{t.accent};vertical-align:2px}}
.item{{font-size:18px;line-height:25px;font-weight:500;color:{t.body_color};
  padding-left:1.1em;text-indent:-1.1em;overflow-wrap:anywhere;text-wrap:pretty}}
.cmd{{color:{t.heading_color}}}
.desc{{margin-left:.45em}}
.badge{{display:inline-block;text-indent:0;margin-left:.4em;padding:0 6px;border-radius:8px;
  font-size:13px;line-height:18px;color:{t.accent};background:#e3ecf8;vertical-align:1px}}
.item-example{{display:flex;align-items:baseline;gap:6px;margin:2px 0 3px 14px;
  font-size:15px;line-height:20px;overflow-wrap:anywhere}}
.eg-tag{{flex-shrink:0;padding:0 5px;border-radius:4px;background:#e2ebf6;color:{t.accent};
  font-size:12px;font-weight:700;line-height:16px}}
.eg-code{{color:#434343;font-weight:600;background:rgba(0,0,0,.04);padding:0 5px;border-radius:4px}}
.note{{margin-top:6px;padding-left:10px;border-left:3px solid {t.accent_soft};
  font-size:17px;line-height:24px;font-weight:500;color:#6b6b6b;overflow-wrap:anywhere;text-wrap:pretty}}
.foot{{margin-top:auto;padding-top:12px;text-align:right;font-size:18px;line-height:22px;font-weight:500;
  color:{t.body_color};overflow-wrap:anywhere}}
</style></head><body><div class="page">
<div class="banner"><div class="titles"><h1 data-fit>{escape(page.title)}</h1>{subtitle}</div>{_dots(t.accent_soft)}<div class="pill"></div></div>
<div class="card"><img class="backdrop" alt="backdrop" src="{art_uri}">
<div class="panel"><div class="cols">{columns}</div>{footnote}</div>
<div class="window"><img alt="{escape(art.id)}" src="{art_uri}"></div></div>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Browser rendering and layout checks

LAYOUT_CHECK = """() => {
  const root = document.querySelector('.page');
  const rootBox = root.getBoundingClientRect();
  const escaped = [];
  for (const node of root.querySelectorAll('[data-fit]')) {
    const box = node.getBoundingClientRect();
    const owner = (node.closest('.panel, .banner') || root).getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(node);
    const outside = [...range.getClientRects()].some(r =>
      r.left < owner.left - 1 || r.right > owner.right + 1 || r.top < owner.top - 1 || r.bottom > owner.bottom + 1);
    if (outside || node.scrollWidth > node.clientWidth + 1 || box.right > rootBox.right + 1)
      escaped.push(node.textContent.trim().slice(0, 80));
  }
  const titles = document.querySelector('.titles').getBoundingClientRect();
  const dots = document.querySelector('.dots').getBoundingClientRect();
  const cols = [...document.querySelectorAll('.col')].map(n => {
    const top = n.getBoundingClientRect().top;
    const last = n.lastElementChild;
    const bottom = last ? last.getBoundingClientRect().bottom : top;
    return {top, bottom, height: bottom - top};
  });
  const foot = document.querySelector('.foot');
  return {
    width: Math.round(rootBox.width),
    height: Math.round(rootBox.height),
    escaped,
    titleClear: titles.right <= dots.left - 8,
    footClear: !foot || foot.getBoundingClientRect().top >= Math.max(0, ...cols.map(r => r.bottom)) - 1,
    columnHeights: cols.map(r => Math.round(r.height)),
    fontsLoaded: document.fonts.size > 0 && [...document.fonts].every(f => f.status === 'loaded'),
    brokenImages: [...document.images].filter(i => !i.complete || !i.naturalWidth).map(i => i.alt),
  };
}"""


def layout_problems(report: dict[str, Any], theme: HelpTheme = HelpTheme()) -> list[str]:
    problems = []
    if report.get("escaped"):
        problems.append(f"text escapes its box: {report['escaped']}")
    if not report.get("titleClear"):
        problems.append("title/subtitle runs into the banner dots")
    if not report.get("footClear"):
        problems.append("footnote overlaps the columns")
    if not report.get("fontsLoaded"):
        problems.append("help fonts did not load")
    if report.get("brokenImages"):
        problems.append(f"images failed to load: {report['brokenImages']}")
    if report.get("externalRequests"):
        problems.append(f"page tried to reach the network: {report['externalRequests']}")
    if report.get("width") != theme.page_width:
        problems.append(f"width {report.get('width')} != {theme.page_width}")
    if (report.get("height") or 0) > theme.max_height:
        problems.append(f"height {report.get('height')} > {theme.max_height}; trim the text or split the page")
    return problems


def finalize_png(content: bytes, digest: str) -> bytes:
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text(DIGEST_KEY, digest)
    with Image.open(io.BytesIO(content)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG", optimize=True, pnginfo=info)
    return buffer.getvalue()


async def launch_browser(playwright):
    last_error: Exception | None = None
    for channel in (None, "chrome", "msedge"):
        try:
            return await playwright.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
        except Exception as exc:  # noqa: BLE001 - try the next installed browser
            last_error = exc
    raise RuntimeError("No browser found. Run: playwright install chromium") from last_error


async def render_target(
    browser,
    page: HelpPage,
    art: Artwork,
    html_path: Path,
    theme: HelpTheme = HelpTheme(),
) -> tuple[bytes, dict[str, Any]]:
    """Render one page with one artwork; returns raw PNG bytes and the layout report."""
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(page, art, theme), encoding="utf-8")
    tab = await browser.new_page(viewport={"width": theme.page_width, "height": 900}, device_scale_factor=1)
    external: list[str] = []
    tab.on("request", lambda request: external.append(request.url)
           if request.url.startswith(("http:", "https:")) else None)
    await tab.route(re.compile(r"^https?://"), lambda route: route.abort())
    try:
        await tab.goto(html_path.resolve().as_uri())
        await tab.evaluate("document.fonts.ready")
        await tab.evaluate("Promise.all([...document.images].map(i => i.decode().catch(() => null)))")
        report = await tab.evaluate(LAYOUT_CHECK)
        report["externalRequests"] = external
        png = await tab.locator(".page").screenshot()
    finally:
        await tab.close()
    return png, report


def iter_sections(page: HelpPage) -> Iterator[HelpSection]:
    for column in page.columns:
        yield from column
