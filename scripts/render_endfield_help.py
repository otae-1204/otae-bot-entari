"""Build the shipped Endfield help image without starting the bot or using the network.

    python scripts/render_endfield_help.py --stress --write-asset

Edit the endfield entry in scripts/help_pages.json, then run this command.
HTML, PNG and layout diagnostics are written to output/endfield-help by default.
The shipped image is only replaced after every requested layout check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from html import escape
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "scripts/help_pages.json"
ASSET_PATH = ROOT / "assets/image/help/endfield.png"
FONT_DIR = ROOT / "plugins/endfield/assets/fonts"
CARD_WIDTH = 1200


def load_spec() -> dict:
    return next(
        page for page in json.loads(SPEC_PATH.read_text(encoding="utf-8"))["pages"]
        if page["id"] == "endfield"
    )


def render_html(spec: dict) -> str:
    sections = {}
    ordered = [section for group in spec["groups"] for section in spec["sections"]
               if section["group"] == group["id"]]
    if len(ordered) != len(spec["sections"]):
        raise ValueError("Every help section must belong to exactly one group")
    for number, section in enumerate(ordered, start=1):
        rows = []
        for item in section["items"]:
            badge = (
                f'<span class="badge">{escape(item["badge"])}</span>'
                if item.get("badge") else ""
            )
            rows.append(
                '<li class="entry">'
                f'<code data-fit>{escape(item["command"])}</code>'
                f'<p class="description" data-fit>{badge}{escape(item["description"])}</p>'
                '</li>'
            )
        notes = "".join(f'<p data-fit>{escape(note)}</p>' for note in section.get("notes", []))
        sections.setdefault(section["group"], []).append(
            '<section class="panel">'
            '<header class="panel-head">'
            f'<span class="number">{number:02d}</span>'
            f'<h2 data-fit>{escape(section["heading"])}</h2>'
            f'<span class="access" data-fit>{escape(section["access"])}</span>'
            '</header>'
            f'<ul>{"".join(rows)}</ul>'
            + (f'<aside class="notes">{notes}</aside>' if notes else "")
            + '</section>'
        )
    columns = "".join(
        '<div class="column">'
        f'<h2 class="group-title" data-fit>{escape(group["title"])}</h2>'
        + "".join(sections[group["id"]]) + '</div>'
        for group in spec["groups"]
    )
    regular = (FONT_DIR / "HarmonyOS_Sans_SC_Regular.ttf").as_uri()
    bold = (FONT_DIR / "HarmonyOS_Sans_SC_Bold.ttf").as_uri()
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(spec['title'])}</title>
<style>
@font-face {{ font-family:Help; src:url('{regular}'); font-weight:400; }}
@font-face {{ font-family:Help; src:url('{bold}'); font-weight:700; }}
* {{ box-sizing:border-box; }}
html, body {{ margin:0; padding:0; background:#e9e9e4; color:#222620;
  font-family:Arial,Help,'Microsoft YaHei',sans-serif; }}
.help-card {{ width:100%; max-width:{CARD_WIDTH}px; padding:30px;
  background-color:#f1f2ec;
  background-image:linear-gradient(#242b2410 1px, transparent 1px),
    linear-gradient(90deg, #242b2410 1px, transparent 1px);
  background-size:32px 32px; }}
.masthead {{ border-top:8px solid #e9db46; padding:18px 24px;
  background:#252b26; color:#fff; }}
.eyebrow {{ margin:0 0 8px; color:#e7db5d; font:700 14px Help,sans-serif;
  letter-spacing:2px; }}
h1 {{ margin:0; font-size:42px; line-height:1.3; font-weight:700; }}
.subtitle {{ margin:9px 0 0; font-size:22px; line-height:1.5; color:#eceee7; }}
.legend {{ margin:0; padding:14px 4px 16px; font-size:20px; line-height:1.5; color:#525b50; }}
.sections {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; align-items:start; }}
.column {{ display:flex; flex-direction:column; gap:16px; min-width:0; }}
.group-title {{ font-size:23px; line-height:1.4; color:#48533e; padding-left:4px; }}
.panel {{ min-width:0; border:1px solid #cbd0c4; background:#fff; padding:18px 20px; }}
.panel-head {{ display:flex; flex-wrap:wrap; align-items:center; gap:9px;
  border-bottom:2px solid #e6e9df; padding-bottom:11px; margin-bottom:3px; }}
.number {{ flex:none; display:block; font-size:17px; font-weight:700;
  color:#3b452c; background:#f2e982; padding:4px 7px; }}
h2 {{ flex:1; min-width:0; font-size:28px; line-height:1.4; margin:0; }}
.access {{ font-size:17px; line-height:1.5; color:#626c59; max-width:100%; }}
ul {{ list-style:none; margin:0; padding:0; }}
.entry {{ padding:9px 0; border-bottom:1px solid #edf0e8; }}
.entry:last-child {{ border-bottom:0; padding-bottom:0; }}
code {{ display:block; margin:0; font-family:inherit; font-weight:700;
  font-size:23px; line-height:1.45; color:#20291f; white-space:normal; }}
.description {{ margin:3px 0 0; font-size:19px; line-height:1.5; color:#58614f; }}
.badge {{ display:inline; border:1px solid #d6d0a6; background:#faf5d6;
  color:#655324; padding:1px 5px; margin-right:7px; font-size:16px; }}
.notes {{ border-left:3px solid #d8cc52; background:#f4f5ed;
  margin-top:13px; padding:9px 11px; font-size:18px; line-height:1.5; color:#57604e; }}
.notes p {{ margin:0; }}
.notes p + p {{ margin-top:6px; }}
[data-fit], .badge {{ overflow-wrap:anywhere; }}
footer {{ margin-top:22px; padding-top:17px; border-top:2px solid #bdc4b4;
  font-size:20px; line-height:1.6; color:#48533e; }}
footer p {{ margin:0; }}
@media (max-width:760px) {{
  .help-card {{ padding:22px; }}
  .sections {{ grid-template-columns:minmax(0,1fr); }}
  .masthead {{ padding:18px; }}
  h1 {{ font-size:36px; }}
}}
</style></head><body><main class="help-card">
<header class="masthead"><p class="eyebrow" data-fit>ENDFIELD / COMMAND GUIDE</p>
<h1 data-fit>{escape(spec['title'])}</h1>
<p class="subtitle" data-fit>{escape(spec['subtitle'])}</p></header>
<p class="legend" data-fit>{escape(spec['legend'])}</p>
<div class="sections">{columns}</div>
<footer><p data-fit>{escape(spec['footnote'])}</p></footer>
</main></body></html>"""


async def inspect_layout(page) -> dict:
    """Check rendered text bounds, panel overlap and footer placement, not CSS strings."""
    return await page.evaluate("""() => {
      const root = document.querySelector('.help-card');
      const bounds = root.getBoundingClientRect();
      const overflow = [];
      for (const node of root.querySelectorAll('[data-fit]')) {
        const box = node.getBoundingClientRect();
        const owner = node.closest('.panel,.masthead,footer') || root;
        const parent = owner.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(node);
        const escaped = [...range.getClientRects()].some(r =>
          r.left < box.left - 1 || r.right > box.right + 1 ||
          r.top < box.top - 1 || r.bottom > box.bottom + 1);
        if (escaped || node.scrollWidth > node.clientWidth + 1 ||
            box.left < parent.left - 1 || box.right > parent.right + 1 ||
            box.top < parent.top - 1 || box.bottom > parent.bottom + 1 ||
            box.right > bounds.right + 1 || box.bottom > bounds.bottom + 1)
          overflow.push(node.textContent);
      }
      const panels = [...root.querySelectorAll('.panel')].map(n => n.getBoundingClientRect());
      const overlaps = [];
      panels.forEach((a, i) => panels.slice(i + 1).forEach((b, j) => {
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
            Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1)
          overlaps.push([i, i + j + 1]);
      }));
      const footer = root.querySelector('footer').getBoundingClientRect();
      return {width:bounds.width, height:Math.ceil(bounds.height), overflow, overlaps,
        sections:panels.length, commands:root.querySelectorAll('.entry').length,
        footerClear:footer.top >= Math.max(...panels.map(r => r.bottom)),
        fontsLoaded:document.fonts.check('23px Help') && document.fonts.check('700 23px Help')};
    }""")


async def render_case(browser, spec: dict, directory: Path, name: str, width: int) -> dict:
    html_path = directory / f"{name}.html"
    html_path.write_text(render_html(spec), encoding="utf-8")
    page = await browser.new_page(viewport={"width": width, "height": 900}, device_scale_factor=1)
    external_requests = []
    page.on("request", lambda request: external_requests.append(request.url)
            if request.url.startswith(("http:", "https:")) else None)
    await page.route("http**/*", lambda route: route.abort())
    try:
        await page.goto(html_path.resolve().as_uri())
        await page.evaluate("document.fonts.ready")
        report = await inspect_layout(page)
        report["externalRequests"] = external_requests
        if (report["overflow"] or report["overlaps"] or not report["footerClear"]
                or not report["fontsLoaded"] or external_requests):
            raise RuntimeError(f"Help layout validation failed: {report}")
        await page.locator(".help-card").screenshot(path=str(directory / f"{name}.png"))
        report["pngBytes"] = (directory / f"{name}.png").stat().st_size
        return report
    finally:
        await page.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/endfield-help")
    parser.add_argument("--write-asset", action="store_true", help="Replace the shipped help PNG after validation")
    parser.add_argument("--stress", action="store_true", help="Also check long text, extra rows and narrow layout")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    spec = load_spec()
    cases = {"endfield": (spec, CARD_WIDTH)}
    if args.stress:
        stress = deepcopy(spec)
        stress["sections"][0]["items"].extend([
            {"command": "/ef " + "LONG_COMMAND_" * 14,
             "description": '超长说明 <必填> & "引号" ' * 14},
            *deepcopy(spec["sections"][0]["items"]),
        ])
        cases["long-content"] = (stress, CARD_WIDTH)
        cases["narrow"] = (spec, 600)
    reports = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for name, (case, width) in cases.items():
                reports[name] = await render_case(browser, case, args.output_dir, name, width)
                print(f"{name}: {json.dumps(reports[name], ensure_ascii=False)}")
        finally:
            await browser.close()
    (args.output_dir / "validation.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    if args.write_asset:
        ASSET_PATH.write_bytes((args.output_dir / "endfield.png").read_bytes())
        print(f"Updated {ASSET_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
