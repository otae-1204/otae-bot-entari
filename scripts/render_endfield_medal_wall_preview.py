"""Render medal wall states using public medal art and synthetic player progress.

No player credentials are read. A saved medal_snapshot.json can be passed with
--snapshot; otherwise the public AKEData tables are fetched without saving a bot snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from otae_bot.infrastructure.rendering.browser import close_browser
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.medals.store import _dict_to_snapshot
from plugins.endfield.providers.akedata import AKEDATA_ICON_BASE
from plugins.endfield.rendering import cards


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/medal-wall-preview")
    args = parser.parse_args()
    # 预览只走 AKEData 公开表与纯函数视图构建，不会用到森空岛 client，所以可以传 None。
    service = EndfieldService(None)
    if args.snapshot:
        saved = json.loads(args.snapshot.read_text(encoding="utf-8"))
        snapshot = _dict_to_snapshot(saved.get("current", saved))
    else:
        snapshot = await service.fetch_medal_snapshot_akedata()
    if snapshot is None or not snapshot.medals:
        raise ValueError("Preview requires a nonempty medal snapshot")

    # Include medals with protruding details so clipping/spacing regressions are visible.
    detailed_ids = (
        "achv_bat_defeat_ruanyi", "achv_bat_defeat_fdcentur", "achv_bat_defeat_agtrinit",
        "achv_growth_wpn_skill_level", "achv_bat_defeat_nefarp",
    )
    by_id = {m.medal_id: m for m in snapshot.medals}
    plated = [by_id[key] for key in detailed_ids if key in by_id and by_id[key].can_be_plated]
    plated = (plated + [m for m in snapshot.medals if m.can_be_plated and m not in plated])[:5]
    ordinary = [m for m in snapshot.medals if not m.can_be_plated][:5]
    displayed = [m for pair in zip(plated, ordinary) for m in pair]
    shown_ids = {m.medal_id for m in displayed}
    missing = [m for m in snapshot.medals if m.medal_id not in shown_ids][:4]
    missing_ids = {m.medal_id for m in missing}
    # 两枚可镀层章保持未镀层、一枚可升级章停在低一档，让「未镀层 / 未升满」双卡也出现在预览里。
    rest = [m for m in snapshot.medals if m.medal_id not in shown_ids | missing_ids]
    unplated_ids = {m.medal_id for m in [m for m in rest if m.can_be_plated][:2]}
    unmaxed_ids = {m.medal_id for m in [
        m for m in rest if m.can_be_upgraded and m.max_level > max(m.init_level, 1)
    ][:1]}

    def achievement_data(m):
        data = {"id": hashlib.md5(m.medal_id.encode()).hexdigest(), "name": m.name, "initLevel": m.init_level}
        if m.can_be_plated:
            # 演示用：以 AKEData 公开镀层原图充当森空岛 platedIcon。
            data["platedIcon"] = f"{AKEDATA_ICON_BASE}/{m.medal_id}_lv{m.max_level:02d}_plating.png"
        return data

    progress = [{
        "achievementData": achievement_data(m),
        "level": m.max_level - max(m.init_level, 1) + (0 if m.medal_id in unmaxed_ids else 1),
        "isPlated": m.can_be_plated and m.medal_id not in unplated_ids,
    } for m in snapshot.medals if m.medal_id not in missing_ids]
    view = service.build_medal_missing_view(
        {"data": {"detail": {"achieve": {
            "achieveMedals": progress,
            "display": {str(i): hashlib.md5(m.medal_id.encode()).hexdigest()
                        for i, m in enumerate(displayed, 1)},
        }}}}, snapshot, nickname="管理员（演示数据）", uid="示例账号", server_name="国服",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    groups = [*view.not_obtained, *view.not_maxed, *view.not_plated]
    urls = [m.icon_url for m in [*groups, *view.wall] if m.icon_url]
    urls += [m.next_icon_url for m in groups if m.next_icon_url]
    icons = await cards._image_data_urls(urls)
    failed = [url for url in urls if not icons.get(url)]
    if failed:
        raise RuntimeError(f"Public preview icons failed: {failed}")

    async def assets(requested):
        return {url: icons.get(url, "") for url in requested}

    samples = (
        ("full", view),
        ("few-empty", replace(view, wall=[m for m in view.wall if m.slot not in (2, 7, 10)])),
        ("middle-empty", replace(view, wall=[m for m in view.wall if m.slot not in (3, 4, 5, 6)])),
        ("tail-empty", replace(view, wall=[m for m in view.wall if m.slot <= 7])),
        ("partial", replace(view, wall=[m for m in view.wall if m.slot in (1, 4, 9)])),
        ("single", replace(view, wall=view.wall[-1:])),
        ("empty", replace(view, wall=[])),
        ("unavailable", replace(view, wall=[
            replace(m, icon_url="", fallback_icon_url="") for m in view.wall[:1]
        ] + view.wall[1:3])),
        ("long-name", replace(view, nickname="长昵称测试・" * 14)),
    )
    original_write = cards._write_temp_html
    try:
        for name, sample in samples:
            def capture(document, name=name):
                (args.output_dir / f"{name}.html").write_text(document, encoding="utf-8")
                return original_write(document)

            with patch.object(cards, "_image_data_urls", assets), patch.object(cards, "_write_temp_html", capture):
                pages = await cards.draw_medal_missing_card(sample)
            for i, png in enumerate(pages, 1):
                path = args.output_dir / f"{name}-{i}.png"
                path.write_bytes(png)
                print(f"{path} ({len(png)} bytes)", flush=True)
    finally:
        await close_browser()


if __name__ == "__main__":
    asyncio.run(main())
