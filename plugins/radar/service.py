"""AI 智商雷达的查询语义层。

这里只做「把 provider 拿到的只读模型整理成回答用户问题的形状」：筛选、排序、档位策略、
别名解析、派生结构。**不出网、不拼文案、不渲染**（框架 §2.1）。

口径红线（这些是设计文档里反复强调、最容易在实现里被"顺手修一下"的地方）：

1. **两个 IQ 口径不可互推**。``/leaderboard`` 的 ``pass_rate``（口径 = 该频道的
   ``score_label``）与 ``/radar-insights`` 的 ``iq``（跨频道加权）是两个东西：实测
   deep-swe ``gpt-6-astra@low`` 是 ``pass_rate 0.676 → 101.4`` 而 ``iq 108.62``。
   因此 ``iq`` 优先用 insights 的值，只有 insights 里没有该档位时才用
   ``pass_rate × 150`` 兜底，并置 ``iq_derived=True`` 让 formatter 标注。
2. **``(model, effort)`` 是原子键**。任何比较、展示、筛选都必须带档位；
   ``gpt-5.6-sol@low`` 与 ``@max`` 差 25 IQ 以上。
3. **不跨频道混排**。``top_models`` 只在单一 benchmark 内排序；跨频道的综合 IQ
   只能来自 ``/radar-insights``，并且必须标注 ``recommendation_mode``。
4. **降智预警原样转发**。上游规则已排除 DeepSeek、只与自身历史比较，本地**不重算**。
5. **空数据不是异常**。空元组 + ``meta``，由 formatter 说「无数据」。
6. **``stale`` 必须传播**。任一环节用了陈旧缓存，最终 ``meta.stale=True``。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, Mapping, Sequence

from .config import EFFORT_TIERS, RadarConfig
from .errors import InvalidArgument, UnknownModel
from .models import (
    BenchmarkInfo,
    CellState,
    Comparison,
    ContributorRow,
    DegradationAlert,
    EfficiencyPoint,
    FlagRace,
    FleetPulse,
    HistorySeries,
    InsightPoint,
    MetricPoint,
    ModelConfig,
    ModelProfile,
    ModelRow,
    RadarEvent,
    RadarMeta,
    Recommendation,
    TablePayload,
    TaskDetail,
    TaskInfo,
    TrendPoint,
)
from .provider import RadarClient

#: IQ 上限。上游把 0–100 的通过率线性映射到 0–150（``pass_rate × 100 × 1.5``）。
IQ_SCALE = 150.0

#: 档位由低到高的序（用于「取最高档」）。
_EFFORT_ORDER: Mapping[str, int] = {tier: index for index, tier in enumerate(EFFORT_TIERS)}

#: 口语别名 → 上游 id。只做映射，**不做猜测**：匹配不唯一就报候选。
_ALIASES: Mapping[str, str] = {
    "astra": "gpt-6-astra",
    "gpt6": "gpt-6-astra",
    "gpt6astra": "gpt-6-astra",
    "sol": "gpt-5.6-sol",
    "gpt56sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "gpt56terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
    "gpt56luna": "gpt-5.6-luna",
    "gpt55": "gpt-5.5",
    "opus": "claude-opus-5",
    "opus5": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "sonnet5": "claude-sonnet-5",
    "gemini": "gemini-3.8-flash",
    "gemini38": "gemini-3.8-flash",
    "gemini37": "gemini-3.7-flash",
    "glm": "glm-5.3",
    "glmflash": "glm-5.3-flash",
    "glm53": "glm-5.3",
    "glm53flash": "glm-5.3-flash",
    "grok": "grok-4.6",
    "grok46": "grok-4.6",
    "k3": "k3",
    "hy4": "hy4-preview",
    "hy4preview": "hy4-preview",
    "deepseek": "deepseek-v4.1-flash",
    "deepseekflash": "deepseek-v4-flash",
    "dsh": "dsh-deepseek-v4-flash",
    "vision": "dsh-deepseek-v4-flash-vision-exp",
}


def _normalize(text: str) -> str:
    """把口语名归一：去空格 / 连字符 / 下划线 / 点，转小写。

    ``gpt-6 astra`` / ``gpt6astra`` / ``GPT-6-Astra`` 都应归到 ``gpt-6-astra``。
    """
    return re.sub(r"[\s\-_.]+", "", str(text or "")).casefold()


def _sort_key(row: ModelRow) -> tuple[float, int, str]:
    """三级稳定排序：``iq`` 降序 → 样本量降序 → ``model`` 升序。

    第三级用模型名而非档位，保证同分同样本时不因输入顺序而抖动。
    """
    iq = row.iq if row.iq is not None else float("-inf")
    return (-iq, -row.graded, row.model)


def _dedupe(rows: Iterable[ModelRow]) -> tuple[ModelRow, ...]:
    """按 ``(model, effort)`` 去重，保留首次出现（上游不应有重复，防御性处理）。"""
    seen: dict[tuple[str, str], ModelRow] = {}
    for row in rows:
        seen.setdefault((row.model, row.effort), row)
    return tuple(seen.values())


class RadarService:
    """把 provider 的只读数据变成「回答一个问题」所需的形状。

    本类**不缓存**（缓存已在 provider），**不渲染**（在 formatters），
    也**不吞异常**（``RadarError`` 由 handler 统一转文案）。
    """

    def __init__(self, client: RadarClient, config: RadarConfig) -> None:
        self.client = client
        self.config = config
        #: 缓存「已合并 insights IQ 的榜单」，避免同一命令里重复拉 insights。
        self._insight_index: dict[str, dict[str, InsightPoint]] = {}

    # ── 别名与档位 ──

    def _insight_map(self, benchmark: str) -> dict[str, InsightPoint]:
        return self._insight_index.get(benchmark, {})

    async def _ensure_insights(self, benchmark: str) -> dict[str, InsightPoint]:
        """拉一次 insights 并把 ``(model@effort) → InsightPoint`` 建索引。

        失败**不致命**：insights 挂了不该让榜单也查不了（降级为主榜口径 + 标注）。
        """
        if benchmark in self._insight_index:
            return self._insight_index[benchmark]
        try:
            payload = await self.client.insights(benchmark)
        except Exception:  # noqa: BLE001 - 见 docstring：insights 不可用不影响主榜
            self._insight_index[benchmark] = {}
            return {}
        index = {point.key: point for point in payload.comprehensive_points}
        self._insight_index[benchmark] = index
        return index

    def _apply_iq(self, rows: Sequence[ModelRow], index: Mapping[str, InsightPoint]) -> tuple[ModelRow, ...]:
        """给榜单行补 ``iq``。

        上游 insights 有该档位 → 用上游值（``iq_derived=False``）；
        没有 → ``pass_rate × 100 × 1.5`` 兜底并置 ``iq_derived=True``。
        **绝不反向**（不从 pass_rate 推 insights 的 iq，也不把两者混进同一个排序的同一含义里）。
        """
        out = []
        for row in rows:
            point = index.get(row.key)
            if point is not None:
                out.append(replace(row, iq=point.iq, iq_derived=False))
            else:
                out.append(replace(row, iq=row.pass_rate * 100.0 * (IQ_SCALE / 100.0), iq_derived=True))
        return tuple(out)

    def resolve_model(self, query: str, known: Iterable[str]) -> str:
        """把口语名解析成上游 model id。

        - 精确命中（大小写/分隔符无关）→ 直接返回。
        - 别名表命中 → 返回映射值。
        - 命中多个候选 → :class:`UnknownModel` 带候选列表（**不猜**）。
        - 全不命中 → :class:`UnknownModel` 带最接近的候选。
        """
        raw = str(query or "").strip()
        if not raw:
            raise InvalidArgument(detail="empty model")
        models = sorted(set(known))
        target = _normalize(raw)
        exact = [name for name in models if _normalize(name) == target]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise UnknownModel(f"模型名「{raw}」对应多个候选：{'、'.join(exact)}", detail="ambiguous")

        alias = _ALIASES.get(target)
        if alias:
            if alias in models:
                return alias
            # 别名指向的模型不在该频道（例如视觉档位只出现在某些频道）。
            raise UnknownModel(
                f"「{raw}」对应的 {alias} 在当前频道没有实测数据。",
                detail="alias-not-in-benchmark",
            )

        partial = [name for name in models if target in _normalize(name)]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise UnknownModel(
                f"「{raw}」对应多个候选：{'、'.join(partial[:8])}", detail="ambiguous"
            )
        raise UnknownModel(detail=f"unknown model: {raw}")

    def resolve_effort(self, effort: str | None) -> str | None:
        """归一档位名。``None``/空 → ``None``（由调用方决定默认策略）。"""
        if not effort:
            return None
        target = _normalize(effort)
        for tier in EFFORT_TIERS:
            if _normalize(tier) == target:
                return tier
        raise InvalidArgument(
            f"档位「{effort}」不合法，可选：{'/'.join(EFFORT_TIERS)}。", detail="unknown effort"
        )

    @staticmethod
    def highest_effort(rows: Iterable[ModelRow]) -> tuple[ModelRow, ...]:
        """每个模型只留最高档（``ultra > max > xhigh > high > medium > low``）。"""
        best: dict[str, ModelRow] = {}
        for row in rows:
            current = best.get(row.model)
            if current is None or _EFFORT_ORDER.get(row.effort, -1) > _EFFORT_ORDER.get(
                current.effort, -1
            ):
                best[row.model] = row
        return tuple(best.values())

    def _filter(
        self,
        rows: Sequence[ModelRow],
        *,
        model: str | None = None,
        effort: str | None = None,
        min_samples: int | None = None,
    ) -> tuple[ModelRow, ...]:
        out = rows
        if model:
            out = tuple(row for row in out if row.model == model)
        if effort:
            out = tuple(row for row in out if row.effort == effort)
        if min_samples is not None:
            out = tuple(row for row in out if row.graded >= min_samples)
        return out

    # ── 频道 ──

    async def benchmarks(self) -> tuple[BenchmarkInfo, ...]:
        """频道清单。"""
        return await self.client.benchmarks()

    async def model_catalog(self, *, benchmark: str | None = None) -> tuple[ModelConfig, ...]:
        """档位清单。

        权威来源是 ``/table`` 的 ``combos``（不是价格表）：实测 ``deepseek-v4-pro``
        在 ``token_pricing`` 里有价却在任何频道的 ``combos`` 里都不出现 ——
        价格表 ≠ 被评测的模型集合。
        """
        payload = await self.client.table(benchmark)
        return payload.combos

    async def table(self, *, benchmark: str | None = None) -> TablePayload:
        return await self.client.table(benchmark)

    # ── 榜单 ──

    async def top_models(
        self,
        *,
        benchmark: str | None = None,
        effort: str | None = None,
        by: str = "iq",
        limit: int | None = None,
        min_samples: int | None = None,
    ) -> tuple[tuple[ModelRow, ...], RadarMeta]:
        """单一频道内的档位排行。

        ``effort=None`` 时取每个模型的**最高档**并标注；``by`` 支持 ``iq`` / ``pass_rate``
        / ``cost``（成本取 ``/intelligence-efficiency``）。
        """
        target = benchmark or self.config.default_benchmark
        wanted_effort = self.resolve_effort(effort)
        payload = await self.client.leaderboard(target)
        index = await self._ensure_insights(target)
        rows = self._apply_iq(payload.models, index)
        rows = self._filter(rows, effort=wanted_effort, min_samples=min_samples)

        note = ""
        if wanted_effort is None:
            rows = self.highest_effort(rows)
            note = "已取各模型最高档"

        if by == "iq":
            rows = tuple(sorted(rows, key=_sort_key))
        elif by == "pass_rate":
            rows = tuple(
                sorted(rows, key=lambda row: (-row.pass_rate, -row.graded, row.model))
            )
        elif by == "cost":
            cost_payload = await self.client.efficiency(target)
            price = {point.key: point for point in cost_payload.points}
            rows = tuple(
                sorted(
                    rows,
                    key=lambda row: (
                        price[row.key].average_price_usd
                        if row.key in price and price[row.key].average_price_usd is not None
                        else float("inf"),
                        -row.pass_rate,
                        row.model,
                    ),
                )
            )
            note = f"{note}；成本为上游估算口径".lstrip("；")
        else:
            raise InvalidArgument(
                f"排序字段「{by}」不合法，可选：iq/pass_rate/cost。", detail="unknown sort"
            )

        cap = limit if limit is not None else self.config.max_models_listed
        rows = rows[: max(0, cap)]
        meta = replace(payload.meta, note=note)
        return rows, meta

    async def model_profile(
        self,
        query: str,
        *,
        effort: str | None = None,
        benchmark: str | None = None,
        trend_hours: int = 72,
        recent_limit: int = 5,
    ) -> ModelProfile:
        """单个模型的档案：全部档位 + 效率 + 运行特征 + 综合 IQ + 趋势 + 最近流水。"""
        target = benchmark or self.config.default_benchmark
        payload = await self.client.leaderboard(target)
        model = self.resolve_model(query, {row.model for row in payload.models})
        index = await self._ensure_insights(target)
        rows = self._apply_iq(payload.models, index)
        wanted = self.resolve_effort(effort)
        variants = self._filter(rows, model=model, effort=wanted)
        if not variants:
            raise UnknownModel(
                f"{model} 在当前频道没有该档位的实测数据。", detail="no variant"
            )

        efficiency = None
        metrics = None
        try:
            efficiency_payload = await self.client.efficiency(target)
            efficiency = next(
                (point for point in efficiency_payload.points if point.key == variants[0].key), None
            )
        except Exception:  # noqa: BLE001 - 辅助数据缺失不该让主查询失败
            efficiency = None
        try:
            metrics_payload = await self.client.model_metrics(
                model=model, effort=variants[0].effort, benchmark=target
            )
            metrics = metrics_payload.points[0] if metrics_payload.points else None
        except Exception:  # noqa: BLE001
            metrics = None

        insight = index.get(variants[0].key)
        best = max(variants, key=lambda row: _EFFORT_ORDER.get(row.effort, -1))

        trend: tuple[TrendPoint, ...] = ()
        try:
            series = await self.client.history(target)
            trend = self._series_points(series, model, variants[0].effort)
        except Exception:  # noqa: BLE001
            trend = ()

        recent: tuple[RadarEvent, ...] = ()
        try:
            payload_events = await self.client.events(n=50, benchmark=target)
            recent = tuple(
                event
                for event in payload_events.events
                if event.model == model and event.effort == variants[0].effort
            )[: max(0, recent_limit)]
        except Exception:  # noqa: BLE001
            recent = ()

        note = ""
        if wanted is None and len(variants) > 1:
            note = "含全部档位"
        # 只请求了单一档位时，样本量就是那一档的 ``graded``（上游给的真实运行数）；
        # 请求了全部档位时各档位行自带 ``n=``，脚注不造一个含糊的总数。
        samples = variants[0].graded if len(variants) == 1 else None
        meta = replace(payload.meta, note=note, samples=samples)
        return ModelProfile(
            model=model,
            variants=variants,
            best=best,
            efficiency=efficiency,
            metrics=metrics,
            insight=insight,
            trend=trend,
            recent=recent,
            meta=meta,
        )

    def _series_points(
        self, series: Sequence[HistorySeries], model: str, effort: str
    ) -> tuple[TrendPoint, ...]:
        """取趋势点：**带档位就只认 ``模型@档位``**，不混裸模型名（两者口径不同）。

        上游 ``/iq-history`` 的键有三种形态且分数不同（实测 ``gpt-5.6-sol`` 96.7 vs
        ``latest:gpt-5.6-sol`` 97.3），因此这里显式排除 ``latest:`` 前缀的序列。
        """
        for item in series:
            if item.latest:
                continue
            if item.model == model and item.effort == effort:
                return item.points
        return ()

    async def compare(
        self,
        left: str,
        right: str,
        *,
        effort: str | None = None,
        benchmark: str | None = None,
    ) -> Comparison:
        """两个档位对比。delta 一律 ``left - right``（正数 = 左边更高/更贵/更慢）。"""
        left_profile = await self.model_profile(left, effort=effort, benchmark=benchmark)
        right_profile = await self.model_profile(right, effort=effort, benchmark=benchmark)

        def _delta(one: float | None, other: float | None) -> float | None:
            if one is None or other is None:
                return None
            return one - other

        left_row = left_profile.best
        right_row = right_profile.best
        left_eff = left_profile.efficiency
        right_eff = right_profile.efficiency
        return Comparison(
            left=left_profile,
            right=right_profile,
            iq_delta=_delta(
                left_row.iq if left_row else None, right_row.iq if right_row else None
            ),
            pass_rate_delta=_delta(
                left_row.pass_rate if left_row else None,
                right_row.pass_rate if right_row else None,
            ),
            cost_delta_usd=_delta(
                left_eff.average_price_usd if left_eff else None,
                right_eff.average_price_usd if right_eff else None,
            ),
            duration_delta_minutes=_delta(
                left_eff.average_minutes if left_eff else None,
                right_eff.average_minutes if right_eff else None,
            ),
            cost_index_delta=_delta(
                left_eff.combined_cost_index if left_eff else None,
                right_eff.combined_cost_index if right_eff else None,
            ),
            meta=left_profile.meta,
        )

    # ── 推荐与预警 ──

    async def recommendations(self, *, benchmark: str | None = None) -> tuple[Recommendation, ...]:
        """上游推荐（4 组固定 key）。**原样转发**，不改排序、不重算。"""
        payload = await self.client.insights(benchmark)
        return payload.recommendations

    async def recommendations_meta(self, *, benchmark: str | None = None) -> RadarMeta:
        payload = await self.client.insights(benchmark)
        return payload.meta

    async def degradation_alerts(
        self, *, benchmark: str | None = None
    ) -> tuple[tuple[DegradationAlert, ...], RadarMeta]:
        """降智预警。

        **只转发上游结果**：上游规则已排除 DeepSeek、只与模型自身历史比较，本地补算
        会造出上游不承认的结论（plugin_api §5.1）。
        """
        payload = await self.client.insights(benchmark)
        return payload.degradation_alerts, payload.meta

    # ── 性价比 ──

    async def value_picks(
        self,
        *,
        benchmark: str | None = None,
        limit: int = 5,
        max_cost_usd: float | None = None,
        min_iq: float | None = None,
    ) -> tuple[tuple[EfficiencyPoint, ...], RadarMeta]:
        """性价比：按 ``combined_cost_index`` 升序（越低越划算），只在单频道内。"""
        target = benchmark or self.config.default_benchmark
        payload = await self.client.efficiency(target)
        points = payload.points
        if max_cost_usd is not None:
            points = tuple(
                point
                for point in points
                if point.average_price_usd is not None and point.average_price_usd <= max_cost_usd
            )
        if min_iq is not None:
            points = tuple(point for point in points if point.iq is not None and point.iq >= min_iq)
        ordered = tuple(
            sorted(
                points,
                key=lambda point: (
                    point.combined_cost_index
                    if point.combined_cost_index is not None
                    else float("inf"),
                    -(point.iq or 0.0),
                    point.model,
                ),
            )
        )
        return ordered[: max(0, limit)], payload.meta

    # ── 趋势 ──

    async def trend(
        self,
        model: str,
        *,
        effort: str | None = None,
        hours: int = 72,
        benchmark: str | None = None,
    ) -> tuple[tuple[TrendPoint, ...], RadarMeta]:
        """趋势。

        裸模型名 → 跨档位合并序列；带档位 → 单档位序列。**两者不可混**（口径不同），
        因此这里显式跳过 ``latest:`` 前缀序列，并在 ``meta.note`` 里说明用的是哪一种。
        """
        target = benchmark or self.config.default_benchmark
        payload = await self.client.leaderboard(target)
        known = {row.model for row in payload.models}
        name = self.resolve_model(model, known)
        wanted = self.resolve_effort(effort)
        series = await self.client.history(target)
        points: tuple[TrendPoint, ...] = ()
        note = ""
        for item in series:
            if item.latest:
                continue
            if item.model != name:
                continue
            if wanted is None and item.effort is None:
                points = item.points
                note = "跨档位合并口径"
                break
            if wanted is not None and item.effort == wanted:
                points = item.points
                note = f"单档位 {wanted}"
                break
        if hours and points:
            points = points[-max(1, int(hours)) :]
        # 趋势的样本量取序列末点（``/iq-history`` 的 ``n`` 是该时刻的有效样本数）。
        samples = points[-1].samples if points else None
        meta = replace(payload.meta, note=note or "无匹配序列", samples=samples)
        return points, meta

    # ── 题目 ──

    async def task_detail(self, task_id: str, *, benchmark: str | None = None) -> TaskDetail:
        """题目详情：题目元信息 + 所有档位的格子 + 通过的档位。"""
        target = benchmark or self.config.default_benchmark
        payload = await self.client.table(target)
        key = str(task_id or "").strip()
        task = next((item for item in payload.tasks if item.id == key), None)
        if task is None:
            # 支持关键词模糊匹配，命中多个时报候选而不是随便挑。
            matched = [
                item for item in payload.tasks if key and key.casefold() in item.id.casefold()
            ]
            if len(matched) == 1:
                task = matched[0]
            elif len(matched) > 1:
                raise InvalidArgument(
                    f"「{key}」匹配多道题：{'、'.join(item.id for item in matched[:5])}",
                    detail="ambiguous task",
                )
            else:
                raise InvalidArgument(detail=f"unknown task: {key}")
        cells = tuple(
            sorted(
                (cell for cell in payload.cells.values() if cell.task_id == task.id),
                key=lambda cell: (
                    cell.model,
                    _EFFORT_ORDER.get(cell.effort, -1),
                ),
            )
        )
        return TaskDetail(
            task=task,
            cells=cells,
            solved_by=tuple(cell for cell in cells if cell.p > 0),
            meta=payload.meta,
        )

    async def task_ranking(
        self,
        *,
        benchmark: str | None = None,
        min_discrimination: float | None = None,
        limit: int = 10,
    ) -> tuple[tuple[TaskInfo, ...], RadarMeta]:
        """好题榜：按区分度 ``score`` 降序。

        只有 deep-swe 有 ``discrimination``（pompeii 实测 0/86 条）—— 没有区分度的题目
        不参与排序，并在 ``meta.note`` 里说明，避免让人误以为「没排上 = 题不好」。
        """
        target = benchmark or self.config.default_benchmark
        payload = await self.client.table(target)
        scored = [task for task in payload.tasks if task.discrimination is not None]
        if min_discrimination is not None:
            scored = [
                task
                for task in scored
                if task.discrimination is not None
                and task.discrimination.score >= min_discrimination
            ]
        ordered = tuple(
            sorted(
                scored,
                key=lambda task: (
                    -(task.discrimination.score if task.discrimination else 0.0),
                    task.id,
                ),
            )
        )
        note = ""
        if len(scored) < len(payload.tasks):
            note = f"{len(payload.tasks) - len(scored)} 道题无区分度数据，未参与排序"
        meta = replace(payload.meta, note=note)
        cap = limit if limit is not None else self.config.max_tasks_listed
        return ordered[: max(0, cap)], meta

    async def who_solved(self, task_id: str, *, benchmark: str | None = None) -> tuple[CellState, ...]:
        """哪些档位做出来了（``p > 0``）。"""
        detail = await self.task_detail(task_id, benchmark=benchmark)
        return detail.solved_by

    # ── 社区 ──

    async def top_contributors(
        self, *, scope: str = "month", limit: int = 10, benchmark: str | None = None
    ) -> tuple[tuple[ContributorRow, ...], RadarMeta]:
        """贡献者榜。``scope`` 决定用月榜还是总榜字段排序（上游两个口径都有值）。"""
        target = benchmark or self.config.default_benchmark
        payload = await self.client.leaderboard(target)
        if scope not in ("month", "total"):
            raise InvalidArgument(
                f"榜单范围「{scope}」不合法，可选：month/total。", detail="unknown scope"
            )
        key = (lambda row: -row.month_points) if scope == "month" else (lambda row: -row.points)
        ordered = tuple(sorted(payload.contributors, key=lambda row: (key(row), row.display_name)))
        return ordered[: max(0, limit)], payload.meta

    async def fleet_pulse(self, *, benchmark: str | None = None) -> tuple[FleetPulse | None, RadarMeta]:
        payload = await self.client.leaderboard(benchmark)
        return payload.pulse, payload.meta

    async def flag_race(self, *, benchmark: str | None = None) -> tuple[FlagRace | None, RadarMeta]:
        payload = await self.client.leaderboard(benchmark)
        return payload.flag_race, payload.meta

    async def recent_events(
        self, *, limit: int = 10, benchmark: str | None = None
    ) -> tuple[tuple[RadarEvent, ...], RadarMeta]:
        if limit > 50:
            raise InvalidArgument("流水条数上限为 50。", detail="limit too large")
        target = benchmark or self.config.default_benchmark
        payload = await self.client.events(n=max(1, limit), benchmark=target)
        return payload.events[: max(0, limit)], payload.meta


__all__ = ["IQ_SCALE", "RadarService"]
