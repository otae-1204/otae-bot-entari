"""图鉴分类规则：白名单、道具分桶、词条是否进候选、companion、族名。

白名单与排除类型逐字来自方案附录 B.3，不在第 0 期用猜测 id 先写死。
"""

from __future__ import annotations

from typing import Collection

# 按 type 排除，不按 id 前缀：`item_equip_material_probe` 前缀像装备，type 是 8，
# 仍在白名单里，必须留在物品索引。
EXCLUDED_TYPES: dict[int, str] = {5: "weapon", 6: "equipment", 100: "medal"}

# 附录 B.3：v2 直接点名的四个 type，加上由展示类型反推的八个。
# 沉积具象物有两个 type：72 进白名单，同名的 73 不进（73 的 showingType 全为 0）。
ITEM_TYPE_WHITELIST: dict[int, str] = {
    8: "材料",
    9: "普通设备",
    10: "特种设备",
    34: "培养基核",
    48: "战术物品",
    52: "消耗品",
    54: "功能设备",
    55: "探测器",
    71: "沉积结核",
    72: "沉积具象物",
    101: "随身装置",
    111: "装饰物品",
}

PROP_BUCKETS: dict[int, str] = {48: "战术物品", 52: "消耗品", 55: "探测器"}
PROP_OTHER_BUCKET = "其他"

TERM_FAMILY_NAMES: dict[str, str] = {
    "ba.fire": "灼热",
    "ba.phy": "物理",
    "ba.cryst": "寒冷",
    "ba.natur": "自然",
    "ba.pulse": "电磁",
    "ba.key": "关键词",
    "ba.poise": "失衡",
    "": "其他",
}

TERM_ONCHAR_SUFFIX = "onchar"
TERM_ONCHAR_PREFIX = "干员受到"
# 事实 7 第二段：去后缀后不存在主条目的四个词干，落到 <词干>inflict。
TERM_INFLICT_STEMS = frozenset({"cryst", "pulse", "fire", "natural"})


def kind_for_item(type_id: int | None, item_id: str, use_item_ids: Collection[str]) -> str:
    """在 `UseItemTable` 的是道具；排除类型返回空；白名单返回物品；其余空。"""
    if item_id in use_item_ids:
        return "prop"
    if type_id in EXCLUDED_TYPES:
        return ""
    if type_id in ITEM_TYPE_WHITELIST:
        return "item"
    return ""


def prop_bucket(type_id: int | None) -> str:
    return PROP_BUCKETS.get(type_id, PROP_OTHER_BUCKET)


def term_listed(name: str) -> bool:
    """以「干员受到」开头的从条目默认不进候选。"""
    return not str(name or "").strip().startswith(TERM_ONCHAR_PREFIX)


def term_family(rich_text_id: str) -> tuple[str, str]:
    """返回 (richTextId, 族名)。未知的 richTextId 用 id 本身当组名。"""
    key = str(rich_text_id or "")
    return key, TERM_FAMILY_NAMES.get(key, key or TERM_FAMILY_NAMES[""])


def term_companion(
    term_id: str,
    existing_ids: Collection[str] | None = None,
) -> str:
    """把 `onchar` 从条目挂到主条目；两段规则见方案 §6.3。

    `existing_ids` 为 `None` 时只走第二段（四个已知词干），因为第一段必须确认主条目存在。
    """
    text = str(term_id or "")
    if not text.endswith(TERM_ONCHAR_SUFFIX):
        return ""
    stem = text[: -len(TERM_ONCHAR_SUFFIX)]
    if existing_ids is not None and stem in existing_ids:
        return stem
    name = stem.rpartition(".")[2]
    if name not in TERM_INFLICT_STEMS:
        return ""
    candidate = f"{stem}inflict"
    if existing_ids is None or candidate in existing_ids:
        return candidate
    return ""
