"""Dynamic/Opus link parsing and preview card creation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.bilibili import asyncio_test
from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    return SimpleNamespace(api=_load_bili_new_module("api"))


class _FakeSession:
    def __init__(self, location: str):
        self.location = location

    async def head_location(self, url: str) -> str:
        return self.location


@asyncio_test
async def test_parses_t_bilibili_dynamic_link(bili):
    api = SimpleNamespace(session=_FakeSession(""))
    parsed = await bili.api.links.parse_link(api, "https://t.bilibili.com/1266116596080836657")
    assert parsed.kind == bili.api.links.KIND_DYNAMIC
    assert parsed.value == "1266116596080836657"


@asyncio_test
async def test_parses_opus_link_with_query(bili):
    api = SimpleNamespace(session=_FakeSession(""))
    parsed = await bili.api.links.parse_link(
        api, "https://www.bilibili.com/opus/1266116596080836657?share_from=dynamic"
    )
    assert parsed.kind == bili.api.links.KIND_DYNAMIC
    assert parsed.value == "1266116596080836657"


@asyncio_test
async def test_short_link_expands_to_dynamic(bili):
    api = SimpleNamespace(session=_FakeSession("https://www.bilibili.com/opus/1266116596080836657"))
    parsed = await bili.api.links.parse_link(api, "https://b23.tv/abc")
    assert parsed.kind == bili.api.links.KIND_DYNAMIC
    assert parsed.value == "1266116596080836657"


@asyncio_test
async def test_live_and_video_links_still_parse(bili):
    api = SimpleNamespace(session=_FakeSession(""))
    live = await bili.api.links.parse_link(api, "https://live.bilibili.com/25731103")
    video = await bili.api.links.parse_link(api, "https://www.bilibili.com/video/BV1xx411c7mD")
    assert (live.kind, live.value) == (bili.api.links.KIND_LIVE, "25731103")
    assert (video.kind, video.value) == (bili.api.links.KIND_VIDEO, "BV1xx411c7mD")


@asyncio_test
async def test_card_for_dynamic_link_uses_detail_item(bili):
    detail_item = {
        "id_str": "1266116596080836657",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"mid": "341368", "pub_ts": 1793629142, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {"summary": {"text": "下一首应该是一个完全的手癖放出"}},
                },
            },
        },
    }

    async def dynamic_detail(item_id):
        assert item_id == "1266116596080836657"
        return detail_item

    api = SimpleNamespace(dynamic_detail=dynamic_detail)
    parsed = bili.api.links.ParsedLink(
        bili.api.links.KIND_DYNAMIC,
        "1266116596080836657",
        "https://t.bilibili.com/1266116596080836657",
    )
    card = await bili.api.BiliApi.card_for_link(api, parsed)
    assert card.card_type == bili.api.links.KIND_DYNAMIC
    assert card.item_id == "1266116596080836657"
    assert card.description == "下一首应该是一个完全的手癖放出"
    assert card.uid == "341368"
