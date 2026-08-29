from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
import re
from typing import Iterable, Sequence

from pypinyin import Style, lazy_pinyin

from .aliases import aliases_for, normalize_alias_text
from .sources import normalize_source, source_labels, source_order


ROOT_ALIASES = ("终末地", "endfield", "ef", "zmd")
OPERATOR_ALIASES = {"干员", "角色", "operator", "op"}
WEAPON_ALIASES = {"武器", "weapon", "wp"}
EQUIPMENT_ALIASES = {"装备", "equipment", "equip", "eq"}
STAGE_ALIASES = {"关卡", "副本", "stage", "dungeon"}
LOADOUT_ALIASES = {"配装", "配装模拟器", "loadout", "build"}
QUICK_CALC_ALIASES = {"速算", "quickcalc", "calc"}
SEARCH_ALIASES = {"搜索", "search", "s"}
HELP_ALIASES = {"帮助", "help", "h", "?"}
SOURCE_ALIASES = {"数据源", "source", "sources"}
CALENDAR_ALIASES = {"版本日历", "日历", "calendar", "schedule"}
DEV_ALIASES = {"dev"}
ALIAS_COMMAND_ALIASES = {"别名", "alias"}
ALIAS_ADD_ALIASES = {"添加", "新增", "add"}
BIND_ALIASES = {"绑定", "添加账号", "新增账号", "bind", "add-account", "addaccount"}
ACCOUNT_ALIASES = {"账号", "账户", "account", "accounts"}
INVESTMENT_ALIASES = {"养成统计", "资源消耗", "养成消耗", "investment", "resource-usage", "resourceusage"}
ACCOUNT_BASE_ALIASES = {"基建", "帝江号", "base", "infrastructure"}
PRIMARY_ALIASES = {"主账号", "主账户", "primary"}
UNBIND_ALIASES = {"解绑", "unbind"}
ATTENDANCE_ALIASES = {"签到", "checkin", "attendance"}
GACHA_ALIASES = {"抽卡", "gacha"}
GACHA_HISTORY_ALIASES = {"抽卡记录", "历史抽卡", "gacha-history", "history"}
GACHA_SYNC_ALIASES = {"抽卡同步", "同步抽卡", "gacha-sync", "sync"}
GACHA_IMPORT_ALIASES = {"抽卡导入", "小黑盒导入", "xhh-import", "gacha-import", "import"}
CURRENCY_LOG_ALIASES = {
    "资源流水",
    "资源记录",
    "货币流水",
    "流水",
    "currency-log",
    "currencylog",
    "resource-log",
    "resourcelog",
    "logs",
}
MEDAL_ALIASES = {"奖章", "蚀刻章", "medal", "medals"}
MEDAL_REFRESH_ALIASES = {"刷新", "refresh", "update"}
MEDAL_MISSING_ALIASES = {"缺章", "未获得", "missing"}
OWNERSHIP_ALIASES = {"持有率", "干员占比", "干员统计", "ownership", "ownership-rate"}
OWNERSHIP_GROUP_ALIASES = {"群内", "本群", "当前群", "group", "guild"}
OWNERSHIP_GLOBAL_ALIASES = {"全局", "全部", "global", "all"}

SCOPE_LABELS = {
    "operator": "干员",
    "weapon": "武器",
    "equipment": "装备",
    "equipment_catalog": "装备套组",
    "equipment_attribute": "装备",
    "stage": "关卡",
    "stage_catalog": "关卡目录",
}

EQUIPMENT_ATTRIBUTE_NAMES = {
    "力量": "力量",
    "strength": "力量",
    "str": "力量",
    "敏捷": "敏捷",
    "agility": "敏捷",
    "agi": "敏捷",
    "智识": "智识",
    "智力": "智识",
    "wisdom": "智识",
    "wisd": "智识",
    "意志": "意志",
    "will": "意志",
}
EQUIPMENT_ATTRIBUTE_SHORT_NAMES = {"力": "力量", "敏": "敏捷", "智": "智识", "意": "意志"}
EQUIPMENT_ATTRIBUTE_ROLES = {"主": "main", "副": "sub", "main": "main", "sub": "sub"}
EQUIPMENT_ATTRIBUTE_ROLE_LABELS = {"main": "主", "sub": "副"}
_EQUIPMENT_ATTRIBUTE_SEPARATORS = re.compile(r"[+＋、,，/／|｜]+")
_EQUIPMENT_ATTRIBUTE_NOISE = re.compile(r"^[-_:：·\s]*(?:属性|能力|词条)?[-_:：·\s]*")

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
    alias_action: str = ""
    args: tuple[str, ...] = ()
    char_level: int = 90
    char_potential: int = 5
    weapon_level: int = 90
    weapon_potential: int = 5
    weapon_skill_levels: tuple[tuple[int, int], ...] = ()
    enhance: int = 3
    account_selector: str = ""
    page: int = 1
    pool_filter: str = ""
    full: bool = False
    currency_types: tuple[int, ...] = ()
    change_type: int = 0
    start_date: str = ""
    end_date: str = ""
    days: int = 0
    all_history: bool = False
    status_name: str = ""
    status_level: int = 0
    arts_strength: int = 0
    error: str = ""


@dataclass(frozen=True)
class EndfieldCandidate:
    kind: str
    key: str
    display_name: str
    score: int
    source: str = ""
    reason: str = ""
    variant: str = ""
    mode: str = ""
    revision: str = ""


@dataclass(frozen=True)
class EquipmentAttributeFilter:
    attribute: str
    role: str = "any"


@dataclass(frozen=True)
class LoadoutSlotSpec:
    name: str
    forge_levels: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ParsedLoadoutSpec:
    items: tuple[LoadoutSlotSpec, ...]


def parse_command(rest: str) -> ParsedEndfieldCommand:
    parts = _split(rest)
    if not parts:
        return ParsedEndfieldCommand("help")

    personal = _parse_personal_command(parts)
    if personal is not None:
        return personal

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
    if head in CALENDAR_ALIASES:
        return ParsedEndfieldCommand("calendar")
    if head in DEV_ALIASES:
        dev_action = parts[1].lower() if len(parts) > 1 else "help"
        return ParsedEndfieldCommand("dev", dev_action=dev_action, args=tuple(parts[2:]))
    if head in ALIAS_COMMAND_ALIASES:
        action = parts[1].lower() if len(parts) > 1 else "help"
        if action in ALIAS_ADD_ALIASES:
            return ParsedEndfieldCommand("alias", alias_action="add", args=tuple(parts[2:]))
        return ParsedEndfieldCommand("alias", alias_action="add", args=tuple(parts[1:]))
    if head in LOADOUT_ALIASES:
        loadout_parts, levels, weapon_skill_levels, option_error = _parse_loadout_options(parts[1:])
        if option_error:
            return ParsedEndfieldCommand("invalid", error=option_error)
        return ParsedEndfieldCommand(
            "loadout",
            query=" ".join(loadout_parts).strip(),
            char_level=levels[0],
            char_potential=levels[1],
            weapon_level=levels[2],
            weapon_potential=levels[3],
            weapon_skill_levels=weapon_skill_levels,
            enhance=levels[4],
        )
    if head in QUICK_CALC_ALIASES:
        return _parse_quick_calc_command(parts[1:])
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
    if head in STAGE_ALIASES:
        return ParsedEndfieldCommand(
            "query", scope="stage", query=" ".join(parts[1:]).strip(), source=source, rarity=rarity
        )

    return ParsedEndfieldCommand("query", scope="all", query=" ".join(parts).strip(), source=source, rarity=rarity)


def _parse_quick_calc_command(parts: list[str]) -> ParsedEndfieldCommand:
    usage = "用法：/ef 速算 2腐蚀 200（效果可选腐蚀、导电、碎甲，等级为 1–4）"
    if len(parts) != 2:
        return ParsedEndfieldCommand("quick_calc", error=usage)

    effect_text = parts[0].strip()
    match = re.fullmatch(r"(?:lv\s*)?(\d+)\s*(腐蚀|导电|碎甲)", effect_text, flags=re.I)
    if match:
        level_text, status_name = match.groups()
    else:
        match = re.fullmatch(r"(腐蚀|导电|碎甲)\s*(?:lv\s*)?(\d+)", effect_text, flags=re.I)
        if not match:
            return ParsedEndfieldCommand("quick_calc", error=usage)
        status_name, level_text = match.groups()

    level = int(level_text)
    if level not in range(1, 5):
        return ParsedEndfieldCommand("quick_calc", error="异常效果等级必须在 1–4 之间")
    if not re.fullmatch(r"\d+", parts[1].strip()):
        return ParsedEndfieldCommand("quick_calc", error="源石技艺强度必须是大于或等于 0 的整数")

    return ParsedEndfieldCommand(
        "quick_calc",
        status_name=status_name,
        status_level=level,
        arts_strength=int(parts[1]),
    )


def _parse_personal_command(parts: list[str]) -> ParsedEndfieldCommand | None:
    head = parts[0].lower()
    if head in OWNERSHIP_ALIASES:
        return _parse_ownership_command(parts[1:])
    if head in BIND_ALIASES:
        return ParsedEndfieldCommand("bind")
    if head in ACCOUNT_ALIASES:
        if len(parts) > 1 and parts[1].lower() in ACCOUNT_BASE_ALIASES:
            return ParsedEndfieldCommand(
                "account_base",
                account_selector=" ".join(parts[2:]).strip(),
            )
        return ParsedEndfieldCommand("accounts", account_selector=" ".join(parts[1:]).strip())
    if head in INVESTMENT_ALIASES:
        return ParsedEndfieldCommand("account_investment", account_selector=" ".join(parts[1:]).strip())
    if head in CURRENCY_LOG_ALIASES:
        return _parse_currency_log_command(parts[1:])
    if head in PRIMARY_ALIASES:
        selector = " ".join(parts[1:]).strip()
        return ParsedEndfieldCommand("primary", account_selector=selector, error="请指定账号编号" if not selector else "")
    if head in UNBIND_ALIASES:
        selector = " ".join(parts[1:]).strip()
        return ParsedEndfieldCommand("unbind", account_selector=selector, error="请指定账号编号" if not selector else "")
    if head in ATTENDANCE_ALIASES:
        return ParsedEndfieldCommand("attendance", account_selector=" ".join(parts[1:]).strip() or "全部")
    if head in GACHA_SYNC_ALIASES:
        remaining, full, error = _parse_full_option(parts[1:])
        return ParsedEndfieldCommand(
            "gacha_sync", account_selector=" ".join(remaining).strip(), full=full, error=error
        )
    if head in GACHA_IMPORT_ALIASES:
        return ParsedEndfieldCommand("gacha_import", account_selector=" ".join(parts[1:]).strip())
    if head in GACHA_HISTORY_ALIASES:
        remaining, pool_filter, error = _parse_pool_option(parts[1:])
        if error:
            return ParsedEndfieldCommand("gacha_history", error=error)
        page = 1
        if len(remaining) >= 2 and remaining[-1].isdigit():
            page = int(remaining.pop())
            if page < 1:
                return ParsedEndfieldCommand("gacha_history", error="页码必须大于 0")
        return ParsedEndfieldCommand(
            "gacha_history",
            account_selector=" ".join(remaining).strip(),
            page=page,
            pool_filter=pool_filter,
        )
    if head in GACHA_ALIASES:
        return ParsedEndfieldCommand("gacha", account_selector=" ".join(parts[1:]).strip())
    if head in MEDAL_ALIASES:
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub in MEDAL_REFRESH_ALIASES:
            return ParsedEndfieldCommand("medal_refresh")
        if sub in MEDAL_MISSING_ALIASES:
            return ParsedEndfieldCommand(
                "medal_missing",
                account_selector=" ".join(parts[2:]).strip() or "主账号",
            )
        return ParsedEndfieldCommand("medal_view")
    return None


def _parse_ownership_command(parts: list[str]) -> ParsedEndfieldCommand:
    scope = "auto"
    refresh = False
    for raw in parts:
        token = raw.casefold()
        if token in MEDAL_REFRESH_ALIASES:
            if refresh:
                return ParsedEndfieldCommand("ownership_stats", error="“刷新”参数不能重复")
            refresh = True
            continue
        if token in OWNERSHIP_GROUP_ALIASES:
            if scope != "auto":
                return ParsedEndfieldCommand("ownership_stats", error="统计范围只能指定一次")
            scope = "group"
            continue
        if token in OWNERSHIP_GLOBAL_ALIASES:
            if scope != "auto":
                return ParsedEndfieldCommand("ownership_stats", error="统计范围只能指定一次")
            scope = "global"
            continue
        return ParsedEndfieldCommand(
            "ownership_stats",
            error="用法：/ef 持有率 [群内|全局]，或 /ef 持有率 刷新 [群内|全局]",
        )
    return ParsedEndfieldCommand("ownership_refresh" if refresh else "ownership_stats", scope=scope)


def _parse_currency_log_command(parts: list[str]) -> ParsedEndfieldCommand:
    remaining: list[str] = []
    currency_types: list[int] = []
    date_values: list[str] = []
    start_date = ""
    end_date = ""
    days = 0
    change_type = 0
    all_history = False
    index = 0

    while index < len(parts):
        part = parts[index]
        lowered = part.casefold()
        option, equals, inline_value = part.partition("=")
        option_lower = option.casefold()
        if option_lower in {"--资源", "--币种", "--currency", "--currencies", "--resource", "-r"}:
            value, index, error = _take_currency_option_value(parts, index, inline_value if equals else "")
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            parsed, error = _parse_currency_types(value)
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            currency_types.extend(parsed)
            continue
        if option_lower in {"--类型", "--变动", "--change", "--change-type", "-t"}:
            value, index, error = _take_currency_option_value(parts, index, inline_value if equals else "")
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            parsed = _parse_change_type(value)
            if parsed is None:
                return ParsedEndfieldCommand("currency_log", error=f"不支持的流水类型 {value}，可选全部、获取、消耗")
            if change_type and change_type != parsed:
                return ParsedEndfieldCommand("currency_log", error="只能指定一个流水类型")
            change_type = parsed
            continue
        if option_lower in {"--开始", "--起始", "--from", "--start"}:
            value, index, error = _take_currency_option_value(parts, index, inline_value if equals else "")
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            normalized = _normalize_currency_date(value)
            if normalized is None:
                return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{value}")
            if start_date:
                return ParsedEndfieldCommand("currency_log", error="只能指定一个开始日期")
            start_date = normalized
            continue
        if option_lower in {"--结束", "--截止", "--to", "--end"}:
            value, index, error = _take_currency_option_value(parts, index, inline_value if equals else "")
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            normalized = _normalize_currency_date(value)
            if normalized is None:
                return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{value}")
            if end_date:
                return ParsedEndfieldCommand("currency_log", error="只能指定一个结束日期")
            end_date = normalized
            continue
        if option_lower in {"--天数", "--天", "--days", "--day", "-d"}:
            value, index, error = _take_currency_option_value(parts, index, inline_value if equals else "")
            if error:
                return ParsedEndfieldCommand("currency_log", error=error)
            if not re.fullmatch(r"\d+", value) or int(value) <= 0:
                return ParsedEndfieldCommand("currency_log", error="天数必须是大于 0 的整数")
            parsed_days = int(value)
            if days and days != parsed_days:
                return ParsedEndfieldCommand("currency_log", error="只能指定一个查询天数")
            days = parsed_days
            continue
        if option_lower in {"-a", "--all", "--全部"}:
            if equals and inline_value.strip():
                return ParsedEndfieldCommand("currency_log", error=f"{part} 不需要参数")
            all_history = True
            index += 1
            continue

        range_values = _currency_date_values(part)
        if range_values is not None:
            if not range_values:
                return ParsedEndfieldCommand("currency_log", error=f"日期格式不正确：{part}")
            date_values.extend(range_values)
            index += 1
            continue
        if lowered in _CURRENCY_TYPE_ALIASES:
            currency_types.append(_CURRENCY_TYPE_ALIASES[lowered])
        elif lowered in _CHANGE_TYPE_ALIASES:
            parsed = _CHANGE_TYPE_ALIASES[lowered]
            if change_type and change_type != parsed:
                return ParsedEndfieldCommand("currency_log", error="只能指定一个流水类型")
            change_type = parsed
        else:
            remaining.append(part)
        index += 1

    if date_values:
        if start_date or end_date:
            return ParsedEndfieldCommand("currency_log", error="日期请使用位置参数或 --开始/--结束，不要混用")
        if len(date_values) > 2:
            return ParsedEndfieldCommand("currency_log", error="最多指定开始和结束两个日期")
        start_date = date_values[0]
        end_date = date_values[-1]
    if all_history and (start_date or end_date):
        return ParsedEndfieldCommand("currency_log", error="--all 不能与日期范围同时使用")
    if days and (start_date or end_date):
        return ParsedEndfieldCommand("currency_log", error="天数不能与日期范围同时使用")
    if all_history and days:
        return ParsedEndfieldCommand("currency_log", error="--all 不能与天数同时使用")
    return ParsedEndfieldCommand(
        "currency_log",
        account_selector=" ".join(remaining).strip(),
        currency_types=tuple(dict.fromkeys(currency_types)),
        change_type=change_type,
        start_date=start_date,
        end_date=end_date,
        days=days,
        all_history=all_history,
    )


_CURRENCY_TYPE_ALIASES = {
    "源石": 1,
    "originium": 1,
    "嵌晶玉": 2,
    "晶玉": 2,
    "diamond": 2,
    "crystal": 2,
    "武库配额": 3,
    "配额": 3,
    "quota": 3,
}

_CHANGE_TYPE_ALIASES = {
    "全部": 0,
    "all": 0,
    "获取": 1,
    "获得": 1,
    "收入": 1,
    "gain": 1,
    "消耗": 2,
    "支出": 2,
    "consume": 2,
    "spend": 2,
}


def _take_currency_option_value(parts: list[str], index: int, inline_value: str) -> tuple[str, int, str]:
    if inline_value.strip():
        return inline_value.strip(), index + 1, ""
    if index + 1 >= len(parts):
        return "", index + 1, f"{parts[index]} 后需要参数"
    return parts[index + 1].strip(), index + 2, ""


def _parse_currency_types(value: str) -> tuple[tuple[int, ...], str]:
    tokens = [token.strip() for token in re.split(r"[+,，、/／|｜]+", str(value or "")) if token.strip()]
    if not tokens:
        return (), "资源类型不能为空"
    result: list[int] = []
    for token in tokens:
        lowered = token.casefold()
        if lowered in {"全部", "all"}:
            result.extend((1, 2, 3))
            continue
        if token.isdecimal() and int(token) in {1, 2, 3}:
            result.append(int(token))
            continue
        currency_type = _CURRENCY_TYPE_ALIASES.get(lowered)
        if currency_type is None:
            return (), f"不支持的资源类型 {token}，可选源石、嵌晶玉、武库配额"
        result.append(currency_type)
    return tuple(dict.fromkeys(result)), ""


def _parse_change_type(value: str) -> int | None:
    lowered = str(value or "").strip().casefold()
    if lowered.isdecimal() and int(lowered) in {0, 1, 2}:
        return int(lowered)
    return _CHANGE_TYPE_ALIASES.get(lowered)


def _currency_date_values(value: str) -> tuple[str, ...] | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text) or text.casefold() in {"今天", "today", "昨天", "yesterday"}:
        normalized = _normalize_currency_date(text)
        return (normalized,) if normalized else ()
    match = re.fullmatch(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|今天|today|昨天|yesterday)\s*(?:~|～|至|到)\s*"
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|今天|today|昨天|yesterday)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    start = _normalize_currency_date(match.group(1))
    end = _normalize_currency_date(match.group(2))
    return (start, end) if start and end else ()


def _normalize_currency_date(value: str) -> str | None:
    text = str(value or "").strip()
    if text.casefold() in {"今天", "today", "昨天", "yesterday"}:
        return text.casefold()
    normalized = text.replace("/", "-")
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        return None


def _parse_pool_option(parts: list[str]) -> tuple[list[str], str, str]:
    remaining: list[str] = []
    pool_filter = ""
    index = 0
    while index < len(parts):
        part = parts[index]
        lowered = part.lower()
        if lowered in {"--池", "--pool"}:
            if index + 1 >= len(parts):
                return remaining, pool_filter, f"{part} 后需要卡池名称"
            value = parts[index + 1].strip()
            index += 2
        elif lowered.startswith("--池=") or lowered.startswith("--pool="):
            value = part.split("=", 1)[1].strip()
            index += 1
        else:
            remaining.append(part)
            index += 1
            continue
        if not value:
            return remaining, pool_filter, "卡池名称不能为空"
        if pool_filter and pool_filter != value:
            return remaining, pool_filter, "只能指定一个卡池筛选"
        pool_filter = value
    return remaining, pool_filter, ""


def _parse_full_option(parts: list[str]) -> tuple[list[str], bool, str]:
    remaining: list[str] = []
    full = False
    for part in parts:
        if part.lower() == "--full":
            if full:
                return remaining, full, "--full 只能指定一次"
            full = True
        else:
            remaining.append(part)
    return remaining, full, ""


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
    query_pinyin = _pinyin_syllables(query) if not normalized_query.isascii() else ()
    best = 0
    for value in values:
        normalized_value = _normalize(value)
        if not normalized_value:
            continue
        if (
            normalized_query.isascii()
            and normalized_value.isascii()
            and _ascii_name_gap_too_large(normalized_query, value)
        ):
            continue
        best = max(best, _score_normalized_pair(normalized_query, normalized_value))
        value_keys = _search_keys(value)
        if normalized_query.isascii():
            pinyin_score = _score_ascii_pinyin_pair(query_keys[0], _pinyin_syllables(value))
            best = max(best, min(pinyin_score, 88))
            if query_keys[1] == value_keys[1] and len(query_keys[1]) >= 2:
                best = max(best, 88)
        else:
            pinyin_score = _score_pinyin_syllable_pair(query_pinyin, _pinyin_syllables(value))
            if pinyin_score >= 82:
                best = max(best, min(pinyin_score, 88))
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


def normalize_alias_kind(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in OPERATOR_ALIASES:
        return "operator"
    if lowered in WEAPON_ALIASES:
        return "weapon"
    if lowered in EQUIPMENT_ALIASES:
        return "equipment"
    return ""


def format_help() -> str:
    return "\n".join(
        [
            "终末地查询用法：",
            "  /ef 绑定 | /ef 添加账号（仅私聊；国服支持 Token/短信，二维码绑定暂不支持；亚服支持 Token；可重复追加多个账号）",
            "  /ef 账号 [编号]（账号详情图：干员、装备、武器、技能等级、潜能）",
            "  /ef 养成统计 [编号]（当前档案可见养成投入与材料明细；别名：资源消耗）",
            "  /ef 资源流水 [账号] [日期|开始日期 结束日期|--天数 N/-d N]（源石、嵌晶玉、武库配额；默认最近一个月）",
            "  /ef 资源流水 [账号] [--资源 源石|嵌晶玉|武库配额] [--类型 获取|消耗] [-a/--all]",
            "  /ef 账号 基建 [账号]（据点存票、增长速度与帝江号心情）",
            "  /ef 主账号 <编号> | /ef 解绑 <编号>（仅私聊）",
            "  /ef 签到 [全部|编号|昵称|UID后四位]",
            "  /ef 抽卡 [账号] | /ef 抽卡同步 [账号] [--full]",
            "  /ef 抽卡导入 [账号]（仅私聊，手机号验证码导入小黑盒历史统计）",
            "  /ef 抽卡记录 [账号] [页码] [--池 <名称>]",
            "  /ef 奖章（查看蚀刻章总数与本版本新增）",
            "  /ef 奖章 刷新（重新抓取 AKEData 数据并更新上一游戏版本基线）",
            "  /ef 奖章 缺章 [账号]（查询自己未获得/未升满/未镀层）",
            "  /ef 持有率 [群内|全局]（匿名干员持有率；群聊默认群内，私聊默认全局）",
            "  /ef 持有率 刷新 [群内|全局]（群管理员可刷新本群，SUPERUSER 可刷新全局）",
            "  /ef 速算 2腐蚀 200（效果可替换为导电或碎甲）",
            "  /ef 版本日历（查看当前版本全部开放日程）",
            "",
            "  /ef <关键词>",
            "  /ef 干员 <名称> | /ef op <名称>",
            "  /ef 武器 <名称> | /ef wp <名称>",
            "  /ef 装备 <名称> | /ef eq <名称>",
            "  /ef 装备（查看全部套组）| /ef 装备 <套组名>",
            "  /ef 装备 主力量 副敏捷（按主副属性筛选）",
            "  /ef 关卡 | /ef 副本（查看关卡资料目录）",
            "  /ef 副本 <关卡名> [变体名|总览]",
            "  /ef 配装（交互输入干员、可选武器与装备）",
            "  /ef 配装 佩丽卡 脉冲源石配件 脉冲甲 脉冲源石配件 超轻域手 角色潜能2 武器潜能3",
            "  /ef 搜索 <关键词> | /efs <关键词>",
            "  /ef <关键词> --source <fz|akedata|warfarin>",
            "  /ef 数据源",
            "",
            "参数：-s/--source 可指定 FZ Wiki、AkeData 或 Warfarin Wiki。",
            "干员速查：/ef 干员；可按元素或职业筛选，例如 /ef 干员 灼热、/ef 干员 术师。",
            "武器速查：/ef 武器；可按类型筛选，例如 /ef 武器 单手剑。",
            "装备目录：默认仅金色；--all 显示全部，--rarity 可选 gold、purple、blue、all。",
            "装备属性筛选：主/副可省略，写“力量 敏捷”表示两条属性都要有；多条件同时满足才会列出。",
            "配装第一个名称固定为干员；之后武器与装备无需固定顺序，省略武器时自动使用推荐武器。干员/武器默认90级，角色/武器潜能默认5，装备词条默认3锻。",
            "潜能指定：追加“角色潜能2 武器潜能3”。",
            "武器技能指定：追加“武器技能1等级5”；可重复指定多个技能。",
            "单独调整词条：在装备后追加“词条2锻造2”；可重复追加多个词条设置。",
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
            f"关卡：{source_labels(source_order('stage'))}",
            "若主数据源暂时不可用或没有可用结果，会按顺序尝试备选源。",
        ]
    )


def format_unknown() -> str:
    return "未知命令或参数错误。发送 /ef help 查看用法。"


def format_error(error: str) -> str:
    return f"参数错误：{error}\n发送 /ef help 查看用法。"


def format_not_found(scope: str, query: str) -> str:
    label = SCOPE_LABELS.get(scope, "内容")
    if scope in {"stage", "stage_catalog"}:
        return f"未找到{label}：{query}\n可以发送 /ef 副本 浏览关卡资料目录"
    if scope == "equipment_attribute":
        return (
            f"没有同时满足 {query} 的装备。\n"
            "可以少写一条属性，例如 /ef 装备 主力量；或加 --all 放开稀有度限制"
        )
    return f"未找到{label}：{query}\n可以尝试 /ef 搜索 {query}"


def candidate_options(
    candidates: Sequence[EndfieldCandidate],
    *,
    query: str = "",
    limit: int = 8,
) -> list[EndfieldCandidate]:
    ordered = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    normalized_query = _normalize(query)
    if normalized_query:
        literal_matches = [
            candidate
            for candidate in ordered
            if any(
                normalized_query in _normalize(value)
                for value in (candidate.display_name, candidate.key)
            )
        ]
        if literal_matches:
            ordered = literal_matches
        else:
            accurate_matches = [
                candidate
                for candidate in ordered
                if score_entity_candidate(
                    candidate.kind,
                    query,
                    candidate.display_name,
                ) >= CANDIDATE_SCORE_THRESHOLD
            ]
            ordered = accurate_matches
    return ordered[:limit]


def parse_candidate_selection(value: str, option_count: int) -> int | None:
    text = str(value or "").strip()
    if not text.isdecimal():
        return None
    index = int(text) - 1
    return index if 0 <= index < option_count else None


def format_candidates(
    candidates: Sequence[EndfieldCandidate],
    *,
    title: str = "找到多个可能结果",
    interactive: bool = False,
) -> str:
    if not candidates:
        return "未找到相关结果。"
    options = candidate_options(candidates)
    lines = [f"{title}："]
    for index, item in enumerate(options, 1):
        label = SCOPE_LABELS.get(item.kind, item.kind)
        suffix = f" ({item.key})" if item.key and item.key != item.display_name else ""
        lines.append(f"{index}. [{label}] {item.display_name}{suffix}")
    if interactive:
        lines.append(f"可引用本消息并回复 1-{len(options)} 查询对应内容，也可不回复并忽略本消息。")
    else:
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
    if head in STAGE_ALIASES:
        return "stage", parts[1:]
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
            return remaining, source, f"不支持的数据源 {value}，可选 fz、akedata、warfarin"
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


def parse_equipment_attribute_filters(query: str) -> tuple[EquipmentAttributeFilter, ...]:
    """Read a query as 主/副属性 filters, e.g. “主力量 副敏捷”.

    Returns an empty tuple when any token is not an attribute term, so the
    caller can fall back to the normal name lookup.
    """
    tokens = _equipment_attribute_tokens(query)
    if not tokens:
        return ()
    filters: list[EquipmentAttributeFilter] = []
    for token in tokens:
        parsed = _parse_equipment_attribute_token(token)
        if parsed is None:
            return ()
        if parsed not in filters:
            filters.append(parsed)
    return tuple(filters)


def format_equipment_attribute_filters(filters: Sequence[EquipmentAttributeFilter]) -> str:
    return " · ".join(
        f"{EQUIPMENT_ATTRIBUTE_ROLE_LABELS.get(item.role, '')}{item.attribute}" for item in filters
    )


def _equipment_attribute_tokens(query: str) -> list[str]:
    parts = [
        piece
        for part in _split(query)
        for piece in _EQUIPMENT_ATTRIBUTE_SEPARATORS.split(part)
        if piece
    ]
    tokens: list[str] = []
    pending_role = ""
    for part in parts:
        if part.lower() in EQUIPMENT_ATTRIBUTE_ROLES:
            if pending_role:
                return []
            pending_role = part
            continue
        tokens.append(f"{pending_role}{part}")
        pending_role = ""
    return [] if pending_role else tokens


def _parse_equipment_attribute_token(token: str) -> EquipmentAttributeFilter | None:
    text = str(token or "").strip()
    role = "any"
    for prefix, value in EQUIPMENT_ATTRIBUTE_ROLES.items():
        if len(text) > len(prefix) and text.lower().startswith(prefix):
            role = value
            text = text[len(prefix):]
            break
    text = _EQUIPMENT_ATTRIBUTE_NOISE.sub("", text).strip()
    attribute = EQUIPMENT_ATTRIBUTE_NAMES.get(text.lower(), "")
    if not attribute and role != "any":
        attribute = EQUIPMENT_ATTRIBUTE_SHORT_NAMES.get(text, "")
    return EquipmentAttributeFilter(attribute, role) if attribute else None


def parse_loadout_spec(query: str, default_enhance: int = 3) -> tuple[ParsedLoadoutSpec | None, str]:
    del default_enhance
    items: list[LoadoutSlotSpec] = []
    for raw_token in _split(query):
        if raw_token.lower() in {"无", "none", "-"}:
            continue
        forge_syntax = re.fullmatch(r"(.*?)(?:词条)?([1-9]\d*)锻造(\d+)", raw_token)
        if forge_syntax and not 0 <= int(forge_syntax.group(3)) <= 3:
            return None, f"词条锻造等级必须在 0–3：{raw_token}"
        token, inline_forge = _split_inline_forge(raw_token)
        if token:
            items.append(LoadoutSlotSpec(token))
        if inline_forge is not None:
            if not items:
                return None, "词条锻造设置前需要先写装备名称"
            item = items[-1]
            forge_levels = dict(item.forge_levels)
            forge_levels[inline_forge[0]] = inline_forge[1]
            items[-1] = LoadoutSlotSpec(item.name, tuple(sorted(forge_levels.items())))
    if not items:
        return None, "请至少填写一个干员"
    return ParsedLoadoutSpec(tuple(items)), ""


def _split_inline_forge(token: str) -> tuple[str, tuple[int, int] | None]:
    match = re.fullmatch(r"(.*?)(?:词条)?([1-9]\d*)锻造([0-3])", token)
    if not match:
        return token, None
    return match.group(1).strip(), (int(match.group(2)), int(match.group(3)))


def _parse_loadout_options(
    parts: list[str],
) -> tuple[list[str], tuple[int, int, int, int, int], tuple[tuple[int, int], ...], str]:
    definitions = {
        "--char-level": (0, 1, 90, "干员等级"),
        "--char-potential": (1, 0, 5, "角色潜能"),
        "--operator-potential": (1, 0, 5, "角色潜能"),
        "--weapon-level": (2, 1, 90, "武器等级"),
        "--weapon-potential": (3, 0, 5, "武器潜能"),
        "--enhance": (4, 0, 3, "装备强化档位"),
    }
    values = [90, 5, 90, 5, 3]
    weapon_skill_levels: dict[int, int] = {}
    remaining: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if re.fullmatch(r"潜能\d+", part):
            return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "潜能类型不明确，请写角色潜能N或武器潜能N"
        if part.lower() == "--potential" or part.lower().startswith("--potential="):
            return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "请使用 --weapon-potential 指定武器潜能"
        skill_match = re.fullmatch(r"武器技能([1-9]\d*)等级([1-9]\d*)", part)
        if skill_match:
            skill_index = int(skill_match.group(1))
            skill_level = int(skill_match.group(2))
            if skill_level > 9:
                return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), "武器技能等级必须在 1–9"
            weapon_skill_levels[skill_index] = skill_level
            index += 1
            continue
        compact_match = re.fullmatch(r"(干员等级|角色等级|干员潜能|角色潜能|武器等级|武器潜能|默认锻造|装备锻造)(\d+)", part)
        if compact_match:
            compact_definitions = {
                "干员等级": (0, 1, 90, "干员等级"),
                "角色等级": (0, 1, 90, "干员等级"),
                "干员潜能": (1, 0, 5, "角色潜能"),
                "角色潜能": (1, 0, 5, "角色潜能"),
                "武器等级": (2, 1, 90, "武器等级"),
                "武器潜能": (3, 0, 5, "武器潜能"),
                "默认锻造": (4, 0, 3, "装备强化档位"),
                "装备锻造": (4, 0, 3, "装备强化档位"),
            }
            target, minimum, maximum, label = compact_definitions[compact_match.group(1)]
            value = int(compact_match.group(2))
            if not minimum <= value <= maximum:
                return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须在 {minimum}–{maximum}"
            values[target] = value
            index += 1
            continue
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
            return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{part} 后需要数值"
        target, minimum, maximum, label = definition
        try:
            value = int(raw_value)
        except ValueError:
            return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须是整数"
        if not minimum <= value <= maximum:
            return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), f"{label}必须在 {minimum}–{maximum}"
        values[target] = value
    return remaining, tuple(values), tuple(sorted(weapon_skill_levels.items())), ""


def _split(text: str) -> list[str]:
    return [part for part in str(text or "").split() if part]


def _normalize(text: str) -> str:
    return normalize_alias_text(text)


def _search_keys(text: str) -> tuple[str, str]:
    normalized = _normalize(text)
    if not normalized:
        return "", ""
    full_pinyin = _normalize("".join(lazy_pinyin(str(text or ""), errors="default")))
    initials = _normalize("".join(lazy_pinyin(str(text or ""), style=Style.FIRST_LETTER, errors="default")))
    return full_pinyin, initials


def _pinyin_syllables(text: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for part in lazy_pinyin(str(text or ""), errors="default")
        if (normalized := _normalize(part))
    )


def _ascii_name_gap_too_large(query: str, value: str) -> bool:
    words = tuple(part.casefold() for part in re.findall(r"[a-z0-9]+", str(value), flags=re.I))
    normalized_value = "".join(words)
    if not query or not normalized_value:
        return False
    if query == normalized_value or normalized_value.startswith(query):
        return False
    if any(word.startswith(query) or (len(query) >= 3 and query in word) for word in words):
        return False
    if len(query) >= 2 and len(words) >= 2 and query == "".join(word[0] for word in words):
        return False
    return SequenceMatcher(None, query, normalized_value).ratio() < 0.66


def _score_pinyin_syllable_pair(query: tuple[str, ...], value: tuple[str, ...]) -> int:
    if len(query) < 2 or len(query) > len(value):
        return 0
    if query == value:
        return 100
    for index in range(len(value) - len(query) + 1):
        if value[index : index + len(query)] == query:
            return 92 if index == 0 else 82
    return 0


def _score_ascii_pinyin_pair(query: str, value: tuple[str, ...]) -> int:
    if len(query) < 2 or not value:
        return 0
    joined_value = "".join(value)
    boundaries = {0}
    offset = 0
    for syllable in value:
        offset += len(syllable)
        boundaries.add(offset)
    if query == joined_value:
        return 100
    start = joined_value.find(query)
    while start >= 0:
        end = start + len(query)
        if start in boundaries and end in boundaries:
            return 92 if start == 0 else 82
        start = joined_value.find(query, start + 1)
    return 0


def _score_normalized_pair(query: str, value: str) -> int:
    if not query or not value:
        return 0
    if value == query:
        return 100
    if (len(query) == 1 and not query.isascii()) or (len(value) == 1 and not value.isascii()):
        return 0
    if (
        len(query) == len(value) == 3
        and not query.isascii()
        and not value.isascii()
        and query[1:] == value[1:]
    ):
        return 0
    if value.startswith(query):
        return 92
    if query in value and (not query.isascii() or len(query) >= 3):
        return 82
    if value in query:
        return 72

    best = 0
    if len(query) == len(value) and len(query) <= 4:
        diff_count = sum(left != right for left, right in zip(query, value))
        if diff_count == 1:
            if len(query) >= 3:
                best = max(best, 82)

    ratio = SequenceMatcher(None, query, value).ratio()
    if ratio >= 0.86:
        best = max(best, 78)
    elif ratio >= 0.76:
        best = max(best, 72)
    elif ratio >= 0.66:
        best = max(best, 65)
    return best
