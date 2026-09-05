"""Equipment view construction; no I/O or command registration."""

from __future__ import annotations

import re
from typing import (
    Any,
    Sequence,
)

from ..commands import (
    EquipmentAttributeFilter,
    format_equipment_attribute_filters,
)
from ..models import (
    EquipmentCatalogAttributeView,
    EquipmentCatalogGroupView,
    EquipmentCatalogItemView,
    EquipmentCatalogView,
    EquipmentPieceView,
    EquipmentStatView,
    EquipmentView,
)
from .common import (
    _build_fz_term_styles,
    _clean_fz_rich_text,
    _equipment_stat_is_percent,
    _first_text,
    _first_value,
    _format_equipment_stat,
    _format_fz_template,
    _fz_asset_raw_url,
    _fz_overview_entries,
    _fz_template_attrs,
    _to_int,
    _unwrap_fz_list,
    clean_text,
)
from .constants import (
    EQUIPMENT_ABILITY_ATTRIBUTES,
    EQUIPMENT_ABILITY_WILDCARD,
    INDEPENDENT_EQUIPMENT_GROUP_NAMES,
)


def build_fz_equipment_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> EquipmentView:
    article = raw.get("article") or {}
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    if not hero:
        raise ValueError("FZ equipment article does not match the supported card schema")

    title = _first_text(article, "title")
    name = _first_text(hero, "name", "title") or title.split("/", 1)[-1]
    if not name:
        raise ValueError("FZ equipment article is missing name")

    stats_raw = attrs.get("stats") if isinstance(attrs.get("stats"), dict) else {}
    stats: list[EquipmentStatView] = []
    for row in stats_raw.get("rows") or []:
        if not isinstance(row, dict):
            continue
        label = _first_text(row, "label", "name")
        raw_values = row.get("values") or []
        value = raw_values[0] if isinstance(raw_values, list) and raw_values else row.get("value")
        if not label or value in (None, ""):
            continue
        formatted_values = [
            _format_equipment_stat(item, _equipment_stat_is_percent(row))
            for item in (raw_values[:4] if isinstance(raw_values, list) else [value])
        ]
        while len(formatted_values) < 4:
            formatted_values.append(formatted_values[-1] if formatted_values else "--")
        stats.append(
            EquipmentStatView(
                label=label,
                value=formatted_values[0],
                values=formatted_values,
                icon_key=str(row.get("attrType") or ""),
            )
        )

    suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
    bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
    bonus_levels = _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
    bonus_level = bonus_levels[-1] if bonus_levels and isinstance(bonus_levels[-1], dict) else {}
    bonus_values = _first_value(bonus_level, "values", "blackboard", "params")
    suit_description = _format_fz_template(
        _first_text(bonus, "description", "desc"),
        bonus_values,
    )
    suit_required_count = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
    suit_name = _first_text(suit, "suitName", "name") or _first_text(hero, "suitName")
    group_name = _first_text(suit, "groupName") or _first_text(hero, "groupName")
    has_suit_effect = bool(clean_text(suit_description))
    pieces: list[EquipmentPieceView] = []
    self_equipment_id = str(suit.get("selfEquipId") or "")
    for piece in suit.get("pieces") or []:
        if not isinstance(piece, dict):
            continue
        piece_id = str(piece.get("equipId") or "")
        if piece_id and piece_id == self_equipment_id:
            continue
        piece_name = _first_text(piece, "name", "title")
        if not piece_name:
            continue
        pieces.append(
            EquipmentPieceView(
                name=piece_name.split("/", 1)[-1],
                slot_type=_first_text(piece, "slotType", "partType") or "装备",
                icon_url=_fz_asset_raw_url(_first_text(piece, "iconUrl", "icon")),
                equipment_id=piece_id,
            )
        )
    if not has_suit_effect:
        suit_name = "独立装备"
        group_name = "独立装备套组"
        suit_required_count = 0
        pieces = []

    materials = attrs.get("materials") if isinstance(attrs.get("materials"), dict) else {}
    return EquipmentView(
        name=name,
        title=title or f"装备/{name}",
        equipment_id=self_equipment_id,
        rarity=_to_int(_first_value(hero, "rarity", "star", "stars")),
        max_level=_to_int(_first_value(hero, "level", "maxLevel", "maxLv")),
        part_type=_first_text(hero, "partType"),
        slot_type=_first_text(hero, "slotType", "type") or "装备",
        suit_name=suit_name,
        group_name=group_name,
        description=_clean_fz_rich_text(_first_text(hero, "description", "desc")),
        flavor=_clean_fz_rich_text(_first_text(hero, "flavor", "quote")),
        icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "icon")),
        stats=stats,
        suit_required_count=suit_required_count,
        suit_description=suit_description,
        suit_pieces=pieces,
        acquisition=_equipment_acquisition(materials),
        term_styles=_build_fz_term_styles(richtext or {}),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_fz_equipment_catalog_view(
    raw: dict[str, Any],
    group_name: str = "",
    rarity_filter: str = "gold",
) -> EquipmentCatalogView:
    article = raw.get("article") or {}
    entries = _fz_equipment_roster_entries(raw)
    if not entries:
        raise ValueError("FZ equipment roster does not match the supported catalog schema")

    normalized_group_name = _normalize_equipment_group_name(group_name)
    grouped: dict[str, list[EquipmentCatalogItemView]] = {}
    rarity_value = {"gold": 5, "purple": 4, "blue": 3}.get(rarity_filter)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if rarity_value is not None and _to_int(entry.get("rarity")) != rarity_value:
            continue
        name = _first_text(entry, "name", "title")
        title = _first_text(entry, "title") or (f"装备/{name}" if name else "")
        current_group = _normalize_equipment_group_name(_first_text(entry, "group")) or "独立装备套组"
        if not name or not title:
            continue
        attributes = _fz_equipment_roster_attributes(entry)
        grouped.setdefault(current_group, []).append(
            EquipmentCatalogItemView(
                name=name,
                title=title,
                group_name=current_group,
                equipment_id=str(entry.get("equipId") or ""),
                level=_to_int(entry.get("level")),
                rarity=_to_int(entry.get("rarity")),
                slot_type=_first_text(entry, "slotType", "partType") or "装备",
                icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
                attributes=attributes,
            )
        )

    slot_order = {"护甲": 0, "护手": 1, "配件": 2}
    groups: list[EquipmentCatalogGroupView] = []
    for current_group, items in grouped.items():
        if normalized_group_name and current_group != normalized_group_name:
            continue
        items.sort(key=lambda item: (slot_order.get(item.slot_type, 9), item.name))
        groups.append(EquipmentCatalogGroupView(current_group, items))
    if normalized_group_name and not groups:
        raise ValueError(f"FZ equipment group not found: {group_name}")

    total_count = sum(len(group.items) for group in groups)
    return EquipmentCatalogView(
        title=normalized_group_name or "全部装备套组",
        groups=groups,
        total_count=total_count,
        rarity_filter=rarity_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def _fz_equipment_roster_attributes(entry: dict[str, Any]) -> list[EquipmentCatalogAttributeView]:
    attributes = [
        EquipmentCatalogAttributeView(
            label=_first_text(attribute, "label", "name"),
            value=clean_text(_first_value(attribute, "value", "text")),
        )
        for attribute in (entry.get("attrList") or [])
        if isinstance(attribute, dict) and _first_text(attribute, "label", "name")
    ]
    if attributes:
        return attributes
    return [
        EquipmentCatalogAttributeView(label=clean_text(label))
        for label in (entry.get("attrKeys") or [])
        if clean_text(label)
    ]


def build_fz_equipment_attribute_catalog_view(
    raw: dict[str, Any],
    detail_raws: dict[str, dict[str, Any]],
    filters: Sequence[EquipmentAttributeFilter],
    rarity_filter: str = "gold",
) -> EquipmentCatalogView:
    if not filters:
        raise ValueError("FZ equipment attribute filter is empty")

    view = build_fz_equipment_catalog_view(raw, "", rarity_filter)
    groups: list[EquipmentCatalogGroupView] = []
    for group in view.groups:
        items: list[EquipmentCatalogItemView] = []
        for item in group.items:
            detail = detail_raws.get(item.title)
            if not isinstance(detail, dict):
                continue
            _apply_fz_equipment_catalog_item_detail(item, detail)
            main, sub = item.main_attribute, item.sub_attribute
            if not _equipment_attributes_match(main, sub, filters):
                continue
            items.append(item)
        if items:
            groups.append(EquipmentCatalogGroupView(group.name, items))

    # 主/副能力套组对任何属性组合都成立，排在具体属性匹配之后展示。
    groups.sort(key=lambda group: all(_equipment_group_item_is_wildcard(item) for item in group.items))
    attribute_filter = format_equipment_attribute_filters(filters)
    if not groups:
        raise ValueError(f"FZ equipment attribute filter not found: {attribute_filter}")
    view.title = attribute_filter
    view.attribute_filter = attribute_filter
    view.groups = groups
    view.total_count = sum(len(group.items) for group in groups)
    return view


def _apply_fz_equipment_catalog_item_details(
    view: EquipmentCatalogView,
    detail_raws: dict[str, dict[str, Any]],
) -> None:
    for group in view.groups:
        for item in group.items:
            detail = detail_raws.get(item.title)
            if isinstance(detail, dict):
                _apply_fz_equipment_catalog_item_detail(item, detail)


def _apply_fz_equipment_catalog_item_detail(
    item: EquipmentCatalogItemView,
    detail: dict[str, Any],
) -> None:
    main, sub, extras = _fz_equipment_attribute_slots(detail)
    item.level = item.level or _fz_equipment_detail_level(detail)
    item.main_attribute = main
    item.sub_attribute = sub
    item.attributes = [
        attribute
        for attribute in (
            EquipmentCatalogAttributeView(label=main, role="main") if main else None,
            EquipmentCatalogAttributeView(label=sub, role="sub") if sub else None,
            *extras,
        )
        if attribute is not None
    ]


def _fz_equipment_detail_level(raw: dict[str, Any]) -> int:
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    return _to_int(_first_value(hero, "level", "maxLevel", "maxLv"))


def _equipment_group_item_is_wildcard(item: EquipmentCatalogItemView) -> bool:
    return EQUIPMENT_ABILITY_WILDCARD in {item.main_attribute, item.sub_attribute}


def _fz_equipment_attribute_slots(
    raw: dict[str, Any],
) -> tuple[str, str, list[EquipmentCatalogAttributeView]]:
    """Split a FZ equipment card into its 主属性、副属性 and remaining 词条.

    FZ lists the ability rows right after the base 防御力 row, larger value
    first, so the first ability row is the 主属性 and the second the 副属性.
    The 集成实训 style rows carry no concrete attribute (they scale with the
    wearer's own 主/副能力) and become wildcards that match every filter.
    """
    attrs = _fz_template_attrs(raw)
    stats = attrs.get("stats") if isinstance(attrs.get("stats"), dict) else {}
    rows = stats.get("rows") or []
    slots: list[str] = []
    extras: list[EquipmentCatalogAttributeView] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("isBase"):
            continue
        label = _first_text(row, "label", "name")
        attribute = _fz_equipment_ability_attribute(row)
        if attribute and len(slots) < 2 and not extras:
            slots.append(attribute)
            continue
        if label:
            extras.append(
                EquipmentCatalogAttributeView(
                    label=label,
                    value=_format_equipment_stat(
                        (row.get("values") or [row.get("value")])[-1],
                        _equipment_stat_is_percent(row),
                    ),
                )
            )
    main = slots[0] if slots else ""
    sub = slots[1] if len(slots) > 1 else ""
    return main, sub, extras


def _fz_equipment_ability_attribute(row: dict[str, Any]) -> str:
    attribute = EQUIPMENT_ABILITY_ATTRIBUTES.get(str(row.get("attrType") or ""), "")
    if attribute:
        return attribute
    composite = str(row.get("compositeAttr") or "")
    if composite in {"Main", "Sub"} and str(row.get("modifierType") or "") == "BaseAddition":
        return EQUIPMENT_ABILITY_WILDCARD
    return ""


def _equipment_attributes_match(
    main: str,
    sub: str,
    filters: Sequence[EquipmentAttributeFilter],
) -> bool:
    for item in filters:
        if item.role == "main":
            matched = _equipment_attribute_slot_matches(main, item.attribute)
        elif item.role == "sub":
            matched = _equipment_attribute_slot_matches(sub, item.attribute)
        else:
            matched = _equipment_attribute_slot_matches(
                main, item.attribute
            ) or _equipment_attribute_slot_matches(sub, item.attribute)
        if not matched:
            return False
    return True


def _equipment_attribute_slot_matches(slot: str, attribute: str) -> bool:
    return bool(slot) and slot in {attribute, EQUIPMENT_ABILITY_WILDCARD}


def _apply_fz_equipment_catalog_suit_effects(
    view: EquipmentCatalogView,
    detail_raws: list[dict[str, Any]],
) -> None:
    groups = {group.name: group for group in view.groups}
    for raw in detail_raws:
        attrs = _fz_template_attrs(raw)
        suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
        bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
        group_name = _normalize_equipment_group_name(_first_text(suit, "groupName"))
        group = groups.get(group_name)
        if group is None or not bonus:
            continue
        levels = [
            level
            for level in _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
            if isinstance(level, dict)
        ]
        selected = levels[-1] if levels else {}
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        description = _format_fz_template(
            _first_text(bonus, "description", "desc"),
            values,
        )
        description = re.sub(r"^\s*\d+\s*件套组效果\s*[：:]\s*", "", description)
        group.suit_name = _first_text(suit, "suitName") or _first_text(bonus, "name")
        group.suit_required_count = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        group.suit_effect_description = description


def _normalize_equipment_group_name(name: str) -> str:
    name = clean_text(name)
    if name in INDEPENDENT_EQUIPMENT_GROUP_NAMES or "独立装备组" in name or "独立装备套组" in name:
        return "独立装备套组"
    return name


def _fz_equipment_roster_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _fz_overview_entries(raw)


def _equipment_acquisition(materials: dict[str, Any]) -> str:
    unlock_type = str(materials.get("unlockType") or "").strip()
    return {
        "EquipFormulaChest": "装备制造",
        "DomainShop": "地区商店",
    }.get(unlock_type, clean_text(unlock_type) or "未知方式")
