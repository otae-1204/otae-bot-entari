"""物品索引与物品卡视图。

`/ef 物品` 只读 `ItemTable` / `ItemTypeTable` / `ItemShowingTypeTable` /
`UseItemTable`（判道具）/ `I18nTextTable_CN`；不读 `SkillPatchTable`。
`/ef 物品 <名称>` 才去 `SystemJumpTable` 取获取途径。
"""

from __future__ import annotations

from ..providers.assets import item_icon_urls
from ..providers.repository import AkeDataIncomplete, AkeSnapshot
from . import akedata, classify
from .models import EncyclopediaIndex, IndexEntry, ItemView

SUMMARY_LIMIT = 42


async def build_item_index(snapshot: AkeSnapshot) -> EncyclopediaIndex:
    rows = await akedata.items(snapshot)
    texts = await akedata.texts(snapshot)
    use_ids = set(await akedata.use_items(snapshot))
    type_names = await akedata.item_type_names(snapshot)
    showing_names = await akedata.showing_type_names(snapshot)
    entries: list[IndexEntry] = []
    redirects: set[str] = set()
    for item_id, row in rows.items():
        if not isinstance(row, dict):
            continue
        type_id = _int(row.get("type"))
        name = akedata.text_of(row.get("name"), texts)
        if type_id in classify.EXCLUDED_TYPES:
            # 蚀刻章的规范化名字进旁路集合；武器与装备不进。
            if classify.EXCLUDED_TYPES[type_id] == "medal" and name:
                from ..catalog.aliases import normalize_alias_text

                redirects.add(normalize_alias_text(name))
            continue
        if classify.kind_for_item(type_id, str(item_id), use_ids) != "item":
            continue
        if not name:
            continue
        showing = _int(row.get("showingType"))
        entries.append(
            IndexEntry(
                kind="item",
                key=str(item_id),
                display_name=name,
                extra_names=(str(item_id),),
                summary=_summary(akedata.text_of(row.get("desc"), texts)),
                group=str(type_id) if type_id is not None else "",
                icon_url=_icon(str(item_id), row.get("iconId")),
                rarity=_int(row.get("rarity")) or 0,
                subtitle=showing_names.get(showing, "") if showing else "",
            )
        )
    entries.sort(key=lambda entry: entry.display_name)
    return EncyclopediaIndex(
        kind="item",
        revision=snapshot.revision,
        entries=tuple(entries),
        types=_present_types(entries, type_names),
        redirects=frozenset(redirects),
    )


async def build_item_view(snapshot: AkeSnapshot, item_id: str) -> ItemView:
    rows = await akedata.items(snapshot)
    row = rows.get(str(item_id))
    if not isinstance(row, dict):
        raise AkeDataIncomplete(f"AKE ItemTable is missing {item_id}")
    texts = await akedata.texts(snapshot)
    type_names = await akedata.item_type_names(snapshot)
    type_id = _int(row.get("type"))
    ways = await akedata.translate_field(
        snapshot, akedata.OBTAIN_WAY_TABLE, row.get("obtainWayIds"), "desc"
    )
    return ItemView(
        item_id=str(item_id),
        name=akedata.text_of(row.get("name"), texts),
        type_name=type_names.get(type_id, "") if type_id is not None else "",
        rarity=_int(row.get("rarity")) or 0,
        icon_url=_icon(str(item_id), row.get("iconId")),
        description=akedata.text_of(row.get("desc"), texts),
        deco_description=akedata.text_of(row.get("decoDesc"), texts),
        obtain_ways=tuple(ways),
        no_obtain_hint=akedata.text_of(row.get("noObtainWayHint"), texts),
        revision=snapshot.revision,
    )


def _present_types(
    entries: list[IndexEntry], type_names: dict[int, str]
) -> tuple[tuple[str, str], ...]:
    seen: list[str] = []
    for entry in entries:
        if entry.group and entry.group not in seen:
            seen.append(entry.group)
    seen.sort(key=lambda key: _int(key) if _int(key) is not None else 10**6)
    return tuple((key, type_names.get(_int(key) or -1, f"类型{key}")) for key in seen)


def _icon(item_id: str, icon_id: object) -> str:
    urls = item_icon_urls(item_id, str(icon_id or ""))
    return urls[0] if urls else ""


def _summary(text: str) -> str:
    clean = " ".join(str(text or "").split())
    return clean[:SUMMARY_LIMIT]


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
