from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .client import WarfarinAPIError, WarfarinClient
from .commands import AMBIGUITY_MARGIN, CLEAR_SCORE, score_candidate
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
from .sources import source_order


STATIC_BASE = "https://static.warfarin.wiki/v4"
FZ_ASSET_HOST = "assets.fz.wiki"
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
        for source in source_order("operator"):
            try:
                if source == "fz":
                    view = await self.get_operator_view_from_fz(query)
                elif source == "warfarin":
                    view = await self.get_operator_view_from_warfarin(query)
                else:
                    continue
            except (WarfarinAPIError, ValueError, KeyError, TypeError):
                continue
            if view is not None:
                return view
        return None

    async def get_operator_view_from_warfarin(self, query: str) -> OperatorView | None:
        query = _strip_title_prefix(query, "干员/")
        slug = await self.find_operator_slug(query)
        if not slug:
            return None
        raw = await self.client.operator_detail(slug)
        return build_operator_view(raw)

    async def get_operator_view_from_fz(self, query: str) -> OperatorView | None:
        title = await self.find_fz_operator_title(query)
        if not title:
            return None
        raw, richtext = await _fz_article_and_richtext(self.client, title)
        return build_fz_operator_view(raw, richtext)

    async def get_weapon_view(self, query: str) -> WeaponView | None:
        for source in source_order("weapon"):
            try:
                if source == "fz":
                    view = await self.get_weapon_view_from_fz(query)
                elif source == "warfarin":
                    view = await self.get_weapon_view_from_warfarin(query)
                else:
                    continue
            except (WarfarinAPIError, ValueError, KeyError, TypeError):
                continue
            if view is not None:
                return view
        return None

    async def get_weapon_view_from_fz(self, query: str) -> WeaponView | None:
        title = await self.find_weapon_title(query)
        if not title:
            return None
        raw, richtext = await _fz_article_and_richtext(self.client, title)
        view = build_weapon_view(raw, richtext)
        view.operator_names = await self.find_weapon_operator_names(view)
        return view

    async def get_weapon_view_from_warfarin(self, query: str) -> WeaponView | None:
        query = _strip_title_prefix(query, "武器/")
        slug = await self.find_weapon_slug(query)
        if not slug:
            return None
        raw = await self.client.weapon_detail(slug)
        view = build_warfarin_weapon_view(raw)
        view.operator_names = await self.find_weapon_operator_names(view)
        return view

    async def find_weapon_operator_names(self, view: WeaponView) -> list[str]:
        try:
            weapons_data, operators_data = await asyncio.gather(
                self.client.weapons(),
                self.client.operators(),
            )
            weapon = _match_weapon_record(view, weapons_data.get("data") or [])
            weapon_id = str((weapon or {}).get("id") or view.weapon_id).strip()
            weapon_type = str((weapon or {}).get("weaponType") or "").strip()
            if not weapon_id or not weapon_type:
                return []
            candidates = [
                item
                for item in operators_data.get("data") or []
                if str(item.get("weaponType") or "").strip() == weapon_type and item.get("slug")
            ]
            details = await asyncio.gather(
                *(self.client.operator_detail(str(item["slug"])) for item in candidates),
                return_exceptions=True,
            )
        except Exception:
            return []

        default_names: list[str] = []
        recommended_names: list[str] = []
        for item, detail in zip(candidates, details):
            if isinstance(detail, Exception) or not isinstance(detail, dict):
                continue
            data = detail.get("data") or {}
            character = data.get("characterTable") or {}
            recommendations = data.get("charWpnRecommendTable") or {}
            name = str((detail.get("meta") or {}).get("name") or item.get("name") or "").strip()
            if not name:
                continue
            if str(character.get("defaultWeaponId") or "").strip() == weapon_id:
                default_names.append(name)
                continue
            recommended_ids = {
                str(candidate_id).strip()
                for key, values in recommendations.items()
                if str(key).startswith("weaponIds") and isinstance(values, list)
                for candidate_id in values
            }
            if weapon_id in recommended_ids:
                recommended_names.append(name)
        return _unique_names(default_names or recommended_names)

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

    async def find_fz_operator_title(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query.startswith("干员/"):
            return query
        exact_title = f"干员/{query}"
        try:
            summaries = await self.client.fz_article_summaries("干员/")
        except WarfarinAPIError:
            summaries = {}
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
        try:
            search_data = await self.client.fz_search(query)
        except WarfarinAPIError:
            search_data = {}
        for item in search_data.get("hits") or []:
            title = str(item.get("title") or "")
            if title.startswith("干员/"):
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
        return _best_slug_match(query, data.get("data") or [])

    async def find_weapon_slug(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I):
            return query
        data = await self.client.search(query)
        for item in data.get("results") or []:
            if str(item.get("type") or "") in {"weapons", "weapon"} and item.get("slug"):
                return str(item["slug"])
        return await self._match_weapon_by_name(query)

    async def _match_weapon_by_name(self, query: str) -> str | None:
        data = await self.client.weapons()
        return _best_slug_match(query, data.get("data") or [])


async def _fz_article_and_richtext(client: WarfarinClient, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    article_result, richtext_result = await asyncio.gather(
        client.fz_article_by_title(title),
        client.fz_game_richtext(),
        return_exceptions=True,
    )
    if isinstance(article_result, Exception):
        raise article_result
    if isinstance(richtext_result, Exception):
        richtext = {}
    else:
        richtext = richtext_result
    return article_result, richtext


def _best_slug_match(query: str, records: list[dict[str, Any]]) -> str | None:
    scored: list[tuple[int, str]] = []
    for record in records:
        slug = str(record.get("slug") or "").strip()
        name = str(record.get("name") or "").strip()
        if not slug or not name:
            continue
        score = score_candidate(query, name, slug)
        if score >= CLEAR_SCORE:
            scored.append((score, slug))
    scored.sort(reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN:
        return None
    return scored[0][1]


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


def build_fz_operator_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> OperatorView:
    article = raw.get("article") or {}
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    skills = _build_fz_operator_skills(attrs.get("skills"))
    if not hero or not skills:
        raise ValueError("FZ operator article does not match the supported card schema")

    title = str(article.get("title") or "")
    name = _first_text(hero, "name", "nameCn", "cnName", "title") or title.split("/", 1)[-1]
    if not name:
        raise ValueError("FZ operator article is missing name")

    rarity = _to_int(_first_value(hero, "rarity", "star", "stars"))
    species_label, species_value = _fz_species_info(attrs)
    return OperatorView(
        name=name,
        slug=title or name,
        operator_id=str(_first_value(hero, "id", "charId", "operatorId") or ""),
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


def _fz_template_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        if isinstance(attrs, dict) and isinstance(attrs.get("hero"), dict):
            return attrs
    return {}


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
        match = re.search(r"【种族】\s*([^【\n\r]+)", clean_text(text))
        if match:
            species = match.group(1).strip()
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


def _fz_hero_meta_value(hero: dict[str, Any], *labels: str) -> str:
    wanted = {label.strip().lower() for label in labels if label.strip()}
    for item in hero.get("meta") or []:
        if not isinstance(item, dict):
            continue
        label = clean_text(_first_value(item, "label", "name", "title", "key")).strip().lower()
        if label in wanted:
            value = clean_text(_first_value(item, "value", "text", "content"))
            if value:
                return value
    return ""


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
        param_values = _fz_param_table_values(item.get("paramTable"), selected_positions)
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
                levels=levels,
            )
        )
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
                label=f"Lv{level}",
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
    if any("倍率" in name and name != "攻击倍率" for name in param_row):
        mapped_values.pop("攻击倍率", None)
    if any("失衡值" in name and name != "失衡值" for name in param_row):
        mapped_values.pop("失衡值", None)
    if any(("技力" in name or "终结技能量" in name) and name != "技力" for name in param_row):
        mapped_values.pop("技力", None)


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


def _ordered_fz_levels(levels: list[Any]) -> list[dict[str, Any]]:
    records = [item for item in levels if isinstance(item, dict)]
    records.sort(key=lambda item: _to_int(_first_value(item, "level", "lv")))
    return records


def _fz_param_table_values(raw: Any, selected_positions: list[int]) -> list[dict[str, str]]:
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
    add("冷却", "cooldown", "CoolDown")
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


def _format_fz_template(desc: Any, values: Any) -> str:
    value_map = _normalized_value_map(values if isinstance(values, dict) else {})

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key_expr, _, fmt = expr.partition(":")
        value = _eval_fz_template_expr(key_expr.strip(), value_map)
        return _format_template_value(value, fmt)

    return _clean_fz_rich_text(re.sub(r"\{([^{}]+)\}", replace, str(desc or "")))


def _clean_fz_rich_text(value: Any) -> str:
    text = str(value or "")
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(r"</>|<[@#][A-Za-z0-9_.-]+>", protect, text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(
        r"\x00(\d+)\x00",
        lambda match: protected[int(match.group(1))],
        text,
    )
    text = text.replace("\\n", "\n")
    return " ".join(text.split())


def _normalized_value_map(values: dict[str, Any]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for key, value in values.items():
        number = _to_float(value)
        if number is None:
            continue
        normalized[str(key).strip().lower()] = number
    return normalized


def _eval_fz_template_expr(expr: str, values: dict[str, float]) -> float | None:
    expr = expr.strip().lower()
    if not expr:
        return None
    match = re.fullmatch(
        r"(-?\d+(?:\.\d+)?|[a-z0-9_]+)([+\-*/])(-?\d+(?:\.\d+)?|[a-z0-9_]+)",
        expr,
    )
    if match:
        left = _fz_template_operand(match.group(1), values)
        right = _fz_template_operand(match.group(3), values)
        if left is None or right is None:
            return None
        operator = match.group(2)
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        return None if right == 0 else left / right
    value = values.get(expr)
    if value is not None:
        return value
    return values.get(_alias_key(expr).lower())


def _fz_template_operand(operand: str, values: dict[str, float]) -> float | None:
    try:
        return float(operand)
    except ValueError:
        value = values.get(operand)
        if value is not None:
            return value
        return values.get(_alias_key(operand).lower())


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, str) and value.strip().endswith("%"):
            return float(value.strip().removesuffix("%")) / 100
        return float(value)
    except (TypeError, ValueError):
        return None


def _case_insensitive_get(values: dict[str, Any], key: str) -> Any:
    if key in values:
        return values[key]
    lowered = key.lower()
    for raw_key, value in values.items():
        if str(raw_key).lower() == lowered:
            return value
    return None


def _fz_effect_title(kind: str, title: str, level: int, index: int) -> str:
    if kind == "potential":
        return f"P{level or index} {title or '潜能'}"
    return title or f"天赋 {index}"


def _unwrap_fz_list(raw: Any, *keys: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_text(data: dict[str, Any], *keys: str) -> str:
    value = _first_value(data, *keys)
    if isinstance(value, dict):
        value = _first_value(value, "name", "text", "value", "url")
    return str(value or "").strip()


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，/、\s]+", value) if item.strip()]
    return []


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _level_label(level: int) -> str:
    for expected, label in LEVEL_COLUMNS:
        if level == expected:
            return label
    return f"Lv{level}"


def _strip_title_prefix(query: str, prefix: str) -> str:
    query = str(query or "").strip()
    if query.startswith(prefix):
        return query[len(prefix):]
    return query


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
        weapon_id=_fz_weapon_id(skills),
        source_name="api.fz.wiki",
        english_name=str(hero.get("nameEn") or ""),
        rarity=int(hero.get("rarity") or 0),
        weapon_type=str(hero.get("weaponType") or "未知武器"),
        max_level=max_level,
        max_atk=max_atk,
        icon_url=_fz_asset_raw_url(hero.get("iconUrl")),
        skills=[_build_weapon_skill(skill) for skill in skills],
        rich_text_styles=richtext.get("RICH_TEXT_STYLES") or {},
        rich_text_links=_fz_rich_text_links(richtext),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_warfarin_weapon_view(raw: dict[str, Any]) -> WeaponView:
    meta = raw.get("meta") or {}
    data = raw.get("data") or {}
    refs = raw.get("refs") or {}
    basic = data.get("weaponBasicTable") or {}
    item = data.get("itemTable") or {}
    upgrade = data.get("weaponUpgradeTemplateTable") or {}
    skill_table = data.get("skillPatchTable") or {}

    name = str(meta.get("name") or item.get("name") or "").strip()
    slug = str(meta.get("slug") or "").strip() or _weapon_slug(name)
    max_level = int(basic.get("maxLv") or 0)
    max_atk = _warfarin_weapon_max_atk(upgrade.get("list") or [], max_level)
    weapon_type_id = str(basic.get("weaponType") or "")
    weapon_type = str((refs.get("weaponTypes") or {}).get(weapon_type_id) or _weapon_name(basic.get("weaponType")))

    return WeaponView(
        name=name,
        slug=slug,
        title=f"Warfarin/{slug}",
        weapon_id=str(basic.get("weaponId") or meta.get("id") or ""),
        source_name="Warfarin Wiki",
        english_name=str(basic.get("engName") or ""),
        rarity=int(basic.get("rarity") or item.get("rarity") or 0),
        weapon_type=weapon_type,
        max_level=max_level,
        max_atk=max_atk,
        icon_url=_warfarin_weapon_icon_url(str(item.get("iconId") or basic.get("weaponId") or meta.get("id") or "")),
        skills=_build_warfarin_weapon_skills(basic.get("weaponSkillList") or [], skill_table),
        rich_text_styles=_warfarin_rich_text_styles(refs.get("richTextStyleTable") or {}),
        rich_text_links=refs.get("hyperlinkTextTable") or {},
        source_version=str(meta.get("version") or ""),
    )


def _warfarin_weapon_max_atk(rows: list[dict[str, Any]], max_level: int) -> int | str:
    if not rows:
        return "--"
    for row in rows:
        if int(row.get("weaponLv") or 0) == max_level and row.get("baseAtk") is not None:
            return int(row["baseAtk"])
    for row in reversed(rows):
        if row.get("baseAtk") is not None:
            return int(row["baseAtk"])
    return "--"


def _build_warfarin_weapon_skills(skill_ids: list[Any], skill_table: dict[str, Any]) -> list[WeaponSkillView]:
    skills: list[WeaponSkillView] = []
    ordered_ids = [str(skill_id) for skill_id in skill_ids]
    if not ordered_ids:
        ordered_ids = list(skill_table)
    for skill_id in ordered_ids:
        bundle = (skill_table.get(skill_id) or {}).get("SkillPatchDataBundle") or []
        if not bundle:
            continue
        first = bundle[0]
        skills.append(
            WeaponSkillView(
                title=clean_text(first.get("skillName")) or "技能",
                description=str(first.get("description") or ""),
                levels=[
                    WeaponSkillLevelView(
                        level=int(item.get("level") or index + 1),
                        values=_blackboard_values(item.get("blackboard") or []),
                    )
                    for index, item in enumerate(bundle)
                ],
            )
        )
    return skills


def _blackboard_values(rows: list[dict[str, Any]]) -> dict[str, float | int | str]:
    values: dict[str, float | int | str] = {}
    for row in rows:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        value = row.get("valueStr")
        if value in (None, ""):
            value = row.get("value")
        values[key] = value
    return values


def _warfarin_rich_text_styles(raw: dict[str, Any]) -> dict[str, dict]:
    styles: dict[str, dict] = {}
    for key, item in raw.items():
        style: dict[str, str] = {"id": str(item.get("id") or key)}
        pre_defs = item.get("preDef") or []
        pre = str(pre_defs[0] if pre_defs else "")
        color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
        if color_match:
            style["color"] = f"#{color_match.group(1)}"
        styles[str(key)] = style
    return styles


def _warfarin_weapon_icon_url(icon_id: str) -> str:
    return f"{STATIC_BASE}/itemicon/{icon_id}.webp" if icon_id else ""


def _fz_weapon_id(skills: list[dict[str, Any]]) -> str:
    for skill in reversed(skills):
        skill_id = str(skill.get("skillId") or skill.get("id") or "")
        match = re.fullmatch(r"sk_(wpn_[a-z0-9_]+)", skill_id, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _match_weapon_record(view: WeaponView, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if view.weapon_id:
        for record in records:
            if str(record.get("id") or "").strip() == view.weapon_id:
                return record
    target_name = clean_text(view.name).casefold()
    if not target_name:
        return None
    for record in records:
        if clean_text(record.get("name")).casefold() == target_name:
            return record
    return None


def _unique_names(names: list[str]) -> list[str]:
    return list(dict.fromkeys(name for name in names if name))


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


def _fz_asset_raw_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text or text.startswith("data:"):
        return text
    parts = urlsplit(text)
    if parts.netloc.lower() != FZ_ASSET_HOST:
        return text
    if parts.path.endswith("@raw"):
        return text
    return urlunsplit((parts.scheme, parts.netloc, f"{parts.path}@raw", parts.query, parts.fragment))


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


def _build_fz_term_styles(richtext: dict[str, Any]) -> dict[str, TermStyleView]:
    rich_text_table = richtext.get("RICH_TEXT_STYLES") or {}
    hyperlink_table = richtext.get("HYPERLINK_TEXTS") or {}
    result: dict[str, TermStyleView] = {}
    for tag_id, entry in rich_text_table.items():
        if not str(tag_id).startswith("ba."):
            continue
        color = _fz_rich_text_color(entry if isinstance(entry, dict) else {})
        if color:
            result[str(tag_id)] = TermStyleView(term=str(tag_id), color=color, icon_url="")
    for tag_id, entry in hyperlink_table.items():
        if not str(tag_id).startswith("ba.") or not isinstance(entry, dict):
            continue
        rich_text_id = str(entry.get("richTextId") or tag_id)
        color = _fz_rich_text_color(rich_text_table.get(rich_text_id) or rich_text_table.get(str(tag_id)) or {})
        icon_path = str(entry.get("iconPath") or "").strip()
        icon_url = _fz_asset_raw_url(static_resource_url(icon_path)) if icon_path else ""
        style = TermStyleView(term=str(tag_id), color=color, icon_url=icon_url)
        result[str(tag_id)] = style
        name = clean_text(entry.get("name") or entry.get("text") or entry.get("title"))
        if name:
            result[name] = TermStyleView(term=name, color=color, icon_url=icon_url)
    return result


def _fz_rich_text_links(richtext: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag_id, entry in (richtext.get("HYPERLINK_TEXTS") or {}).items():
        if not isinstance(entry, dict):
            result[tag_id] = entry
            continue
        copied = dict(entry)
        icon_path = str(copied.get("iconPath") or "").strip()
        if icon_path:
            copied["iconPath"] = _fz_asset_raw_url(static_resource_url(icon_path))
        result[tag_id] = copied
    return result


def _fz_rich_text_color(entry: dict[str, Any]) -> str:
    color = str(entry.get("color") or "").strip()
    if color:
        return color
    pre_defs = entry.get("preDef") or []
    pre = str(pre_defs[0] if pre_defs else "")
    color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
    return f"#{color_match.group(1)}" if color_match else ""


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
