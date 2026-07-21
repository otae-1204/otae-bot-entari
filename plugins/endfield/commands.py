from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from pypinyin import Style, lazy_pinyin

from .aliases import aliases_for
from .sources import normalize_source, source_labels, source_order


ROOT_ALIASES = ("终末地", "endfield", "ef", "zmd")
OPERATOR_ALIASES = {"干员", "角色", "operator", "op"}
WEAPON_ALIASES = {"武器", "weapon", "wp"}
EQUIPMENT_ALIASES = {"装备", "equipment", "equip", "eq"}
LOADOUT_ALIASES = {"配装", "配装模拟器", "loadout", "build"}
SEARCH_ALIASES = {"搜索", "search", "s"}
HELP_ALIASES = {"帮助", "help", "h", "?"}
SOURCE_ALIASES = {"数据源", "source", "sources"}
DEV_ALIASES = {"dev"}

SCOPE_LABELS = {
    "operator": "干员",
    "weapon": "武器",
    "equipment": "装备",
    "equipment_catalog": "装备套组",
}

SHORTCUT_COMMANDS = {
    "efop": ("query", "operator"),
    "efoperator": ("query", "operator"),
    "终末地干员": ("query", "operator"),
    "efwp": ("query", "weapon"),
    "efweapon": ("query", "weapon"),
    "终末地武器": ("query", "weapon"),
    "efeq": ("query", "equipment"),
    "efequipment": ("query", "equipment"),
    "终末地装备": ("query", "equipment"),
    "efs": ("search", "all"),
    "efsearch": ("search", "all"),
    "终末地搜索": ("search", "all"),
}

CANDIDATE_SCORE_THRESHOLD = 65
CLEAR_SCORE = 70
AMBIGUITY_MARGIN = 8


@dataclass(frozen=True)
class ParsedEndfieldCommand:
    action: str
    scope: str = "all"
    query: str = ""
    source: str = ""
    rarity: str = ""
    dev_action: str = ""
    args: tuple[str, ...] = ()
    char_level: int = 90
    weapon_level: int = 90
    potential: int = 5
    enhance: int = 3
    error: str = ""


@dataclass(frozen=True)
class EndfieldCandidate:
    kind: str
    key: str
    display_name: str
    score: int
    source: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LoadoutSlotSpec:
    name: str
    enhance: int = 3


@dataclass(frozen=True)
class ParsedLoadoutSpec:
    operator: str
    weapon: str
    body: LoadoutSlotSpec
    hand: LoadoutSlotSpec
    accessory_1: LoadoutSlotSpec
    accessory_2: LoadoutSlotSpec


def parse_command(rest: str) -> ParsedEndfieldCommand:
    parts = _split(rest)
    if not parts:
        return ParsedEndfieldCommand("help")

    parts, source, error = _parse_source_option(parts)
    if error:
        return ParsedEndfieldCommand("invalid", error=error)
    parts, rarity, error = _parse_rarity_option(parts)
    if error:
        return ParsedEndfieldCommand("invalid", error=error)
    if not parts:
        return ParsedEndfieldCommand("help")

    head = parts[0].lower()
    if head in HELP_ALIASES:
        return ParsedEndfieldCommand("help")
    if head in SOURCE_ALIASES:
        return ParsedEndfieldCommand("source")
    if head in DEV_ALIASES:
        dev_action = parts[1].lower() if len(parts) > 1 else "help"
        return ParsedEndfieldCommand("dev", dev_action=dev_action, args=tuple(parts[2:]))
    if head in LOADOUT_ALIASES:
        loadout_parts, levels, option_error = _parse_loadout_options(parts[1:])
        if option_error:
            return ParsedEndfieldCommand("invalid", error=option_error)
        return ParsedEndfieldCommand(
            "loadout",
            query=" ".join(loadout_parts).strip(),
            char_level=levels[0],
            weapon_level=levels[1],
            potential=levels[2],
            enhance=levels[3],
        )
    if head in SEARCH_ALIASES:
        scope, query_parts = _parse_optional_scope(parts[1:])
        return ParsedEndfieldCommand("search", scope=scope, query=" ".join(query_parts).strip(), source=source, rarity=rarity)
    if head in OPERATOR_ALIASES:
        return ParsedEndfieldCommand(
            "query", scope="operator", query=" ".join(parts[1:]).strip(), source=source, rarity=rarity
        )
    if head in WEAPON_ALIASES:
        return ParsedEndfieldCommand(
            "query", scope="weapon", query=" ".join(parts[1:]).strip(), source=source, rarity=rarity
        )
    if head in EQUIPMENT_ALIASES:
        return ParsedEndfieldCommand(
            "query", scope="equipment", query=" ".join(parts[1:]).strip(), source=source, rarity=rarity
        )

    return ParsedEndfieldCommand("query", scope="all", query=" ".join(parts).strip(), source=source, rarity=rarity)


def parse_shortcut_command(command_name: str, rest: str) -> ParsedEndfieldCommand:
    key = command_name.strip().lstrip("/").lower()
    action, scope = SHORTCUT_COMMANDS.get(key, ("query", "all"))
    parts, source, error = _parse_source_option(_split(rest))
    if error:
        return ParsedEndfieldCommand("invalid", error=error)
    parts, rarity, error = _parse_rarity_option(parts)
    if error:
        return ParsedEndfieldCommand("invalid", error=error)
    return ParsedEndfieldCommand(action, scope=scope, query=" ".join(parts).strip(), source=source, rarity=rarity)


def choose_candidate(candidates: Sequence[EndfieldCandidate]) -> tuple[EndfieldCandidate | None, list[EndfieldCandidate]]:
    ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
    if not ordered:
        return None, []
    best = ordered[0]
    if best.score < CLEAR_SCORE:
        return None, ordered
    if len(ordered) > 1 and best.score - ordered[1].score < AMBIGUITY_MARGIN:
        return None, ordered
    return best, []


def score_candidate(query: str, *values: str) -> int:
    normalized_query = _normalize(query)
    if not normalized_query:
        return 0
    query_keys = _search_keys(query)
    best = 0
    for value in values:
        normalized_value = _normalize(value)
        if not normalized_value:
            continue
        best = max(best, _score_normalized_pair(normalized_query, normalized_value))
        for query_key in query_keys:
            for value_key in _search_keys(value):
                if query_key == normalized_query and value_key == normalized_value:
                    continue
                best = max(best, min(_score_normalized_pair(query_key, value_key), 88))
    return best


def score_entity_candidate(kind: str, query: str, canonical_name: str, *values: str) -> int:
    return score_candidate(
        query,
        canonical_name,
        *values,
        *aliases_for(kind, canonical_name),
    )


def dev_visible_for_user(user_id: str, superusers: Iterable[str]) -> bool:
    return str(user_id) in {str(item) for item in superusers}


def format_help() -> str:
    return "\n".join(
        [
            "终末地查询用法：",
            "  /ef <关键词> 或 /zmd <关键词>",
            "  /ef 干员 <名称> | /ef op <名称>",
            "  /ef 武器 <名称> | /ef wp <名称>",
            "  /ef 装备 <名称> | /ef eq <名称>",
            "  /ef 装备（查看全部套组）| /ef 装备 <套组名>",
            "  /ef 配装（交互选择干员、武器与四件装备）",
            "  /ef 配装 干员 | 武器 | 护甲@3 | 护手@3 | 配件1@3 | 配件2@3",
            "  /ef 搜索 <关键词> | /efs <关键词>",
            "  /ef <关键词> --source <fz|warfarin>",
            "  /ef 数据源",
            "",
            "参数：-s/--source 可指定 FZ Wiki 或 Warfarin Wiki。",
            "干员速查：/ef 干员；可按元素或职业筛选，例如 /ef 干员 灼热、/ef 干员 术师。",
            "武器速查：/ef 武器；可按类型筛选，例如 /ef 武器 单手剑。",
            "装备目录：默认仅金色；--all 显示全部，--rarity 可选 gold、purple、blue、all。",
            "配装参数：--char-level 90、--weapon-level 90、--potential 5、--enhance 3；装备可填“无”。",
            "快捷：/efop <名称>、/efwp <名称>、/efeq <名称>、/终末地干员 <名称>、/终末地武器 <名称>、/终末地装备 <名称>",
        ]
    )


def format_source() -> str:
    return "\n".join(
        [
            "数据源：默认优先使用 FZ Wiki。",
            f"干员：{source_labels(source_order('operator'))}",
            f"武器：{source_labels(source_order('weapon'))}",
            f"装备：{source_labels(source_order('equipment'))}",
            "若主数据源暂时不可用或没有可用结果，会按顺序尝试备选源。",
        ]
    )


def format_unknown() -> str:
    return "未知命令或参数错误。发送 /ef help 查看用法。"


def format_error(error: str) -> str:
    return f"参数错误：{error}\n发送 /ef help 查看用法。"


def format_not_found(scope: str, query: str) -> str:
    label = SCOPE_LABELS.get(scope, "内容")
    return f"未找到{label}：{query}\n可以尝试 /ef 搜索 {query}"


def format_candidates(candidates: Sequence[EndfieldCandidate], *, title: str = "找到多个可能结果") -> str:
    if not candidates:
        return "未找到相关结果。"
    lines = [f"{title}："]
    for index, item in enumerate(sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:8], 1):
        label = SCOPE_LABELS.get(item.kind, item.kind)
        suffix = f" ({item.key})" if item.key and item.key != item.display_name else ""
        lines.append(f"{index}. [{label}] {item.display_name}{suffix}")
    lines.append("请使用 /ef 干员 <名称>、/ef 武器 <名称> 或 /ef 装备 <名称> 精确查询。")
    return "\n".join(lines)


def _parse_optional_scope(parts: list[str]) -> tuple[str, list[str]]:
    if not parts:
        return "all", []
    head = parts[0].lower()
    if head in OPERATOR_ALIASES:
        return "operator", parts[1:]
    if head in WEAPON_ALIASES:
        return "weapon", parts[1:]
    if head in EQUIPMENT_ALIASES:
        return "equipment", parts[1:]
    return "all", parts


def _parse_source_option(parts: list[str]) -> tuple[list[str], str, str]:
    remaining: list[str] = []
    source = ""
    index = 0
    while index < len(parts):
        part = parts[index]
        lowered = part.lower()
        value = ""
        if lowered in {"-s", "--source"}:
            if index + 1 >= len(parts):
                return remaining, source, f"{part} 后需要数据源名称"
            value = parts[index + 1]
            index += 2
        elif lowered.startswith("--source="):
            value = part.split("=", 1)[1]
            index += 1
        else:
            remaining.append(part)
            index += 1
            continue

        normalized = normalize_source(value)
        if not normalized:
            return remaining, source, f"不支持的数据源 {value}，可选 fz、warfarin"
        if source and source != normalized:
            return remaining, source, "只能指定一个数据源"
        source = normalized
    return remaining, source, ""


def _parse_rarity_option(parts: list[str]) -> tuple[list[str], str, str]:
    remaining: list[str] = []
    rarity = ""
    aliases = {
        "gold": "gold",
        "金": "gold",
        "金色": "gold",
        "purple": "purple",
        "紫": "purple",
        "紫色": "purple",
        "blue": "blue",
        "蓝": "blue",
        "蓝色": "blue",
        "all": "all",
        "全部": "all",
    }
    index = 0
    while index < len(parts):
        part = parts[index]
        lowered = part.lower()
        value = ""
        if lowered == "--all":
            value = "all"
            index += 1
        elif lowered == "--rarity":
            if index + 1 >= len(parts):
                return remaining, rarity, "--rarity 后需要稀有度名称"
            value = parts[index + 1]
            index += 2
        elif lowered.startswith("--rarity="):
            value = part.split("=", 1)[1]
            index += 1
        else:
            remaining.append(part)
            index += 1
            continue
        normalized = aliases.get(str(value).strip().lower(), "")
        if not normalized:
            return remaining, rarity, f"不支持的装备稀有度 {value}，可选 gold、purple、blue、all"
        if rarity and rarity != normalized:
            return remaining, rarity, "只能指定一个装备稀有度"
        rarity = normalized
    return remaining, rarity, ""


def parse_loadout_spec(query: str, default_enhance: int = 3) -> tuple[ParsedLoadoutSpec | None, str]:
    fields = [field.strip() for field in str(query or "").split("|")]
    if len(fields) != 6:
        return None, "完整配装需要依次填写干员、武器、护甲、护手和两个配件，并用 | 分隔"
    if not fields[0] or fields[0] in {"无", "none", "-"}:
        return None, "干员不能为空"
    if not fields[1] or fields[1] in {"无", "none", "-"}:
        return None, "武器不能为空"

    slots: list[LoadoutSlotSpec] = []
    for field in fields[2:]:
        slot, error = _parse_loadout_slot(field, default_enhance)
        if error:
            return None, error
        slots.append(slot)
    return ParsedLoadoutSpec(fields[0], fields[1], *slots), ""


def _parse_loadout_slot(value: str, default_enhance: int) -> tuple[LoadoutSlotSpec, str]:
    value = value.strip()
    if not value or value.lower() in {"无", "none", "-"}:
        return LoadoutSlotSpec("", default_enhance), ""
    name = value
    enhance = default_enhance
    if "@" in value:
        name, raw_enhance = value.rsplit("@", 1)
        name = name.strip()
        try:
            enhance = int(raw_enhance)
        except ValueError:
            return LoadoutSlotSpec(name, default_enhance), f"装备强化档位无效：{value}"
    if not name:
        return LoadoutSlotSpec("", default_enhance), f"装备名称不能为空：{value}"
    if not 0 <= enhance <= 3:
        return LoadoutSlotSpec(name, enhance), f"装备强化档位必须在 0–3：{value}"
    return LoadoutSlotSpec(name, enhance), ""


def _parse_loadout_options(parts: list[str]) -> tuple[list[str], tuple[int, int, int, int], str]:
    definitions = {
        "--char-level": (0, 1, 90, "干员等级"),
        "--weapon-level": (1, 1, 90, "武器等级"),
        "--potential": (2, 0, 5, "武器潜能"),
        "--enhance": (3, 0, 3, "装备强化档位"),
    }
    values = [90, 90, 5, 3]
    remaining: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        option = part.split("=", 1)[0].lower()
        definition = definitions.get(option)
        if definition is None:
            remaining.append(part)
            index += 1
            continue
        if "=" in part:
            raw_value = part.split("=", 1)[1]
            index += 1
        elif index + 1 < len(parts):
            raw_value = parts[index + 1]
            index += 2
        else:
            return remaining, tuple(values), f"{part} 后需要数值"
        target, minimum, maximum, label = definition
        try:
            value = int(raw_value)
        except ValueError:
            return remaining, tuple(values), f"{label}必须是整数"
        if not minimum <= value <= maximum:
            return remaining, tuple(values), f"{label}必须在 {minimum}–{maximum}"
        values[target] = value
    return remaining, tuple(values), ""


def _split(text: str) -> list[str]:
    return [part for part in str(text or "").split() if part]


def _normalize(text: str) -> str:
    return "".join(char for char in str(text or "").lower() if char.isalnum())


def _search_keys(text: str) -> set[str]:
    normalized = _normalize(text)
    if not normalized:
        return set()
    keys = {normalized}
    full_pinyin = _normalize("".join(lazy_pinyin(str(text or ""), errors="default")))
    initials = _normalize("".join(lazy_pinyin(str(text or ""), style=Style.FIRST_LETTER, errors="default")))
    if full_pinyin:
        keys.add(full_pinyin)
    if initials:
        keys.add(initials)
    return keys


def _score_normalized_pair(query: str, value: str) -> int:
    if not query or not value:
        return 0
    if value == query:
        return 100
    if value.startswith(query):
        return 92
    if query in value:
        return 82
    if value in query:
        return 72

    best = 0
    if len(query) == len(value) and len(query) <= 4:
        diff_count = sum(left != right for left, right in zip(query, value))
        if diff_count == 1:
            best = max(best, 78 if len(query) <= 2 else 82)
        elif diff_count == 2 and len(query) >= 3:
            best = max(best, 66)

    ratio = SequenceMatcher(None, query, value).ratio()
    if ratio >= 0.86:
        best = max(best, 78)
    elif ratio >= 0.76:
        best = max(best, 72)
    elif ratio >= 0.66:
        best = max(best, 65)
    return best
