"""Live-recommendation dynamics should be recognisable before detection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    return SimpleNamespace(api=_load_bili_new_module("api"))


def _live_item(room_id: int | None):
    live_info = {"title": "试试新麦"}
    if room_id is not None:
        live_info["room_id"] = room_id
    return {
        "id_str": "1269827911680000004",
        "type": "DYNAMIC_TYPE_LIVE_RCMD",
        "modules": {
            "module_author": {"pub_ts": 1794493250, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_LIVE_RCMD",
                    "live_rcmd": {"content": json.dumps({"live_play_info": live_info})},
                },
            },
        },
    }


def test_live_recommendation_maps_to_live_room(bili):
    card = bili.api.space.dynamic_item_to_card(_live_item(544803), "341368")
    assert card.live_dynamic is True
    assert card.room_id == "544803"
    assert card.url == "https://live.bilibili.com/544803"


def test_live_recommendation_without_room_id_is_still_marked(bili):
    card = bili.api.space.dynamic_item_to_card(_live_item(None), "341368")
    assert card.live_dynamic is True
    assert card.room_id == ""
    assert card.url.startswith("https://t.bilibili.com/")


def test_live_rcmd_payload_can_be_json_strings(bili):
    item = _live_item(544803)
    major = item["modules"]["module_dynamic"]["major"]
    major["live_rcmd"] = json.dumps(
        {"content": json.dumps({"live_play_info": json.dumps({"room_id": 544803})})}
    )
    card = bili.api.space.dynamic_item_to_card(item, "341368")
    assert card.room_id == "544803"
    assert card.url == "https://live.bilibili.com/544803"


def test_forwarded_live_recommendation_is_marked(bili):
    item = {
        "id_str": "2",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"pub_ts": 2, "name": "UP"},
            "module_dynamic": {"desc": {"text": "转发直播"}, "major": None},
        },
        "orig": _live_item(544803),
    }
    card = bili.api.space.dynamic_item_to_card(item, "341368")
    assert card.live_dynamic is True
    assert card.room_id == "544803"
