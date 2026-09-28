"""Offline HTML/CSS cards, using the project's shared browser renderer."""

from __future__ import annotations

import base64
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from otae_bot.infrastructure.rendering.browser import screenshot_web_element

from .matrix import MATRIX_MAX_HEIGHT, MATRIX_SAMPLE_REMINDER, MATRIX_WIDTH
from .presentation import RadarPage, notice, stamp, text

#: Phone-first canvas: rendered at 2x, it reads at roughly half size on a phone.
CARD_WIDTH = 820
CARD_MAX_HEIGHT = 5000
#: Rasterise at 2x so the delivered PNG stays sharp after the chat client
#: downscales it to a phone-width viewport. Matches the endfield cards.
DEVICE_SCALE_FACTOR = 2.0
#: Chromium caps one screenshot near 16384 device pixels per axis. At 2x the CSS
#: budget must shrink by the same factor, or tall cards fail to render at all.
DEVICE_PIXEL_LIMIT = 16384
ASSETS = Path(__file__).parent / "assets"
FONT = Path(__file__).resolve().parents[2] / "assets/font/steamInfo/MiSans-Regular.ttf"
_MODEL_ICONS = {
    **{
        name: f"{name}.png"
        for name in (
            "astra",
            "sol",
            "terra",
            "luna",
            "system",
            "deepseek",
            "gemini",
            "hunyuan",
        )
    },
    **{name: f"{name}.svg" for name in ("claude", "glm", "kimi", "grok", "generic")},
}


@lru_cache(maxsize=16)
def _model_icon(icon: str) -> str:
    path = ASSETS / "models" / _MODEL_ICONS.get(icon, "generic.svg")
    kind = "image/svg+xml" if path.suffix == ".svg" else "image/png"
    return f"data:{kind};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'"


def _font_face(path: Path, weight: int) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'@font-face{{font-family:RadarSans;src:url(data:font/ttf;base64,{data}) format("truetype");font-weight:{weight};font-display:block}}'


@lru_cache(maxsize=1)
def _styles() -> str:
    return (
        _font_face(FONT, 400)
        + _font_face(FONT.with_name("MiSans-Bold.ttf"), 700)
        + (ASSETS / "card.css").read_text(encoding="utf-8")
    )


SOURCE_NOTE = "数据来源 api.codexradar.com · 众测评测分数，不等同于人类智商 · — 缺失 · ~ 估算"


def _masthead(page: RadarPage) -> str:
    return (
        '<header class="masthead"><span class="kicker">AI 智商雷达</span>'
        f'<span class="page-index">{text(page.section)} <b>{page.number:02}</b> / {page.total:02}</span></header>'
        f'<h1>{text(page.title)}</h1><p class="subtitle">{text(page.subtitle)}</p>'
    )


def page_html(page: RadarPage, *, preview: bool = False) -> str:
    if page.layout == "matrix":
        return _matrix_html(page, preview=preview)
    meta = page.meta
    context = footer = ""
    if meta:
        chips = [f'<span class="chip chip-accent">{text(meta.benchmark_id)}</span>']
        if meta.score_label:
            chips.append(f'<span class="chip">{text(meta.score_label)}</span>')
        if meta.rolling_window:
            chips.append(f'<span class="chip">最近 {meta.rolling_window} 次有效运行</span>')
        if meta.note:
            chips.append(f'<span class="chip">{text(meta.note)}</span>')
        context = '<div class="context">' + "".join(chips) + "</div>"
        if meta.stale:
            context += notice(
                "数据可能过期 · 上游暂不可用，当前展示陈旧缓存。", warning=True
            )
        source_label = (
            "榜单快照"
            if page.section in {"榜单", "模型", "对比", "趋势"}
            else "源数据"
        )
        footer = f"<p>{source_label} <b>{text(stamp(meta.source_updated_at))}</b> · 获取 <b>{text(stamp(meta.fetched_at))}</b></p>"
        if page.section in {"榜单", "模型", "对比"}:
            footer += "<p>综合 IQ 的独立更新时间与缓存状态上游未提供；效率数据时间见各条记录。</p>"
        elif page.section == "趋势":
            footer += "<p>曲线的数据时间见图中起止点；历史序列的缓存状态上游未提供。</p>"
        modes = [
            f"{label} {text(value)}"
            for label, value in (
                ("统计", meta.mode),
                ("计分", meta.scoring_mode),
                ("推荐", meta.recommendation_mode),
            )
            if value
        ]
        if modes:
            footer += "<p>口径 " + " · ".join(modes) + "</p>"
    else:
        footer = "<p>本页为命令说明或频道配置，不含数据更新时间。</p>"
    preview_label = (
        '<div class="preview-label">离线设计预览 · 来自仓库录制夹具的裁剪样本，不是实时榜单；边界示例另行标注。</div>'
        if preview
        else ""
    )
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>{text(page.title)} · AI 智商雷达</title><style>{_styles()}</style></head>
<body><main class="radar-card">{preview_label}
{_masthead(page)}
{context}<article>{page.body}</article>
<footer class="card-foot"><p class="why"><b>读图</b>{text(page.why)}</p>
<div class="meta-footer">{footer}</div>
<div class="bottom-row"><b>OTAE BOT</b><span>{SOURCE_NOTE}</span></div></footer>
</main></body></html>'''


def _matrix_html(page: RadarPage, *, preview: bool = False) -> str:
    meta = page.meta
    source_time = stamp(meta.source_updated_at if meta else None)
    preview_note = " · 预览快照" if preview else ""
    status = (
        '<div class="matrix-status">效率数据可能过期 · 当前为陈旧缓存，各模型历史状态另行标注。</div>'
        if meta and meta.stale
        else ""
    )
    styles = _styles() + (ASSETS / "matrix.css").read_text(encoding="utf-8")
    body = re.sub(
        r'data-model-icon="([a-z]+)"',
        lambda match: f'{match[0]} src="{_model_icon(match[1])}"',
        page.body,
    )
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>AI 智商雷达 · 模型档位总览</title><style>{styles}</style></head>
<body><main class="radar-card radar-matrix"><header class="matrix-header">
{_masthead(page)}<p class="matrix-headnote">快照 {text(source_time)}{preview_note}</p></header>
{status}{body}
<footer class="matrix-footer"><div class="matrix-legend"><span><b>大字 IQ / 曲线</b>同模型跨档位历史末点与走势</span><span><b>档位 IQ / 得分</b>本频道效率口径，整数展示</span><span><b>数字颜色</b>0–150 红 → 黄 → 绿，按原始 IQ 连续取色</span><span><b>n</b>样本量</span><span><b>~</b>耗时与 API 等价成本为估算，费用按美元/次</span><span><b>—</b>暂无数据</span><span><b>＊ 样本偏少</b>0&lt;n&lt;{MATRIX_SAMPLE_REMINDER} 为前端提醒线，非上游统计判定；不提示不代表结果稳定</span></div>
<div class="bottom-row"><b>OTAE BOT</b><span>统计 {text(meta.mode if meta else None)} · 时间均为 UTC · 按研发厂商分组，组内保留源顺序 · {SOURCE_NOTE}</span></div></footer>
</main></body></html>'''


async def render_page(page: RadarPage) -> bytes:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", encoding="utf-8", delete=False
    ) as file:
        file.write(page_html(page))
        path = Path(file.name)
    matrix = page.layout == "matrix"
    # The CSS budget is in layout pixels; the rasteriser multiplies it by
    # DEVICE_SCALE_FACTOR, so the device-pixel ceiling has to be divided back out.
    budget = min(
        MATRIX_MAX_HEIGHT if matrix else CARD_MAX_HEIGHT,
        int(DEVICE_PIXEL_LIMIT / DEVICE_SCALE_FACTOR),
    )
    try:
        return await screenshot_web_element(
            path.as_uri(),
            ".radar-card",
            viewport=(MATRIX_WIDTH if matrix else CARD_WIDTH, 900),
            max_height=budget,
            device_scale_factor=DEVICE_SCALE_FACTOR,
            strict_max_height=True,
            wait_for_fonts=True,
            wait_for_images=matrix,
            settle_ms=0,
        )
    finally:
        path.unlink(missing_ok=True)
