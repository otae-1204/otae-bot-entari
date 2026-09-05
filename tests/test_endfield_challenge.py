from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import plugins.endfield.rendering.cards as endfield_draw
from plugins.endfield.catalog import commands
from plugins.endfield.account.challenge import draw as challenge
from plugins.endfield.account.challenge.draw import (
    ChallengeIdentity,
    ChallengeRecord,
    ChallengeResolutionError,
    _best_cleared_html,
    _css,
    _feature_items,
    _monument_detail_html,
    _monument_difficulty_label,
    _monument_history_block,
    _monument_stage_card,
    _war_stage_card,
    _war_week_block,
    parse_monument,
    parse_war_echoes,
    resolve_monument_detail,
    resolve_war_detail,
)
from plugins.endfield.account.client import EndfieldOfficialClient
from plugins.endfield.account.challenge.i18n import build_challenge_locale
from otae_bot.infrastructure.http.client import HttpResource


def _monument_fixture() -> dict:
    return {
        "indieHardGroups": [
            {
                "id": "active",
                "name": "当前主题",
                "pic": "https://assets.invalid/current.png",
                "isInActivity": True,
                "activityStartTs": "1700000000",
                "activityEndTs": "1900000000",
                "dungeonGroups": [
                    {
                        "normalDungeon": {
                            "id": "normal-1", "name": "清波访客", "isPass": True,
                            "bestRecord": {"chars": [{"charId": "char-a", "avatarUrl": "https://assets.invalid/a.png"}], "ts": "1800000000", "passTs": "65"},
                            "recommendLevel": 60,
                        },
                        "hardDungeon": {"id": "hard-1", "name": "清波访客·苦难", "isPass": False, "recommendLevel": 90},
                    }
                ],
            },
            {"id": "old", "name": "旧主题", "isInActivity": False, "activityEndTs": "1600000000", "dungeonGroups": []},
        ]
    }


def _war_fixture() -> dict:
    return {
        "seasons": [
            {
                "id": "s1", "name": "当前赛季", "headerImage": "https://assets.invalid/war.png",
                "startTs": "1700000000", "endTs": "1900000000", "stars": 6, "allPlusTasks": False,
                "weeks": [
                    {
                        "id": "w1", "name": "轮换Ⅰ", "startTs": "1700000000", "endTs": "1900000000",
                        "groups": [], "dungeonGroups": [
                            {"name": "战争简史", "star": 3, "plusTask": True,
                             "normalDungeon": {"id": "n", "name": "战争简史", "isPass": True, "firstPassTs": "1800000000"},
                             "hardDungeon": {"id": "h", "name": "战争简史", "isPass": True},
                             "cruelDungeon": {"id": "c", "name": "战争简史", "isPass": False}},
                        ],
                    }
                ],
            }
        ],
        "achieves": [{"name": "战争简史", "star": 3, "firstPassTs": "1800000000"}],
    }


class EndfieldChallengeTests(unittest.TestCase):
    def test_prepare_assets_keeps_mixed_download_failures_without_crashing(self):
        async def run():
            success = "https://assets.invalid/success.png"
            failed = "https://assets.invalid/failed.png"
            fetched = (
                {
                    success: HttpResource(b"png", "image/png", 200, success),
                    failed: None,
                },
                {failed: "http 404"},
            )
            with patch.object(
                endfield_draw,
                "fetch_many_resilient",
                AsyncMock(return_value=fetched),
            ):
                prepared = await endfield_draw._prepare_assets(
                    [success, failed],
                    inline=False,
                )
            self.assertTrue(prepared.urls[success].startswith("https://endfield.local/assets/"))
            self.assertEqual(prepared.urls[failed], "")
            self.assertEqual(prepared.failures, {failed: "http 404"})

        asyncio.run(run())

    def test_localizes_english_challenge_payloads_from_akedata_text_ids(self):
        chinese = {
            "1": "谵妄赛季",
            "2": "谵妄轮换Ⅰ",
            "3": "战争简史",
            "4": "关卡中文描述",
            "5": "关卡中文机制",
            "6": "中文追加目标",
            "7": "火雾源石虫",
            "8": "主题中文名",
            "9": "中文蚀刻章",
        }
        english = {
            "1": "Season of Delirating",
            "2": "Cycle of Delirating I",
            "3": "Brief History of War",
            "4": "English stage description",
            "5": "English stage mechanics",
            "6": "English additional target",
            "7": "Firemist Originium Slug",
            "8": "English Monument Theme",
            "9": "English Medal",
        }
        dungeon_id = "indie_battletower001"
        locale = build_challenge_locale(
            chinese,
            english,
            {
                dungeon_id: {
                    "dungeonId": dungeon_id,
                    "dungeonName": {"id": "3", "text": ""},
                    "dungeonDesc": {"id": "4", "text": ""},
                    "featureDesc": {"id": "5", "text": ""},
                    "extraGoalDesc": {"id": "6", "text": ""},
                }
            },
            version="test",
        )
        hashed_id = hashlib.md5(dungeon_id.encode()).hexdigest()
        raw_war = {
            "seasons": [
                {
                    "id": "1",
                    "name": "Season of Delirating",
                    "weeks": [
                        {
                            "id": "1",
                            "name": "Cycle of Delirating I",
                            "dungeonGroups": [
                                {
                                    "name": "Brief History of War",
                                    "normalDungeon": {
                                        "id": hashed_id,
                                        "name": "Brief History of War",
                                        "desc": "English stage description",
                                        "feature": "English stage mechanics",
                                        "additionalChallengeTarget": "English additional target",
                                        "enemies": [{"name": "Firemist Originium Slug"}],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
            "achieves": [{"name": "Brief History of War", "star": 3}],
        }
        war = parse_war_echoes(raw_war, locale)
        season = war.seasons[0]
        dungeon = season.weeks[0].groups[0].normal
        self.assertEqual(season.name, "谵妄赛季")
        self.assertEqual(season.weeks[0].name, "谵妄轮换Ⅰ")
        self.assertEqual(season.weeks[0].groups[0].name, "战争简史")
        self.assertEqual((dungeon.name, dungeon.desc, dungeon.feature), ("战争简史", "关卡中文描述", "关卡中文机制"))
        self.assertEqual(dungeon.additional_target, "中文追加目标")
        self.assertEqual(dungeon.enemies[0].name, "火雾源石虫")
        self.assertEqual(war.achievements[0].name, "战争简史")

        raw_monument = {
            "indieHardGroups": [
                {
                    "name": "English Monument Theme",
                    "activityName": "English Monument Theme",
                    "dungeonGroups": [{"normalDungeon": {"id": hashed_id, "name": "Brief History of War"}}],
                    "achieve": {"achievementData": {"name": "English Medal"}},
                }
            ]
        }
        monument = parse_monument(raw_monument, locale).groups[0]
        self.assertEqual((monument.name, monument.activity_name, monument.medal_name), ("主题中文名", "主题中文名", "中文蚀刻章"))
        self.assertEqual(monument.stages[0][0].name, "战争简史")

    def test_localizes_monument_group_and_medal_from_stable_hashed_ids(self):
        group_id = "indie_group_h06"
        achievement_id = "achv_bat_hard_6"
        locale = build_challenge_locale(
            {"theme": "山中见犼", "medal": "“山中见犼”"},
            {},
            dungeon_series_table={
                group_id: {
                    "id": group_id,
                    "gameCategory": "dungeon_highdifficulty",
                    "name": {"id": "theme", "text": ""},
                }
            },
            achievement_table={
                achievement_id: {"name": {"id": "medal", "text": ""}}
            },
            version="test",
        )
        raw = {
            "indieHardGroups": [
                {
                    "id": hashlib.md5(group_id.encode()).hexdigest(),
                    "name": "Howlers of the Crag (provider copy changed)",
                    "activityName": "Monumental Etching: Beastly Howl",
                    "dungeonGroups": [],
                    "achieve": {
                        "achievementData": {
                            "id": hashlib.md5(achievement_id.encode()).hexdigest(),
                            "name": "Howlers of the Crag Medal (provider copy changed)",
                        }
                    },
                }
            ]
        }

        monument = parse_monument(raw, locale).groups[0]
        self.assertEqual(monument.name, "山中见犼")
        self.assertEqual(monument.medal_name, "“山中见犼”")
        self.assertEqual(monument.activity_name, "Monumental Etching: Beastly Howl")

    def test_localizes_ambiguous_enemy_and_multiline_ability_from_hashed_id(self):
        template_id = "eny_0055_hscrane"
        variant_id = "eny_0055_hscrane_hard"
        locale = build_challenge_locale(
            {
                "enemy_name": "劫云客",
                "enemy_desc": "装备了奇异兵器的非法武装人员。",
                "ability_1": "可以喷出干扰视线的烟雾。",
                "ability_2": "自身的战斗能力相对薄弱，可以考虑优先针对。",
                "ability_3": "绿色的烟雾可以为其他敌人提供治疗。",
            },
            {},
            enemy_table={
                variant_id: {
                    "enemyId": variant_id,
                    "templateId": template_id,
                }
            },
            enemy_template_display_table={
                template_id: {
                    "templateId": template_id,
                    "name": {"id": "enemy_name", "text": ""},
                    "description": {"id": "enemy_desc", "text": ""},
                    "abilityDescIds": ["a1", "a2", "a3"],
                }
            },
            enemy_ability_desc_table={
                "a1": {"description": {"id": "ability_1", "text": ""}},
                "a2": {"description": {"id": "ability_2", "text": ""}},
                "a3": {"description": {"id": "ability_3", "text": ""}},
            },
            version="test",
        )
        raw = {
            "indieHardGroups": [
                {
                    "name": "Theme",
                    "dungeonGroups": [
                        {
                            "normalDungeon": {
                                "id": "stage",
                                "name": "Stage",
                                "enemies": [
                                    {
                                        "id": hashlib.md5(variant_id.encode()).hexdigest(),
                                        "name": "Cloud Stalker",
                                        "desc": "Provider English description",
                                        "ability": (
                                            "Spews disrupting smoke.\n"
                                            "The enemy is a relatively poor combatant.\n"
                                            "Spews green smoke that heals other enemies."
                                        ),
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }

        enemy = parse_monument(raw, locale).groups[0].stages[0][0].enemies[0]
        self.assertEqual(enemy.name, "劫云客")
        self.assertEqual(enemy.desc, "装备了奇异兵器的非法武装人员。")
        self.assertEqual(
            enemy.ability,
            "可以喷出干扰视线的烟雾。 自身的战斗能力相对薄弱，可以考虑优先针对。 "
            "绿色的烟雾可以为其他敌人提供治疗。",
        )

    def test_command_parser_supports_overview_history_and_detail(self):
        overview = commands.parse_command("影拓 账号 2")
        self.assertEqual((overview.action, overview.challenge_kind, overview.challenge_view, overview.account_selector), ("challenge", "monument", "overview", "2"))

        history = commands.parse_command("回响 历史 第3页 账号 小号")
        self.assertEqual((history.challenge_kind, history.challenge_view, history.page, history.account_selector), ("war_echo", "history", 3, "小号"))
        self.assertFalse(history.all_history)

        detail = commands.parse_command("影拓 当前主题 清波访客 苦难")
        self.assertEqual(detail.challenge_view, "detail")
        self.assertEqual(detail.challenge_difficulty, "hard")
        self.assertEqual(detail.challenge_terms, ("当前主题", "清波访客"))
        self.assertTrue(commands.parse_command("影拓 历史 第0页").error)
        self.assertTrue(commands.parse_command("回响 历史 额外参数").error)

    def test_command_parser_ignores_serialized_at_segments_for_challenges(self):
        overview = commands.parse_command(
            commands.strip_message_mentions('影拓 <at id="1231"/>')
        )
        self.assertEqual(
            (overview.challenge_kind, overview.challenge_view, overview.challenge_terms),
            ("monument", "overview", ()),
        )

        history = commands.parse_command(
            commands.strip_message_mentions('回响 历史 第2页 <at id="1231"/>')
        )
        self.assertEqual(
            (history.challenge_kind, history.challenge_view, history.page, history.error),
            ("war_echo", "history", 2, ""),
        )

    def test_challenge_mentions_ignore_bot_and_preserve_target_order(self):
        from arclet.entari import At, MessageChain, Text
        import plugins.endfield.handlers as endfield_plugin

        event = SimpleNamespace(
            content=MessageChain(
                [Text("/ef 影拓 "), At("10000"), At("1231"), At("1231")]
            )
        )
        self.assertEqual(
            endfield_plugin._challenge_mention_targets(
                event,
                SimpleNamespace(self_id="10000"),
            ),
            ("1231",),
        )

    def test_challenge_mention_without_binding_returns_text(self):
        from arclet.entari import At, MessageChain, Text
        import plugins.endfield.handlers as endfield_plugin

        async def run():
            matcher = SimpleNamespace(finish=AsyncMock())
            event = SimpleNamespace(
                content=MessageChain([Text("/ef 回响 "), At("1231")]),
                guild=SimpleNamespace(id="group-1"),
            )
            with patch.object(endfield_plugin.account_store, "list_roles", return_value=[]) as list_roles:
                await endfield_plugin._handle_challenge(
                    matcher,
                    event,
                    "sender",
                    commands.parse_command("回响"),
                    SimpleNamespace(),
                    bot=SimpleNamespace(self_id="10000"),
                )
            list_roles.assert_called_once_with("1231")
            matcher.finish.assert_awaited_once_with("被 @ 的用户尚未绑定终末地账号。")

        asyncio.run(run())

    def test_challenge_mention_selects_target_primary_account(self):
        from arclet.entari import At, MessageChain, Text
        import plugins.endfield.handlers as endfield_plugin

        async def run():
            matcher = SimpleNamespace(finish=AsyncMock())
            event = SimpleNamespace(
                content=MessageChain([Text("/ef 影拓 "), At("1231")]),
                guild=SimpleNamespace(id="group-1"),
            )
            roles = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
            primary = SimpleNamespace(id=2)
            with patch.object(endfield_plugin.account_store, "list_roles", return_value=roles):
                with patch.object(
                    endfield_plugin.account_store,
                    "resolve_role",
                    return_value=primary,
                ) as resolve_role:
                    with patch.object(
                        endfield_plugin.account_store,
                        "decrypt_token",
                        side_effect=RuntimeError("stop-after-selection"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "stop-after-selection"):
                            await endfield_plugin._handle_challenge(
                                matcher,
                                event,
                                "sender",
                                commands.parse_command("影拓 账号 1"),
                                SimpleNamespace(),
                                bot=SimpleNamespace(self_id="10000"),
                            )
            resolve_role.assert_called_once_with("1231", "")

        asyncio.run(run())

    def test_monument_normalizes_active_group_and_records(self):
        payload = parse_monument(_monument_fixture())
        self.assertEqual(payload.current().name, "当前主题")
        self.assertTrue(payload.has_records)
        self.assertEqual(payload.current().stages[0][0].record.pass_time, 65)
        group, dungeon = resolve_monument_detail(payload, ("当前主题", "清波访客"), "hard")
        self.assertEqual((group.name, dungeon.difficulty), ("当前主题", "hard"))
        group, dungeon = resolve_monument_detail(payload, ("当前主题",), "normal")
        self.assertEqual((group.name, dungeon.name), ("当前主题", "清波访客"))
        with self.assertRaises(ChallengeResolutionError):
            resolve_monument_detail(payload, ("不存在",), "hard")

    def test_war_normalizes_season_week_and_difficulty(self):
        payload = parse_war_echoes(_war_fixture())
        self.assertTrue(payload.has_records)
        self.assertEqual(payload.current().current_week().name, "轮换Ⅰ")
        season, week, group, dungeon = resolve_war_detail(payload, ("当前赛季", "轮换Ⅰ", "战争简史"), "cruel")
        self.assertEqual((season.name, week.name, group.name, dungeon.difficulty), ("当前赛季", "轮换Ⅰ", "战争简史", "cruel"))

    def test_history_cards_show_best_tier_and_pad_empty_slots(self):
        monument = parse_monument(_monument_fixture()).current()
        history = _monument_history_block(monument, {})
        # 列头与总览同一套中文难度名，压在用时轨上
        self.assertIn('<span class="head-tier">普通</span>', history)
        self.assertIn('<span class="head-tier head-hard">苦难</span>', history)
        self.assertIn("history-team", history)
        self.assertIn("2027-01-15", history)
        # 历史页同样不画通关勾：24 个方块曾经是全页最响的东西，却零信息
        self.assertNotIn("✓", history)
        self.assertNotIn("status-chip", history)
        # 没打过的档由「未通关」回答，不铺空话
        self.assertIn("未通关", history)
        war = parse_war_echoes(_war_fixture()).current().current_week()
        history = _war_week_block(war, {})
        self.assertIn("战争简史", history)
        # 只取最高通关档：残酷未通关 → 困难，且不再罗列低档或冗余注释
        self.assertIn("困难", history)
        for absent in ("普通", "残酷", "追加目标", "仅显示最高通关难度"):
            self.assertNotIn(absent, history)
        # 队伍缺员时补满 4 个空框
        self.assertIn("team-avatar is-empty", history)

    def test_monument_overview_lists_both_difficulties(self):
        monument_group = parse_monument(_monument_fixture()).current()
        monument_card = _monument_stage_card(monument_group.stages[0], {}, 1)
        # 难度由列头表达，通关与否不画勾：有用时本身就是通关的证据
        self.assertEqual(monument_card.count('<div class="ladder-cell'), 2)
        self.assertNotIn("difficulty-label", monument_card)
        self.assertNotIn("✓", monument_card)
        self.assertNotIn("tier-mark", monument_card)
        self.assertIn("01:05", monument_card)
        # 整档没有任何记录时只写一句结论，不铺三条空话
        self.assertIn("未通关", monument_card)
        self.assertEqual(monument_card.count('<div class="ladder-cell is-blank">'), 1)
        self.assertNotIn("用时未返回", monument_card)
        overview = challenge._monument_overview_html(
            ChallengeIdentity("脱敏账号", "国服", "****1234"),
            parse_monument(_monument_fixture()),
            monument_group,
            "b",
            {},
        )
        # 列头用中文难度名，与详情页的难度字面一致
        self.assertIn(
            '<div class="ladder-head"><span>#</span><span>关卡</span><span class="head-tier">普通</span>'
            '<span class="head-tier head-hard">苦难</span></div>',
            overview,
        )
        self.assertNotIn("NORMAL", overview)
        self.assertNotIn(">HARD<", overview)
        # 关卡数直接决定行数：主题加关不塌版
        self.assertEqual(overview.count('<div class="ladder-row'), 1)
        # 主题图整张供在海报位上，不裁切也不压暗幕
        self.assertIn("theme-poster", overview)
        self.assertIn("theme-figure", overview)
        self.assertNotIn("✓", overview)
        # 历届主题里当前主题必须被标出来，其余保持静默
        self.assertEqual(overview.count("history-chip is-current"), 1)

    def test_monument_detail_pairs_both_tiers_and_marks_current(self):
        group = parse_monument(_monument_fixture()).current()
        normal = group.stages[0][0]
        identity = ChallengeIdentity("脱敏账号", "国服", "****1234")
        html = _monument_detail_html(identity, group, normal, "b", {})
        # 页眉讲主题、题头讲关卡：同一页不再把关卡名与难度各说三遍
        self.assertIn("影拓丰碑 · 当前主题", html)
        self.assertIn("MONUMENT / STAGE 01 OF 01", html)
        self.assertIn("清波访客</h1>", html)
        self.assertNotIn("PERSONAL CHALLENGE / MONUMENT DETAIL", html)
        # 同关两档并置，正在查看的档位反相；档位身份色由行内 --tier-* 给出
        self.assertEqual(html.count('<div class="duo-row'), 2)
        self.assertIn('<div class="duo-row is-current" style=', html)
        self.assertIn("--tier-bar:#d64035", html)
        self.assertIn("推荐 Lv.60", html)
        self.assertIn("推荐 Lv.90", html)
        self.assertIn("未记录", html)
        # 头像取不到时留空框，不把 charId 片段印给用户
        self.assertIn("team-avatar is-empty", html)
        self.assertNotIn("char-a", html)

    def test_war_detail_marks_only_the_viewed_tier(self):
        """三档的 name 完全相同，只能按身份+难度判定当前档。"""
        season = parse_war_echoes(_war_fixture()).current()
        week = season.weeks[0]
        group = week.groups[0]
        identity = ChallengeIdentity("脱敏账号", "国服", "****1234")
        for tier, bar in ((group.cruel, "#d64035"), (group.hard, "#2c63a8"), (group.normal, "rgba(255,255,255,.34)")):
            html = challenge._war_detail_html(identity, season, week, group, tier, "b", {})
            self.assertEqual(html.count("duo-row is-current"), 1, tier.difficulty)
            self.assertIn(f"--tier-bar:{bar}", html)
            self.assertIn("GROUP 01 OF 01", html)
            self.assertNotIn("✓", html)

    def test_best_cleared_reports_the_highest_cleared_tier(self):
        """题头大字答「最高通关档」，不是「正在查看的那一档」。"""
        group = parse_monument(_monument_fixture()).current()
        normal, hard = group.stages[0]
        self.assertEqual(
            _best_cleared_html((normal, hard), ("hard", "normal"), _monument_difficulty_label),
            ("01:05", "普通 · " + datetime.fromtimestamp(normal.record.record_ts or normal.first_pass_ts).strftime("%Y-%m-%d %H:%M")),
        )
        # 全没过就明说没有，不用 0 或假数字填
        blank = replace(normal, passed=False, record=ChallengeRecord())
        self.assertEqual(
            _best_cleared_html((blank, hard), ("hard", "normal"), _monument_difficulty_label),
            ("--", "尚无通关记录"),
        )
        # 过了但接口没给用时：说「用时未返回」，不编一个数字
        slow = replace(hard, passed=True, record=ChallengeRecord(record_ts=1800000000))
        self.assertEqual(
            _best_cleared_html((normal, slow), ("hard", "normal"), _monument_difficulty_label),
            ("用时未返回", "苦难 · " + datetime.fromtimestamp(slow.record.record_ts).strftime("%Y-%m-%d %H:%M")),
        )

    def test_feature_items_split_official_bullets(self):
        items = _feature_items(
            " - 敌人被击败时，其他所有敌人回复一定生命值。\n\n <@ba.info>- 禁止使用战术物品和消耗品。</>"
        )
        self.assertEqual(items, ("敌人被击败时，其他所有敌人回复一定生命值", "禁止使用战术物品和消耗品"))

    def test_war_overview_shows_best_cleared_tier_only(self):
        raw = _war_fixture()
        dungeon_group = raw["seasons"][0]["weeks"][0]["dungeonGroups"][0]
        for key, duration, char_id in (
            ("normalDungeon", "61", "unit-normal"),
            ("hardDungeon", "72", "unit-hard"),
            ("cruelDungeon", "83", "unit-cruel"),
        ):
            dungeon_group[key]["bestRecord"] = {
                "chars": [{"charId": char_id}],
                "passTs": duration,
                "ts": "1800000000",
            }
        war_group = parse_war_echoes(raw).current().current_week().groups[0]
        # 残酷未通关 → 只显示最高的已通关档（困难），低档不再出现
        card = _war_stage_card(war_group, {}, 1)
        self.assertIn("困难", card)
        self.assertIn("01:12", card)
        for absent in ("普通", "残酷", "01:01", "01:23"):
            self.assertNotIn(absent, card)
        # 残酷通关后自动切换到最高档
        dungeon_group["cruelDungeon"]["isPass"] = True
        cleared = parse_war_echoes(raw).current().current_week().groups[0]
        cruel_card = _war_stage_card(cleared, {}, 1)
        self.assertIn("残酷", cruel_card)
        self.assertIn("01:23", cruel_card)
        self.assertNotIn("01:12", cruel_card)

    def test_war_history_assets_match_highest_cleared_tier_only(self):
        raw = _war_fixture()
        group = raw["seasons"][0]["weeks"][0]["dungeonGroups"][0]
        for key, avatar, enemy in (
            ("normalDungeon", "https://assets.invalid/normal.png", "https://assets.invalid/normal-enemy.png"),
            ("hardDungeon", "https://assets.invalid/hard.png", "https://assets.invalid/hard-enemy.png"),
            ("cruelDungeon", "https://assets.invalid/cruel.png", "https://assets.invalid/cruel-enemy.png"),
        ):
            group[key]["bestRecord"] = {
                "chars": [{"charId": key, "avatarUrl": avatar}],
                "passTs": "60",
            }
            group[key]["enemies"] = [{"name": "敌人", "imageUrl": enemy}]

        season = parse_war_echoes(raw).current()
        urls = challenge._asset_urls_war_history((season,))

        self.assertIn("https://assets.invalid/war.png", urls)
        self.assertIn("https://assets.invalid/hard.png", urls)
        for absent in (
            "https://assets.invalid/normal.png",
            "https://assets.invalid/cruel.png",
            "https://assets.invalid/normal-enemy.png",
            "https://assets.invalid/hard-enemy.png",
            "https://assets.invalid/cruel-enemy.png",
        ):
            self.assertNotIn(absent, urls)

    def test_war_history_batch_prepares_once_and_renders_serially(self):
        payload = parse_war_echoes(_war_fixture())
        identity = ChallengeIdentity("脱敏账号", "国服", "****1234")
        hero = "https://endfield.local/assets/war-header"
        prepared = SimpleNamespace(
            urls={payload.seasons[0].header_url: hero},
            resources={hero: object()},
        )
        active = 0
        max_active = 0
        pages: list[int] = []
        documents: list[str] = []

        async def fake_render(document, resources, *, kind, page=0, page_count=0):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            pages.append(page)
            documents.append(document)
            await asyncio.sleep(0.01)
            active -= 1
            return f"png-{page}".encode()

        async def run():
            with patch.object(
                challenge,
                "_prepare_challenge_assets",
                AsyncMock(return_value=prepared),
            ) as prepare:
                with patch.object(challenge, "_render_challenge", side_effect=fake_render):
                    result = await challenge.draw_war_history_pages(
                        identity,
                        (payload.seasons[0], payload.seasons[0]),
                        achievements=payload.achievements,
                    )
                self.assertEqual(prepare.await_count, 1)
                return result

        result = asyncio.run(run())
        self.assertEqual(result, (b"png-1", b"png-2"))
        self.assertEqual(pages, [1, 2])
        self.assertEqual(max_active, 1)
        self.assertTrue(all(hero in document for document in documents))

    def test_challenge_renderer_uses_bounded_cdp_export(self):
        screenshot = AsyncMock(return_value=b"raw-png")
        optimize = AsyncMock(return_value=b"optimized-png")

        async def run():
            with patch.object(challenge, "screenshot_web_element", screenshot):
                with patch.object(challenge, "run_image_render", optimize):
                    return await challenge._render_challenge(
                        "<main class='challenge-card'></main>",
                        {},
                        kind="war_history",
                        page=1,
                        page_count=2,
                    )

        self.assertEqual(asyncio.run(run()), b"optimized-png")
        kwargs = screenshot.await_args.kwargs
        self.assertTrue(kwargs["wait_for_images"])
        self.assertTrue(kwargs["wait_for_fonts"])
        self.assertEqual(kwargs["resource_wait_timeout_ms"], 5000)
        self.assertEqual(kwargs["screenshot_timeout_ms"], 60000)
        self.assertEqual(kwargs["screenshot_backend"], "cdp")

    def test_war_rating_key_follows_stars_and_plus_tasks(self):
        def season(stars, plus):
            raw = _war_fixture()
            raw["seasons"][0]["stars"] = stars
            raw["seasons"][0]["allPlusTasks"] = plus
            return parse_war_echoes(raw).current()

        self.assertEqual(challenge._war_rating_key(season(0, False)), "unrated")
        self.assertEqual(challenge._war_rating_key(season(1, False)), "d")
        self.assertEqual(challenge._war_rating_key(season(2, True)), "d")
        self.assertEqual(challenge._war_rating_key(season(4, True)), "c")
        self.assertEqual(challenge._war_rating_key(season(5, False)), "b")
        self.assertEqual(challenge._war_rating_key(season(6, True)), "b")
        self.assertEqual(challenge._war_rating_key(season(8, True)), "a")
        self.assertEqual(challenge._war_rating_key(season(9, False)), "s")
        self.assertEqual(challenge._war_rating_key(season(9, True)), "s_plus")

    def test_war_overview_prefers_rating_image_with_star_fallback(self):
        payload = parse_war_echoes(_war_fixture())
        season = payload.current()
        identity = ChallengeIdentity("脱敏账号", "国服", "****1234")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(challenge, "WAR_RATING_ASSET_DIR", Path(tmp)):
                # 缺图：回退星标刻度
                self.assertEqual(challenge._war_rating_html(season), "")
                fallback = challenge._war_overview_html(identity, payload, season, "b", {})
                self.assertIn('class="stars"', fallback)
                # 图就位：按当前评级选章并替换星标
                name = challenge.WAR_RATING_FILES[challenge._war_rating_key(season)]
                (Path(tmp) / name).write_bytes(b"png")
                self.assertIn('class="seal-rating"', challenge._war_rating_html(season))
                replaced = challenge._war_overview_html(identity, payload, season, "b", {})
                self.assertIn('class="seal-rating"', replaced)
                self.assertNotIn('class="stars"', replaced)

    def test_empty_payload_has_no_personal_records(self):
        monument = parse_monument({"indieHardGroups": [{"name": "空主题", "dungeonGroups": [{"normalDungeon": {"name": "N", "isPass": False}, "hardDungeon": {"name": "H", "isPass": False}}]}]})
        war = parse_war_echoes({"seasons": [{"name": "空赛季", "weeks": [{"name": "空轮换", "dungeonGroups": [{"name": "空关卡", "normalDungeon": {"name": "N", "isPass": False}}]}]}]})
        self.assertFalse(monument.has_records)
        self.assertFalse(war.has_records)

    def test_challenge_css_uses_adaptive_canvas_and_variants(self):
        css = _css("b", "#286cd6", "")
        # 宽度可被 --card-width 收窄，高度随内容生长（不再写死 1080）。
        self.assertIn("width:var(--card-width,1920px)", css)
        self.assertIn("height:auto", css)
        self.assertIn("min-height:640px", css)
        self.assertIn("--hero-width:20%", _css("a", "#a9ef45", ""))
        self.assertIn("--hero-width:55%", _css("c", "#286cd6", ""))


class EndfieldForwardSendTests(unittest.TestCase):
    """合并转发：优先 Satori 原生 <message forward>，失败才回退 OneBot 动作。"""

    def test_make_forward_nests_pages_with_one_author(self):
        from otae_bot.adapters.entari import make_forward

        element = make_forward(
            ["第一页", "第二页"],
            name="Endfield",
            uin="10001",
        )
        dumped = str(element)
        self.assertTrue(dumped.startswith("<message forward>"))
        self.assertEqual(dumped.count("<message>"), 2)
        self.assertEqual(dumped.count('<author id="10001" name="Endfield"/>'), 2)
        self.assertIn("第一页", dumped)
        self.assertIn("第二页", dumped)

    def test_make_forward_omits_author_without_uin(self):
        from otae_bot.adapters.entari import make_forward

        self.assertNotIn("<author", str(make_forward(["仅正文"])))

    def test_forward_prefers_satori_element(self):
        import plugins.endfield.handlers as endfield_plugin

        send_forward = AsyncMock()
        onebot = AsyncMock()

        async def run():
            with patch.object(endfield_plugin, "send_forward", send_forward):
                with patch.object(endfield_plugin, "send_forward_images", onebot):
                    with patch.object(endfield_plugin, "_png_image", lambda png: png):
                        await endfield_plugin._send_forward_pngs(
                            SimpleNamespace(self_id="10001"),
                            SimpleNamespace(),
                            (b"page-1", b"page-2", b"page-3"),
                        )

        asyncio.run(run())
        onebot.assert_not_awaited()
        self.assertEqual(send_forward.await_args.args[0], [b"page-1", b"page-2", b"page-3"])
        self.assertEqual(send_forward.await_args.kwargs["uin"], "10001")

    def test_forward_falls_back_to_onebot_action(self):
        import plugins.endfield.handlers as endfield_plugin

        send_forward = AsyncMock(side_effect=RuntimeError("no session"))
        onebot = AsyncMock()
        pages = (b"page-1", b"page-2", b"page-3")

        async def run():
            with patch.object(endfield_plugin, "send_forward", send_forward):
                with patch.object(endfield_plugin, "send_forward_images", onebot):
                    with patch.object(endfield_plugin, "_png_image", lambda png: png):
                        await endfield_plugin._send_forward_pngs(
                            SimpleNamespace(self_id="10001"),
                            SimpleNamespace(),
                            pages,
                        )

        asyncio.run(run())
        self.assertEqual(onebot.await_args.args[2], pages)


class EndfieldChallengeClientTests(unittest.TestCase):
    def test_personal_endpoints_return_challenge_data_and_sign_query(self):
        async def run():
            client = EndfieldOfficialClient(http=SimpleNamespace())
            client._skland_context = AsyncMock(return_value=SimpleNamespace(provider="hypergryph"))
            client._signed_skland_request = AsyncMock(side_effect=[
                {"data": {"indieHard": {"indieHardGroups": []}}},
                {"data": {"warEchoes": {"seasons": []}}},
            ])
            role = SimpleNamespace(role_id="role", server_id="1")
            self.assertEqual(await client.indie_hard("token", role), {"indieHardGroups": []})
            self.assertEqual(await client.war_echoes("token", role, season_id="2"), {"seasons": []})
            first_call = client._signed_skland_request.await_args_list[0]
            first = first_call.kwargs
            second = client._signed_skland_request.await_args_list[1].kwargs
            self.assertEqual(first_call.args[2], "/api/v1/game/endfield/card/indie-hard")
            self.assertNotIn("seasonId", first["params"])
            self.assertEqual(second["params"]["seasonId"], "2")

        asyncio.run(run())

    def test_successful_null_business_payload_is_empty_record(self):
        async def run():
            client = EndfieldOfficialClient(http=SimpleNamespace())
            client._skland_context = AsyncMock(return_value=SimpleNamespace(provider="hypergryph"))
            client._signed_skland_request = AsyncMock(side_effect=[{"data": {"indieHard": None}}, {"data": {"warEchoes": None}}])
            role = SimpleNamespace(role_id="role", server_id="1")
            self.assertEqual(await client.indie_hard("token", role), {})
            self.assertEqual(await client.war_echoes("token", role), {})

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
