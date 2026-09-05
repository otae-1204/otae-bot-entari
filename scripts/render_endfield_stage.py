from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.endfield.providers.warfarin import WarfarinClient
from plugins.endfield.catalog.commands import score_candidate
from plugins.endfield.stages.draw import draw_stage_card, draw_stage_catalog_cards
from plugins.endfield.stages.service import EndfieldStageService
from otae_bot.infrastructure.http.client import close_http_client
from otae_bot.infrastructure.rendering.browser import close_browser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Endfield stage cards from live FZ Wiki data.")
    parser.add_argument("stage", nargs="*", help="Stage names; empty renders the catalog only.")
    parser.add_argument("--variant", default="", help="Variant selector for the first stage.")
    parser.add_argument("--overview", action="store_true", help="Render the overview card instead.")
    parser.add_argument("--catalog", action="store_true", help="Also render the catalog card.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".runtime" / "endfield_stage",
    )
    return parser.parse_args()


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")


async def render(args: argparse.Namespace) -> dict[str, str]:
    service = EndfieldStageService(WarfarinClient())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    if args.catalog or not args.stage:
        view = await service.get_catalog_view()
        for index, image in enumerate(await draw_stage_catalog_cards(view), start=1):
            path = args.output_dir / f"catalog_{index}.png"
            path.write_bytes(image)
            outputs[f"catalog_{index}"] = path

    for name in args.stage:
        matches = await service.discover_matches(name)
        ranked = sorted(
            matches,
            key=lambda item: score_candidate(item.query_text, item.display_name, item.title),
            reverse=True,
        )
        if not ranked:
            outputs[f"{_slug(name)}_error"] = Path(f"no catalog match for {name}")
            continue
        match = ranked[0]
        mode = "overview" if args.overview else match.mode
        selector = args.variant or match.selector
        view = await service.get_stage_view(
            match.key,
            mode=mode,
            selector=selector,
            source=match.source,
        )
        path = args.output_dir / f"{_slug(match.display_name)}_{mode}{'_' + _slug(selector) if selector else ''}.png"
        path.write_bytes(await draw_stage_card(view))
        outputs[path.stem] = path

    return {key: str(value) for key, value in outputs.items()}


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
