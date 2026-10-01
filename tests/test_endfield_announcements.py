"""Offline announcement contracts: parsing, durable outbox and account isolation."""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Import business modules without registering the real Endfield plugin.
PACKAGE = "endfield_announcements_for_test"
package = ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins/endfield")]
sys.modules.setdefault(PACKAGE, package)
models = importlib.import_module(f"{PACKAGE}.announcements.models")
source_module = importlib.import_module(f"{PACKAGE}.announcements.source")
store_module = importlib.import_module(f"{PACKAGE}.announcements.store")
service_module = importlib.import_module(f"{PACKAGE}.announcements.service")
commands = importlib.import_module(f"{PACKAGE}.announcements.commands")
runtime_module = importlib.import_module(f"{PACKAGE}.announcements.runtime")
catalog_commands = importlib.import_module(f"{PACKAGE}.catalog.commands")
NOW = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())


def article(
    cid="1",
    *,
    published=NOW - 100,
    start=NOW + 7200,
    end=NOW + 86400 * 2,
    fingerprint="v1",
    kind="activity",
):
    return models.Announcement(
        cid,
        "测试活动",
        published,
        f"https://endfield.hypergryph.com/news/{cid}",
        "公告摘要",
        (kind,),
        (models.ActivityWindow(f"{cid}:event", "测试活动", kind, start, end),),
        fingerprint,
    )


def destination(target="100", bot="bot-a", *, private=False):
    return models.Destination("qq", bot, target, target, private)


def subscribe(store, target="100", bot="bot-a", **kwargs):
    return store.subscribe(
        models.Subscription(destination(target, bot), created_at=NOW, **kwargs)
    )


@pytest.fixture
def store(tmp_path):
    return store_module.AnnouncementStore(tmp_path / "announcements.db")


def payload(body, *, title="「示例」限时活动", cid="1"):
    return {
        "code": 0,
        "data": {
            "cid": cid,
            "title": title,
            "displayTime": NOW,
            "data": body,
            "brief": "测试摘要",
        },
    }


@pytest.mark.parametrize("text", ["公告", "活动提醒", "announcements"])
def test_root_routes_to_announcement_command(text):
    result = catalog_commands.parse_command(f"{text} 订阅 活动")
    assert result.action == "announcements"
    assert result.args == ("订阅", "活动")


@pytest.mark.parametrize(
    "args, action",
    [
        ((), "help"),
        (("help",), "help"),
        (("订阅",), "subscribe"),
        (("取消订阅",), "unsubscribe"),
        (("状态",), "status"),
        (("列表",), "list"),
    ],
)
def test_command_actions(args, action):
    assert commands.parse(args).action == action
    assert not commands.parse(args).error


def test_type_filter_and_lead_validation():
    assert commands.parse(("订阅", "活动", "签到", "活动")).kinds == (
        "activity",
        "signin",
    )
    assert commands.parse(("提前", "结束", "10080")).minutes == 10080
    assert commands.parse(("提前", "维护", "关闭")).minutes == 0


@pytest.mark.parametrize(
    "args",
    [
        ("订阅", "未知"),
        ("订阅", "全部", "活动"),
        ("提前", "开始", "0"),
        ("提前", "开始", "-1"),
        ("提前", "结束", "1.5"),
        ("提前", "开始", "10081"),
        ("提前", "结束", "9" * 5000),
        ("提前",),
        ("状态", "100"),
        ("退订", "100"),
        ("help", "x"),
    ],
)
def test_invalid_commands_do_not_mutate(args):
    assert commands.parse(args).error


def test_official_shape_and_beijing_timezone():
    parsed = source_module.parse_article(
        payload(
            "<p>▼//活动时间</p><p>2030/01/02 12:00 - 2030/01/09 04:00（服务器时间）</p>"
        )
    )
    assert parsed.cid == "1"
    assert len(parsed.windows) == 1
    assert parsed.windows[0].start_at == int(
        datetime(2030, 1, 2, 4, tzinfo=timezone.utc).timestamp()
    )
    assert models.local_time(parsed.windows[0].start_at) == "2030-01-02 12:00"
    assert parsed.kinds == ("activity",)
    assert parsed == models.Announcement.loads(parsed.dumps())


def test_multiple_sections_classify_signin_banner_and_maintenance():
    body = """<h3>■ 更新维护时间</h3><p>2030/01/02 06:00 - 2030/01/02 12:00（UTC+8）</p>
    <p>▼//「星河」特许寻访</p><p>· 开放时间：2030/01/02 12:00 - 2030/01/12 12:00</p>
    <p>▼//「星光」限时签到活动</p><p>活动时间：2030/01/03 04:00 - 2030/01/10 04:00</p>"""
    parsed = source_module.parse_article(payload(body, title="版本更新说明"))
    assert [window.kind for window in parsed.windows] == [
        "maintenance",
        "banner",
        "signin",
    ]


def test_unknown_boundary_is_not_invented():
    start = source_module.parse_article(
        payload(
            "<p>▼//活动时间</p><p>2030/01/02 12:00（服务器时间） - 版本更新维护前</p>"
        )
    )
    assert start.windows[0].start_at and start.windows[0].end_at == 0
    end = source_module.parse_article(
        payload("<p>活动时间：版本更新维护后 - 2030/01/09 04:00</p>")
    )
    assert end.windows[0].start_at == 0 and end.windows[0].end_at


@pytest.mark.parametrize(
    "body",
    [
        "<p>奖励发放：2030/01/02 12:00 - 2030/01/03 12:00</p>",
        "<p>活动时间：版本更新后 - 维护前</p>",
        "<p>活动时间：2030/02/31 12:00 - 2030/03/01 12:00</p>",
        "<p>活动时间：2030/01/03 12:00 - 2030/01/02 12:00</p>",
        "<p>活动时间：2030/01/02 12:00 - 2030/01/03 12:00（UTC-5）</p>",
    ],
)
def test_unreliable_time_does_not_schedule(body):
    assert source_module.parse_article(payload(body)).windows == ()


def test_year_boundary_and_chinese_date():
    parsed = source_module.parse_article(
        payload("<p>活动时间：2030年12月31日12:00 - 2031年1月2日04:00（UTC+8）</p>")
    )
    assert parsed.windows[0].end_at > parsed.windows[0].start_at


def test_bundled_signin_without_own_time_still_matches_notice_filter():
    parsed = source_module.parse_article(
        payload(
            "<p>▼//「星光」限时签到活动</p><p>累计签到领取奖励。</p>",
            title="「星河」特许寻访说明",
        )
    )
    assert parsed.windows == ()
    assert set(parsed.kinds) == {"banner", "signin"}


def test_same_named_activity_has_stable_identity_across_articles():
    body = "<p>活动时间：2030/01/02 12:00 - 2030/01/09 04:00</p>"
    first = source_module.parse_article(
        payload("<p>▼//「示例」限时活动</p>" + body, title="版本更新说明", cid="1")
    )
    second = source_module.parse_article(
        payload(body, title="「示例」限时活动说明", cid="2")
    )
    assert first.windows[0].key == second.windows[0].key


@pytest.mark.parametrize(
    "change",
    [{"cid": "../x"}, {"data": ""}, {"displayTime": True}, {"displayTime": "wrong"}],
)
def test_source_rejects_changed_contract(change):
    data = payload("<p>公告</p>")
    data["data"].update(change)
    with pytest.raises(source_module.AnnouncementSourceError):
        source_module.parse_article(data)


def test_sticky_and_cover_change_does_not_emit_revision():
    original = payload("<p>公告内容</p>")
    changed = {
        **original,
        "data": {**original["data"], "sticky": True, "cover": "new-image"},
    }
    assert (
        source_module.parse_article(original).fingerprint
        == source_module.parse_article(changed).fingerprint
    )
    changed["data"]["data"] = "<p>调整后的公告内容</p>"
    assert (
        source_module.parse_article(original).fingerprint
        != source_module.parse_article(changed).fingerprint
    )


def test_source_uses_verified_api_and_refreshes_watched_articles():
    async def run():
        requests = []

        async def fetch(url, **kwargs):
            requests.append((url, kwargs))
            if url == source_module.API_ROOT:
                return {
                    "code": 0,
                    "data": {"list": [{"cid": "1", "displayTime": NOW}], "total": 1},
                }
            cid = url.rsplit("/", 1)[-1]
            return payload("<p>公告</p>", cid=cid)

        results = await source_module.OfficialAnnouncementSource(fetch).fetch(
            ("2",), now=NOW
        )
        assert {item.cid for item in results} == {"1", "2"}
        assert requests[0][1]["params"] == {
            "lang": "zh-cn",
            "code": "endfield_web",
            "page": 1,
            "pageSize": 20,
        }
        assert all(kwargs["ttl_seconds"] == 600 for _, kwargs in requests)

    asyncio.run(run())


def test_source_fails_atomically_on_invalid_detail():
    async def run():
        async def fetch(url, **kwargs):
            if url == source_module.API_ROOT:
                return {
                    "code": 0,
                    "data": {
                        "list": [
                            {"cid": "1", "displayTime": NOW},
                            {"cid": "2", "displayTime": NOW},
                        ]
                    },
                }
            return payload("<p>ok</p>") if url.endswith("/1") else {"code": 500}

        with pytest.raises(source_module.AnnouncementSourceError):
            await source_module.OfficialAnnouncementSource(fetch).fetch(now=NOW)

    asyncio.run(run())


def test_first_snapshot_suppresses_history_but_schedules_future(store):
    sub = subscribe(store)
    store.apply_snapshot([article(start=NOW + 1800)], NOW)
    store.plan(NOW)
    jobs = store.due(NOW)
    assert len(jobs) == 1
    assert "即将开始" in jobs[0].text
    assert "新公告" not in jobs[0].text
    assert store.subscription(sub.destination.key).initialized


def test_new_and_revised_notices_only_once_across_restart(store):
    subscribe(store)
    store.apply_snapshot([article()], NOW)
    fresh = article("2", published=NOW + 5)
    store.apply_snapshot([fresh], NOW + 10)
    jobs = store.due(NOW + 10)
    assert len(jobs) == 1 and "新公告" in jobs[0].text
    store.sent(jobs[0].key)
    restarted = store_module.AnnouncementStore(store.path)
    restarted.apply_snapshot([fresh], NOW + 20)
    assert not restarted.due(NOW + 20)
    revised = replace(fresh, fingerprint="revised", summary="修订摘要")
    restarted.apply_snapshot([revised], NOW + 30)
    assert "公告更新" in restarted.due(NOW + 30)[0].text


def test_per_subscriber_baseline_and_type_filter(store):
    subscribe(store, kinds=("maintenance",))
    store.apply_snapshot([article()], NOW)
    subscribe(store, "200")
    store.apply_snapshot([article("2", published=NOW + 1)], NOW + 10)
    assert not store.due(NOW + 10)
    store.apply_snapshot(
        [article("3", published=NOW + 11, kind="maintenance")], NOW + 20
    )
    assert len(store.due(NOW + 20)) == 2


def test_announcement_date_before_subscription_is_not_backfilled(store):
    subscribe(store)
    store.apply_snapshot([], NOW)
    store.apply_snapshot([article("late-discovery", published=NOW - 5)], NOW + 20)
    assert not store.due(NOW + 20)


def test_edit_cancels_stale_schedule_and_keeps_receipts(store):
    subscribe(store)
    original = article(start=NOW + 3700)
    store.apply_snapshot([original], NOW)
    store.plan(NOW)
    assert not store.due(NOW)
    revised = replace(
        original,
        fingerprint="v2",
        windows=(replace(original.windows[0], start_at=NOW + 10800),),
    )
    store.apply_snapshot([revised], NOW + 10)
    store.plan(NOW + 10)
    jobs = store.due(NOW + 120)
    assert all("即将开始" not in job.text for job in jobs)
    assert any("公告更新" in job.text for job in jobs)


def test_deduplicate_overview_and_article_use_newer_schedule_and_withdrawal(store):
    subscribe(store)
    old = article(start=NOW + 1800)
    new = replace(old, cid="2", published_at=NOW - 50)
    store.apply_snapshot([old, new], NOW)
    store.plan(NOW)
    jobs = store.due(NOW)
    assert len(jobs) == 1 and jobs[0].article_id == "2"
    delayed = replace(
        new,
        fingerprint="delayed",
        windows=(replace(new.windows[0], start_at=NOW + 7200),),
    )
    store.apply_snapshot([old, delayed], NOW + 10)
    store.plan(NOW + 10)
    assert all("即将开始" not in job.text for job in store.due(NOW + 10))
    withdrawn = replace(delayed, fingerprint="withdrawn", windows=())
    store.apply_snapshot([withdrawn, old], NOW + 20)
    store.plan(NOW + 20)
    assert all("即将开始" not in job.text for job in store.due(NOW + 20))
    # Re-fetching the unchanged older overview must not resurrect its schedule.
    store.apply_snapshot([old, withdrawn], NOW + 30)
    store.plan(NOW + 30)
    assert all("即将开始" not in job.text for job in store.due(NOW + 30))


def test_settings_cancel_disabled_reminders_and_preserve_sent(store):
    sub = subscribe(store)
    store.apply_snapshot([article(start=NOW + 1800)], NOW)
    store.plan(NOW)
    job = store.due(NOW)[0]
    store.sent(job.key)
    sub = store.subscription(sub.destination.key)
    store.subscribe(replace(sub, start_minutes=30))
    store.plan(NOW)
    assert not store.due(NOW)
    store.subscribe(replace(sub, start_minutes=0, end_minutes=0))
    store.plan(NOW)
    assert store.pending_count(sub.destination.key) == 0


def test_settings_preserve_queued_news_unless_type_is_unsubscribed(store):
    sub = subscribe(store)
    store.apply_snapshot([], NOW)
    store.apply_snapshot(
        [
            article("activity", published=NOW + 1),
            article("maintenance", published=NOW + 1, kind="maintenance"),
        ],
        NOW + 2,
    )
    jobs = store.due(NOW + 2)
    assert len(jobs) == 2
    store.failed(jobs[0].key, NOW + 2, 0)
    sub = store.subscription(sub.destination.key)
    store.subscribe(replace(sub, start_minutes=30))
    store.plan(NOW + 3)
    assert len(store.due(NOW + 3)) == 1
    assert {job.key for job in store.due(NOW + 62)} == {job.key for job in jobs}
    store.subscribe(replace(sub, kinds=("maintenance",)))
    store.plan(NOW + 63)
    assert [job.article_id for job in store.due(NOW + 63)] == ["maintenance"]


def test_unsubscribe_cancels_and_private_group_bot_scopes_are_distinct(store):
    first = subscribe(store)
    second = subscribe(store, bot="bot-b")
    third = store.subscribe(
        models.Subscription(destination(private=True), created_at=NOW)
    )
    store.apply_snapshot([article(start=NOW + 1800)], NOW)
    store.plan(NOW)
    assert len(store.due(NOW)) == 3
    assert store.unsubscribe(first.destination.key)
    assert {job.subscription_key for job in store.due(NOW)} == {
        second.destination.key,
        third.destination.key,
    }


def test_expired_reminders_and_old_receipts_are_cleaned(store):
    subscribe(store)
    store.apply_snapshot([article(start=NOW + 1800, end=0)], NOW)
    store.plan(NOW)
    assert store.due(NOW)
    store.plan(NOW + 1800)
    assert not store.due(NOW + 1800)
    store.plan(NOW + 100 * 86400)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0


def test_queue_fairness_and_backoff(store):
    subscribe(store)
    subscribe(store, "200")
    store.apply_snapshot([], NOW)
    store.apply_snapshot(
        [article(str(i), published=NOW + 1) for i in range(8)], NOW + 2
    )
    jobs = store.due(NOW + 2)
    assert len(jobs) == 6
    assert len({job.subscription_key for job in jobs}) == 2
    store.failed(jobs[0].key, NOW + 2, 0)
    assert jobs[0].key not in {job.key for job in store.due(NOW + 3)}
    assert jobs[0].key in {job.key for job in store.due(NOW + 62)}


def test_service_failure_does_not_block_other_recipient_and_restart_retries(store):
    async def run():
        subscribe(store)
        subscribe(store, "200")
        clock = [NOW]
        source = SimpleNamespace(
            fetch=AsyncMock(return_value=[article(start=NOW + 1800)])
        )
        sent = []

        async def sender(dest, text):
            if dest.target_id == "100":
                raise OSError("offline")
            sent.append(dest.target_id)

        service = service_module.AnnouncementService(
            store, source, sender, clock=lambda: clock[0]
        )
        await service.tick()
        assert sent == ["200"]
        assert store.due(NOW + 60)[0].attempts == 1
        clock[0] += 60
        delivered = AsyncMock()
        restart = service_module.AnnouncementService(
            store_module.AnnouncementStore(store.path),
            source,
            delivered,
            clock=lambda: clock[0],
        )
        await restart.tick()
        assert delivered.await_count == 1
        assert delivered.await_args.args[0].target_id == "100"

    asyncio.run(run())


def test_service_no_subscribers_no_poll_and_no_overlapping_refresh(store):
    async def run():
        source = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        service = service_module.AnnouncementService(
            store, source, AsyncMock(), clock=lambda: NOW
        )
        await service.tick()
        source.fetch.assert_not_awaited()
        subscribe(store)
        await asyncio.gather(service.tick(), service.tick())
        source.fetch.assert_awaited_once()

    asyncio.run(run())


def test_retention_cleanup_continues_without_subscribers(store):
    async def run():
        store.apply_snapshot([article(start=0, end=0)], NOW)
        source = SimpleNamespace(fetch=AsyncMock())
        service = service_module.AnnouncementService(
            store, source, AsyncMock(), clock=lambda: NOW + 100 * 86400
        )
        await service.tick()
        assert store.articles() == []
        source.fetch.assert_not_awaited()

    asyncio.run(run())


def test_upstream_failure_preserves_baseline_and_pauses_stale_delivery(store):
    async def run():
        sub = subscribe(store)
        source = SimpleNamespace(fetch=AsyncMock(side_effect=OSError("unavailable")))
        sender = AsyncMock()
        service = service_module.AnnouncementService(
            store, source, sender, clock=lambda: NOW
        )
        await service.tick()
        assert not store.subscription(sub.destination.key).initialized
        assert store.metadata()["last_error"] == "OSError"
        store.apply_snapshot([article(start=NOW + 3700)], NOW)
        service.clock = lambda: NOW + 1900
        await service.tick()
        sender.assert_not_awaited()
        assert store.articles()

    asyncio.run(run())


def test_deferred_delivery_does_not_consume_retry(store):
    async def run():
        subscribe(store)
        source = SimpleNamespace(
            fetch=AsyncMock(return_value=[article(start=NOW + 1800)])
        )
        sender = AsyncMock(side_effect=service_module.DeliveryDeferred)
        await service_module.AnnouncementService(
            store, source, sender, clock=lambda: NOW
        ).tick()
        assert store.due(NOW)[0].attempts == 0

    asyncio.run(run())


def fake_bot(name="bot-a"):
    from satori import Role

    return SimpleNamespace(
        platform="qq",
        self_id=name,
        guild_member_get=AsyncMock(
            return_value=SimpleNamespace(roles=[Role("member")])
        ),
        protocol=SimpleNamespace(
            send_message=AsyncMock(return_value=[object()]),
            send_private_message=AsyncMock(return_value=[object()]),
        ),
    )


def fake_event(*, private=False, admin=False):
    from satori import ChannelType, Role

    return SimpleNamespace(
        user=SimpleNamespace(id="user"),
        guild=None if private else SimpleNamespace(id="100"),
        channel=SimpleNamespace(
            id="100", type=ChannelType.DIRECT if private else ChannelType.TEXT
        ),
        member=SimpleNamespace(roles=[Role("admin" if admin else "member")]),
    )


def test_runtime_permissions_private_scope_and_invalid_input(store, monkeypatch):
    async def run():
        runtime = runtime_module.AnnouncementRuntime(
            store, SimpleNamespace(fetch=AsyncMock(return_value=[])), clock=lambda: NOW
        )
        bot = fake_bot()
        from otae_bot.config.settings import Config

        monkeypatch.setattr(Config, "SUPERUSERS", [])
        assert "仅群主" in await runtime.command(fake_event(), ("订阅",), bot)
        assert not store.subscriptions()
        assert "已订阅" in await runtime.command(
            fake_event(admin=True), ("订阅", "活动"), bot
        )
        assert "已订阅" in await runtime.command(
            fake_event(private=True), ("订阅", "签到"), bot
        )
        assert {s.destination.target_id for s in store.subscriptions()} == {
            "100",
            "user",
        }
        assert "不接受" in await runtime.command(
            fake_event(private=True), ("取消订阅", "someone-else"), bot
        )
        assert len(store.subscriptions()) == 2
        assert "设置已保存" in await runtime.command(
            fake_event(admin=True), ("提前", "结束", "30"), bot
        )
        assert "仅群主" in await runtime.command(fake_event(), ("取消订阅",), bot)
        assert "已取消" in await runtime.command(
            fake_event(private=True), ("取消订阅",), bot
        )
        assert len(store.subscriptions()) == 1

    asyncio.run(run())


def test_delivery_uses_bound_account_and_honors_feature_switch(store, monkeypatch):
    async def run():
        runtime = runtime_module.AnnouncementRuntime(store)
        first, other = fake_bot(), fake_bot("bot-b")
        runtime.remember(first)
        runtime.remember(other)
        monkeypatch.setattr(
            runtime_module.feature_store, "is_enabled", lambda scope, plugin: True
        )
        await runtime.send(destination(), "hello")
        first.protocol.send_message.assert_awaited_once()
        other.protocol.send_message.assert_not_awaited()
        monkeypatch.setattr(
            runtime_module.feature_store, "is_enabled", lambda scope, plugin: False
        )
        with pytest.raises(service_module.DeliveryDeferred):
            await runtime.send(destination(), "paused")
        assert first.protocol.send_message.await_count == 1
        await runtime.send(destination(private=True), "private")
        first.protocol.send_private_message.assert_awaited_once()
        with pytest.raises(service_module.DeliveryDeferred):
            await runtime.send(destination(bot="missing"), "offline")

    asyncio.run(run())


def test_empty_delivery_ack_is_retried(store, monkeypatch):
    async def run():
        runtime = runtime_module.AnnouncementRuntime(store)
        bot = fake_bot()
        bot.protocol.send_message.return_value = []
        runtime.remember(bot)
        monkeypatch.setattr(
            runtime_module.feature_store, "is_enabled", lambda scope, plugin: True
        )
        with pytest.raises(RuntimeError):
            await runtime.send(destination(), "unconfirmed")

    asyncio.run(run())
