"""Official sample classification plus the actual durable polling/delivery path."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_core_logic import _load_bili_new_module

UID = "1265652806"
NOW = 1790840000
FIXTURES = Path(__file__).parent / "fixtures/bilibili/endfield_official"


def sample(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["item"]


@pytest.fixture
def bili():
    poller = _load_bili_new_module("poller")
    package = poller.__package__
    return SimpleNamespace(
        poller=poller,
        **{
            name: importlib.import_module(f"{package}.{path}")
            for name, path in {
                "models": "models",
                "store": "store",
                "detect": "detect",
                "filter": "dynamic_filter",
                "mapping": "api.mapping",
                "api": "api",
                "space": "api.space",
                "notifier": "notifier",
                "service": "service",
            }.items()
        },
    )


@pytest.mark.parametrize(
    "name,blocked",
    [
        ("livestream", False),
        ("activity", False),
        ("lottery_start", True),
        ("lottery_result", True),
    ],
)
def test_official_examples(bili, name, blocked):
    item = sample(name)
    card = bili.mapping.dynamic_item_to_card(item, UID)
    assert bili.filter.suppress_dynamic(card) is blocked
    assert card.item_id == item["id_str"]
    assert card.description
    if not blocked:
        assert card.cover_url and card.title != "发布了新动态"
        assert card.url.startswith("https://")


def test_reservation_prize_metadata_is_not_a_giveaway_post(bili):
    item = sample("livestream")
    reserve = item["modules"]["module_dynamic"]["additional"]["reserve"]
    assert "抽奖" in reserve["button"]["uncheck"]["toast"]
    assert "/lottery/" in reserve["desc3"]["jump_url"]
    assert not bili.filter.dynamic_lottery_reason(item)
    # A lottery in the post itself still takes precedence over the widget.
    item["modules"]["module_dynamic"]["desc"] = {
        "text": "关注并转发，抽取10位管理员送出周边"
    }
    assert bili.filter.dynamic_lottery_reason(item)


@pytest.mark.parametrize(
    "text",
    [
        "互动抽奖",
        "开奖公告",
        "恭喜中奖，已私信通知",
        "兑奖截止时间变更",
        "抽獎結果公布",
        "抽\u200b 奖",
        "关注并转发，抽取１０位管理员送出周边",
        "随机选出三名幸运观众赠送奖品",
        "转发有奖",
        "评论赢周边",
        "参与限时活动，还有机会抽取iPad Air、游戏手柄、京东卡等丰厚奖励。",
        "https://www.bilibili.com/h5/lottery/result?business_id=123",
    ],
)
def test_giveaway_text_variants(bili, text):
    assert bili.filter.text_lottery_reason(text)


@pytest.mark.parametrize(
    "text",
    [
        "完成任务领取奖励",
        "累计签到免费领取寻访凭证",
        "新卡池开启，角色获取概率UP",
        "预约有奖：直播预约福利",
        "创作活动获奖作品展示",
        "游戏内抽取一位六星干员",
    ],
)
def test_regular_game_rewards_remain_eligible(bili, text):
    assert not bili.filter.text_lottery_reason(text)


def test_structured_lottery_and_forwarded_original_are_checked(bili):
    item = sample("activity")
    item["modules"]["module_dynamic"]["desc"] = {
        "text": "",
        "rich_text_nodes": [{"type": "RICH_TEXT_NODE_TYPE_LOTTERY", "text": "参与"}],
    }
    assert bili.filter.dynamic_lottery_reason(item) == "lottery-node"
    forward = sample("lottery_result")
    forward["modules"]["module_dynamic"]["desc"] = {"text": "转发动态"}
    assert bili.filter.dynamic_lottery_reason(forward)
    forward["orig"] = sample("livestream")
    assert not bili.filter.dynamic_lottery_reason(forward)


def test_filter_is_scoped_to_official_uid_and_dynamic_kind(bili):
    card = bili.mapping.dynamic_item_to_card(sample("lottery_start"), UID)
    assert not bili.filter.suppress_dynamic(replace(card, uid="999"))
    assert not bili.filter.suppress_dynamic(replace(card, card_type="video"))
    updated, events, marked = bili.detect.detect_dynamic(
        bili.models.TargetInfo("dynamic", "999"),
        [replace(card, uid="999")],
        seen_ids=set(),
        video_subscribed=False,
    )
    assert len(events) == 1 and not marked and updated.latest_ts == card.published_at


def test_missing_flat_text_uses_rich_nodes_without_truncating_filter(bili):
    item = sample("activity")
    item["modules"]["module_dynamic"]["major"]["opus"]["summary"] = {
        "rich_text_nodes": [
            {"text": "普通说明" * 200},
            {"text": "抽取10名玩家送出周边"},
        ]
    }
    card = bili.mapping.dynamic_item_to_card(item, UID)
    assert len(card.description) > 800 and bili.filter.suppress_dynamic(card)


def test_filtered_posts_advance_cursor_and_are_marked_seen(bili):
    cards = [
        bili.mapping.dynamic_item_to_card(sample(name), UID)
        for name in ("lottery_start", "lottery_result")
    ]
    updated, events, marked = bili.detect.detect_dynamic(
        bili.models.TargetInfo("dynamic", UID),
        cards,
        seen_ids=set(),
        video_subscribed=False,
    )
    assert not events
    assert {item.item_id for item in marked} == {card.item_id for card in cards}
    assert updated.latest_ts == max(card.published_at for card in cards)
    _, events, marked = bili.detect.detect_dynamic(
        updated, cards, seen_ids=set(), video_subscribed=False
    )
    assert not events and not marked


def test_feed_requests_opus_and_avoids_incomplete_rss_for_official(bili):
    async def run():
        api = bili.api.BiliApi()
        try:
            primary = AsyncMock(return_value={"data": {"items": [sample("activity")]}})
            api._get_json_with_risk_retry = primary
            assert await api.dynamic_items(UID)
            assert primary.await_args.kwargs["params"]["features"] == "itemOpusStyle"
            api._get_json_with_risk_retry = AsyncMock(
                side_effect=bili.api.BiliAPIError("upstream unavailable")
            )
            rss = AsyncMock(return_value=[{"link": "https://t.bilibili.com/123"}])
            api.rsshub_items = rss
            with pytest.raises(bili.api.BiliAPIError, match="保留进度等待重试"):
                await api.dynamic_items(UID)
            rss.assert_awaited_once()
        finally:
            await api.aclose()

    asyncio.run(run())


def test_first_subscription_uses_newest_post_not_pinned_post(bili):
    async def run():
        api = SimpleNamespace(
            dynamic_items=AsyncMock(
                return_value=[sample("lottery_start"), sample("activity")]
            )
        )
        card = await bili.space.latest_dynamic(api, UID)
        assert card.item_id == sample("activity")["id_str"]

    asyncio.run(run())


def test_official_video_dynamics_keep_dynamic_identity(bili):
    item = sample("activity")
    item["modules"]["module_dynamic"] = {
        "desc": {"text": "新版本宣传片"},
        "major": {
            "type": "MAJOR_TYPE_ARCHIVE",
            "archive": {
                "title": "宣传片",
                "jump_url": "https://www.bilibili.com/video/BV1extE6LEKB",
            },
        },
    }
    first = bili.mapping.dynamic_item_to_card(item, UID)
    item["id_str"] = "another-dynamic"
    second = bili.mapping.dynamic_item_to_card(item, UID)
    assert first.item_id != second.item_id


def test_polling_filters_samples_and_replays_only_valid_outbox_after_restart(
    bili, tmp_path
):
    async def run():
        path = tmp_path / "bili.db"
        store = bili.store.BiliStore(path, tmp_path / "missing.db")
        await store.open()
        cards = []
        try:
            await store.upsert_target(bili.models.TargetInfo("dynamic", UID))
            for group in ("900", "901"):
                await store.add_subscription("dynamic", UID, "group", group)
            samples = [
                sample(name)
                for name in (
                    "lottery_result",
                    "activity",
                    "lottery_start",
                    "livestream",
                )
            ]
            api = SimpleNamespace(dynamic_items=AsyncMock(return_value=samples))
            await bili.poller.Poller(
                api, store, clock=lambda: NOW, intervals={"dynamic": 0}
            ).tick_dynamic()
            rows = await store.outbox_rows()
            assert len(rows) == 4
            assert {row.card().item_id for row in rows} == {
                sample(name)["id_str"] for name in ("activity", "livestream")
            }
            assert await store.has_seen(
                "dynamic", UID, sample("lottery_result")["id_str"]
            )
        finally:
            await store.close()
        store = bili.store.BiliStore(path, tmp_path / "missing.db")
        await store.open()
        try:
            await bili.poller.Poller(
                api, store, clock=lambda: NOW, intervals={"dynamic": 0}
            ).tick_dynamic()
            assert await store.outbox_count() == 4  # no duplicate fan-out

            async def render(card):
                cards.append(card)
                return b"png"

            send = AsyncMock()
            notifier = bili.notifier.Notifier(
                store, render=render, send=send, clock=lambda: NOW
            )
            while await notifier._dispatch_once():
                pass
            assert send.await_count == 4
            assert len(cards) == 2  # same event shares its rendered image across groups
            assert not await store.outbox_count()
        finally:
            await store.close()

    asyncio.run(run())


def test_lotteries_do_not_crowd_valid_older_items_out_of_feed_page(bili, tmp_path):
    async def run():
        store = bili.store.BiliStore(tmp_path / "bili.db", tmp_path / "missing.db")
        await store.open()
        try:
            await store.upsert_target(bili.models.TargetInfo("dynamic", UID))
            await store.add_subscription("dynamic", UID, "group", "900")
            items = []
            for i in range(7):
                item = sample("lottery_start")
                item["id_str"] = str(i)
                item["modules"]["module_author"]["pub_ts"] = NOW - i
                items.append(item)
            items.append(sample("activity"))
            api = SimpleNamespace(dynamic_items=AsyncMock(return_value=items))
            await bili.poller.Poller(
                api, store, clock=lambda: NOW, intervals={"dynamic": 0}
            ).tick_dynamic()
            rows = await store.outbox_rows()
            assert (
                len(rows) == 1
                and rows[0].card().item_id == sample("activity")["id_str"]
            )
            assert await store.has_seen("dynamic", UID, "6")
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("legacy", [False, True])
def test_queued_lottery_is_discarded_before_render_or_send(bili, tmp_path, legacy):
    async def run():
        store = bili.store.BiliStore(tmp_path / "bili.db", tmp_path / "missing.db")
        await store.open()
        try:
            card = bili.mapping.dynamic_item_to_card(sample("lottery_result"), UID)
            if legacy:
                card.lottery_reason = ""  # old outbox has only title and text
            event = bili.models.BiliEvent("dynamic", UID, card)
            await store.apply_poll_result(
                [],
                [],
                [event],
                NOW,
                expand=lambda _: [("group", "900")],
                event_key=lambda _: "legacy",
            )
            render, send = AsyncMock(), AsyncMock()
            notifier = bili.notifier.Notifier(
                store, render=render, send=send, clock=lambda: NOW
            )
            assert await notifier._dispatch_once() == 1
            assert not await store.outbox_count()
            render.assert_not_awaited()
            send.assert_not_awaited()
        finally:
            await store.close()

    asyncio.run(run())


def test_second_subscriber_does_not_reset_shared_cursor(bili, tmp_path):
    async def run():
        store = bili.store.BiliStore(tmp_path / "bili.db", tmp_path / "missing.db")
        await store.open()
        try:
            previous = bili.models.TargetInfo(
                "dynamic", UID, latest_id="old", latest_ts=100
            )
            await store.upsert_target(previous)
            client = SimpleNamespace(
                resolve_dynamic_target=AsyncMock(
                    return_value=replace(previous, latest_id="new", latest_ts=200)
                )
            )
            service = bili.service.BiliService(store, client)
            ok, failed = await service.follow("dynamic", [UID], "group", "900")
            assert ok and not failed and "过滤抽奖" in ok[0]
            assert (await store.get_target("dynamic", UID)).latest_ts == 100
            assert "过滤抽奖" in "\n".join(
                await service.list_subscriptions("group", "900")
            )
        finally:
            await store.close()

    asyncio.run(run())


def test_rss_fallback_rehydrates_all_samples_and_caches_official_details(bili):
    async def run():
        api = bili.api.BiliApi()
        samples = {
            sample(name)["id_str"]: sample(name)
            for name in ("livestream", "activity", "lottery_start", "lottery_result")
        }
        requests = []

        async def request(url, label, *, params):
            requests.append(url)
            if url == bili.space.DYNAMIC_URL:
                raise bili.api.BiliAPIError("HTTP 412")
            assert params["features"] == "itemOpusStyle"
            return {"data": {"item": samples[params["id"]]}}

        api._get_json_with_risk_retry = request
        api.rsshub_items = AsyncMock(
            return_value=[
                {
                    "link": f"https://www.bilibili.com/opus/{item_id}?from=rss",
                    "description": "不可信的简化摘要",
                }
                for item_id in samples
            ]
        )
        try:
            items = await api.dynamic_items(UID)
            assert len(items) == 4
            assert (
                sum(
                    bili.filter.suppress_dynamic(
                        bili.mapping.dynamic_item_to_card(item, UID)
                    )
                    for item in items
                )
                == 2
            )
            await api.dynamic_items(UID)
            assert requests.count(bili.space.DYNAMIC_DETAIL_URL) == 4
        finally:
            await api.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["foreign-author", "wrong-id", "missing-text"])
def test_rss_details_must_be_complete_and_belong_to_official_account(bili, invalid):
    async def run():
        api = bili.api.BiliApi()
        item = sample("activity")
        item_id = item["id_str"]
        if invalid == "foreign-author":
            item["modules"]["module_author"]["mid"] = 123
        elif invalid == "wrong-id":
            item["id_str"] = "123"
        else:
            item["modules"]["module_dynamic"]["major"] = {
                "type": "MAJOR_TYPE_DRAW",
                "draw": {"items": []},
            }
        api._get_json_with_risk_retry = AsyncMock(return_value={"data": {"item": item}})
        try:
            with pytest.raises(bili.api.BiliAPIError):
                await api.official_dynamic_detail(item_id)
        finally:
            await api.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("atom", [False, True])
def test_rss_discovery_keeps_multiple_items_and_limits_page(bili, atom):
    rss = importlib.import_module(f"{bili.poller.__package__}.api.rsshub")
    if atom:
        xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>官号 的 Bilibili 动态</title>'
            + "".join(
                f'<entry><title>{i}</title><link href="https://t.bilibili.com/{i}"/><content>正文</content></entry>'
                for i in range(25)
            )
            + "</feed>"
        )
    else:
        xml = (
            "<rss><channel><title>官号 的 Bilibili 动态</title>"
            + "".join(
                f"<item><title>{i}</title><link>https://t.bilibili.com/{i}</link><description>正文</description></item>"
                for i in range(25)
            )
            + "</channel></rss>"
        )
    items = rss.parse_items(xml)
    assert len(items) == 20
    assert items[0] == rss.parse_first_item(xml)
    assert items[-1]["title"] == "19" and items[-1]["author"] == "官号"


def test_rss_discovery_uses_shared_transport(bili):
    async def run():
        api = bili.api.BiliApi()
        api.rsshub_base_urls = ["https://rss.example"]
        api.session.get_text = AsyncMock(
            return_value="<rss><channel><title>官号</title><item><title>A</title><link>https://t.bilibili.com/1</link></item>"
            "<item><title>B</title><link>https://t.bilibili.com/2</link></item></channel></rss>"
        )
        try:
            items = await api.rsshub_items(f"/bilibili/user/dynamic/{UID}")
            assert [item["title"] for item in items] == ["A", "B"]
            api.session.get_text.assert_awaited_once()
        finally:
            await api.aclose()

    asyncio.run(run())


def test_partial_official_fallback_failure_does_not_advance_cursor(bili, tmp_path):
    async def run():
        store = bili.store.BiliStore(tmp_path / "bili.db", tmp_path / "missing.db")
        await store.open()
        api = bili.api.BiliApi()
        previous = bili.models.TargetInfo(
            "dynamic", UID, latest_id="old", latest_ts=100
        )
        try:
            await store.upsert_target(previous)
            await store.add_subscription("dynamic", UID, "group", "900")
            api._get_json_with_risk_retry = AsyncMock(
                side_effect=bili.api.BiliAPIError("HTTP 412")
            )
            api.rsshub_items = AsyncMock(
                return_value=[
                    {"link": "https://t.bilibili.com/1"},
                    {"link": "https://t.bilibili.com/2"},
                ]
            )

            async def detail(item_id, **kwargs):
                if item_id == "2":
                    raise bili.api.BiliAPIError("missing post")
                return sample("activity")

            api.official_dynamic_detail = detail
            await bili.poller.Poller(
                api, store, clock=lambda: NOW, intervals={"dynamic": 0}
            ).tick_dynamic()
            assert (
                await store.get_target("dynamic", UID)
            ).latest_ts == previous.latest_ts
            assert not await store.outbox_count()
            assert not await store.has_seen(
                "dynamic", UID, sample("activity")["id_str"]
            )
        finally:
            await api.aclose()
            await store.close()

    asyncio.run(run())


def test_official_video_dynamic_is_not_silenced_by_another_groups_video_subscription(
    bili, tmp_path
):
    async def run():
        store = bili.store.BiliStore(tmp_path / "bili.db", tmp_path / "missing.db")
        await store.open()
        try:
            await store.upsert_target(bili.models.TargetInfo("dynamic", UID))
            await store.add_subscription("dynamic", UID, "group", "900")
            await store.upsert_target(bili.models.TargetInfo("video", UID))
            await store.add_subscription("video", UID, "group", "901")
            item = sample("activity")
            item["modules"]["module_dynamic"] = {
                "desc": {"text": "新版本宣传片"},
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "title": "宣传片",
                        "jump_url": "https://www.bilibili.com/video/BV1extE6LEKB",
                    },
                },
            }
            api = SimpleNamespace(dynamic_items=AsyncMock(return_value=[item]))
            await bili.poller.Poller(
                api, store, clock=lambda: NOW, intervals={"dynamic": 0}
            ).tick_dynamic()
            rows = await store.outbox_rows()
            assert len(rows) == 1 and rows[0].subscriber_id == "900"
        finally:
            await store.close()

    asyncio.run(run())
