"""抽卡分析的视图数据模型（后端 → 前端字段约定）。

全部为不可变 dataclass，所有新增字段都带默认值：``PoolAnalysis("id", "名", "角色", 10, 0)``
这类位置参数构造始终合法。字段语义见 ``gacha-out/impl/field_contract.md``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..account.store import EndfieldRole
from .assets import GachaPoolBanner


@dataclass(frozen=True, slots=True)
class NextReward:
    kind: str            # "rush" | "keepsake" | "weapon_box" | "up_weapon"
    label: str           # 加急招募 | 信物 | 武库赠礼 | 点绘赠礼 | UP武器
    at_position: int     # 触发点（角色：系列累计第几抽；武器：系列累计第几次申领）
    remaining: int       # 还差多少（单位同 unit）
    unit: str = "抽"     # "抽" | "次申领"


@dataclass(frozen=True, slots=True)
class SixStarEvent:
    name: str
    pool_name: str
    item_type: str
    gacha_ts: int
    item_id: str = ""
    interval: int = 0
    icon_path: str = ""
    pool_position: int = 0
    pity_labels: tuple[str, ...] = ()
    is_free: bool = False
    up_status: str = ""          # "up" | "off" | ""（无法判定）
    soft_pity_hit: bool = False  # soft_pity_start <= interval < small_pity
    run_label: str = ""          # "#2"；非系列池为 ""


@dataclass(frozen=True, slots=True)
class FreePullBatch:
    gacha_ts: int
    pull_count: int
    six_stars: tuple[SixStarEvent, ...] = ()
    source: str = ""             # "free_ten" | "rush" | ""
    threshold: int = 0           # 加急招募对应档位 30/60/90；否则 0


@dataclass(frozen=True, slots=True)
class KeepsakeGift:
    name: str
    item_id: str
    gacha_ts: int
    pool_position: int           # 本期内第几抽触发
    icon_path: str = ""
    gift_type: str = "信物"      # 文案：信物 | 武器 | 武库箱
    claim_count: int = 0         # 武器：系列累计第几次申领；信物：第几个
    gift_kind: str = "keepsake"  # "keepsake" | "up_weapon" | "weapon_box"
    series_position: int = 0     # 系列累计第几抽触发；非系列池等于 pool_position
    estimated: bool = False      # 落在无逐抽记录的区段


@dataclass(frozen=True, slots=True)
class PoolAnalysis:
    pool_id: str
    name: str
    item_type: str
    total: int
    since_six_star: int
    latest_ts: int = 0
    six_stars: tuple[SixStarEvent, ...] = ()
    is_current: bool = False
    paid_total: int = 0
    free_pull_count: int = 0
    free_batches: tuple[FreePullBatch, ...] = ()
    small_pity_progress: int = 0
    small_pity_limit: int = 0
    large_pity_progress: int = 0
    large_pity_limit: int = 0
    large_pity_known: bool = False
    large_pity_consumed: bool = False
    large_pity_consumed_at: int = 0
    large_pity_up_name: str = ""
    keepsake_progress: int = 0
    keepsake_claims: int = 0
    recorded_total: int = 0
    history_missing_count: int = 0
    keepsake_gifts: tuple[KeepsakeGift, ...] = ()
    sort_order: int = -1
    up_item_ids: tuple[str, ...] = ()
    banners: tuple[GachaPoolBanner, ...] = ()
    # ---- v3 身份 ----
    card_key: str = ""
    pool_version: int = 0
    kind_key: str = ""
    kind_label: str = ""
    kind_short: str = ""
    is_unknown_kind: bool = False
    hidden_by_default: bool = False
    sync_error: str = ""
    up_status_known: bool = False
    # ---- 小保底链 ----
    pity_family: str = ""
    pity_scope: str = ""
    soft_pity_start: int = 0
    soft_pity_active: bool = False
    five_star_pity_progress: int | None = None
    five_star_pity_limit: int = 0
    # ---- 大保底 ----
    large_pity_source: str = ""   # "fz" | "default" | ""
    large_pity_scope: str = "run"
    # ---- 系列 ----
    series_key: str = ""
    series_name: str = ""
    series_index: int = 1
    series_run_count: int = 1
    series_inherited_to: int = 0
    series_inherited_total: int = 0
    series_total: int = 0
    series_estimated: bool = False
    run_label: str = ""
    weapon_claims: int = 0
    series_claims: int = 0
    series_inherited_claims: int = 0
    # ---- 累计奖励 ----
    rush_thresholds: tuple[int, ...] = ()
    rush_claimed: int = 0
    rush_used: int = 0
    rush_next_threshold: int = 0
    rush_next_remaining: int = 0
    next_rewards: tuple[NextReward, ...] = ()


@dataclass(frozen=True, slots=True)
class SixStarExpectation:
    before_up: float
    after_up: float
    combined_before_up: float | None
    combined_after_up: float | None
    actual: float | None
    paid_pulls: int
    free_pulls: int
    account_pulls: int
    outcomes: int
    up_before: float
    up_after: float | None
    up_combined_before: float | None
    up_combined_after: float | None
    actual_up: float | None
    up_outcomes: int
    group: str = ""
    up_known: bool = True


@dataclass(frozen=True, slots=True)
class PityChainState:
    family: str
    scope: str
    label: str
    progress: int
    limit: int
    soft_pity_start: int = 0
    soft_pity_active: bool = False
    five_star_progress: int | None = None
    five_star_limit: int = 0
    last_six_pool_id: str = ""
    last_six_ts: int = 0
    pool_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRef:
    pool_id: str
    pool_version: int
    card_key: str
    name: str
    run_label: str
    first_ts: int
    last_ts: int
    paid_total: int
    free_batches: int
    is_current: bool


@dataclass(frozen=True, slots=True)
class SeriesState:
    series_key: str
    kind_key: str
    name: str
    runs: tuple[RunRef, ...]
    paid_total: int
    estimated: bool
    rush_thresholds: tuple[int, ...] = ()
    rush_claimed: int = 0
    rush_used: int = 0
    rush_next_threshold: int = 0
    rush_next_remaining: int = 0
    keepsake_cycle: int = 0
    keepsake_progress: int = 0
    keepsake_claims: int = 0
    claims: int = 0
    next_rewards: tuple[NextReward, ...] = ()


@dataclass(frozen=True, slots=True)
class KindSummary:
    key: str
    label: str
    short: str
    item_type: str
    pool_count: int = 0
    paid_total: int = 0
    free_pull_count: int = 0
    six_star_count: int = 0
    free_six_star_count: int = 0
    current_pool_id: str = ""
    current_card_key: str = ""
    chain: PityChainState | None = None
    series: SeriesState | None = None
    expectation_group: str = ""
    sync_error: str = ""
    is_unknown: bool = False
    hidden_by_default: bool = False


@dataclass(frozen=True, slots=True)
class GachaAnalysis:
    role: EndfieldRole
    total: int
    rarity_counts: dict[int, int]
    pools: tuple[PoolAnalysis, ...]
    six_stars: tuple[SixStarEvent, ...]
    intervals: tuple[int, ...]
    average_interval: float | None
    last_sync_at: int
    complete: bool
    errors: tuple[str, ...]
    paid_total: int = 0
    free_pull_count: int = 0
    free_ten_count: int = 0
    recorded_total: int = 0
    history_missing_count: int = 0
    xhh_imported_at: int = 0
    expectations: dict[str, SixStarExpectation] = field(default_factory=dict)
    kind_summaries: tuple[KindSummary, ...] = ()
    chains: tuple[PityChainState, ...] = ()
    series: tuple[SeriesState, ...] = ()
    show_standard_pools: bool = True
    stream_errors: tuple[tuple[str, str], ...] = ()
