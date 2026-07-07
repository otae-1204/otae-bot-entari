from __future__ import annotations

import re
from typing import Any

from .client import WarfarinClient
from .models import (
    EffectView,
    LEVEL_COLUMNS,
    OperatorView,
    SkillLevelView,
    SkillView,
    TermStyleView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponView,
)


STATIC_BASE = "https://static.warfarin.wiki/v4"
WEAPON_OPTIONS = ("单手剑", "双手剑", "施术单元", "长枪", "手铳")

SKILL_CATEGORY_ORDER = {
    "普攻": 0,
    "战技": 1,
    "终结技": 2,
    "连携技": 3,
}

WEAPON_NAMES = {
    1: "单手剑",
    2: "双手剑",
    3: "施术单元",
    4: "长枪",
    5: "手铳",
}

TERM_SUFFIXES = (
    "附着",
    "异常",
    "伤害",
    "脆弱",
    "爆发",
    "增幅",
    "抗性",
    "击飞",
    "破防",
    "猛击",
    "倒地",
    "碎甲",
    "碎冰",
    "冻结",
    "燃烧",
    "导电",
    "腐蚀",
    "失衡",
    "消耗",
)


class EndfieldService:
    def __init__(self, client: WarfarinClient):
        self.client = client

    async def get_operator_view(self, query: str) -> OperatorView | None:
        slug = await self.find_operator_slug(query)
        if not slug:
            return None
        raw = await self.client.operator_detail(slug)
        return build_operator_view(raw)

    async def get_weapon_view(self, query: str) -> WeaponView | None:
        title = await self.find_weapon_title(query)
        if not title:
            return None
        raw = await self.client.fz_article_by_title(title)
        richtext = await self.client.fz_game_richtext()
        return build_weapon_view(raw, richtext)

    async def find_weapon_title(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query.startswith("武器/"):
            return query
        exact_title = f"武器/{query}"
        try:
            summaries = await self.client.fz_article_summaries("武器/")
        except Exception:
            return exact_title
        lowered = query.lower()
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if name == query or name.lower() == lowered:
                return title
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if query in name or lowered in name.lower():
                return title
        return exact_title

    async def find_operator_slug(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I):
            return query
        data = await self.client.search(query)
        for item in data.get("results") or []:
            if str(item.get("type") or "") == "operators" and item.get("slug"):
                return str(item["slug"])
        return await self._match_operator_by_name(query)

    async def _match_operator_by_name(self, query: str) -> str | None:
        data = await self.client.operators()
        lowered = query.lower()
        for operator in data.get("data") or []:
            name = str(operator.get("name") or "").strip()
            if name and name.lower() == lowered:
                slug = str(operator.get("slug") or "").strip()
                if slug:
                    return slug
        return None


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
        name=str(meta.get("name") or character.get("name") or ""),
        slug=str(meta.get("slug") or ""),
        operator_id=operator_id,
        english_name=str(character.get("engName") or ""),
        rarity=int(character.get("rarity") or 0),
        profession=str(profession_ref.get("name") or "未知职业"),
        damage_type=str(type_ref.get("name") or char_type_id or "未知属性"),
        weapon_type=_weapon_name(character.get("weaponType"), item_table.get("desc")),
        species=_extract_species(character),
        tags=tags[:4],
        icon_url=f"{STATIC_BASE}/charicon/icon_{operator_id}.webp" if operator_id else "",
        round_icon_url=f"{STATIC_BASE}/charroundicon/icon_round_{operator_id}.webp" if operator_id else "",
        portrait_url=f"{STATIC_BASE}/charsplash/{operator_id}.webp" if operator_id else "",
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


def build_weapon_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> WeaponView:
    article = raw.get("article") or {}
    revision = raw.get("revision") or {}
    content = ((revision.get("contentJson") or {}).get("content") or [{}])[0]
    attrs = content.get("attrs") or {}
    hero = attrs.get("hero") or {}
    stats = attrs.get("stats") or {}
    skills = (attrs.get("skills") or {}).get("skills") or []
    title = str(article.get("title") or "")
    name = str(hero.get("name") or title.split("/", 1)[-1] or "")
    max_level = int(hero.get("maxLv") or 0)
    max_atk = next((row.get("atk") for row in stats.get("curve", []) if row.get("lv") == max_level), None)
    if max_atk is None:
        max_atk = next((row.get("atk") for row in reversed(stats.get("curve", []) or []) if row.get("atk") is not None), "--")
    richtext = richtext or {}
    return WeaponView(
        name=name,
        slug=_weapon_slug(title or name),
        title=title or f"武器/{name}",
        english_name=str(hero.get("nameEn") or ""),
        rarity=int(hero.get("rarity") or 0),
        weapon_type=str(hero.get("weaponType") or "未知武器"),
        max_level=max_level,
        max_atk=max_atk,
        icon_url=str(hero.get("iconUrl") or ""),
        skills=[_build_weapon_skill(skill) for skill in skills],
        rich_text_styles=richtext.get("RICH_TEXT_STYLES") or {},
        rich_text_links=richtext.get("HYPERLINK_TEXTS") or {},
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def _weapon_slug(title: str) -> str:
    name = str(title or "").split("/", 1)[-1]
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", name).strip("-").lower()
    return slug or "weapon"


def _build_weapon_skill(raw: dict[str, Any]) -> WeaponSkillView:
    return WeaponSkillView(
        title=clean_text(raw.get("name")) or "技能",
        description=str(raw.get("description") or ""),
        levels=[
            WeaponSkillLevelView(
                level=int(item.get("level") or index + 1),
                values=item.get("values") or {},
            )
            for index, item in enumerate((raw.get("levels") or [])[:9])
        ],
    )


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[@#]?[A-Za-z0-9_.-]+>", "", text)
    text = re.sub(r"</>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    return " ".join(text.split())


def static_resource_url(path: str) -> str:
    path = str(path or "").strip().replace("\\", "/")
    if not path:
        return ""
    if path.startswith(("http://", "https://", "data:")):
        return path
    if path.lower().startswith("termicon/"):
        return f"{STATIC_BASE}/termicon/{path.rsplit('/', 1)[-1].lower()}.webp"
    return f"{STATIC_BASE}/{path}.webp"


def skill_icon_url(icon_id: str) -> str:
    return f"{STATIC_BASE}/skillicon/{icon_id}.webp" if icon_id else ""


def _build_term_styles(refs: dict[str, Any]) -> dict[str, TermStyleView]:
    hyperlink_table = refs.get("hyperlinkTextTable") or {}
    rich_text_table = refs.get("richTextStyleTable") or {}
    result: dict[str, TermStyleView] = {}
    for term_id, entry in hyperlink_table.items():
        if not str(term_id).startswith("ba."):
            continue
        rich_text_id = str(entry.get("richTextId") or term_id)
        color, style_icon = _rich_text_visual(rich_text_table.get(rich_text_id) or rich_text_table.get(str(term_id)) or {})
        icon_url = static_resource_url(entry.get("iconPath") or style_icon)
        own_terms, referenced_terms = _term_names_from_entry(entry)
        for term in own_terms:
            current = result.get(term)
            result[term] = TermStyleView(term=term, color=color or (current.color if current else ""), icon_url=icon_url or (current.icon_url if current else ""))
        for term in referenced_terms - own_terms:
            current = result.get(term)
            if current:
                if not current.color and color:
                    result[term] = TermStyleView(term=term, color=color, icon_url=current.icon_url)
                continue
            result[term] = TermStyleView(term=term, color=color, icon_url="")
    return result


def _term_names_from_entry(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    own_names: set[str] = set()
    referenced_names: set[str] = set()
    raw_name = clean_text(entry.get("name"))
    if raw_name:
        own_names.add(raw_name)
        if " - " in raw_name:
            left, right = raw_name.split(" - ", 1)
            suffix = _term_suffix_from_name(left)
            if len(right) >= 2 and suffix:
                own_names.add(f"{right}{suffix}")
        elif " - " not in raw_name and " " in raw_name:
            own_names.add(raw_name.rsplit(" ", 1)[-1])
    desc = str(entry.get("desc") or "")
    for _, text in re.findall(r"<[@#]([^>]+)>([^<]+)</>", desc):
        cleaned = clean_text(text)
        if cleaned and _looks_like_term(cleaned):
            referenced_names.add(cleaned)
    own_names = {name for name in own_names if 2 <= len(name) <= 12 and _looks_like_term(name)}
    referenced_names = {name for name in referenced_names if 2 <= len(name) <= 12 and _looks_like_term(name)}
    return own_names, referenced_names


def _term_suffix_from_name(left: str) -> str:
    candidates = sorted((suffix for suffix in TERM_SUFFIXES if suffix and left.endswith(suffix)), key=len, reverse=True)
    if candidates:
        return candidates[0]
    candidates = sorted((suffix for suffix in TERM_SUFFIXES if suffix and suffix in left), key=len, reverse=True)
    return candidates[0] if candidates else ""


def _looks_like_term(name: str) -> bool:
    return any(suffix in name for suffix in TERM_SUFFIXES)


def _rich_text_visual(style: dict[str, Any]) -> tuple[str, str]:
    pre_defs = style.get("preDef") or []
    pre = str(pre_defs[0] if pre_defs else "")
    color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
    icon_match = re.search(r'image="([^"]+)"', pre)
    color = f"#{color_match.group(1)}" if color_match else ""
    icon = icon_match.group(1) if icon_match else ""
    return color, icon


def _weapon_name(weapon_type: Any, item_desc: Any = "") -> str:
    desc = clean_text(item_desc)
    for name in WEAPON_OPTIONS:
        if name in desc:
            return name
    return WEAPON_NAMES.get(weapon_type, "未知武器")


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
                levels=levels,
                extra_levels=_build_extra_levels(skill_table, skill_ids, category),
            )
        )
    return sorted(items, key=lambda item: (SKILL_CATEGORY_ORDER.get(item.category, 99), item.skill_id))


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
        values[_metric_label_from_key(key)] = _format_blackboard_value(key, item.get("value"), item.get("valueStr"))
    return values


def _format_effect_desc(effect: dict[str, Any]) -> str:
    desc = str(effect.get("desc") or "")
    values = _effect_values(effect)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        if key.startswith("1-"):
            base_key = key[2:]
            value = 1 - float(values.get(base_key, 0))
        else:
            value = values.get(key)
            if value is None:
                value = values.get(_alias_key(key))
        return _format_template_value(value, fmt)

    return clean_text(re.sub(r"\{([^{}]+)\}", replace, desc))


def _format_skill_desc(desc: Any, records: list[dict[str, Any]], category: str = "") -> str:
    text = _primary_skill_desc(str(desc or ""), category)
    values = _skill_template_values(records)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        value = values.get(key)
        if value is None:
            value = values.get(_alias_key(key))
        return _format_template_value(value, fmt)

    return clean_text(re.sub(r"\{([^{}]+)\}", replace, text))


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
            values.setdefault(key, value)
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
        42: "Wil",
        50: "PhysicalDamageIncrease",
        51: "FireDamageIncrease",
        52: "PulseDamageIncrease",
        53: "CrystDamageIncrease",
        54: "NaturalDamageIncrease",
        55: "EtherDamageIncrease",
    }.get(attr_type, f"attr_{attr_type}")


def _alias_key(key: str) -> str:
    return {"costValue": "costvalue"}.get(key, key)


def _format_template_value(value: Any, fmt: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if "%" in fmt:
        decimals = 1 if ".0" in fmt else 0
        return f"{number * 100:.{decimals}f}%"
    if ".0" in fmt:
        return f"{number:.1f}"
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _potential_icon_url(potential_table: dict[str, Any], item: dict[str, Any]) -> str:
    item_ids = list(item.get("itemIds") or [])
    if item_ids:
        item_id = str(item_ids[0] or "")
        if item_id:
            return f"{STATIC_BASE}/itemicon/{item_id}.webp"
    first_item_id = str(potential_table.get("firstItemId") or "")
    if first_item_id:
        return f"{STATIC_BASE}/itemicon/{first_item_id}.webp"
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


def _metric_label_from_key(key: str) -> str:
    return key.replace("_", " ").strip()


def _format_blackboard_value(key: str, value: Any, value_str: Any = "") -> str:
    if value_str:
        return clean_text(value_str)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    if any(token in key for token in ("scale", "rate", "ratio")):
        return f"{number * 100:.0f}%"
    return _format_plain_number(number)


def _format_plain_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(number) < 0.0001:
        return "--"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
