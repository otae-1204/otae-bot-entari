"""Feed shells must be filled from the detail endpoint before detection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.bilibili import asyncio_test
from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    return SimpleNamespace(api=_load_bili_new_module("api"))


def _empty_draw(item_id: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {"type": "MAJOR_TYPE_DRAW", "draw": {"items": []}},
            },
        },
    }


def _opus_detail(item_id: str, text: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {"summary": {"text": text}, "pics": []},
                },
            },
        },
    }


def _text_item(item_id: str, text: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {"desc": {"text": text}},
        },
    }


class _FakeApi:
    def __init__(self, space, feed: list[dict], details: dict[str, dict]):
        self.space = space
        self.feed = feed
        self.details = details
        self.detail_calls: list[str] = []

    async def _within(self, label, deadline, factory):
        return await factory()

    async def dynamic_detail(self, item_id: str, *, deadline=None):
        return await self.space.dynamic_detail(self, item_id, deadline=deadline)

    async def _get_json_with_risk_retry(self, url, label, params=None):
        if url == self.space.DYNAMIC_URL:
            return {"data": {"items": self.feed}}
        if url == self.space.DYNAMIC_DETAIL_URL:
            item_id = str((params or {}).get("id") or "")
            self.detail_calls.append(item_id)
            return {"data": {"item": self.details.get(item_id, {})}}
        raise AssertionError(url)


def _fake(bili, feed, details):
    return _FakeApi(bili.api.space, feed, details)


def test_content_check_distinguishes_empty_draw_from_opus_text(bili):
    assert not bili.api.space._dynamic_has_content(_empty_draw("1"))
    assert bili.api.space._dynamic_has_content(_opus_detail("1", "正文"))
    assert bili.api.space._dynamic_has_content(_text_item("1", "正文"))


def test_content_check_accepts_ugc_when_mapping_renders_it(bili):
    item = {
        "id_str": "1",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": 1},
            "module_dynamic": {
                "desc": None,
                "major": None,
                "additional": {
                    "type": "ADDITIONAL_TYPE_UGC",
                    "ugc": {"title": "视频标题", "cover": "https://example.com/c.jpg"},
                },
            },
        },
    }
    assert bili.api.space._dynamic_has_content(item)


def test_ugc_additional_does_not_mask_an_empty_draw(bili):
    item = _empty_draw("1")
    item["modules"]["module_dynamic"]["additional"] = {
        "type": "ADDITIONAL_TYPE_UGC",
        "ugc": {"title": "视频标题", "cover": "https://example.com/c.jpg"},
    }
    assert not bili.api.space._dynamic_has_content(item)


@asyncio_test
async def test_empty_feed_item_is_replaced_with_detail_item(bili):
    feed = [_empty_draw("1266116596080836657")]
    details = {"1266116596080836657": _opus_detail("1266116596080836657", "下一首应该是一个完全的手癖放出")}
    api = _fake(bili, feed, details)

    items = await bili.api.space.dynamic_items(api, "341368")

    assert api.detail_calls == ["1266116596080836657"]
    assert bili.api.space._dynamic_has_content(items[0])


@asyncio_test
async def test_non_empty_feed_item_does_not_trigger_a_detail_request(bili):
    api = _fake(bili, [_text_item("1", "正文")], {})

    items = await bili.api.space.dynamic_items(api, "341368")

    assert api.detail_calls == []
    assert items[0]["id_str"] == "1"


@asyncio_test
async def test_detail_requests_are_capped(bili):
    feed = [_empty_draw(str(index)) for index in range(6)]
    details = {str(index): _opus_detail(str(index), f"正文 {index}") for index in range(6)}
    api = _fake(bili, feed, details)

    items = await bili.api.space.dynamic_items(api, "341368")

    assert len(api.detail_calls) == bili.api.space.DYNAMIC_DETAIL_LIMIT
    assert bili.api.space._dynamic_has_content(items[0])
    assert not bili.api.space._dynamic_has_content(items[5])


@asyncio_test
async def test_invisible_detail_keeps_the_feed_item(bili):
    feed = [_empty_draw("1")]
    api = _fake(bili, feed, {})

    items = await bili.api.space.dynamic_items(api, "341368")

    assert items[0] is feed[0]
    assert not bili.api.space._dynamic_has_content(items[0])


@asyncio_test
async def test_official_feed_never_calls_generic_detail(bili):
    feed = [_text_item("1", "官号正文")]
    api = _fake(bili, feed, {})

    items = await bili.api.space.dynamic_items(api, "1265652806")

    assert api.detail_calls == []
    assert items[0]["id_str"] == "1"
