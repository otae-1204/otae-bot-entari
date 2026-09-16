"""Render the AI radar card suite offline with recorded fixtures.

Usage: .venv/bin/python scripts/preview_ai_radar.py
Outputs desktop PNGs, responsive HTML, a gallery and layout checks under
output/ai-radar. No bot startup, live queries or credentials required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from plugins.radar import handlers
from plugins.radar.config import RadarConfig
from plugins.radar.models import DegradationAlert, RadarMeta, TrendPoint
from plugins.radar.presentation import alert_pages, ranking_pages, text, trend_pages
from plugins.radar.provider import RadarClient
from plugins.radar.rendering import CARD_MAX_HEIGHT, page_html
from plugins.radar.service import RadarService

FIXTURES = ROOT / "tests/fixtures/radar"
ROUTES = {
    "benchmarks": "benchmarks",
    "leaderboard": "leaderboard",
    "table": "table",
    "radar-insights": "insights",
    "intelligence-efficiency": "efficiency",
    "model-metrics": "model_metrics",
    "iq-history": "iq_history",
    "events": "events",
}


async def fixture_http(url, *, params=None, **kwargs):
    endpoint = url.rsplit("/", 1)[-1]
    name = ROUTES[endpoint]
    if (params or {}).get("benchmark") == "pompeii-adjacency" and endpoint in {
        "table",
        "intelligence-efficiency",
    }:
        name += "_pompeii"
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fixture_service() -> RadarService:
    config = RadarConfig()
    return RadarService(RadarClient(config, http=fixture_http), config)


async def preview_pages(service):
    old = handlers._service
    handlers._service = service
    cases = (
        ("guide", "help", []),
        ("ranking", "rank", []),
        ("model", "model", ["astra", "low"]),
        ("compare", "compare", ["astra", "sol"]),
        ("recommend", "recommend", []),
        ("alerts", "alert", []),
        ("value", "value", []),
        ("trend", "trend", ["astra", "low"]),
        ("channels", "bench", []),
        ("tiers", "combos", []),
        ("visual-value", "value", ["@pompeii-adjacency"]),
    )
    pages = {}
    try:
        for name, command, args in cases:
            reply = await handlers._dispatch(command, args)
            for i, page in enumerate(reply.pages):
                pages[name if i == 0 else f"{name}-{i + 1}"] = page
    finally:
        handlers._service = old
    rows, meta = await service.top_models(by="pass_rate")
    pages["stale"] = ranking_pages(rows, replace(meta, stale=True))[0]
    pages["empty"] = ranking_pages((), meta)[0]
    long_rows = tuple(
        replace(
            rows[0], model="测试超长模型名称-" + "very-long-model-" * 4, effort="ultra"
        )
        for _ in range(17)
    )
    pages["long-names"] = replace(
        ranking_pages(long_rows, meta)[0], subtitle="合成边界示例 · 长模型名与多页榜单"
    )
    # This fixture is explicitly synthetic; do not invent an upstream alert.
    synthetic = DegradationAlert(model="synthetic-model", effort="high", current_iq=0)
    pages["alert-missing"] = replace(
        alert_pages((synthetic,), meta)[0],
        subtitle="合成边界示例 · 预警字段缺失 / 实测值为零",
    )
    pages["single-point"] = replace(
        trend_pages(
            (TrendPoint("2026-09-14T00:00:00Z", 100, 5),),
            "synthetic-model [low]",
            RadarMeta("deep-swe", note="单档位 low"),
        )[0],
        subtitle="合成边界示例 · 单点趋势",
    )
    return pages


async def inspect(page):
    result = await page.evaluate("""() => {
      const root = document.querySelector('.radar-card');
      const bounds = root.getBoundingClientRect();
      const outside = [...root.querySelectorAll('h1,h2,p,td,th,.metric,.identity,code,.data-note,.brand-row')]
        .filter(n => n.scrollWidth > n.clientWidth + 2 ||
          n.getBoundingClientRect().right > bounds.right + 1 ||
          n.getBoundingClientRect().left < bounds.left - 1)
        .map(n => ({tag:n.tagName, class:n.className, text:n.textContent.slice(0,80)}));
      return {width:bounds.width, height:bounds.height, overflow:outside,
        brokenImages:[...document.images].filter(n => !n.complete || !n.naturalWidth).length,
        fontLoaded:document.fonts.check('16px RadarSans')};
    }""")
    assert not result["overflow"], result
    assert result["height"] <= CARD_MAX_HEIGHT, result
    assert result["fontLoaded"] and not result["brokenImages"], result
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/ai-radar")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pages = await preview_pages(fixture_service())
    checks = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for name, view in pages.items():
                file = args.output_dir / f"{name}.html"
                file.write_text(page_html(view, preview=True), encoding="utf-8")
                page = await browser.new_page(
                    viewport={"width": 1080, "height": 900}, device_scale_factor=1
                )
                requests = []
                page.on(
                    "request",
                    lambda request, seen=requests: (
                        seen.append(request.url)
                        if request.url.startswith(("http:", "https:"))
                        else None
                    ),
                )
                await page.route("http**/*", lambda route: route.abort())
                await page.goto(file.resolve().as_uri())
                await page.evaluate("document.fonts.ready")
                desktop = await inspect(page)
                await page.locator(".radar-card").screenshot(
                    path=str(args.output_dir / f"{name}.png")
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                mobile = await inspect(page)
                if name in {"ranking", "guide"}:
                    await page.screenshot(
                        path=str(args.output_dir / f"{name}-mobile.png"), full_page=True
                    )
                assert not requests, requests
                await page.close()
                checks[name] = {
                    "desktop": desktop,
                    "mobile": mobile,
                    "externalRequests": requests,
                }
                print(
                    f"{name}: {desktop['width']}×{desktop['height']} / mobile {mobile['width']}×{mobile['height']}"
                )
        finally:
            await browser.close()
    gallery = "".join(
        f'<article><a href="{name}.html"><img loading="lazy" src="{name}.png" alt="{text(view.title)}"><h2>{text(view.title)} · {text(name)}</h2></a>'
        f'<p><a href="{name}.html">打开 HTML</a> · <a href="{name}.png" download>下载 PNG</a></p></article>'
        for name, view in pages.items()
    )
    (args.output_dir / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>AI 智商雷达 · 设计预览</title><style>body{background:#f4f3ed;color:#172d2a;font-family:system-ui;margin:0;padding:40px}"
        "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px}article{min-width:0;background:#fff;padding:15px;border:1px solid #d4dbd0}"
        "img{width:100%;height:420px;object-fit:cover;object-position:top}a{color:#286456}h2{font-size:18px}p{line-height:1.7}</style>"
        "<h1>AI / RADAR · 模型观察手册</h1><p>离线样本预览，非实时榜单。点击卡片打开可自适应宽度的 HTML；可单独下载渲染图。</p>"
        "<main>" + gallery + "</main></html>",
        encoding="utf-8",
    )
    (args.output_dir / "validation.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Gallery: {args.output_dir / 'index.html'}")


if __name__ == "__main__":
    asyncio.run(main())
