"""AI 智商雷达的**唯一出网层**。

职责边界（框架 §2.1）：只做 HTTP、超时、重试、缓存、错误码归一与「上游 JSON → 只读模型」
的字段搬运。**不做**业务筛选、排序、IQ 换算、文案拼装（那些在 ``service`` / ``formatters``）。

四条纪律：

1. **只读 A 面**：``api.codexradar.com/api/v1/*``。``/api/private/*`` 与全部写侧端点
   （``assignment`` / ``runner`` / ``submissions`` / ``run-plans`` / ``feedback``）不实现，
   并且 ``_url()`` 会对 ``/api/private/`` 直接抛 :class:`PrivatePathRefused`。
2. **不伪造身份**：只发自有 UA（``otae-bot-radar/<version>``），不发
   ``X-DRadar-Client-Version`` / ``X-DRadar-Capabilities`` / ``Authorization``。
3. **口径只搬运不改造**：``mode`` / ``scoring_mode`` / ``score_label`` / ``src`` /
   ``token_pricing_version`` / 三个 cost 布尔全部原样透传。
4. **缺字段是 schema_drift，不是 KeyError**：必需字段缺失 → :class:`SchemaDrift`，
   实际键名只写 debug 日志（见 :func:`_need`）。

缓存分两层，各自解决不同问题：

- 共享 HTTP 层（``fetch_json``）的内存池：API 池预算 8 MiB，而 ``/table`` 原始 9.3 MB、
  解析后约 23 MB —— **共享层缓存不住它**（会记 ``oversized`` 并每次真发请求）。
- 本模块的 **payload 缓存**：缓存的是**解析后的只读模型**（比冻结 JSON 树紧凑得多），
  因此既能省掉重复的 9.3 MB 拉取，又给「上游挂了 → 回陈旧数据 + ``meta.stale=True``」
  提供了落点（共享层明确不提供 stale-on-error，见 ``disk.py`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, TypeVar

import httpx
from loguru import logger

from otae_bot.infrastructure.http.client import fetch_json

from .config import RadarConfig
from .errors import (
    PrivatePathRefused,
    RadarSourceError,
    schema_drift,
    upstream_failure,
)
from .models import (
    BenchmarkInfo,
    CellState,
    ContributorRow,
    DegradationAlert,
    Discrimination,
    EfficiencyPayload,
    EfficiencyPoint,
    EventsPayload,
    FlagRace,
    FleetPulse,
    HistorySeries,
    InsightPoint,
    InsightsPayload,
    LeaderboardPayload,
    MetricPoint,
    MetricsPayload,
    ModelConfig,
    ModelRow,
    QuotaPayload,
    RadarEvent,
    RadarMeta,
    Recommendation,
    RecommendationItem,
    RunRecord,
    SuggestCell,
    SuggestPayload,
    TablePayload,
    TaskBundle,
    TaskInfo,
    TaskVote,
    TrendPoint,
    now_iso,
)

#: 共享 HTTP 层的缓存命名空间。与 UA 一起构成缓存键的一部分。
NAMESPACE = "radar"

#: 上游公开只读端点的公共前缀。
API_PREFIX = "/api/v1"

#: 上游明确禁止访问的前缀（文档 §16 红线 13）。
_PRIVATE_PREFIX = "/api/private/"

#: payload 缓存的条目上限。``/table`` 一条就很大，故限制得比较紧。
_CACHE_MAX_ENTRIES = 8

#: 过期之后仍允许用来「回陈旧数据」的宽限期（秒）。
_STALE_GRACE_SECONDS = 900.0

#: 上游 ``/iq-history`` 的 ``latest:`` key 前缀（另一套窗口口径，必须标注）。
LATEST_PREFIX = "latest:"

T = TypeVar("T")


# ── 字段访问助手：缺必需字段一律 schema_drift ──


def _need(mapping: object, key: str, endpoint: str) -> Any:
    """取必需字段。缺失 → :class:`SchemaDrift`（并把实际键名写 debug 日志）。"""
    if not isinstance(mapping, Mapping):
        raise schema_drift(endpoint, "<object>", got=mapping)
    if key not in mapping:
        logger.debug(
            "[radar] {} is missing required key {!r}; available keys: {}",
            endpoint,
            key,
            sorted(str(name) for name in mapping),
        )
        raise schema_drift(endpoint, key)
    return mapping[key]


def _opt(mapping: object, key: str, default: Any = None) -> Any:
    if isinstance(mapping, Mapping):
        value = mapping.get(key)
        return default if value is None else value
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any, default: bool = False) -> bool:
    return default if value is None else bool(value)


def _seq(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float_map(value: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, raw in _dict(value).items():
        number = _num(raw)
        if number is not None:
            out[str(key)] = number
    return out


# ── 解析函数（纯函数，便于用夹具单测） ──


def _parse_bundle(raw: Any) -> TaskBundle | None:
    if not isinstance(raw, Mapping):
        return None
    return TaskBundle(
        url=_text(raw.get("url")),
        sha256=_text(raw.get("sha256")),
        bytes=_int(raw.get("bytes")),
        format=_text(raw.get("format")),
    )


def parse_benchmarks(data: Mapping[str, Any], *, endpoint: str = "benchmarks") -> tuple[BenchmarkInfo, ...]:
    """解析 ``/benchmarks``。

    注意 ``reference_task_id`` / ``reference_url`` / ``task_bundle`` 对某些频道整体为
    ``null``（实测 deep-swe 三项皆 None），是正常形态而非缺字段。
    """
    out = []
    for raw in _seq(_need(data, "benchmarks", endpoint)):
        out.append(
            BenchmarkInfo(
                id=_text(_need(raw, "id", endpoint)),
                title=_text(_opt(raw, "title")),
                short_title=_text(_opt(raw, "short_title")),
                description=_text(_opt(raw, "description")),
                task_count=_int(_opt(raw, "task_count"), 0) or 0,
                scoring_mode=_text(_opt(raw, "scoring_mode")),
                score_label=_text(_opt(raw, "score_label")),
                rolling_window=_int(_opt(raw, "rolling_window"), 0) or 0,
                model_config_count=_int(_opt(raw, "model_config_count"), 0) or 0,
                reference_task_id=_opt(raw, "reference_task_id"),
                reference_url=_opt(raw, "reference_url"),
                task_bundle=_parse_bundle(_opt(raw, "task_bundle")),
                default=_flag(_opt(raw, "default")),
            )
        )
    return tuple(out)


def parse_discrimination(raw: Any) -> Discrimination | None:
    """解析题目区分度。**按存在性**：pompeii 频道实测没有这个字段。"""
    if not isinstance(raw, Mapping):
        return None
    return Discrimination(
        score=_num(raw.get("score"), 0.0) or 0.0,
        confidence=_num(raw.get("confidence"), 0.0) or 0.0,
        raw_score=_num(raw.get("raw_score"), 0.0) or 0.0,
        model=_num(raw.get("model"), 0.0) or 0.0,
        effort=_num(raw.get("effort"), 0.0) or 0.0,
        monotonic=_num(raw.get("monotonic"), 0.0) or 0.0,
        config=_num(raw.get("config"), 0.0) or 0.0,
        threshold=_num(raw.get("threshold"), 0.0) or 0.0,
        cells=_int(raw.get("cells"), 0) or 0,
        samples=_int(raw.get("samples"), 0) or 0,
    )


def parse_task(raw: Mapping[str, Any], *, endpoint: str) -> TaskInfo:
    """解析一条题目。两频道键集不同（deep-swe 有 ``discrimination``，pompeii 有
    ``fragment_count``/``metric``），因此可选字段全部按存在性解析。"""
    return TaskInfo(
        id=_text(_need(raw, "id", endpoint)),
        title=_text(_opt(raw, "title")),
        language=_text(_opt(raw, "language")),
        repo=_text(_opt(raw, "repo")),
        category=_text(_opt(raw, "category")),
        fragment_count=_int(_opt(raw, "fragment_count")),
        metric=_opt(raw, "metric"),
        discrimination=parse_discrimination(_opt(raw, "discrimination")),
    )


def parse_task_vote(raw: Any) -> TaskVote:
    return TaskVote(
        votes=_int(_opt(raw, "votes"), 0) or 0,
        pass_votes=_int(_opt(raw, "pass_votes"), 0) or 0,
        majority_pass=_flag(_opt(raw, "majority_pass")),
        score_sum=_num(_opt(raw, "score_sum"), 0.0) or 0.0,
        score_rate=_num(_opt(raw, "score_rate"), 0.0) or 0.0,
    )


def parse_model_row(raw: Mapping[str, Any], *, endpoint: str, meta: RadarMeta) -> ModelRow:
    """解析榜单一行。

    ``iq`` **不在这里算**：上游 ``pass_rate`` 与 ``/radar-insights`` 的 ``iq`` 是两个不同
    口径，换算与优先级只由 ``service`` 决定（plugin_api §5.1）。
    """
    return ModelRow(
        model=_text(_need(raw, "model", endpoint)),
        effort=_text(_need(raw, "effort", endpoint)),
        graded=_int(_opt(raw, "graded"), 0) or 0,
        passed=_int(_opt(raw, "passed"), 0) or 0,
        score_sum=_num(_opt(raw, "score_sum"), 0.0) or 0.0,
        cells=_int(_opt(raw, "cells"), 0) or 0,
        cells_passed=_int(_opt(raw, "cells_passed"), 0) or 0,
        pass_rate=_num(_opt(raw, "pass_rate"), 0.0) or 0.0,
        tasks={str(k): parse_task_vote(v) for k, v in _dict(_opt(raw, "tasks")).items()},
        meta=meta,
    )


def parse_run(raw: Mapping[str, Any]) -> RunRecord:
    """解析一条 ``ran_by``。

    ``login`` 与 ``nickname`` 二选一，归一到 ``display_name``（``login`` 优先）。
    ``token_pricing_version`` 必须保留：同一格子里不同 run 的价表版本可能不同，
    历史成本不得用最新价表重算（数据字典 §5）。
    """
    return RunRecord(
        display_name=_text(_opt(raw, "login") or _opt(raw, "nickname")),
        avatar_url=_opt(raw, "avatar_url"),
        passed=_flag(_opt(raw, "passed")),
        score=_num(_opt(raw, "score"), 0.0) or 0.0,
        graded_at=_text(_opt(raw, "graded_at")),
        points_base=_num(_opt(raw, "points_base")),
        points_multiplier=_num(_opt(raw, "points_multiplier")),
        duration_sec=_num(_opt(raw, "duration_sec")),
        actual_cost_usd=_num(_opt(raw, "actual_cost_usd")),
        cost_source=_opt(raw, "cost_source"),
        cost_complete=_opt(raw, "cost_complete"),
        token_pricing_version=_opt(raw, "token_pricing_version")
        or _opt(raw, "settled_token_pricing_version"),
    )


def parse_cell(cell_id: str, raw: Mapping[str, Any], *, endpoint: str) -> CellState:
    """解析一个格子。

    ``cell_id`` 形如 ``"<task_id>|<model>|<effort>"``（``/table`` 的 dict 键）。
    ``src`` 与 ``cost`` 成对保留：``src != measured`` 时成本是估算，展示必须带 ``~``。
    """
    parts = str(cell_id).split("|")
    if len(parts) != 3:
        logger.debug("[radar] {} unexpected cell key shape: {!r}", endpoint, cell_id)
        raise schema_drift(endpoint, "cells[<task>|<model>|<effort>]")
    task_id, model, effort = parts
    return CellState(
        cell_id=str(cell_id),
        task_id=task_id,
        model=model,
        effort=effort,
        st=_text(_opt(raw, "st")),
        n=_int(_opt(raw, "n"), 0) or 0,
        p=_int(_opt(raw, "p"), 0) or 0,
        score_sum=_num(_opt(raw, "score_sum"), 0.0) or 0.0,
        rate=_num(_opt(raw, "rate"), 0.0) or 0.0,
        total_n=_int(_opt(raw, "total_n"), 0) or 0,
        total_p=_int(_opt(raw, "total_p"), 0) or 0,
        total_score=_num(_opt(raw, "total_score"), 0.0) or 0.0,
        base_mult=_num(_opt(raw, "base_mult"), 0.0) or 0.0,
        wasteland=_flag(_opt(raw, "wasteland")),
        wasteland_multiplier=_num(_opt(raw, "wasteland_multiplier"), 0.0) or 0.0,
        mult=_num(_opt(raw, "mult"), 0.0) or 0.0,
        last_graded_at=_opt(raw, "last_graded_at"),
        last_points=_num(_opt(raw, "last_points")),
        cost=_num(_opt(raw, "cost")),
        cost_src=_opt(raw, "src"),
        minutes=_int(_opt(raw, "min")),
        ran_by=tuple(parse_run(run) for run in _seq(_opt(raw, "ran_by"))),
    )


def parse_contributor(raw: Mapping[str, Any]) -> ContributorRow:
    """解析一条贡献者记录。

    ``usd`` 直接用上游值（上游口径 ``folded_usd + deepseek_api_usd``）——插件不自行加总，
    避免与上游口径漂移。
    """
    return ContributorRow(
        display_name=_text(_opt(raw, "nickname") or _opt(raw, "github_login")),
        github_login=_opt(raw, "github_login"),
        avatar_url=_opt(raw, "avatar_url"),
        submissions=_int(_opt(raw, "submissions"), 0) or 0,
        graded=_int(_opt(raw, "graded"), 0) or 0,
        points=_num(_opt(raw, "points"), 0.0) or 0.0,
        month_points=_num(_opt(raw, "month_points"), 0.0) or 0.0,
        tokens=_int(_opt(raw, "tokens"), 0) or 0,
        folded_usd=_num(_opt(raw, "folded_usd"), 0.0) or 0.0,
        deepseek_api_usd=_num(_opt(raw, "deepseek_api_usd"), 0.0) or 0.0,
        usd=_num(_opt(raw, "usd"), 0.0) or 0.0,
        is_radar_admin=_flag(_opt(raw, "is_radar_admin")),
        flag_race_winner=_flag(_opt(raw, "flag_race_winner")),
        rank_change_24h=_int(_opt(raw, "rank_change_24h")),
    )


def parse_pulse(raw: Any) -> FleetPulse | None:
    """解析 ``pedal_speed``。``scope`` 是**全队提交**口径，不是单模型。"""
    if not isinstance(raw, Mapping):
        return None
    return FleetPulse(
        window_minutes=_int(_opt(raw, "window_minutes"), 0) or 0,
        submitted_runs=_int(_opt(raw, "submitted_runs"), 0) or 0,
        tokens_per_hour=_int(_opt(raw, "tokens_per_hour")),
        cache_hit_ratio=_num(_opt(raw, "cache_hit_ratio")),
        api_equivalent_usd_per_hour=_num(_opt(raw, "api_equivalent_usd_per_hour")),
        usd_per_hour=_num(_opt(raw, "usd_per_hour")),
    )


def parse_flag_race(raw: Any) -> FlagRace | None:
    if not isinstance(raw, Mapping):
        return None
    winner = _dict(_opt(raw, "winner"))
    submission = _dict(_opt(raw, "submission"))
    return FlagRace(
        status=_text(_opt(raw, "status")),
        target_usd=_num(_opt(raw, "target_usd")),
        current_usd=_num(_opt(raw, "current_usd")),
        reward_points=_num(_opt(raw, "reward_points")),
        # winner.github_login 实测可能为 null，nickname 也可能缺 —— 都按存在性解析。
        winner_name=_opt(winner, "nickname") or _opt(winner, "github_login"),
        winning_task_id=_opt(submission, "task_id"),
        winning_model=_opt(submission, "model"),
        winning_effort=_opt(submission, "effort"),
    )


def parse_leaderboard(data: Mapping[str, Any], *, meta: RadarMeta) -> LeaderboardPayload:
    endpoint = "leaderboard"
    models = tuple(
        parse_model_row(raw, endpoint=endpoint, meta=meta)
        for raw in _seq(_need(data, "models", endpoint))
    )
    return LeaderboardPayload(
        meta=meta,
        models=models,
        # 上游 tasks 是**扁平 task-id 字符串列表**，不是对象数组。
        tasks=tuple(_text(item) for item in _seq(_opt(data, "tasks"))),
        contributors=tuple(parse_contributor(raw) for raw in _seq(_opt(data, "contributors"))),
        pulse=parse_pulse(_opt(data, "pedal_speed")),
        flag_race=parse_flag_race(_opt(data, "flag_race")),
        pending_grades=_int(_opt(data, "pending_grades")),
        error_grades=_int(_opt(data, "error_grades")),
        online_volunteers=_int(_opt(data, "online_volunteers")),
    )


def parse_table(data: Mapping[str, Any], *, meta: RadarMeta) -> TablePayload:
    endpoint = "table"
    raw_cells = _need(data, "cells", endpoint)
    if not isinstance(raw_cells, Mapping):
        raise schema_drift(endpoint, "cells", got=raw_cells)
    cells = {
        str(cell_id): parse_cell(str(cell_id), raw, endpoint=endpoint)
        for cell_id, raw in raw_cells.items()
    }
    combos = tuple(
        ModelConfig(model=_text(_need(raw, "model", endpoint)), effort=_text(_need(raw, "effort", endpoint)))
        for raw in _seq(_need(data, "combos", endpoint))
    )
    return TablePayload(
        meta=meta,
        tasks=tuple(parse_task(raw, endpoint=endpoint) for raw in _seq(_need(data, "tasks", endpoint))),
        cells=cells,
        combos=combos,
        baseline_generated_at=_opt(data, "baseline_generated_at"),
        discrimination_generated_at=_opt(data, "discrimination_generated_at"),
    )


def parse_trend_point(raw: Mapping[str, Any]) -> TrendPoint:
    """归一趋势点。

    上游两个端点字段名不同：``/iq-history`` 是 ``{ts, score, n}``，
    ``/radar-insights`` 的 ``trend_48h`` 是 ``{timestamp, iq, samples}``。
    """
    timestamp = _opt(raw, "timestamp") or _opt(raw, "ts")
    value = _num(_opt(raw, "iq"))
    if value is None:
        value = _num(_opt(raw, "score"))
    samples = _int(_opt(raw, "samples"))
    if samples is None:
        samples = _int(_opt(raw, "n"))
    return TrendPoint(
        timestamp=_text(timestamp),
        iq=value if value is not None else 0.0,
        samples=samples if samples is not None else 0,
    )


def parse_history(data: Mapping[str, Any]) -> tuple[HistorySeries, ...]:
    """解析 ``/iq-history``：顶层是**扁平 dict**（无 envelope），键即序列名。

    三种键形态口径不同、分数不同，必须原样保留并标注：
    裸模型名（跨档位合并）、``模型@effort``（单档位）、``latest:`` 前缀（另一套窗口）。
    """
    series = []
    for key, points in data.items():
        name = str(key)
        latest = name.startswith(LATEST_PREFIX)
        if latest:
            name = name[len(LATEST_PREFIX) :]
        model, _, effort = name.rpartition("@")
        if not model:
            model, effort = name, ""
        series.append(
            HistorySeries(
                key=str(key),
                model=model,
                effort=effort or None,
                points=tuple(parse_trend_point(raw) for raw in _seq(points)),
                latest=latest,
            )
        )
    return tuple(series)


def parse_insight_point(raw: Mapping[str, Any], *, endpoint: str) -> InsightPoint:
    """``comprehensive_points`` 一项。三个 IQ 必须一起给（视觉分数高是频道难度差异）。"""
    return InsightPoint(
        model=_text(_need(raw, "model", endpoint)),
        effort=_text(_need(raw, "effort", endpoint)),
        iq=_num(_opt(raw, "iq"), 0.0) or 0.0,
        software_iq=_num(_opt(raw, "software_iq")),
        visual_iq=_num(_opt(raw, "visual_iq")),
        samples=_int(_opt(raw, "samples"), 0) or 0,
    )


def parse_recommendation(raw: Mapping[str, Any], *, endpoint: str) -> Recommendation:
    items = []
    for item in _seq(_opt(raw, "items")):
        items.append(
            RecommendationItem(
                model=_text(_need(item, "model", endpoint)),
                effort=_text(_need(item, "effort", endpoint)),
                iq=_num(_opt(item, "iq"), 0.0) or 0.0,
                # 上游 passed 是浮点（加权通过数），不是「过了多少题」。
                weighted_passed=_num(_opt(item, "passed"), 0.0) or 0.0,
                samples=_int(_opt(item, "samples"), 0) or 0,
                average_cost_usd=_num(_opt(item, "average_cost_usd")),
                average_duration_minutes=_num(_opt(item, "average_duration_minutes")),
                combined_cost_index=_num(_opt(item, "combined_cost_index")),
                trend_48h=tuple(
                    parse_trend_point(point) for point in _seq(_opt(item, "trend_48h"))
                ),
            )
        )
    return Recommendation(
        key=_text(_opt(raw, "key")),
        title=_text(_opt(raw, "title")),
        rule=_text(_opt(raw, "rule")),
        items=tuple(items),
    )


def parse_alert(raw: Mapping[str, Any], *, endpoint: str) -> DegradationAlert:
    """解析一条降智预警。

    上游实测结构（pompeii 4 条，deep-swe 0 条）字段名与设计文档的模型名不同，这里做映射：
    ``iq``→``current_iq``、``average_iq_24h``→``avg_24h``、``average_iq_48h``→``avg_48h``、
    ``degradation_24h_iq``→``delta_24h``、``degradation_48h_iq``→``delta_48h``、
    ``degradation_severity_score``→``severity``。其余字段按存在性解析，未知键记入 ``raw_keys``
    —— 这样上游改版时不会 KeyError，也不会静默丢掉信息。
    """
    return DegradationAlert(
        model=_text(_need(raw, "model", endpoint)),
        effort=_text(_need(raw, "effort", endpoint)),
        current_iq=_num(_opt(raw, "iq")),
        avg_24h=_num(_opt(raw, "average_iq_24h")),
        avg_48h=_num(_opt(raw, "average_iq_48h")),
        delta_24h=_num(_opt(raw, "degradation_24h_iq")),
        delta_48h=_num(_opt(raw, "degradation_48h_iq")),
        severity=_num(_opt(raw, "degradation_severity_score")),
        average_cost_usd=_num(_opt(raw, "average_cost_usd")),
        average_duration_minutes=_num(_opt(raw, "average_duration_minutes")),
        smooth_delta_24h=_num(_opt(raw, "smooth_degradation_24h_iq")),
        peak_24h_iq=_num(_opt(raw, "from_24h_high_iq")),
        peak_48h_iq=_num(_opt(raw, "from_48h_high_iq")),
        trend_48h=tuple(parse_trend_point(point) for point in _seq(_opt(raw, "trend_48h"))),
        raw_keys=tuple(sorted(str(name) for name in raw)),
    )


def parse_insights(data: Mapping[str, Any], *, meta: RadarMeta) -> InsightsPayload:
    endpoint = "insights"
    alerts = _dict(_opt(data, "degradation_alerts"))
    return InsightsPayload(
        meta=meta,
        comprehensive_points=tuple(
            parse_insight_point(raw, endpoint=endpoint)
            for raw in _seq(_opt(data, "comprehensive_points"))
        ),
        recommendations=tuple(
            parse_recommendation(raw, endpoint=endpoint)
            for raw in _seq(_opt(data, "recommendations"))
        ),
        degradation_alerts=tuple(
            parse_alert(raw, endpoint=endpoint) for raw in _seq(_opt(alerts, "items"))
        ),
        degradation_rule=_text(_opt(alerts, "rule")),
        generated_at=_opt(data, "generated_at"),
    )


def parse_efficiency_point(raw: Mapping[str, Any], *, endpoint: str) -> EfficiencyPoint:
    """``/intelligence-efficiency`` 一项。

    ``passed``/``total`` 原样保留为浮点：连续制频道下它是 F1 加权和（实测
    ``49.79059751561299 / 55``），不是「过了多少题」。
    """
    return EfficiencyPoint(
        model=_text(_need(raw, "model", endpoint)),
        effort=_text(_need(raw, "effort", endpoint)),
        iq=_num(_opt(raw, "iq")),
        passed=_num(_opt(raw, "passed")),
        total=_num(_opt(raw, "total")),
        average_price_usd=_num(_opt(raw, "average_price_usd")),
        average_minutes=_num(_opt(raw, "average_minutes")),
        combined_cost_index=_num(_opt(raw, "combined_cost_index")),
        average_agent_steps=_num(_opt(raw, "average_agent_steps")),
        agent_steps_samples=_int(_opt(raw, "agent_steps_samples")),
        average_total_tokens=_num(_opt(raw, "average_total_tokens")),
        token_samples=_int(_opt(raw, "token_samples")),
        cache_hit_rate=_num(_opt(raw, "cache_hit_rate")),
        cache_token_samples=_int(_opt(raw, "cache_token_samples")),
        runs_24h=_int(_opt(raw, "runs_24h"), 0) or 0,
        runs_48h=_int(_opt(raw, "runs_48h"), 0) or 0,
        runs_total=_int(_opt(raw, "runs_total"), 0) or 0,
        # point 级数据时间优先于顶层同名值（两者实测可不同）。
        source_updated_at=_opt(raw, "source_updated_at"),
    )


def parse_efficiency(data: Mapping[str, Any], *, meta: RadarMeta) -> EfficiencyPayload:
    endpoint = "efficiency"
    return EfficiencyPayload(
        meta=meta,
        points=tuple(
            parse_efficiency_point(raw, endpoint=endpoint)
            for raw in _seq(_need(data, "points", endpoint))
        ),
    )


def parse_metrics(data: Mapping[str, Any], *, meta: RadarMeta) -> MetricsPayload:
    endpoint = "model-metrics"
    points = []
    for raw in _seq(_need(data, "points", endpoint)):
        points.append(
            MetricPoint(
                model=_text(_need(raw, "model", endpoint)),
                effort=_text(_need(raw, "effort", endpoint)),
                average_agent_steps=_num(_opt(raw, "average_agent_steps")),
                agent_steps_samples=_int(_opt(raw, "agent_steps_samples")),
                average_total_tokens=_num(_opt(raw, "average_total_tokens")),
                token_samples=_int(_opt(raw, "token_samples")),
                cache_hit_rate=_num(_opt(raw, "cache_hit_rate")),
                cache_token_samples=_int(_opt(raw, "cache_token_samples")),
                runs_24h=_int(_opt(raw, "runs_24h"), 0) or 0,
                runs_48h=_int(_opt(raw, "runs_48h"), 0) or 0,
                runs_total=_int(_opt(raw, "runs_total"), 0) or 0,
            )
        )
    return MetricsPayload(meta=meta, points=tuple(points))


def parse_events(data: Mapping[str, Any]) -> tuple[RadarEvent, ...]:
    """解析 ``/events`` 的流水数组。三个 cost 布尔原样透传（不合并、不推导）。"""
    endpoint = "events"
    out = []
    for raw in _seq(_need(data, "events", endpoint)):
        out.append(
            RadarEvent(
                graded_at=_text(_opt(raw, "graded_at")),
                passed=_flag(_opt(raw, "passed")),
                score=_num(_opt(raw, "score"), 0.0) or 0.0,
                task_id=_text(_need(raw, "task_id", endpoint)),
                model=_text(_need(raw, "model", endpoint)),
                effort=_text(_need(raw, "effort", endpoint)),
                harness=_text(_opt(raw, "harness")),
                cost_usd=_num(_opt(raw, "cost_usd")),
                cost_source=_opt(raw, "cost_source"),
                cost_is_estimate=_flag(_opt(raw, "cost_is_estimate")),
                cost_is_fallback_estimate=_flag(_opt(raw, "cost_is_fallback_estimate")),
                cost_is_api_equivalent=_flag(_opt(raw, "cost_is_api_equivalent")),
                points=_num(_opt(raw, "points")),
                points_deferred=_flag(_opt(raw, "points_deferred")),
                display_name=_opt(raw, "login") or _opt(raw, "nickname"),
                avatar_url=_opt(raw, "avatar_url"),
            )
        )
    return tuple(out)


def parse_events_payload(data: Mapping[str, Any], *, meta: RadarMeta) -> EventsPayload:
    return EventsPayload(meta=meta, events=parse_events(data))


def parse_quota(data: Mapping[str, Any], *, meta: RadarMeta) -> QuotaPayload:
    return QuotaPayload(
        meta=meta,
        quota_window=_text(_opt(data, "quota_window")),
        source=_text(_opt(data, "source")),
        measured_at=_opt(data, "measured_at"),
        updated_at=_opt(data, "updated_at"),
        tier_windows_usd=_float_map(_opt(data, "tier_windows_usd")),
    )


def parse_suggest(data: Mapping[str, Any], *, meta: RadarMeta) -> SuggestPayload:
    endpoint = "suggest"
    cells = []
    for raw in _seq(_need(data, "cells", endpoint)):
        cells.append(
            SuggestCell(
                task_id=_text(_need(raw, "task_id", endpoint)),
                model=_text(_need(raw, "model", endpoint)),
                effort=_text(_need(raw, "effort", endpoint)),
                agent=_text(_opt(raw, "agent")),
                agent_version=_text(_opt(raw, "agent_version")),
                est_minutes=_int(_opt(raw, "est_minutes")),
                est_quota_pct=_num(_opt(raw, "est_quota_pct")),
                tier_windows_usd=_float_map(_opt(raw, "tier_windows_usd")),
            )
        )
    return SuggestPayload(
        meta=meta,
        cells=tuple(cells),
        holding=_int(_opt(data, "holding")),
        replaceable_unstarted=_int(_opt(data, "replaceable_unstarted")),
        protected_started=_int(_opt(data, "protected_started")),
    )


def _benchmark_field(data: Mapping[str, Any], benchmark: str, field: str) -> Any:
    """从响应内嵌的 ``benchmarks[]`` 里取指定频道的某个字段。

    ``/leaderboard`` / ``/table`` 都会把频道清单嵌在 ``benchmarks`` 里，而 ``rolling_window``
    这类**频道级**参数只在那一层 —— 顶层没有。取不到就返回 ``None``（不猜默认值）。
    """
    for raw in _seq(_opt(data, "benchmarks")):
        if isinstance(raw, Mapping) and _text(_opt(raw, "id")) == benchmark:
            return _opt(raw, field)
    return None


def _data_time(data: Mapping[str, Any]) -> Any:
    """挑一个「这份数据什么时候的」时间戳，按新鲜度优先级回退。

    上游只有部分端点给顶层 ``source_updated_at``；其余要按各自语义回退
    （数据字典 §16 要求「必须带数据时间」，缺失比不精确更糟）：

    - ``baseline_generated_at``（``/table`` 的基线生成时间）
    - ``latest_burn.submitted_at``（``/leaderboard`` 最近一次提交运行）
    - ``discrimination_generated_at``（``/table`` 的区分度生成时间）

    都没有就返回 ``None``，脚注按缺失处理（不编时间）。
    """
    direct = _opt(data, "source_updated_at")
    if direct:
        return direct
    for key in ("baseline_generated_at", "discrimination_generated_at"):
        value = _opt(data, key)
        if value:
            return value
    burn = _dict(_opt(data, "latest_burn"))
    return _opt(burn, "submitted_at") or None


def retry_after_seconds(response: httpx.Response | None, default: float) -> float:
    """读 ``Retry-After``。秒数优先；HTTP-date 形态也支持（上游未观察到，但不难兼容）。"""
    if response is None:
        return default
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if parsed is None:
        return default
    import datetime as _dt

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, parsed.timestamp() - time.time())


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float
    stored_at: float


def _mark_stale(value: Any) -> Any:
    """把缓存里的新鲜结果标成陈旧视图（不改缓存本体）。

    payload 都带 ``meta``，用 ``dataclasses.replace`` 造一个 ``stale=True`` 的副本；
    ``HistorySeries`` 元组逐条打标；没有口径字段的元组（如 ``BenchmarkInfo``）原样返回。
    """
    if isinstance(value, tuple):
        if value and all(isinstance(item, HistorySeries) for item in value):
            return tuple(replace(item, stale=True) for item in value)
        return value
    meta = getattr(value, "meta", None)
    if isinstance(meta, RadarMeta):
        return replace(value, meta=replace(meta, stale=True, fetched_at=now_iso()))
    return value


class RadarClient:
    """上游只读客户端。``http`` 仅用于测试注入（默认走共享 ``fetch_json``）。"""

    def __init__(self, config: RadarConfig, *, http: Callable[..., Any] | None = None) -> None:
        self.config = config
        self._fetch = http or fetch_json
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

    # ── 基础设施 ──

    def _url(self, path: str) -> str:
        if path.startswith(_PRIVATE_PREFIX):
            # 实现缺陷：正常路径不应出现，直接抛而不是发请求。
            raise PrivatePathRefused(detail=path)
        return f"{self.config.base_url}{API_PREFIX}{path}"

    def _cached_entry(self, key: str) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        self._cache.move_to_end(key)
        return entry

    def _store(self, key: str, value: Any, ttl: float) -> None:
        now = time.monotonic()
        self._cache[key] = _CacheEntry(value=value, expires_at=now + max(ttl, 0.0), stored_at=now)
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            evicted, _ = self._cache.popitem(last=False)
            logger.debug("[radar] payload cache evicted {}", evicted)

    async def _request(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        *,
        ttl: float,
        timeout: float,
        max_bytes: int,
        endpoint: str,
    ) -> Any:
        """发一次请求，按官方客户端的策略处理 429/503。

        - 429：读 ``Retry-After``（缺失/不可解析 → ``default_retry_after``）→ clamp
          ``[min_wait, max_wait]`` → 叠抖动 → 重试，最多 ``rate_limit_retries`` 次。
        - 503：维护栅栏，``Retry-After`` **不 clamp**，只受 ``total_budget`` 约束。
        - 其他 5xx / 超时 / 连接失败：立刻放弃（``upstream_unavailable``）。
        - 不做连接级重试（``HTTPTransport(retries=2)``）：本机常驻代理，重试只会放大故障，
          官方客户端在检测到代理时同样会关掉它。

        ``ttl`` 传给共享 HTTP 层，**不得短于上游 ``Cache-Control`` 的 max-age/s-maxage**，
        否则等于用自己的流量替上游刷新缓存。
        """
        retry = self.config.retry
        attempts = 0
        started = time.monotonic()
        while True:
            try:
                return await self._fetch(
                    url,
                    namespace=NAMESPACE,
                    params=dict(params) if params else None,
                    headers={"User-Agent": self.config.user_agent},
                    timeout_seconds=timeout,
                    ttl_seconds=ttl,
                    max_bytes=max_bytes,
                    read_only=True,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in (429, 503):
                    raise upstream_failure(endpoint, exc) from None
                retry_after = retry_after_seconds(exc.response, retry.default_retry_after)
                if status == 429:
                    wait = min(max(retry_after, retry.min_wait), retry.max_wait)
                else:
                    wait = max(retry_after, 0.0)
                wait += random.uniform(0.0, min(1.0, retry_after * retry.jitter_ratio))
                elapsed = time.monotonic() - started
                if attempts >= retry.rate_limit_retries or elapsed + wait > retry.total_budget:
                    raise upstream_failure(endpoint, exc) from None
                logger.debug(
                    "[radar] {} got {}, retrying in {:.2f}s (attempt {}/{})",
                    endpoint,
                    status,
                    wait,
                    attempts + 1,
                    retry.rate_limit_retries,
                )
                await asyncio.sleep(wait)
                attempts += 1
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise upstream_failure(endpoint, exc) from None
            except ValueError as exc:
                # 共享 HTTP 层用 ValueError 表达「空响应」与「超出 max_bytes」，
                # 不接住就会把裸 ValueError 漏给上层（用户看到的是异常名而不是错误码）。
                raise upstream_failure(endpoint, exc) from None

    async def _payload(
        self,
        key: str,
        path: str,
        builder: Callable[[Mapping[str, Any], RadarMeta], T],
        *,
        params: Mapping[str, Any] | None = None,
        ttl: float,
        timeout: float,
        max_bytes: int,
        endpoint: str,
        benchmark: str,
    ) -> T:
        """取并解析成 payload。命中缓存时**不重新解析**（``/table`` 解析一次很贵）。"""
        entry = self._cached_entry(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.value
        try:
            data = await self._request(
                self._url(path),
                params,
                ttl=ttl,
                timeout=timeout,
                max_bytes=max_bytes,
                endpoint=endpoint,
            )
        except RadarSourceError as exc:
            if entry is not None and now - entry.stored_at <= _STALE_GRACE_SECONDS:
                logger.warning(
                    "[radar] {} unavailable ({}), serving payload cached {:.0f}s ago",
                    endpoint,
                    exc.code,
                    now - entry.stored_at,
                )
                return _mark_stale(entry.value)
            raise
        # builder 的 ``meta`` 是 keyword-only，必须按关键字传（位置传会 TypeError）。
        payload = builder(data, meta=self._meta_for(endpoint, benchmark, data))
        self._store(key, payload, ttl)
        return payload

    def _meta_for(self, endpoint: str, benchmark: str, data: Mapping[str, Any]) -> RadarMeta:
        """从响应自身拼 ``RadarMeta``。

        上游各端点的 envelope 字段不完全一致（``/radar-insights`` 还有 ``mode`` /
        ``recommendation_mode`` / 两个 ``*_source_updated_at``），这里只搬运存在的。

        两处**必要的回退**（否则脚注会丢掉红线要求的段）：

        - ``/leaderboard`` **没有顶层 ``source_updated_at``**，但它带着
          ``latest_burn.submitted_at``（最近一次提交运行）—— 这是该榜单里最新鲜的数据时间，
          取它作为「数据时间」；``/table`` 则回退 ``baseline_generated_at``。
        - ``/leaderboard`` **没有顶层 ``rolling_window``**，值在 ``benchmarks[]`` 里
          （deep-swe = 3），按 ``benchmark_id`` 取对应频道的那一份。
        """
        return RadarMeta(
            benchmark_id=_text(_opt(data, "benchmark_id"), benchmark),
            scoring_mode=_text(_opt(data, "scoring_mode")),
            score_label=_text(_opt(data, "score_label")),
            mode=_opt(data, "mode"),
            rolling_window=_int(_opt(data, "rolling_window"))
            or _benchmark_field(data, benchmark, "rolling_window"),
            pass_threshold=_num(_opt(data, "pass_threshold")),
            source_updated_at=_opt(data, "source_updated_at") or _data_time(data),
            recommendation_mode=_opt(data, "recommendation_mode"),
        )

    # ── 频道与任务 ──

    async def benchmarks(self) -> tuple[BenchmarkInfo, ...]:
        """频道清单（``/benchmarks``，TTL 3600）。"""
        key = "benchmarks"
        entry = self._cached_entry(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.value
        try:
            data = await self._request(
                self._url("/benchmarks"),
                None,
                ttl=self.config.cache_ttl.benchmarks,
                timeout=self.config.timeout,
                max_bytes=self.config.max_bytes,
                endpoint="benchmarks",
            )
        except RadarSourceError as exc:
            if entry is not None and now - entry.stored_at <= _STALE_GRACE_SECONDS:
                logger.warning("[radar] benchmarks unavailable ({}), serving cache", exc.code)
                return entry.value
            raise
        items = parse_benchmarks(data)
        self._store(key, items, self.config.cache_ttl.benchmarks)
        return items

    async def benchmark_info(self, benchmark: str) -> BenchmarkInfo | None:
        """从 ``/benchmarks`` 里取单个频道的元信息（用于补 ``rolling_window`` 等）。"""
        for item in await self.benchmarks():
            if item.id == benchmark:
                return item
        return None

    async def table(self, benchmark: str | None = None) -> TablePayload:
        """``/table``：格子级明细。**大**（deep-swe 9.3 MB / 7504 格），只在需要时调用。"""
        target = self._benchmark(benchmark)
        return await self._payload(
            f"table:{target}",
            "/table",
            parse_table,
            params={"benchmark": target},
            ttl=self.config.cache_ttl.table,
            timeout=self.config.table_timeout,
            max_bytes=self.config.table_max_bytes,
            endpoint="table",
            benchmark=target,
        )

    async def tasks(self, benchmark: str | None = None) -> tuple[TaskInfo, ...]:
        """题目清单：**复用** ``/table`` 的缓存，不单独发请求（plugin_api §4.1）。"""
        payload = await self.table(benchmark)
        return payload.tasks

    # ── 榜单 ──

    async def leaderboard(
        self, benchmark: str | None = None, *, view: str | None = None
    ) -> LeaderboardPayload:
        """``/leaderboard``。

        ``view`` 参数上游完全无效（实测 total/month/history 三种取值返回字节完全一致），
        因此接受但**不发送**，只记一条 debug。
        """
        if view:
            logger.debug("[radar] view={} is ignored: upstream ignores it too", view)
        target = self._benchmark(benchmark)
        return await self._payload(
            f"leaderboard:{target}",
            "/leaderboard",
            parse_leaderboard,
            params={"benchmark": target},
            ttl=self.config.cache_ttl.leaderboard,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="leaderboard",
            benchmark=target,
        )

    # ── 洞察 ──

    async def insights(self, benchmark: str | None = None) -> InsightsPayload:
        """``/radar-insights``：唯一把编程与视觉合成单一 IQ 的端点。

        注意 ``comprehensive_points`` 是**按频道的**（deep-swe 有 40 点，pompeii 为 0 点），
        而推荐与降智**不从** ``comprehensive_points`` 派生 —— 两者都要单独展示。
        """
        target = self._benchmark(benchmark)
        return await self._payload(
            f"insights:{target}",
            "/radar-insights",
            parse_insights,
            params={"benchmark": target},
            ttl=self.config.cache_ttl.radar_insights,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="insights",
            benchmark=target,
        )

    async def efficiency(self, benchmark: str | None = None) -> EfficiencyPayload:
        target = self._benchmark(benchmark)
        return await self._payload(
            f"efficiency:{target}",
            "/intelligence-efficiency",
            parse_efficiency,
            params={"benchmark": target},
            ttl=self.config.cache_ttl.intelligence_efficiency,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="efficiency",
            benchmark=target,
        )

    async def model_metrics(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        benchmark: str | None = None,
    ) -> MetricsPayload:
        """``/model-metrics``。未知 model 时上游返回 ``200 + points: []`` —— **不报错**，
        空元组由 service 转成 ``unknown_model`` 话术（plugin_api §4.2 第 4 条）。"""
        target = self._benchmark(benchmark)
        params: dict[str, Any] = {"benchmark": target}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        return await self._payload(
            f"metrics:{target}:{model or ''}:{effort or ''}",
            "/model-metrics",
            parse_metrics,
            params=params,
            ttl=self.config.cache_ttl.model_metrics,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="model-metrics",
            benchmark=target,
        )

    async def history(self, benchmark: str | None = None) -> tuple[HistorySeries, ...]:
        """``/iq-history``：顶层扁平 dict，键即序列名（三种形态口径不同）。"""
        target = self._benchmark(benchmark)
        key = f"history:{target}"
        entry = self._cached_entry(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.value
        try:
            data = await self._request(
                self._url("/iq-history"),
                {"benchmark": target},
                ttl=self.config.cache_ttl.iq_history,
                timeout=self.config.timeout,
                max_bytes=self.config.max_bytes,
                endpoint="iq-history",
            )
        except RadarSourceError as exc:
            if entry is not None and now - entry.stored_at <= _STALE_GRACE_SECONDS:
                logger.warning("[radar] iq-history unavailable ({}), serving cache", exc.code)
                return _mark_stale(entry.value)
            raise
        series = parse_history(_dict(data))
        self._store(key, series, self.config.cache_ttl.iq_history)
        return series

    # ── 流水与辅助 ──

    async def events(
        self, *, n: int = 30, benchmark: str | None = None
    ) -> EventsPayload:
        """``/events``：最近判分流水（带自己的口径 envelope）。"""
        target = self._benchmark(benchmark)
        return await self._payload(
            f"events:{target}:{n}",
            "/events",
            parse_events_payload,
            params={"n": n, "benchmark": target},
            ttl=self.config.cache_ttl.events,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="events",
            benchmark=target,
        )

    async def quota(self) -> QuotaPayload:
        return await self._payload(
            "quota",
            "/quota",
            parse_quota,
            ttl=self.config.cache_ttl.quota,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="quota",
            benchmark=self.config.default_benchmark,
        )

    async def suggest(
        self,
        *,
        n: int = 5,
        benchmark: str | None = None,
        harness: str | None = None,
        replace_unstarted: bool = False,
    ) -> SuggestPayload:
        """``/suggest``：上游给的「接下来跑什么」。只读展示，**不代为领取任务**。"""
        target = self._benchmark(benchmark)
        params: dict[str, Any] = {"n": n, "benchmark": target}
        if harness:
            params["harness"] = harness
        if replace_unstarted:
            params["replace_unstarted"] = "true"
        return await self._payload(
            f"suggest:{target}:{n}:{harness or ''}:{int(bool(replace_unstarted))}",
            "/suggest",
            parse_suggest,
            params=params,
            ttl=self.config.cache_ttl.suggest,
            timeout=self.config.timeout,
            max_bytes=self.config.max_bytes,
            endpoint="suggest",
            benchmark=target,
        )

    # ── 内部 ──

    def _benchmark(self, benchmark: str | None) -> str:
        return benchmark or self.config.default_benchmark

    def clear_cache(self) -> None:
        """清空 payload 缓存（测试与手动刷新用）。"""
        self._cache.clear()

    def cache_keys(self) -> tuple[str, ...]:
        """当前缓存键（诊断用，不含任何上游数据）。"""
        return tuple(self._cache)


__all__ = [
    "API_PREFIX",
    "LATEST_PREFIX",
    "NAMESPACE",
    "RadarClient",
    "parse_alert",
    "parse_benchmarks",
    "parse_cell",
    "parse_contributor",
    "parse_discrimination",
    "parse_efficiency",
    "parse_efficiency_point",
    "parse_events",
    "parse_events_payload",
    "parse_flag_race",
    "parse_history",
    "parse_insight_point",
    "parse_insights",
    "parse_leaderboard",
    "parse_metrics",
    "parse_model_row",
    "parse_pulse",
    "parse_quota",
    "parse_recommendation",
    "parse_run",
    "parse_suggest",
    "parse_table",
    "parse_task",
    "parse_task_vote",
    "parse_trend_point",
    "retry_after_seconds",
]
