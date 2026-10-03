"""终末地抽卡池类型注册表。

叶子模块：只依赖标准库（loguru 可选），`scripts/aggregate_endfield_gacha_stats.py` 等轻量脚本
可以单独加载。所有"这是什么池"的判定都集中在这里，识别优先级固定为：

    AKE 池表 type / poolId 前缀  →  官方接口枚举  →  名称启发式  →  兜底（unknown 或遗留默认）

规则参数（80 抽链共享范围、120/80 大保底、信物 240、加急招募 30/60/90、申领赠礼节奏……）
全部挂在 :class:`PoolKind` 上，统计层只按 kind 参数化，不再写死任何字符串。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Mapping

try:  # loguru 是运行时依赖；脚本环境缺失时退回标准库日志
    from loguru import logger
except ImportError:  # pragma: no cover - 只在精简环境触发
    logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..account.store import GachaRecord, XhhGachaPool
    from .assets import GachaPoolRule


ENUM_PREFIX = "E_CharacterGachaPoolType_"
LEGACY_CHARACTER_POOL_TYPES = (
    "E_CharacterGachaPoolType_Special",
    "E_CharacterGachaPoolType_Joint",
    "E_CharacterGachaPoolType_Standard",
    "E_CharacterGachaPoolType_Beginner",
)
XHH_RERUN_POOL_TYPE = "E_CharacterGachaPoolType_Rerun"
DEFAULT_CHARACTER_POOL_TYPES = (*LEGACY_CHARACTER_POOL_TYPES, XHH_RERUN_POOL_TYPE)
SHOW_STANDARD_ENV_KEYS = ("ENDFIELD_GACHA_SHOW_STANDARD", "GACHA_SHOW_STANDARD")
WEAPON_STREAM_ALL = "weapon:all"


@dataclass(frozen=True, slots=True)
class RewardSchedule:
    """累计申领赠礼节奏（武器池）。``claims`` 为申领次数（每次 10 发）。"""

    first: tuple[tuple[int, str], ...] = ()   # ((10, "weapon_box"), (18, "up_weapon"))
    step: int = 0                              # 之后每 step 次
    alternate: bool = False                    # True: step 后在 first 最后两种之间交替；False: 只重复最后一种
    include_box: bool = True                   # False: 只产出 up_weapon（限时武库沿用的现状口径）


@dataclass(frozen=True, slots=True)
class PoolKind:
    key: str
    item_type: str                      # "角色" | "武器"
    label: str                          # 分组标题："特许寻访"
    short: str                          # 芯片短名："特许"
    pity_scope: str                     # "shared:special" | "shared:rerun" | "shared:standard" | "per_pool"
    small_pity: int = 0                 # 角色 80；武器 4（十连批次）；0 = 不展示
    soft_pity_start: int = 0            # 66（第 66 抽起每抽 +5%）；0 = 无
    hard_guarantee_default: int = 0     # 120 / 80 / 0；FZ hardGuarantee 优先
    fixed_guarantee_position: int = 0   # 启程 40
    has_up: bool = False                # 是否做 UP / 歪 / 大保底消耗判定
    five_star_pity: int = 0             # 10；0 = 不展示
    cumulative_scope: str = "pool"      # "pool" | "series" —— 信物 / 加急 / 赠礼的累计范围
    keepsake_cycle: int = 0             # 240；0 = 无
    rush_thresholds: tuple[int, ...] = ()   # (30, 60, 90)；() = 无
    free_ten_unlock: int = 0            # 特许 30（≥30 抽送一次免费十连）；0 = 无
    reward_schedule: RewardSchedule | None = None
    in_expectation: bool = False
    expectation_group: str = ""         # "special" | "rerun" | "weapon" | ""
    sort_rank: int = 0
    is_unknown: bool = False
    char_type_code: int = -1            # AKE GachaCharPoolTable.type
    enum_suffixes: tuple[str, ...] = () # 接口枚举去前缀后的小写
    id_prefixes: tuple[str, ...] = ()   # pool_id 前缀（小写）
    name_markers: tuple[str, ...] = ()  # 名称 / 遗留 pool_type 启发式子串


WEAPON_LIMITED_SCHEDULE = RewardSchedule(first=((18, "up_weapon"),), step=16, include_box=False)
WEAPON_RERUN_SCHEDULE = RewardSchedule(first=((10, "weapon_box"), (18, "up_weapon")), step=8, alternate=True)

SPECIAL = PoolKind(
    "special", "角色", "特许寻访", "特许", "shared:special",
    small_pity=80, soft_pity_start=66, hard_guarantee_default=120, has_up=True, five_star_pity=10,
    cumulative_scope="pool", keepsake_cycle=240, free_ten_unlock=30,
    in_expectation=True, expectation_group="special", sort_rank=10, char_type_code=0,
    enum_suffixes=("special",), id_prefixes=("special_",), name_markers=("特许", "special"),
)
RERUN = PoolKind(
    "rerun", "角色", "重构寻访", "重构", "shared:rerun",
    small_pity=80, soft_pity_start=66, hard_guarantee_default=120, has_up=True, five_star_pity=10,
    cumulative_scope="series", keepsake_cycle=240, rush_thresholds=(30, 60, 90),
    in_expectation=True, expectation_group="rerun", sort_rank=20, char_type_code=4,
    enum_suffixes=("rerun",), id_prefixes=("rerun_chr_",), name_markers=("重构寻访", "重构", "rerun"),
)
JOINT = PoolKind(
    "joint", "角色", "特殊寻访", "特殊", "per_pool",
    small_pity=80, five_star_pity=10, sort_rank=30, char_type_code=3,
    enum_suffixes=("joint",), id_prefixes=("joint_",), name_markers=("特殊寻访", "庆典", "联合", "joint"),
)
BEGINNER = PoolKind(
    "beginner", "角色", "启程寻访", "启程", "per_pool",
    small_pity=80, fixed_guarantee_position=40, five_star_pity=10, sort_rank=50, char_type_code=1,
    enum_suffixes=("beginner",), id_prefixes=("beginner",), name_markers=("启程", "新手", "特惠", "beginner"),
)
STANDARD = PoolKind(
    "standard", "角色", "基础寻访", "基础", "shared:standard",
    small_pity=80, five_star_pity=10, sort_rank=60, char_type_code=2,
    enum_suffixes=("standard",), id_prefixes=("standard",),
    name_markers=("基础寻访", "基础", "常驻", "standard", "constant"),
)
UNKNOWN_CHAR = PoolKind(
    "unknown_char", "角色", "其他寻访", "待识别", "per_pool", sort_rank=90, is_unknown=True,
)
WEAPON_LIMITED = PoolKind(
    "weapon_limited", "武器", "限时申领", "限时", "per_pool",
    small_pity=4, hard_guarantee_default=80, has_up=True, cumulative_scope="pool",
    reward_schedule=WEAPON_LIMITED_SCHEDULE, in_expectation=True, expectation_group="weapon", sort_rank=110,
    id_prefixes=("weponbox_", "weaponbox_"), name_markers=("限时",),
)
WEAPON_RERUN = PoolKind(
    "weapon_rerun", "武器", "重构申领", "点绘", "per_pool",
    small_pity=4, hard_guarantee_default=80, has_up=True, cumulative_scope="series",
    reward_schedule=WEAPON_RERUN_SCHEDULE, in_expectation=True, expectation_group="weapon", sort_rank=120,
    id_prefixes=("rerun_wpn_",), name_markers=("重构申领", "点绘", "重构", "rerun"),
)
WEAPON_CONSTANT = PoolKind(
    "weapon_constant", "武器", "常驻申领", "常驻", "per_pool",
    small_pity=4, hard_guarantee_default=80, has_up=True, cumulative_scope="pool",
    reward_schedule=WEAPON_LIMITED_SCHEDULE, in_expectation=True, expectation_group="weapon", sort_rank=130,
    id_prefixes=("weaponbox_constant_", "weponbox_constant_"), name_markers=("常驻", "constant"),
)
UNKNOWN_WEAPON = PoolKind(
    "unknown_weapon", "武器", "其他申领", "待识别", "per_pool", sort_rank=190, is_unknown=True,
)

KINDS: tuple[PoolKind, ...] = (
    SPECIAL, RERUN, JOINT, BEGINNER, STANDARD, UNKNOWN_CHAR,
    WEAPON_LIMITED, WEAPON_RERUN, WEAPON_CONSTANT, UNKNOWN_WEAPON,
)
KINDS_BY_KEY: dict[str, PoolKind] = {kind.key: kind for kind in KINDS}
# 前缀 / 名称启发式的检查顺序：具体的在前（常驻武库必须先于普通武库，点绘先于"申领"）。
DETECTION_ORDER: tuple[PoolKind, ...] = (
    RERUN, JOINT, BEGINNER, STANDARD, SPECIAL,
    WEAPON_RERUN, WEAPON_CONSTANT, WEAPON_LIMITED,
)
CHAR_TYPE_CODES: dict[int, PoolKind] = {
    kind.char_type_code: kind for kind in KINDS if kind.char_type_code >= 0
}
EXPECTATION_GROUPS: dict[str, tuple[str, ...]] = {
    "special": ("special",),
    "rerun": ("rerun",),
    "weapon": ("weapon_limited", "weapon_rerun", "weapon_constant"),
}
CHAIN_LABELS = {
    "shared:special": "特许间共享继承",
    "shared:rerun": "重构间共享继承",
    "shared:standard": "基础寻访独立",
    "per_pool": "本池独立",
}

for _kind in KINDS:
    assert _kind.label.endswith("寻访") == (_kind.item_type == "角色"), _kind.key
    assert _kind.label.endswith("申领") == (_kind.item_type == "武器"), _kind.key
del _kind

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    logger.warning("[endfield] {}", message)


def _normalize_item_type(item_type: object) -> str:
    return "武器" if str(item_type or "").strip() == "武器" else "角色"


def _kind_from_rule(rule: object, pool_id: str, item_type: str) -> PoolKind | None:
    if rule is None:
        return None
    try:
        type_code = int(getattr(rule, "type_code", -1))
    except (TypeError, ValueError):
        return None
    if type_code < 0:
        return None
    table = str(getattr(rule, "table", "") or getattr(rule, "pool_kind", "") or "").strip().casefold()
    if table == "char":
        kind = CHAR_TYPE_CODES.get(type_code)
        return kind if kind is not None and item_type == "角色" else None
    if table == "weapon":
        if item_type != "武器":
            return None
        if type_code == 1:
            return WEAPON_RERUN
        client_top_time_id = str(getattr(rule, "client_top_time_id", "") or "")
        try:
            sort_id = int(getattr(rule, "sort_id", 0) or 0)
        except (TypeError, ValueError):
            sort_id = 0
        if pool_id.startswith(WEAPON_CONSTANT.id_prefixes) or (not client_top_time_id and sort_id == 0):
            return WEAPON_CONSTANT
        return WEAPON_LIMITED
    return None


def resolve_pool_kind(
    *,
    pool_id: str = "",
    pool_type: str = "",
    pool_name: str = "",
    item_type: str = "角色",
    rule: "GachaPoolRule | None" = None,
) -> PoolKind:
    """总识别函数，永不抛错。优先级：AKE 表 / 前缀 → 官方枚举 → 名称启发式 → 兜底。"""
    pid = str(pool_id or "").strip().casefold()
    ptype = str(pool_type or "").strip()
    pname = str(pool_name or "").strip()
    item = _normalize_item_type(item_type)
    # ① AKE 表权威
    kind = _kind_from_rule(rule, pid, item)
    if kind is not None:
        return kind
    # ①' poolId 前缀（与 AKE 表同一命名空间）
    for kind in DETECTION_ORDER:
        if kind.item_type == item and kind.id_prefixes and pid.startswith(kind.id_prefixes):
            return kind
    # ② 官方枚举
    if item == "角色" and ptype.startswith(ENUM_PREFIX):
        suffix = ptype[len(ENUM_PREFIX):].casefold()
        for kind in DETECTION_ORDER:
            if kind.item_type == "角色" and suffix in kind.enum_suffixes:
                return kind
        _warn_once(f"unknown character pool enum {ptype}")
        return UNKNOWN_CHAR
    # ③ 名称启发式（旧散落子串逻辑的集中版，含小黑盒原始 pool_type 串）
    identity = f"{ptype} {pid} {pname}".casefold()
    for kind in DETECTION_ORDER:
        if kind.item_type == item and any(marker.casefold() in identity for marker in kind.name_markers):
            return kind
    # ④ 兜底：遗留非枚举串维持旧默认（角色→特许链，武器→限时申领）；"E_" 开头的新枚举不猜规则
    if ptype.startswith("E_"):
        _warn_once(f"unresolved {item} pool {pool_id!r}/{ptype!r}")
        return UNKNOWN_WEAPON if item == "武器" else UNKNOWN_CHAR
    return WEAPON_LIMITED if item == "武器" else SPECIAL


def kind_from_enum(pool_type: str) -> PoolKind:
    """同步层给流起名用（无 rule）：只认官方枚举，其余一律 unknown。"""
    ptype = str(pool_type or "").strip()
    if ptype.startswith(ENUM_PREFIX):
        suffix = ptype[len(ENUM_PREFIX):].casefold()
        for kind in DETECTION_ORDER:
            if kind.item_type == "角色" and suffix in kind.enum_suffixes:
                return kind
    return UNKNOWN_CHAR


def kind_for_record(record: "GachaRecord", rule: "GachaPoolRule | None" = None) -> PoolKind:
    return resolve_pool_kind(
        pool_id=record.pool_id, pool_type=record.pool_type, pool_name=record.pool_name,
        item_type=record.item_type, rule=rule,
    )


def kind_for_xhh_pool(snapshot: "XhhGachaPool", rule: "GachaPoolRule | None" = None) -> PoolKind:
    return resolve_pool_kind(
        pool_id=snapshot.pool_id, pool_type=snapshot.pool_type, pool_name=snapshot.pool_name,
        item_type=snapshot.item_type, rule=rule,
    )


def pity_family(kind: PoolKind, pool_id: str) -> str:
    """80 抽保底链的归属：共享链用固定键，独立链带 pool_id。"""
    if kind.pity_scope.startswith("shared:"):
        return kind.pity_scope.split(":", 1)[1]
    return f"{kind.key}:{str(pool_id or '').casefold()}"


def chain_label(kind: PoolKind) -> str:
    return CHAIN_LABELS.get(kind.pity_scope, CHAIN_LABELS["per_pool"])


_SERIES_SUFFIX = re.compile(r"[\s#＃]*\d+$")
_SERIES_ID_SUFFIX = re.compile(r"(_v?\d+)$")


def series_base_name(pool_name: str) -> str:
    """"绚丽异彩#2" → "绚丽异彩"。"""
    return _SERIES_SUFFIX.sub("", str(pool_name or "").strip()).strip()


def series_key(kind: PoolKind, rule: "GachaPoolRule | None", pool_id: str, pool_name: str) -> str:
    """系列 = 同一 UP 的全部开放；非系列 kind 返回 ``pool:<pool_id>``。"""
    if kind.cumulative_scope != "series":
        return f"pool:{pool_id}"
    up_item_ids = tuple(getattr(rule, "up_item_ids", ()) or ()) if rule is not None else ()
    if up_item_ids:
        return f"{kind.key}:{up_item_ids[0]}"
    base = series_base_name(pool_name)
    if base:
        return f"{kind.key}:name:{base}"
    return f"{kind.key}:id:{_SERIES_ID_SUFFIX.sub('', str(pool_id or '').casefold())}"


def resolve_character_pool_types(api_names: Mapping[str, str] | None) -> tuple[str, ...]:
    """接口目录（按返回顺序）∪ 默认列表；非枚举键只取名字、不建流。"""
    api_types = [key for key in (api_names or {}) if str(key).startswith(ENUM_PREFIX)]
    return tuple(dict.fromkeys((*api_types, *DEFAULT_CHARACTER_POOL_TYPES)))


def weapon_reward_claims(schedule: RewardSchedule | None, max_claims: int) -> Iterator[tuple[int, str]]:
    """按节奏产出 (申领次数, 奖励种类)，直到超过 max_claims。"""
    if schedule is None or not schedule.first:
        return
    for count, reward in schedule.first:
        if count <= max_claims and (schedule.include_box or reward != "weapon_box"):
            yield count, reward
    if not schedule.step:
        return
    count, reward = schedule.first[-1]
    order = [item for _, item in schedule.first[-2:]] if schedule.alternate else [reward]
    index = order.index(reward)
    while True:
        count += schedule.step
        index = (index + 1) % len(order)
        reward = order[index]
        if count > max_claims:
            return
        if schedule.include_box or reward != "weapon_box":
            yield count, reward


def upcoming_weapon_rewards(schedule: RewardSchedule | None, claims: int) -> tuple[tuple[int, str], ...]:
    """每种奖励在 ``claims`` 之后的下一次触发点，按触发先后排列。"""
    if schedule is None or not schedule.first:
        return ()
    horizon = claims + max(schedule.step, 1) * 4 + max((count for count, _ in schedule.first), default=0)
    seen: dict[str, int] = {}
    for count, reward in weapon_reward_claims(schedule, horizon):
        if count > claims and reward not in seen:
            seen[reward] = count
    return tuple(sorted(((count, reward) for reward, count in seen.items())))


def kind_from_stream_key(stream_key: str) -> tuple[PoolKind | None, str]:
    """"char:<enum>" → (kind, ""); "weapon:all" → (None, "weapon"); "weapon:<id>" → (None, id)。"""
    text = str(stream_key or "")
    if text.startswith("char:"):
        return kind_from_enum(text[len("char:"):]), ""
    if text == WEAPON_STREAM_ALL:
        return None, "weapon"
    if text.startswith("weapon:"):
        return None, text[len("weapon:"):]
    return None, ""


def show_standard_pools() -> bool:
    """基础寻访是否展示（纯视图开关，后端不过滤）。默认 True。"""
    for key in SHOW_STANDARD_ENV_KEYS:
        value = os.environ.get(key)
        if value is None or not value.strip():
            continue
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return True
