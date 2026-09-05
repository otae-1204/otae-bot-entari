"""Render every coded preview with Chromium and check browser-level invariants."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from pathlib import Path

from playwright.async_api import async_playwright
from serve import make_server

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT.parents[1] / "output/endfield-code-preview"


async def capture(output: Path, only: str = "") -> list[dict]:
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    result = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(
                viewport={"width": 1440, "height": 1000}, device_scale_factor=1
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    errors.append(message.text) if message.type == "error" else None
                ),
            )
            await page.goto(base + "/?capture=1#operator")
            await page.locator('#artboard[data-ready="true"]').wait_for()
            pages = await page.evaluate("window.previewPages")
            if len(pages) != 45:
                raise AssertionError(f"Expected 45 review surfaces, got {len(pages)}")
            for entry in pages:
                if only and entry["id"] not in only.split(","):
                    continue
                await page.goto(base + "/?capture=1#" + entry["id"])
                await page.locator('#artboard[data-ready="true"]').wait_for()
                audit = await page.evaluate("""() => {
                  const root=document.querySelector('#artboard');
                  const bounds=root.getBoundingClientRect();
                  return {
                    missing:[...root.querySelectorAll('img')].filter(i=>!i.complete||!i.naturalWidth).map(i=>i.src),
                    overflow:[...root.querySelectorAll('h1,h2,h3,td,th,.metric,.section,.gear-row,.record,.calendar-row')]
                      .filter(e=>{const r=e.getBoundingClientRect();return r.right>bounds.right+1||r.left<bounds.left-1||e.scrollWidth>e.clientWidth+2;})
                      .map(e=>e.tagName+'.'+e.className+': '+e.textContent.slice(0,55)+' [client='+e.clientWidth+', scroll='+e.scrollWidth+']'),
                    width:bounds.width,height:bounds.height,
                    sample:root.textContent.includes('示例数据'),
                    readableText:root.innerText.length,
                  };
                }""")
                await page.locator("#artboard").screenshot(
                    path=str(output / f"{entry['id']}.png")
                )
                entry.update(audit)
                result.append(entry)
                print(
                    f"{entry['id']}: {audit['width']}x{audit['height']} / missing={len(audit['missing'])} overflow={len(audit['overflow'])}",
                    flush=True,
                )

            # Review controls and hash navigation are real; artboard commands are not.
            await page.goto(base + "/#operator")
            await page.locator('#artboard[data-ready="true"]').wait_for()
            await page.locator("#next").click()
            await page.locator(
                '#artboard[data-page-id="operators"][data-ready="true"]'
            ).wait_for()
            await page.locator("#page-search").fill("回响")
            if await page.locator("#page-nav a").count() != 3:
                raise AssertionError("Navigation filtering failed")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(
                path=str(output / "review-mobile.png"), full_page=True
            )
            if await page.evaluate(
                "document.documentElement.scrollWidth > innerWidth + 1"
            ):
                raise AssertionError("Mobile reviewer has horizontal document overflow")
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    report = {
        "pages": result,
        "browser_errors": errors,
        "generated_by": "Playwright / real HTML+CSS, no image generation",
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failing = [
        row["id"]
        for row in result
        if row["missing"] or row["overflow"] or not row["sample"]
    ]
    if failing or errors:
        raise AssertionError(
            f"Preview validation failed: pages={failing}, browser_errors={errors}"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--only", default="", help="Comma-separated page IDs for focused checks"
    )
    args = parser.parse_args()
    asyncio.run(capture(args.output, args.only))
