from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from pypinyin import Style, lazy_pinyin

from .sources import source_labels, source_order


ROOT_ALIASES = ("终末地", "endfield", "ef", "zmd")
OPERATOR_ALIASES = {"干员", "operator", "op"}
WEAPON_ALIASES = {"武器", "weapon", "wp"}
SEARCH_ALIASES = {"搜索", "search", "s"}
HELP_ALIASES = {"帮助", "help", "h", "?"}
SOURCE_ALIASES = {"数据源", "source", "sources"}
DEV_ALIASES = {"dev"}

SCOPE_LABELS = {
    "operator": "干员",
    "weapon": "武器",
}

SHORTCUT_COMMANDS = {
    "efop": ("query", "operator"),
    "efoperator": ("query", "operator"),
    "终末地干员": ("query", "operator"),
    "efwp": ("query", "weapon"),
    "efweapon": ("query", "weapon"),
    "终末地武器": ("query", "weapon"),
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
    dev_action: str = ""
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class EndfieldCandidate:
    kind: str
    key: str
    display_name: str
    score: int
    source: str = ""
    reason: str = ""


def parse_command(rest: str) -> ParsedEndfieldCommand:
    parts = _split(rest)
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
    if head in SEARCH_ALIASES:
        scope, query_parts = _parse_optional_scope(parts[1:])
        return ParsedEndfieldCommand("search", scope=scope, query=" ".join(query_parts).strip())
    if head in OPERATOR_ALIASES:
        return ParsedEndfieldCommand("query", scope="operator", query=" ".join(parts[1:]).strip())
    if head in WEAPON_ALIASES:
        return ParsedEndfieldCommand("query", scope="weapon", query=" ".join(parts[1:]).strip())

    return ParsedEndfieldCommand("query", scope="all", query=" ".join(parts).strip())


def parse_shortcut_command(command_name: str, rest: str) -> ParsedEndfieldCommand:
    key = command_name.strip().lstrip("/").lower()
    action, scope = SHORTCUT_COMMANDS.get(key, ("query", "all"))
    return ParsedEndfieldCommand(action, scope=scope, query=str(rest or "").strip())


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


def dev_visible_for_user(user_id: str, superusers: Iterable[str]) -> bool:
    return str(user_id) in {str(item) for item in superusers}


def format_help() -> str:
    return "\n".join(
        [
            "终末地查询用法：",
            "  /ef <关键词> 或 /zmd <关键词>",
            "  /ef 干员 <名称> | /ef op <名称>",
            "  /ef 武器 <名称> | /ef wp <名称>",
            "  /ef 搜索 <关键词> | /efs <关键词>",
            "  /ef 数据源",
            "",
            "快捷：/efop <名称>、/efwp <名称>、/终末地干员 <名称>、/终末地武器 <名称>",
        ]
    )


def format_source() -> str:
    return "\n".join(
        [
            "数据源：默认优先使用 FZ Wiki。",
            f"干员：{source_labels(source_order('operator'))}",
            f"武器：{source_labels(source_order('weapon'))}",
            "若主数据源暂时不可用或没有可用结果，会按顺序尝试备选源。",
        ]
    )


def format_unknown() -> str:
    return "未知命令或参数错误。发送 /ef help 查看用法。"


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
    lines.append("请使用 /ef 干员 <名称> 或 /ef 武器 <名称> 精确查询。")
    return "\n".join(lines)


def _parse_optional_scope(parts: list[str]) -> tuple[str, list[str]]:
    if not parts:
        return "all", []
    head = parts[0].lower()
    if head in OPERATOR_ALIASES:
        return "operator", parts[1:]
    if head in WEAPON_ALIASES:
        return "weapon", parts[1:]
    return "all", parts


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
