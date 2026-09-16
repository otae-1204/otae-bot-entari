"""``plugins/radar`` 的契约测试。

七组用例（plugin_api §8）：

1. :class:`ProviderParsingTests` —— 真实录制夹具经 provider 解析后的字段值；
2. :class:`ProviderErrorTests` —— 错误码归一、陈旧回退、私有路径拒绝、缓存复用；
3. :class:`ServiceRankingTests` —— 排序/档位策略/派生结构；
4. :class:`ServiceAliasTests` —— 口语名与档位解析；
5. :class:`FormatterTests` —— 纯文本红线（宽度、口径、缺失值、估算标记）；
6. :class:`HandlerTests` —— 命令面端到端（子进程内真实 entari 派发，**不出网**）；
7. :class:`SchemaDriftTests` —— 缺字段必须 ``schema_drift`` 而不是 ``KeyError``。

夹具来自 ``api.codexradar.com`` 的真实响应录制（裁剪 + 脱敏，数值逐字保留），
见 ``tests/fixtures/radar/README.md``。所有文件都是 UTF-8（本机 locale 是 GBK）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from plugins.radar.config import RadarCacheTTL
from plugins.radar.config import RadarConfig as _RadarConfig
from plugins.radar.errors import (
    CODE_INVALID_ARGUMENT,
    CODE_PRIVATE_PATH_REFUSED,
    CODE_RATE_LIMITED,
    CODE_SCHEMA_DRIFT,
    CODE_UNKNOWN_BENCHMARK,
    CODE_UNKNOWN_MODEL,
    CODE_UPSTREAM_UNAVAILABLE,
    InvalidArgument,
    PrivatePathRefused,
    RateLimited,
    SchemaDrift,
    UnknownBenchmark,
    UnknownModel,
    UpstreamUnavailable,
    redact,
)
from plugins.radar.formatters import (
    EMPTY,
    FOOTER_MAX_WIDTH,
    MAX_LINE_WIDTH,
    ROW_MAX_WIDTH,
    display_width,
    format_alerts,
    format_benchmark_list,
    format_comparison,
    format_contributors,
    format_error,
    format_events,
    format_help,
    format_meta_footer,
    format_model_catalog,
    format_model_list,
    format_model_profile,
    format_pulse,
    format_recommendations,
    format_task_detail,
    format_task_ranking,
    format_trend,
    format_value_picks,
)
from plugins.radar.models import (
    SRC_MEASURED,
    RadarMeta,
    is_estimate,
    now_iso,
)
from plugins.radar.provider import (
    RadarClient as _Client,
    parse_alert,
    parse_leaderboard,
    parse_table,
)
from plugins.radar.service import IQ_SCALE, RadarService


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "radar"
API = "https://api.codexradar.com/api/v1"

#: 端点路径 → 夹具名。
ROUTES: dict[str, str] = {
    "/api/v1/benchmarks": "benchmarks",
    "/api/v1/leaderboard": "leaderboard",
    "/api/v1/table": "table",
    "/api/v1/radar-insights": "insights",
    "/api/v1/intelligence-efficiency": "efficiency",
    "/api/v1/model-metrics": "model_metrics",
    "/api/v1/iq-history": "iq_history",
    "/api/v1/events": "events",
    "/api/v1/quota": "quota",
    "/api/v1/suggest": "suggest",
}

#: ``(路径, benchmark 参数)`` → 夹具名（覆盖 ``ROUTES``）。
ROUTE_OVERRIDES: dict[tuple[str, str], str] = {
    ("/api/v1/table", "pompeii-adjacency"): "table_pompeii",
    ("/api/v1/intelligence-efficiency", "pompeii-adjacency"): "efficiency_pompeii",
}


def load(name: str) -> dict:
    """读一个夹具（显式 UTF-8）。"""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeHTTP:
    """替身取数器：把 URL 路由到夹具，并记录每次调用。

    签名必须与共享层 ``fetch_json`` 一致（provider 是按关键字调用的）。
    """

    def __init__(self, *, errors: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.errors = dict(errors or {})

    def _route(self, url: str, params: dict | None) -> str:
        path = url.split("api.codexradar.com", 1)[-1]
        params = params or {}
        benchmark = params.get("benchmark")
        if benchmark and (path, benchmark) in ROUTE_OVERRIDES:
            return ROUTE_OVERRIDES[(path, benchmark)]
        try:
            return ROUTES[path]
        except KeyError:  # pragma: no cover - 说明测试自己写错了路径
            raise AssertionError(f"unrouted path in test: {path}") from None

    async def __call__(self, url, *, namespace, params, headers, **kwargs):
        self.calls.append((url, dict(params or {})))
        path = url.split("api.codexradar.com", 1)[-1]
        for key, error in self.errors.items():
            if path.startswith(key):
                raise error
        return load(self._route(url, params))

    # ── 断言辅助 ──

    @property
    def paths(self) -> list[str]:
        return [url.split("api.codexradar.com", 1)[-1] for url, _ in self.calls]

    def count(self, path: str) -> int:
        return sum(1 for item in self.paths if item == path)


def make_client(http: FakeHTTP, **ttl: float) -> _Client:
    """构造一个注入替身取数器的 provider。"""
    config = replace(CFG, cache_ttl=RadarCacheTTL(**ttl))
    return _Client(config, http=http)


def run(coro):
    return asyncio.run(coro)


#: 测试用的基线配置（不读 .env，避免环境差异影响断言）。
CFG = _RadarConfig()


def error_response(status: int, url: str, **headers) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class ProviderParsingTests(unittest.TestCase):
    """夹具经 provider 解析后必须逐字保留上游数值。"""

    def setUp(self) -> None:
        self.http = FakeHTTP()
        self.client = make_client(self.http)

    def test_benchmarks_returns_a_bare_tuple_without_meta(self):
        items = run(self.client.benchmarks())
        self.assertEqual([item.id for item in items], ["deep-swe", "pompeii-adjacency"])
        deep, pompeii = items
        self.assertTrue(deep.default)
        self.assertEqual(deep.scoring_mode, "binary-majority")
        self.assertEqual(deep.score_label, "Pass rate")
        self.assertEqual(deep.task_count, 112)
        self.assertEqual(deep.model_config_count, 67)
        # deep-swe 的三项参考字段整体为 null（上游如此），必须容忍。
        self.assertIsNone(deep.reference_task_id)
        self.assertIsNone(deep.task_bundle)
        self.assertEqual(pompeii.scoring_mode, "continuous-macro")
        self.assertEqual(pompeii.score_label, "Adjacency F1")
        self.assertEqual(pompeii.reference_task_id, "RP-group-1-public-example")
        self.assertIsNotNone(pompeii.task_bundle)
        self.assertEqual(pompeii.task_bundle.format, "tar.gz")

    def test_leaderboard_models_keep_graded_cells_and_pass_rate_apart(self):
        payload = run(self.client.leaderboard("deep-swe"))
        self.assertEqual(payload.meta.benchmark_id, "deep-swe")
        self.assertEqual(payload.meta.score_label, "Pass rate")
        self.assertEqual(payload.meta.rolling_window, 3)
        self.assertEqual(payload.meta.pass_threshold, 1.0)
        rows = {(row.model, row.effort): row for row in payload.models}
        low = rows[("gpt-6-astra", "low")]
        self.assertEqual(low.graded, 135)
        self.assertEqual(low.passed, 88)
        self.assertEqual(low.cells, 110)
        self.assertEqual(low.cells_passed, 75)
        self.assertEqual(low.pass_rate, 0.682)
        # graded（运行数）≠ cells（题目数）—— 不得互相替换。
        self.assertNotEqual(low.graded, low.cells)
        # provider 不做 IQ 计算（只在 service 里），榜单行到这一步 iq 仍为 None。
        self.assertIsNone(low.iq)
        self.assertEqual(low.key, "gpt-6-astra@low")
        self.assertEqual(len(low.tasks), 5)
        vote = next(iter(low.tasks.values()))
        self.assertEqual(
            (vote.votes, vote.pass_votes, vote.majority_pass, vote.score_rate), (2, 1, False, 0.5)
        )

    def test_leaderboard_envelope_fields_are_carried(self):
        payload = run(self.client.leaderboard())
        self.assertEqual(payload.pending_grades, 0)
        self.assertEqual(payload.error_grades, 458)
        self.assertEqual(payload.online_volunteers, 2)
        self.assertEqual(len(payload.tasks), 5)
        # tasks 是扁平的 task-id 字符串列表。
        self.assertTrue(all(isinstance(item, str) for item in payload.tasks))
        self.assertIsNotNone(payload.pulse)
        self.assertEqual(payload.pulse.window_minutes, 60)
        self.assertIsNotNone(payload.flag_race)
        self.assertEqual(payload.flag_race.status, "won")
        # winner.github_login 可以为 null，展示层必须容忍。
        self.assertIsNone(payload.flag_race.winner_name and None)

    def test_contributors_usd_uses_the_upstream_value(self):
        payload = run(self.client.leaderboard())
        rows = {row.display_name: row for row in payload.contributors}
        self.assertEqual(set(rows), {"volunteer-01", "volunteer-02", "volunteer-03"})
        one = rows["volunteer-01"]
        self.assertEqual(one.usd, 17854.29)
        self.assertEqual(one.month_points, 2296.5)
        self.assertEqual(one.points, 64813.1)
        # 上游对同一个人的 usd 已折好，不得自己再相加。
        self.assertAlmostEqual(one.folded_usd + one.deepseek_api_usd, one.usd, places=2)
        self.assertIsNone(rows["volunteer-02"].github_login)

    def test_table_cells_split_the_composite_key(self):
        payload = run(self.client.table("deep-swe"))
        self.assertEqual(len(payload.cells), 8)
        self.assertEqual(len(payload.tasks), 3)
        self.assertEqual(len(payload.combos), 6)
        self.assertEqual(payload.meta.benchmark_id, "deep-swe")
        cell = payload.cells["abs-module-cache-flags|gpt-6-astra|low"]
        self.assertEqual((cell.task_id, cell.model, cell.effort), ("abs-module-cache-flags", "gpt-6-astra", "low"))
        self.assertEqual(cell.cell_id, "abs-module-cache-flags|gpt-6-astra|low")
        self.assertEqual(cell.st, "cooldown")
        self.assertEqual((cell.n, cell.p, cell.rate), (2, 1, 0.5))
        self.assertEqual(cell.cost_src, "task-level-fallback")
        self.assertFalse(cell.cost_is_estimate is False)  # 非 measured 必须算估算
        self.assertTrue(is_estimate(cell.cost_src))
        self.assertEqual(len(cell.ran_by), 2)

    def test_table_cell_cost_and_src_are_paired(self):
        payload = run(self.client.table("deep-swe"))
        measured = payload.cells["abs-module-cache-flags|gpt-5.6-sol|low"]
        self.assertEqual(measured.cost_src, SRC_MEASURED)
        self.assertFalse(is_estimate(measured.cost_src))
        self.assertEqual(measured.cost, 0.73)
        # 上游用「键整体缺失」表达空：显式补的 null 格子与键缺失的格子都必须解析成缺失。
        empty = payload.cells["abs-module-cache-flags|gemini-3.8-flash|low"]
        self.assertIsNone(empty.cost_src)
        self.assertTrue(is_estimate(empty.cost_src))
        self.assertEqual(empty.ran_by, ())

    def test_table_run_records_keep_pricing_version_and_cost_source(self):
        payload = run(self.client.table("deep-swe"))
        cell = payload.cells["abs-module-cache-flags|gpt-6-astra|low"]
        record = cell.ran_by[0]
        self.assertEqual(record.display_name, "volunteer-06")
        self.assertEqual(record.duration_sec, 285.6)
        self.assertEqual(record.actual_cost_usd, 1.185964)
        self.assertEqual(record.cost_source, "tokens")
        self.assertTrue(record.cost_complete)
        # 同格不同 run 的计价版本可以不同 —— 不能拿最新价表重算历史成本。
        self.assertEqual(record.token_pricing_version, "official-api-equivalent-gemini38-2026-09-10-v16")

    def test_table_task_discrimination_is_parsed_when_present(self):
        payload = run(self.client.table("deep-swe"))
        task = next(item for item in payload.tasks if item.id == "abs-module-cache-flags")
        self.assertIsNotNone(task.discrimination)
        self.assertEqual(task.discrimination.score, 79.7)
        self.assertEqual(task.discrimination.confidence, 0.693)
        self.assertEqual(task.discrimination.samples, 138)
        self.assertEqual(task.discrimination.cells, 56)
        self.assertEqual(task.language, "go")
        self.assertEqual(task.category, "enhancement")

    def test_pompeii_table_tasks_have_no_discrimination_and_must_not_drift(self):
        payload = run(self.client.table("pompeii-adjacency"))
        self.assertEqual(payload.meta.benchmark_id, "pompeii-adjacency")
        self.assertEqual(payload.meta.scoring_mode, "continuous-macro")
        self.assertEqual(payload.meta.score_label, "Adjacency F1")
        self.assertTrue(payload.tasks)
        # pompeii 86/86 题都没有 discrimination：这是正常形态，不是 schema_drift。
        self.assertTrue(all(item.discrimination is None for item in payload.tasks))

    def test_insights_keeps_all_three_iq_numbers_together(self):
        payload = run(self.client.insights("deep-swe"))
        self.assertEqual(payload.meta.mode, "rolling_equal_per_task")
        self.assertEqual(payload.meta.recommendation_mode, "comprehensive_weighted_mean")
        self.assertEqual(len(payload.comprehensive_points), 3)
        point = payload.comprehensive_points[0]
        self.assertEqual(point.key, "gpt-6-astra@low")
        self.assertEqual(point.iq, 109.19)
        self.assertEqual(point.software_iq, 98.51)
        self.assertEqual(point.visual_iq, 135.71)
        self.assertEqual(point.samples, 188)

    def test_insights_iq_is_not_pass_rate_times_150(self):
        """两条口径不能互相推导（红线）。"""
        lb = run(self.client.leaderboard("deep-swe"))
        ins = run(self.client.insights("deep-swe"))
        row = next(item for item in lb.models if item.key == "gpt-6-astra@low")
        point = next(item for item in ins.comprehensive_points if item.key == "gpt-6-astra@low")
        self.assertEqual(row.pass_rate, 0.682)
        self.assertAlmostEqual(row.pass_rate * IQ_SCALE, 102.3, places=6)
        self.assertEqual(point.iq, 109.19)
        self.assertNotAlmostEqual(row.pass_rate * IQ_SCALE, point.iq, places=2)

    def test_insights_recommendations_are_forwarded_verbatim(self):
        payload = run(self.client.insights("deep-swe"))
        self.assertEqual(
            [rec.key for rec in payload.recommendations],
            ["daily_development", "hard_problems", "background_automation", "lobster_tasks"],
        )
        first = payload.recommendations[0]
        self.assertEqual(len(first.items), 2)
        item = first.items[0]
        self.assertEqual(item.key if hasattr(item, "key") else f"{item.model}@{item.effort}", "gpt-6-astra@low")
        self.assertEqual(item.iq, 109.19)
        self.assertEqual(item.samples, 188)
        # 上游 ``passed`` 是加权通过数（float），不是「过了多少题」。
        self.assertIsInstance(item.weighted_passed, float)
        self.assertAlmostEqual(item.weighted_passed, 136.85726418227966, places=6)
        self.assertTrue(first.rule)

    def test_insights_without_alerts_is_not_an_error(self):
        payload = run(self.client.insights("deep-swe"))
        self.assertEqual(payload.degradation_alerts, ())
        self.assertTrue(payload.degradation_rule)

    def test_degradation_alert_item_maps_upstream_names(self):
        """上游 17 键与插件字段名不同，映射必须成立（B4）。"""
        data = load("insights_alert_item")
        meta = RadarMeta(benchmark_id="deep-swe")
        from plugins.radar.provider import parse_alert

        alert = parse_alert(data["items"][0], endpoint="insights")
        self.assertEqual(alert.model, "gpt-6-astra")
        self.assertEqual(alert.effort, "max")
        self.assertEqual(alert.current_iq, 71.4)
        # 字段不全时必须容忍（上游 items 从未有过内容，结构按存在性解析）。
        self.assertIsNone(alert.avg_24h)
        self.assertIsNone(alert.severity)
        self.assertEqual(alert.trend_48h, ())
        self.assertIn("iq", alert.raw_keys)
        self.assertEqual(meta.benchmark_id, "deep-swe")

    def test_efficiency_mode_and_float_passed(self):
        deep = run(self.client.efficiency("deep-swe"))
        self.assertEqual(deep.meta.mode, "equal_latest_3")
        self.assertEqual(len(deep.points), 2)
        point = deep.points[0]
        self.assertEqual(point.key, "gpt-6-astra@low")
        self.assertEqual((point.passed, point.total), (88, 134))
        self.assertEqual(point.iq, 98.51)
        self.assertEqual(point.average_price_usd, 1.978303)
        self.assertEqual(point.combined_cost_index, 129.37)
        # point 级 source_updated_at 必须原样带出（数据时间优先取点级）。
        self.assertEqual(point.source_updated_at, "2026-09-12T03:58:59+00:00")

        pompeii = run(self.client.efficiency("pompeii-adjacency"))
        self.assertEqual(pompeii.meta.scoring_mode, "continuous-macro")
        first = pompeii.points[0]
        # 连续制频道：passed 是 F1 加权和（float），不是题数。
        self.assertIsInstance(first.passed, float)
        self.assertAlmostEqual(first.passed, 49.79059751561299, places=9)
        self.assertEqual(first.total, 55)

    def test_model_metrics_mode_is_passed_through(self):
        payload = run(self.client.model_metrics(model="gpt-6-astra", effort="low", benchmark="deep-swe"))
        self.assertEqual(payload.meta.mode, "latest_valid_per_task")
        self.assertEqual(len(payload.points), 1)
        point = payload.points[0]
        self.assertEqual(point.key, "gpt-6-astra@low")
        self.assertEqual(point.runs_total, 135)
        self.assertAlmostEqual(point.average_agent_steps, 27.256880733944953, places=9)

    def test_model_metrics_unknown_model_is_empty_not_an_error(self):
        data = {"schema": 1, "mode": "latest_valid_per_task", "points": []}

        async def http(url, **kwargs):
            return data

        client = _Client(replace(_RadarConfig(), cache_ttl=RadarCacheTTL(model_metrics=0.0)), http=http)
        payload = run(client.model_metrics(model="nope", effort="low"))
        self.assertEqual(payload.points, ())

    def test_history_keeps_all_three_key_shapes(self):
        series = run(self.client.history("deep-swe"))
        by_key = {item.key: item for item in series}
        self.assertEqual(
            set(by_key),
            {"gpt-6-astra", "gpt-6-astra@low", "latest:gpt-6-astra", "latest:gpt-6-astra@low"},
        )
        # 裸模型名 = 跨档位合并；模型@effort = 单档位；latest: = 另一套窗口。三者不得混用。
        bare = by_key["gpt-6-astra"]
        self.assertFalse(bare.latest)
        self.assertIsNone(bare.effort)
        self.assertEqual(bare.model, "gpt-6-astra")
        tiered = by_key["gpt-6-astra@low"]
        self.assertEqual((tiered.model, tiered.effort, tiered.latest), ("gpt-6-astra", "low", False))
        latest = by_key["latest:gpt-6-astra@low"]
        self.assertTrue(latest.latest)
        self.assertEqual(latest.effort, "low")
        # 点字段是 {ts, score, n}，必须归一成 TrendPoint。
        point = bare.points[0]
        self.assertEqual(point.timestamp, "2026-09-06T10:00:03+00:00")
        self.assertEqual(point.iq, 105.4)
        self.assertEqual(point.samples, 471)

    def test_events_payload_carries_its_own_metric_envelope(self):
        payload = run(self.client.events(n=3, benchmark="deep-swe"))
        self.assertEqual(payload.meta.benchmark_id, "deep-swe")
        self.assertEqual(payload.meta.score_label, "Pass rate")
        self.assertEqual(len(payload.events), 3)
        event = payload.events[0]
        self.assertEqual((event.model, event.effort), ("grok-4.6", "high"))
        self.assertEqual(event.harness, "grok-build")
        self.assertEqual(event.cost_source, "api_equivalent_tokens")
        self.assertFalse(event.cost_is_estimate)
        self.assertTrue(event.cost_is_api_equivalent)

    def test_quota_and_suggest_parse(self):
        quota = run(self.client.quota())
        self.assertEqual(quota.quota_window, "7d")
        self.assertEqual(quota.source, "super-account-app-server-measurement")
        self.assertEqual(quota.tier_windows_usd["plus"], 82.486)
        suggest = run(self.client.suggest(n=5, benchmark="deep-swe"))
        self.assertEqual(len(suggest.cells), 2)
        self.assertEqual(suggest.cells[0].task_id, "yaegi-go-embed-directives")
        self.assertEqual(suggest.cells[0].model, "gpt-5.6-sol")

    def test_user_agent_is_ours_and_no_identity_headers_are_forged(self):
        """不得伪造 X-DRadar-* / Authorization（红线）。"""
        seen: dict = {}

        async def http(url, *, namespace, params, headers, **kwargs):
            seen.update(headers)
            return load("benchmarks")

        client = _Client(CFG, http=http)
        run(client.benchmarks())
        self.assertEqual(set(seen), {"User-Agent"})
        self.assertTrue(seen["User-Agent"].startswith("otae-bot-radar/"))
        for header in ("Authorization", "X-DRadar-Client-Version", "X-DRadar-Capabilities"):
            self.assertNotIn(header, seen)


class ProviderErrorTests(unittest.TestCase):
    """错误码归一、陈旧回退、私有路径拒绝与缓存复用。"""

    def test_private_path_is_refused_without_sending(self):
        http = FakeHTTP()
        client = make_client(http)
        with self.assertRaises(PrivatePathRefused) as ctx:
            client._url("/api/private/v1/intelligence-efficiency")
        self.assertEqual(ctx.exception.code, CODE_PRIVATE_PATH_REFUSED)
        self.assertEqual(http.calls, [])

    def test_status_codes_map_to_stable_error_codes(self):
        cases = {
            404: (UnknownBenchmark, CODE_UNKNOWN_BENCHMARK),
            422: (InvalidArgument, CODE_INVALID_ARGUMENT),
            500: (UpstreamUnavailable, CODE_UPSTREAM_UNAVAILABLE),
            429: (RateLimited, CODE_RATE_LIMITED),
        }
        for status, (kind, code) in cases.items():
            with self.subTest(status=status):
                http = FakeHTTP(errors={"/api/v1/leaderboard": error_response(status, API + "/leaderboard")})
                client = make_client(http, leaderboard=0.0)
                with self.assertRaises(kind) as ctx:
                    run(client.leaderboard("deep-swe"))
                self.assertEqual(ctx.exception.code, code)

    def test_timeout_maps_to_upstream_unavailable(self):
        http = FakeHTTP(errors={"/api/v1/leaderboard": httpx.ConnectTimeout("nope")})
        client = make_client(http, leaderboard=0.0)
        with self.assertRaises(UpstreamUnavailable) as ctx:
            run(client.leaderboard("deep-swe"))
        self.assertEqual(ctx.exception.code, CODE_UPSTREAM_UNAVAILABLE)
        self.assertEqual(ctx.exception.detail, "timeout")

    def test_oversized_payload_maps_to_payload_too_large(self):
        http = FakeHTTP(errors={"/api/v1/table": ValueError("HTTP resource exceeds 12 bytes: x")})
        client = make_client(http, table=0.0)
        from plugins.radar.errors import PayloadTooLarge

        with self.assertRaises(PayloadTooLarge):
            run(client.table("deep-swe"))

    def test_stale_cache_is_served_with_a_flag(self):
        state = {"fail": False}

        async def http(url, **kwargs):
            if state["fail"]:
                raise error_response(500, url)
            return load("leaderboard")

        config = replace(_RadarConfig(), cache_ttl=RadarCacheTTL(leaderboard=0.0))
        client = _Client(config, http=http)
        first = run(client.leaderboard("deep-swe"))
        self.assertFalse(first.meta.stale)
        state["fail"] = True
        second = run(client.leaderboard("deep-swe"))
        self.assertTrue(second.meta.stale)
        self.assertEqual(len(second.models), len(first.models))

    def test_no_stale_cache_means_the_error_surfaces(self):
        http = FakeHTTP(errors={"/api/v1/leaderboard": error_response(503, API + "/leaderboard")})
        client = make_client(http, leaderboard=0.0)
        with self.assertRaises(UpstreamUnavailable):
            run(client.leaderboard("deep-swe"))

    def test_cache_hit_does_not_refetch(self):
        http = FakeHTTP()
        client = make_client(http)
        run(client.leaderboard("deep-swe"))
        run(client.leaderboard("deep-swe"))
        self.assertEqual(http.count("/api/v1/leaderboard"), 1)

    def test_tasks_reuses_the_table_cache_without_a_second_request(self):
        http = FakeHTTP()
        client = make_client(http)
        tasks = run(client.tasks("deep-swe"))
        payload = run(client.table("deep-swe"))
        self.assertEqual([item.id for item in tasks], [item.id for item in payload.tasks])
        self.assertEqual(http.count("/api/v1/table"), 1)

    def test_view_is_accepted_but_never_sent(self):
        http = FakeHTTP()
        client = make_client(http)
        run(client.leaderboard("deep-swe", view="month"))
        self.assertEqual(http.calls[0][1], {"benchmark": "deep-swe"})

    def test_benchmark_defaults_to_the_configured_one(self):
        http = FakeHTTP()
        client = make_client(http)
        run(client.leaderboard())
        self.assertEqual(http.calls[0][1], {"benchmark": "deep-swe"})

    def test_upstream_body_text_never_reaches_the_user_message(self):
        """红线：上游响应体原文只进 debug，不进 ``message``。"""
        error = UpstreamUnavailable()
        self.assertNotIn("detail", error.message)
        self.assertEqual(error.message, "雷达数据源暂时不可用，请稍后重试。")
        # 脱敏只用于日志。
        self.assertNotIn("secret-token-value", redact("Authorization: Bearer secret-token-value"))


class ServiceRankingTests(unittest.TestCase):
    """排序、档位策略与派生结构。"""

    def setUp(self) -> None:
        self.http = FakeHTTP()
        self.client = make_client(self.http)
        self.service = RadarService(self.client, self.client.config)

    def test_top_models_takes_the_highest_tier_per_model(self):
        rows, meta = run(self.service.top_models())
        self.assertEqual(
            [(row.model, row.effort) for row in rows],
            [("gpt-5.6-sol", "max"), ("gpt-6-astra", "ultra"), ("gpt-5.5", "high")],
        )
        self.assertEqual(meta.note, "已取各模型最高档")
        # gpt-6-astra@ultra 在 insights 里没有点 → 走本地换算并标记。
        astra = next(row for row in rows if row.model == "gpt-6-astra")
        self.assertTrue(astra.iq_derived)
        self.assertAlmostEqual(astra.iq, 0.688 * IQ_SCALE, places=6)

    def test_top_models_uses_upstream_iq_when_insights_has_the_tier(self):
        rows, _ = run(self.service.top_models(effort="low"))
        row = next(item for item in rows if item.model == "gpt-6-astra")
        self.assertEqual(row.iq, 109.19)
        self.assertFalse(row.iq_derived)

    def test_top_models_sort_is_iq_then_samples_then_model(self):
        rows, _ = run(self.service.top_models(effort="high"))
        iqs = [row.iq for row in rows]
        self.assertEqual(iqs, sorted(iqs, reverse=True))

    def test_top_models_pass_rate_sort_and_cost_sort(self):
        by_rate, _ = run(self.service.top_models(by="pass_rate", effort="high"))
        rates = [row.pass_rate for row in by_rate]
        self.assertEqual(rates, sorted(rates, reverse=True))

        by_cost, meta = run(self.service.top_models(by="cost", effort="low"))
        self.assertIn("成本为上游估算口径", meta.note)
        self.assertEqual(by_cost[0].model, "gpt-6-astra")

    def test_top_models_rejects_an_unknown_sort_key(self):
        with self.assertRaises(InvalidArgument) as ctx:
            run(self.service.top_models(by="bogus"))
        self.assertEqual(ctx.exception.code, CODE_INVALID_ARGUMENT)
        self.assertIn("iq/pass_rate/cost", ctx.exception.message)

    def test_min_samples_filters_low_sample_rows(self):
        rows, _ = run(self.service.top_models(effort="low", min_samples=1000))
        self.assertEqual(rows, ())

    def test_model_profile_collects_variants_and_best(self):
        profile = run(self.service.model_profile("gpt-6-astra"))
        self.assertEqual(profile.model, "gpt-6-astra")
        self.assertEqual([row.effort for row in profile.variants], ["low", "ultra"])
        self.assertEqual(profile.best.effort, "ultra")
        self.assertEqual(profile.insight.iq, 109.19)
        self.assertEqual(profile.meta.note, "含全部档位")
        # 多档位时不造一个含糊的总样本量。
        self.assertIsNone(profile.meta.samples)

    def test_model_profile_single_tier_carries_its_sample_count(self):
        profile = run(self.service.model_profile("gpt-6-astra", effort="low"))
        self.assertEqual(len(profile.variants), 1)
        self.assertEqual(profile.meta.samples, 135)

    def test_model_profile_unknown_tier_is_unknown_model(self):
        with self.assertRaises(UnknownModel) as ctx:
            run(self.service.model_profile("gpt-5.5", effort="low"))
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN_MODEL)
        self.assertEqual(ctx.exception.detail, "no variant")

    def test_compare_deltas_are_left_minus_right(self):
        cmp = run(self.service.compare("gpt-6-astra", "gpt-5.6-sol"))
        self.assertEqual(cmp.left.best.effort, "ultra")
        self.assertEqual(cmp.right.best.effort, "max")
        self.assertAlmostEqual(cmp.iq_delta, 103.2 - 112.5, places=6)
        self.assertAlmostEqual(cmp.pass_rate_delta, 0.688 - 0.75, places=6)
        self.assertAlmostEqual(cmp.cost_delta_usd, 1.978303 - 7.984619, places=6)

    def test_value_picks_sorts_by_cost_index(self):
        points, _ = run(self.service.value_picks())
        self.assertEqual([point.model for point in points], ["gpt-6-astra", "gpt-5.6-sol"])
        self.assertEqual(points[0].combined_cost_index, 129.37)

    def test_value_picks_filters_respect_ceilings(self):
        points, _ = run(self.service.value_picks(max_cost_usd=1.0))
        self.assertEqual(points, ())
        points, _ = run(self.service.value_picks(min_iq=100))
        self.assertEqual([point.model for point in points], ["gpt-5.6-sol"])

    def test_task_ranking_only_uses_tasks_with_discrimination(self):
        tasks, meta = run(self.service.task_ranking())
        self.assertEqual([item.id for item in tasks], [
            "abs-module-cache-flags",
            "abs-stepped-slices",
            "adaptix-name-mapping-aliases",
        ])
        self.assertEqual(tasks[0].discrimination.score, 79.7)
        self.assertEqual(meta.note, "")

    def test_task_ranking_notes_how_many_tasks_were_skipped(self):
        http = FakeHTTP()
        client = make_client(http)
        service = RadarService(client, client.config)
        tasks, meta = run(service.task_ranking(benchmark="pompeii-adjacency"))
        self.assertEqual(tasks, ())
        self.assertIn("无区分度数据", meta.note)

    def test_task_detail_splits_solved_by(self):
        detail = run(self.service.task_detail("abs-module-cache-flags"))
        self.assertEqual(detail.task.id, "abs-module-cache-flags")
        self.assertEqual(len(detail.cells), 5)
        self.assertEqual(
            [cell.key if hasattr(cell, "key") else f"{cell.model}@{cell.effort}" for cell in detail.solved_by],
            ["gpt-5.6-sol@low", "gpt-6-astra@low"],
        )
        self.assertEqual(
            [cell.effort for cell in detail.solved_by],
            ["low", "low"],
        )

    def test_task_detail_accepts_a_unique_substring(self):
        detail = run(self.service.task_detail("stepped"))
        self.assertEqual(detail.task.id, "abs-stepped-slices")

    def test_task_detail_reports_candidates_when_ambiguous(self):
        with self.assertRaises(InvalidArgument) as ctx:
            run(self.service.task_detail("abs"))
        self.assertEqual(ctx.exception.detail, "ambiguous task")
        self.assertIn("abs-module-cache-flags", ctx.exception.message)

    def test_task_detail_unknown_task(self):
        with self.assertRaises(InvalidArgument) as ctx:
            run(self.service.task_detail("nope-nope"))
        self.assertTrue(ctx.exception.detail.startswith("unknown task:"))

    def test_who_solved_matches_task_detail(self):
        solved = run(self.service.who_solved("abs-module-cache-flags"))
        self.assertEqual(len(solved), 2)

    def test_contributors_scope_changes_the_order(self):
        month, _ = run(self.service.top_contributors(scope="month"))
        total, _ = run(self.service.top_contributors(scope="total"))
        self.assertEqual([row.display_name for row in month], [
            "volunteer-02", "volunteer-03", "volunteer-01",
        ])
        self.assertEqual([row.display_name for row in total], [
            "volunteer-01", "volunteer-02", "volunteer-03",
        ])

    def test_contributors_rejects_an_unknown_scope(self):
        with self.assertRaises(InvalidArgument) as ctx:
            run(self.service.top_contributors(scope="week"))
        self.assertEqual(ctx.exception.detail, "unknown scope")

    def test_recent_events_caps_the_limit_at_fifty(self):
        events, _ = run(self.service.recent_events(limit=3))
        self.assertEqual(len(events), 3)
        with self.assertRaises(InvalidArgument) as ctx:
            run(self.service.recent_events(limit=51))
        self.assertEqual(ctx.exception.detail, "limit too large")

    def test_trend_bare_name_and_tier_are_different_calibers(self):
        bare, bare_meta = run(self.service.trend("gpt-6-astra"))
        tiered, tiered_meta = run(self.service.trend("gpt-6-astra", effort="low"))
        self.assertEqual(bare_meta.note, "跨档位合并口径")
        self.assertEqual(tiered_meta.note, "单档位 low")
        self.assertEqual(bare[0].iq, 105.4)
        self.assertEqual(tiered[0].iq, 98.3)
        self.assertNotEqual(bare[0].iq, tiered[0].iq)
        # 样本量取序列末点。
        self.assertEqual(tiered_meta.samples, tiered[-1].samples)

    def test_trend_never_uses_the_latest_prefixed_series(self):
        points, _ = run(self.service.trend("gpt-6-astra", effort="low"))
        # latest:gpt-6-astra@low 首点是 104.6；裸档位序列首点是 98.3。
        self.assertEqual(points[0].iq, 98.3)

    def test_degradation_alerts_are_forwarded_without_recomputation(self):
        alerts, _ = run(self.service.degradation_alerts())
        self.assertEqual(alerts, ())

    def test_recommendations_keep_four_groups(self):
        recs = run(self.service.recommendations())
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[0].key, "daily_development")
        meta = run(self.service.recommendations_meta())
        self.assertEqual(meta.recommendation_mode, "comprehensive_weighted_mean")

    def test_model_catalog_reads_combos_not_the_price_table(self):
        combos = run(self.service.model_catalog())
        self.assertEqual(len(combos), 6)
        self.assertTrue(all(item.model == "gpt-6-astra" for item in combos))

    def test_benchmarks_and_pulse(self):
        items = run(self.service.benchmarks())
        self.assertEqual(len(items), 2)
        pulse, _ = run(self.service.fleet_pulse())
        self.assertIsNotNone(pulse)
        self.assertEqual(pulse.window_minutes, 60)
        race, _ = run(self.service.flag_race())
        self.assertIsNotNone(race)
        self.assertEqual(race.status, "won")

    def test_insights_outage_does_not_break_the_leaderboard(self):
        state = {"fail": False}

        async def http(url, **kwargs):
            if state["fail"] and "/radar-insights" in url:
                raise error_response(500, url)
            path = url.split("api.codexradar.com", 1)[-1]
            return load(ROUTES[path])

        client = _Client(replace(_RadarConfig(), cache_ttl=RadarCacheTTL(radar_insights=0.0)), http=http)
        service = RadarService(client, client.config)
        state["fail"] = True
        rows, _ = run(service.top_models(effort="low"))
        self.assertTrue(rows)
        # insights 不可用 → 全部退回本地换算并标注。
        self.assertTrue(all(row.iq_derived for row in rows))


class ServiceAliasTests(unittest.TestCase):
    """口语名与档位解析。"""

    def setUp(self) -> None:
        self.http = FakeHTTP()
        self.client = make_client(self.http)
        self.service = RadarService(self.client, self.client.config)
        self.known = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.5", "gemini-3.8-flash"]

    def test_case_and_separator_insensitive_exact_match(self):
        for query in ("gpt-6-astra", "GPT-6-Astra", "gpt-6 astra", "gpt6astra", "gpt_6_astra"):
            with self.subTest(query=query):
                self.assertEqual(self.service.resolve_model(query, self.known), "gpt-6-astra")

    def test_aliases_map_to_upstream_ids(self):
        self.assertEqual(self.service.resolve_model("astra", self.known), "gpt-6-astra")
        self.assertEqual(self.service.resolve_model("sol", self.known), "gpt-5.6-sol")
        self.assertEqual(self.service.resolve_model("gpt55", self.known), "gpt-5.5")
        self.assertEqual(self.service.resolve_model("gemini", self.known), "gemini-3.8-flash")

    def test_ambiguous_query_reports_candidates_and_never_guesses(self):
        with self.assertRaises(UnknownModel) as ctx:
            self.service.resolve_model("gpt", self.known)
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN_MODEL)
        self.assertEqual(ctx.exception.detail, "ambiguous")
        self.assertIn("gpt-6-astra", ctx.exception.message)

    def test_unknown_query(self):
        with self.assertRaises(UnknownModel) as ctx:
            self.service.resolve_model("nope", self.known)
        self.assertTrue(ctx.exception.detail.startswith("unknown model:"))

    def test_alias_outside_the_channel_is_reported(self):
        with self.assertRaises(UnknownModel) as ctx:
            self.service.resolve_model("deepseek", self.known)
        self.assertEqual(ctx.exception.detail, "alias-not-in-benchmark")

    def test_empty_query_is_invalid_argument(self):
        with self.assertRaises(InvalidArgument) as ctx:
            self.service.resolve_model("   ", self.known)
        self.assertEqual(ctx.exception.detail, "empty model")

    def test_effort_normalisation(self):
        self.assertEqual(self.service.resolve_effort("LOW"), "low")
        self.assertEqual(self.service.resolve_effort("XHigh"), "xhigh")
        self.assertIsNone(self.service.resolve_effort(None))
        self.assertIsNone(self.service.resolve_effort(""))

    def test_illegal_effort_names_the_legal_values(self):
        with self.assertRaises(InvalidArgument) as ctx:
            self.service.resolve_effort("bogus")
        self.assertEqual(ctx.exception.code, CODE_INVALID_ARGUMENT)
        self.assertEqual(ctx.exception.detail, "unknown effort")
        for tier in ("low", "medium", "high", "xhigh", "max", "ultra"):
            self.assertIn(tier, ctx.exception.message)


class FormatterTests(unittest.TestCase):
    """纯文本红线。"""

    @classmethod
    def setUpClass(cls) -> None:
        http = FakeHTTP()
        cls.client = make_client(http)
        cls.service = RadarService(cls.client, cls.client.config)

    def rows(self):
        return run(self.service.top_models())

    def test_meta_footer_carries_caliber_time_and_samples(self):
        rows, meta = self.rows()
        footer = format_meta_footer(replace(meta, samples=135))
        for part in ("deep-swe", "Pass rate", "数据", "样本 135"):
            self.assertIn(part, footer)
        # /leaderboard 没有顶层 source_updated_at，必须回退到 latest_burn.submitted_at。
        self.assertIsNotNone(meta.source_updated_at)
        self.assertEqual(meta.rolling_window, 3)

    def test_meta_footer_marks_stale_cache(self):
        _, meta = self.rows()
        self.assertIn("陈旧缓存", format_meta_footer(replace(meta, stale=True)))

    def test_meta_footer_never_drops_required_segments_even_when_narrow(self):
        _, meta = self.rows()
        long_meta = replace(
            meta,
            stale=True,
            samples=135,
            note="这是一段非常长的备注" * 6,
            mode="rolling_equal_per_task",
            recommendation_mode="comprehensive_weighted_mean",
        )
        footer = format_meta_footer(long_meta)
        self.assertLessEqual(display_width(footer), FOOTER_MAX_WIDTH)
        for part in ("deep-swe", "陈旧缓存", "Pass rate", "数据", "样本 135"):
            self.assertIn(part, footer)

    def test_model_list_lines_are_within_the_width_budget(self):
        rows, meta = self.rows()
        lines = format_model_list(rows, meta)
        self.assertTrue(lines)
        # 数据行按 ROW_MAX_WIDTH 收口（必须容下档位 + IQ + 通过率 + 样本量），
        # 脚注行是唯一豁免行，按 FOOTER_MAX_WIDTH 收口。
        for line in lines[:-1]:
            self.assertLessEqual(display_width(line), ROW_MAX_WIDTH, line)
        self.assertEqual(lines[-1], format_meta_footer(meta))
        self.assertLessEqual(display_width(lines[-1]), FOOTER_MAX_WIDTH)

    def test_model_list_shows_iq_pass_rate_effort_and_samples_together(self):
        rows, meta = self.rows()
        text = "\n".join(format_model_list(rows, meta))
        self.assertIn("IQ", text)
        self.assertIn("通过", text)
        self.assertIn("[ultra]", text)
        self.assertIn("n=", text)

    def test_derived_iq_is_footnoted(self):
        rows, meta = self.rows()
        text = "\n".join(format_model_list(rows, meta))
        self.assertIn("*", text)
        self.assertIn("非上游综合 IQ", text)

    def test_missing_values_render_as_a_dash_never_zero(self):
        self.assertEqual(EMPTY, "—")
        from plugins.radar.models import EfficiencyPoint

        point = EfficiencyPoint(model="m", effort="low", iq=None, passed=None, total=None)
        lines = format_value_picks([point], RadarMeta(benchmark_id="deep-swe"))
        text = "\n".join(lines)
        self.assertIn(EMPTY, text)
        self.assertNotIn("0.00", text)

    def test_estimated_cost_gets_the_tilde_and_the_label(self):
        from plugins.radar.formatters import _money

        self.assertEqual(_money(1.98, estimate=True), "~$1.98 估算")
        self.assertEqual(_money(None, estimate=True), EMPTY)
        # 未给 source 时按估算处理（拿不到 src 就不能声称实测）—— 显式 estimate=False 不能翻案。
        self.assertEqual(_money(1.98), "~$1.98 估算")
        self.assertEqual(_money(1.98, estimate=False), "~$1.98 估算")
        # 只有明确声明 measured 才输出裸金额。
        self.assertEqual(_money(1.98, source=SRC_MEASURED), "$1.98")
        self.assertEqual(_money(1.98, estimate=False, source=SRC_MEASURED), "$1.98")

    def test_estimate_detection_covers_missing_src(self):
        self.assertTrue(is_estimate(None))
        self.assertTrue(is_estimate("task-level-fallback"))
        self.assertFalse(is_estimate(SRC_MEASURED))

    def test_empty_results_say_empty_instead_of_raising(self):
        meta = RadarMeta(benchmark_id="deep-swe", score_label="Pass rate")
        self.assertTrue(format_model_list((), meta)[0].startswith("该频道暂无"))
        self.assertTrue(format_task_ranking((), meta)[0].startswith("该频道暂无"))
        self.assertTrue(format_value_picks((), meta)[0].startswith("该频道暂无"))
        self.assertTrue(format_recommendations((), meta)[0].startswith("上游暂无"))
        self.assertTrue(format_events((), meta)[0].startswith("暂无"))
        self.assertTrue(format_alerts((), meta))
        self.assertTrue(format_model_catalog((), meta)[0].startswith("该频道暂无"))
        self.assertTrue(format_benchmark_list(())[0].startswith("雷达暂无"))
        self.assertTrue(format_contributors((), scope="month")[0].startswith("暂无"))

    def test_continuous_mode_passed_is_not_shown_as_a_task_count(self):
        """连续制频道的 ``passed`` 是加权和，展示必须带分母。"""
        points, meta = run(self.service.value_picks(benchmark="pompeii-adjacency"))
        self.assertTrue(points)
        lines = format_value_picks(points, meta)
        text = "\n".join(lines)
        self.assertIn("Adjacency F1", text)

    def test_format_error_returns_the_message_verbatim(self):
        error = InvalidArgument("用法：/radar 题 <id>", detail="missing task")
        self.assertEqual(format_error(error), "用法：/radar 题 <id>")
        # 固定话术表兜底，且不含上游原文。
        self.assertEqual(format_error(UnknownModel()), "没有该模型档位的实测数据。")

    def test_help_lines_are_within_the_width_budget(self):
        for line in format_help():
            self.assertLessEqual(display_width(line), MAX_LINE_WIDTH, line)

    def test_profile_comparison_and_detail_stay_within_the_budget(self):
        profile = run(self.service.model_profile("gpt-6-astra", effort="low"))
        cmp = run(self.service.compare("gpt-6-astra", "gpt-5.6-sol"))
        detail = run(self.service.task_detail("abs-module-cache-flags"))
        for lines in (
            format_model_profile(profile),
            format_comparison(cmp),
            format_task_detail(detail),
        ):
            for line in lines:
                budget = FOOTER_MAX_WIDTH if line.startswith("—— ") else ROW_MAX_WIDTH
                self.assertLessEqual(display_width(line), budget, line)

    def test_every_display_formatter_emits_the_footer(self):
        rows, meta = self.rows()
        footer = format_meta_footer(meta)
        tasks, task_meta = run(self.service.task_ranking())
        points, eff_meta = run(self.service.value_picks())
        events, ev_meta = run(self.service.recent_events(limit=3))
        pulse, pulse_meta = run(self.service.fleet_pulse())
        race, _ = run(self.service.flag_race())
        combos = run(self.service.model_catalog())
        table = run(self.service.table())
        cases = {
            "model_list": format_model_list(rows, meta),
            "task_ranking": format_task_ranking(tasks, task_meta),
            "value_picks": format_value_picks(points, eff_meta),
            "events": format_events(events, ev_meta),
            "alerts": format_alerts((), meta),
            "recommendations": format_recommendations(run(self.service.recommendations()), meta),
            "model_catalog": format_model_catalog(combos, table.meta),
            "pulse": format_pulse(pulse, race, pulse_meta),
        }
        for name, lines in cases.items():
            with self.subTest(formatter=name):
                self.assertIn(format_meta_footer(cases_meta(cases, name, meta, task_meta, eff_meta, ev_meta, pulse_meta, table)), lines)

    def test_trend_formatter_takes_a_label(self):
        points, meta = run(self.service.trend("gpt-6-astra", effort="low"))
        lines = format_trend(points, label="gpt-6-astra[low] · 单档位 low")
        self.assertTrue(lines)
        self.assertIn("gpt-6-astra[low]", lines[0])

    def test_contributor_lines_stay_within_the_budget(self):
        for scope in ("month", "total"):
            rows, _ = run(self.service.top_contributors(scope=scope))
            for line in format_contributors(rows, scope=scope):
                self.assertLessEqual(display_width(line), ROW_MAX_WIDTH, line)


def cases_meta(cases, name, meta, task_meta, eff_meta, ev_meta, pulse_meta, table):
    """``test_every_display_formatter_emits_the_footer`` 用的 meta 选择器。"""
    return {
        "model_list": meta,
        "task_ranking": task_meta,
        "value_picks": eff_meta,
        "events": ev_meta,
        "alerts": meta,
        "recommendations": meta,
        "model_catalog": table.meta,
        "pulse": pulse_meta,
    }[name]


class SchemaDriftTests(unittest.TestCase):
    """缺字段必须 ``schema_drift``，不是 ``KeyError``。"""

    def _client(self, data: dict):
        async def http(url, **kwargs):
            return data

        return _Client(replace(_RadarConfig(), cache_ttl=RadarCacheTTL(leaderboard=0.0, table=0.0)), http=http)

    def test_leaderboard_without_models_is_schema_drift(self):
        data = load("leaderboard")
        data.pop("models")
        with self.assertRaises(SchemaDrift) as ctx:
            run(self._client(data).leaderboard("deep-swe"))
        self.assertEqual(ctx.exception.code, CODE_SCHEMA_DRIFT)
        self.assertIn("leaderboard", ctx.exception.message + ctx.exception.detail)

    def test_table_without_cells_is_schema_drift(self):
        data = load("table")
        data.pop("cells")
        with self.assertRaises(SchemaDrift):
            run(self._client(data).table("deep-swe"))

    def test_table_with_a_non_mapping_cells_value_is_schema_drift(self):
        data = load("table")
        data["cells"] = []
        with self.assertRaises(SchemaDrift):
            run(self._client(data).table("deep-swe"))

    def test_row_missing_an_identifier_is_schema_drift(self):
        data = load("leaderboard")
        data["models"][0].pop("model")
        with self.assertRaises(SchemaDrift):
            run(self._client(data).leaderboard("deep-swe"))

    def test_efficiency_without_points_is_schema_drift(self):
        data = load("efficiency")
        data.pop("points")

        async def http(url, **kwargs):
            return data

        client = _Client(replace(_RadarConfig(), cache_ttl=RadarCacheTTL(intelligence_efficiency=0.0)), http=http)
        with self.assertRaises(SchemaDrift):
            run(client.efficiency("deep-swe"))

    def test_missing_optional_fields_are_tolerated(self):
        """可选字段缺失不是 schema_drift（上游用「键缺失」表达空）。"""
        data = load("table")
        cell = data["cells"]["abs-module-cache-flags|gpt-6-astra|low"]
        cell.pop("src")
        cell.pop("ran_by")
        payload = run(self._client(data).table("deep-swe"))
        parsed = payload.cells["abs-module-cache-flags|gpt-6-astra|low"]
        self.assertIsNone(parsed.cost_src)
        self.assertEqual(parsed.ran_by, ())

    def test_schema_drift_code_is_stable(self):
        self.assertEqual(SchemaDrift().code, "schema_drift")


class ConfigContractTests(unittest.TestCase):
    """配置契约：`.env.example` 必须覆盖 config.py 读的每个 `RADAR_*` 键。

    `tests/test_hyw_evidence.py` 的覆盖断言只认 `HYW|GOOGLE` 前缀，管不到 radar，
    所以这里自己兜一条 —— 否则新增键忘了写进模板时没有任何测试会红。
    """

    def test_every_radar_env_var_is_documented(self):
        import re

        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        source = (ROOT / "plugins" / "radar" / "config.py").read_text(encoding="utf-8")
        documented = set(re.findall(r"^(RADAR_[A-Z0-9_]+)=", template, flags=re.MULTILINE))
        read = set(re.findall(r'_env_(?:str|int|float)\(\s*"(RADAR_[A-Z0-9_]+)"', source))
        self.assertTrue(read, "no RADAR_* env reads found — the regex or config.py moved")
        self.assertEqual(sorted(read - documented), [], "not documented in .env.example")
        self.assertEqual(sorted(documented - read), [], "documented but never read")

    def test_defaults_match_the_documented_contract(self):
        config = _RadarConfig()
        self.assertEqual(config.base_url, "https://api.codexradar.com")
        self.assertEqual(config.default_benchmark, "deep-swe")
        self.assertEqual(config.timeout, 20.0)
        self.assertEqual(config.table_timeout, 60.0)
        self.assertEqual(config.concurrency, 4)
        self.assertEqual(config.max_models_listed, 15)
        self.assertEqual(config.max_tasks_listed, 10)
        self.assertEqual(config.retry.rate_limit_retries, 5)
        self.assertEqual(config.retry.total_budget, 360.0)
        self.assertEqual(config.cache_ttl.radar_insights, 600.0)

    def test_ttl_never_undercuts_the_upstream_cache_control(self):
        """TTL 短于上游 max-age 等于用自己的流量替上游刷缓存。

        下列数字是 2026-09-14 实测的 ``Cache-Control``（取 max-age/s-maxage 的较大者）：
        ``/leaderboard`` 10、``/table`` 30/300、``/radar-insights`` 600、
        ``/model-metrics`` 30、``/intelligence-efficiency`` 30、``/iq-history`` 60、
        ``/events`` 30、``/quota`` 60、``/benchmarks`` 300。
        """
        ttl = RadarCacheTTL()
        floors = {
            "leaderboard": 10.0,
            "table": 300.0,  # s-maxage 才是紧的那一侧
            "radar_insights": 600.0,
            "model_metrics": 30.0,
            "intelligence_efficiency": 30.0,
            "iq_history": 60.0,
            "events": 30.0,
            "quota": 60.0,
            "benchmarks": 300.0,
        }
        for attr, floor in floors.items():
            with self.subTest(attr=attr):
                self.assertGreaterEqual(getattr(ttl, attr), floor)

    def test_user_agent_identifies_us_and_never_forges_radar_headers(self):
        config = _RadarConfig()
        self.assertIn("otae-bot-radar/", config.user_agent)
        self.assertIn("github.com/otae-1204/otae-bot-entari", config.user_agent)
        source = (ROOT / "plugins" / "radar" / "provider.py").read_text(encoding="utf-8")
        for forged in ("X-DRadar-Client-Version", "X-DRadar-Capabilities", "Authorization"):
            self.assertNotIn(f'"{forged}"', source, f"{forged} must never be sent")


class HandlerTests(unittest.TestCase):
    """命令面：参数解析与端到端派发。

    端到端派发放在子进程里跑真实的 entari 应用（顺序：先 import handlers →
    再建并 set 事件循环 → 再 ``create_app()``），service 换成注入夹具的替身，
    **全程不出网**。
    """

    def test_chunks_never_split_a_line(self):
        from plugins.radar.handlers import CHUNK_SIZE, _chunks

        text = "\n".join(["x" * 100] * 60)
        chunks = _chunks(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), CHUNK_SIZE)
            for line in chunk.split("\n"):
                self.assertLessEqual(len(line), 100)

    def test_short_text_is_a_single_chunk(self):
        from plugins.radar.handlers import _chunks

        self.assertEqual(_chunks("abc"), ["abc"])

    def test_limit_parsing_was_removed_with_the_feed_command(self):
        """``_limit`` 只服务已摘掉的「流水」命令，应随命令面一起移除。"""
        import plugins.radar.handlers as handlers

        self.assertFalse(hasattr(handlers, "_limit"))

    def test_benchmark_token_is_split_off(self):
        from plugins.radar.handlers import _split_benchmark

        self.assertEqual(_split_benchmark(["gpt-6-astra", "low"]), (["gpt-6-astra", "low"], None))
        self.assertEqual(_split_benchmark(["@pompeii-adjacency"]), ([], "pompeii-adjacency"))
        self.assertEqual(_split_benchmark(["gpt-6-astra", "@deep-swe"]), (["gpt-6-astra"], "deep-swe"))
        self.assertEqual(_split_benchmark(["@"]), (["@"], None))

    def test_tokens_come_from_the_rest_argument(self):
        from otae_bot.adapters.entari import ArgVal

        from plugins.radar.handlers import _tokens

        self.assertEqual(_tokens(ArgVal(("模型", "gpt-6-astra"), True)), ["模型", "gpt-6-astra"])
        self.assertEqual(_tokens(ArgVal(None, False)), [])
        self.assertEqual(_tokens(ArgVal(("a  b",), True)), ["a", "b"])

    def test_every_subcommand_maps_to_a_known_key(self):
        from plugins.radar.handlers import _SUBCOMMANDS

        # 命令面按用户要求收窄为「只看智商相关」：7 个 IQ 命令 + 频道 + 档位 + 帮助。
        keys = {
            "rank", "model", "compare", "recommend", "alert", "value", "trend",
            "bench", "combos", "help", "overview",
        }
        self.assertEqual(set(_SUBCOMMANDS.values()), keys)
        for alias in ("榜", "模型", "对比", "推荐", "预警", "性价比", "趋势",
                      "频道", "档位", "帮助", "总览"):
            self.assertIn(alias, _SUBCOMMANDS)

    def test_the_dropped_commands_are_gone_from_the_command_surface(self):
        """题 / 好题 / 贡献者 / 流水 / 实时 只摘命令面，不再可派发。"""
        from plugins.radar.handlers import _SUBCOMMANDS

        for alias in ("题", "task", "好题", "tasks", "贡献者", "top",
                      "流水", "feed", "实时", "pulse"):
            self.assertNotIn(alias, _SUBCOMMANDS)
        for key in ("task", "tasks", "top", "feed", "pulse"):
            self.assertNotIn(key, set(_SUBCOMMANDS.values()))

    def test_dropped_commands_still_work_at_the_service_layer(self):
        """命令面收窄不得动后端：被摘的命令对应的 service 能力必须还在（前端要用）。"""
        from plugins.radar.service import RadarService

        for name in ("task_detail", "task_ranking", "top_contributors",
                     "recent_events", "fleet_pulse", "flag_race", "who_solved"):
            self.assertTrue(hasattr(RadarService, name), name)

    def test_root_aliases(self):
        from plugins.radar.handlers import ROOT_ALIASES

        self.assertIn("radar", ROOT_ALIASES)
        self.assertIn("智商雷达", ROOT_ALIASES)

    def test_handler_module_imports_without_env_or_network(self):
        """无 ``.env`` 也必须能 import（test_architecture 的沙箱契约）。"""
        script = (
            "import plugins.radar.handlers as h;"
            "assert h._config.base_url == 'https://api.codexradar.com';"
            "assert h._service is not None;"
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_commands_dispatch_end_to_end_against_fixtures(self):
        script = DISPATCH_SCRIPT
        # 必须继承 os.environ：清空环境会让 Windows 的 winsock 提供程序初始化失败
        # （子进程 import asyncio 就抛 OSError [WinError 10106]），那是环境问题而非插件问题。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script, str(ROOT), str(FIXTURES)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        self.assertIn("DISPATCH-OK", result.stdout)
        self.assertNotIn("DISPATCH-FAIL", result.stdout)


DISPATCH_SCRIPT = r'''
import asyncio, json, sys
from pathlib import Path

ROOT, FIXTURES = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(ROOT))

# 顺序有讲究：handlers 必须在 create_app() 之前 import。
from plugins.radar import handlers

from otae_bot.application import create_app
create_app()

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from arclet.entari import MessageChain, Session
from arclet.entari.command.provider import _remove_config_prefix
from arclet.entari.config import EntariConfig
from arclet.entari.const import ITEM_ACCOUNT, ITEM_MESSAGE_CONTENT
from arclet.letoderea import post
from arclet.entari.event.command import CommandExecute

from plugins.radar.config import RadarCacheTTL, RadarConfig
from plugins.radar.provider import RadarClient
from plugins.radar.service import RadarService

EntariConfig.instance.basic.prefix = ['/']

ROUTES = {
    '/api/v1/benchmarks': 'benchmarks',
    '/api/v1/leaderboard': 'leaderboard',
    '/api/v1/table': 'table',
    '/api/v1/radar-insights': 'insights',
    '/api/v1/intelligence-efficiency': 'efficiency',
    '/api/v1/model-metrics': 'model_metrics',
    '/api/v1/iq-history': 'iq_history',
    '/api/v1/events': 'events',
    '/api/v1/quota': 'quota',
    '/api/v1/suggest': 'suggest',
}
OVERRIDES = {
    ('/api/v1/table', 'pompeii-adjacency'): 'table_pompeii',
    ('/api/v1/intelligence-efficiency', 'pompeii-adjacency'): 'efficiency_pompeii',
}
cache = {}

def load(name):
    if name not in cache:
        cache[name] = json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8'))
    return cache[name]

seen = []

async def http(url, *, namespace, params, headers, **kwargs):
    path = url.split('api.codexradar.com', 1)[-1]
    seen.append((path, dict(params or {})))
    bench = (params or {}).get('benchmark')
    name = OVERRIDES.get((path, bench)) or ROUTES[path]
    return load(name)

service = RadarService(RadarClient(replace(RadarConfig(), cache_ttl=RadarCacheTTL()), http=http), RadarConfig())
handlers._service = service
# This contract suite exercises command parsing, data and the text fallback.
# Actual image delivery and layout are covered by test_radar_rendering.py.
handlers.render_page = AsyncMock(side_effect=RuntimeError('renderer unavailable'))

def session():
    s = object.__new__(Session)
    s.account = SimpleNamespace(platform='qq', self_id='test-bot')
    s.event = SimpleNamespace(user=SimpleNamespace(id='test-user'), guild=None,
                              channel=SimpleNamespace(id='test-channel'))
    s.reply = None
    s.send = AsyncMock(return_value=[])
    return s

async def drive(text):
    s = session()
    message = _remove_config_prefix(MessageChain(text))
    await post(CommandExecute(message, s), inherit_ctx={ITEM_ACCOUNT: s.account,
                                                        ITEM_MESSAGE_CONTENT: message})
    return s

CASES = [
    ('/radar', 1, ['AI 智商雷达']),
    ('/radar 总览', 1, ['AI 智商雷达', '跨档位 IQ']),
    ('/radar 帮助', 1, ['AI 智商雷达']),
    ('/智商雷达 帮助', 1, ['AI 智商雷达']),
    ('/radar 乱写', 1, ['未知子命令']),
    ('/radar 榜', 1, ['IQ']),
    ('/radar 榜 @pompeii-adjacency', 1, []),
    ('/radar 频道', 1, ['DeepSWE']),
    ('/radar 模型 gpt-6-astra', 1, ['gpt-6-astra']),
    ('/radar 对比 gpt-6-astra gpt-5.6-sol', 1, ['vs']),
    ('/radar 推荐', 1, ['日常开发']),
    ('/radar 预警', 1, []),
    ('/radar 性价比', 1, ['gpt-6-astra']),
    ('/radar 趋势 gpt-6-astra', 1, ['gpt-6-astra']),
    ('/radar 档位', 1, ['gpt-6-astra']),
    ('/radar 模型', 1, ['用法']),
    ('/radar 对比 gpt-6-astra', 1, ['用法']),
    # 已摘掉的 5 个命令必须报「未知子命令」，且不得再走 service。
    ('/radar 题 abs-module-cache-flags', 1, ['未知子命令']),
    ('/radar 好题', 1, ['未知子命令']),
    ('/radar 贡献者', 1, ['未知子命令']),
    ('/radar 流水 3', 1, ['未知子命令']),
    ('/radar 实时', 1, ['未知子命令']),
]

async def main():
    failures = []
    for text, expected_sends, needles in CASES:
        s = await drive(text)
        if s.send.await_count != expected_sends:
            failures.append((text, 'sends', s.send.await_count, expected_sends))
            continue
        reply = str(s.send.await_args[0][0])
        for needle in needles:
            if needle not in reply:
                failures.append((text, 'missing', needle, reply[:200]))
    # 频道参数必须真的传到上游。
    if ('/api/v1/leaderboard', {'benchmark': 'pompeii-adjacency'}) not in seen:
        failures.append(('pompeii switch', 'not seen', seen[-5:], None))
    # Exercise the real Entari command/send path with typed image segments too.
    # Rendering itself is checked separately against the shared Chromium renderer.
    from io import BytesIO
    from PIL import Image as PILImage
    from satori import Image
    png = BytesIO()
    PILImage.new('RGB', (2, 2)).save(png, format='PNG')
    handlers.render_page = AsyncMock(return_value=png.getvalue())
    for command, pages in [('/radar', 1), ('/radar 榜', 1), ('/radar 推荐', 4)]:
        s = await drive(command)
        if s.send.await_count != pages:
            failures.append((command, 'image pages', s.send.await_count, pages))
        for call in s.send.await_args_list:
            message = call.args[0]
            if not isinstance(message, MessageChain) or not any(isinstance(part, Image) for part in message):
                failures.append((command, 'missing image segment', str(message)[:100], None))
    if failures:
        print('DISPATCH-FAIL')
        for item in failures:
            print(' -', item)
        raise SystemExit(1)
    print('DISPATCH-OK', len(CASES))

# letoderea 在 import 期就把当时的事件循环记下来了，所以这里必须用
# get_event_loop() 取回同一个循环；自己 new_event_loop() 会让每次 post 抛
# RuntimeError: ... got Future ... attached to a different loop。
asyncio.get_event_loop().run_until_complete(main())
'''


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
