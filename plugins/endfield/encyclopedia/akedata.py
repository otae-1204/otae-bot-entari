"""在已打开的 `AkeSnapshot` 上取表、翻译。

只有本模块碰 `AkeSnapshot`。可选表（`SystemJumpTable`、`UseItemTable`、
`EnemyTemplateDisplayInfoTable` 之类）缺表或取表失败时进模块内负缓存，
按空表继续，不把整次查询拖成「资料暂时不可用」。
"""

from __future__ import annotations

from typing import Any

from ..providers.repository import AkeDataIncomplete, AkeSnapshot, localize

OBTAIN_WAY_TABLE = "SystemJumpTable"
DISTRIBUTION_TABLE = "DistributionInfoTable"
ABILITY_TABLE = "EnemyAbilityDescTable"
TEXT_TABLE = "I18nTextTable_CN"

# 负缓存：(revision, 表名) → 已知取不到。只留最近两个 revision，
# 与 `index.get_index` 的淘汰口径一致。
_MISSING: set[tuple[str, str]] = set()
_REVISION_ORDER: list[str] = []


def _remember_missing(revision: str, name: str) -> None:
    _MISSING.add((revision, name))
    if revision in _REVISION_ORDER:
        _REVISION_ORDER.remove(revision)
    _REVISION_ORDER.append(revision)
    while len(_REVISION_ORDER) > 2:
        stale = _REVISION_ORDER.pop(0)
        _MISSING.difference_update(key for key in _MISSING if key[0] == stale)


def clear_caches() -> None:
    _MISSING.clear()
    _REVISION_ORDER.clear()


async def optional_table(snapshot: AkeSnapshot, name: str) -> dict:
    """取可选表；缺表 / 空表 / 请求失败一律返回空字典并记负缓存。"""
    key = (snapshot.revision, name)
    if key in _MISSING:
        return {}
    try:
        table = await snapshot.table(name)
    except (AkeDataIncomplete, ValueError):
        _remember_missing(*key)
        return {}
    if not isinstance(table, dict) or not table:
        _remember_missing(*key)
        return {}
    return table


async def items(snapshot: AkeSnapshot) -> dict:
    return await snapshot.table("ItemTable")


async def texts(snapshot: AkeSnapshot) -> dict:
    return await snapshot.table(TEXT_TABLE)


async def use_items(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "UseItemTable")


async def equip_items(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "EquipItemTable")


async def item_type_names(snapshot: AkeSnapshot) -> dict[int, str]:
    rows = await optional_table(snapshot, "ItemTypeTable")
    if not rows:
        return {}
    text = await texts(snapshot)
    names: dict[int, str] = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        type_id = _to_int(row.get("itemType"))
        if type_id is None:
            type_id = _to_int(key)
        if type_id is None:
            continue
        names[type_id] = text_of(row.get("name"), text)
    return names


async def showing_type_names(snapshot: AkeSnapshot) -> dict[int, str]:
    rows = await optional_table(snapshot, "ItemShowingTypeTable")
    if not rows:
        return {}
    text = await texts(snapshot)
    names: dict[int, str] = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        type_id = _to_int(row.get("type"))
        if type_id is None:
            type_id = _to_int(key)
        if type_id is None:
            continue
        names[type_id] = text_of(row.get("name"), text)
    return names


async def enemies(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "EnemyTable")


async def enemy_display_rows(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "EnemyTemplateDisplayInfoTable")


async def enemy_attribute_rows(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "EnemyAttributeTemplateTable")


async def enemy_ability_rows(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, ABILITY_TABLE)


async def hyperlinks(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "HyperlinkTextTable")


async def rich_text_styles(snapshot: AkeSnapshot) -> dict:
    return await optional_table(snapshot, "RichTextStyleTable")


def text_of(value: Any, texts: dict) -> str:
    """把一个 i18n 引用或纯文本收成字符串。"""
    if isinstance(value, dict):
        if "id" in value and "text" in value:
            return str(texts.get(str(value["id"])) or value.get("text") or "")
        return ""
    return "" if value is None else str(value)


def localized_row(value: Any, texts: dict) -> Any:
    return localize(value, texts)


async def translate_field(
    snapshot: AkeSnapshot,
    table_name: str,
    ids: Any,
    field: str,
) -> list[str]:
    """按 id 列表去目标表取一个文本字段并本地化；缺表返回空列表。"""
    if not table_name or not isinstance(ids, (list, tuple)) or not ids:
        return []
    rows = await optional_table(snapshot, table_name)
    if not rows:
        return []
    text = await texts(snapshot)
    result: list[str] = []
    for item_id in ids:
        row = rows.get(str(item_id))
        if not isinstance(row, dict):
            continue
        value = text_of(row.get(field), text)
        if value and value not in result:
            result.append(value)
    return result


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
