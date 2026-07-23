from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Iterable, Sequence

from pypinyin import Style, lazy_pinyin

from .aliases import aliases_for
from .sources import normalize_source, source_labels, source_order


ROOT_ALIASES = ("终末地", "endfield", "ef", "zmd")
OPERATOR_ALIASES = {"干员", "角色", "operator", "op"}
WEAPON_ALIASES = {"武器", "weapon", "wp"}
EQUIPMENT_ALIASES = {"装备", "equipment", "equip", "eq"}
LOADOUT_ALIASES = {"配装", "配装模拟器", "loadout", "build"}
QUICK_CALC_ALIASES = {"速算", "quickcalc", "calc"}
SEARCH_ALIASES = {"搜索", "search", "s"}
HELP_ALIASES = {"帮助", "help", "h", "?"}
SOURCE_ALIASES = {"数据源", "source", "sources"}
DEV_ALIASES = {"dev"}
ALIAS_COMMAND_ALIASES = {"别名", "alias"}
ALIAS_ADD_ALIASES = {"添加", "新增", "add"}
BIND_ALIASES = {"绑定", "bind"}
ACCOUNT_ALIASES = {"账号", "账户", "account", "accounts"}
PRIMARY_ALIASES = {"主账号", "主账户", "primary"}
UNBIND_ALIASES = {"解绑", "unbind"}
ATTENDANCE_ALIASES = {"签到", "checkin", "attendance"}
GACHA_ALIASES = {"抽卡", "gacha"}
GACHA_HISTORY_ALIASES = {"抽卡记录", "历史抽卡", "gacha-history", "history"}
GACHA_SYNC_ALIASES = {"抽卡同步", "同步抽卡", "gacha-sync", "sync"}
GACHA_IMPORT_ALIASES = {"抽卡导入", "小黑盒导入", "xhh-import", "gacha-import", "import"}

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

    return ParsedEndfieldCommand("query", scope="all", query=" ".join(parts).strip(), source=source, rarity=rarity)


def _parse_quick_calc_command(parts: list[str]) -> ParsedEndfieldCommand:
    usage = "用法：/zmd 速算 2腐蚀 200（效果可选腐蚀、导电、碎甲，等级为 1–4）"
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
    if head in BIND_ALIASES:
        return ParsedEndfieldCommand("bind")
    if head in ACCOUNT_ALIASES:
        return ParsedEndfieldCommand("accounts")
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
    best = 0
    for value in values:
        normalized_value = _normalize(value)
        if not normalized_value:
            continue
        best = max(best, _score_normalized_pair(normalized_query, normalized_value))
        for query_key, value_key in zip(query_keys, _search_keys(value)):
            best = max(best, min(_score_search_key_pair(query_key, value_key), 88))
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
            "  /zmd 绑定（仅私聊，支持 Token 或短信验证码）",
            "  /zmd 账号 | /zmd 主账号 <编号> | /zmd 解绑 <编号>（仅私聊）",
            "  /zmd 签到 [全部|编号|昵称|UID后四位]",
            "  /zmd 抽卡 [账号] | /zmd 抽卡同步 [账号] [--full]",
            "  /zmd 抽卡导入 [账号]（仅私聊，手机号验证码导入小黑盒历史统计）",
            "  /zmd 抽卡记录 [账号] [页码] [--池 <名称>]",
            "  /zmd 速算 2腐蚀 200（效果可替换为导电或碎甲）",
            "",
            "  /ef <关键词> 或 /zmd <关键词>",
            "  /ef 干员 <名称> | /ef op <名称>",
            "  /ef 武器 <名称> | /ef wp <名称>",
            "  /ef 装备 <名称> | /ef eq <名称>",
            "  /ef 装备（查看全部套组）| /ef 装备 <套组名>",
            "  /ef 配装（交互输入干员、可选武器与装备）",
            "  /zmd 配装 佩丽卡 脉冲源石配件 脉冲甲 脉冲源石配件 超轻域手 角色潜能2 武器潜能3",
            "  /ef 搜索 <关键词> | /efs <关键词>",
            "  /ef <关键词> --source <fz|warfarin>",
            "  /ef 数据源",
            "",
            "参数：-s/--source 可指定 FZ Wiki 或 Warfarin Wiki。",
            "干员速查：/ef 干员；可按元素或职业筛选，例如 /ef 干员 灼热、/ef 干员 术师。",
            "武器速查：/ef 武器；可按类型筛选，例如 /ef 武器 单手剑。",
            "装备目录：默认仅金色；--all 显示全部，--rarity 可选 gold、purple、blue、all。",
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
    return "".join(char for char in str(text or "").lower() if char.isalnum())


def _search_keys(text: str) -> tuple[str, str]:
    normalized = _normalize(text)
    if not normalized:
        return "", ""
    full_pinyin = _normalize("".join(lazy_pinyin(str(text or ""), errors="default")))
    initials = _normalize("".join(lazy_pinyin(str(text or ""), style=Style.FIRST_LETTER, errors="default")))
    return full_pinyin, initials


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


def _score_search_key_pair(query: str, value: str) -> int:
    if query.isascii() and value.isascii():
        shorter_length = min(len(query), len(value))
        if shorter_length < 2:
            return 0
        score = _score_normalized_pair(query, value)
        if shorter_length < 3 and score in {72, 82}:
            return 0
        return score
    return _score_normalized_pair(query, value)
