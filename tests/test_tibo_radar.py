from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from PIL import Image

from configs.config import Config as GlobalConfig
from plugins.tibo_radar import _is_subscription_manager
from plugins.tibo_radar.client import parse_codex_reset_feed, parse_codex_reset_timeline, parse_codexradar_html
from plugins.tibo_radar.draw import CardSection, render_card
from plugins.tibo_radar.draw_x import CardSection as XCardSection, render_card as render_x_card
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

    def test_subscription_manager_accepts_superuser(self):
        configured = GlobalConfig.SUPERUSERS
        if not configured:
            self.skipTest("no SUPERUSERS configured")
        superuser_id = str(configured[0] if isinstance(configured, (list, tuple)) else configured)
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

    def test_x_design_card_is_valid_png(self):
        async def render():
            return await render_x_card("Tibo 测试", "本地缓存", [XCardSection("当前雷达状态", ["官方重置预告窗口进行中", "等待完成核验。"])])

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


if __name__ == "__main__":
    unittest.main()
