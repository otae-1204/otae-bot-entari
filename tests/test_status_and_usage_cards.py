"""``/status`` 与 ``/统计`` 的权限、参数、卡片内容与降级。"""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage
from satori import ChannelType

from otae_bot.command_usage.store import (
    CallRecord,
    Scope,
    TodaySummary,
    UsageReport,
    UsageStore,
)
from otae_bot.compat.entari import ArgVal
from otae_bot.infrastructure.recent_alerts import Alert
from plugins.status import handlers as status_handlers
from tests.support.dispatch import run_scenarios
from plugins.status.collect import Job, Meter, StatusSnapshot
from plugins.status.presentation import panel_html as status_panel
from plugins.usage_stats import handlers as stats_handlers
from plugins.usage_stats.presentation import format_text
from plugins.usage_stats.presentation import panel_html as stats_panel
from plugins.usage_stats.query import parse_query

NOW = datetime(2026, 10, 27, 15, 0, 0)
PLUGINS = ["endfield", "hyw", "radar"]


def event(*, user="200", group="100", private=False):
    return SimpleNamespace(
        user=SimpleNamespace(id=user),
        guild=None if private else SimpleNamespace(id=group),
        channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT),
    )


def png() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (1, 1)).save(buffer, format="PNG")
    return buffer.getvalue()


def snapshot(**overrides) -> StatusSnapshot:
    values = {
        "now": NOW,
        "version": "v1.36.1",
        "started_at": NOW - timedelta(days=3, hours=7),
        "accounts": [("qq", "3940000000", 12)],
        "render": {"inflight": 0, "waiting": {"interactive": 0, "background": 0}, "concurrency": 2, "contexts": 1},
        "render_timing": {"count": 3, "failures": 0, "mean": 1.5, "p95": 2.0},
        "loop_lag": {"last": 0.004, "max": 0.03},
        "meters": [Meter("CPU", 0.1, "10%"), Meter("内存", 0.3, "3 GB / 10 GB")],
        "plugins": [("endfield", True), ("forkout", False), ("peek", True), ("group_manager", True), ("eft_helper", True)],
        "today": TodaySummary(12, 10, 1, 900.0, [0] * 24, [("/ef 持有率", 5)]),
        "endpoints": ["localhost:5500"],
        "jobs": [Job("Tibo 雷达 · refresh", 600, NOW - timedelta(minutes=3), False, "HTTPStatusError: 502", 2, False)],
        "alerts": [Alert(NOW, "WARNING", "x", "上游 502")],
        "runtime_info": [("Python", "3.12")],
    }
    values.update(overrides)
    return StatusSnapshot(**values)


class StatusPermissionTests(unittest.TestCase):
    def test_detail_only_for_superuser_in_private_chat(self):
        with patch.object(status_handlers, "is_superuser", side_effect=lambda e: e.user.id == "1"):
            self.assertTrue(status_handlers.wants_detail(event(user="1", private=True)))
            self.assertFalse(status_handlers.wants_detail(event(user="1")))
            self.assertFalse(status_handlers.wants_detail(event(user="2", private=True)))


class StatusCardTests(unittest.TestCase):
    def test_public_card_hides_internal_details(self):
        html = status_panel(snapshot(), detail=False)
        for hidden in ("localhost:5500", "3940000000", "窥视", "群管理", "eft_helper", "后台任务", "最近告警", "上游 502", "Python"):
            self.assertNotIn(hidden, html)
        for shown in ("终末地", "叉出去", "1 个账号在线", "今日指令", "/ef 持有率"):
            self.assertIn(shown, html)

    def test_detail_card_includes_jobs_alerts_admin_plugins_and_runtime(self):
        html = status_panel(snapshot(), detail=True)
        for shown in ("localhost:5500", "12 个群", "窥视", "群管理", "HTTPStatusError: 502", "上游 502", "Python", "仅超管私聊可见"):
            self.assertIn(shown, html)

    def test_untrusted_text_is_escaped(self):
        html = status_panel(snapshot(alerts=[Alert(NOW, "ERROR", "x", "<script>bad()</script>")]), detail=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class QueryTests(unittest.TestCase):
    def test_words_combine_in_any_order(self):
        self.assertEqual(parse_query("", PLUGINS), parse_query("7天", PLUGINS))
        query = parse_query("30天 全局 终末地", PLUGINS)
        self.assertEqual((query.global_scope, query.days, query.plugin), (True, 30, "endfield"))
        self.assertEqual(parse_query("q 14d", PLUGINS).plugin, "hyw")

    def test_bad_words_and_ranges_explain_usage(self):
        self.assertIn("1 到 90", parse_query("365天", PLUGINS).error)
        self.assertIn("看不懂", parse_query("明天", PLUGINS).error)


class UsageCardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = UsageStore(Path(directory.name) / "usage.sqlite3")
        self.addCleanup(self.store.close)

    def report(self, scope=None, days=7):
        return self.store.report(scope or Scope("qq", "100"), days, NOW)

    def test_group_and_global_titles_and_user_sections(self):
        self.store.insert_many([
            CallRecord((NOW - timedelta(hours=2)).timestamp(), "qq", "100", "1", "endfield", "/ef", "持有率", 800, True, "<b>夜岚</b>"),
        ])
        group_html = stats_panel(self.report(), 7, NOW)
        self.assertIn("最常用的群友", group_html)
        self.assertIn("&lt;b&gt;夜岚&lt;/b&gt;", group_html)
        self.assertNotIn("<b>夜岚</b>", group_html)
        self.assertIn("使用量最高的用户", stats_panel(self.report(Scope()), 7, NOW))
        self.assertIn("1. /ef 持有率  1 次", format_text(self.report(), 7))

    def test_empty_report_shows_a_single_hint(self):
        html = stats_panel(self.report(), 1, NOW)
        self.assertIn("这段时间还没有人用过指令", html)
        self.assertNotIn("指令排行", html)


class UsageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def run_command(self, text, ev, render=None):
        self.seen = []

        def report(scope, days, now):
            self.seen.append((scope, days))
            return UsageReport(scope, now, now)

        recorder = SimpleNamespace(flush=AsyncMock(), store=SimpleNamespace(report=report))
        sent = []
        session = SimpleNamespace(event=ev, account=SimpleNamespace(platform="qq"))
        with patch.object(stats_handlers, "usage_recorder", recorder), \
             patch.object(stats_handlers, "loaded_group_plugins", return_value=PLUGINS), \
             patch.object(stats_handlers, "render_card", render or AsyncMock(return_value=png())), \
             patch.object(stats_handlers, "send", AsyncMock(side_effect=lambda s, m: sent.append(m))):
            await stats_handlers.handle_usage_stats(ArgVal(tuple(text.split()) or None, bool(text)), session)
        return sent

    async def test_scope_follows_chat_and_global_word(self):
        await self.run_command("", event())
        self.assertEqual(self.seen[-1], (Scope("qq", "100"), 7))
        await self.run_command("全局 30天", event())
        self.assertEqual(self.seen[-1], (Scope(), 30))
        await self.run_command("", event(private=True))
        self.assertEqual(self.seen[-1], (Scope(), 7))

    async def test_render_failure_falls_back_to_text_and_bad_input_explains(self):
        sent = await self.run_command("", event(), render=AsyncMock(side_effect=RuntimeError("no browser")))
        self.assertIsInstance(sent[-1], str)
        self.assertIn("指令统计", sent[-1])
        sent = await self.run_command("明天", event())
        self.assertIn("看不懂", sent[-1])



STATUS_SCENARIOS = r'''
from unittest.mock import AsyncMock, patch
from otae_bot.config.settings import Config

status = plugin('plugins.status.handlers')
Config.SUPERUSERS = ['42']
seen = []


from io import BytesIO
from PIL import Image as PILImage
buffer = BytesIO()
PILImage.new('RGB', (1, 1)).save(buffer, 'PNG')
PNG = buffer.getvalue()


async def card(model):
    seen.append(model)
    return PNG


async def main():
    # 别名同样可用；卡片作为图片发出
    with patch.object(status, 'render_card', card):
        for text in ('/status', '/状态', '/status 多余参数'):
            sent = await say(text)
            assert len(sent) == 1 and sent[0].startswith('<img') and 'base64' in sent[0], sent
    # 卡片渲染失败退回文本
    with patch.object(status, 'render_card', AsyncMock(side_effect=RuntimeError('no browser'))):
        sent = await say('/status')
        assert len(sent) == 1 and not sent[0].startswith('<img'), sent

    # 详细版只给私聊里的超管；超管在群里、普通人在私聊里都是公开版
    collect = AsyncMock(side_effect=RuntimeError('stop here'))
    with patch.object(status, 'collect', collect):
        for kwargs, detail in ((dict(user='42', private=True), True), (dict(user='42'), False),
                               (dict(user='7', private=True), False)):
            collect.reset_mock()
            await say('/status', **kwargs)
            assert collect.await_args.kwargs == {'detail': detail}, (kwargs, collect.await_args)
    print('ALL-OK')

run(main())
'''


class StatusDispatchTests(unittest.TestCase):
    def test_status_through_the_real_event_path(self):
        result = run_scenarios(STATUS_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
