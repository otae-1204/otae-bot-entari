"""Live-recommendation dynamics: top-level cards are recognisable, forwards are not."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    api = _load_bili_new_module("api")
    detect = _load_bili_new_module("detect")
    return SimpleNamespace(
        api=api,
        detect=detect,
        models=sys.modules[detect.__package__ + ".models"],
    )


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


def _forwarded_item(room_id: int | None):
    return {
        "id_str": "2",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"pub_ts": 2, "name": "UP"},
            "module_dynamic": {"desc": {"text": "转发直播"}, "major": None},
        },
        "orig": _live_item(room_id),
    }


def _plain_item(text: str):
    return {
        "id_str": "3",
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"pub_ts": 3, "name": "UP"},
            "module_dynamic": {"desc": {"text": text}, "major": None},
        },
    }


def _live_card_item(item_type: str, major_type: str | None, room_id: int = 544803):
    return {
        "id_str": "4",
        "type": item_type,
        "modules": {
            "module_author": {"pub_ts": 4, "name": "UP"},
            "module_dynamic": {
                "desc": {"text": "直播中"},
                "major": {
                    "type": major_type,
                    "live_rcmd": {
                        "content": json.dumps({"live_play_info": {"room_id": room_id}})
                    },
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
    assert card.live_dynamic is True
    assert card.room_id == "544803"
    assert card.url == "https://live.bilibili.com/544803"


def test_forwarded_live_recommendation_stays_a_normal_dynamic(bili):
    card = bili.api.space.dynamic_item_to_card(_forwarded_item(544803), "341368")
    # 只有顶层直播推荐条目才算「直播推荐动态」，转发里套的直播卡不改写 URL。
    assert card.live_dynamic is False
    assert card.room_id == ""
    assert card.url == "https://t.bilibili.com/2"
    assert card.item_id == "2"


def test_plain_dynamic_linking_to_a_live_room_stays_normal(bili):
    text = "今晚八点直播 https://live.bilibili.com/544803 不见不散"
    card = bili.api.space.dynamic_item_to_card(_plain_item(text), "341368")
    # 正文里贴直播间链接不代表这是直播推荐卡：不置标记、不改写 URL。
    assert card.live_dynamic is False
    assert card.room_id == ""
    assert card.url == "https://t.bilibili.com/3"


@pytest.mark.parametrize(
    "item_type,major_type",
    [
        ("DYNAMIC_TYPE_LIVE", "MAJOR_TYPE_LIVE"),
        ("DYNAMIC_TYPE_LIVE", None),
        ("DYNAMIC_TYPE_WORD", "MAJOR_TYPE_LIVE"),
    ],
)
def test_live_card_with_rcmd_payload_is_not_a_live_recommendation(
    bili, item_type, major_type
):
    card = bili.api.space.dynamic_item_to_card(
        _live_card_item(item_type, major_type), "341368"
    )
    # 真正的直播卡即使带 live_rcmd 载荷，也只按显式推荐类型识别，不能误标。
    assert card.live_dynamic is False
    assert card.room_id == ""
    assert card.url == "https://t.bilibili.com/4"


def test_forwarded_live_recommendation_is_pushed_when_live_subscribed(bili):
    card = bili.api.space.dynamic_item_to_card(_forwarded_item(544803), "341368")
    prev = bili.models.TargetInfo("dynamic", "341368")
    _, events, marked = bili.detect.detect_dynamic(
        prev,
        [card],
        seen_ids=set(),
        video_subscribed=False,
        live_subscribed=True,
        live_room_id="544803",
    )
    # 转发卡按普通动态渲染推送，不会被订阅直播的去重吃掉。
    assert [event.card.item_id for event in events] == ["2"]
    assert events[0].card.card_type == "dynamic"
    assert events[0].card.url == "https://t.bilibili.com/2"
    assert marked == []
