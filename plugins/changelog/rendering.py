"""离线 HTML/CSS 卡片，复用项目共享的浏览器渲染器。"""

from __future__ import annotations

import base64
import tempfile
from functools import lru_cache
from pathlib import Path

from otae_bot.infrastructure.rendering.browser import screenshot_web_element

from .presentation import ChangelogPage, text

#: 与雷达卡片同一张 820px 画布，按 2x 渲染，手机上约以一半尺寸阅读。
CARD_WIDTH = 820
CARD_MAX_HEIGHT = 6000
DEVICE_SCALE_FACTOR = 2.0
ASSETS = Path(__file__).parent / "assets"
FONT = Path(__file__).resolve().parents[2] / "assets/font/steamInfo/MiSans-Regular.ttf"

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
    "img-src data:; base-uri 'none'; form-action 'none'"
)


def _font_face(path: Path, weight: int) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        "@font-face{font-family:ChangeLogSans;"
        f'src:url(data:font/ttf;base64,{data}) format("truetype");'
        f"font-weight:{weight};font-display:block}}"
    )


@lru_cache(maxsize=1)
def _styles() -> str:
    """内嵌字体与样式表：渲染期间不发起任何网络请求。"""
    return (
        _font_face(FONT, 400)
        + _font_face(FONT.with_name("MiSans-Bold.ttf"), 700)
        + (ASSETS / "card.css").read_text(encoding="utf-8")
    )


def page_html(page: ChangelogPage) -> str:
    """把一张卡片渲染成完整的离线 HTML 文档。"""
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>{text(page.title)} · 更新日志</title>
<style>{_styles()}</style></head>
<body><main class="cl-card">
<header class="cl-masthead"><span class="cl-kicker">更新日志</span>
<span class="cl-index">{text(page.section)} <b>{page.number:02}</b> / {page.total:02}</span></header>
<h1>{text(page.title)}</h1><p class="cl-subtitle">{text(page.subtitle)}</p>
<article>{page.body}</article>
<footer class="cl-foot"><b>OTAE BOT</b>每个版本改了什么，都在这里。</footer>
</main></body></html>'''


async def render_page(page: ChangelogPage) -> bytes:
    """渲染单张卡片为 PNG。"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", encoding="utf-8", delete=False
    ) as file:
        file.write(page_html(page))
        path = Path(file.name)
    try:
        return await screenshot_web_element(
            path.as_uri(),
            ".cl-card",
            viewport=(CARD_WIDTH, 900),
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=DEVICE_SCALE_FACTOR,
            strict_max_height=True,
            wait_for_fonts=True,
            settle_ms=0,
        )
    finally:
        path.unlink(missing_ok=True)


__all__ = ["CARD_MAX_HEIGHT", "CARD_WIDTH", "DEVICE_SCALE_FACTOR", "page_html", "render_page"]
