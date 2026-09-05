"""Common view construction; no I/O or command registration."""

from __future__ import annotations

import re
from typing import (
    Any,
)
from urllib.parse import (
    urlsplit,
    urlunsplit,
)

from ...account.i18n import (
    localized_text,
)
from ...providers.assets import (
    skill_icon_urls,
    static_sprite_url,
)
from ..models import (
    LEVEL_COLUMNS,
    TermStyleView,
)
from .constants import (
    FZ_ASSET_HOST,
    TERM_SUFFIXES,
    WEAPON_NAMES,
    WEAPON_OPTIONS,
)


def _equipment_stat_is_percent(row: dict[str, Any]) -> bool:
    value_format = str(row.get("valueFormat") or "")
    if value_format:
        return "%" in value_format
    if str(row.get("modifierType") or "") == "BaseMultiplier":
        return True
    return bool(row.get("isPercent"))


def _fz_overview_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        roster = attrs.get("roster") if isinstance(attrs, dict) else None
        if isinstance(roster, dict) and isinstance(roster.get("entries"), list):
            return [entry for entry in roster["entries"] if isinstance(entry, dict)]
    return []


def _format_equipment_stat(value: Any, is_percent: bool) -> str:
    number = _to_float(value)
    if number is None:
        return clean_text(value) or "--"
    if is_percent:
        if abs(number) <= 2:
            number *= 100
        return f"{number:.1f}".rstrip("0").rstrip(".") + "%"
    return _format_plain_number(number)


def _fz_template_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        if isinstance(attrs, dict) and isinstance(attrs.get("hero"), dict):
            return attrs
    return {}


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


def _ordered_fz_levels(levels: list[Any]) -> list[dict[str, Any]]:
    records = [item for item in levels if isinstance(item, dict)]
    records.sort(key=lambda item: _to_int(_first_value(item, "level", "lv")))
    return records


def _format_fz_template(desc: Any, values: Any) -> str:
    return _clean_fz_rich_text(_substitute_fz_placeholders(desc, values))


def _substitute_fz_placeholders(desc: Any, values: Any) -> str:
    value_map = _normalized_value_map(values if isinstance(values, dict) else {})

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key_expr, _, fmt = expr.partition(":")
        value = _eval_fz_template_expr(key_expr.strip(), value_map)
        return _format_template_value(value, fmt)

    return re.sub(r"\{([^{}]+)\}", replace, str(desc or ""))


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
    unary_match = re.fullmatch(r"([+-])([a-z0-9_]+)", expr)
    if unary_match:
        value = _fz_template_operand(unary_match.group(2), values)
        if value is None:
            return None
        return -value if unary_match.group(1) == "-" else value
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
    text = localized_text(value)
    if text:
        return text
    if isinstance(value, dict):
        return localized_text(_first_value(value, "url"))
    return ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := localized_text(item))]
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


def _fz_weapon_id(skills: list[dict[str, Any]]) -> str:
    for skill in reversed(skills):
        skill_id = str(skill.get("skillId") or skill.get("id") or "")
        match = re.fullmatch(r"sk_(wpn_[a-z0-9_]+)", skill_id, flags=re.I)
        if match:
            return match.group(1)
    return ""


def clean_text(value: Any) -> str:
    text = localized_text(value)
    text = re.sub(r"<[@#]?[A-Za-z0-9_.-]+>", "", text)
    text = re.sub(r"</>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    return " ".join(text.split())


def static_resource_url(path: str) -> str:
    return static_sprite_url(path)


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
    urls = skill_icon_urls(icon_id)
    return urls[0] if urls else ""


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


def _alias_key(key: str) -> str:
    return {
        "costValue": "costvalue",
        "Wil": "Will",
    }.get(key, key)


def _format_template_value(value: Any, fmt: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if "%" in fmt:
        decimal_match = re.search(r"\.(0+)%", fmt)
        decimals = len(decimal_match.group(1)) if decimal_match else 0
        return f"{number * 100:.{decimals}f}%"
    decimal_match = re.search(r"\.(0+)$", fmt)
    if decimal_match:
        return f"{number:.{len(decimal_match.group(1))}f}"
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


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
