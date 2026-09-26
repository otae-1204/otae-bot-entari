"""Detection layer tests: pure functions, no network, no database, no clock."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tests.test_core_logic import _load_bili_new_module

START = 1790059295  # 2026-09-22 14:41:35, UTC+8.
NOW = START + 2 * 3600 + 18 * 60


@pytest.fixture(scope="module")
def bili():
    detect = _load_bili_new_module("detect")
    return SimpleNamespace(
        detect=detect,
        models=sys.modules[detect.__package__ + ".models"],
    )


def live_obs(bili, **kwargs):
    kwargs.setdefault("uid", "123")
    kwargs.setdefault("room_id", "456")
    return bili.models.LiveObservation(**kwargs)


# --- detect_live ------------------------------------------------------------


@pytest.mark.parametrize(
    "prev_live,obs_live,expected",
    [
        (False, True, "live_on"),
        (True, False, "live_off"),
        (False, False, None),
        (True, True, None),
    ],
)
def test_detect_live_only_reports_state_changes(bili, prev_live, obs_live, expected):
    prev = bili.models.TargetInfo("live", "123", is_live=prev_live)
    updated, events = bili.detect.detect_live(prev, live_obs(bili, is_live=obs_live), NOW)
    assert updated.is_live is obs_live
    if expected is None:
        assert events == []
    else:
        assert [event.card.card_type for event in events] == [expected]
        assert events[0].kind == "live"
        assert events[0].uid == "123"
        assert events[0].seen is None


def test_detect_live_without_observation_never_moves_state(bili):
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - 60
    )
    updated, events = bili.detect.detect_live(prev, None, NOW)
    # A failed poll must not advance last_seen or produce an end notification.
    assert updated is prev
    assert events == []
    assert (prev.live_started_at, prev.live_last_seen_at) == (START, NOW - 60)


@pytest.mark.parametrize(
    "start,last_seen,expected",
    [
        (START, NOW - 60, 8280),
        (START, NOW - 180, 8280),
        (START, NOW - 181, None),
        (START, NOW - 86400, None),
        (START, 0, None),
        (0, NOW - 60, None),
        (START, NOW + 1, None),
        (NOW + 1, NOW - 60, None),
    ],
)
def test_end_estimate_requires_recent_valid_observation(bili, start, last_seen, expected):
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=start, live_last_seen_at=last_seen
    )
    updated, events = bili.detect.detect_live(prev, live_obs(bili, is_live=False), NOW)
    assert [event.card.live_duration_seconds for event in events] == [expected]
    assert (updated.live_started_at, updated.live_last_seen_at) == (0, 0)
    assert updated.is_live is False


@pytest.mark.parametrize(
    "gap,new_start,expected",
    [
        (60, 0, START),
        (181, 0, 0),
        (3600, NOW - 30, NOW - 30),
        (60, NOW - 30, NOW - 30),
        (60, NOW + 1, START),
    ],
)
def test_ongoing_stream_keeps_new_session_start_and_limits_fallback(
    bili, gap, new_start, expected
):
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - gap
    )
    updated, events = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, started_at=new_start), NOW
    )
    assert updated.live_started_at == expected
    assert updated.live_last_seen_at == NOW
    assert events == []


def test_detect_live_syncs_name_and_avatar_every_round(bili):
    prev = bili.models.TargetInfo("live", "123", name="旧名", avatar_url="old.png")
    updated, _ = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, uname="新名", face="new.png"), NOW
    )
    assert (updated.name, updated.avatar_url) == ("新名", "new.png")
    # Empty values keep the previous ones instead of blanking them.
    updated, _ = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, uname="", face=""), NOW
    )
    assert (updated.name, updated.avatar_url) == ("旧名", "old.png")


def test_detect_live_keeps_previous_room_and_cover_when_missing(bili):
    prev = bili.models.TargetInfo("live", "123", room_id="456", last_cover="cover.png")
    updated, _ = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, room_id="", cover=""), NOW
    )
    assert (updated.room_id, updated.last_cover) == ("456", "cover.png")


def test_live_on_card_carries_the_link_and_badge(bili):
    prev = bili.models.TargetInfo("live", "123", name="主播")
    _, events = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, title="标题", room_id="456"), NOW
    )
    card = events[0].card
    assert card.card_type == "live_on"
    assert (card.badge, card.room_id, card.url) == ("LIVE", "456", "https://live.bilibili.com/456")
    assert card.live_duration_seconds is None


# --- detect_video -----------------------------------------------------------


def video_card(bili, item_id="BV1", published_at=NOW):
    return bili.models.BiliCard("video", "标题", uid="123", item_id=item_id, published_at=published_at)


def test_detect_video_pushes_only_new_unseen_items(bili):
    prev = bili.models.TargetInfo("video", "123", latest_id="BV0", latest_ts=NOW - 100)
    updated, events = bili.detect.detect_video(prev, video_card(bili), already_seen=False)
    assert [event.card.item_id for event in events] == ["BV1"]
    assert events[0].seen == bili.models.SeenItem("video", "123", "BV1", NOW)
    assert updated.latest_id == "BV1"


def test_detect_video_skips_the_current_and_already_seen_items(bili):
    prev = bili.models.TargetInfo("video", "123", latest_id="BV1")
    _, events = bili.detect.detect_video(prev, video_card(bili, item_id="BV1"), already_seen=False)
    assert events == []
    _, events = bili.detect.detect_video(prev, video_card(bili, item_id="BV2"), already_seen=True)
    assert events == []


def test_detect_video_refines_uid_name_and_empty_avatar(bili):
    prev = bili.models.TargetInfo("video", "123", name="123")
    card = video_card(bili)
    card.author, card.avatar_url = "作者", "face.png"
    updated, _ = bili.detect.detect_video(prev, card, already_seen=False)
    assert (updated.name, updated.avatar_url) == ("作者", "face.png")


def test_detect_video_does_not_override_custom_name(bili):
    prev = bili.models.TargetInfo("video", "123", name="我起的名字", avatar_url="mine.png")
    card = video_card(bili)
    card.author, card.avatar_url = "作者", "face.png"
    updated, _ = bili.detect.detect_video(prev, card, already_seen=False)
    assert (updated.name, updated.avatar_url) == ("我起的名字", "mine.png")


def test_detect_video_keeps_previous_fields_when_card_is_sparse(bili):
    prev = bili.models.TargetInfo(
        "video", "123", latest_id="BV0", latest_ts=1, last_title="旧", last_cover="c", last_desc="d"
    )
    updated, _ = bili.detect.detect_video(
        prev, bili.models.BiliCard("video", ""), already_seen=False
    )
    assert (updated.latest_id, updated.latest_ts) == ("BV0", 1)
    assert (updated.last_title, updated.last_cover, updated.last_desc) == ("旧", "c", "d")


# --- detect_dynamic ---------------------------------------------------------


def dynamic_card(bili, item_id, published_at, url="https://t.bilibili.com/1"):
    return bili.models.BiliCard(
        "dynamic", f"动态{item_id}", uid="123", item_id=item_id, published_at=published_at, url=url
    )


def test_detect_dynamic_processes_oldest_first_and_skips_seen(bili):
    prev = bili.models.TargetInfo("dynamic", "123", latest_ts=NOW - 300)
    cards = [
        dynamic_card(bili, "c", NOW),
        dynamic_card(bili, "a", NOW - 100),
        dynamic_card(bili, "b", NOW - 50),
    ]
    updated, events, marked = bili.detect.detect_dynamic(
        prev, cards, seen_ids={"a"}, video_subscribed=False
    )
    assert [event.card.item_id for event in events] == ["b", "c"]
    assert marked == []
    assert updated.latest_id == "c"
    assert updated.latest_ts == NOW


def test_detect_dynamic_skips_items_not_newer_than_latest_ts(bili):
    prev = bili.models.TargetInfo("dynamic", "123", latest_ts=NOW - 50)
    cards = [dynamic_card(bili, "old", NOW - 50), dynamic_card(bili, "new", NOW - 49)]
    _, events, _ = bili.detect.detect_dynamic(prev, cards, seen_ids=set(), video_subscribed=False)
    assert [event.card.item_id for event in events] == ["new"]


BVID = "BV1extE6LEKB"


def test_detect_dynamic_marks_video_dynamics_when_video_subscribed(bili):
    prev = bili.models.TargetInfo("dynamic", "123")
    cards = [
        dynamic_card(bili, BVID, NOW, url=f"https://www.bilibili.com/video/{BVID}"),
        dynamic_card(bili, "plain", NOW + 1),
    ]
    updated, events, marked = bili.detect.detect_dynamic(
        prev, cards, seen_ids=set(), video_subscribed=True
    )
    # The video dynamic is only marked as read; the plain one is pushed.
    assert [event.card.item_id for event in events] == ["plain"]
    assert [item.item_id for item in marked] == [BVID]
    assert updated.latest_id == "plain"


def test_detect_dynamic_pushes_video_dynamics_without_video_subscription(bili):
    prev = bili.models.TargetInfo("dynamic", "123")
    cards = [dynamic_card(bili, BVID, NOW, url=f"https://www.bilibili.com/video/{BVID}")]
    _, events, marked = bili.detect.detect_dynamic(
        prev, cards, seen_ids=set(), video_subscribed=False
    )
    assert [event.card.item_id for event in events] == [BVID]
    assert marked == []


def test_detect_dynamic_without_cards_keeps_previous_state(bili):
    prev = bili.models.TargetInfo("dynamic", "123", latest_id="x", latest_ts=5)
    updated, events, marked = bili.detect.detect_dynamic(
        prev, [], seen_ids=set(), video_subscribed=False
    )
    assert updated is prev
    assert (events, marked) == ([], [])


def test_detect_dynamic_refines_name_but_never_overrides_custom(bili):
    prev = bili.models.TargetInfo("dynamic", "123", name="123")
    card = dynamic_card(bili, "a", NOW)
    card.author = "作者"
    updated, _, _ = bili.detect.detect_dynamic(prev, [card], seen_ids=set(), video_subscribed=False)
    assert updated.name == "作者"

    custom = bili.models.TargetInfo("dynamic", "123", name="自定义")
    card2 = dynamic_card(bili, "b", NOW + 1)
    card2.author = "作者"
    updated2, _, _ = bili.detect.detect_dynamic(
        custom, [card2], seen_ids=set(), video_subscribed=False
    )
    assert updated2.name == "自定义"


def test_detect_does_not_import_io_modules(bili):
    # The whole point of this layer: no transport, storage, delivery or clock.
    assert "api" not in dir(bili.detect)
    source = bili.detect.__loader__.get_source(bili.detect.__name__)
    for forbidden in ("import time", "from .api", "from .store", "from .notifier"):
        assert forbidden not in source


def test_seen_items_of_collects_event_marks(bili):
    prev = bili.models.TargetInfo("video", "123", latest_id="BV0")
    _, events = bili.detect.detect_video(prev, video_card(bili), already_seen=False)
    assert bili.detect.seen_items_of(events) == [bili.models.SeenItem("video", "123", "BV1", NOW)]
    assert bili.detect.seen_items_of([]) == []
