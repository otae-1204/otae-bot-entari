"""Dynamic subscription baselines must use the newest feed item, not items[0]."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tests.support.bilibili import asyncio_test
from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    detect = _load_bili_new_module("detect")
    return SimpleNamespace(
        api=_load_bili_new_module("api"),
        detect=detect,
        models=sys.modules[detect.__package__ + ".models"],
    )


def _item(item_id: str, pub_ts: int, text: str) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {"desc": {"text": text}},
        },
    }


class _FakeApi:
    def __init__(self, items: list[dict]):
        self._items = items

    async def dynamic_items(self, uid: str, deadline=None):
        assert uid == "123"
        return self._items


@asyncio_test
async def test_latest_dynamic_picks_newest_pub_ts_not_first_item(bili):
    items = [
        _item("old-pinned", 100, "2020 年的置顶旧动态"),
        _item("newest", 300, "订阅前的最新动态"),
        _item("middle", 200, "中间动态"),
    ]
    card = await bili.api.space.latest_dynamic(_FakeApi(items), "123")
    assert card.item_id == "newest"
    assert card.published_at == 300


@asyncio_test
async def test_first_poll_with_newest_baseline_emits_no_history(bili):
    items = [
        _item("old-pinned", 100, "2020 年的置顶旧动态"),
        _item("newest", 300, "订阅前的最新动态"),
        _item("older", 200, "更早的动态"),
    ]
    latest = await bili.api.space.latest_dynamic(_FakeApi(items), "123")
    prev = bili.models.TargetInfo(
        "dynamic", "123", latest_id=latest.item_id, latest_ts=latest.published_at
    )
    cards = [bili.api.space.dynamic_item_to_card(item, "123") for item in items]
    _, events, marked = bili.detect.detect_dynamic(
        prev, cards, seen_ids=set(), video_subscribed=False
    )
    assert events == []
    assert marked == []


@asyncio_test
async def test_latest_dynamic_keeps_first_item_when_feed_is_ordered(bili):
    items = [
        _item("newest", 300, "最新"),
        _item("older", 200, "旧"),
    ]
    card = await bili.api.space.latest_dynamic(_FakeApi(items), "123")
    assert card.item_id == "newest"
