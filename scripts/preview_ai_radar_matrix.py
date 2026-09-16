"""Render the user-reference model matrix, keeping a replayable public snapshot.

Use --live once to capture both read-only endpoints; subsequent runs reuse the
saved snapshot. Without a snapshot, recorded test fixtures are used and labelled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from otae_bot.infrastructure.http.client import fetch_json
from plugins.radar.config import RadarConfig
from plugins.radar.matrix import MATRIX_MAX_HEIGHT, MATRIX_WIDTH, iq_color, matrix_pages
from plugins.radar.provider import RadarClient
from plugins.radar.rendering import page_html
from plugins.radar.service import RadarService


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--benchmark", default="deep-swe")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/ai-radar")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    capture = args.output_dir / f"matrix-source-{args.benchmark}.json"
    recorded = (
        json.loads(capture.read_text(encoding="utf-8"))
        if capture.exists() and not args.live
        else {}
    )
    fixture = not args.live and not recorded

    async def request(url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        if args.live:
            payload = await fetch_json(url, **kwargs)
            recorded[endpoint] = payload
            return payload
        if not fixture:
            return recorded[endpoint]
        names = {"intelligence-efficiency": "efficiency", "iq-history": "iq_history"}
        if (
            args.benchmark == "pompeii-adjacency"
            and endpoint == "intelligence-efficiency"
        ):
            names[endpoint] = "efficiency_pompeii"
        return json.loads(
            (ROOT / "tests/fixtures/radar" / f"{names[endpoint]}.json").read_text(
                encoding="utf-8"
            )
        )

    service = RadarService(RadarClient(RadarConfig(), http=request), RadarConfig())
    snapshot = await service.model_matrix(benchmark=args.benchmark)
    if args.live:
        capture.write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
    pages = matrix_pages(snapshot)
    print(
        f"Snapshot: {len(snapshot.models)} models, {sum(len(model.tiers) for model in snapshot.models)} efforts, {len(pages)} pages"
    )
    report = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for index, view in enumerate(pages):
                name = "matrix" if not index else f"matrix-{index + 1}"
                document = page_html(view, preview=True)
                if fixture:
                    document = document.replace(
                        "预览快照", "裁剪夹具预览 · 非完整实时榜单"
                    )
                path = args.output_dir / f"{name}.html"
                path.write_text(document, encoding="utf-8")
                page = await browser.new_page(
                    viewport={"width": MATRIX_WIDTH, "height": 1100}
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
                await page.goto(path.resolve().as_uri())
                await page.evaluate("document.fonts.ready")
                await page.evaluate(
                    "Promise.all([...document.images].map(img => img.decode()))"
                )
                results = {}
                for width in (MATRIX_WIDTH, 1024, 768, 390):
                    await page.set_viewport_size({"width": width, "height": 1100})
                    info = await page.evaluate(r"""() => {
                      const root=document.querySelector('.radar-matrix'), bounds=root.getBoundingClientRect();
                      const nodes=[...root.querySelectorAll('h1,h2,h3,h4,p,.brand-row,.brand,.edition,.page-head,.matrix-headnote,.tier,.tier-name,.tier-performance,.tier-input,.tier-duration,.tier-price,.tier-evidence,.tier-warning,.model-warning,.model-history,.history-label,.family-evidence,.model-panel,.model-heading,.model-title,.tier-count,.panel-data-time,.matrix-header,.matrix-toolbar,.matrix-footer,.region-heading,.vendor-heading')];
                      const cardWidths=[...root.querySelectorAll('.model-panel')].map(card => card.getBoundingClientRect().width);
                      const charts=[...root.querySelectorAll('.history-chart')];
                      const iqColors={}, contrasts=[];
                      function luminance(rgb) {
                        return rgb.match(/[\d.]+/g).slice(0,3).map(value => {
                          const v=Number(value)/255;
                          return v<=0.04045 ? v/12.92 : ((v+0.055)/1.055)**2.4;
                        }).reduce((total,value,index) => total+value*[0.2126,0.7152,0.0722][index],0);
                      }
                      for (const score of root.querySelectorAll('.family-score > strong,.tier-score b')) {
                        const value=score.dataset.iq || 'missing', color=getComputedStyle(score).color;
                        (iqColors[value] ||= new Set()).add(color);
                        if (value !== 'missing') {
                          let background=score;
                          while (getComputedStyle(background).backgroundColor === 'rgba(0, 0, 0, 0)') background=background.parentElement;
                          const a=luminance(color), b=luminance(getComputedStyle(background).backgroundColor);
                          contrasts.push((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05));
                        }
                      }
                      return {width:bounds.width,height:bounds.height,
                        panels:root.querySelectorAll('.model-panel').length,
                        tiers:root.querySelectorAll('.tier').length,
                        icons:root.querySelectorAll('.model-icon').length,
                        iqColors:Object.fromEntries(Object.entries(iqColors).map(([key,colors]) => [key,[...colors]])),
                        minScoreContrast:contrasts.length ? Math.min(...contrasts) : null,
                        chartIssues:charts.flatMap(chart => {
                          const bounds=chart.getBoundingClientRect();
                          const labels=[...chart.querySelectorAll('.spark-x-tick,.spark-y-tick')];
                          return labels.filter(label => {
                            const box=label.getBoundingClientRect();
                            return box.left<bounds.left-1 || box.right>bounds.right+1 || box.bottom>bounds.bottom+1 ||
                              parseFloat(getComputedStyle(label).fontSize)<10 || labels.some(other => {
                                if (other === label) return false;
                                const peer=other.getBoundingClientRect();
                                return box.left<peer.right && box.right>peer.left && box.top<peer.bottom && box.bottom>peer.top;
                              });
                          }).map(label => label.textContent);
                        }),
                        warnings:{models:root.querySelectorAll('.model-warning').length,tiers:root.querySelectorAll('.tier-warning').length},
                        cardWidths:cardWidths.length ? {min:Math.min(...cardWidths),max:Math.max(...cardWidths)} : null,
                        singleModelVendors:[...root.querySelectorAll('.vendor-group')]
                          .filter(vendor => vendor.querySelectorAll('.model-panel').length === 1)
                          .map(vendor => ({vendor:vendor.dataset.vendor,width:vendor.querySelector('.model-panel').getBoundingClientRect().width})),
                        regions:[...root.querySelectorAll('.region-section')].map(region => ({
                          name:region.dataset.region,
                          vendors:[...region.querySelectorAll('.vendor-group')].map(vendor => ({
                            name:vendor.dataset.vendor,
                            models:[...vendor.querySelectorAll('.model-panel')].map(model => model.dataset.model)
                          }))
                        })),
                        brokenImages:[...document.images].filter(img => !img.complete || !img.naturalWidth).map(img => img.alt),
                        overflow:nodes.filter(n => n.scrollWidth>n.clientWidth+1 || n.getBoundingClientRect().right>bounds.right+1).map(n => n.className || n.tagName),
                        fontLoaded:document.fonts.check('16px RadarSans')};
                    }""")
                    assert not info["overflow"] and info["fontLoaded"], (
                        name,
                        width,
                        info,
                    )
                    assert info["height"] <= MATRIX_MAX_HEIGHT, info
                    assert not info["chartIssues"], (width, info["chartIssues"])
                    for value, colors in info["iqColors"].items():
                        expected = iq_color(
                            None if value == "missing" else float(value)
                        )
                        rgb = ", ".join(
                            str(int(expected[i : i + 2], 16)) for i in (1, 3, 5)
                        )
                        assert colors == [f"rgb({rgb})"], (
                            width,
                            value,
                            colors,
                            expected,
                        )
                    assert (
                        info["minScoreContrast"] is None
                        or info["minScoreContrast"] >= 4.5
                    ), info
                    if info["cardWidths"]:
                        assert (
                            info["cardWidths"]["max"] - info["cardWidths"]["min"] < 2
                        ), (width, info["cardWidths"])
                    assert (
                        info["icons"] == len(snapshot.models)
                        and not info["brokenImages"]
                    ), info
                    results[str(width)] = info
                    if width == MATRIX_WIDTH or (width == 390 and index == 0):
                        suffix = "" if width == MATRIX_WIDTH else "-mobile"
                        await page.locator(".radar-matrix").screenshot(
                            path=str(args.output_dir / f"{name}{suffix}.png")
                        )
                        if width == 390 and snapshot.models:
                            await page.locator(".model-panel").first.screenshot(
                                path=str(args.output_dir / "matrix-mobile-detail.png")
                            )
                        if width == MATRIX_WIDTH:
                            detail_height = await page.evaluate("""() => Math.ceil(Math.max(
                                document.querySelector('.vendor-group')?.getBoundingClientRect().bottom || 400
                            ) + 8)""")
                            await page.screenshot(
                                path=str(args.output_dir / "matrix-detail.png"),
                                full_page=True,
                                clip={
                                    "x": 0,
                                    "y": 0,
                                    "width": width,
                                    "height": min(detail_height, info["height"]),
                                },
                            )
                            if snapshot.models:
                                await page.locator(".model-panel").first.screenshot(
                                    path=str(
                                        args.output_dir / "matrix-model-detail.png"
                                    )
                                )
                            score_example = page.locator(
                                '.model-panel[data-model="gpt-5.6-luna"]'
                            )
                            if await score_example.count():
                                await score_example.screenshot(
                                    path=str(
                                        args.output_dir / "matrix-score-detail.png"
                                    )
                                )
                            warning_cards = page.locator(".model-panel").filter(
                                has=page.locator(".tier-warning")
                            )
                            if await warning_cards.count():
                                await warning_cards.first.screenshot(
                                    path=str(
                                        args.output_dir / "matrix-insufficient.png"
                                    )
                                )
                            empty_sample_cards = page.locator(".model-panel").filter(
                                has=page.locator(
                                    ".tier-warning", has_text="暂无有效样本"
                                )
                            )
                            if await empty_sample_cards.count():
                                await empty_sample_cards.first.screenshot(
                                    path=str(args.output_dir / "matrix-no-samples.png")
                                )
                            single_vendor = await page.evaluate("""() => [...document.querySelectorAll('.vendor-group')]
                                .find(vendor => vendor.querySelectorAll('.model-panel').length === 1)?.dataset.vendor""")
                            if single_vendor:
                                await page.locator(
                                    f'.vendor-group[data-vendor="{single_vendor}"] .model-panel'
                                ).screenshot(
                                    path=str(
                                        args.output_dir / "matrix-single-model.png"
                                    )
                                )
                            for region in ("international", "domestic"):
                                section = page.locator(
                                    f'.region-section[data-region="{region}"]'
                                )
                                if await section.count():
                                    await section.screenshot(
                                        path=str(
                                            args.output_dir / f"matrix-{region}.png"
                                        )
                                    )
                assert not requests, requests
                results["externalRequests"] = requests
                report[name] = results
                await page.close()
                print(
                    f"{name}: "
                    + json.dumps(
                        {
                            width: {
                                key: info[key]
                                for key in (
                                    "height",
                                    "panels",
                                    "tiers",
                                    "icons",
                                    "cardWidths",
                                    "overflow",
                                )
                            }
                            for width, info in results.items()
                            if isinstance(info, dict)
                        },
                        ensure_ascii=False,
                    )
                )
        finally:
            await browser.close()
    (args.output_dir / "matrix-validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Preview: {args.output_dir / 'matrix.png'}")


if __name__ == "__main__":
    asyncio.run(main())
