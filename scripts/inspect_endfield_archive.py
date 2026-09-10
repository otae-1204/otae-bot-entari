"""Inspect AKEData Prts* archive tables: structure, page/category mapping, version diff.

Read-only against AKEData (no credentials); fetched tables are cached under
``output/endfield-archive-inspect/`` so repeat runs don't re-download.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

BASE = "https://data.akedata.wiki"
OUT = Path("output/endfield-archive-inspect")
TABLES = ("PrtsPage", "PrtsCategory", "PrtsFirstLv", "PrtsAllItem", "PrtsReading")


def version_label(version_id: str) -> str:
    head = str(version_id).split("@", 1)[0]
    parts = head.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else head


def path_label(cfg: str) -> str:
    # ``public/1.5.3/9885010-4/TableCfg`` → ``1.5``
    return version_label(str(cfg).strip("/").split("/")[1])


def pick_previous(manifest: dict) -> dict | None:
    latest = manifest["latest"]
    latest_label = version_label(latest)
    for entry in manifest["versions"]:
        if not isinstance(entry, dict) or not entry.get("id") or entry["id"] == latest:
            continue
        if version_label(str(entry["id"])) != latest_label:
            return entry
    return None


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=True,
        timeout=120,
        headers={
            "User-Agent": "otae-bot-entari/1.0",
            "Referer": "https://cf.akedata.top/",
        },
    ) as client:
        manifest = (await client.get(f"{BASE}/manifest.json")).json()
        latest = manifest["latest"]
        entry = next(v for v in manifest["versions"] if v["id"] == latest)
        prev = pick_previous(manifest)
        print("latest:", latest, entry["tableCfgPath"])
        print("previous:", prev["id"] if prev else None, prev["tableCfgPath"] if prev else "")

        async def cached_table(cfg: str, name: str):
            target = OUT / f"{path_label(cfg)}_{name}.json"
            if not target.exists():
                response = await client.get(f"{BASE}/{cfg}/{name}.json")
                response.raise_for_status()
                response.json()
                target.write_bytes(response.content)
            return json.loads(target.read_bytes())

        async def load(cfg: str, name: str):
            return await cached_table(str(cfg).strip("/"), name)

        page, cat, first_lv, all_item, reading = await asyncio.gather(
            *(load(entry["tableCfgPath"], name) for name in TABLES)
        )
        i18n = await load(entry["tableCfgPath"], "I18nTextTable_CN")
        prev_item = await load(prev["tableCfgPath"], "PrtsAllItem") if prev else {}

        def text(obj) -> str:
            if isinstance(obj, dict) and "id" in obj:
                return str(i18n.get(str(obj["id"])) or obj.get("text") or "")
            return str(obj) if obj else ""

        print("\n== PrtsPage (%d entries) ==" % len(page))
        for key, value in page.items():
            print(json.dumps({**value, "name_cn": text(value.get("name"))}, ensure_ascii=False))

        print("\n== PrtsCategory (%d entries) ==" % len(cat))
        for key, value in cat.items():
            print(json.dumps({**value, "name_cn": text(value.get("name"))}, ensure_ascii=False))

        print("\n== PrtsFirstLv sample (first 2 raw) ==")
        for key, value in list(first_lv.items())[:2]:
            print(key, json.dumps(value, ensure_ascii=False)[:600])

        print("\n== PrtsAllItem sample (first 2 raw) ==")
        for key, value in list(all_item.items())[:2]:
            print(key, json.dumps(value, ensure_ascii=False)[:600])

        # Cross-tab: item type × category, to discover the page→category mapping.
        groups_by_id = {k: v for k, v in first_lv.items() if isinstance(v, dict)}
        cat_by_id = {k: v for k, v in cat.items() if isinstance(v, dict)}
        type_x_cat: dict[tuple[str, str], int] = {}
        orphans = 0
        for item_id, item in all_item.items():
            if not isinstance(item, dict):
                continue
            group = groups_by_id.get(str(item.get("firstLvId") or ""))
            if group is None:
                orphans += 1
                continue
            type_x_cat[(str(item.get("type") or ""), str(group.get("categoryId") or ""))] = (
                type_x_cat.get((str(item.get("type") or ""), str(group.get("categoryId") or "")), 0) + 1
            )
        print("\n== item type × group categoryId ==")
        for (item_type, cat_id), count in sorted(type_x_cat.items()):
            cat_name = text(cat_by_id.get(cat_id, {}).get("name"))
            print(f"  type={item_type:<12} cat={cat_id:<12} {cat_name:<8} items={count}")
        print("  orphan items (no firstLvId match):", orphans)

        # Group-level category → set of item types (does a group mix types?)
        group_types: dict[str, set[str]] = {}
        for item in all_item.values():
            if isinstance(item, dict):
                gid = str(item.get("firstLvId") or "")
                if gid in groups_by_id:
                    group_types.setdefault(gid, set()).add(str(item.get("type") or ""))
        mixed = {g: t for g, t in group_types.items() if len(t) > 1}
        print("  groups with mixed item types:", len(mixed), list(mixed.items())[:5])

        # Reading linkage: does PrtsReading reference PrtsAllItem ids?
        reading_refs: list[str] = []
        for key, value in reading.items():
            if not isinstance(value, dict):
                continue
            for sub in (value.get("list") or {}).values():
                if isinstance(sub, dict) and sub.get("prtsId"):
                    reading_refs.append(str(sub["prtsId"]))
        in_all_item = [r for r in reading_refs if r in all_item]
        print("\n== PrtsReading ==")
        print("  entries:", len(reading), "refs:", len(reading_refs), "in PrtsAllItem:", len(in_all_item))
        print("  sample ref present:", reading_refs[:5], [r in all_item for r in reading_refs[:5]])

        # Version diff: id stability between latest and previous PrtsAllItem.
        cur_ids = {k for k, v in all_item.items() if isinstance(v, dict)}
        prev_ids = {k for k, v in prev_item.items() if isinstance(v, dict)}
        overlap = cur_ids & prev_ids
        print("\n== version diff ==")
        print(f"  latest items: {len(cur_ids)}, previous items: {len(prev_ids)}")
        print(f"  overlap: {len(overlap)} ({len(overlap) / max(len(prev_ids), 1):.1%} of previous)")
        print(f"  new in latest: {len(cur_ids - prev_ids)}")
        for item_id in list(cur_ids - prev_ids)[:10]:
            item = all_item[item_id]
            group = groups_by_id.get(str(item.get("firstLvId") or ""), {})
            print(
                f"    {item_id} type={item.get('type')} cat={group.get('categoryId')} "
                f"name={text(item.get('name'))!r} group={text(group.get('name'))!r}"
            )
        print(f"  removed in latest: {len(prev_ids - cur_ids)}")

        # docNum universe: items grouped by type (=page) counts.
        print("\n== totals by type (=page) ==")
        by_type: dict[str, int] = {}
        for item in all_item.values():
            if isinstance(item, dict):
                by_type[str(item.get("type") or "?")] = by_type.get(str(item.get("type") or "?"), 0) + 1
        print(json.dumps(by_type, ensure_ascii=False))
        page_names = {str(p.get("pageType") or k): text(p.get("name")) for k, p in page.items() if isinstance(p, dict)}
        print("  page localized names:", json.dumps(page_names, ensure_ascii=False))
        group_counts = {t: sum(1 for g, ts in group_types.items() if ts == {t}) for t in by_type}
        print("  groups per single-type:", json.dumps(group_counts, ensure_ascii=False))
        print(f"  total groups: {len(groups_by_id)}, total items: {len(cur_ids)}")


if __name__ == "__main__":
    asyncio.run(main())
