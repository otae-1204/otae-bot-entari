"""Weapons view construction; no I/O or command registration."""

from __future__ import annotations

import re
from typing import (
    Any,
)

from ...providers.assets import (
    item_icon_urls,
    weapon_type_icon_urls,
)
from ..models import (
    WeaponCatalogGroupView,
    WeaponCatalogItemView,
    WeaponCatalogView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponView,
)
from .common import (
    _first_text,
    _first_value,
    _fz_asset_raw_url,
    _fz_overview_entries,
    _fz_rich_text_links,
    _fz_weapon_id,
    _to_int,
    _weapon_name,
    clean_text,
)
from .constants import (
    WEAPON_TYPE_ORDER,
)


def build_fz_weapon_catalog_view(
    raw: dict[str, Any],
    weapon_type_filter: str = "",
) -> WeaponCatalogView:
    article = raw.get("article") or {}
    entries = _fz_overview_entries(raw)
    if not entries:
        raise ValueError("FZ weapon roster does not match the supported catalog schema")

    weapon_type_filter = clean_text(weapon_type_filter)
    grouped: dict[str, list[WeaponCatalogItemView]] = {}
    type_icons: dict[str, str] = {}
    for entry in entries:
        name = _first_text(entry, "name")
        title = _first_text(entry, "title") or (f"武器/{name}" if name else "")
        weapon_type = _first_text(entry, "weaponType") or "未知武器"
        if not name or not title:
            continue
        if weapon_type_filter and weapon_type != weapon_type_filter:
            continue
        type_icon_url = (weapon_type_icon_urls(weapon_type) or ("",))[0]
        grouped.setdefault(weapon_type, []).append(
            WeaponCatalogItemView(
                name=name,
                title=title,
                weapon_id=str(entry.get("weaponId") or ""),
                english_name=_first_text(entry, "nameEn", "englishName"),
                rarity=_to_int(entry.get("rarity")),
                weapon_type=weapon_type,
                max_level=_to_int(_first_value(entry, "maxLv", "maxLevel")),
                max_atk=_to_int(_first_value(entry, "maxAtk", "attack")) or "--",
                icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
                weapon_type_icon_url=type_icon_url,
                substrate_icon_url=_fz_asset_raw_url(_first_text(entry, "substrateIconUrl")),
                terms_main=[clean_text(value) for value in (entry.get("termsMain") or []) if clean_text(value)],
                terms_sub=[clean_text(value) for value in (entry.get("termsSub") or []) if clean_text(value)],
                terms_skill=[clean_text(value) for value in (entry.get("termsSkill") or []) if clean_text(value)],
            )
        )
        type_icons.setdefault(weapon_type, type_icon_url)

    groups: list[WeaponCatalogGroupView] = []
    for weapon_type, items in grouped.items():
        items.sort(key=lambda item: (-item.rarity, -(_to_int(item.max_atk)), item.name))
        groups.append(WeaponCatalogGroupView(weapon_type, type_icons.get(weapon_type, ""), items))
    groups.sort(key=lambda group: (WEAPON_TYPE_ORDER.get(group.name, 99), group.name))
    total_count = sum(len(group.items) for group in groups)
    if weapon_type_filter and not groups:
        raise ValueError(f"FZ weapon type not found: {weapon_type_filter}")
    return WeaponCatalogView(
        title=f"{weapon_type_filter}武器" if weapon_type_filter else "全部武器",
        groups=groups,
        total_count=total_count,
        weapon_type_filter=weapon_type_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_weapon_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> WeaponView:
    article = raw.get("article") or {}
    revision = raw.get("revision") or {}
    content = ((revision.get("contentJson") or {}).get("content") or [{}])[0]
    attrs = content.get("attrs") or {}
    hero = attrs.get("hero") or {}
    stats = attrs.get("stats") or {}
    skills = (attrs.get("skills") or {}).get("skills") or []
    title = _first_text(article, "title")
    name = _first_text(hero, "name") or title.split("/", 1)[-1] or ""
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
        english_name=_first_text(hero, "nameEn"),
        rarity=int(hero.get("rarity") or 0),
        weapon_type=_first_text(hero, "weaponType") or "未知武器",
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

    name = _first_text(meta, "name") or _first_text(item, "name")
    slug = _first_text(meta, "slug") or _weapon_slug(name)
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
        english_name=_first_text(basic, "engName"),
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
    urls = item_icon_urls(icon_id)
    return urls[0] if urls else ""


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
