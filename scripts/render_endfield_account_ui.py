from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.endfield.account.draw import (
    draw_account_overview,
    draw_crisis_contract,
    draw_indie_hard,
    render_account_overview_html,
    render_crisis_contract_html,
    render_indie_hard_html,
)
from plugins.endfield.account.models import AccountUiPayload
from otae_bot.infrastructure.http.client import close_http_client
from otae_bot.infrastructure.rendering.browser import close_browser


RESPONSE_DIR = ROOT / ".runtime" / "skland_reverse" / "responses"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Endfield account, Indie Hard and Crisis Contract replica UIs."
    )
    parser.add_argument(
        "--detail",
        type=Path,
        default=RESPONSE_DIR / "redacted_public_self_20260713_detail.json",
    )
    parser.add_argument(
        "--crisis",
        type=Path,
        default=RESPONSE_DIR / "redacted_public_self_20260713_crisis_contract.json",
    )
    parser.add_argument(
        "--indie-hard",
        type=Path,
        default=RESPONSE_DIR / "redacted_public_self_20260713_indie_hard.json",
    )
    parser.add_argument(
        "--crisis-record",
        type=Path,
        default=RESPONSE_DIR / "redacted_public_self_20260713_crisis_record.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".runtime" / "endfield_account_ui_replicated",
    )
    parser.add_argument("--html", action="store_true", help="Also write standalone HTML files.")
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def render(args: argparse.Namespace) -> dict[str, str]:
    payload = AccountUiPayload.from_responses(
        load_json(args.detail),
        load_json(args.crisis),
        load_json(args.indie_hard),
        load_json(args.crisis_record) if args.crisis_record.exists() else None,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "profile": args.output_dir / "endfield_profile.png",
        "indie_hard": args.output_dir / "endfield_indie_hard.png",
        "crisis_contract": args.output_dir / "endfield_crisis_contract.png",
    }
    images = await asyncio.gather(
        draw_account_overview(payload),
        draw_indie_hard(payload),
        draw_crisis_contract(payload),
    )
    for path, content in zip(outputs.values(), images):
        path.write_bytes(content)

    if args.html:
        html_outputs = {
            "profile_html": args.output_dir / "endfield_profile.html",
            "indie_hard_html": args.output_dir / "endfield_indie_hard.html",
            "crisis_contract_html": args.output_dir / "endfield_crisis_contract.html",
        }
        html_pages = await asyncio.gather(
            render_account_overview_html(payload),
            render_indie_hard_html(payload),
            render_crisis_contract_html(payload),
        )
        for path, content in zip(html_outputs.values(), html_pages):
            path.write_text(content, encoding="utf-8")
        outputs.update(html_outputs)
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
