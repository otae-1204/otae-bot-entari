"""AkeData sprites URL。候选里不再放 Warfarin / FZ。"""

from __future__ import annotations

import re
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit

from ..catalog.models import EffectView, OperatorView, SkillView, WeaponView

AKEDATA_SPRITES_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)
WARFARIN_STATIC_BASE = "https://static.warfarin.wiki/v4"

_T = TypeVar("_T")


def unique_urls(*urls: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))


def is_http_url(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("http://", "https://", "data:"))


def sprite_png(folder: str, stem: str) -> str:
    folder = str(folder or "").strip().strip("/")
    stem = str(stem or "").strip()
    if not folder or not stem:
        return ""
    return f"{AKEDATA_SPRITES_BASE}/{folder}/{stem}.png"


def static_sprite_url(path: str) -> str:
    """把相对路径或 Warfarin 静态地址收成 AkeData PNG；FZ 哈希链原样返回。"""
    text = str(path or "").strip().replace("\\", "/")
    if not text or text.startswith("data:"):
        return text
    if is_http_url(text):
        parts = urlsplit(text)
        host = parts.netloc.lower()
        if "akedata.wiki" in host:
            return text
        if "warfarin.wiki" in host:
            rel = parts.path
            if "/v4/" in rel:
                rel = rel.split("/v4/", 1)[1]
            folder, _, name = rel.strip("/").partition("/")
            stem = name.rsplit(".", 1)[0]
            return sprite_png(folder, stem)
        return text
    folder, _, name = text.partition("/")
    stem = (name or folder).rsplit(".", 1)[0]
    if name:
        return sprite_png(folder, stem)
    return ""


def operator_icon_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.extend(
            (
                sprite_png("charremoteicon", f"icon_{operator_id}"),
                sprite_png("charicon", f"icon_{operator_id}"),
            )
        )
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def operator_round_icon_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.append(sprite_png("charroundicon", f"icon_round_{operator_id}"))
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def operator_portrait_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.append(sprite_png("characterportrait", operator_id))
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def skill_icon_urls(*icon_ids: str) -> tuple[str, ...]:
    urls: list[str] = []
    for item in icon_ids:
        text = str(item or "").strip()
        if not text:
            continue
        key = skill_icon_key(text)
        if key:
            urls.append(sprite_png("skillicon", key))
            continue
        rewritten = static_sprite_url(text)
        if rewritten and "akedata.wiki" in rewritten:
            urls.append(rewritten)
    return unique_urls(*urls)


def skill_icon_key(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("fz_"):
        return ""
    if not is_http_url(text):
        return text
    path = urlsplit(text).path
    if path.endswith("@raw"):
        path = path[: -len("@raw")]
    if "/skillicon/" not in path:
        return ""
    name = path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    return "" if not stem or stem.startswith("fz_") else stem


def item_icon_urls(item_id: str, primary: str = "") -> tuple[str, ...]:
    item_id = str(item_id or "").strip()
    extras: list[str] = []
    if item_id:
        extras.extend(
            (
                sprite_png("itemiconbig", item_id),
                sprite_png("itemicon", item_id),
            )
        )
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def term_icon_urls(primary: str = "") -> tuple[str, ...]:
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        return unique_urls(rewritten)
    return ()


_PROFESSION_ICON_IDS = {
    "近卫": 0,
    "重装": 2,
    "辅助": 4,
    "术师": 5,
    "先锋": 7,
    "突击": 8,
}

_ELEMENT_ICON_STEMS = {
    "物理": "icon_charattrtype_physical",
    "灼热": "icon_charattrtype_fire",
    "电磁": "icon_charattrtype_pulse",
    "寒冷": "icon_charattrtype_cold",
    "自然": "icon_charattrtype_nature",
}


def profession_icon_urls(profession: str, primary: str = "") -> tuple[str, ...]:
    extras: list[str] = []
    profession_id = _PROFESSION_ICON_IDS.get(str(profession or "").strip())
    if profession_id is not None:
        extras.append(sprite_png("charprofessionicon", f"icon_profession_{profession_id}_s"))
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def element_icon_urls(element: str, primary: str = "") -> tuple[str, ...]:
    extras: list[str] = []
    stem = _ELEMENT_ICON_STEMS.get(str(element or "").strip())
    if stem:
        extras.append(sprite_png("elementicon", stem))
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        extras.append(rewritten)
    return unique_urls(*extras)


def weapon_type_icon_urls(weapon_type: str, primary: str = "") -> tuple[str, ...]:
    rewritten = static_sprite_url(primary)
    if rewritten and "akedata.wiki" in rewritten:
        return unique_urls(rewritten)
    return ()


_SKILL_GROUP_CATEGORIES = {
    0: "普攻",
    1: "战技",
    2: "终结技",
    3: "连携技",
}


def apply_akedata_growth_icons(view: OperatorView, growth: dict[str, Any] | None) -> None:
    """用 CharGrowthTable 行里的 icon / iconId 补技能、天赋 sprite id。"""
    if not isinstance(growth, dict):
        return
    _apply_growth_skill_icons(view.skills, growth.get("skillGroupMap"))
    _apply_growth_talent_icons(view.talents, growth.get("talentNodeMap"))


def _apply_growth_skill_icons(skills: list[SkillView], skill_group_map: Any) -> None:
    leftover: list[tuple[str, str]] = []
    if isinstance(skill_group_map, dict):
        for group in skill_group_map.values():
            if not isinstance(group, dict):
                continue
            icon = str(group.get("icon") or "").strip()
            if not icon:
                continue
            leftover.append((_skill_group_category(group.get("skillGroupType")), icon))
    for skill in skills:
        match = _take_match(leftover, lambda item, current=skill: bool(current.category) and item[0] == current.category)
        if match is None:
            continue
        skill.icon_fallbacks = unique_urls(*skill.icon_fallbacks, match[1])


def _apply_growth_talent_icons(talents: list[EffectView], talent_node_map: Any) -> None:
    by_slot: dict[int, str] = {}
    if isinstance(talent_node_map, dict):
        for key, node in talent_node_map.items():
            if not isinstance(node, dict):
                continue
            info = node.get("passiveSkillNodeInfo")
            if not isinstance(info, dict):
                continue
            icon = str(info.get("iconId") or "").strip()
            if not icon:
                continue
            slot = _talent_slot_from_id(str(info.get("talentEffectId") or key))
            if slot is None:
                continue
            by_slot.setdefault(slot, icon)
    leftover = [by_slot[slot] for slot in sorted(by_slot)]
    for talent in talents:
        icon = ""
        slot = _talent_slot_from_title(talent.title)
        if slot is not None:
            icon = by_slot.get(slot, "")
            if icon in leftover:
                leftover.remove(icon)
        if not icon and leftover:
            icon = leftover.pop(0)
        if icon:
            talent.icon_fallbacks = unique_urls(*talent.icon_fallbacks, icon)


def _skill_group_category(group_type: Any) -> str:
    try:
        value = int(group_type)
    except (TypeError, ValueError):
        return ""
    return _SKILL_GROUP_CATEGORIES.get(value, "")


def _talent_slot_from_id(value: str) -> int | None:
    match = re.search(r"_talent_(\d+)_", value)
    if match:
        return int(match.group(1))
    match = re.search(r"_passive_skill_(\d+)_", value)
    if match:
        return int(match.group(1)) + 1
    return None


def _talent_slot_from_title(title: str) -> int | None:
    text = str(title or "").strip()
    if len(text) >= 2 and text[0] in {"T", "t"} and text[1].isdigit():
        return int(text[1])
    return None


def apply_operator_asset_donor(primary: OperatorView, donor: OperatorView) -> None:
    if not primary.operator_id:
        primary.operator_id = donor.operator_id
    if not primary.icon_url:
        primary.icon_url = donor.icon_url
    if not primary.round_icon_url:
        primary.round_icon_url = donor.round_icon_url
    if not primary.portrait_url:
        primary.portrait_url = donor.portrait_url
    _donate_skill_icons(primary.skills, donor.skills)
    _donate_effect_icons(primary.talents, donor.talents)
    _donate_effect_icons(primary.potentials, donor.potentials)


def apply_weapon_asset_donor(primary: WeaponView, donor: WeaponView) -> None:
    if not primary.weapon_id:
        primary.weapon_id = donor.weapon_id
    if not primary.icon_url:
        primary.icon_url = donor.icon_url


def _donate_skill_icons(primary: list[SkillView], donors: list[SkillView]) -> None:
    leftover = list(donors)
    for skill in primary:
        match = _take_match(leftover, lambda item, current=skill: item.title == current.title)
        if match is None:
            match = _take_match(
                leftover,
                lambda item, current=skill: bool(current.category) and item.category == current.category,
            )
        if match is None:
            continue
        skill.icon_fallbacks = unique_urls(*skill.icon_fallbacks, match.icon_id, *match.icon_fallbacks)


def _donate_effect_icons(primary: list[EffectView], donors: list[EffectView]) -> None:
    leftover = list(donors)
    for effect in primary:
        match = _take_match(
            leftover,
            lambda item, current=effect: _effect_title_key(item) == _effect_title_key(current),
        )
        if match is None:
            continue
        effect.icon_fallbacks = unique_urls(
            *effect.icon_fallbacks,
            skill_icon_key(match.icon_url),
            match.icon_url,
            *match.icon_fallbacks,
        )


def _effect_title_key(effect: EffectView) -> str:
    title = str(effect.title or "").strip()
    if title[:1] in {"T", "P"} and len(title) > 2 and title[1].isdigit():
        title = title.split(" ", 1)[-1]
    return title


def _take_match(items: list[_T], predicate: Callable[[_T], bool]) -> _T | None:
    for index, item in enumerate(items):
        if predicate(item):
            return items.pop(index)
    return None


def operator_needs_asset_donor(view: OperatorView) -> bool:
    if not view.operator_id or not view.round_icon_url:
        return True
    if any(not skill_icon_key(skill.icon_id) and not skill.icon_fallbacks for skill in view.skills):
        return True
    if any(not skill_icon_key(effect.icon_url) and not effect.icon_fallbacks for effect in view.talents):
        return True
    return False


def weapon_needs_asset_donor(view: WeaponView) -> bool:
    return not view.weapon_id
