"""Loadout view construction; no I/O or command registration."""

from __future__ import annotations

import math
import re
from typing import (
    Any,
)

from ..models import (
    EquipmentStatView,
    LoadoutEffectView,
    LoadoutEquipmentView,
    LoadoutPanelStatView,
    LoadoutStatusEffectView,
    LoadoutStatusLevelView,
    LoadoutView,
)
from .common import (
    _alias_key,
    _build_fz_term_styles,
    _case_insensitive_get,
    _clean_fz_rich_text,
    _equipment_stat_is_percent,
    _first_text,
    _first_value,
    _format_equipment_stat,
    _format_fz_template,
    _format_plain_number,
    _fz_asset_raw_url,
    _fz_hero_meta_value,
    _fz_template_attrs,
    _fz_weapon_id,
    _ordered_fz_levels,
    _to_float,
    _to_int,
    _unwrap_fz_list,
)
from .constants import (
    LOADOUT_ATTRIBUTE_NAMES,
    LOADOUT_EFFECT_KEY_TARGETS,
    LOADOUT_GROWTH_ATTRIBUTE_KEYS,
    LOADOUT_PERCENT_ATTRIBUTES,
    LOADOUT_STATUS_DURATION_KEYS,
    LOADOUT_STATUS_LEVELS,
    LOADOUT_STATUS_REACTIONS,
    LOADOUT_STATUS_TAGS,
)


def build_fz_loadout_view(
    operator_raw: dict[str, Any],
    weapon_raw: dict[str, Any],
    equipment_raws: list[tuple[dict[str, Any], int, tuple[tuple[int, int], ...]]],
    *,
    operator_level: int = 90,
    operator_potential: int = 5,
    weapon_level: int = 90,
    weapon_potential: int = 5,
    weapon_skill_levels: tuple[tuple[int, int], ...] = (),
    richtext: dict[str, Any] | None = None,
    operator_growth: dict[str, Any] | None = None,
) -> LoadoutView:
    operator_attrs = _fz_template_attrs(operator_raw)
    weapon_attrs = _fz_template_attrs(weapon_raw)
    operator_hero = operator_attrs.get("hero") if isinstance(operator_attrs.get("hero"), dict) else {}
    weapon_hero = weapon_attrs.get("hero") if isinstance(weapon_attrs.get("hero"), dict) else {}
    if not operator_hero or not weapon_hero:
        raise ValueError("FZ loadout data is missing operator or weapon fields")

    operator_level = max(1, min(90, int(operator_level)))
    operator_potential = max(0, min(5, int(operator_potential)))
    weapon_level = max(1, min(90, int(weapon_level)))
    weapon_potential = max(0, min(5, int(weapon_potential)))
    operator_weapon_type = _first_text(operator_hero, "weaponType", "weapon")
    weapon_type = _first_text(weapon_hero, "weaponType", "weapon")
    if operator_weapon_type and weapon_type and operator_weapon_type != weapon_type:
        raise ValueError(f"武器类型不匹配：干员使用{operator_weapon_type}，所选武器为{weapon_type}")

    base_stats = _fz_operator_attributes_at_level(operator_attrs.get("attributes"), operator_level)
    additions: dict[str, float] = {}
    base_multipliers: dict[str, float] = {}
    final_additions: dict[str, float] = {}
    multipliers: dict[str, float] = {}
    effects: list[LoadoutEffectView] = []

    main_attribute, sub_attribute = _fz_main_sub_attributes(operator_hero)
    main_key = _loadout_attribute_key(main_attribute)
    sub_key = _loadout_attribute_key(sub_attribute)
    _apply_loadout_operator_growth(
        operator_growth,
        _fz_operator_break_stage_at_level(operator_attrs.get("attributes"), operator_level),
        additions,
        effects,
    )
    equipment_views: list[LoadoutEquipmentView] = []
    suits: dict[str, dict[str, Any]] = {}
    suit_counts: dict[str, int] = {}
    part_counts: dict[str, int] = {}
    for raw, default_enhance, forge_overrides in equipment_raws:
        attrs = _fz_template_attrs(raw)
        hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
        if not hero:
            raise ValueError("FZ equipment article does not match the loadout schema")
        actual_part = _first_text(hero, "partType")
        if actual_part not in {"Body", "Hand", "EDC"}:
            raise ValueError(f"无法识别装备槽位：{_first_text(hero, 'name', 'title')}")
        part_counts[actual_part] = part_counts.get(actual_part, 0) + 1
        maximum = 2 if actual_part == "EDC" else 1
        if part_counts[actual_part] > maximum:
            label = {"Body": "护甲", "Hand": "护手", "EDC": "配件"}[actual_part]
            raise ValueError(f"{label}数量超过槽位上限")
        default_enhance = max(0, min(3, int(default_enhance)))
        stat_rows = (attrs.get("stats") or {}).get("rows") or []
        forge_levels = _loadout_equipment_forge_levels(stat_rows, default_enhance, forge_overrides)
        forge_index = 0
        equipment_stats: list[EquipmentStatView] = []
        for row in stat_rows:
            if isinstance(row, dict):
                if bool(row.get("enhances", True)):
                    enhance = forge_levels[forge_index]
                    forge_index += 1
                else:
                    enhance = default_enhance
                _apply_loadout_equipment_row(
                    row,
                    enhance,
                    main_attribute,
                    sub_attribute,
                    additions,
                    base_multipliers,
                    final_additions,
                    multipliers,
                )
                stat = _build_loadout_equipment_stat(row, enhance, main_attribute, sub_attribute)
                if stat is not None:
                    equipment_stats.append(stat)
        suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
        suit_name = _first_text(suit, "suitName", "name") or _first_text(hero, "suitName")
        required = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        if suit_name and required > 0:
            suit_counts[suit_name] = suit_counts.get(suit_name, 0) + 1
            suits.setdefault(suit_name, suit)
        equipment_views.append(
            LoadoutEquipmentView(
                name=_first_text(hero, "name", "title") or str((raw.get("article") or {}).get("title") or "").split("/", 1)[-1],
                slot_type=_first_text(hero, "slotType", "partType") or "装备",
                enhance_levels=forge_levels,
                icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "icon")),
                equipment_id=str(suit.get("selfEquipId") or _first_value(hero, "id", "equipId") or ""),
                suit_name=suit_name,
                stats=equipment_stats,
            )
        )

    _apply_loadout_weapon_skills(
        weapon_attrs.get("skills"),
        weapon_potential,
        weapon_skill_levels,
        additions,
        final_additions,
        multipliers,
        effects,
        source=_first_text(weapon_hero, "name") or "武器",
    )
    _apply_loadout_operator_effects(
        operator_attrs,
        operator_potential,
        additions,
        final_additions,
        multipliers,
        effects,
    )
    for suit_name, suit in suits.items():
        required = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        if suit_counts.get(suit_name, 0) >= required:
            _apply_loadout_set_effect(
                suit_name,
                suit,
                additions,
                final_additions,
                multipliers,
                effects,
            )

    main_percent = additions.pop("MainPercent", 0.0)
    if main_percent:
        base_multipliers[main_key] = base_multipliers.get(main_key, 0.0) + main_percent
    sub_percent = additions.pop("SubPercent", 0.0)
    if sub_percent:
        base_multipliers[sub_key] = base_multipliers.get(sub_key, 0.0) + sub_percent
    stats = dict(base_stats)
    for key, value in additions.items():
        stats[key] = stats.get(key, 0.0) + value
    for key, value in base_multipliers.items():
        stats[key] = stats.get(key, 0.0) * (1 + value)
    operator_attack = base_stats.get("Atk", 0.0)
    weapon_attack = _fz_weapon_attack_at_level(weapon_attrs.get("stats"), weapon_level)
    attack_percent = additions.get("AtkPercent", 0.0)
    fixed_attack = final_additions.get("Atk", 0.0)
    main_value = math.floor(stats.get(main_key, 0.0))
    sub_value = math.floor(stats.get(sub_key, 0.0))
    ability_bonus = main_value * 0.005 + sub_value * 0.002
    attack = math.floor(((operator_attack + weapon_attack) * (1 + attack_percent) + fixed_attack) * (1 + ability_bonus))
    strength = math.floor(stats.get("Str", 0.0))
    hp = math.floor(
        base_stats.get("MaxHp", 0.0) * (1 + additions.get("MaxHpPercent", 0.0))
        + final_additions.get("MaxHp", 0.0)
        + strength * 5
    )
    defense = math.floor(stats.get("Def", 0.0) + final_additions.get("Def", 0.0))
    physical_resistance = 1 - 1 / (0.001 * math.floor(stats.get("Agi", 0.0)) + 1)
    spell_resistance = 1 - 1 / (0.001 * math.floor(stats.get("Wisd", 0.0)) + 1)
    healing_taken = stats.get("HealTakenIncrease", 0.0) + math.floor(stats.get("Will", 0.0)) * 0.001
    arts_strength = stats.get("PhysicalAndSpellInflictionEnhance", 0.0)
    status_effect_bonus = _loadout_status_effect_bonus(arts_strength)

    primary_stats = [
        LoadoutPanelStatView("Atk", "攻击力", str(attack), f"{int(operator_attack)} + {int(weapon_attack)}，攻击加成 {_format_loadout_percent(attack_percent)}，能力加成 {_format_loadout_percent(ability_bonus)}"),
        LoadoutPanelStatView("MaxHp", "生命值", str(hp), f"基础 {int(base_stats.get('MaxHp', 0))}，力量额外 +{strength * 5}"),
        LoadoutPanelStatView("Def", "防御力", str(defense)),
    ]
    ability_stats = [
        LoadoutPanelStatView(key, LOADOUT_ATTRIBUTE_NAMES[key], str(math.floor(stats.get(key, 0.0))))
        for key in ("Str", "Agi", "Wisd", "Will")
    ]
    advanced_values = dict(stats)
    advanced_values["PhysicalResistance"] = physical_resistance
    for key in ("FireResistance", "PulseResistance", "CrystResistance", "NaturalResistance", "EtherResistance"):
        advanced_values[key] = spell_resistance
    advanced_values["HealTakenIncrease"] = healing_taken
    if "AllDamageTakenScalar" in multipliers:
        advanced_values["AllDamageTakenScalar"] = 1 - multipliers["AllDamageTakenScalar"]
    advanced_stats = _build_loadout_advanced_stats(advanced_values)
    for row in advanced_stats:
        if row.key == "PhysicalAndSpellInflictionEnhance":
            row.detail = f"导电 / 腐蚀 / 碎甲附带效果 +{status_effect_bonus * 100:.1f}%"
    status_effects = _build_loadout_status_effects(operator_attrs, operator_potential, arts_strength)

    versions = [
        str((operator_raw.get("article") or {}).get("updatedAt") or "")[:10],
        str((weapon_raw.get("article") or {}).get("updatedAt") or "")[:10],
        *(str((raw.get("article") or {}).get("updatedAt") or "")[:10] for raw, _, _ in equipment_raws),
    ]
    return LoadoutView(
        operator_name=_first_text(operator_hero, "name", "title"),
        weapon_name=_first_text(weapon_hero, "name", "title"),
        operator_level=operator_level,
        operator_potential=operator_potential,
        weapon_level=weapon_level,
        weapon_potential=weapon_potential,
        main_attribute=main_attribute,
        sub_attribute=sub_attribute,
        weapon_type=weapon_type,
        operator_icon_url=_fz_asset_raw_url(_first_text(operator_hero, "iconUrl", "avatarUrl", "icon")),
        weapon_icon_url=_fz_asset_raw_url(_first_text(weapon_hero, "iconUrl", "icon")),
        operator_id=str(_first_value(operator_hero, "id", "charId", "operatorId") or ""),
        weapon_id=str(
            _first_value(weapon_hero, "id", "weaponId")
            or _fz_weapon_id(_unwrap_fz_list(weapon_attrs.get("skills"), "skills", "items", "list"))
        ),
        equipment=equipment_views,
        primary_stats=primary_stats,
        ability_stats=ability_stats,
        advanced_stats=advanced_stats,
        status_effect_bonus=status_effect_bonus,
        status_effects=status_effects,
        effects=effects,
        source_version=max((version for version in versions if version), default=""),
        term_styles=_build_fz_term_styles(richtext or {}),
    )


def _fz_operator_attributes_at_level(raw: Any, level: int) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("FZ operator attributes are missing")
    breaks = raw.get("breaks") or []
    rows = raw.get("rows") or []
    selected_group = -1
    selected_index = -1
    for group_index, group in enumerate(breaks):
        levels = group.get("levels") if isinstance(group, dict) else None
        if isinstance(levels, list) and level in levels:
            selected_group = group_index
            selected_index = levels.index(level)
    if selected_group < 0:
        raise ValueError(f"FZ operator level not found: {level}")
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get("cells") or []
        if selected_group >= len(cells) or selected_index >= len(cells[selected_group]):
            continue
        value = _to_float(cells[selected_group][selected_index])
        key = str(row.get("key") or row.get("hint") or "")
        if key and value is not None:
            result[key] = value
    return result


def _fz_operator_break_stage_at_level(raw: Any, level: int) -> int:
    if not isinstance(raw, dict):
        return 0
    selected_stage = 0
    for group in raw.get("breaks") or []:
        if not isinstance(group, dict):
            continue
        levels = group.get("levels")
        if isinstance(levels, list) and level in levels:
            selected_stage = _to_int(group.get("breakStage"))
    return selected_stage


def _apply_loadout_operator_growth(
    raw: dict[str, Any] | None,
    break_stage: int,
    additions: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
    growth = data.get("charGrowthTable") if isinstance(data, dict) else None
    if not isinstance(growth, dict):
        return
    totals: dict[str, float] = {}
    for node in (growth.get("talentNodeMap") or {}).values():
        if not isinstance(node, dict):
            continue
        info = node.get("attributeNodeInfo")
        if not isinstance(info, dict) or _to_int(info.get("breakStage")) > break_stage:
            continue
        for modifier in info.get("attributeModifiers") or []:
            if not isinstance(modifier, dict):
                continue
            raw_type = modifier.get("attrType")
            key = LOADOUT_GROWTH_ATTRIBUTE_KEYS.get(_to_int(raw_type), _loadout_attribute_key(str(raw_type or "")))
            value = _to_float(modifier.get("attrValue"))
            if key not in LOADOUT_ATTRIBUTE_NAMES or value is None:
                continue
            totals[key] = totals.get(key, 0.0) + value
    if not totals:
        return
    for key, value in totals.items():
        additions[key] = additions.get(key, 0.0) + value
    description = "，".join(
        f"{LOADOUT_ATTRIBUTE_NAMES[key]}+{_format_plain_number(totals[key])}"
        for key in ("Str", "Agi", "Wisd", "Will")
        if key in totals
    )
    effects.append(LoadoutEffectView("干员 · 能力天赋", description, active=True))


def _fz_main_sub_attributes(hero: dict[str, Any]) -> tuple[str, str]:
    value = _fz_hero_meta_value(hero, "主 / 副属性", "主/副属性", "主副属性")
    parts = [part.strip() for part in re.split(r"[/／]", value) if part.strip()]
    if len(parts) != 2:
        raise ValueError("FZ operator data is missing main/sub attributes")
    return parts[0], parts[1]


def _loadout_attribute_key(label: str) -> str:
    return {"力量": "Str", "敏捷": "Agi", "智识": "Wisd", "意志": "Will"}.get(label, label)


def _loadout_equipment_forge_levels(
    rows: list[Any],
    default_enhance: int,
    overrides: tuple[tuple[int, int], ...],
) -> tuple[int, ...]:
    forgeable_count = sum(isinstance(row, dict) and bool(row.get("enhances", True)) for row in rows)
    override_map = dict(overrides)
    invalid = [index for index, level in override_map.items() if index < 1 or index > forgeable_count or not 0 <= level <= 3]
    if invalid:
        raise ValueError(f"词条编号超出范围：词条{invalid[0]}（该装备共有{forgeable_count}条可锻造词条）")
    return tuple(override_map.get(index, default_enhance) for index in range(1, forgeable_count + 1))


def _apply_loadout_equipment_row(
    row: dict[str, Any],
    enhance: int,
    main_attribute: str,
    sub_attribute: str,
    additions: dict[str, float],
    base_multipliers: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
) -> None:
    values = row.get("values") or []
    raw_value = values[min(enhance, len(values) - 1)] if isinstance(values, list) and values else row.get("value")
    value = _to_float(raw_value)
    if value is None:
        return
    target = str(row.get("compositeAttr") or row.get("attrType") or "")
    if target == "Main":
        target = _loadout_attribute_key(main_attribute)
    elif target == "Sub":
        target = _loadout_attribute_key(sub_attribute)
    if not target or target == "Level":
        return
    modifier = str(row.get("modifierType") or "BaseAddition")
    if modifier == "BaseFinalAddition":
        final_additions[target] = final_additions.get(target, 0.0) + value
    elif modifier == "BaseMultiplier":
        base_multipliers[target] = base_multipliers.get(target, 0.0) + value
    elif modifier == "BaseFinalMultiplier":
        multipliers[target] = multipliers.get(target, 1.0) * value
    elif target == "Atk" and _equipment_stat_is_percent(row):
        additions["AtkPercent"] = additions.get("AtkPercent", 0.0) + value
    elif target == "MaxHp" and _equipment_stat_is_percent(row):
        additions["MaxHpPercent"] = additions.get("MaxHpPercent", 0.0) + value
    else:
        additions[target] = additions.get(target, 0.0) + value


def _build_loadout_equipment_stat(
    row: dict[str, Any],
    enhance: int,
    main_attribute: str,
    sub_attribute: str,
) -> EquipmentStatView | None:
    if str(row.get("attrType") or "") == "Def":
        return None
    values = row.get("values") or []
    raw_value = values[min(enhance, len(values) - 1)] if isinstance(values, list) and values else row.get("value")
    if raw_value in (None, ""):
        return None
    label = _first_text(row, "label", "name")
    composite = str(row.get("compositeAttr") or "")
    if composite == "Main":
        label = f"{label or '主能力'}（{main_attribute}）"
    elif composite == "Sub":
        label = f"{label or '副能力'}（{sub_attribute}）"
    if not label:
        return None
    return EquipmentStatView(
        label=label,
        value=_format_equipment_stat(raw_value, _equipment_stat_is_percent(row)),
        icon_key=str(row.get("attrType") or ""),
    )


def _fz_weapon_attack_at_level(raw: Any, level: int) -> float:
    stats = raw if isinstance(raw, dict) else {}
    curve = stats.get("curve") or []
    exact = next((row for row in curve if isinstance(row, dict) and _to_int(row.get("lv")) == level), None)
    if exact is None:
        raise ValueError(f"FZ weapon level not found: {level}")
    return _to_float(exact.get("atk")) or 0.0


def _apply_loadout_weapon_skills(
    raw: Any,
    potential: int,
    skill_levels: tuple[tuple[int, int], ...],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
    *,
    source: str,
) -> None:
    overrides = dict(skill_levels)
    skills = _unwrap_fz_list(raw, "skills", "items", "list")
    for skill_index, skill in enumerate(skills, 1):
        if not isinstance(skill, dict):
            continue
        maximum = min(9, _to_int(skill.get("zeroPotentialMaxLevel")) + potential)
        levels = _ordered_fz_levels(_unwrap_fz_list(skill.get("levels"), "levels", "items", "list"))
        requested_level = overrides.get(skill_index)
        if requested_level is not None and requested_level > maximum:
            raise ValueError(f"武器技能{skill_index}在当前潜能下最高为等级{maximum}")
        target_level = requested_level if requested_level is not None else maximum
        selected = next((item for item in levels if _to_int(item.get("level")) == target_level), None)
        if selected is None and requested_level is not None:
            available = [
                _to_int(item.get("level"))
                for item in levels
                if 0 < _to_int(item.get("level")) <= maximum
            ]
            choices = "、".join(str(level) for level in available) or "无"
            raise ValueError(f"武器技能{skill_index}不支持等级{requested_level}（可选：{choices}）")
        if selected is None:
            selected = levels[-1] if levels else None
        if not isinstance(selected, dict):
            continue
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        description = _first_text(skill, "description", "desc")
        _apply_loadout_description(
            description,
            values,
            additions,
            final_additions,
            multipliers,
            effects,
            f"{source} · {_first_text(skill, 'name', 'title') or '武器效果'} Lv.{_to_int(selected.get('level'))}",
        )
    unknown_indices = sorted(set(overrides) - set(range(1, len(skills) + 1)))
    if unknown_indices:
        raise ValueError(f"武器技能序号超出范围：武器技能{unknown_indices[0]}（该武器共有{len(skills)}个技能）")


def _apply_loadout_operator_effects(
    attrs: dict[str, Any],
    operator_potential: int,
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    item_groups = (
        ("talents", _unwrap_fz_list(attrs.get("talents"), "talents", "items", "list")),
        ("potentials", _loadout_operator_potentials(attrs.get("potentials"), operator_potential)),
    )
    for field, items in item_groups:
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            if isinstance(item, dict):
                latest[_first_text(item, "name", "title") or str(len(latest))] = item
        for name, item in latest.items():
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            _apply_loadout_description(
                _first_text(item, "description", "desc", "effect"),
                values,
                additions,
                final_additions,
                multipliers,
                effects,
                f"干员 · {name}",
            )


def _apply_loadout_set_effect(
    suit_name: str,
    suit: dict[str, Any],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
    levels = _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
    selected = levels[-1] if levels and isinstance(levels[-1], dict) else {}
    values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
    _apply_loadout_description(
        _first_text(bonus, "description", "desc"),
        values,
        additions,
        final_additions,
        multipliers,
        effects,
        f"{suit_name}套装",
    )


def _apply_loadout_description(
    description: str,
    values: dict[str, Any],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
    source: str,
) -> None:
    if not description or not values:
        return
    for clause in (part.strip() for part in re.split(r"[。；\n]+", description) if part.strip()):
        keys = [str(key) for key in values if re.search(rf"\b{re.escape(str(key))}\b", clause, flags=re.I)]
        if not keys:
            continue
        triggered = _loadout_clause_is_triggered(clause)
        rendered = _format_fz_template(clause, values)
        resolved: list[tuple[str, float]] = []
        for key in keys:
            value = _to_float(_case_insensitive_get(values, key))
            if value is None:
                continue
            target = _loadout_effect_target(key, clause, allow_label_fallback=len(keys) == 1)
            if not target:
                continue
            resolved.append((target, value))
        active = not triggered
        effects.append(LoadoutEffectView(source, rendered, active=active))
        if triggered:
            continue
        for target, value in resolved:
            if target == "AllDamageTakenScalar":
                multipliers[target] = multipliers.get(target, 1.0) * value
            elif target == "AtkFinal":
                final_additions["Atk"] = final_additions.get("Atk", 0.0) + value
            elif target == "MaxHpFinal":
                final_additions["MaxHp"] = final_additions.get("MaxHp", 0.0) + value
            else:
                additions[target] = additions.get(target, 0.0) + value


def _loadout_clause_is_triggered(clause: str) -> bool:
    plain = _clean_fz_rich_text(clause)
    return bool(
        re.search(
            r"(?:当|每|若|如果|期间|时[，,]|后[，,使]|根据|使(?:其他队友|敌人)|装备者施加|装备者造成|"
            r"对.+(?:敌人|目标)|(?:命中|击中|施放|释放|消耗|触发|使用|进入|离开).{0,32}?时(?!间)|"
            r"(?:连携技|终结技|战技|普通攻击).+的|所需)",
            plain,
        )
    )


def _loadout_effect_target(key: str, clause: str, *, allow_label_fallback: bool) -> str:
    lowered = _alias_key(key).lower()
    plain = _clean_fz_rich_text(clause)
    if lowered == "dmg_taken_down":
        return "AllDamageTakenScalar"
    target = LOADOUT_EFFECT_KEY_TARGETS.get(lowered)
    if target:
        # FZ data also uses names such as ``atk_up`` for skill damage ratios.
        # Only treat those ambiguous keys as a panel attack bonus when the
        # rendered description explicitly says 攻击力.
        if target == "AtkPercent" and "攻击力" not in plain:
            return ""
        return target
    if any(token in lowered for token in ("duration", "time", "count", "cost", "stack", "limit", "interval", "cooldown")):
        return ""
    semantic_targets = (
        (("phy", "physical"), "PhysicalDamageIncrease"),
        (("spell",), "SpellDamageIncrease"),
        (("fire",), "FireDamageIncrease"),
        (("pulse",), "PulseDamageIncrease"),
        (("cryst", "cold"), "CrystDamageIncrease"),
        (("natural",), "NaturalDamageIncrease"),
        (("ether",), "EtherDamageIncrease"),
    )
    if any(token in lowered for token in ("dmg", "damage", "up")):
        for tokens, semantic_target in semantic_targets:
            if any(token in lowered for token in tokens):
                return semantic_target
    if re.fullmatch(r"(?:owner_)?(?:atk|attack)(?:_(?:up|increase|bonus|percent|pct))?", lowered):
        return "AtkPercent"
    if not allow_label_fallback:
        return ""
    label_targets = (
        ("暴击伤害", "CriticalDamageIncrease"),
        ("暴击率", "CriticalRate"),
        ("治疗效率", "HealOutputIncrease"),
        ("主能力", "MainPercent"),
        ("副能力", "SubPercent"),
        ("终结技充能效率", "UltimateSpGainScalar"),
        ("源石技艺强度", "PhysicalAndSpellInflictionEnhance"),
        ("物理伤害", "PhysicalDamageIncrease"),
        ("法术伤害", "SpellDamageIncrease"),
        ("灼热伤害", "FireDamageIncrease"),
        ("电磁伤害", "PulseDamageIncrease"),
        ("寒冷伤害", "CrystDamageIncrease"),
        ("自然伤害", "NaturalDamageIncrease"),
        ("超域伤害", "EtherDamageIncrease"),
        ("攻击力", "AtkPercent" if "%" in clause else "AtkFinal"),
        ("生命值", "MaxHpPercent" if "%" in clause else "MaxHpFinal"),
        ("力量", "Str"),
        ("敏捷", "Agi"),
        ("智识", "Wisd"),
        ("意志", "Will"),
    )
    return next((target for label, target in label_targets if label in plain), "")


def _loadout_status_effect_bonus(arts_strength: float) -> float:
    strength = max(0.0, float(arts_strength))
    return 2 * strength / (strength + 300) if strength else 0.0


def format_status_quick_calc(status_name: str, level: int, arts_strength: int) -> str:
    if status_name not in LOADOUT_STATUS_LEVELS:
        raise ValueError("仅支持腐蚀、导电或碎甲")
    if level not in range(1, 5):
        raise ValueError("异常效果等级必须在 1–4 之间")
    if arts_strength < 0:
        raise ValueError("源石技艺强度不能小于 0")

    bonus = _loadout_status_effect_bonus(arts_strength)
    effect = _make_loadout_status_levels(status_name, bonus)[level - 1]
    return "\n".join(
        [
            f"Lv{level} {status_name}速算",
            f"源石技艺强度：{arts_strength}（附带效果 +{bonus * 100:.1f}%）",
            f"效果：{effect.value}",
            f"构成：{effect.detail}",
            f"持续：{effect.duration}",
        ]
    )


def _build_loadout_status_effects(
    attrs: dict[str, Any],
    operator_potential: int,
    arts_strength: float,
) -> list[LoadoutStatusEffectView]:
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    tags = hero.get("tags") if isinstance(hero.get("tags"), list) else []
    bonus = _loadout_status_effect_bonus(arts_strength)
    latest_talents = _latest_loadout_operator_items(attrs.get("talents"), "talents")
    potentials = _loadout_operator_potentials(attrs.get("potentials"), operator_potential)
    duration_additions = {name: 0.0 for name in LOADOUT_STATUS_TAGS}
    maximum_multipliers = {name: 1.0 for name in LOADOUT_STATUS_TAGS}
    for item in (*latest_talents, *potentials):
        description = _first_text(item, "description", "desc", "effect")
        plain = _clean_fz_rich_text(description)
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        for status_name in _loadout_status_names(description):
            if "自身施加" in plain and "效果持续时间" in plain:
                duration_additions[status_name] += sum(
                    _to_float(value) or 0.0
                    for key, value in values.items()
                    if "duration_add" in str(key).lower()
                )
            if status_name == "腐蚀" and "降低的最大抗性" in plain:
                maximum_multipliers[status_name] += sum(
                    _to_float(value) or 0.0
                    for key, value in values.items()
                    if "corrupt_rate" in str(key).lower()
                )

    status_sources = {
        status_name: "普通附带效果"
        for status_name in LOADOUT_STATUS_TAGS
        if status_name in tags
    }
    for attachment_name, status_name in LOADOUT_STATUS_REACTIONS.items():
        if attachment_name in tags and status_name not in status_sources:
            status_sources[status_name] = f"法术反应 · {attachment_name}"

    result: list[LoadoutStatusEffectView] = []
    for status_name in LOADOUT_STATUS_TAGS:
        source = status_sources.get(status_name)
        if not source:
            continue
        notes = [f"源石技艺增益 +{bonus * 100:.1f}%"]
        if duration_additions[status_name]:
            notes.append(f"特性持续 +{_format_status_number(duration_additions[status_name])}秒")
        if maximum_multipliers[status_name] != 1:
            notes.append(f"最大降抗 ×{maximum_multipliers[status_name]:.2f}")
        result.append(
            LoadoutStatusEffectView(
                name=status_name,
                source=source,
                levels=_make_loadout_status_levels(
                    status_name,
                    bonus,
                    duration_add=duration_additions[status_name],
                    maximum_multiplier=maximum_multipliers[status_name],
                ),
                note=" · ".join(notes),
            )
        )

    for skill in _unwrap_fz_list(attrs.get("skills"), "skills", "items", "list"):
        if not isinstance(skill, dict):
            continue
        description = _first_text(skill, "description", "desc", "effect")
        plain = _clean_fz_rich_text(description)
        if "强制施加" not in plain:
            continue
        skill_name = _first_text(skill, "name", "title") or "强制异常技能"
        levels = [item for item in skill.get("levels") or [] if isinstance(item, dict)]
        selected = levels[-1] if levels else {}
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        for status_name in _loadout_status_names(description):
            duration_key = LOADOUT_STATUS_DURATION_KEYS[status_name]
            duration = _to_float(_case_insensitive_get(values, duration_key))
            if duration is None:
                duration = _to_float(_case_insensitive_get(values, "duration"))
            duration = duration or 0.0
            characteristic_multiplier = 1.0
            characteristic_notes: list[str] = []
            for potential in potentials:
                potential_description = _first_text(potential, "description", "desc", "effect")
                potential_plain = _clean_fz_rich_text(potential_description)
                if skill_name not in potential_plain or status_name not in _loadout_status_names(potential_description):
                    continue
                potential_values = potential.get("values") if isinstance(potential.get("values"), dict) else {}
                for key, raw_value in potential_values.items():
                    value = _to_float(raw_value)
                    if value is None:
                        continue
                    lowered = str(key).lower()
                    if "duration" in lowered and "持续时间" in potential_plain:
                        if re.search(rf"\{{\s*{re.escape(str(key))}\s*-\s*1\s*:", potential_description, flags=re.I):
                            duration *= value
                            characteristic_notes.append(f"持续 ×{_format_status_number(value)}")
                        else:
                            duration += value
                            characteristic_notes.append(f"持续 +{_format_status_number(value)}秒")
                    if lowered in {"extra_scaling", "effect_scaling"} and "提升至原本" in potential_plain:
                        characteristic_multiplier *= value
                        characteristic_notes.append(f"效果 ×{_format_status_number(value)}")
            note_parts = ["强制异常按 Lv1 基础值", f"源石技艺增益 +{bonus * 100:.1f}%", *characteristic_notes]
            result.append(
                LoadoutStatusEffectView(
                    name=status_name,
                    source=skill_name,
                    forced=True,
                    levels=_make_loadout_status_levels(
                        status_name,
                        bonus,
                        characteristic_multiplier=characteristic_multiplier,
                        forced_duration=duration,
                        level_count=1,
                    ),
                    note=" · ".join(note_parts),
                )
            )
    return result


def _latest_loadout_operator_items(raw: Any, field: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in _unwrap_fz_list(raw, field, "items", "list"):
        if isinstance(item, dict):
            latest[_first_text(item, "name", "title") or str(len(latest))] = item
    return list(latest.values())


def _loadout_operator_potentials(raw: Any, operator_potential: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(_unwrap_fz_list(raw, "potentials", "items", "list"), 1):
        if not isinstance(item, dict):
            continue
        unlock_level = _to_int(_first_value(item, "level", "potentialLevel", "rank")) or index
        if unlock_level <= operator_potential:
            selected.append(item)
    return selected


def _loadout_status_names(description: str) -> list[str]:
    plain = _clean_fz_rich_text(description)
    return [
        name
        for name, richtext_id in LOADOUT_STATUS_TAGS.items()
        if name in plain or richtext_id in description
    ]


def _make_loadout_status_levels(
    status_name: str,
    bonus: float,
    *,
    duration_add: float = 0.0,
    maximum_multiplier: float = 1.0,
    characteristic_multiplier: float = 1.0,
    forced_duration: float = 0.0,
    level_count: int = 4,
) -> list[LoadoutStatusLevelView]:
    effect_multiplier = (1 + bonus) * characteristic_multiplier
    result: list[LoadoutStatusLevelView] = []
    for index, base in enumerate(LOADOUT_STATUS_LEVELS[status_name][:level_count], 1):
        if status_name == "腐蚀":
            initial, per_second, maximum = base
            value = f"最大降抗 {_format_status_number(maximum * effect_multiplier * maximum_multiplier)}"
            detail = (
                f"初始 {_format_status_number(initial * effect_multiplier)}"
                f" · 每秒 {_format_status_number(per_second * effect_multiplier)}"
            )
            duration = forced_duration or (15 + duration_add)
        else:
            base_value, base_duration = base
            label = "法术易伤" if status_name == "导电" else "物理易伤"
            value = f"{label} {_format_status_percent(base_value * effect_multiplier)}"
            detail = f"基础 {_format_status_percent(base_value)}"
            duration = forced_duration or (base_duration + duration_add)
        result.append(
            LoadoutStatusLevelView(
                level=index,
                value=value,
                detail=detail,
                duration=f"{_format_status_number(duration)}秒",
            )
        )
    return result


def _format_status_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_status_percent(value: float) -> str:
    return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _build_loadout_advanced_stats(values: dict[str, float]) -> list[LoadoutPanelStatView]:
    labels = dict(LOADOUT_ATTRIBUTE_NAMES)
    labels.update(
        {
            "PhysicalResistance": "物理抗性",
            "FireResistance": "灼热抗性",
            "PulseResistance": "电磁抗性",
            "CrystResistance": "寒冷抗性",
            "NaturalResistance": "自然抗性",
            "EtherResistance": "超域抗性",
        }
    )
    order = (
        "CriticalRate",
        "CriticalDamageIncrease",
        "PhysicalAndSpellInflictionEnhance",
        "PhysicalResistance",
        "FireResistance",
        "PulseResistance",
        "CrystResistance",
        "NaturalResistance",
        "EtherResistance",
        "HealOutputIncrease",
        "HealTakenIncrease",
        "UltimateSpGainScalar",
        "ComboSkillCooldownScalar",
        "PoiseDamageOutputScalar",
        "AllDamageIncrease",
        "AllDamageTakenScalar",
        "NormalAttackDamageIncrease",
        "NormalSkillDamageIncrease",
        "ComboSkillDamageIncrease",
        "UltimateSkillDamageIncrease",
        "PhysicalDamageIncrease",
        "SpellDamageIncrease",
        "FireDamageIncrease",
        "PulseDamageIncrease",
        "CrystDamageIncrease",
        "NaturalDamageIncrease",
        "EtherDamageIncrease",
    )
    resistance_keys = {
        "PhysicalResistance",
        "FireResistance",
        "PulseResistance",
        "CrystResistance",
        "NaturalResistance",
        "EtherResistance",
    }
    always_show = {"CriticalRate", "CriticalDamageIncrease", "PhysicalAndSpellInflictionEnhance", *resistance_keys}
    result: list[LoadoutPanelStatView] = []
    for key in order:
        value = values.get(key, 0.0)
        if key not in always_show and abs(value) < 1e-9:
            continue
        if key in LOADOUT_PERCENT_ATTRIBUTES or key in resistance_keys | {"AllDamageTakenScalar"}:
            formatted = _format_loadout_percent(value)
        else:
            formatted = str(math.floor(value))
        result.append(LoadoutPanelStatView(key, labels.get(key, key), formatted))
    return result


def _format_loadout_percent(value: float) -> str:
    return f"{value * 100:.1f}%"
