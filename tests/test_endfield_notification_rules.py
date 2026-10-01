from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from otae_bot.endfield_notifications.classification import classify, window_event
from otae_bot.endfield_notifications.receipts import (
    ReceiptStore,
    destination_key,
    equivalent,
)

NOW = 1790751600
START = 1790827200
BODY = "「融合！山团团！」趣味活动说明\n活动时间\n2026/10/01 12:00 - 2026/10/15 12:00\n完成任务领取奖励"
DEST = destination_key("qq", "bot", "group")


@pytest.mark.parametrize(
    "sample",
    json.loads(
        (
            Path(__file__).parent / "fixtures/endfield_notification_categories.json"
        ).read_text(encoding="utf-8")
    )["samples"],
    ids=lambda item: item["source"].rsplit("/", 1)[-1],
)
def test_verified_website_and_bilibili_category_samples(sample):
    result = classify(sample["title"], sample["text_excerpt"], sample["published_at"])
    assert result.category == sample["category"]


@pytest.mark.parametrize(
    "title,text,category",
    [
        ("「雪凇幽梦」版本更新说明", "停机维护与多项活动", "version"),
        ("「雪凇幽梦」版本预下载与更新预告", "", "version"),
        ("「雪凇幽梦」版本研发通讯", "卡池调整", "research"),
        ("封禁处理公示", "", "governance"),
        ("版本创作征集活动进行中", "参与互动抽奖", "community"),
        ("完成高校认证，领专属福利", "限时抽取奖品", "community"),
        ("「冬猎」特许寻访说明", "同步开放签到活动", "banner"),
        ("「幽寒申领」限时特卖说明", "", "banner"),
        ("「丹青渡」前瞻特别节目", "预约直播", "preview"),
        ("提弗洛斯干员演示", "转发抽奖", "operator"),
        ("提弗洛斯专属单曲MV发布", "转发抽奖", "music"),
        ("奈雪的茶联名预告", "", "collaboration"),
        ("互动抽奖", "秋澄万里，清光盈袖。转发抽奖。", "greeting"),
        ("恭喜中奖", "开奖通知", "lottery_result"),
        (
            "互动抽奖",
            "互动抽奖 #终末地#\n演出即将开始！\n「晨星于此闪耀」特许寻访 已开启！",
            "banner",
        ),
    ],
)
def test_category_is_separate_from_lottery(title, text, category):
    result = classify(title, text, NOW)
    assert result.category == category
    if "抽奖" in text:
        assert "lottery" in result.tags


def test_full_activity_and_footer_boundary():
    result = classify("活动说明", BODY + "\n————————\n「旧活动」已开启！", NOW)
    assert result.category == "activity"
    event = result.units[0].event
    assert event.name == "融合山团团" and event.start_at == START
    assert event.phase == "announcement"
    assert "旧活动" not in result.units[0].text


def test_multisection_units_and_unspecified_date_are_conservative():
    text = "▼「甲」活动\n活动时间\n2026/10/01 12:00 - 2026/10/15 12:00\n玩法甲\n▼「乙」签到活动\n活动时间\n2026/10/01 12:00 - 2026/10/16 12:00\n玩法乙"
    result = classify("活动一览", text, NOW)
    assert len(result.units) == 2
    assert {u.event.kind for u in result.units} == {"activity", "signin"}
    assert "乙" not in result.units[0].text
    assert "multiple_activities" in result.tags
    assert classify("活动已开启", "活动已开启", NOW).units[0].event is None
    ambiguous = text.replace("2026/10/01 12:00 - 2026/10/16 12:00", "版本更新后")
    uncertain = classify("活动一览", ambiguous, NOW)
    assert len(uncertain.units) == 1 and uncertain.units[0].event is None


def event():
    return classify("活动说明", BODY, NOW).units[0].event


@pytest.mark.parametrize(
    "delta,expected",
    [(0, True), (86399, True), (86400, False), (86401, False), (-86400, False)],
)
def test_24_hours_uses_source_time_in_both_orders(delta, expected):
    assert equivalent(event(), replace(event(), source_at=NOW + delta)) is expected


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "started"},
        {"phase": "ending"},
        {"kind": "signin"},
        {"name": "别的活动"},
        {"start_at": START + 86400},
        {"end_at": START + 100},
        {"source_at": 0},
    ],
)
def test_other_stages_occurrences_and_missing_times_remain(changes):
    assert not equivalent(event(), replace(event(), **changes))


def test_version_and_updates_require_stronger_identity():
    first = replace(event(), version="雪凇幽梦")
    assert not equivalent(first, replace(first, version="丹青渡"))
    first = replace(first, phase="update")
    assert not equivalent(first, replace(first, fingerprint="new content"))
    assert equivalent(first, replace(first, source_at=NOW + 1))
    assert window_event("活动已开启", "", "activity", "started", NOW, START) is None


@pytest.mark.parametrize(
    "suffix,phase",
    [("玩法介绍", "guide"), ("参与指南", "guide"), ("活动回顾", "recap")],
)
def test_activity_guides_and_recaps_do_not_hide_new_content(suffix, phase):
    first = classify(f"「融合！山团团！」活动{suffix}", BODY, NOW).units[0].event
    second = (
        classify(f"「融合！山团团！」活动{suffix}", BODY + "\n补充另一种玩法。", NOW)
        .units[0]
        .event
    )
    assert first.phase == phase
    assert not equivalent(first, event())
    assert not equivalent(first, second)


def test_receipt_only_after_ack_retry_restart_and_destinations(tmp_path):
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path)
    claim = store.reserve(DEST, "website", [event()], NOW)
    assert claim.accepted == (0,)
    assert ReceiptStore(path).reserve(DEST, "bilibili", [event()], NOW).busy
    store.release(claim)
    retry = store.reserve(DEST, "bilibili", [event()], NOW)
    assert retry.accepted == (0,)
    store.finish(retry, NOW)
    assert ReceiptStore(path).reserve(
        DEST, "website", [event()], NOW + 1
    ).duplicates == (0,)
    for scope in [
        destination_key("qq", "bot", "other"),
        destination_key("qq", "other", "group"),
        destination_key("qq", "bot", "group", True),
        destination_key("discord", "bot", "group"),
    ]:
        assert store.reserve(scope, "website", [event()], NOW).accepted == (0,)
    assert store.reserve(
        DEST, "website", [replace(event(), source_at=NOW + 86400)], NOW + 86400
    ).accepted == (0,)


def test_racing_sources_only_one_claim_and_expired_lease_recovers(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda source: store.reserve(DEST, source, [event()], NOW),
                ["website", "bilibili"],
            )
        )
    assert sum(c.busy for c in claims) == 1
    assert store.reserve(DEST, "website", [event()], NOW + 301).accepted == (0,)


def test_partial_batch_keeps_unseen_activity_and_unknown_content(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.db")
    claim = store.reserve(DEST, "bilibili", [event()], NOW)
    store.finish(claim, NOW)
    result = store.reserve(
        DEST, "website", [event(), replace(event(), name="乙"), None], NOW
    )
    assert result.accepted == (1, 2) and result.duplicates == (0,)


def test_partial_busy_batch_releases_earlier_claims(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.db")
    store.reserve(DEST, "website", [event()], NOW)
    assert store.reserve(
        DEST, "bilibili", [replace(event(), name="乙"), event()], NOW
    ).busy
    assert store.reserve(
        DEST, "website", [replace(event(), name="乙")], NOW
    ).accepted == (0,)
