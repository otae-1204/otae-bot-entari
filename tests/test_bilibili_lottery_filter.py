"""社交抽奖动态的过滤规则与接入点。

样本取自 B 站官方详情接口（2026-10-01 采集），只保留判定与绘图所需字段，
开奖正文已省略中奖用户。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from otae_bot.config import settings
from plugins.bilibilibot.api.mapping import dynamic_item_to_card
from plugins.bilibilibot.detect import detect_dynamic
from plugins.bilibilibot.lottery_filter import (
    ALL_ACCOUNTS,
    DEFAULT_FILTER_UIDS,
    applies_to,
    lottery_reason,
    parse_filter_uids,
    suppress_dynamic,
    text_lottery_reason,
)
from plugins.bilibilibot.models import KIND_DYNAMIC, BiliCard, TargetInfo

FIXTURES = Path(__file__).parent / "fixtures/bilibili/endfield_official"
OFFICIAL_UID = "1265652806"
OTHER_UID = "12345"


def _uids(value: str):
    return settings.override({"plugins": {"plugins.bilibilibot": {"lottery_filter_uids": value}}})


def sample(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["item"]


def card_of(name: str, uid: str = OFFICIAL_UID) -> BiliCard:
    return dynamic_item_to_card(sample(name), uid)


class OfficialSampleTests(unittest.TestCase):
    """四条已核实的官方样本：两条保留，两条过滤。"""

    def test_livestream_preview_is_kept(self):
        card = card_of("livestream")
        self.assertEqual(card.lottery_reason, "")
        self.assertFalse(suppress_dynamic(card))
        self.assertIn("前瞻特别节目", card.title)

    def test_activity_notice_is_kept(self):
        card = card_of("activity")
        self.assertEqual(card.lottery_reason, "")
        self.assertFalse(suppress_dynamic(card))
        self.assertIn("趣味活动说明", card.title)

    def test_lottery_start_is_suppressed(self):
        card = card_of("lottery_start")
        self.assertTrue(card.lottery_reason)
        self.assertTrue(suppress_dynamic(card))

    def test_lottery_result_forward_is_suppressed(self):
        card = card_of("lottery_result")
        self.assertTrue(card.lottery_reason)
        self.assertTrue(suppress_dynamic(card))

    def test_opus_summary_supplies_the_visible_text(self):
        """官号正文在 Opus 摘要里，desc 为 null；过滤依赖这段文字。"""
        card = card_of("activity")
        self.assertTrue(card.description)
        self.assertNotEqual(card.title, "发布了新动态")


class ReservationWidgetTests(unittest.TestCase):
    """预约组件自带的按钮提示不是抽奖公告。"""

    def test_reserve_toast_alone_does_not_suppress(self):
        item = {
            "id_str": "1",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"mid": OFFICIAL_UID, "pub_ts": 1},
                "module_dynamic": {
                    "desc": None,
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {"summary": {"text": "版本前瞻特别节目即将播出"}},
                    },
                    "additional": {
                        "type": "ADDITIONAL_TYPE_RESERVE",
                        "reserve": {"button": {"uncheck": {"toast": "预约成功，已参与抽奖"}}},
                    },
                },
            },
        }
        self.assertEqual(lottery_reason(item), "")

    def test_body_lottery_still_suppresses_next_to_a_reserve_widget(self):
        item = {
            "id_str": "1",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"mid": OFFICIAL_UID, "pub_ts": 1},
                "module_dynamic": {
                    "desc": None,
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {"summary": {"text": "转发本条动态参与抽奖"}},
                    },
                    "additional": {"type": "ADDITIONAL_TYPE_RESERVE", "reserve": {}},
                },
            },
        }
        self.assertEqual(lottery_reason(item), "lottery-text")


class StructuredEvidenceTests(unittest.TestCase):
    def test_lottery_node_type_is_evidence_even_without_the_word(self):
        item = {
            "modules": {
                "module_dynamic": {
                    "desc": None,
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "summary": {
                                "rich_text_nodes": [
                                    {"type": "RICH_TEXT_NODE_TYPE_LOTTERY", "text": "点我参与"}
                                ]
                            }
                        },
                    },
                }
            }
        }
        self.assertEqual(lottery_reason(item), "lottery-node")

    def test_forwarded_original_is_checked(self):
        original = {
            "modules": {
                "module_dynamic": {"desc": {"text": "抽取3位管理员送出周边"}, "major": None}
            },
        }
        item = {
            "modules": {"module_dynamic": {"desc": {"text": "查看详情"}, "major": None}},
            "orig": original,
        }
        self.assertEqual(lottery_reason(item), "giveaway-text")

    def test_author_name_is_not_content(self):
        item = {
            "modules": {
                "module_author": {"name": "抽奖小助手"},
                "module_dynamic": {"desc": {"text": "版本更新公告"}, "major": None},
            }
        }
        self.assertEqual(lottery_reason(item), "")

    def test_self_referencing_forward_terminates(self):
        item = {"modules": {"module_dynamic": {"desc": {"text": "普通动态"}, "major": None}}}
        item["orig"] = item
        self.assertEqual(lottery_reason(item), "")


class TextRuleTests(unittest.TestCase):
    def test_social_giveaways_are_detected(self):
        for text in (
            "互动抽奖",
            "恭喜中奖，已私信通知",
            "开奖结果公示",
            "开獎公告",
            "兑奖说明",
            "抽取3位管理员送出周边",
            "关注+转发抽送月卡",
            "详情见 https://t.bilibili.com/lottery/123",
        ):
            with self.subTest(text=text):
                self.assertTrue(text_lottery_reason(text))

    def test_regular_game_content_is_not_detected(self):
        for text in (
            "完成活动任务即可获得奖励",
            "新版本「雪凇幽梦」现已开启",
            "签到即得合成玉",
            "寻访概率提升，卡池开放",
            "限时福利免费领取",
            "周边将于近期上架商城",
        ):
            with self.subTest(text=text):
                self.assertEqual(text_lottery_reason(text), "")

    def test_fullwidth_and_zero_width_writing_cannot_bypass(self):
        self.assertTrue(text_lottery_reason("抽\u200b奖"))
        self.assertTrue(text_lottery_reason("抽獎"))


class AccountListTests(unittest.TestCase):
    """默认对所有账号过滤，只有显式配置才收窄或关闭。"""

    def test_unset_variable_filters_every_account(self):
        self.assertEqual(parse_filter_uids(None), frozenset({ALL_ACCOUNTS}))
        self.assertEqual(parse_filter_uids(None), frozenset(DEFAULT_FILTER_UIDS))
        self.assertTrue(applies_to(OFFICIAL_UID))
        self.assertTrue(applies_to(OTHER_UID))

    def test_empty_value_disables_the_filter(self):
        self.assertEqual(parse_filter_uids(""), frozenset())
        self.assertEqual(parse_filter_uids("  ,  "), frozenset())

    def test_several_uids_are_supported(self):
        self.assertEqual(parse_filter_uids("111, 222 ,333"), frozenset({"111", "222", "333"}))

    def test_star_enables_every_account(self):
        for raw in ("*", "all", "ALL", "111,*"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_filter_uids(raw), frozenset({ALL_ACCOUNTS}))

    def test_setting_drives_the_scope(self):
        with _uids("999"):
            self.assertTrue(applies_to("999"))
            self.assertFalse(applies_to(OFFICIAL_UID))
        with _uids(""):
            self.assertFalse(applies_to(OFFICIAL_UID))
        with _uids(ALL_ACCOUNTS):
            self.assertTrue(applies_to(OTHER_UID))
        with settings.override({}):
            self.assertTrue(applies_to(OTHER_UID))

    def test_explicit_list_overrides_the_setting(self):
        with _uids("999"):
            self.assertTrue(applies_to(OFFICIAL_UID, frozenset({OFFICIAL_UID})))
            self.assertFalse(applies_to("999", frozenset({OFFICIAL_UID})))

    def test_narrowing_to_one_uid_leaves_other_accounts_alone(self):
        """显式收窄到官号时，其他账号的抽奖动态照常推送。"""
        card = card_of("lottery_start", uid=OTHER_UID)
        narrowed = frozenset({OFFICIAL_UID})
        self.assertFalse(suppress_dynamic(card, uids=narrowed))
        self.assertTrue(suppress_dynamic(card_of("lottery_start"), uids=narrowed))


class ScopeTests(unittest.TestCase):
    def test_only_dynamic_cards_are_filtered(self):
        """直播与视频订阅不受过滤影响。"""
        for card_type in ("video", "live_on", "live_off"):
            with self.subTest(card_type=card_type):
                card = BiliCard(card_type, "互动抽奖", uid=OFFICIAL_UID, description="恭喜中奖")
                self.assertFalse(suppress_dynamic(card))

    def test_dynamic_kind_is_filtered_for_any_account(self):
        lottery = card_of("lottery_start")
        self.assertTrue(suppress_dynamic(lottery, uid=OFFICIAL_UID))
        self.assertTrue(suppress_dynamic(lottery, uid=OTHER_UID))

    def test_other_accounts_are_filtered_by_default_too(self):
        """默认全部账号：其他账号的抽奖动态同样跳过。"""
        card = card_of("lottery_start", uid=OTHER_UID)
        self.assertTrue(card.lottery_reason)
        self.assertTrue(suppress_dynamic(card))

    def test_other_accounts_keep_their_lottery_dynamics_when_narrowed(self):
        card = card_of("lottery_start", uid=OTHER_UID)
        self.assertFalse(suppress_dynamic(card, uids=frozenset({OFFICIAL_UID})))

    def test_star_extends_the_filter_to_every_account(self):
        card = card_of("lottery_start", uid=OTHER_UID)
        self.assertTrue(suppress_dynamic(card, uids=frozenset({ALL_ACCOUNTS})))
        self.assertTrue(suppress_dynamic(card, uids=frozenset({OTHER_UID})))

    def test_disabling_the_filter_keeps_every_dynamic(self):
        card = card_of("lottery_start")
        self.assertFalse(suppress_dynamic(card, uids=frozenset()))


class DetectionIntegrationTests(unittest.TestCase):
    def _prev(self) -> TargetInfo:
        return TargetInfo(KIND_DYNAMIC, OFFICIAL_UID, name="明日方舟终末地")

    def test_suppressed_cards_are_marked_seen_and_never_pushed(self):
        cards = [card_of("lottery_start"), card_of("activity")]
        updated, events, marked = detect_dynamic(
            self._prev(), cards, seen_ids=set(), video_subscribed=False
        )
        self.assertEqual([event.card.title for event in events], [card_of("activity").title])
        marked_ids = {item.item_id for item in marked}
        self.assertIn(card_of("lottery_start").item_id, marked_ids)
        # 被过滤的动态仍推进游标，重启后不会重新投递。
        self.assertEqual(updated.latest_id, card_of("activity").item_id)

    def test_all_lottery_page_advances_the_cursor_without_pushing(self):
        cards = [card_of("lottery_start"), card_of("lottery_result")]
        updated, events, marked = detect_dynamic(
            self._prev(), cards, seen_ids=set(), video_subscribed=False
        )
        self.assertEqual(events, [])
        self.assertEqual(len(marked), 2)
        self.assertTrue(updated.latest_id)

    def test_other_accounts_page_is_filtered_by_default(self):
        cards = [card_of("lottery_start", uid=OTHER_UID)]
        _, events, marked = detect_dynamic(
            TargetInfo(KIND_DYNAMIC, OTHER_UID),
            cards,
            seen_ids=set(),
            video_subscribed=False,
        )
        self.assertEqual(events, [])
        self.assertEqual(len(marked), 1)


if __name__ == "__main__":
    unittest.main()
