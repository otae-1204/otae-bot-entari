from __future__ import annotations

import asyncio
import json
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from PIL import Image

from otae_bot.config.settings import Config as GlobalConfig
from plugins.tibo_radar.handlers import _is_subscription_manager
from plugins.tibo_radar.client import parse_codex_reset_feed, parse_codex_reset_timeline, parse_codexradar_html
from plugins.tibo_radar.draw_scope import CardSection, render_card
from plugins.tibo_radar.draw_xfeed import render_xfeed
from plugins.tibo_radar.models import EVENT_CONFIRMED, EVENT_EXPECTED_WINDOW, EVENT_REJECTED, RELEVANCE_DIRECT, RELEVANCE_INDIRECT, RELEVANCE_NONE, ResetEvent, TiboPost
from plugins.tibo_radar.service import TiboRadarService
from plugins.tibo_radar.store import TiboStore


def _html_fixture() -> str:
    return """
    <html><body><section class="desktop-tibo-radar"
      data-tibo-post-ids="101,102"
      data-tibo-posts-updated-at="2026-08-21T03:16:14+08:00"
      data-tibo-posts-fingerprint="fixture-fingerprint">
      <ol class="reset-tibo-posts">
        <li class="reset-tibo-post" data-tibo-post-id="101" data-reset-relevance="direct">
          <div class="reset-tibo-post-head"><a href="https://x.com/thsottiaux/status/101">Tibo X</a><time datetime="2026-08-21T03:00:00+08:00">time</time></div>
          <p class="reset-tibo-post-original"><b>英文原文</b>Reset limits for everyone.</p>
          <p class="reset-tibo-post-translation"><b>中文翻译</b>所有人的额度都重置了。</p>
          <p class="reset-tibo-post-analysis"><b>模型语境解读</b>这是完成确认。</p>
          <p class="reset-tibo-post-phrases"><b>关键词句</b>reset limits</p>
        </li>
        <li class="reset-tibo-post" data-tibo-post-id="102" data-reset-relevance="none">
          <div class="reset-tibo-post-head"><a href="https://x.com/thsottiaux/status/102">Tibo X</a><time datetime="2026-08-21T02:00:00+08:00">time</time></div>
          <p class="reset-tibo-post-original"><b>英文原文</b>More context, better help.</p>
          <p class="reset-tibo-post-translation"><b>中文翻译</b>更多上下文，更好的帮助。</p>
        </li>
      </ol>
    </section></body></html>
    """


def _feed_fixture() -> dict:
    return {
        "version": 1,
        "fetched_at": "2026-08-21T01:35:03.905Z",
        "stale": False,
        "profile": {"handle": "thsottiaux"},
        "tweets": [
            {"id": "101", "url": "https://x.com/thsottiaux/status/101", "text": "Reset limits for everyone.", "localized_text": "所有人的额度都重置了。", "at": "2026-08-21T03:00:00+08:00", "tibo_lane": "reset_announcement", "explicit_reset_claim": True},
            {"id": "103", "url": "https://x.com/thsottiaux/status/103", "text": "A fancy reset button joke", "localized_text": "一个重置按钮玩笑", "at": "2026-08-20T03:00:00Z", "tibo_lane": "reset_related", "explicit_reset_claim": False},
            {"id": "104", "url": "https://x.com/thsottiaux/status/104", "text": "Zero Data Retention preview", "localized_text": "数据保留预览", "at": "2026-08-19T03:00:00Z", "tibo_lane": "reset_related", "explicit_reset_claim": False},
        ],
    }


def _timeline_fixture() -> dict:
    return {
        "updated_at": "2026-08-21T01:35:03.905Z",
        "events": [
            {"id": "201", "url": "https://x.com/thsottiaux/status/201", "type": "reset", "group": "reset", "summary": "It is done.", "localized_summary": "完成了。", "announced_at": "2026-08-21T00:00:00Z", "preview": False, "source": "archive", "confidence": "high", "reset_verification_status": "confirmed", "scope": "global", "audience": ["codex"]},
            {"id": "202", "url": "https://x.com/thsottiaux/status/202", "type": "reset", "group": "reset", "summary": "Landing within an hour", "localized_summary": "一小时内到达", "announced_at": "2026-08-20T00:00:00Z", "preview": True, "official_window": {"label": "within an hour", "start_at": "2026-08-20T00:00:00Z", "end_at": "2026-08-20T01:00:00Z"}, "source": "live", "confidence": "high", "reset_verification_status": "rejected", "scope": "global", "audience": []},
        ],
    }


class TiboRadarTests(unittest.TestCase):
    def test_codexradar_html_parser_keeps_translation_and_relevance(self):
        collection = parse_codexradar_html(_html_fixture())
        self.assertEqual(collection.source_name, "codexradar")
        self.assertEqual([post.post_id for post in collection.posts], ["101", "102"])
        self.assertEqual(collection.posts[0].relevance, RELEVANCE_DIRECT)
        self.assertEqual(collection.posts[0].translation, "所有人的额度都重置了。")
        self.assertEqual(collection.posts[0].analysis, "这是完成确认。")
        self.assertEqual(collection.state.fingerprint, "fixture-fingerprint")

    def test_feed_classifier_excludes_false_reset_wording(self):
        collection = parse_codex_reset_feed(_feed_fixture())
        relevance = {post.post_id: post.relevance for post in collection.posts}
        self.assertEqual(relevance["101"], RELEVANCE_DIRECT)
        self.assertEqual(relevance["103"], RELEVANCE_INDIRECT)
        self.assertEqual(relevance["104"], RELEVANCE_NONE)

    def test_timeline_maps_confirmed_window_and_rejected(self):
        collection = parse_codex_reset_timeline(_timeline_fixture())
        statuses = {event.event_id: event.status for event in collection.events}
        self.assertEqual(statuses["201"], EVENT_CONFIRMED)
        # A rejected preview must never become a completed reset.
        self.assertEqual(statuses["202"], EVENT_REJECTED)

    def test_store_deduplicates_and_codexradar_enrichment_wins(self):
        with TemporaryDirectory() as tmp:
            store = TiboStore(Path(tmp) / "radar.db")
            feed_post = parse_codex_reset_feed(_feed_fixture()).posts[0]
            html_post = parse_codexradar_html(_html_fixture()).posts[0]
            store.upsert_post(feed_post)
            store.upsert_post(html_post)
            saved = store.posts(limit=10)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].translation, "所有人的额度都重置了。")
            self.assertEqual(saved[0].relevance, RELEVANCE_DIRECT)
            self.assertEqual(set(saved[0].source_names), {"codex-reset", "codexradar"})
            store.close()

    def test_subscription_starts_after_existing_history_and_tracks_cursor(self):
        with TemporaryDirectory() as tmp:
            store = TiboStore(Path(tmp) / "radar.db")
            store.upsert_post(
                TiboPost(
                    post_id="old",
                    text="old",
                    url="https://x.com/thsottiaux/status/old",
                    source_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
                    source_names=("codex-reset",),
                )
            )
            already, subscription = store.subscribe("group-1", "channel-1")
            self.assertFalse(already)
            self.assertEqual(store.posts_after(subscription.last_notified_at.isoformat(), subscription.last_notified_post_id), [])

            store.upsert_post(
                TiboPost(
                    post_id="new",
                    text="new",
                    url="https://x.com/thsottiaux/status/new",
                    source_time=datetime(2026, 8, 21, tzinfo=timezone.utc),
                    source_names=("codex-reset",),
                )
            )
            pending = store.posts_after(subscription.last_notified_at.isoformat(), subscription.last_notified_post_id)
            self.assertEqual([post.post_id for post in pending], ["new"])
            cursor = pending[-1].first_seen_at.isoformat()
            store.mark_subscription_delivered("group-1", cursor, pending[-1].post_id)
            self.assertEqual(store.posts_after(cursor, "new"), [])

            enabled_again, updated = store.subscribe("group-1", "channel-2")
            self.assertTrue(enabled_again)
            self.assertEqual(updated.channel_id, "channel-2")
            self.assertEqual(updated.last_notified_post_id, "new")
            store.close()

    def test_new_subscription_skips_initial_snapshot_when_store_is_empty(self):
        with TemporaryDirectory() as tmp:
            store = TiboStore(Path(tmp) / "radar.db")
            already, subscription = store.subscribe("group-1", "channel-1")
            self.assertFalse(already)
            self.assertTrue(subscription.baseline_pending)
            store.upsert_post(
                TiboPost(
                    post_id="snapshot",
                    text="snapshot",
                    url="https://x.com/thsottiaux/status/snapshot",
                    source_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
                    source_names=("codex-reset",),
                )
            )
            store.mark_subscription_initialized("group-1")
            initialized = store.subscription("group-1")
            self.assertIsNotNone(initialized)
            self.assertFalse(initialized.baseline_pending)
            self.assertEqual(store.posts_after(initialized.last_notified_at.isoformat(), initialized.last_notified_post_id), [])
            store.close()

    def test_subscription_manager_accepts_group_admin_role_and_rejects_regular_member(self):
        group = SimpleNamespace(id="group-1")
        admin_event = SimpleNamespace(
            guild=group,
            user=SimpleNamespace(id="admin-1"),
            member=SimpleNamespace(roles=[SimpleNamespace(id="admin", name="群管理员")]),
        )
        regular_event = SimpleNamespace(
            guild=group,
            user=SimpleNamespace(id="member-1"),
            member=SimpleNamespace(roles=[]),
        )

        self.assertTrue(asyncio.run(_is_subscription_manager(object(), admin_event, "group-1", "admin-1")))
        self.assertFalse(asyncio.run(_is_subscription_manager(object(), regular_event, "group-1", "member-1")))

    @patch.object(GlobalConfig, "SUPERUSERS", ["root"])
    def test_subscription_manager_accepts_superuser(self):
        superuser_id = "root"
        event = SimpleNamespace(
            guild=SimpleNamespace(id="group-1"),
            user=SimpleNamespace(id=superuser_id),
            member=SimpleNamespace(roles=[]),
        )
        self.assertTrue(asyncio.run(_is_subscription_manager(object(), event, "group-1", superuser_id)))

    def test_service_status_does_not_promote_old_rejected_window(self):
        with TemporaryDirectory() as tmp:
            store = TiboStore(Path(tmp) / "radar.db")
            now = datetime(2026, 8, 21, tzinfo=timezone.utc)
            store.upsert_event(ResetEvent(event_id="old", summary="window", url="https://x.com/thsottiaux/status/old", announced_at=datetime(2026, 8, 18, tzinfo=timezone.utc), window_end=datetime(2026, 8, 18, 1, tzinfo=timezone.utc), preview=True, status=EVENT_REJECTED, source_names=("codex-reset",)))
            service = TiboRadarService(store, object())
            status = service.status(now=now)
            self.assertFalse(status.active)
            self.assertIn("暂无", status.label)
            store.close()

    def test_draw_card_is_valid_png(self):
        async def render():
            return await render_card("Tibo 测试", "本地缓存", [CardSection("状态", ["中文内容", "source https://x.com/thsottiaux/status/1"])])

        output = asyncio.run(render())
        with Image.open(BytesIO(output)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.width, 1080)
            self.assertGreaterEqual(image.height, 430)

    def test_xfeed_card_is_valid_png(self):
        posts = [
            TiboPost(
                post_id="101",
                text="Reset limits for everyone.",
                url="https://x.com/thsottiaux/status/101",
                source_time=datetime(2026, 8, 21, tzinfo=timezone.utc),
                translation="所有人的额度都重置了。",
                analysis="这是完成确认。",
                relevance=RELEVANCE_DIRECT,
                phrases="reset limits",
            ),
            TiboPost(
                post_id="102",
                text="Working on the weekly reset pipeline.",
                url="https://x.com/thsottiaux/status/102",
                source_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
                relevance=RELEVANCE_INDIRECT,
            ),
        ]

        async def render():
            return await render_xfeed(
                posts,
                lambda value: "直接相关" if value == RELEVANCE_DIRECT else "间接相关",
                title="Tibo 最新 X 动态",
                subtitle="测试",
                page="1/1",
            )

        output = asyncio.run(render())
        with Image.open(BytesIO(output)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.width, 1080)
            self.assertGreaterEqual(image.height, 430)



TIBO_SCENARIOS = r"""
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from PIL import Image as PILImage
from satori import Member, Role, User
from plugins.tibo_radar.models import EVENT_CONFIRMED, ResetEvent, TiboPost

tibo = plugin('plugins.tibo_radar.handlers')
buffer = io.BytesIO()
PILImage.new('RGB', (2, 2)).save(buffer, format='PNG')
PNG = buffer.getvalue()
ADMIN = Member(user=User('admin-1'), roles=[Role('admin', '群管理员')])
REGULAR = Member(user=User('member-1'), roles=[])
AT = datetime(2026, 8, 21, tzinfo=timezone.utc)


def posts(n):
    return [TiboPost(str(i), 'text ' + str(i), 'https://x.com/t/' + str(i), source_time=AT) for i in range(n)]


def events(n):
    return [ResetEvent(str(i), 'done', 'https://x.com/e/' + str(i), announced_at=AT, status=EVENT_CONFIRMED) for i in range(n)]


def is_card(item, link):
    return item.startswith('<img') and link in item


async def main():
    service = tibo.service
    renders = []
    card = AsyncMock(side_effect=lambda *a, **k: renders.append(('card', a[0], k.get('page', ''))) or PNG)
    feed = AsyncMock(side_effect=lambda *a, **k: renders.append(('feed', k.get('page', ''))) or PNG)
    with patch.object(tibo, 'render_card', card), patch.object(tibo, 'render_xfeed', feed):
        # 两个指令名；无参数与 overview / 概览 走总览卡片
        for text in ('/tibo', '/雷达', '/tibo overview', '/tibo 概览'):
            sent = await say(text)
            assert len(sent) == 1 and is_card(sent[0], '来源链接：') and 'codexradar.com' in sent[0], (text, sent)
            assert renders[-1][:2] == ('card', 'Tibo 雷达总览'), renders
        # 帮助与未知子命令
        for text in ('/tibo 帮助', '/tibo help'):
            sent = await say(text)
            assert len(sent) == 1 and sent[0].startswith('Tibo 雷达使用指南'), sent
        sent = await say('/tibo 乱写')
        assert len(sent) == 1 and sent[0].startswith('未知子命令，请参考以下说明：') and 'Tibo 雷达使用指南' in sent[0], sent
        # 动态：参数校验、空列表、默认一页、超过 6 条分页
        assert await say('/tibo 动态 x') == ['参数错误：数量需为整数；格式示例：/tibo 动态 6']
        assert await say('/tibo 最新 21') == ['参数错误：数量需介于 1～20 之间；格式示例：/tibo 动态 6']
        with patch.object(service, 'latest_posts', lambda limit=6: []):
            sent = await say('/tibo 动态')
            assert len(sent) == 1 and is_card(sent[0], '来源链接：') and renders[-1][:2] == ('card', 'Tibo 最新动态'), (sent, renders)
        asked = []
        with patch.object(service, 'latest_posts', lambda limit=6: asked.append(limit) or posts(limit)):
            sent = await say('/tibo 动态')
            assert asked[-1] == 6 and len(sent) == 1 and is_card(sent[0], 'https://x.com/t/5'), sent
            assert renders[-1] == ('feed', ''), renders
            sent = await say('/tibo 动态 8')
            assert asked[-1] == 8 and len(sent) == 2, sent
            assert is_card(sent[0], 'https://x.com/t/5') and is_card(sent[1], 'https://x.com/t/7'), sent
            assert renders[-2:] == [('feed', '1/2'), ('feed', '2/2')], renders
        # 状态、最近（有/无）、历史（参数、空、分页）
        for text in ('/tibo 状态', '/tibo reset', '/tibo 重置'):
            sent = await say(text)
            assert len(sent) == 1 and is_card(sent[0], '来源链接：') and renders[-1][:2] == ('card', 'Tibo 重置状态'), sent
        with patch.object(service, 'latest_confirmed', lambda: None):
            sent = await say('/tibo 最近')
            assert len(sent) == 1 and renders[-1][:2] == ('card', '最近一次重置'), renders
        with patch.object(service, 'latest_confirmed', lambda: events(1)[0]):
            sent = await say('/tibo last')
            assert len(sent) == 1 and is_card(sent[0], 'https://x.com/e/0') and renders[-1][:2] == ('card', '最近一次已确认重置'), sent
        assert await say('/tibo 历史 0') == ['参数错误：数量需介于 1～20 之间；格式示例：/tibo 历史 6']
        with patch.object(service, 'history', lambda limit=6: []):
            sent = await say('/tibo history')
            assert len(sent) == 1 and renders[-1] == ('card', 'Tibo 重置历史', ''), renders
        with patch.object(service, 'history', lambda limit=6: events(limit)):
            sent = await say('/tibo 历史 4')
            assert len(sent) == 2 and is_card(sent[0], 'https://x.com/e/2') and is_card(sent[1], 'https://x.com/e/3'), sent
            assert renders[-2:] == [('card', 'Tibo 重置历史', '1/2'), ('card', 'Tibo 重置历史', '2/2')], renders

    # 订阅：私聊受限、普通成员无权、管理员开启/重复开启、状态、取消、重复取消
    limit = '使用场景限制：Tibo 新帖订阅仅限在群聊中配置。'
    for text in ('/tibo 订阅', '/tibo 取消订阅', '/tibo 订阅状态'):
        assert await say(text, private=True, user='admin-1') == [limit], text
    for text in ('/tibo subscribe', '/tibo unsub'):
        assert await say(text, user='member-1', member=REGULAR) == ['权限不足：仅限群主、群管理员或 SUPERUSER 操作 Tibo 新帖订阅。'], text
    assert await say('/tibo 订阅状态', group='300') == ['当前群尚未启用 Tibo 新帖订阅；可使用 /tibo 订阅 进行开启。']
    assert await say('/tibo 订阅', group='300', user='admin-1', member=ADMIN) == ['已成功开启本群 Tibo 新帖订阅，后续新帖将携图文卡片及源帖链接实时推送。']
    assert tibo.store.subscription('300').channel_id == '300'
    assert await say('/tibo sub', group='300', user='admin-1', member=ADMIN) == ['本群此前已启用 Tibo 新帖订阅，新动态将依采集周期自动推送。']
    for text in ('/tibo 订阅 状态', '/tibo substatus', '/tibo subscriptions'):
        sent = await say(text, group='300')
        assert len(sent) == 1 and sent[0].startswith('当前群订阅状态：已开启 Tibo 新帖推送\n最新推送记录：') and '最近投递方式：暂无' in sent[0], sent
    assert await say('/tibo 退订', group='300', user='admin-1', member=ADMIN) == ['已成功关闭本群的 Tibo 新帖推送服务。']
    assert await say('/tibo unsubscribe', group='300', user='admin-1', member=ADMIN) == ['本群当前尚未启用 Tibo 新帖订阅，无需退订。']
    print('ALL-OK')

run(main())
"""


class TiboDispatchTests(unittest.TestCase):
    def test_tibo_command_through_the_real_event_path(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(TIBO_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
