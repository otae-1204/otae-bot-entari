# AI 智商雷达插件 · 插件侧接口契约

> 这是**插件自身**的接口文档（Python 层契约），不是上游 API。
> 上游 API 见 [`ai_radar_interface.md`](ai_radar_interface.md)；字段释义见 [`ai_radar_data_dictionary.md`](ai_radar_data_dictionary.md)；框架见 [`ai_radar_plugin_framework.md`](ai_radar_plugin_framework.md)。
> **本阶段只定义契约，不含实现**。所有签名中的类型名对应 `plugins/radar/models.py` 的只读数据模型。

---

## 0. 约定

- 全部 IO 为 **async**（走共享 httpx 客户端）。
- 全部返回值为**只读数据模型**或**纯文本片段**；插件不返回图片、不返回 HTML。
- `effort` 省略时表示「该模型的全部档位」，比较类接口**必须**要求显式档位或自动取最高档并标注。
- 所有接口返回的模型都带一个 `meta: RadarMeta`，承载口径与数据时间。

### 0.1 `RadarMeta`（每个结果必带）

```python
@dataclass(frozen=True, slots=True)
class RadarMeta:
    benchmark_id: str          # deep-swe / pompeii-adjacency
    scoring_mode: str          # binary-majority / continuous-macro
    score_label: str           # Pass rate / Adjacency F1
    mode: str | None           # equal_latest_3 / latest_valid_per_task / rolling_equal_per_task
    rolling_window: int | None # 3
    pass_threshold: float | None
    source_updated_at: str | None   # 优先 point 级，其次顶层
    stale: bool                # 是否来自过期缓存
    fetched_at: str            # 插件取数时间（ISO8601）
```

---

## 1. `config.py`

```python
@dataclass(frozen=True, slots=True)
class RadarConfig:
    base_url: str = "https://api.codexradar.com"
    proxy: str | None = None          # 读 RADAR_PROXY；缺省继承全局代理策略
    timeout: float = 20.0             # 单请求超时（/table 8.6MB 需要更长，见 per-endpoint）
    default_benchmark: str = "deep-swe"
    max_models_listed: int = 15       # 排行榜默认展示条数
    max_tasks_listed: int = 10
    concurrency: int = 4              # 全局并发闸门（对齐 hyw 的 4）
    cache_ttl: RadarCacheTTL = ...    # 见下
    user_agent: str = ...             # 自有 UA，见 §1.1（不伪造 X-DRadar-* 头）
    retry: RadarRetry = ...           # 见 §1.2（对齐官方客户端策略）
```

```python
@dataclass(frozen=True, slots=True)
class RadarRetry:
    """退避参数**对齐官方客户端**（codex-radar/dradar v0.5.200 api_client.py），不要自行发明。

    - 429：最多 5 次重试（共 6 次尝试）
    - Retry-After：解析失败用默认值；取值 clamp 到 [min_wait, max_wait] = [1.0, 60.0] 秒
    - 等待 = clamp(retry_after) + uniform(0, min(1.0, retry_after * 0.1))  ← 抖动防羊群
    - 503 + 维护栅栏（deployment_maintenance）：Retry-After **不 clamp**，由 total_budget 裁决
    """
    rate_limit_retries: int = 5
    min_wait: float = 1.0
    max_wait: float = 60.0
    default_retry_after: float = 5.0
    jitter_ratio: float = 0.1
    total_budget: float = 360.0       # 维护栅栏场景的总预算
```

> 只读 GET 是**幂等**的，故重试安全（无重复副作用）。**不要**用连接级重试（`HTTPTransport(retries=2)`）：本机常年开代理，代理层重连会放大故障——官方客户端也是在检测到代理时禁用该重试的。

```python
@dataclass(frozen=True, slots=True)
class RadarCacheTTL:
    benchmarks: float = 3600.0
    leaderboard: float = 60.0
    table: float = 300.0
    iq_history: float = 120.0
    radar_insights: float = 600.0
    intelligence_efficiency: float = 60.0
    model_metrics: float = 60.0
    events: float = 30.0
    quota: float = 3600.0
```

> **TTL 下限规则**：插件 TTL **不得短于**上游 `Cache-Control` 的 `max-age`/`s-maxage`（上表已按实测值取齐或更长），否则等于用我们的请求量去替上游刷缓存。

### 1.1 User-Agent 与合规自律

- 使用**自有标识 UA**（如 `otae-bot-radar/<version> (+https://github.com/otae-1204/otae-bot-entari)`），便于上游识别与联系。
- **不伪造 `X-DRadar-Client-Version` / `X-DRadar-Capabilities` / `Authorization`**：伪装成官方客户端既不诚实也无必要（公开读端点免鉴权）。
- 不做高频轮询、不在无人使用时后台定时拉取、不代跑题、不写任何数据（见 framework §1.2 与 open_items A1）。

### 1.2 环境变量（写入 `.env.example` 的 `RADAR_*` 组）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RADAR_BASE_URL` | `https://api.codexradar.com` | 便于指向本地 `127.0.0.1:8399` 调试 |
| `RADAR_PROXY` | 空 | `direct` 或 `http://127.0.0.1:7890`；空 = 走全局策略 |
| `RADAR_TIMEOUT` | `20` | 秒 |
| `RADAR_TABLE_TIMEOUT` | `60` | `/table` 单独放宽（8.6MB） |
| `RADAR_DEFAULT_BENCHMARK` | `deep-swe` | |
| `RADAR_CONCURRENCY` | `4` | |

> **实现提醒**：数值型配置必须用 `_env(name, None)` + `is None` 判空，**不能**用 `int(str(_env(n,"") or "") or default)` —— 本仓库的 `.env` 加载器会把 `"0"` 经 `json.loads` 变成 int `0`，真值判断会把合法的 0 当未设置（`plugins/hyw/config.py` 的 `_env_int/_env_float` 即为修复后的写法）。

---

## 2. `errors.py`

```python
class RadarError(Exception):
    code: str            # 见下表
    message: str         # 面向用户的中文固定话术（不含上游原文）

class RadarSourceError(RadarError): ...    # 上游/网络问题
class RadarQueryError(RadarError): ...     # 参数/语义问题（本地可判）
```

| `code` | 触发 | 用户话术要点 |
| --- | --- | --- |
| `upstream_unavailable` | 5xx / 超时 / 连接失败 | 「雷达数据源暂时不可用，稍后重试」+ 是否有陈旧缓存 |
| `unknown_benchmark` | 上游 404 未知 benchmark | 列出可用 id |
| `unknown_model` | 本地别名解析失败 / 上游 `points: []` | 「没有该模型档位的实测数据」+ 候选列表 |
| `invalid_argument` | 本地校验失败（effort 非法、n 越界） | 指出合法取值 |
| `payload_too_large` | `/table` 超限/超时 | 提示改用轻量端点 |
| `schema_drift` | 必需字段缺失（上游演进） | 「数据源结构变化，请反馈」+ 记录原始键名 |
| `rate_limited` | 429 且重试耗尽 | 「数据源限流，请稍后再试」+ 尊重 `Retry-After`（**不**向用户暴露上游原文） |
| `private_path_refused` | 代码误触 `/api/private/*` | 属实现缺陷，直接抛（不应在正常路径出现） |

**规则**：上游响应体原文**不得**进入 `message`；只记入 debug 日志（脱敏）。

**429 / 5xx 处理顺序**（对齐 §1 的 `RadarRetry`）：

1. 429 → 读 `Retry-After`（缺失/不可解析用 `default_retry_after`）→ clamp `[1.0, 60.0]` → 加抖动 → 重试，最多 `rate_limit_retries` 次。
2. 503 且响应提示维护栅栏 → `Retry-After` **不 clamp**，总等待受 `total_budget` 约束。
3. 重试耗尽 → `rate_limited`，**若本地有陈旧缓存则优先回陈旧数据 + `meta.stale=True`**（见 §8 降级链）。
4. 其他 5xx / 超时 / 连接失败 → `upstream_unavailable`，同样优先回陈旧缓存。

---

## 3. `models.py`（只读模型）

```python
@dataclass(frozen=True, slots=True)
class BenchmarkInfo:
    id: str; title: str; short_title: str; description: str
    task_count: int; scoring_mode: str; score_label: str
    rolling_window: int; model_config_count: int
    reference_task_id: str | None; reference_url: str | None
    task_bundle: TaskBundle | None; default: bool

@dataclass(frozen=True, slots=True)
class TaskBundle:
    url: str; sha256: str; bytes: int; format: str

@dataclass(frozen=True, slots=True)
class TaskInfo:
    id: str; title: str; language: str; repo: str; category: str
    fragment_count: int | None; metric: str | None
    discrimination: Discrimination | None

@dataclass(frozen=True, slots=True)
class Discrimination:
    score: float; confidence: float; raw_score: float
    model: float; effort: float; monotonic: float; config: float
    threshold: float; cells: int; samples: int

@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str; effort: str

@dataclass(frozen=True, slots=True)
class ModelRow:
    model: str; effort: str
    graded: int; passed: int; score_sum: float
    cells: int; cells_passed: int; pass_rate: float
    iq: float | None                 # 由 pass_rate×150 换算，或取自 insights
    tasks: Mapping[str, TaskVote]    # task_id → 多数表决
    meta: RadarMeta

@dataclass(frozen=True, slots=True)
class TaskVote:
    votes: int; pass_votes: int; majority_pass: bool
    score_sum: float; score_rate: float

@dataclass(frozen=True, slots=True)
class RunRecord:                     # = 上游 ran_by 条目
    display_name: str                # login 或 nickname（二选一，已归一）
    avatar_url: str | None
    passed: bool; score: float; graded_at: str
    points_base: float; points_multiplier: float
    duration_sec: float; actual_cost_usd: float | None
    cost_source: str | None; cost_complete: bool | None
    token_pricing_version: str | None

@dataclass(frozen=True, slots=True)
class CellState:
    cell_id: str                     # "<task>|<model>|<effort>"
    task_id: str; model: str; effort: str
    st: str                          # open/cooldown/leased/running
    n: int; p: int; score_sum: float; rate: float
    total_n: int; total_p: int; total_score: float
    base_mult: float; wasteland: bool; wasteland_multiplier: float; mult: float
    last_graded_at: str | None; last_points: float | None
    cost: float | None; cost_src: str | None      # src != measured → 估算
    minutes: int | None
    ran_by: tuple[RunRecord, ...]

@dataclass(frozen=True, slots=True)
class EfficiencyPoint:
    model: str; effort: str
    iq: float; passed: int; total: int
    average_price_usd: float | None
    average_minutes: float | None
    combined_cost_index: float | None
    average_agent_steps: float | None; average_total_tokens: float | None
    cache_hit_rate: float | None
    runs_24h: int; runs_48h: int; runs_total: int
    source_updated_at: str | None

@dataclass(frozen=True, slots=True)
class MetricPoint:
    model: str; effort: str
    average_agent_steps: float | None; agent_steps_samples: int | None
    average_total_tokens: float | None; token_samples: int | None
    cache_hit_rate: float | None; cache_token_samples: int | None
    runs_24h: int; runs_48h: int; runs_total: int

@dataclass(frozen=True, slots=True)
class InsightPoint:                  # comprehensive_points
    model: str; effort: str
    iq: float; software_iq: float | None; visual_iq: float | None; samples: int

@dataclass(frozen=True, slots=True)
class Recommendation:
    key: str; title: str; rule: str
    items: tuple[RecommendationItem, ...]

@dataclass(frozen=True, slots=True)
class RecommendationItem:
    model: str; effort: str; iq: float
    weighted_passed: float           # 上游 passed 是浮点，勿当题数
    samples: int
    average_cost_usd: float | None; average_duration_minutes: float | None
    combined_cost_index: float | None
    trend_48h: tuple[TrendPoint, ...]

@dataclass(frozen=True, slots=True)
class TrendPoint:
    timestamp: str; iq: float; samples: int

@dataclass(frozen=True, slots=True)
class DegradationAlert:
    model: str; effort: str
    current_iq: float; avg_24h: float | None; avg_48h: float | None
    delta_24h: float | None; delta_48h: float | None
    severity: float | None

@dataclass(frozen=True, slots=True)
class ContributorRow:
    display_name: str; github_login: str | None; avatar_url: str | None
    submissions: int; graded: int
    points: float; month_points: float
    tokens: int
    folded_usd: float; deepseek_api_usd: float; usd: float
    is_radar_admin: bool; flag_race_winner: bool
    rank_change_24h: int | None

@dataclass(frozen=True, slots=True)
class RadarEvent:
    graded_at: str; passed: bool; score: float
    task_id: str; model: str; effort: str; harness: str
    cost_usd: float | None; cost_is_estimate: bool; cost_is_api_equivalent: bool
    points: float | None; points_deferred: bool
    display_name: str | None; avatar_url: str | None

@dataclass(frozen=True, slots=True)
class HistorySeries:
    key: str                         # "model" 或 "model@effort"
    model: str; effort: str | None   # 裸模型名时 effort=None（跨档位合并口径）
    points: tuple[TrendPoint, ...]

@dataclass(frozen=True, slots=True)
class FleetPulse:                    # 来自 leaderboard 的 pedal_speed/latest_burn
    window_minutes: int
    submitted_runs: int
    tokens_per_hour: int | None
    cache_hit_ratio: float | None
    api_equivalent_usd_per_hour: float | None

@dataclass(frozen=True, slots=True)
class FlagRace:
    status: str; target_usd: float; current_usd: float
    reward_points: float
    winner_name: str | None
    winning_task_id: str | None; winning_model: str | None; winning_effort: str | None
```

**注意**：`models.py` 里**没有** `Cell.cost` 的裸 float 语义——一律包成 `cost` + `cost_src` 两个字段，强制调用方处理「估算 vs 实测」。

---

## 4. `provider.py`（唯一出网层）

```python
class RadarClient:
    def __init__(self, config: RadarConfig, *, http=None) -> None: ...

    # —— 频道与任务 ——
    async def benchmarks(self) -> tuple[BenchmarkInfo, ...]
    async def tasks(self, benchmark: str | None = None) -> tuple[TaskInfo, ...]

    # —— 榜单 ——
    async def leaderboard(
        self, benchmark: str | None = None, *, view: str | None = None
    ) -> LeaderboardPayload

    # —— 洞察 ——
    async def insights(self, benchmark: str | None = None) -> InsightsPayload
    async def efficiency(self, benchmark: str | None = None) -> EfficiencyPayload
    async def model_metrics(
        self, *, model: str | None = None, effort: str | None = None,
        benchmark: str | None = None,
    ) -> MetricsPayload
    async def history(self, benchmark: str | None = None) -> tuple[HistorySeries, ...]

    # —— 流水与辅助 ——
    async def events(self, *, n: int = 30, benchmark: str | None = None) -> tuple[RadarEvent, ...]
    async def quota(self) -> QuotaPayload
    async def suggest(
        self, *, n: int = 5, benchmark: str | None = None,
        harness: str | None = None, replace_unstarted: bool = False,
    ) -> SuggestPayload
    async def table(self, benchmark: str | None = None) -> TablePayload   # 8.6MB，慎用
```

### 4.1 端点映射（唯一事实源）

| 方法 | HTTP | 缓存键 |
| --- | --- | --- |
| `benchmarks()` | `GET /api/v1/benchmarks` | `benchmarks` |
| `leaderboard(b)` | `GET /api/v1/leaderboard?benchmark={b}` | `leaderboard:{b}` |
| `table(b)` | `GET /api/v1/table?benchmark={b}` | `table:{b}` |
| `history(b)` | `GET /api/v1/iq-history?benchmark={b}` | `history:{b}` |
| `insights(b)` | `GET /api/v1/radar-insights?benchmark={b}` | `insights:{b}` |
| `efficiency(b)` | `GET /api/v1/intelligence-efficiency?benchmark={b}` | `efficiency:{b}` |
| `model_metrics(...)` | `GET /api/v1/model-metrics?model=&effort=&benchmark=` | `metrics:{b}:{m}:{e}` |
| `events(n,b)` | `GET /api/v1/events?n={n}&benchmark={b}` | `events:{b}:{n}` |
| `quota()` | `GET /api/v1/quota` | `quota` |
| `suggest(...)` | `GET /api/v1/suggest?n=&benchmark=&harness=&replace_unstarted=` | `suggest:{b}:{n}:{h}:{r}` |
| `tasks(b)` | 从 `table(b)` 的 `tasks` 取（**不单独发请求**） | 复用 `table:{b}` |

> `tasks()` 复用 `/table` 缓存：`/table` 虽大，但 300s TTL + 磁盘缓存后，「按题查询」与「题目清单」共用一份数据，避免两次 8.6MB。

### 4.2 归一化规则（provider 内）

1. `benchmark` 缺省 → `config.default_benchmark`。
2. 上游 404 → `RadarQueryError(code="unknown_benchmark")`。
3. 上游 422 → `RadarError(code="invalid_argument")`（本地应已拦下，此路为兜底）。
4. 未知 model 的 `points: []` → **不报错**，返回空元组，由 service 层转成 `unknown_model` 话术。
5. `ran_by` 的 `login`/`nickname` → 统一 `display_name`（`login` 优先）。
6. `cells` 从 dict 解析键 → `CellState`（拆出 `task_id`/`model`/`effort`）。
7. 缺失必需字段 → `RadarError(code="schema_drift")`，并把实际键名写入 debug 日志。
8. 超时/5xx → 有陈旧缓存则回陈旧数据 + `meta.stale=True`。

---

## 5. `service.py`（查询语义）

```python
class RadarService:
    def __init__(self, client: RadarClient, config: RadarConfig) -> None: ...

    # —— 榜单类 ——
    async def top_models(
        self, *, benchmark: str | None = None, by: Literal["iq", "pass_rate"] = "iq",
        limit: int = 10, effort: str | None = None,
    ) -> tuple[ModelRow, ...]

    async def model_profile(
        self, query: str, *, effort: str | None = None, benchmark: str | None = None,
    ) -> ModelProfile
    """query 支持口语别名（gpt6 / astra / sol / opus / glm flash / k3 …）。
    歧义 → raise RadarQueryError(code="unknown_model") 并带候选列表。"""

    async def compare(
        self, left: str, right: str, *, effort: str | None = None,
        benchmark: str | None = None,
    ) -> Comparison

    # —— 洞察类 ——
    async def recommendations(
        self, *, benchmark: str | None = None,
    ) -> tuple[Recommendation, ...]

    async def degradation_alerts(
        self, *, benchmark: str | None = None,
    ) -> tuple[DegradationAlert, ...]

    async def value_picks(
        self, *, benchmark: str | None = None, limit: int = 5,
        max_cost_usd: float | None = None, min_iq: float | None = None,
    ) -> tuple[EfficiencyPoint, ...]

    async def trend(
        self, model: str, *, effort: str | None = None, hours: int = 72,
        benchmark: str | None = None,
    ) -> tuple[TrendPoint, ...]
    """裸模型名 → 跨档位合并 series；带 effort → 单档位 series。两者不可混。"""

    # —— 题目类（走 /table）——
    async def task_detail(self, task_id: str, *, benchmark: str | None = None) -> TaskDetail

    async def task_ranking(
        self, *, benchmark: str | None = None, min_discrimination: float | None = None,
        limit: int = 10,
    ) -> tuple[TaskInfo, ...]

    async def who_solved(
        self, task_id: str, *, benchmark: str | None = None,
    ) -> tuple[CellState, ...]

    # —— 社区类 ——
    async def top_contributors(
        self, *, scope: Literal["month", "total"] = "month", limit: int = 10,
    ) -> tuple[ContributorRow, ...]

    async def fleet_pulse(self) -> FleetPulse
    async def flag_race(self) -> FlagRace
    async def recent_events(self, *, limit: int = 10, benchmark: str | None = None) -> tuple[RadarEvent, ...]

    # —— 元信息 ——
    async def benchmarks(self) -> tuple[BenchmarkInfo, ...]
    async def model_catalog(self, *, benchmark: str | None = None) -> tuple[ModelConfig, ...]
```

### 5.1 语义规则（实现必须遵守）

| 规则 | 说明 |
| --- | --- |
| **IQ 换算只在 service 层** | `iq = pass_rate * 100 * 1.5`；若上游 `/radar-insights` 已有 `iq`，**优先用上游值**，本地换算只作为缺失兜底并标注 `iq_derived=True`。 |
| **排序必须稳定** | `by="iq"` 时以 `iq` 降序、`samples` 降序、`model` 升序三级排序，避免同分抖动。 |
| **档位默认策略** | `effort=None` 时，`top_models` 返回**每个模型的最高档**（`ultra > max > xhigh > high > medium > low`）并在输出标注「已取各模型最高档」；`model_profile` 返回**全部档位**。 |
| **别名解析** | 别名表只做「口语 → 上游 id」映射，且必须容忍大小写/空格/连字符差异（`gpt-6 astra` / `gpt6astra` → `gpt-6-astra`）。匹配不唯一时报候选，**不做猜测性选取**。 |
| **`stale` 传播** | 任一数据来自陈旧缓存，最终 `meta.stale=True` 且 formatter 必须显示。 |
| **不跨 benchmark 混合** | `top_models` 只在单一 benchmark 内排序；`comprehensive`（综合 IQ）必须走 `/radar-insights` 并标注 `recommendation_mode`。 |
| **DeepSeek 预警豁免** | `degradation_alerts()` 直接转发上游结果，**不本地重算**，尤其不对 DeepSeek 系列补算。 |
| **空数据语义** | 空元组 + 对应 `meta`，由 formatter 输出「无数据」，**不抛异常**。 |

### 5.2 派生结构

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    model: str
    variants: tuple[ModelRow, ...]        # 各档位
    best: ModelRow | None
    efficiency: EfficiencyPoint | None
    metrics: MetricPoint | None
    insight: InsightPoint | None          # 含 software_iq / visual_iq
    trend: tuple[TrendPoint, ...]
    recent: tuple[RadarEvent, ...]
    meta: RadarMeta

@dataclass(frozen=True, slots=True)
class Comparison:
    left: ModelProfile
    right: ModelProfile
    iq_delta: float | None
    pass_rate_delta: float | None
    cost_delta_usd: float | None
    duration_delta_minutes: float | None
    cost_index_delta: float | None
    meta: RadarMeta

@dataclass(frozen=True, slots=True)
class TaskDetail:
    task: TaskInfo
    cells: tuple[CellState, ...]          # 该题所有档位
    solved_by: tuple[CellState, ...]      # rate>0 的
    meta: RadarMeta
```

---

## 6. `formatters.py`（纯文本，无渲染）

```python
def format_benchmark_list(items: tuple[BenchmarkInfo, ...]) -> list[str]
def format_model_list(rows: tuple[ModelRow, ...], meta: RadarMeta) -> list[str]
def format_model_profile(profile: ModelProfile) -> list[str]
def format_comparison(cmp: Comparison) -> list[str]
def format_recommendations(recs: tuple[Recommendation, ...], meta: RadarMeta) -> list[str]
def format_alerts(alerts: tuple[DegradationAlert, ...], meta: RadarMeta) -> list[str]
def format_value_picks(points: tuple[EfficiencyPoint, ...], meta: RadarMeta) -> list[str]
def format_trend(points: tuple[TrendPoint, ...], *, label: str) -> list[str]
def format_task_detail(detail: TaskDetail) -> list[str]
def format_contributors(rows: tuple[ContributorRow, ...], *, scope: str) -> list[str]
def format_events(events: tuple[RadarEvent, ...], meta: RadarMeta) -> list[str]
def format_meta_footer(meta: RadarMeta) -> str      # 统一「数据时间 + 口径 + 样本量」脚注
def format_error(error: RadarError) -> str
```

**格式约束**：

1. 每个函数返回 `list[str]`，每行 ≤ 40 个显示宽度（中文按 2 计），超长由调用方分段。
2. `format_meta_footer` 必须被所有展示函数调用，输出形如：

   ```
   —— DeepSWE · Pass rate 口径 · 最近 3 次有效运行 · 数据 2026-09-13 17:12 · 样本 135
   ```

3. 成本数字格式：`$1.98`（两位小数）；`src != measured` 时前缀 `~`（如 `~$0.82 估算`）。
4. 缺失值统一 `—`，**不用 0 代替**。
5. 不含 Markdown 表格（群聊等宽不成立），用「名次. 模型[档位] IQ 98.5 · 通过 68.2% · n=135 · $1.98」这类单行结构。

---

## 7. `handlers.py`（命令面）

沿用 `plugins/endfield/handlers.py` 的 `on_alconna(Alconna([...别名], Args["rest;?", MultiVar(AnyString)]))` 形态。

| 命令 | 别名 | 参数 | 调用的 service |
| --- | --- | --- | --- |
| `/radar` | `/雷达`、`/智商雷达` | 无 → 帮助 | — |
| `/radar 榜 [频道]` | `/radar rank` | 可选 benchmark | `top_models` |
| `/radar 模型 <名> [档位]` | `/radar model` | 名称 + 可选 effort | `model_profile` |
| `/radar 对比 <A> <B> [档位]` | `/radar vs` | 两个名称 | `compare` |
| `/radar 推荐 [频道]` | `/radar rec` | 可选 benchmark | `recommendations` |
| `/radar 预警 [频道]` | `/radar alert` | 可选 benchmark | `degradation_alerts` |
| `/radar 性价比 [频道]` | `/radar value` | 可选 benchmark | `value_picks` |
| `/radar 趋势 <名> [档位]` | `/radar trend` | 名称 + 可选 effort | `trend` |
| `/radar 题 <id 或关键词>` | `/radar task` | 题目查询 | `task_detail` |
| `/radar 好题 [频道]` | `/radar tasks` | 可选 benchmark | `task_ranking` |
| `/radar 贡献者 [月榜\|总榜]` | `/radar top` | 可选 scope | `top_contributors` |
| `/radar 流水 [N]` | `/radar feed` | N ≤ 50 | `recent_events` |
| `/radar 实时` | `/radar pulse` | 无 | `fleet_pulse` + `flag_race` |
| `/radar 频道` | `/radar bench` | 无 | `benchmarks` |
| `/radar 档位 [频道]` | `/radar combos` | 可选 benchmark | `model_catalog` |

> **实现注记（2026-09-14，用户要求）**：上表是设计阶段的完整命令面；实现时命令面**收窄为「只看智商相关」**，只保留 10 个——榜 / 模型 / 对比 / 推荐 / 预警 / 性价比 / 趋势 + 频道 / 档位 / 帮助。
> 被摘掉的 5 个（题 / 好题 / 贡献者 / 流水 / 实时）**只摘命令面**：`service.py` / `provider.py` / `formatters.py` 里的对应能力（`task_detail` / `task_ranking` / `top_contributors` / `recent_events` / `fleet_pulse` / `flag_race` / `who_solved`）**原样保留**，供前端经 `RadarService` 取数。
> 另：根命令别名实现为 `/radar`、`/智商雷达`（`/雷达` 归 `tibo_radar`，见 `otae_bot/group_features.py`）。差异明细见 `docs/ai_radar_frontend_api.md` §19。

**handler 职责边界**：

- 只做：参数解析 → 调用 service → `formatters` → `session.send` 分段（≤2000 字/段，与 hyw 一致）。
- 全局并发闸门（`config.concurrency`，超出回「雷达当前较忙」）。
- 单命令总超时（建议 30s，`/table` 类放宽到 60s）。
- **不缓存结果**（缓存已在 provider 层）。
- **不注册任何定时推送**（本阶段无订阅/推送需求；若要加，属新阶段）。

---

## 8. 依赖与测试契约

### 8.1 依赖

- 仅用仓库已有：`httpx`（经共享客户端）、`arclet.alconna`、`arclet.entari`、`loguru`。
- **不新增第三方依赖**（不需要 pandas/matplotlib/任何图表库 —— 本阶段无渲染）。

### 8.2 测试（`tests/test_radar.py`，标准库 unittest）

| 组 | 用例要点 |
| --- | --- |
| `ProviderParsingTests` | 用录制的 JSON 夹具（`tests/fixtures/radar/*.json`）验证：`cells` dict → `CellState`；`ran_by` 两种身份都归一到 `display_name`；`src != measured` 的 `cost_src` 保留；缺失字段 → `schema_drift` |
| `ProviderErrorTests` | 404 → `unknown_benchmark`；422 → `invalid_argument`；5xx + 陈旧缓存 → `stale=True`；未知 model 的 `points: []` → 空元组不报错 |
| `ServiceRankingTests` | IQ 排序三级稳定；`effort=None` 取各模型最高档；跨 benchmark 不混排 |
| `ServiceAliasTests` | `gpt6astra` / `gpt-6 astra` / `GPT-6-Astra` 归一到 `gpt-6-astra`；歧义报候选不猜 |
| `FormatterTests` | 脚注必含口径与数据时间；缺失值输出 `—`；估算成本带 `~`；每行宽度上限 |
| `HandlerTests` | 假 Session 驱动命令；并发闸门；分段 ≤2000 字 |
| `SchemaDriftTests` | 夹具删掉一个必需键 → 抛 `schema_drift` 而非 `KeyError` |

**夹具要求**：必须来自真实响应（可用 `%TEMP%\radar\deep_*.json` 里的实测样本裁剪），且**脱敏**——上游 payload 含 GitHub 用户名与头像 URL，夹具里应替换为合成值（沿用本仓库既有约定：夹具不留真实身份材料）。

### 8.3 实现时不得破坏的既有约束

- 不改 `otae_bot/infrastructure/http/client.py` 的公共签名（只新增 `namespace="radar"` 用法）。
- 不改 `plugins/hyw`、`plugins/endfield` 任何文件。
- `.env.example` 新增 `RADAR_*` 键后，`tests/test_hyw_evidence.py` 的「`.env.example` 覆盖 config 读取的全部变量」断言模式（正则 `^((?:HYW|GOOGLE)_[A-Z0-9_]+)=`）**不覆盖 `RADAR_`**，不会误伤；但若未来扩展该测试，应同步把 `RADAR_` 纳入。
