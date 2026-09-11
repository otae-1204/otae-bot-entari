"""Render synthetic daily dashboards offline, without starting the bot or reading accounts.

Usage: .venv/Scripts/python.exe scripts/render_endfield_daily_preview.py
       Add --stress to render 15 accounts; --avatar PATH to preview a local avatar.
Outputs PNG, standalone HTML and browser layout checks under output/daily-dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = "endfield_daily_preview"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules[PACKAGE] = package
models = importlib.import_module(f"{PACKAGE}.catalog.models")
cards = importlib.import_module(f"{PACKAGE}.rendering.cards")

from playwright.async_api import async_playwright

from otae_bot.infrastructure.rendering.browser import close_browser
from otae_bot.infrastructure.rendering.executor import (
    close_image_executor,
)


def sample_accounts(avatar: Path | None) -> list:
    normal = models.DailyAccountView(
        nickname="夜岚", uid="****1024", server_name="国服", account_level=60,
        avatar_url=cards._local_image_data_url(avatar) if avatar else "",
        stamina_current=46, stamina_max=360, stamina_recover_text="37 小时 40 分回满",
        daily_current=100, daily_max=100, weekly_current=10, weekly_max=10,
        bp_level=45, bp_max=60,
    )
    return [
        normal,
        replace(normal, nickname="亚服小号", uid="****8888", server_name="亚服",
                avatar_url="", account_level=32, stamina_current=360, stamina_recover_text="已回满",
                daily_current=40, weekly_current=4, bp_level=60),
        replace(normal, nickname="今天从零开始", uid="****0000", avatar_url="",
                stamina_current=0, stamina_recover_text="45 小时 0 分回满",
                daily_current=0, weekly_current=0, bp_level=0),
        models.DailyAccountView(
            nickname="失效账号 <备用>", uid="****6666", server_name="国服",
            status="failed", message="登录凭据已过期，请重新绑定。<error> & \"detail\"",
        ),
        models.DailyAccountView(
            nickname="这是一个用于检查换行的超长账号昵称ABCDEFGHIJKLMNOPQRSTUVWXYZ管理员",
            uid="----", server_name="国服",
        ),
    ]


async def inspect_layout(browser, path: Path) -> dict:
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    external_requests = []
    page.on("request", lambda request: external_requests.append(request.url)
            if request.url.startswith(("http:", "https:")) else None)
    await page.route("http**/*", lambda route: route.abort())
    try:
        await page.goto(path.resolve().as_uri())
        result = await page.evaluate("""() => {
          const root = document.querySelector('.daily-dashboard-card');
          const bounds = root.getBoundingClientRect();
          const nodes = [...root.querySelectorAll(
            '.daily-head,.daily-id,.daily-id strong,.daily-id span,.daily-error,' +
            '.daily-error>span:last-child,.daily-row-head,.daily-row-label,.daily-row-value,' +
            '.daily-sanity-value,.daily-recover')];
          return {
            width: bounds.width, height: bounds.height,
            panelHeights: [...root.querySelectorAll('.daily-panel')].map(n => n.offsetHeight),
            overflow: nodes.filter(n => n.scrollWidth > n.clientWidth + 1 ||
              n.getBoundingClientRect().right > bounds.right + 1 ||
              n.getBoundingClientRect().left < bounds.left - 1).map(n => n.className),
            brokenImages: [...document.images].filter(n => !n.complete || !n.naturalWidth).length,
            panels: root.querySelectorAll('.daily-panel').length,
            progressWidths: [...root.querySelectorAll('.daily-bar i')].map(n => parseFloat(n.style.width)),
            progressColors: [...new Set([...root.querySelectorAll('.daily-bar i')]
              .map(n => getComputedStyle(n).backgroundColor))],
            sanityBackgrounds: [...new Set([...root.querySelectorAll('.daily-sanity')]
              .map(n => getComputedStyle(n).backgroundColor))],
            recoveryDotColors: [...new Set([...root.querySelectorAll('.daily-recover i')]
              .map(n => getComputedStyle(n).backgroundColor))],
            background: getComputedStyle(root).backgroundColor,
          };
        }""")
        result["externalRequests"] = external_requests
        assert result["width"] == 1280 and result["height"] <= cards.CARD_MAX_HEIGHT, result
        assert not result["overflow"] and not result["brokenImages"], result
        assert not external_requests, external_requests
        assert all(0 <= value <= 100 for value in result["progressWidths"]), result
        for key in ("progressColors", "sanityBackgrounds", "recoveryDotColors"):
            assert len(result[key]) <= 1, f"State-dependent color in {key}: {result[key]}"
        return result
    finally:
        await page.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/daily-dashboard")
    parser.add_argument("--avatar", type=Path)
    parser.add_argument("--stress", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    accounts = sample_accounts(args.avatar)
    cases = {"single": accounts[:1], "multi": accounts, "empty": []}
    cases["boundaries"] = [
        replace(accounts[0], nickname="部分字段缺失", stamina_max=None,
                daily_current=None, weekly_max=None, bp_max=0),
        replace(accounts[0], nickname="超出上限 / 异常负值", stamina_current=420,
                stamina_recover_text="已回满", daily_current=120, weekly_current=-1, bp_level=65),
        replace(accounts[3], message="很长的错误详情 " + "NETWORK_ERROR_" * 32),
    ]
    if args.stress:
        cases["stress-15"] = [replace(accounts[0], nickname=f"管理员 {i:02d}") for i in range(1, 16)]

    reports = {}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for name, rows in cases.items():
                    html_path = args.output_dir / f"{name}.html"
                    write_temp = cards._write_temp_html

                    def capture_html(document, destination=html_path, writer=write_temp):
                        destination.write_text(document, encoding="utf-8")
                        return writer(document)

                    view = models.DailyDashboardView(accounts=rows, generated_at="2026-09-11 11:06")
                    with patch.object(cards, "_write_temp_html", side_effect=capture_html):
                        png = await cards.draw_daily_dashboard_card(view)
                    (args.output_dir / f"{name}.png").write_bytes(png)
                    reports[name] = await inspect_layout(browser, html_path)
                    reports[name]["pngBytes"] = len(png)
                    print(f"{name}: {json.dumps(reports[name], ensure_ascii=False)}")
            finally:
                await browser.close()
    finally:
        await close_browser()
        await close_image_executor()
    (args.output_dir / "validation.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
