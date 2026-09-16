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

CARD_WIDTH = 1080
CARD_MAX_HEIGHT = 5000
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


@lru_cache(maxsize=1)
def _matrix_bold_font() -> str:
    bold = base64.b64encode(FONT.with_name("MiSans-Bold.ttf").read_bytes()).decode(
        "ascii"
    )
    return f'@font-face{{font-family:RadarSans;src:url(data:font/ttf;base64,{bold}) format("truetype");font-weight:700;font-display:block}}'


@lru_cache(maxsize=1)
def _styles() -> str:
    font = base64.b64encode(FONT.read_bytes()).decode("ascii")
    return (
        f'@font-face{{font-family:RadarSans;src:url(data:font/ttf;base64,{font}) format("truetype");font-weight:100 900;font-display:block}}'
        + (ASSETS / "card.css").read_text(encoding="utf-8")
    )


def page_html(page: RadarPage, *, preview: bool = False) -> str:
    if page.layout == "matrix":
        return _matrix_html(page, preview=preview)
    meta = page.meta
    context = footer = ""
    if meta:
        context = '<div class="context">'
        context += f"<strong>{text(meta.benchmark_id)}</strong><span>{text(meta.score_label or '评测口径见数据区')}</span>"
        if meta.rolling_window:
            context += f"<span>最近 {meta.rolling_window} 次有效运行</span>"
        if meta.note:
            context += f"<span>{text(meta.note)}</span>"
        context += "</div>"
        if meta.stale:
            context += notice(
                "数据可能过期 · 上游暂不可用，当前展示陈旧缓存。", warning=True
            )
        source_label = (
            "榜单快照时间"
            if page.section in {"榜单", "模型", "对比", "趋势"}
            else "源数据时间"
        )
        footer = f"<p>{source_label} {text(stamp(meta.source_updated_at))} · 获取时间 {text(stamp(meta.fetched_at))}</p>"
        if page.section in {"榜单", "模型", "对比"}:
            footer += "<p>综合 IQ 独立更新时间与缓存状态：未提供；效率数据时间见各条记录。</p>"
        elif page.section == "趋势":
            footer += "<p>曲线的数据时间见图中起止点，独立历史缓存状态未提供。</p>"
        footer += f"<p>mode: {text(meta.mode)} · scoring: {text(meta.scoring_mode)} · recommendation_mode: {text(meta.recommendation_mode)}</p>"
    else:
        footer = "<p>数据时间 — · 本页为命令说明或频道配置；未提供数据更新时间。</p>"
    preview_label = (
        '<div class="preview-label">离线设计预览 · 来自仓库录制夹具的裁剪样本，不是实时榜单；边界示例另行标注。</div>'
        if preview
        else ""
    )
    csp = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}"><title>{text(page.title)} · AI 智商雷达</title><style>{_styles()}</style></head>
<body><main class="radar-card">{preview_label}
<header class="brand-row"><div class="brand"><span class="brand-icon" aria-hidden="true"></span>AI / RADAR</div><div class="edition">模型观察手册<br>INTELLIGENCE OBSERVATORY</div></header>
<div class="page-head"><div><h1>{text(page.title)}</h1><p>{text(page.subtitle)}</p></div><span class="section-label">{text(page.section)} / {page.number:02}</span></div>
{context}<article>{page.body}</article>
<aside class="why"><span>i</span><div><h3>为什么看这些数据</h3><p>{text(page.why)}</p></div></aside>
<footer><div class="meta-footer">{footer}<p>数据来源 api.codexradar.com · 众测评测分数，不等同于人类智商 · — 表示缺失 · ~ 表示估算</p></div>
<div class="bottom-row"><span>OTAE BOT / AI 智商雷达</span><span>MODEL INTELLIGENCE, IN CONTEXT.</span><span>{page.number:02} / {page.total:02}</span></div></footer>
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
    styles = (
        _styles()
        + _matrix_bold_font()
        + (ASSETS / "matrix.css").read_text(encoding="utf-8")
    )
    body = re.sub(
        r'data-model-icon="([a-z]+)"',
        lambda match: f'{match[0]} src="{_model_icon(match[1])}"',
        page.body,
    )
    csp = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}"><title>AI 智商雷达 · 模型档位总览</title><style>{styles}</style></head>
<body><main class="radar-card radar-matrix"><header class="matrix-header">
<div class="brand-row"><div class="brand"><span class="brand-icon" aria-hidden="true"></span>AI / RADAR</div><div class="edition">模型观察手册<br>INTELLIGENCE OBSERVATORY</div></div>
<div class="page-head"><div><h1>AI 智商雷达</h1><p>模型与推理档位 / MODEL MATRIX</p></div><div class="matrix-headnote"><strong>全量总览 / FIELD GUIDE</strong><br>{text(source_time)}{preview_note}</div></div></header>
{status}{body}
<footer class="matrix-footer"><div class="matrix-legend"><span><b>大字 IQ / 趋势</b> 同模型跨档位历史 · 纵轴固定 0–150</span><span><b>档位 IQ / 得分</b> 本频道效率口径 · IQ 按整数展示</span><span><b>数字颜色</b> 0–150 红 → 金黄 → 绿 · 11 阶，按原始 IQ 连续取色</span><span><b>n</b> 样本量</span><span><b>~</b> 耗时与 API 等价成本估算 · 美元/次</span><span><b>—</b> 暂无数据</span><span><b>样本偏少</b> 0&lt;n&lt;{MATRIX_SAMPLE_REMINDER} 为前端提醒线，非上游统计判定；不提示不代表结果稳定</span></div>
<div class="matrix-bottom"><span>OTAE / AI RADAR</span><span>api.codexradar.com · {text(meta.mode if meta else None)} · 各项时间为 UTC · 按模型研发厂商分组，组内保留源顺序</span><span>全部模型 · 单张长图</span></div></footer>
</main></body></html>'''


async def render_page(page: RadarPage) -> bytes:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", encoding="utf-8", delete=False
    ) as file:
        file.write(page_html(page))
        path = Path(file.name)
    try:
        return await screenshot_web_element(
            path.as_uri(),
            ".radar-card",
            viewport=(MATRIX_WIDTH if page.layout == "matrix" else CARD_WIDTH, 900),
            max_height=MATRIX_MAX_HEIGHT
            if page.layout == "matrix"
            else CARD_MAX_HEIGHT,
            strict_max_height=True,
            wait_for_fonts=True,
            wait_for_images=page.layout == "matrix",
            settle_ms=0,
        )
    finally:
        path.unlink(missing_ok=True)
