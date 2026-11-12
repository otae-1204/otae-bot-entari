"""指令统计采集层：存储与汇总、子命令登记、经真实 Alconna 解析的钩子，以及运行状态用到的框架快照。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import MessageChain
from arclet.entari.command.provider import AlconnaSuppiler
from arclet.entari.const import ITEM_MESSAGE_CONTENT, ITEM_SESSION
from arclet.letoderea import STOP
from arclet.letoderea import Scope as EventScope
from nepattern import AnyString
from satori import ChannelType
from tarina import LRU

from otae_bot.command_usage import recorder as recorder_module
from otae_bot.command_usage.recorder import (
    CommandUsageRecorder,
    UsageRecorder,
    describe_command,
    install_command_usage_recorders,
    resolve_command,
)
from otae_bot.command_usage.store import CallRecord, Scope, UsageStore
from otae_bot.pipeline import framework_hooks

NOW = datetime(2026, 10, 27, 15, 0, 0)


def record(
    at: datetime, *, group="100", user="1", command="/ef", sub="持有率", plugin="endfield", ok=True, ms=1000,
    name="夜岚", nickname="",
):
    return CallRecord(at.timestamp(), "qq", group, user, plugin, command, sub, ms, ok, name, nickname)


def session(group="100", user="200", private=False):
    return SimpleNamespace(
        account=SimpleNamespace(platform="qq", self_id="300"),
        event=SimpleNamespace(
            guild=None if private else SimpleNamespace(id=group),
            channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT),
            user=SimpleNamespace(id=user, name="u" + user, nick=None),
            member=SimpleNamespace(nick="名片" + user),
            message=None,
        ),
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = UsageStore(Path(directory.name) / "usage.sqlite3")
        self.addCleanup(self.store.close)

    def test_group_report_counts_ranks_and_compares_with_the_previous_period(self):
        self.store.insert_many(
            [
                record(NOW - timedelta(days=1), user="1"),
                record(NOW - timedelta(days=1), user="1"),
                record(NOW - timedelta(days=2), user="2", command="/q", sub="", plugin="hyw", ok=False, ms=3000),
                record(NOW - timedelta(days=9), user="1"),  # 上一期
                record(NOW - timedelta(days=1), group="101", user="3"),  # 别的群
            ]
        )
        report = self.store.report(Scope("qq", "100"), 7, NOW)
        self.assertEqual((report.total, report.users, report.failures, report.previous_total), (3, 2, 1, 1))
        self.assertEqual([(row.label, row.count, row.previous) for row in report.commands], [("/ef 持有率", 2, 1), ("/q", 1, 0)])
        self.assertEqual(report.plugins, [("endfield", 2), ("hyw", 1)])
        self.assertEqual(len(report.days), 7)
        self.assertEqual(report.days[-1][0], "2026-10-27")
        self.assertEqual(sum(count for _, count in report.days), 3)
        self.assertEqual(sum(map(sum, report.heat)), 3)
        self.assertEqual([(user.user_id, user.count) for user in report.top_users], [("1", 2), ("2", 1)])
        # 用户 1 在上一期就出现过，只有 2 算新面孔。
        self.assertEqual(report.new_users, 1)

    def test_global_report_ranks_users_across_groups_by_qq_nickname(self):
        self.store.insert_many(
            [
                record(NOW - timedelta(days=2), group="100", user="1", name="群名片A", nickname="旧昵称"),
                record(NOW - timedelta(days=1), group="101", user="1", name="群名片B", nickname="夜岚"),
                record(NOW - timedelta(days=1), group="", user="2", name="", nickname="晨星"),  # 私聊也记昵称
                record(NOW - timedelta(days=1), group="101", user="3", name="只有名片"),
            ]
        )
        report = self.store.report(Scope(), 7, NOW)
        self.assertEqual(report.total, 4)
        # 全局用 QQ 昵称（取最新的），没有昵称时只显示 QQ 号，不拿某个群的名片顶替。
        self.assertEqual([(u.name, u.count) for u in report.top_users], [("夜岚", 2), ("晨星", 1), ("3", 1)])
        # 本群仍按群名片。
        self.assertEqual([u.name for u in self.store.report(Scope("qq", "101"), 7, NOW).top_users], ["群名片B", "只有名片"])

    def test_plugin_filter_and_private_calls(self):
        self.store.insert_many(
            [
                record(NOW - timedelta(hours=1)),
                record(NOW - timedelta(hours=1), command="/q", sub="", plugin="hyw"),
                record(NOW - timedelta(hours=1), group="", user="9", name=""),  # 私聊
            ]
        )
        self.assertEqual(self.store.report(Scope("qq", "100", plugin="hyw"), 1, NOW).total, 1)
        self.assertEqual(self.store.report(Scope("qq", "100"), 1, NOW).total, 2)
        self.assertEqual(self.store.report(Scope(), 1, NOW).total, 3)

    def test_today_compares_with_the_same_time_yesterday_and_prune_keeps_recent_rows(self):
        self.store.insert_many(
            [
                record(NOW.replace(hour=9)),
                record(NOW.replace(hour=9), ok=False),
                record(NOW - timedelta(days=1, hours=2)),
                record(NOW - timedelta(days=1) + timedelta(hours=2)),  # 昨天此刻之后，不算
                record(NOW - timedelta(days=200)),
            ]
        )
        today = self.store.today(NOW)
        self.assertEqual((today.total, today.failures, today.yesterday), (2, 1, 1))
        self.assertEqual(today.hours[9], 2)
        self.assertEqual(today.top, [("/ef 持有率", 2)])
        self.assertEqual(self.store.prune(NOW), 1)

    def test_empty_store_reports_zeroes(self):
        report = self.store.report(Scope("qq", "100"), 7, NOW)
        self.assertEqual((report.total, report.success_rate, report.commands, report.top_users), (0, 100.0, [], []))


class ResolveCommandTests(unittest.TestCase):
    def setUp(self):
        saved = dict(recorder_module._descriptions)
        self.addCleanup(lambda: (recorder_module._descriptions.clear(), recorder_module._descriptions.update(saved)))
        # 别的测试可能已经导入终末地插件并登记过，这里从空表开始。
        recorder_module._descriptions.clear()
        self.alconna = Alconna(["终末地", "ef"], Args["rest;?", MultiVar(AnyString)])

    def resolve(self, text: str):
        return resolve_command(SimpleNamespace(source=self.alconna, result=self.alconna.parse(text)))

    def test_registered_words_map_to_labels_and_other_arguments_are_never_kept(self):
        describe_command(["终末地", "ef"], display="/ef", subcommands={"持有率": "持有率", "Gacha": "抽卡"}, default_sub="资料查询")
        self.assertEqual(self.resolve("ef 持有率 全局"), ("/ef", "持有率"))
        self.assertEqual(self.resolve("终末地 gacha"), ("/ef", "抽卡"))
        self.assertEqual(self.resolve("ef 13812345678"), ("/ef", "资料查询"))
        self.assertEqual(self.resolve("ef"), ("/ef", ""))

    def test_unregistered_command_keeps_only_its_name(self):
        self.assertEqual(self.resolve("终末地 佩丽卡"), ("/终末地", ""))


class RecorderHookTests(unittest.IsolatedAsyncioTestCase):
    """真实的 AlconnaSuppiler 解析指令；处理函数像 Matcher 一样以 session.stop() 结束。"""

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recorder = UsageRecorder(UsageStore(Path(directory.name) / "usage.sqlite3"))
        self.enterContext(patch.object(recorder_module, "usage_recorder", self.recorder))
        saved = dict(recorder_module._descriptions)
        self.addCleanup(lambda: (recorder_module._descriptions.clear(), recorder_module._descriptions.update(saved)))
        describe_command(["终末地", "ef"], display="/ef", subcommands={"持有率": "持有率"}, default_sub="资料查询")
        self.scope = EventScope("test-command-usage")
        self.addCleanup(self.scope.dispose)
        self.alconna = Alconna(["终末地", "ef"], Args["rest;?", MultiVar(AnyString)])

    def command(self, handler):
        subscriber = self.scope.register(handler)
        subscriber.propagate(AlconnaSuppiler(self.alconna, LRU(10)))
        subscriber.propagate(CommandUsageRecorder("endfield"))
        return subscriber

    async def send(self, subscriber, text, **kwargs):
        try:
            return await subscriber.handle({ITEM_SESSION: session(**kwargs), ITEM_MESSAGE_CONTENT: MessageChain(text)})
        except Exception as error:  # noqa: BLE001 - 失败的处理函数照常向外抛
            return error

    async def rows(self):
        await self.recorder.flush()
        return self.recorder.store._rows("SELECT command, sub, plugin, ok, group_id, user_id FROM calls ORDER BY id", [])

    async def test_matched_commands_are_recorded_with_outcome_and_unmatched_ones_are_not(self):
        async def stops():
            raise STOP

        async def returns():
            return None

        async def fails():
            raise ValueError("boom")

        for handler in (stops, returns):
            await self.send(self.command(handler), "ef 持有率")
        failing = self.command(fails)
        self.assertIsInstance(await self.send(failing, "终末地 佩丽卡"), Exception)
        await self.send(failing, "radar 榜单")  # 指令头不匹配，不算一次调用
        await self.send(self.command(stops), "ef", private=True)
        self.assertEqual(
            await self.rows(),
            [
                ("/ef", "持有率", "endfield", 1, "100", "200"),
                ("/ef", "持有率", "endfield", 1, "100", "200"),
                ("/ef", "资料查询", "endfield", 0, "100", "200"),
                ("/ef", "", "endfield", 1, "", "200"),
            ],
        )
        self.assertEqual(self.recorder.store._rows("SELECT name FROM names", []), [("名片200",)])
        self.assertEqual(self.recorder.store._rows("SELECT name FROM users", []), [("u200",)])

    async def test_plain_listeners_and_cancelled_handlers_are_not_counted(self):
        async def listener():
            return None

        subscriber = self.scope.register(listener)
        subscriber.propagate(CommandUsageRecorder("bilibilibot"))
        await subscriber.handle({ITEM_SESSION: session()})

        async def cancelled():
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.command(cancelled).handle({ITEM_SESSION: session(), ITEM_MESSAGE_CONTENT: MessageChain("ef 持有率")})
        self.assertEqual(await self.rows(), [])

    async def test_install_covers_every_plugin_subscriber_once_including_later_ones(self):
        async def handler():
            return None

        def in_module(module):
            async def command():
                return None

            command.__module__ = module
            return command

        existing = self.scope.register(in_module("plugins.hyw.handlers"))
        with patch.object(framework_hooks, "get_all_subscribers", return_value=[existing]):
            self.addCleanup(install_command_usage_recorders())
            self.addCleanup(install_command_usage_recorders())
        later = self.scope.register(in_module("plugins.endfield"))
        bot_own = self.scope.register(handler)  # defined in tests/, not a plugin

        def hooks(subscriber):
            return [sub for sub in subscriber._propagates if sub.callable_target.__name__ == "record"]

        self.assertEqual(len(hooks(existing)), 1)
        self.assertEqual(len(hooks(later)), 1)
        self.assertEqual(hooks(bot_own), [])
        self.assertEqual(existing.get_propagator(CommandUsageRecorder).plugin, "hyw")
        self.assertEqual(later.get_propagator(CommandUsageRecorder).plugin, "endfield")

    async def test_write_failures_never_reach_the_command(self):
        self.recorder.add(CallRecord(NOW.timestamp(), "qq", "1", "1", "p", "/x", "", 1, True))
        with patch.object(self.recorder.store, "insert_many", side_effect=OSError("disk full")):
            await self.recorder.flush()
        self.assertEqual(self.recorder._pending, [])


class FrameworkSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_records_last_outcome(self):
        from otae_bot.compat.entari import _ManagedJob, _Scheduler

        scheduler = _Scheduler()
        job = _ManagedJob("plugins.demo.handlers.tick", None, set(), 1, interval_seconds=60)

        async def broken():
            raise RuntimeError("upstream 502")

        await scheduler._run_job(job.job_id, broken, job)
        self.assertEqual((job.runs, job.failures, job.last_ok), (1, 1, False))
        self.assertIn("upstream 502", job.last_error)

        async def fine():
            return None

        await scheduler._run_job(job.job_id, fine, job)
        self.assertEqual((job.runs, job.failures, job.last_ok, job.last_error), (2, 1, True, ""))

    def test_loop_lag_and_render_timing_snapshots(self):
        import time

        from otae_bot.infrastructure import loop_watchdog
        from otae_bot.infrastructure.rendering import browser

        with patch.object(loop_watchdog, "_samples", __import__("collections").deque(maxlen=10)) as samples:
            self.assertEqual(loop_watchdog.loop_lag_snapshot(), {"last": None, "max": None})
            samples.extend([(time.time() - 120, 2.0), (time.time() - 5, 0.3), (time.time(), 0.01)])
            self.assertEqual(loop_watchdog.loop_lag_snapshot(), {"last": 0.01, "max": 0.3})
        with patch.object(browser, "_render_log", __import__("collections").deque(maxlen=10)) as log:
            log.extend([(time.time() - 3600, 9.0, True), (time.time(), 1.0, True), (time.time(), 3.0, True), (time.time(), 5.0, False)])
            snapshot = browser.render_timing_snapshot(time.time() - 60)
            self.assertEqual((snapshot["count"], snapshot["failures"], snapshot["mean"]), (2, 1, 2.0))

    def test_recent_alerts_keep_first_line_masked(self):
        from loguru import logger

        from otae_bot.infrastructure import recent_alerts

        with patch.object(recent_alerts, "_alerts", __import__("collections").deque(maxlen=5)):
            sink = logger.add(recent_alerts._sink, level="WARNING", format="{message}")
            try:
                logger.info("不收")
                logger.error("绑定失败 13812345678\nTraceback ...")
            finally:
                logger.remove(sink)
            alerts = recent_alerts.recent_alerts()
        self.assertEqual([(a.level, a.message) for a in alerts], [("ERROR", "绑定失败 138****5678")])


if __name__ == "__main__":
    unittest.main()
