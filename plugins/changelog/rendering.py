"""离线 HTML/CSS 卡片，复用项目共享的浏览器渲染器。"""

from __future__ import annotations

import base64
import tempfile
from functools import lru_cache
from pathlib import Path

from otae_bot.infrastructure.rendering.browser import screenshot_web_element

from .presentation import ChangelogPage, text

CARD_WIDTH = 1080
CARD_MAX_HEIGHT = 6000
ASSETS = Path(__file__).parent / "assets"
FONT = Path(__file__).resolve().parents[2] / "assets/font/steamInfo/MiSans-Regular.ttf"

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
    "img-src data:; base-uri 'none'; form-action 'none'"
)


@lru_cache(maxsize=1)
def _styles() -> str:
    """内嵌字体与样式表：渲染期间不发起任何网络请求。"""
    font = base64.b64encode(FONT.read_bytes()).decode("ascii")
    return (
        "@font-face{font-family:ChangeLogSans;"
        f"src:url(data:font/ttf;base64,{font}) format(\"truetype\");"
        "font-weight:100 900;font-display:block}"
        + (ASSETS / "card.css").read_text(encoding="utf-8")
    )


def page_html(page: ChangelogPage) -> str:
    """把一张卡片渲染成完整的离线 HTML 文档。"""
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>{text(page.title)} · 更新日志</title>
<style>{_styles()}</style></head>
<body><main class="cl-card">
<header class="cl-brand-row"><div class="cl-brand"><span class="cl-mark" aria-hidden="true"></span>OTAE / CHANGELOG</div>
<div class="cl-edition">更新日志<br>RELEASE NOTES</div></header>
<div class="cl-head"><div><h1>{text(page.title)}</h1><p>{text(page.subtitle)}</p></div>
<div class="cl-badge"><strong>{page.number:02}</strong>{page.total:02} / {text(page.section)}</div></div>
<article>{page.body}</article>
<div class="cl-note">{text(page.why)}</div>
<footer class="cl-foot"><p>数据来自仓库提交历史，按版本聚合；每条更新都对应真实提交，合并提交不计入。</p>
<p>校验：scripts/generate_changelog.py --check · 不重不漏。</p>
<div class="cl-bottom"><span>OTAE BOT / 更新日志</span><span>每个版本改了什么，都在这里。</span><span>{page.number:02} / {page.total:02}</span></div>
</footer></main></body></html>'''


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
            strict_max_height=True,
            wait_for_fonts=True,
            settle_ms=0,
        )
    finally:
        path.unlink(missing_ok=True)


__all__ = ["CARD_MAX_HEIGHT", "CARD_WIDTH", "page_html", "render_page"]
