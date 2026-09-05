from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.endfield.account.detail.draw import (
    draw_account_detail_card,
    render_account_detail_card_html,
)
from plugins.endfield.account.detail.service import build_account_detail_view
from otae_bot.infrastructure.http.client import close_http_client
from otae_bot.infrastructure.rendering.browser import close_browser


RESPONSE_DIR = ROOT / ".runtime" / "skland_reverse" / "responses"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Endfield account detail card.")
    parser.add_argument(
        "--detail",
        type=Path,
        default=RESPONSE_DIR / "redacted_public_current_self_20260720_detail.json",
    )
    parser.add_argument("--uid", default="****1234")
    parser.add_argument("--server-name", default="China")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".runtime" / "endfield_account_detail",
    )
    parser.add_argument("--html", action="store_true", help="Also write a standalone HTML file.")
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def render(args: argparse.Namespace) -> dict[str, str]:
    detail = (load_json(args.detail).get("data") or {}).get("detail") or {}
    view = build_account_detail_view(detail, uid=args.uid, server_name=args.server_name)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"card": args.output_dir / "endfield_account_detail.png"}
    outputs["card"].write_bytes(await draw_account_detail_card(view))
    if args.html:
        page = args.output_dir / "endfield_account_detail.html"
        page.write_text(await render_account_detail_card_html(view), encoding="utf-8")
        outputs["html"] = page
    return {key: str(path.resolve()) for key, path in outputs.items()}


async def async_main() -> None:
    args = parse_args()
    try:
        outputs = await render(args)
        print(json.dumps({"ok": True, "outputs": outputs}, ensure_ascii=False, indent=2))
    finally:
        await close_http_client()
        await close_browser()


if __name__ == "__main__":
    asyncio.run(async_main())
