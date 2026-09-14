"""AI 智商雷达的只读数据模型。

三条纪律：

1. **全部 frozen + slots**：provider 之后没有任何一层能改写数据，口径不可能在中途被"修正"。
2. **不丢口径**：每个结果都带 :class:`RadarMeta`（频道 / 打分模式 / 口径 mode / 数据时间 /
   是否陈旧）。缺了它，IQ 与通过率就不可解释。
3. **估算与实测分开**：成本/耗时一律包成 ``cost`` + ``cost_src`` 两个字段，强制调用方处理
   「这是实测还是估算」（数据字典 §5：``src != measured`` 必须标注为估算）。

本模块**不出网、不读环境变量**（框架 §2.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

#: 上游 ``scoring_mode``：二值多数 / 连续宏平均。两者分数不可比（数据字典 §2）。
SCORING_BINARY = "binary-majority"
SCORING_CONTINUOUS = "continuous-macro"

#: 格子状态（``Cell.st``）。
CELL_OPEN = "open"
CELL_COOLDOWN = "cooldown"
CELL_LEASED = "leased"
CELL_RUNNING = "running"

#: 数值来源 ``src``。只有 ``measured`` 是真实测；其余都是估算（数据字典 §5）。
SRC_MEASURED = "measured"


def now_iso() -> str:
    """当前 UTC 时间的 ISO8601 表示（秒级，带 ``+00:00``）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_estimate(cost_src: str | None) -> bool:
    """``src`` 缺失或非 ``measured`` 即视为估算。

    缺失按估算处理：上游方法论页明确「不用回退值制造确定性」，所以「没标注」不能被当成
    「实测」来展示。
    """
    return (cost_src or "") != SRC_MEASURED


@dataclass(frozen=True, slots=True)
class RadarMeta:
    """每个结果必带的口径与数据时间。

    ``source_updated_at`` 优先取 **point 级**时间（如 ``/intelligence-efficiency`` 每个
    point 自带），其次才回落到顶层。
    """

    benchmark_id: str
    scoring_mode: str = ""
    score_label: str = ""
    mode: str | None = None
    rolling_window: int | None = None
    pass_threshold: float | None = None
    source_updated_at: str | None = None
    stale: bool = False
    fetched_at: str = field(default_factory=now_iso)
    #: 上游自己给的加权口径说明（``/radar-insights`` 的 ``recommendation_mode``）。
    recommendation_mode: str | None = None
    #: 说明本条结果的口径被如何选取（如「已取各模型最高档」）。formatter 必须展示。
    note: str = ""
    #: 本条结果背后的样本量（数据字典 §16 红线 2：必须带样本量）。
    #: 只有「有唯一主体」的结果才填（单模型档案 / 单条趋势 / 单题详情）；
    #: 列表类结果每行自带 ``n=``，此处留 ``None`` 以免造出一个含糊的总数。
    samples: int | None = None


@dataclass(frozen=True, slots=True)
class TaskBundle:
    url: str
    sha256: str
    bytes: int | None
    format: str


@dataclass(frozen=True, slots=True)
class BenchmarkInfo:
    id: str
    title: str
    short_title: str
    description: str
    task_count: int
    scoring_mode: str
    score_label: str
    rolling_window: int
    model_config_count: int
    reference_task_id: str | None = None
    reference_url: str | None = None
    task_bundle: TaskBundle | None = None
    default: bool = False


@dataclass(frozen=True, slots=True)
class Discrimination:
    """题目区分度（``task-discrimination-v2``）。``score`` 高 = 好题；``confidence`` 低 = 结论不稳。"""

    score: float
    confidence: float
    raw_score: float
    model: float
    effort: float
    monotonic: float
    config: float
    threshold: float
    cells: int
    samples: int


@dataclass(frozen=True, slots=True)
class TaskInfo:
    id: str
    title: str
    language: str
    repo: str
    category: str
    fragment_count: int | None = None
    metric: str | None = None
    discrimination: Discrimination | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """模型档位。``(model, effort)`` 是不可分割的键（数据字典 §3）。"""

    model: str
    effort: str


@dataclass(frozen=True, slots=True)
class TaskVote:
    votes: int
    pass_votes: int
    majority_pass: bool
    score_sum: float
    score_rate: float


@dataclass(frozen=True, slots=True)
class ModelRow:
    model: str
    effort: str
    graded: int
    passed: int
    score_sum: float
    cells: int
    cells_passed: int
    pass_rate: float
    #: 上游 ``/radar-insights`` 有值时用它；否则由 ``pass_rate × 150`` 换算并标 ``iq_derived``。
    iq: float | None = None
    iq_derived: bool = False
    tasks: Mapping[str, TaskVote] = field(default_factory=dict)
    meta: RadarMeta | None = None

    @property
    def key(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次有效运行（上游 ``ran_by`` 条目）。身份字段已归一到 ``display_name``。"""

    display_name: str
    avatar_url: str | None
    passed: bool
    score: float
    graded_at: str
    points_base: float | None = None
    points_multiplier: float | None = None
    duration_sec: float | None = None
    actual_cost_usd: float | None = None
    cost_source: str | None = None
    cost_complete: bool | None = None
    #: 价格表版本可能因 run 而异 —— 历史成本不得用最新价表重算（数据字典 §5）。
    token_pricing_version: str | None = None


@dataclass(frozen=True, slots=True)
class CellState:
    """一个「某模型某档位在某道题上」的聚合格子。"""

    cell_id: str
    task_id: str
    model: str
    effort: str
    st: str
    n: int
    p: int
    score_sum: float
    rate: float
    total_n: int
    total_p: int
    total_score: float
    base_mult: float
    wasteland: bool
    wasteland_multiplier: float
    mult: float
    last_graded_at: str | None = None
    last_points: float | None = None
    #: 预估成本与它的来源；``is_estimate(cost_src)`` 为真时必须标注为估算。
    cost: float | None = None
    cost_src: str | None = None
    minutes: int | None = None
    ran_by: tuple[RunRecord, ...] = ()

    @property
    def cost_is_estimate(self) -> bool:
        return is_estimate(self.cost_src)


@dataclass(frozen=True, slots=True)
class EfficiencyPoint:
    """``/intelligence-efficiency`` 的一个档位：分数 + 成本 + 耗时。"""

    model: str
    effort: str
    iq: float | None
    #: 连续制频道下是 F1 加权和（浮点），不是「过了多少题」——按 ``scoring_mode`` 展示。
    passed: float | None
    total: float | None
    average_price_usd: float | None = None
    average_minutes: float | None = None
    combined_cost_index: float | None = None
    average_agent_steps: float | None = None
    agent_steps_samples: int | None = None
    average_total_tokens: float | None = None
    token_samples: int | None = None
    cache_hit_rate: float | None = None
    cache_token_samples: int | None = None
    runs_24h: int = 0
    runs_48h: int = 0
    runs_total: int = 0
    #: point 级数据时间，优先于顶层同名值。
    source_updated_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """``/model-metrics`` 的运行特征点（口径 ``latest_valid_per_task``）。"""

    model: str
    effort: str
    average_agent_steps: float | None = None
    agent_steps_samples: int | None = None
    average_total_tokens: float | None = None
    token_samples: int | None = None
    cache_hit_rate: float | None = None
    cache_token_samples: int | None = None
    runs_24h: int = 0
    runs_48h: int = 0
    runs_total: int = 0

    @property
    def key(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass(frozen=True, slots=True)
class InsightPoint:
    """``/radar-insights`` 的综合 IQ 点。

    三个 IQ 必须一起给：视觉频道分数普遍更高是**频道难度差异**，不是「视觉更强」。
    """

    model: str
    effort: str
    iq: float
    software_iq: float | None
    visual_iq: float | None
    samples: int

    @property
    def key(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """趋势点。``/iq-history`` 的字段是 ``ts/score/n``，``/radar-insights`` 的是
    ``timestamp/iq/samples`` —— provider 负责把两者归一到这里。"""

    timestamp: str
    iq: float
    samples: int


@dataclass(frozen=True, slots=True)
class RecommendationItem:
    model: str
    effort: str
    iq: float
    #: 上游 ``passed`` 是浮点（任务等权 + 窗口加权的**加权通过数**），不是「过了多少题」。
    weighted_passed: float
    samples: int
    average_cost_usd: float | None = None
    average_duration_minutes: float | None = None
    combined_cost_index: float | None = None
    trend_48h: tuple[TrendPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class Recommendation:
    key: str
    title: str
    rule: str
    items: tuple[RecommendationItem, ...] = ()


@dataclass(frozen=True, slots=True)
class DegradationAlert:
    """降智预警条目。

    上游实测结构（pompeii 4 条 / deep-swe 0 条）与设计文档的字段名不同，provider 负责映射
    （``iq``→``current_iq``、``average_iq_24h``→``avg_24h``、``degradation_24h_iq``→
    ``delta_24h``、``degradation_severity_score``→``severity``）。只有 ``model`` / ``effort``
    是必需的，其余缺就为 ``None``。

    上游规则排除 DeepSeek，且只与自身历史比较 —— 插件只转发，**不本地重算**。
    """

    model: str
    effort: str
    current_iq: float | None = None
    avg_24h: float | None = None
    avg_48h: float | None = None
    delta_24h: float | None = None
    delta_48h: float | None = None
    severity: float | None = None
    average_cost_usd: float | None = None
    average_duration_minutes: float | None = None
    smooth_delta_24h: float | None = None
    peak_24h_iq: float | None = None
    peak_48h_iq: float | None = None
    trend_48h: tuple[TrendPoint, ...] = ()
    #: 上游给的原始键名（诊断用：上游改版时据此对齐映射，不丢信息）。
    raw_keys: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass(frozen=True, slots=True)
class ContributorRow:
    display_name: str
    github_login: str | None
    avatar_url: str | None
    submissions: int
    graded: int
    points: float
    month_points: float
    tokens: int
    folded_usd: float
    deepseek_api_usd: float
    #: 上游口径：``usd = folded_usd + deepseek_api_usd``。插件不自行加总。
    usd: float
    is_radar_admin: bool = False
    flag_race_winner: bool = False
    rank_change_24h: int | None = None


@dataclass(frozen=True, slots=True)
class RadarEvent:
    """一条判分流水。三个成本布尔必须原样透传。"""

    graded_at: str
    passed: bool
    score: float
    task_id: str
    model: str
    effort: str
    harness: str
    cost_usd: float | None = None
    cost_source: str | None = None
    cost_is_estimate: bool = False
    cost_is_fallback_estimate: bool = False
    cost_is_api_equivalent: bool = False
    points: float | None = None
    points_deferred: bool = False
    display_name: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class HistorySeries:
    """``/iq-history`` 的一条序列。

    ``key`` 有**三种形态**，口径不同且分数不同，不可混用：

    - ``模型``：跨档位合并（``effort=None``）
    - ``模型@effort``：单档位
    - ``latest:模型`` / ``latest:模型@effort``：另一套「最近一次有效运行」窗口
    """

    key: str
    model: str
    effort: str | None
    points: tuple[TrendPoint, ...] = ()
    #: 原始 key 是否带 ``latest:`` 前缀（口径标记，必须标注）。
    latest: bool = False
    #: 该序列是否来自陈旧缓存（上游不可用时的降级结果）。
    stale: bool = False


@dataclass(frozen=True, slots=True)
class FleetPulse:
    """``/leaderboard`` 的 ``pedal_speed``：全队提交口径的实时吞吐。"""

    window_minutes: int
    submitted_runs: int
    tokens_per_hour: int | None = None
    cache_hit_ratio: float | None = None
    api_equivalent_usd_per_hour: float | None = None
    usd_per_hour: float | None = None


@dataclass(frozen=True, slots=True)
class FlagRace:
    status: str
    target_usd: float | None
    current_usd: float | None
    reward_points: float | None = None
    winner_name: str | None = None
    winning_task_id: str | None = None
    winning_model: str | None = None
    winning_effort: str | None = None


# ── provider 返回的聚合 payload ──


@dataclass(frozen=True, slots=True)
class LeaderboardPayload:
    meta: RadarMeta
    models: tuple[ModelRow, ...] = ()
    tasks: tuple[str, ...] = ()
    contributors: tuple[ContributorRow, ...] = ()
    pulse: FleetPulse | None = None
    flag_race: FlagRace | None = None
    pending_grades: int | None = None
    error_grades: int | None = None
    online_volunteers: int | None = None


@dataclass(frozen=True, slots=True)
class TablePayload:
    meta: RadarMeta
    tasks: tuple[TaskInfo, ...] = ()
    cells: Mapping[str, CellState] = field(default_factory=dict)
    combos: tuple[ModelConfig, ...] = ()
    baseline_generated_at: str | None = None
    discrimination_generated_at: str | None = None


@dataclass(frozen=True, slots=True)
class InsightsPayload:
    meta: RadarMeta
    comprehensive_points: tuple[InsightPoint, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    degradation_alerts: tuple[DegradationAlert, ...] = ()
    degradation_rule: str = ""
    generated_at: str | None = None


@dataclass(frozen=True, slots=True)
class EfficiencyPayload:
    meta: RadarMeta
    points: tuple[EfficiencyPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class EventsPayload:
    """``/events`` 的流水（``/events`` 自身也带口径 envelope，故不需要额外拉榜单）。"""

    meta: RadarMeta
    events: tuple[RadarEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricsPayload:
    meta: RadarMeta
    points: tuple[MetricPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class QuotaPayload:
    meta: RadarMeta
    quota_window: str = ""
    source: str = ""
    measured_at: str | None = None
    updated_at: str | None = None
    tier_windows_usd: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SuggestCell:
    task_id: str
    model: str
    effort: str
    agent: str
    agent_version: str
    est_minutes: int | None = None
    est_quota_pct: float | None = None
    tier_windows_usd: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SuggestPayload:
    meta: RadarMeta
    cells: tuple[SuggestCell, ...] = ()
    holding: int | None = None
    replaceable_unstarted: int | None = None
    protected_started: int | None = None


# ── service 的派生结构（plugin_api §5.2） ──


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model: str
    variants: tuple[ModelRow, ...] = ()
    best: ModelRow | None = None
    efficiency: EfficiencyPoint | None = None
    metrics: MetricPoint | None = None
    insight: InsightPoint | None = None
    trend: tuple[TrendPoint, ...] = ()
    recent: tuple[RadarEvent, ...] = ()
    meta: RadarMeta | None = None

    @property
    def effort(self) -> str | None:
        return self.best.effort if self.best else (self.variants[0].effort if self.variants else None)


@dataclass(frozen=True, slots=True)
class Comparison:
    left: ModelProfile
    right: ModelProfile
    iq_delta: float | None = None
    pass_rate_delta: float | None = None
    cost_delta_usd: float | None = None
    duration_delta_minutes: float | None = None
    cost_index_delta: float | None = None
    meta: RadarMeta | None = None


@dataclass(frozen=True, slots=True)
class TaskDetail:
    task: TaskInfo
    cells: tuple[CellState, ...] = ()
    solved_by: tuple[CellState, ...] = ()
    meta: RadarMeta | None = None
