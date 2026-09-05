"""Operators view construction; no I/O or command registration."""

from __future__ import annotations

import re
from typing import (
    Any,
)

from ...providers.assets import (
    element_icon_urls,
    item_icon_urls,
    operator_icon_urls,
    operator_portrait_urls,
    operator_round_icon_urls,
    profession_icon_urls,
    weapon_type_icon_urls,
)
from ..models import (
    LEVEL_COLUMNS,
    EffectView,
    OperatorCatalogElementView,
    OperatorCatalogItemView,
    OperatorCatalogProfessionView,
    OperatorCatalogView,
    OperatorView,
    SkillLevelView,
    SkillView,
)
from ..skill_metrics import (
    fz_metric_replaces_generic,
)
from .common import (
    _alias_key,
    _build_fz_term_styles,
    _build_term_styles,
    _case_insensitive_get,
    _first_text,
    _first_value,
    _format_fz_template,
    _format_plain_number,
    _format_template_value,
    _fz_asset_raw_url,
    _fz_effect_title,
    _fz_hero_meta_value,
    _fz_overview_entries,
    _fz_template_attrs,
    _level_label,
    _ordered_fz_levels,
    _text_list,
    _to_int,
    _unwrap_fz_list,
    _weapon_name,
    clean_text,
    skill_icon_url,
)
from .constants import (
    OPERATOR_ELEMENT_ORDER,
    OPERATOR_PROFESSION_ORDER,
    SKILL_CATEGORY_ORDER,
    WARFARIN_METRIC_LABELS,
    WARFARIN_PERCENT_METRIC_KEYS,
)


def build_operator_view(raw: dict[str, Any]) -> OperatorView:
    meta = raw.get("meta") or {}
    data = raw.get("data") or {}
    refs = raw.get("refs") or {}
    character = data.get("characterTable") or {}
    growth = data.get("charGrowthTable") or {}
    item_table = data.get("itemTable") or {}
    operator_id = str(meta.get("id") or character.get("charId") or "")

    profession_id = character.get("profession")
    profession_ref = (refs.get("charProfessionTable") or {}).get(str(profession_id), {})
    char_type_id = str(character.get("charTypeId") or "")
    type_ref = (refs.get("charTypeTable") or {}).get(char_type_id, {})
    tag_table = refs.get("tagDataTable") or {}

    tags: list[str] = []
    for tag_id in character.get("charBattleTagIds") or []:
        tag_name = str((tag_table.get(str(tag_id)) or {}).get("tagName") or "").strip()
        if tag_name:
            tags.append(tag_name)

    view = OperatorView(
        name=_first_text(meta, "name") or _first_text(character, "name"),
        slug=_first_text(meta, "slug"),
        operator_id=operator_id,
        english_name=_first_text(character, "engName"),
        rarity=int(character.get("rarity") or 0),
        profession=_first_text(profession_ref, "name") or "未知职业",
        damage_type=_first_text(type_ref, "name") or char_type_id or "未知属性",
        weapon_type=_weapon_name(character.get("weaponType"), item_table.get("desc")),
        species=_extract_species(character),
        tags=tags[:4],
        icon_url=(operator_icon_urls(operator_id) or ("",))[0],
        round_icon_url=(operator_round_icon_urls(operator_id) or ("",))[0],
        portrait_url=(operator_portrait_urls(operator_id) or ("",))[0],
        skills=_build_skills(data.get("skillPatchTable") or {}, growth.get("skillGroupMap") or {}),
        talents=_build_talents(
            data.get("potentialTalentEffectTable") or {},
            growth.get("talentNodeMap") or {},
        ),
        potentials=_build_potentials(
            data.get("characterPotentialTable") or {},
            data.get("potentialTalentEffectTable") or {},
        ),
        term_styles=_build_term_styles(refs),
        source_version=str(meta.get("version") or ""),
    )
    return view


def build_fz_operator_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> OperatorView:
    article = raw.get("article") or {}
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    skills = _build_fz_operator_skills(attrs.get("skills"))
    if not hero or not skills:
        raise ValueError("FZ operator article does not match the supported card schema")

    title = _first_text(article, "title")
    name = _first_text(hero, "name", "nameCn", "cnName", "title") or title.split("/", 1)[-1]
    if not name:
        raise ValueError("FZ operator article is missing name")

    rarity = _to_int(_first_value(hero, "rarity", "star", "stars"))
    species_label, species_value = _fz_species_info(attrs)
    return OperatorView(
        name=name,
        slug=title or name,
        operator_id=str(
            _first_value(hero, "id", "charId", "operatorId")
            or ((hero.get("meta") or {}) if isinstance(hero.get("meta"), dict) else {}).get("charId")
            or ""
        ),
        english_name=_first_text(hero, "nameEn", "englishName", "engName"),
        rarity=rarity,
        profession=_first_text(hero, "profession", "class", "job") or "未知职业",
        damage_type=_first_text(hero, "element", "damageType", "type") or "未知属性",
        weapon_type=_first_text(hero, "weaponType", "weapon") or "未知武器",
        species=species_value,
        species_label=species_label,
        tags=_text_list(_first_value(hero, "tags", "tagList"))[:4],
        icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "avatarUrl", "icon")),
        round_icon_url=_fz_asset_raw_url(_first_text(hero, "roundIconUrl", "avatarRoundUrl")),
        portrait_url=_fz_asset_raw_url(_first_text(hero, "portraitFile", "portraitUrl", "illustUrl", "imageUrl")),
        skills=skills,
        talents=_build_fz_effects(attrs.get("talents"), "talent"),
        potentials=_build_fz_effects(attrs.get("potentials"), "potential"),
        term_styles=_build_fz_term_styles(richtext or {}),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_fz_operator_catalog_view(
    raw: dict[str, Any],
    element_filter: str = "",
    profession_filter: str = "",
) -> OperatorCatalogView:
    article = raw.get("article") or {}
    entries = _fz_overview_entries(raw)
    if not entries:
        raise ValueError("FZ operator roster does not match the supported catalog schema")

    element_filter = clean_text(element_filter)
    profession_filter = clean_text(profession_filter)
    grouped: dict[str, dict[str, list[OperatorCatalogItemView]]] = {}
    element_meta: dict[str, tuple[str, str]] = {}
    profession_icons: dict[str, str] = {}
    for entry in entries:
        name = _first_text(entry, "name")
        title = _first_text(entry, "title") or (f"干员/{name}" if name else "")
        element = _first_text(entry, "element") or "未知元素"
        profession = _first_text(entry, "profession") or "未知职业"
        if not name or not title:
            continue
        if element_filter and element != element_filter:
            continue
        if profession_filter and profession != profession_filter:
            continue
        element_icon_url = (element_icon_urls(element) or ("",))[0]
        profession_icon_url = (profession_icon_urls(profession) or ("",))[0]
        weapon_type_icon_url = (weapon_type_icon_urls(_first_text(entry, "weaponType")) or ("",))[0]
        item = OperatorCatalogItemView(
            name=name,
            title=title,
            operator_id=str(entry.get("charId") or ""),
            english_name=_first_text(entry, "nameEn", "englishName"),
            rarity=_to_int(entry.get("rarity")),
            element=element,
            element_color=_first_text(entry, "elementColor") or "#888888",
            profession=profession,
            weapon_type=_first_text(entry, "weaponType"),
            icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
            element_icon_url=element_icon_url,
            profession_icon_url=profession_icon_url,
            weapon_type_icon_url=weapon_type_icon_url,
        )
        grouped.setdefault(element, {}).setdefault(profession, []).append(item)
        element_meta.setdefault(element, (item.element_color, element_icon_url))
        profession_icons.setdefault(profession, profession_icon_url)

    elements: list[OperatorCatalogElementView] = []
    for element, professions in grouped.items():
        profession_views: list[OperatorCatalogProfessionView] = []
        for profession, items in professions.items():
            items.sort(key=lambda item: (-item.rarity, item.name))
            profession_views.append(
                OperatorCatalogProfessionView(profession, profession_icons.get(profession, ""), items)
            )
        profession_views.sort(key=lambda group: (OPERATOR_PROFESSION_ORDER.get(group.name, 99), group.name))
        color, icon_url = element_meta.get(element, ("#888888", ""))
        elements.append(OperatorCatalogElementView(element, color, icon_url, profession_views))
    elements.sort(key=lambda group: (OPERATOR_ELEMENT_ORDER.get(group.name, 99), group.name))
    total_count = sum(len(profession.items) for element in elements for profession in element.professions)
    if (element_filter or profession_filter) and not elements:
        raise ValueError(f"FZ operator catalog filter not found: {element_filter} {profession_filter}".strip())
    if element_filter and profession_filter:
        title = f"{element_filter} · {profession_filter}"
    elif element_filter:
        title = f"{element_filter}干员"
    elif profession_filter:
        title = f"{profession_filter}干员"
    else:
        title = "全部干员"
    return OperatorCatalogView(
        title=title,
        elements=elements,
        total_count=total_count,
        element_filter=element_filter,
        profession_filter=profession_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def _fz_species_info(attrs: dict[str, Any]) -> tuple[str, str]:
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    species = _first_text(hero, "species", "race")
    if species:
        return "种族", species
    meta_species = _fz_hero_meta_value(hero, "种族", "race", "species")
    if meta_species:
        return "种族", meta_species
    archive_species = _fz_archive_species(attrs.get("archive"))
    if archive_species:
        return "种族", archive_species
    faction = _first_text(hero, "faction", "camp", "organization") or _fz_hero_meta_value(hero, "所属", "阵营", "组织")
    if faction:
        return "所属", faction
    return "种族", "未知种族"


def _fz_archive_species(raw: Any) -> str:
    for text in _iter_fz_archive_text(raw):
        match = re.search(r"【种族】[^\S\r\n]*([^【\n\r]+)", text)
        if match:
            species = clean_text(match.group(1)).strip()
            if species:
                return species
    return ""


def _iter_fz_archive_text(raw: Any):
    if isinstance(raw, dict):
        for key in ("body", "text", "content", "desc", "description", "recordDesc"):
            value = raw.get(key)
            if isinstance(value, str):
                yield value
        for key in ("archive", "items", "list", "records"):
            yield from _iter_fz_archive_text(raw.get(key))
    elif isinstance(raw, list):
        for item in raw:
            yield from _iter_fz_archive_text(item)


def _build_fz_operator_skills(raw: Any) -> list[SkillView]:
    skills = _unwrap_fz_list(raw, "skills", "items", "list")
    result: list[SkillView] = []
    for index, item in enumerate(skills, 1):
        if not isinstance(item, dict):
            continue
        title = _first_text(item, "name", "title", "skillName")
        if not title:
            continue
        all_levels = _ordered_fz_levels(_unwrap_fz_list(_first_value(item, "levels", "levelData", "records"), "levels", "items", "records"))
        raw_levels = all_levels[-4:]
        selected_positions = [position for position, level in enumerate(all_levels) if any(level is selected for selected in raw_levels)]
        param_values = _fz_param_table_values(
            item.get("paramTable"),
            selected_positions,
            _fz_skill_condition_names(item),
        )
        levels = _build_fz_skill_levels(raw_levels, param_values)
        best_level = raw_levels[-1] if raw_levels else {}
        result.append(
            SkillView(
                skill_id=str(_first_value(item, "id", "skillId") or f"fz_skill_{index}"),
                title=title,
                icon_id=_fz_icon_url(item) or _first_text(item, "iconId"),
                category=_first_text(item, "category", "type") or _fz_skill_category(index),
                description=_format_fz_template(
                    _first_text(item, "description", "desc"),
                    _first_value(best_level, "values", "blackboard", "params"),
                ),
                form_descriptions=_build_fz_skill_form_descriptions(
                    item,
                    _first_value(best_level, "values", "blackboard", "params"),
                ),
                levels=levels,
            )
        )
    return result


def _build_fz_skill_form_descriptions(item: dict[str, Any], values: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for condition in _unwrap_fz_list(item.get("conditions"), "conditions", "items", "list"):
        if not isinstance(condition, dict):
            continue
        name = _first_text(condition, "name", "title", "label")
        raw_desc = _first_value(condition, "postDesc", "description", "desc")
        if not name or not raw_desc:
            continue
        raw_desc = re.sub(r"(?m)^\s*-\s*", "", str(raw_desc))
        description = _format_fz_template(raw_desc, values)
        description = re.sub(
            rf"(?:<[@#][A-Za-z0-9_.-]+>)?{re.escape(name)}(?:</>)?\s*[：:]\s*",
            "",
            description,
            count=1,
        ).strip()
        if description:
            result.append((name, description))
    return result


def _build_fz_skill_levels(raw: Any, param_values: list[dict[str, str]] | None = None) -> list[SkillLevelView]:
    levels: list[SkillLevelView] = []
    for index, item in enumerate(_select_fz_levels(_unwrap_fz_list(raw, "levels", "items", "records")), 1):
        if not isinstance(item, dict):
            continue
        level = _to_int(_first_value(item, "level", "lv")) or index
        values = _first_value(item, "values", "blackboard", "params")
        if not isinstance(values, dict):
            values = {}
        mapped_values = _map_fz_skill_values(values)
        if param_values and index - 1 < len(param_values):
            param_row = param_values[index - 1]
            _drop_generic_fz_metrics(mapped_values, param_row)
            mapped_values.update(param_row)
        cooldown = mapped_values.get("冷却") or str(_first_value(item, "cooldown", "coolDown", "cd") or "")
        if not cooldown or cooldown == "--":
            cooldown = "--"
        cost = mapped_values.get("所需能量") or str(_first_value(item, "cost", "costValue", "sp") or "")
        if not cost or cost == "--":
            cost = "--"
        levels.append(
            SkillLevelView(
                label=_level_label(level),
                level=level,
                values=mapped_values,
                cooldown=cooldown,
                cost=cost,
                charge=str(_first_value(item, "charge", "maxChargeTime") or "--"),
                description=_format_fz_template(_first_text(item, "description", "desc"), values),
            )
        )
    return levels


def _drop_generic_fz_metrics(mapped_values: dict[str, str], param_row: dict[str, str]) -> None:
    for generic_name in tuple(mapped_values):
        if any(fz_metric_replaces_generic(generic_name, specific_name) for specific_name in param_row):
            mapped_values.pop(generic_name, None)


def _build_fz_effects(raw: Any, kind: str) -> list[EffectView]:
    effects: list[EffectView] = []
    talent_by_title: dict[str, tuple[tuple[int, int], EffectView]] = {}
    for index, item in enumerate(_unwrap_fz_list(raw, "talents", "potentials", "items", "list"), 1):
        if not isinstance(item, dict):
            continue
        title = _first_text(item, "name", "title")
        values = _first_value(item, "values", "blackboard", "params")
        description = _format_fz_template(_first_text(item, "description", "desc", "effect"), values)
        if not title and not description:
            continue
        level = _to_int(_first_value(item, "level", "potentialLevel", "rank")) or index
        view = EffectView(
            effect_id=str(_first_value(item, "id", "effectId") or f"fz_{kind}_{index}"),
            title=_fz_effect_title(kind, title, level, index),
            description=description,
            kind="天赋" if kind == "talent" else "潜能" if kind == "potential" else kind,
            icon_url=_fz_icon_url(item) or _fz_asset_raw_url(_first_text(item, "iconUrl", "icon")),
        )
        if kind == "talent":
            dedupe_key = title or view.title
            rank = (_to_int(_first_value(item, "level", "rank")), _to_int(_first_value(item, "unlockStage", "stage")))
            previous = talent_by_title.get(dedupe_key)
            if previous is None or rank >= previous[0]:
                talent_by_title[dedupe_key] = (rank, view)
            continue
        effects.append(view)
    if kind == "talent":
        return [record[1] for record in talent_by_title.values()]
    return effects


def _fz_icon_url(item: dict[str, Any]) -> str:
    icon = item.get("icon")
    if isinstance(icon, dict):
        glyph = icon.get("glyph")
        if isinstance(glyph, dict):
            glyph_url = _first_text(glyph, "url", "src")
            if glyph_url:
                return _fz_asset_raw_url(glyph_url)
        direct = _first_text(icon, "url", "src", "iconUrl")
        if direct:
            return _fz_asset_raw_url(direct)
    return _fz_asset_raw_url(_first_text(item, "iconUrl", "avatarUrl"))


def _fz_skill_category(index: int) -> str:
    return {
        1: "普攻",
        2: "战技",
        3: "连携技",
        4: "终结技",
    }.get(index, "技能")


def _select_fz_levels(levels: list[Any]) -> list[dict[str, Any]]:
    return _ordered_fz_levels(levels)[-4:]


def _fz_skill_condition_names(item: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for condition in _unwrap_fz_list(item.get("conditions"), "conditions", "items", "list"):
        if not isinstance(condition, dict):
            continue
        condition_id = _first_text(condition, "id", "conditionId", "key")
        name = _first_text(condition, "name", "title", "label")
        if condition_id and name:
            result[condition_id] = name
    return result


def _fz_param_table_values(
    raw: Any,
    selected_positions: list[int],
    condition_names: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = [{} for _ in selected_positions]
    if not isinstance(raw, dict) or not selected_positions:
        return result
    rows = _unwrap_fz_list(raw.get("rows") or raw, "rows", "items", "list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = _map_fz_param_label(_first_text(row, "label", "name", "title", "key"))
        if not label:
            continue
        condition_id = _first_text(row, "conditionId", "condition", "formId")
        condition_name = (condition_names or {}).get(condition_id, "")
        if condition_name:
            label = f"{condition_name}{label}"
        raw_values = _first_value(row, "values", "valueList", "data", "columns")
        for out_index, source_index in enumerate(selected_positions):
            value = _fz_param_value_at(raw_values, source_index)
            if value in (None, ""):
                continue
            result[out_index][label] = _format_fz_metric_value(value, percent=_fz_param_is_percent(label))
    return result


def _fz_param_value_at(values: Any, index: int) -> Any:
    if isinstance(values, list):
        if index >= len(values):
            return None
        value = values[index]
        if isinstance(value, dict):
            return _first_value(value, "value", "text", "display", "content")
        return value
    if isinstance(values, dict):
        value = _first_value(values, str(index), str(index + 1), f"Lv{index + 1}", f"lv{index + 1}")
        if isinstance(value, dict):
            return _first_value(value, "value", "text", "display", "content")
        return value
    return None


def _map_fz_param_label(label: str) -> str:
    label = clean_text(label)
    aliases = {
        "伤害倍率": "攻击倍率",
        "攻击倍率": "攻击倍率",
        "失衡值": "失衡值",
        "所需终结技能量": "所需能量",
        "所需能量": "所需能量",
        "冷却": "冷却",
        "冷却时间": "冷却",
        "技力消耗": "技力消耗",
        "获得终结技能量": "获得终结技能量",
        "持续时间": "持续时间",
    }
    return aliases.get(label, label)


def _fz_param_is_percent(label: str) -> bool:
    return "倍率" in label or "比例" in label


def _map_fz_skill_values(values: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}

    def add(label: str, *keys: str, percent: bool = False) -> None:
        for key in keys:
            value = _case_insensitive_get(values, key)
            if value not in (None, ""):
                result[label] = _format_fz_metric_value(value, percent=percent)
                return

    add("攻击倍率", "display_atk_scale", "atk_scale", percent=True)
    add("失衡值", "display_poise", "poise")
    add("持续时间", "duration")
    add("技力", "usp")
    add("冷却", "cooldown", "CoolDown", "coolDown")
    return result


def _format_fz_metric_value(value: Any, *, percent: bool = False) -> str:
    text = clean_text(value)
    if not text:
        return "--"
    if "%" in text:
        return text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return text
    if percent:
        if abs(number) <= 2:
            number *= 100
        return f"{number:.0f}%"
    return _format_plain_number(number)


def _extract_species(character: dict[str, Any]) -> str:
    for record in character.get("profileRecord") or []:
        desc = clean_text(record.get("recordDesc"))
        match = re.search(r"【种族】([^【\s]+)", desc)
        if match:
            return match.group(1).strip()
    return "未知种族"


def _skill_group_meta(skill_group_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for group in skill_group_map.values():
        category = _skill_group_category(group.get("skillGroupType"))
        for skill_id in group.get("skillIdList") or []:
            result[str(skill_id)] = {
                "name": clean_text(group.get("name")),
                "category": category,
                "icon": str(group.get("icon") or ""),
                "desc": clean_text(group.get("desc")),
                "skillGroupType": str(group.get("skillGroupType") or ""),
            }
    return result


def _skill_group_category(group_type: Any) -> str:
    try:
        group_type_value = int(group_type)
    except (TypeError, ValueError):
        group_type_value = -1
    return {
        0: "普攻",
        1: "战技",
        2: "终结技",
        3: "连携技",
    }.get(group_type_value, "")


def _skill_records_for_group(skill_table: dict[str, Any], skill_ids: list[str], category: str = "") -> list[dict[str, Any]]:
    if category == "普攻":
        preferred_ids = [
            skill_id
            for skill_id in skill_ids
            if re.search(r"(?:attack5|attack_5|attack-5|combo5|combo_5|combo-5)$", skill_id)
        ]
        skill_ids = preferred_ids or skill_ids
    records: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        for record in bundle.get("SkillPatchDataBundle") or []:
            records.append(record)
        if records:
            break
    return records


def _all_skill_records_for_group(skill_table: dict[str, Any], skill_ids: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records.extend(bundle.get("SkillPatchDataBundle") or [])
    return records


def _talent_node_meta(talent_node_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for node in talent_node_map.values():
        info = (node or {}).get("passiveSkillNodeInfo") or {}
        effect_id = str(info.get("talentEffectId") or "")
        if not effect_id:
            continue
        result[effect_id] = {
            "name": clean_text(info.get("name")),
            "icon_id": str(info.get("iconId") or ""),
        }
    return result


def _build_skills(skill_table: dict[str, Any], skill_group_map: dict[str, Any]) -> list[SkillView]:
    items: list[SkillView] = []
    for group in skill_group_map.values():
        category = _skill_group_category(group.get("skillGroupType"))
        if category not in SKILL_CATEGORY_ORDER:
            continue
        skill_ids = [str(skill_id) for skill_id in (group.get("skillIdList") or [])]
        records = _skill_records_for_group(skill_table, skill_ids, category)
        group_records = _all_skill_records_for_group(skill_table, skill_ids)
        if not records:
            continue
        levels = [_build_level(records, level, label, category) for level, label in LEVEL_COLUMNS]
        _merge_additional_skill_levels(levels, skill_table, skill_ids[1:], category)
        sample = _record_by_level(records, 9) or records[0]
        skill_id = str(skill_ids[0] if skill_ids else sample.get("skillId") or group.get("skillGroupId") or "")
        title = clean_text(group.get("name")) or category or "技能"
        items.append(
            SkillView(
                skill_id=skill_id,
                title=title,
                icon_id=str(group.get("icon") or sample.get("iconId") or ""),
                category=category,
                description=_format_skill_desc(group.get("desc") or sample.get("description"), group_records or records, category),
                form_descriptions=_build_skill_form_descriptions(group, group_records or records, category),
                levels=levels,
                extra_levels=_build_extra_levels(skill_table, skill_ids, category),
            )
        )
    return sorted(items, key=lambda item: (SKILL_CATEGORY_ORDER.get(item.category, 99), item.skill_id))


def _build_skill_form_descriptions(
    group: dict[str, Any],
    records: list[dict[str, Any]],
    category: str,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for index in (1, 2):
        name = clean_text(group.get(f"conditionName{index}"))
        raw_desc = group.get(f"conditionPostDesc{index}")
        if not name or not raw_desc:
            continue
        description = _format_skill_desc(raw_desc, records, category)
        description = clean_text(description.replace(f"{name}：", "", 1))
        if description:
            result.append((name, description))
    return result


def _build_extra_levels(skill_table: dict[str, Any], skill_ids: list[str], category: str) -> dict[str, list[SkillLevelView]]:
    if category != "普攻":
        return {}
    result: dict[str, list[SkillLevelView]] = {}
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records = list(bundle.get("SkillPatchDataBundle") or [])
        if records:
            result[skill_id] = [_build_level(records, level, label, category) for level, label in LEVEL_COLUMNS]
    return result


def _merge_additional_skill_levels(
    levels: list[SkillLevelView],
    skill_table: dict[str, Any],
    skill_ids: list[str],
    category: str,
) -> None:
    if category != "终结技":
        return
    levels_by_number = {level.level: level for level in levels}
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records = list(bundle.get("SkillPatchDataBundle") or [])
        for level_number, label in LEVEL_COLUMNS:
            target = levels_by_number.get(level_number)
            if target is None:
                continue
            additional = _build_level(records, level_number, label, category)
            target.values.update(additional.values)


def _build_talents(effect_table: dict[str, Any], talent_node_map: dict[str, Any]) -> list[EffectView]:
    result_by_title: dict[str, EffectView] = {}
    talent_meta = _talent_node_meta(talent_node_map)
    for effect_id, effect in sorted(effect_table.items(), key=lambda item: _effect_sort_key(item[0])):
        if "_talent_" not in effect_id:
            continue
        meta = talent_meta.get(str(effect_id)) or {}
        title = clean_text(meta.get("name")) or _talent_title(effect_id)
        result_by_title[title] = EffectView(
            effect_id=str(effect_id),
            title=title,
            description=_format_effect_desc(effect),
            kind="天赋",
            icon_url=skill_icon_url(str(meta.get("icon_id") or "")),
        )
    return list(result_by_title.values())


def _build_potentials(potential_table: dict[str, Any], effect_table: dict[str, Any]) -> list[EffectView]:
    result: list[EffectView] = []
    for item in potential_table.get("potentialUnlockBundle") or []:
        effect_id = str(item.get("potentialEffectId") or "")
        effect = effect_table.get(effect_id) or {}
        if not effect_id or not effect:
            continue
        level = int(item.get("level") or len(result) + 1)
        name = clean_text(item.get("name")) or f"潜能 {level}"
        result.append(
            EffectView(
                effect_id=effect_id,
                title=f"P{level} {name}",
                description=_format_effect_desc(effect),
                kind="潜能",
                icon_url=_potential_icon_url(potential_table, item),
            )
        )
    return result


def _build_level(records: list[dict[str, Any]], level: int, label: str, category: str = "") -> SkillLevelView:
    record = _record_by_level(records, level)
    if not record:
        return SkillLevelView(label=label, level=level)
    return SkillLevelView(
        label=label,
        level=level,
        values=_extract_values(record, category),
        cooldown=_format_plain_number(record.get("coolDown")),
        cost=_format_plain_number(record.get("costValue")),
        charge=_format_plain_number(record.get("maxChargeTime")),
        description=clean_text(record.get("description")),
    )


def _record_by_level(records: list[dict[str, Any]], level: int) -> dict[str, Any] | None:
    for record in records:
        if int(record.get("level") or 0) == level:
            return record
    return None


def _extract_values(record: dict[str, Any], category: str = "") -> dict[str, str]:
    values: dict[str, str] = {}
    names = list(record.get("subDescNameList") or [])
    raw_values = list(record.get("subDescList") or [])
    for index, name in enumerate(names):
        metric = _normalize_metric_name(clean_text(name), category)
        value = clean_text(raw_values[index] if index < len(raw_values) else "")
        if metric and value:
            values[metric] = value
    if values:
        return values
    for item in record.get("blackboard") or []:
        key = str(item.get("key") or "").strip()
        if not key or key.startswith("display_"):
            continue
        label = _metric_label_from_key(key, category, str(record.get("skillId") or ""))
        values[label] = _format_blackboard_value(key, item.get("value"), item.get("valueStr"))
    return values


def _format_effect_desc(effect: dict[str, Any]) -> str:
    desc = str(effect.get("desc") or "")
    values = _effect_values(effect)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        value = _template_expression_value(key, values)
        return _format_template_value(value, fmt)

    rendered = re.sub(r"\{([^{}]+)\}", replace, desc)
    rendered = re.sub(r"(?m)^\s*-\s*", "", rendered)
    return clean_text(rendered)


def _format_skill_desc(desc: Any, records: list[dict[str, Any]], category: str = "") -> str:
    text = _primary_skill_desc(str(desc or ""), category)
    values = _skill_template_values(records)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        value = _template_expression_value(key, values)
        return _format_template_value(value, fmt)

    rendered = re.sub(r"\{([^{}]+)\}", replace, text)
    rendered = re.sub(r"(?m)^\s*-\s*", "", rendered)
    return clean_text(rendered)


def _primary_skill_desc(desc: str, category: str) -> str:
    if category != "普攻":
        return desc
    for marker in ("\n\n下落攻击", "\n下落攻击", "下落攻击：", "\n\n处决攻击", "\n处决攻击", "处决攻击："):
        if marker in desc:
            return desc.split(marker, 1)[0]
    return desc


def _normalize_metric_name(name: str, category: str) -> str:
    if category == "普攻":
        name = re.sub(r"普攻第[一二三四五六七八九十]+段", "普攻", name)
        name = re.sub(r"普攻第\d+段", "普攻", name)
    return name


def _skill_template_values(records: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    # Prefer Lv9 values for the rendered description, but fall back to any
    # available level so Warfarin templates do not leak into the image.
    ordered_records = sorted(
        records,
        key=lambda record: 0 if int(record.get("level") or 0) == 9 else 1,
    )
    for record in ordered_records:
        for item in record.get("blackboard") or []:
            key = str(item.get("key") or "").strip()
            if not key or key.startswith("display_"):
                continue
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            current = values.get(key)
            if current is None or (abs(current) < 0.0001 and abs(value) >= 0.0001):
                values[key] = value
    return values


def _effect_values(effect: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in effect.get("dataList") or []:
        for bb in (item.get("attachBuff") or {}).get("blackboard") or []:
            _store_effect_value(values, bb.get("key"), bb.get("value"))
        for bb in (item.get("attachSkill") or {}).get("blackboard") or []:
            _store_effect_value(values, bb.get("key"), bb.get("value"))
        attr = item.get("attrModifier") or {}
        attr_type = int(attr.get("attrType") or 0)
        attr_value = attr.get("attrValue")
        if attr_type and attr_value not in (None, ""):
            _store_effect_value(values, _attribute_placeholder(attr_type), attr_value)
            if attr_type in {41, 42}:
                _store_effect_value(values, "Will", attr_value)
        skill_bb = item.get("skillBbModifier") or {}
        _store_effect_value(values, skill_bb.get("bbKey"), skill_bb.get("floatValue"))
        skill_param = item.get("skillParamModifier") or {}
        param_type = int(skill_param.get("paramType") or 0)
        if param_type:
            _store_effect_value(values, {1: "costvalue", 2: "coolDown"}.get(param_type, f"param_{param_type}"), skill_param.get("paramValue"))
    return values


def _store_effect_value(values: dict[str, float], key: Any, value: Any) -> None:
    key = str(key or "").strip()
    if not key:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    values.setdefault(key, number)


def _attribute_placeholder(attr_type: int) -> str:
    return {
        39: "Str",
        40: "Agi",
        41: "Int",
        42: "Will",
        50: "PhysicalDamageIncrease",
        51: "FireDamageIncrease",
        52: "PulseDamageIncrease",
        53: "CrystDamageIncrease",
        54: "NaturalDamageIncrease",
        55: "EtherDamageIncrease",
        87: "PhysicalAndSpellInflictionEnhance",
    }.get(attr_type, f"attr_{attr_type}")


def _template_expression_value(expr: str, values: dict[str, float]) -> float | None:
    direct = values.get(expr)
    if direct is not None:
        return direct
    alias = values.get(_alias_key(expr))
    if alias is not None:
        return alias
    match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)\s*([+-])\s*([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)",
        expr,
    )
    if not match:
        return None

    def operand(token: str) -> float | None:
        try:
            return float(token)
        except ValueError:
            value = values.get(token)
            if value is None:
                value = values.get(_alias_key(token))
            return value

    left = operand(match.group(1))
    right = operand(match.group(3))
    if left is None or right is None:
        return None
    return left + right if match.group(2) == "+" else left - right


def _potential_icon_url(potential_table: dict[str, Any], item: dict[str, Any]) -> str:
    item_ids = list(item.get("itemIds") or [])
    if item_ids:
        item_id = str(item_ids[0] or "")
        if item_id:
            urls = item_icon_urls(item_id)
            return urls[0] if urls else ""
    first_item_id = str(potential_table.get("firstItemId") or "")
    if first_item_id:
        urls = item_icon_urls(first_item_id)
        return urls[0] if urls else ""
    return ""


def _talent_title(effect_id: str) -> str:
    match = re.search(r"_talent_(\d+)_(\d+)$", effect_id)
    if not match:
        return "固有天赋"
    group = int(match.group(1))
    stage = int(match.group(2))
    roman = {1: "I", 2: "II", 3: "III"}.get(group, str(group))
    return f"固有天赋 {roman} · 阶段 {stage}"


def _effect_sort_key(effect_id: str) -> tuple[int, ...]:
    numbers = [int(item) for item in re.findall(r"\d+", effect_id)]
    return tuple(numbers or [999])


def _metric_label_from_key(key: str, category: str = "", skill_id: str = "") -> str:
    if category == "普攻" and key == "atk_scale":
        if "power_attack" in skill_id:
            return "处决攻击倍率"
        if "plunging_attack" in skill_id:
            return "下落攻击倍率"
        return "普攻倍率"
    if category == "终结技" and "lizhiyan" in skill_id:
        if "ultimate_skill2" in skill_id:
            if key == "atk_scale":
                return "阵诀·智诀明伤害倍率"
            if key == "atk_scale_will":
                return "阵诀·意诀明伤害倍率"
        if key == "atk_scale":
            return "破晦阵伤害倍率"
        if key == "atk_scale_laser":
            return "阵诀·智集束打击倍率"
        if key == "atk_scale_laser_will":
            return "阵诀·意集束打击倍率"
    return WARFARIN_METRIC_LABELS.get(key, key.replace("_", " ").strip())


def _format_blackboard_value(key: str, value: Any, value_str: Any = "") -> str:
    if value_str:
        return clean_text(value_str)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    key_parts = set(key.lower().split("_"))
    if key_parts.intersection({"scale", "rate", "ratio", "vul"}) or key in WARFARIN_PERCENT_METRIC_KEYS:
        return _format_percent(number)
    return _format_metric_number(number)


def _format_percent(number: float) -> str:
    return f"{number * 100:.4f}".rstrip("0").rstrip(".") + "%"


def _format_metric_number(number: float) -> str:
    if abs(number) < 0.0001:
        return "--"
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")
