"""敌人索引与敌人卡视图。

搜索单位是展示模板（`EnemyTemplateDisplayInfoTable` 的一行）。`EnemyTable` 里
相同 `templateId` 的行收成 `variants`。抗性按 `attrTemplateId or templateId`
去重，恰好一个且属性表有该行才填五系；否则卡上写「N 种属性模板」。
"""

from __future__ import annotations

from ..providers.assets import sprite_png
from ..providers.repository import AkeDataIncomplete, AkeSnapshot
from . import akedata
from .models import (
    EncyclopediaIndex,
    EnemyAbilityView,
    EnemyResistanceView,
    EnemyView,
    IndexEntry,
)

# 附录 B.4：同目录没有带 displayType 字段的枚举表，故留空，兜底统一用「类型{n}」。
DISPLAY_TYPE_NAMES: dict[int, str] = {}

# 与关卡卡 `_enemy_resistances` 一致；颜色存不带 `#` 的形式，draw 再加。
RESISTANCE_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("Physical", "物理", "physicalResistance", "888888"),
    ("Fire", "灼热", "fireResistance", "FF623D"),
    ("Pulse", "电磁", "pulseResistance", "FFC000"),
    ("Cryst", "寒冷", "crystResistance", "21C6D0"),
    ("Natural", "自然", "naturalResistance", "9EDC23"),
)


def display_type_name(display_type: int | None) -> str:
    if display_type is None:
        return "类型未知"
    return DISPLAY_TYPE_NAMES.get(display_type) or f"类型{display_type}"


async def build_enemy_index(snapshot: AkeSnapshot) -> EncyclopediaIndex:
    display_rows = await akedata.enemy_display_rows(snapshot)
    texts = await akedata.texts(snapshot)
    entries: list[IndexEntry] = []
    for template_id, row in display_rows.items():
        if not isinstance(row, dict):
            continue
        name = akedata.text_of(row.get("name"), texts)
        if not name:
            continue
        display_type = _int(row.get("displayType"))
        entries.append(
            IndexEntry(
                kind="enemy",
                key=str(template_id),
                display_name=name,
                extra_names=_extra_names(row, texts, str(template_id)),
                summary=akedata.text_of(row.get("description"), texts),
                group=str(display_type) if display_type is not None else "",
                icon_url=sprite_png("monstericonbig", str(template_id)),
                subtitle=display_type_name(display_type),
            )
        )
    entries.sort(key=lambda entry: entry.display_name)
    return EncyclopediaIndex(
        kind="enemy",
        revision=snapshot.revision,
        entries=tuple(entries),
        types=_present_types(entries),
    )


async def build_enemy_view(snapshot: AkeSnapshot, template_id: str) -> EnemyView:
    display_rows = await akedata.enemy_display_rows(snapshot)
    row = display_rows.get(str(template_id))
    if not isinstance(row, dict):
        raise AkeDataIncomplete(f"AKE EnemyTemplateDisplayInfoTable is missing {template_id}")
    texts = await akedata.texts(snapshot)
    display_type = _int(row.get("displayType"))
    abilities = await _abilities(snapshot, row.get("abilityDescIds"), texts)
    template_ids, attributes = await _attribute_templates(snapshot, str(template_id))
    resistances = _resistances(attributes)
    distributions = await akedata.translate_field(
        snapshot, akedata.DISTRIBUTION_TABLE, row.get("distributionIds"), "areaName"
    )
    return EnemyView(
        template_id=str(template_id),
        name=akedata.text_of(row.get("name"), texts),
        nickname=akedata.text_of(row.get("nickname"), texts),
        description=akedata.text_of(row.get("description"), texts),
        display_type=display_type or 0,
        display_type_name=display_type_name(display_type),
        icon_url=sprite_png("monstericonbig", str(template_id)),
        abilities=abilities,
        resistances=resistances,
        resistance_template_count=len(template_ids),
        distributions=tuple(distributions),
        distribution_count=len(row.get("distributionIds") or ()),
        variant_count=await _variant_count(snapshot, str(template_id)),
        revision=snapshot.revision,
    )


async def _abilities(
    snapshot: AkeSnapshot, ids: object, texts: dict
) -> tuple[EnemyAbilityView, ...]:
    if not isinstance(ids, (list, tuple)) or not ids:
        return ()
    rows = await akedata.enemy_ability_rows(snapshot)
    if not rows:
        return ()
    result: list[EnemyAbilityView] = []
    for ability_id in ids:
        row = rows.get(str(ability_id))
        if not isinstance(row, dict):
            continue
        description = akedata.text_of(row.get("description"), texts)
        name = akedata.text_of(row.get("name"), texts)
        if not description and not name:
            continue
        # 无名能力也保留说明。
        result.append(EnemyAbilityView(name=name, description=description))
    return tuple(result)


async def _attribute_templates(
    snapshot: AkeSnapshot, template_id: str
) -> tuple[set[str], dict]:
    enemies = await akedata.enemies(snapshot)
    attributes = await akedata.enemy_attribute_rows(snapshot)
    template_ids: set[str] = set()
    for row in enemies.values():
        if not isinstance(row, dict):
            continue
        if str(row.get("templateId") or "") != template_id:
            continue
        attr_id = str(row.get("attrTemplateId") or row.get("templateId") or template_id)
        if attr_id:
            template_ids.add(attr_id)
    if len(template_ids) != 1:
        return template_ids, {}
    attributes_row = attributes.get(next(iter(template_ids)))
    if not isinstance(attributes_row, dict):
        return template_ids, {}
    return template_ids, attributes_row


async def _variant_count(snapshot: AkeSnapshot, template_id: str) -> int:
    enemies = await akedata.enemies(snapshot)
    return sum(
        1
        for row in enemies.values()
        if isinstance(row, dict) and str(row.get("templateId") or "") == template_id
    )


def _resistances(attributes: dict) -> tuple[EnemyResistanceView, ...]:
    if not attributes:
        return ()
    rows: list[EnemyResistanceView] = []
    for element, label, field, color in RESISTANCE_FIELDS:
        resistance = _float(attributes.get(field))
        if resistance is None:
            continue
        rows.append(
            EnemyResistanceView(
                element=element, label=label, percent=100.0 - resistance, color=color
            )
        )
    return tuple(rows)


def _extra_names(row: dict, texts: dict, template_id: str) -> tuple[str, ...]:
    names = [template_id]
    nickname = akedata.text_of(row.get("nickname"), texts)
    if nickname and nickname not in names:
        names.append(nickname)
    return tuple(names)


def _present_types(entries: list[IndexEntry]) -> tuple[tuple[str, str], ...]:
    seen: list[str] = []
    for entry in entries:
        if entry.group and entry.group not in seen:
            seen.append(entry.group)
    seen.sort(key=lambda key: _int(key) if _int(key) is not None else 10**6)
    return tuple((key, display_type_name(_int(key))) for key in seen)


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
