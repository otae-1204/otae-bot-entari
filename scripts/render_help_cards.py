"""Render the /help images from scripts/help_pages.json and assets/image/help/art/gallery.json.

    python scripts/render_help_cards.py                  # render every page to output/help-cards and validate
    python scripts/render_help_cards.py --page endfield  # only some pages (repeatable)
    python scripts/render_help_cards.py --stress         # also check long text for each page
    python scripts/render_help_cards.py --write          # validate, then replace the shipped PNGs
    python scripts/render_help_cards.py --check          # no browser: list shipped images that are stale

Nothing is written to assets/ unless every rendered image passes the layout checks.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from otae_bot.infrastructure.rendering import help_cards  # noqa: E402

STRESS_SECTION = {
    "heading": "超长内容压力测试",
    "access": "仅用于校验",
    "items": [
        {"cmd": "/stress " + "LONG_COMMAND_" * 6, "desc": '超长说明 <必填> & "引号" ' * 4, "badge": "仅私聊"},
        "/stress 普通字符串条目  两个空格分隔命令与说明",
    ],
    "notes": ["备注也会换行：" + "这是一段很长的备注文字，" * 6],
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", action="append", default=[], help="page id to render (default: all)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/help-cards")
    parser.add_argument("--write", action="store_true", help="replace shipped PNGs after validation")
    parser.add_argument("--stress", action="store_true", help="also validate an extra long section per page")
    parser.add_argument("--check", action="store_true", help="only report stale or missing shipped images")
    return parser.parse_args(argv)


def select_pages(pages: list[help_cards.HelpPage], wanted: list[str]) -> list[help_cards.HelpPage]:
    if not wanted:
        return pages
    known = {page.id: page for page in pages}
    unknown = sorted(set(wanted) - known.keys())
    if unknown:
        raise SystemExit(f"unknown page id(s): {unknown}; known: {sorted(known)}")
    return [known[page_id] for page_id in wanted]


def stress_page(page: help_cards.HelpPage) -> help_cards.HelpPage:
    raw = copy.deepcopy(page.raw)
    raw["id"] = f"{page.id}-stress"
    raw["columns"][0].append(STRESS_SECTION)
    return help_cards.parse_page(raw)


def check_only(targets: list[help_cards.RenderTarget]) -> int:
    stale = help_cards.stale_targets(targets)
    orphans = help_cards.orphan_variants(targets)
    for target in stale:
        state = "missing" if not target.path.is_file() else "stale"
        print(f"{state}: {target.path.relative_to(ROOT)} (page {target.page.id}, art {target.art.id})")
    for path in orphans:
        print(f"orphan: {path.relative_to(ROOT)}")
    if not stale and not orphans:
        print(f"all {len(targets)} help images are up to date")
    return 1 if stale or orphans else 0


async def render_all(args: argparse.Namespace, targets: list[help_cards.RenderTarget]) -> dict[str, dict]:
    from playwright.async_api import async_playwright

    theme = help_cards.HelpTheme()
    cases = [(target, target.page, f"{target.page.id}.{target.art.id}") for target in targets]
    if args.stress:
        cases += [(None, stress_page(target.page), f"{target.page.id}.stress")
                  for target in targets if target.primary]
    reports: dict[str, dict] = {}
    failures: list[str] = []
    async with async_playwright() as playwright:
        browser = await help_cards.launch_browser(playwright)
        try:
            for target, page, name in cases:
                art = target.art if target else help_cards.load_gallery().resolve(page)[0]
                png, report = await help_cards.render_target(
                    browser, page, art, args.output_dir / f"{name}.html", theme
                )
                problems = help_cards.layout_problems(report, theme)
                report["problems"] = problems
                if target:
                    png = help_cards.finalize_png(png, target.digest)
                (args.output_dir / f"{name}.png").write_bytes(png)
                reports[name] = report
                print(f"{'FAIL' if problems else 'ok  '} {name}: {report['width']}x{report['height']} "
                      f"columns={report['columnHeights']}" + (f" {problems}" if problems else ""))
                if problems:
                    failures.append(name)
        finally:
            await browser.close()
    (args.output_dir / "validation.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if failures:
        raise SystemExit(f"layout validation failed: {failures}; see {args.output_dir / 'validation.json'}")
    return reports


def write_assets(args: argparse.Namespace, targets: list[help_cards.RenderTarget], every_page: bool) -> None:
    for target in targets:
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_bytes((args.output_dir / f"{target.page.id}.{target.art.id}.png").read_bytes())
        print(f"wrote {target.path.relative_to(ROOT)}")
    if every_page:
        for path in help_cards.orphan_variants(targets):
            path.unlink()
            print(f"removed orphan {path.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pages = help_cards.load_pages()
    gallery = help_cards.load_gallery()
    if args.check:
        return check_only(help_cards.plan_targets(pages, gallery))
    selected = select_pages(pages, args.page)
    targets = help_cards.plan_targets(selected, gallery)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(render_all(args, targets))
    if args.write:
        write_assets(args, targets, every_page=not args.page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
