"""Render archive cards from locally cached AKEData tables, without player credentials.

Run inspect_endfield_archive.py first if the version tables are not cached.
Only public icon assets may be fetched. Never updates the bot's snapshot store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from otae_bot.infrastructure.rendering.browser import close_browser
from plugins.endfield.catalog.models import ArchiveBaselineView
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.catalog.views.archives import (
    build_akedata_archive_snapshot,
)
from plugins.endfield.rendering.cards import (
    draw_archive_progress_card,
    draw_archive_stats_card,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", default="1.5")
    parser.add_argument("--previous", default="1.4")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "output/endfield-archive-inspect")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/endfield-archive-inspect")
    args = parser.parse_args()

    def load(version: str, table: str) -> dict:
        return json.loads((args.cache_dir / f"{version}_{table}.json").read_text(encoding="utf-8"))

    tables = [load(args.current, name) for name in (
        "PrtsPage", "PrtsCategory", "PrtsFirstLv", "PrtsAllItem", "I18nTextTable_CN",
    )]
    snapshot = build_akedata_archive_snapshot(*tables, version_label=args.current)
    previous = load(args.previous, "PrtsAllItem")
    baseline = ArchiveBaselineView(
        version=args.previous,
        ids=[str(entry.get("id") or key) for key, entry in previous.items() if isinstance(entry, dict)],
    )
    service = EndfieldService(None)
    diff = service.build_archive_diff(snapshot, baseline)
    print(f"Version {args.current}: total={snapshot.total_count}, new={len(diff.new_items)}", flush=True)
    # 个人侧仅为排版演示，避免被误认为真实账号查询结果。
    progress = service.build_archive_progress_view(
        {"data": {"detail": {"base": {"docNum": 472}}}}, snapshot,
        nickname="管理员（演示数据）", uid="示例账号", server_name="国服",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, view, render in (
            ("archive_stats_optimized", diff, draw_archive_stats_card),
            ("archive_progress_optimized", progress, draw_archive_progress_card),
        ):
            for index, png in enumerate(await render(view), 1):
                path = args.output_dir / f"{name}_{index}.png"
                path.write_bytes(png)
                print(f"{path} ({len(png)} bytes)", flush=True)
    finally:
        await close_browser()


if __name__ == "__main__":
    asyncio.run(main())
