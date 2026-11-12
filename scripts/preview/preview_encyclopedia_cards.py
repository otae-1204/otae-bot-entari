# -*- coding: utf-8 -*-
"""图鉴卡片本地预览：用夹具快照直接出图，不发任何数据表请求。

用法：
    .venv/Scripts/python.exe scripts/preview/preview_encyclopedia_cards.py
    .venv/Scripts/python.exe scripts/preview/preview_encyclopedia_cards.py --only item,prop
    .venv/Scripts/python.exe scripts/preview/preview_encyclopedia_cards.py --html

产物在 output/encyclopedia_preview/ 下；加 --html 会把渲染用的 HTML 一并落盘，
方便对着浏览器调版式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from plugins.endfield.catalog.models import ArchiveItemView, ArchiveSnapshotView  # noqa: E402
from plugins.endfield.encyclopedia import archives as ency_archives  # noqa: E402
from plugins.endfield.encyclopedia import draw as ency_draw  # noqa: E402
from plugins.endfield.encyclopedia import enemies as ency_enemies  # noqa: E402
from plugins.endfield.encyclopedia import index as ency_index  # noqa: E402
from plugins.endfield.encyclopedia import items as ency_items  # noqa: E402
from plugins.endfield.encyclopedia import props as ency_props  # noqa: E402
from plugins.endfield.encyclopedia import service as ency_service  # noqa: E402
from plugins.endfield.encyclopedia import terms as ency_terms  # noqa: E402
from plugins.endfield.providers import repository  # noqa: E402
from plugins.endfield.providers.repository import AkeSnapshot  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/endfield_akedata_1_5_3.json"
OUT_DIR = ROOT / "output/encyclopedia_preview"

ITEM_KEYS = ("item_plant_1_moss", "item_equip_material_probe")
PROP_KEYS = ("item_tactic_ration", "item_consumable_medkit")
ENEMY_KEYS = ("eny_0001_probe", "eny_0002_shield")
TERM_KEYS = ("ba.burning", "ba.crystonchar")


def _tables() -> dict:
    return pickle.loads(pickle.dumps(json.loads(FIXTURE.read_bytes())))


def _snapshot() -> AkeSnapshot:
    snapshot = AkeSnapshot("1.5.3@9885010-4", "public/1.5.3/9885010-4/TableCfg", "fixture")
    snapshot._tables = _tables()
    return snapshot


def _archive_snapshot() -> ArchiveSnapshotView:
    return ArchiveSnapshotView(
        items=[
            ArchiveItemView(
                item_id="nar_1",
                name="终末地物资",
                page_name="中枢档案",
                category_name="调查报告",
                group_id="grp_1",
                group_name="萨米维格",
                group_sub_name="北境记录",
                icon_url="",
            ),
            ArchiveItemView(
                item_id="nar_2",
                name="协议通行证",
                page_name="中枢档案",
                category_name="通行记录",
                group_id="grp_2",
                group_name="帝江号",
                group_sub_name="",
                icon_url="",
            ),
        ],
        version="1.5",
    )


async def _collect(snapshot: AkeSnapshot) -> list[tuple[str, object]]:
    """按 kind 组出「详情卡 + 目录卡」的预览清单。"""
    jobs: list[tuple[str, object]] = []
    for key in ITEM_KEYS:
        jobs.append((f"item-{key}", await ency_items.build_item_view(snapshot, key)))
    for key in PROP_KEYS:
        jobs.append((f"prop-{key}", await ency_props.build_prop_view(snapshot, key)))
    for key in ENEMY_KEYS:
        jobs.append((f"enemy-{key}", await ency_enemies.build_enemy_view(snapshot, key)))
    for key in TERM_KEYS:
        jobs.append((f"term-{key}", await ency_terms.build_term_view(snapshot, key)))

    archive = _archive_snapshot()
    for entry in ency_archives.build_entries(archive):
        jobs.append((f"archive-{entry.key}", ency_archives.entry_view(archive, entry.key)))

    for kind in ("item", "prop", "enemy", "term"):
        index = await ency_index.get_index(snapshot, kind)
        overview = await ency_service.catalog_view(snapshot, kind, "")
        jobs.append((f"catalog-{kind}-overview", overview))
        if index.types:
            type_key = index.types[0][0]
            filled = await ency_service.catalog_view(snapshot, kind, str(type_key))
            jobs.append((f"catalog-{kind}-{type_key}", filled))
    return jobs


_RENDERERS = {
    "item": ency_draw.draw_item_card,
    "prop": ency_draw.draw_prop_card,
    "enemy": ency_draw.draw_enemy_card,
    "term": ency_draw.draw_term_card,
    "archive": ency_draw.draw_archive_entry_card,
}


async def _render_one(name: str, view, *, want_html: bool, out_dir: Path) -> list[Path]:
    kind = name.split("-", 1)[0]
    if kind == "catalog":
        pages = await ency_draw.draw_catalog_cards(view)
        written: list[Path] = []
        for index, page in enumerate(pages, start=1):
            suffix = f"-{index:02d}" if len(pages) > 1 else ""
            path = out_dir / f"{name}{suffix}.png"
            path.write_bytes(page)
            written.append(path)
        return written
    renderer = _RENDERERS[kind]
    path = out_dir / f"{name}.png"
    path.write_bytes(await renderer(view))
    if want_html:
        render_html = getattr(ency_draw, f"render_{_html_name(kind)}_html")
        (out_dir / f"{name}.html").write_text(render_html(view, {}), encoding="utf-8")
    return [path]


def _html_name(kind: str) -> str:
    return "archive_entry" if kind == "archive" else kind


def _text_of(value, texts: dict) -> str:
    if not isinstance(value, dict):
        return str(value or "")
    return str(texts.get(str(value.get("id"))) or value.get("text") or "")


async def _run_live(args, out_dir: Path) -> None:
    """用线上真实快照渲染，验证实际数据下的版式。"""
    snapshot = await repository.snapshot()
    print(f"live snapshot {snapshot.revision}")

    keys = [part.strip() for part in args.keys.split(",") if part.strip()]
    if not keys:
        table = await snapshot.table("ItemTable")
        texts = await snapshot.table("I18nTextTable_CN")
        rows = [
            (int(entry.get("rarity") or 0), key)
            for key, entry in table.items()
            if _text_of(entry.get("name"), texts)
            and entry.get("iconId")
            and _text_of(entry.get("desc"), texts)
        ]
        rows.sort(key=lambda item: (-item[0], item[1]))
        keys = [key for _, key in rows[:6]]

    for key in keys:
        try:
            view = await ency_items.build_item_view(snapshot, key)
        except Exception as exc:  # noqa: BLE001 - 预览脚本只报告，不中断
            print(f"skip {key} {type(exc).__name__} {exc}")
            continue
        png = await ency_draw.draw_item_card(view)
        path = out_dir / f"live-item-{key}.png"
        path.write_bytes(png)
        print(f"{path.relative_to(ROOT)}  {view.name}  {len(png)} bytes")

    overview = await ency_service.catalog_view(snapshot, "item", "")
    for index, page in enumerate(await ency_draw.draw_catalog_cards(overview), start=1):
        path = out_dir / f"live-catalog-item-{index:02d}.png"
        path.write_bytes(page)
        print(f"{path.relative_to(ROOT)}  catalog  {len(page)} bytes")

    from otae_bot.infrastructure.rendering.browser import close_browser

    await close_browser()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="逗号分隔的 kind：item,prop,enemy,term,archive,catalog")
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--html", action="store_true", help="同时落盘渲染 HTML")
    parser.add_argument("--live", action="store_true", help="改用线上真实快照渲染，不发夹具")
    parser.add_argument("--keys", default="", help="live 模式下逗号分隔的物品 id，留空则自动挑 6 个")
    args = parser.parse_args()

    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.live:
        await _run_live(args, out_dir)
        return

    snapshot = _snapshot()
    with patch.object(repository, "_get", side_effect=AssertionError("预览不应请求数据表")):
        jobs = await _collect(snapshot)

    for name, view in jobs:
        kind = name.split("-", 1)[0]
        if wanted and kind not in wanted:
            continue
        written = await _render_one(name, view, want_html=args.html, out_dir=out_dir)
        for path in written:
            print(f"{path.relative_to(ROOT)}  {path.stat().st_size} bytes")

    from otae_bot.infrastructure.rendering.browser import close_browser

    await close_browser()


if __name__ == "__main__":
    asyncio.run(main())
