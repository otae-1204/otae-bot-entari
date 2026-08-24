from __future__ import annotations

import re
from typing import Any, Mapping

from .account_detail_models import (
    AccountDetailView,
    AccountEquipView,
    AccountOperatorView,
    AccountSkillView,
    AccountStatView,
    AccountWeaponView,
)
from .account_i18n import localized_text, semantic_key, semantic_label, server_label
from .account_detail_names import AccountDetailNameMap
from .gacha import format_timestamp


EQUIP_SLOTS: tuple[tuple[str, str], ...] = (
    ("bodyEquip", "护甲"),
    ("armEquip", "护手"),
    ("firstAccessory", "配件Ⅰ"),
    ("secondAccessory", "配件Ⅱ"),
)

ELEMENT_COLORS: dict[str, str] = {
    "char_property_physical": "#a8865c",
    "char_property_fire": "#d4542a",
    "char_property_cryst": "#2f86c4",
    "char_property_pulse": "#f0cf2f",
    "char_property_natural": "#3f9a52",
    "char_property_alien": "#b23f7c",
}

SKILL_PROPERTY_COLORS: dict[str, str] = {
    "skill_property_physical": "#969a99",
    "skill_property_fire": "#ec654d",
    "skill_property_cryst": "#2bcbd8",
    "skill_property_pulse": "#f0cf2f",
    "skill_property_natural": "#77c92f",
    "skill_property_alien": "#b23f7c",
}

ULTIMATE_SKILL_TYPE = "skill_type_ultimate_skill"

def build_account_detail_view(
    detail: Mapping[str, Any],
    *,
    uid: str,
    nickname: str = "",
    server_name: str = "",
    currency_balances: Mapping[int, Any] | None = None,
    name_map: AccountDetailNameMap | None = None,
) -> AccountDetailView:
    """Map a Skland ``data.detail`` payload onto the render view.

    Every tolerance for missing or oddly typed fields lives here; the renderer
    trusts the view. ``uid`` arrives already masked or unmasked by the caller.
    """
    detail = detail or {}
    base = _mapping(detail.get("base"))
    return AccountDetailView(
        nickname=_text(base.get("name")) or nickname or "未知管理员",
        uid=uid,
        server_name=server_label(server_name or _text(base.get("serverName"))),
        level=_int_or_none(base.get("level")),
        world_level=_int_or_none(base.get("worldLevel")),
        main_mission=_text(_mapping(base.get("mainMission")).get("description")),
        avatar_url=_text(base.get("avatarUrl")),
        saved_at=format_timestamp(_int_or_none(base.get("saveTime")) or 0),
        stats=_build_stats(detail, base, currency_balances or {}),
        operators=_build_operators(detail, name_map or AccountDetailNameMap()),
    )


def _build_stats(
    detail: Mapping[str, Any],
    base: Mapping[str, Any],
    currency_balances: Mapping[int, Any],
) -> tuple[AccountStatView, ...]:
    dungeon = _mapping(detail.get("dungeon"))
    bp_system = _mapping(detail.get("bpSystem"))
    daily = _mapping(detail.get("dailyMission"))
    weekly = _mapping(detail.get("weeklyMission"))
    return (
        AccountStatView(
            "理智",
            _ratio(dungeon.get("curStamina"), dungeon.get("maxStamina")),
            _stamina_note(dungeon, detail.get("currentTs")),
        ),
        AccountStatView(
            "日常",
            _ratio(daily.get("dailyActivation"), daily.get("maxDailyActivation")),
            "活跃度",
        ),
        AccountStatView("周常", _ratio(weekly.get("score"), weekly.get("total")), "分数"),
        AccountStatView("通行证", _ratio(bp_system.get("curLevel"), bp_system.get("maxLevel")), "等级"),
        AccountStatView("嵌晶玉", _count(currency_balances.get(2)), "当前持有"),
        AccountStatView("源石", _count(currency_balances.get(1)), "当前持有"),
        AccountStatView("武库配额", _count(currency_balances.get(3)), "当前持有"),
    )


def _build_operators(
    detail: Mapping[str, Any], name_map: AccountDetailNameMap
) -> tuple[AccountOperatorView, ...]:
    operators = []
    for character in _sequence(detail.get("chars")):
        if not isinstance(character, Mapping):
            continue
        raw = _mapping(character)
        operators.append((_character_id(raw), _build_operator(raw, name_map)))
    operators.sort(
        key=lambda item: (
            -item[1].rarity,
            -(item[1].level or 0),
            _character_id_sort_key(item[0]),
        )
    )
    return tuple(operator for _, operator in operators)


def _character_id(character: Mapping[str, Any]) -> str:
    char_data = _mapping(character.get("charData"))
    return _text(char_data.get("id") or character.get("charId") or character.get("id"))


def _character_id_sort_key(character_id: str) -> tuple[int, int, str]:
    match = re.search(r"(?:^|_)(\d+)(?:_|$)", character_id)
    if match:
        return (0, int(match.group(1)), character_id.casefold())
    return (1, 0, character_id.casefold())


def _build_operator(
    character: Mapping[str, Any], name_map: AccountDetailNameMap
) -> AccountOperatorView:
    char_data = _mapping(character.get("charData"))
    element_key = _semantic_key(char_data.get("property"))
    tactical = _mapping(character.get("tacticalItem"))
    tactical_data = _mapping(tactical.get("tacticalItemData"))
    return AccountOperatorView(
        name=_mapped_name(
            name_map.character_names,
            (char_data.get("id"), character.get("charId"), character.get("id")),
            char_data.get("name"),
        ) or "未知干员",
        rarity=_int_or_none(_semantic_value(char_data.get("rarity"))) or 0,
        level=_int_or_none(character.get("level")),
        evolve_phase=_int_or_none(character.get("evolvePhase")),
        potential_level=_int_or_none(character.get("potentialLevel")),
        profession=semantic_label(char_data.get("profession")),
        element=semantic_label(char_data.get("property")),
        element_color=ELEMENT_COLORS.get(element_key, "#888888"),
        weapon_type=semantic_label(char_data.get("weaponType")),
        portrait_url=_text(char_data.get("avatarSqUrl")),
        skills=_build_skills(character, char_data, name_map),
        weapon=_build_weapon(character.get("weapon"), name_map),
        equips=_build_equips(character, name_map),
        tactical_name=_mapped_name(
            name_map.item_names,
            (tactical_data.get("id"), tactical.get("id"), tactical.get("itemId")),
            tactical_data.get("name"),
        ),
        tactical_icon_url=_text(
            tactical_data.get("iconUrl")
        ),
    )


def _build_skills(
    character: Mapping[str, Any], char_data: Mapping[str, Any], name_map: AccountDetailNameMap
) -> tuple[AccountSkillView, ...]:
    user_skills = _mapping(character.get("userSkills"))
    skills = []
    for entry in _sequence(char_data.get("skills")):
        skill = _mapping(entry)
        learned = _mapping(user_skills.get(_text(skill.get("id"))))
        property_key = _semantic_key(skill.get("property"))
        type_key = _semantic_key(skill.get("type"))
        skills.append(
            AccountSkillView(
                name=_mapped_name(
                    name_map.skill_names,
                    (skill.get("id"), skill.get("skillId")),
                    skill.get("name"),
                ),
                icon_url=_text(skill.get("iconUrl")),
                level=_int_or_none(learned.get("level")),
                max_level=_int_or_none(learned.get("maxLevel")),
                type_label=semantic_label(skill.get("type")),
                damage_type=semantic_label(skill.get("property")),
                damage_color=SKILL_PROPERTY_COLORS.get(property_key, "#969a99"),
                is_ultimate=type_key == ULTIMATE_SKILL_TYPE,
            )
        )
    return tuple(skills)


def _build_weapon(value: Any, name_map: AccountDetailNameMap) -> AccountWeaponView | None:
    weapon = _mapping(value)
    weapon_data = _mapping(weapon.get("weaponData"))
    if not weapon or not weapon_data:
        return None
    potential_level = _int_or_none(weapon.get("refineLevel"))
    gem_data = _mapping(_mapping(weapon.get("gem")).get("gemData"))
    return AccountWeaponView(
        name=_mapped_name(
            name_map.weapon_names,
            (weapon_data.get("id"), weapon_data.get("weaponId"), weapon.get("id"), weapon.get("weaponId")),
            weapon_data.get("name"),
        ),
        icon_url=_text(weapon_data.get("iconUrl")),
        rarity=_int_or_none(_semantic_value(weapon_data.get("rarity"))) or 0,
        level=_int_or_none(weapon.get("level")),
        potential_level=potential_level,
        breakthrough_level=_int_or_none(weapon.get("breakthroughLevel")),
        type_label=semantic_label(weapon_data.get("type")),
        gem_name=_mapped_name(
            name_map.item_names,
            (gem_data.get("id"), weapon.get("gemId")),
            gem_data.get("name"),
        ),
        gem_icon_url=_text(gem_data.get("icon")),
    )


def _build_equips(
    character: Mapping[str, Any], name_map: AccountDetailNameMap
) -> tuple[AccountEquipView | None, ...]:
    """Return exactly four positional slots; ``None`` marks an empty slot."""
    slots: list[AccountEquipView | None] = []
    for key, label in EQUIP_SLOTS:
        equip_data = _mapping(_mapping(character.get(key)).get("equipData"))
        if not equip_data:
            slots.append(None)
            continue
        slots.append(
            AccountEquipView(
                slot_label=label,
                name=_mapped_name(
                    name_map.item_names,
                    (
                        equip_data.get("id"),
                        equip_data.get("itemId"),
                        _mapping(character.get(key)).get("equipId"),
                    ),
                    equip_data.get("name"),
                ),
                icon_url=_text(equip_data.get("iconUrl")),
                rarity=_equip_rarity(equip_data.get("rarity")),
                type_label=semantic_label(equip_data.get("type")),
                level_label=_semantic_value(equip_data.get("level")),
                suit_name=_mapped_name(
                    name_map.suit_names,
                    (
                        _mapping(equip_data.get("suit")).get("id"),
                        equip_data.get("suitID"),
                    ),
                    _mapping(equip_data.get("suit")).get("name"),
                ),
            )
        )
    return tuple(slots)


def _mapped_name(
    names: Mapping[str, str], identifiers: tuple[Any, ...], fallback: Any
) -> str:
    for identifier in identifiers:
        key = _text(identifier)
        if key:
            mapped = _text(names.get(key))
            if mapped:
                return mapped
    return _text(fallback)


def _equip_rarity(value: Any) -> int:
    key = _semantic_key(value)
    _, _, tail = key.rpartition("_")
    return _int_or_none(tail) or 0


def _stamina_note(dungeon: Mapping[str, Any], current_ts: Any) -> str:
    full_at = _int_or_none(dungeon.get("maxTs"))
    now = _int_or_none(current_ts)
    if not full_at or not now or full_at <= now:
        return "已回满"
    minutes = (full_at - now) // 60
    return f"{minutes // 60} 小时 {minutes % 60} 分回满"


def _semantic_value(value: Any, default: str = "") -> str:
    """Unwrap the ``{"key": ..., "value": ...}`` envelope used across the payload."""
    if isinstance(value, Mapping):
        return _text(value.get("value")) or default
    return _text(value) or default


def _semantic_key(value: Any) -> str:
    return semantic_key(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _text(value: Any) -> str:
    return localized_text(value)


def _int_or_none(value: Any) -> int | None:
    """Parse an int without inventing a zero for missing data."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _count(value: Any) -> str:
    parsed = _int_or_none(value)
    return "--" if parsed is None else str(parsed)


def _ratio(current: Any, total: Any) -> str:
    left = _int_or_none(current)
    right = _int_or_none(total)
    if left is None and right is None:
        return "--"
    if right is None:
        return str(left)
    return f"{'--' if left is None else left} / {right}"
