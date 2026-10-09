"""Render exploration through the production renderer, without credentials or a running bot."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import types
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = "endfield_exploration_preview"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules[PACKAGE] = package
service = importlib.import_module(f"{PACKAGE}.account.exploration.service")
draw = importlib.import_module(f"{PACKAGE}.account.exploration.draw")
version = importlib.import_module(f"{PACKAGE}.account.exploration.version")

from otae_bot.infrastructure.rendering.browser import _launch_browser, close_browser
from otae_bot.infrastructure.rendering.executor import close_image_executor


def inspect_layout(paths: list[Path]) -> list[dict]:
    from playwright.sync_api import sync_playwright

    reports = []
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, None)
        try:
            for path in paths:
                page = browser.new_page(
                    viewport={"width": draw.CARD_WIDTH, "height": 900}
                )
                requests = []
                page.on(
                    "request",
                    lambda request, collected=requests: (
                        collected.append(request.url)
                        if request.url.startswith(("http:", "https:"))
                        else None
                    ),
                )
                page.route("http**/*", lambda route: route.abort())
                page.goto(path.resolve().as_uri())
                report = page.evaluate("""() => {
                  const root = document.querySelector('.exploration-card');
                  const bounds = root.getBoundingClientRect();
                  return {
                    width: bounds.width, height: bounds.height,
                    rows: root.querySelectorAll('tbody tr:not(.group)').length,
                    group_rows: root.querySelectorAll('tbody tr.group').length,
                    thumbnails: root.querySelectorAll('.map-grid').length,
                    official_thumbnails: root.querySelectorAll('.map-official').length,
                    collection_icons: root.querySelectorAll('.collection-icon').length,
                    broken_images: [...root.querySelectorAll('img')].filter(n => !n.complete || n.naturalWidth === 0).length,
                    overflow: [...root.querySelectorAll('th,td,.identity,.page-label,.section-head,.summary-title,.stat-cell,.legend,.exploration-footer')]
                      .filter(n => n.scrollWidth > n.clientWidth + 1 ||
                        n.getBoundingClientRect().right > bounds.right + 1)
                      .map(n => n.tagName + ':' + n.textContent.slice(0, 80))
                  };
                }""")
                report.update(file=path.name, external_requests=len(requests))
                page.close()
                assert (
                    report["width"] == draw.CARD_WIDTH and report["height"] <= 12000
                ), report
                assert (
                    not report["overflow"]
                    and not requests
                    and not report["broken_images"]
                ), report
                reports.append(report)
        finally:
            browser.close()
    return reports


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, help="Saved official response or unwrapped detail JSON"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/endfield/exploration/previews"
    )
    parser.add_argument(
        "--stress",
        action="store_true",
        help="Also render partial data and future-region pagination",
    )
    parser.add_argument(
        "--progress",
        choices=("edge", "arc", "chip", "spine", "badge"),
        help="Overall tile progress variant (default: the renderer's setting)",
    )
    args = parser.parse_args()
    if args.progress:
        draw.OVERALL_PROGRESS = args.progress
    source = args.file or ROOT / "tests/fixtures/endfield/exploration/screenshot.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    detail = payload.get("data", payload).get("detail", payload)
    view = service.build_exploration_view(
        detail,
        uid="",
        nickname="预览",
        server_name="国服",
        version=await version.fetch_exploration_version(),
    )
    # A file preview must not reveal an account nickname or impersonate a live query.
    view = replace(
        view,
        nickname="文件预览（非实时查询）" if args.file else "截图示例（非实时查询）",
    )
    cases = {"exploration": view}
    if args.stress:
        m = importlib.import_module(f"{PACKAGE}.account.exploration.models")
        partial = m.ExplorationLevel(
            "partial",
            "未完成与缺失数据示例",
            (
                m.CollectionProgress(8, 26),
                m.CollectionProgress(0, 16),
                m.CollectionProgress(0, 0),
                m.CollectionProgress(None, 3),
                m.CollectionProgress(1, None),
                m.CollectionProgress(9, 8),
            ),
        )
        # Scattered shortfalls on the reference layout, for real-distribution review.
        edits = {
            ("应龙关", 0): m.CollectionProgress(19, 26),
            ("首墩", 1): m.CollectionProgress(11, 26),
            ("景玉谷", 4): m.CollectionProgress(1, 4),
            ("武陵城", 5): m.CollectionProgress(3, 9),
            ("北部禁区", 3): m.CollectionProgress(None, 5),
        }

        def edit_level(level):
            values = list(level.collections)
            for (name, column), value in edits.items():
                if level.name == name:
                    values[column] = value
            return replace(level, collections=tuple(values))

        cases["mixed"] = replace(
            view,
            regions=tuple(
                replace(region, levels=tuple(map(edit_level, region.levels)))
                for region in view.regions
            ),
        )
        cases["partial"] = replace(
            view, regions=(m.ExplorationRegion("sample", "边界样例", (partial,)),)
        )
        levels = tuple(
            replace(partial, level_id=f"future-{i}", name=f"未来地区 {i + 1:02d}")
            for i in range(73)
        )
        cases["pagination"] = replace(
            view, regions=(m.ExplorationRegion("future", "分页压力样例", levels),)
        )
        cases["empty"] = replace(view, regions=())
    args.output.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for name, case in cases.items():
            images = await draw.draw_exploration_cards(case)
            pages = draw.paginate_exploration(case)
            assert len(images) == len(pages), (
                "Preview layout needs the renderer's fallback page budget"
            )
            for index, (png, page) in enumerate(zip(images, pages), 1):
                suffix = f"-{index}" if len(images) > 1 else ""
                target = args.output / f"{name}{suffix}"
                target.with_suffix(".png").write_bytes(png)
                html_path = target.with_suffix(".html")
                html_path.write_text(
                    (
                        await draw.prepare_exploration_html(
                            page,
                            inline=True,
                            page_number=index,
                            page_count=len(pages),
                        )
                    ).html,
                    encoding="utf-8",
                )
                paths.append(html_path)
        reports = await asyncio.to_thread(inspect_layout, paths)
        (args.output / "validation.json").write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    finally:
        await close_browser()
        await close_image_executor()


if __name__ == "__main__":
    asyncio.run(main())
