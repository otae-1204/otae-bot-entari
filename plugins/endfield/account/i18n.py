from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# The account APIs return these labels as a ``{key, value}`` envelope.  The
# value is not guaranteed to use the CN locale, so the stable key is the
# source of truth for the account cards.
SEMANTIC_LABELS: dict[str, str] = {
    "profession_vanguard": "先锋",
    "profession_guard": "近卫",
    "profession_defender": "重装",
    "profession_caster": "术师",
    "profession_supporter": "辅助",
    "profession_assault": "突击",
    "char_property_physical": "物理",
    "char_property_fire": "灼热",
    "char_property_cryst": "寒冷",
    "char_property_pulse": "电磁",
    "char_property_natural": "自然",
    "char_property_alien": "异质",
    "skill_property_physical": "物理",
    "skill_property_fire": "灼热",
    "skill_property_cryst": "寒冷",
    "skill_property_pulse": "电磁",
    "skill_property_natural": "自然",
    "skill_property_alien": "异质",
    "skill_type_normal_attack": "普通攻击",
    "skill_type_normal_skill": "战技",
    "skill_type_combo_skill": "连携技",
    "skill_type_ultimate_skill": "终结技",
    "weapon_type_sword": "单手剑",
    "weapon_type_claymores": "双手剑",
    "weapon_type_pistol": "手铳",
    "weapon_type_lance": "长柄武器",
    "weapon_type_wand": "施术单元",
    "equip_type_body": "护甲",
    "equip_type_hand": "护手",
    "equip_type_edc": "配件",
}

# Older/provider-specific payloads sometimes omit ``key`` and only return the
# English display value.  These aliases keep those payloads localized too.
_VALUE_LABELS: dict[str, str] = {
    "vanguard": "先锋",
    "guard": "近卫",
    "defender": "重装",
    "caster": "术师",
    "supporter": "辅助",
    "assault": "突击",
    "physical": "物理",
    "fire": "灼热",
    "cryst": "寒冷",
    "pulse": "电磁",
    "natural": "自然",
    "alien": "异质",
    "normal attack": "普通攻击",
    "normal skill": "战技",
    "combo skill": "连携技",
    "ultimate skill": "终结技",
    "sword": "单手剑",
    "claymores": "双手剑",
    "pistol": "手铳",
    "lance": "长柄武器",
    "wand": "施术单元",
    "body": "护甲",
    "hand": "护手",
    "edc": "配件",
}

_SERVER_LABELS: dict[str, str] = {
    "1": "国服",
    "cn": "国服",
    "china": "国服",
    "official": "国服",
    "official_cn": "国服",
    "国服": "国服",
    "2": "亚服",
    "asia": "亚服",
    "asia server": "亚服",
    "skport": "亚服",
    "gryphline": "亚服",
    "亚服": "亚服",
}

_LOCALE_KEYS = ("zh-CN", "zh_cn", "zhCN", "zh", "cn", "chs", "简体中文")


def localized_text(
    value: Any,
    translations: Mapping[str, Any] | None = None,
    default: str = "",
) -> str:
    """Return the best available Chinese text from a mixed API text value.

    Endfield data uses several shapes for the same field: a bare string, a
    ``{zh, en}`` object, or an AKEData ``{id, text}`` reference.  Keep this
    parsing in one place so every card follows the same CN-first fallback.
    """
    if value is None or isinstance(value, (list, tuple, set)):
        return default
    if not isinstance(value, Mapping):
        text = str(value).strip()
        if translations and text in translations:
            return localized_text(translations[text], default=default) or text
        return text or default

    for key in _LOCALE_KEYS:
        candidate = value.get(key)
        text = localized_text(candidate, translations, "")
        if text:
            return text

    text_id = value.get("id")
    if text_id is not None and translations:
        candidate = translations.get(str(text_id))
        text = localized_text(candidate, None, "")
        if text:
            return text

    text_key = value.get("key")
    if text_key is not None and translations:
        text = localized_text(translations.get(str(text_key)), default="")
        if text:
            return text

    for key in ("text", "value", "name", "label", "en", "default"):
        text = localized_text(value.get(key), translations, "")
        if text:
            return text
    return default


def semantic_key(value: Any) -> str:
    if isinstance(value, Mapping):
        return localized_text(value.get("key"))
    raw = localized_text(value)
    return raw if raw in SEMANTIC_LABELS else ""


def semantic_label(value: Any, default: str = "") -> str:
    key = semantic_key(value)
    if key:
        return SEMANTIC_LABELS.get(key, _semantic_value(value, default))
    raw = _semantic_value(value, default)
    return _VALUE_LABELS.get(raw.casefold(), raw)


def server_label(value: Any, default: str = "") -> str:
    raw = localized_text(value) or default
    return _SERVER_LABELS.get(raw.casefold(), raw)


def _semantic_value(value: Any, default: str = "") -> str:
    return localized_text(value, default=default)


def _text(value: Any) -> str:
    return localized_text(value)
