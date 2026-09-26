"""Preview attendance cards with synthetic data, without signing in or reading accounts.

    python scripts/render_endfield_attendance_preview.py --stress

Writes PNG, HTML and browser layout diagnostics to output/attendance-card.
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
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = "endfield_attendance_preview"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules[PACKAGE] = package
models = importlib.import_module(f"{PACKAGE}.catalog.models")
cards = importlib.import_module(f"{PACKAGE}.rendering.cards")

from otae_bot.infrastructure.rendering.browser import close_browser
from otae_bot.infrastructure.rendering.executor import close_image_executor


def sample_roles(icon_dir: Path) -> list:
    # Optional, unmodified game assets; without them exercise the text-only fallback.
    diamond = cards._local_image_data_url(icon_dir / "item_diamond.png")
    gold = cards._local_image_data_url(icon_dir / "item_gold.png")
    success = models.AttendanceRoleView(
        nickname="夜岚", uid="****1024", server_name="国服", status="success",
        message="签到成功", monthly_count=12,
        rewards=[models.AttendanceRewardView("嵌晶玉", 80, diamond),
                 models.AttendanceRewardView("折金票", 2000, gold)],
    )
    return [
        success,
        replace(success, nickname="备用账号", uid="****2048", status="already",
                message="今日已签到", rewards=[], monthly_count=21),
        replace(success, nickname="失效账号", uid="****4096", status="failed",
                message="登录凭据已过期，请重新绑定后重试。", rewards=[], monthly_count=None),
    ]


async def inspect_layout(browser, path: Path) -> dict:
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    external = []
    page.on("request", lambda request: external.append(request.url)
            if request.url.startswith(("http:", "https:")) else None)
    await page.route("http**/*", lambda route: route.abort())
    try:
        await page.goto(path.resolve().as_uri())
        await page.evaluate("document.fonts.ready")
        report = await page.evaluate("""() => {
          const root = document.querySelector('.attendance-card');
          const bounds = root.getBoundingClientRect();
          const overflow = [...root.querySelectorAll('*')].filter(node => {
            const box = node.getBoundingClientRect();
            return box.width > 0 && (box.left < bounds.left - 1 || box.right > bounds.right + 1 ||
              box.bottom > bounds.bottom + 1 || (node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 1));
          }).map(n => n.className);
          const escapedText = [...root.querySelectorAll(
            '.role-main strong,.role-main>span,.status-copy>b,.attendance-reward>span,' +
            '.attendance-meta b,header time')].filter(node => {
            const box = node.getBoundingClientRect();
            const range = document.createRange(); range.selectNodeContents(node);
            return [...range.getClientRects()].some(r => r.left < box.left - 1 || r.right > box.right + 1 ||
              r.top < box.top - 1 || r.bottom > box.bottom + 1);
          }).map(n => n.textContent);
          const panels = [...root.querySelectorAll('.attendance-row')].map(n => n.getBoundingClientRect());
          return {width:bounds.width, height:bounds.height, overflow, escapedText,
            rows:panels.length, overlaps:panels.slice(1).some((r, i) => r.top < panels[i].bottom),
            brokenImages:[...document.images].filter(n => !n.complete || !n.naturalWidth).length};
        }""")
        report["externalRequests"] = external
        return report
    finally:
        await page.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/attendance-card")
    parser.add_argument("--icon-dir", type=Path, default=ROOT / "output/attendance-real-icons",
                        help="Local directory containing original item_diamond.png and item_gold.png")
    parser.add_argument("--stress", action="store_true")
    parser.add_argument("--all-rewards", action="store_true",
                        help="Check the public Skland fixture; requires its original PNGs in --icon-dir")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    roles = sample_roles(args.icon_dir)
    cases = {"single": roles[:1], "multi": roles, "already": roles[1:2], "failed": roles[2:], "empty": []}
    if args.all_rewards:
        client = importlib.import_module(f"{PACKAGE}.account.client")
        fixture = json.loads((ROOT / "tests/fixtures/endfield_attendance_rewards.json").read_text(encoding="utf-8"))
        resources = fixture["resourceInfoMap"]
        rewards = client._attendance_rewards(client._attendance_award_entries(list(resources)), [resources])
        originals = {}
        for reward in rewards:
            path = args.icon_dir / Path(urlsplit(reward.icon_url).path).name
            if not path.is_file():
                raise FileNotFoundError(f"Original Skland reward icon required: {path}")
            originals[reward.icon_url] = cards._local_image_data_url(path)
        views = [models.AttendanceRewardView(r.name, r.count, originals[r.icon_url]) for r in rewards]
        unique = {r.name: r for r in views}
        cases["all-rewards"] = [replace(roles[0], nickname="奖励显示预览", rewards=list(unique.values()))]
        if args.stress:
            cases["all-22-variants"] = [replace(roles[0], nickname="全部奖励配置预览", rewards=views)]
    if args.stress:
        cases["boundaries"] = [
            replace(roles[0], nickname="超长角色昵称" + "LONG_NICKNAME_" * 9, monthly_count=0,
                    server_name="用于测试换行的超长服务器名称" * 4,
                    rewards=[models.AttendanceRewardView("超长奖励名称" + "LONG_REWARD_" * 10, 123456789),
                             *roles[0].rewards] * 2),
            replace(roles[0], nickname='<测试> & "特殊字符"', rewards=[], monthly_count=None),
            replace(roles[2], message='网络错误 <error> & "detail" ' + "NETWORK_ERROR_" * 20),
        ]
        cases["stress-15"] = [replace(roles[i % 3], nickname=f"管理员 {i + 1:02d}") for i in range(15)]
    reports = {}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for name, rows in cases.items():
                    html_path = args.output_dir / f"{name}.html"
                    writer = cards._write_temp_html

                    def capture(document, destination=html_path, writer=writer):
                        destination.write_text(document, encoding="utf-8")
                        return writer(document)

                    icons = {r.icon_url: r.icon_url for role in rows for r in role.rewards
                             if r.icon_url.startswith("data:")}
                    view = models.AttendanceCardView(rows, "2026-09-21 09:30")
                    with (patch.object(cards, "_write_temp_html", side_effect=capture),
                          patch.object(cards, "_image_data_urls", AsyncMock(return_value=icons))):
                        png = await cards.draw_attendance_card(view)
                    (args.output_dir / f"{name}.png").write_bytes(png)
                    report = await inspect_layout(browser, html_path)
                    report["pngBytes"] = len(png)
                    reports[name] = report
                    print(f"{name}: {json.dumps(report, ensure_ascii=False)}")
            finally:
                await browser.close()
    finally:
        await close_browser()
        await close_image_executor()
    (args.output_dir / "validation.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    for name, report in reports.items():
        if (report["overflow"] or report["escapedText"] or report["overlaps"] or report["brokenImages"]
                or report["externalRequests"] or report["height"] > cards.CARD_MAX_HEIGHT):
            raise RuntimeError(f"Attendance layout failed: {name}: {report}")


if __name__ == "__main__":
    asyncio.run(main())
