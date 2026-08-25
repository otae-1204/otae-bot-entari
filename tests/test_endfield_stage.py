from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import plugins.endfield as endfield
from plugins.endfield import commands
from plugins.endfield.client import WarfarinAPIError
from plugins.endfield import stage_draw
from plugins.endfield.stage_draw import (
    _stage_icon_urls,
    render_stage_card_html,
    render_stage_catalog_html,
)
from plugins.endfield.stage_models import (
    CrisisContractStageDetails,
    MonumentStageDetails,
    Stage,
    StageCardView,
    StageCatalogGroup,
    StageCatalogItem,
    StageCatalogView,
    StageEnemyPoise,
    StageEnemyResistance,
    StageSourceRef,
    StageVariant,
    WarEchoStageDetails,
)
from plugins.endfield.akedata_stage_source import (
    AkeDataStageSource,
    AkeDataVersion,
    _enemy_metrics,
    _enemy_modifiers,
    _enemy_poise,
    _translated,
    parse_akedata_catalog,
    parse_akedata_stage,
)
from plugins.endfield.stage_service import (
    EndfieldStageService,
    StageMatch,
    StageVariantNotFound,
    _match_item,
    select_variant,
)
from plugins.endfield.stage_source import FZStageSource, _localized, parse_enemy_resistances, parse_fz_stage


def _resistance_models() -> tuple[StageEnemyResistance, ...]:
    return tuple(
        StageEnemyResistance(
            element=row["element"],
            label=row["elementLabel"],
            percent=row["percent"],
            scalar=row["scalar"],
            color=row["color"],
        )
        for row in _RESISTANCE_ROWS
    )
from plugins.endfield.sources import source_order


def _article(title: str, content: list[dict], *, description: str = "测试说明") -> dict:
    return {
        "article": {
            "title": title,
            "description": description,
            "categories": ["副本", "危境再现"] if title.startswith("危境再现/") else ["能量淤积点"],
            "currentRevisionId": "revision-1",
            "updatedAt": "2026-07-24T01:02:03Z",
        },
        "revision": {
            "id": "revision-1",
            "createdAt": "2026-07-24T01:02:03Z",
            "contentJson": {"type": "doc", "content": content},
        },
    }


def _boss_fixture() -> dict:
    depths = []
    for order, (label, level) in enumerate((("一级", 40), ("二级", 60), ("三级", 80), ("四级", 90)), 1):
        depths.append(
            {
                "dungeonId": f"rodin-{order}",
                "depthLabel": label,
                "recommendLv": level,
                "flavor": f"{label}客观机制说明",
                "enemies": [
                    {
                        "enemyId": "rodin",
                        "name": "罗丹",
                        "level": level,
                        "hp": order * 1000,
                        "atk": order * 100,
                        "def": 100,
                    }
                ],
            }
        )
    return _article(
        "危境再现/罗丹",
        [
            {
                "type": "wikiTemplateInstance",
                "attrs": {
                    "hero": {
                        "bossName": "罗丹",
                        "intro": "协议空间中的领袖敌人重战关卡。",
                        "seriesId": "boss-rush-rodin",
                        "seriesName": "危境再现·罗丹",
                        "depthCount": 4,
                    },
                    "depths": {"depths": depths},
                },
            }
        ],
    )


_RESISTANCE_ROWS = [
    {"element": "Physical", "elementLabel": "物理", "percent": 80.0, "scalar": 0.8, "color": "888888"},
    {"element": "Fire", "elementLabel": "灼热", "percent": 100.0, "scalar": 1.0, "color": "FF623D"},
    {"element": "Pulse", "elementLabel": "电磁", "percent": 100.0, "scalar": 1.0, "color": "FFC000"},
    {"element": "Cryst", "elementLabel": "寒冷", "percent": 100.0, "scalar": 1.0, "color": "21C6D0"},
    {"element": "Natural", "elementLabel": "自然", "percent": 100.0, "scalar": 1.0, "color": "9EDC23"},
    {"element": "Ether", "elementLabel": "超域", "percent": 100.0, "scalar": 1.0, "color": "A678E8"},
]


_POISE_GROUP = {
    "key": "poise",
    "label": "失衡",
    "rows": [
        {"label": "失衡值上限", "value": 280.0, "format": "scalar", "attrType": "MaxPoise"},
        {
            "label": "失衡承伤倍率",
            "value": 1.75,
            "format": "multiplier",
            "attrType": "BreakingAttackDamageTakenScalar",
        },
        {"label": "失衡恢复时间", "value": 11.0, "format": "seconds", "attrType": "PoiseRecTime"},
    ],
}


def _enemy_article_fixture(*, knots: list | None = None) -> dict:
    """A standalone `敌人/<name>` article — the only place poise knots are published."""
    return {
        "article": {"title": "敌人/碾骨先锋", "categories": ["敌人", "普通敌人"], "currentRevisionId": "enemy-r1"},
        "revision": {
            "id": "enemy-r1",
            "contentJson": {
                "type": "doc",
                "content": [
                    {"type": "endfieldCardEnemyHero", "attrs": {"name": "碾骨先锋"}},
                    {
                        "type": "endfieldCardEnemyStats",
                        "attrs": {
                            "curve": [{"level": 1, "hp": 166, "atk": 26, "def": 100}],
                            "groups": [dict(_POISE_GROUP)],
                            "poiseKnots": [0.25, 0.5, 0.75] if knots is None else knots,
                        },
                    },
                    {"type": "endfieldCardEnemyResistances", "attrs": {"rows": list(_RESISTANCE_ROWS)}},
                ],
            },
        },
    }


def _akedata_fixture() -> tuple[AkeDataVersion, tuple[dict, ...]]:
    version = AkeDataVersion(
        "1.4.4@fixture-1",
        "public/1.4.4/fixture-1/TableCfg",
        "2026-07-27T00:00:00Z",
    )
    texts = {
        "1": "山中见犼",
        "2": "撼山雾火",
        "3": "撼山雾火·苦难",
        "4": "雾中有兽，撼山而来。",
        "5": "- 敌人进入雾中时获得强化。\n<@ba.info>- 敌人抗性被调整。</>",
        "6": "- 禁止使用战术物品。",
        "7": "巨山犼兽",
        "8": "存续的痕迹",
    }
    series = {
        "indie_group_h06": {
            "id": "indie_group_h06",
            "gameCategory": "dungeon_highdifficulty",
            "sortId": 6,
            "name": {"id": 1, "text": ""},
            "includeDungeonIds": ["indie_hard022", "indie_hard022_s"],
        }
    }
    normal = {
        "dungeonId": "indie_hard022",
        "dungeonSeriesId": "indie_group_h06",
        "dungeonCategory": "dungeon_highdifficulty",
        "dungeonName": {"id": 2, "text": ""},
        "dungeonDesc": {"id": 4, "text": ""},
        "featureDesc": {"id": 5, "text": ""},
        "recommendLv": 60,
        "costStamina": 0,
        "enemyIds": ["eny_monument_fixture"],
        "enemyLevels": [60],
        "firstPassRewardId": "reward_monument_fixture",
    }
    hard = dict(normal)
    hard.update(
        dungeonId="indie_hard022_s",
        dungeonName={"id": 3, "text": ""},
        featureDesc={"id": 6, "text": ""},
        recommendLv=90,
        enemyLevels=[90],
    )
    dungeons = {normal["dungeonId"]: normal, hard["dungeonId"]: hard}
    rewards = {
        "reward_monument_fixture": {
            "rewardId": "reward_monument_fixture",
            "itemBundles": [{"id": "item_char_skill_crown", "count": 1}],
        }
    }
    items = {
        "item_char_skill_crown": {
            "id": "item_char_skill_crown",
            "name": {"id": 8, "text": ""},
            "iconId": "item_char_skill_crown",
            "rarity": 5,
        }
    }
    enemies = {
        "eny_monument_fixture": {
            "enemyId": "eny_monument_fixture",
            "templateId": "eny_monument",
            "attrTemplateId": "eny_monument_fixture",
        }
    }
    displays = {"eny_monument": {"templateId": "eny_monument", "name": {"id": 7, "text": ""}}}
    attributes = {
        "eny_monument_fixture": {
            "levelDependentAttributes": [
                {
                    "attrs": [
                        {"attrType": 0, "attrValue": level},
                        {"attrType": 1, "attrValue": hp},
                        {"attrType": 2, "attrValue": attack},
                        {"attrType": 3, "attrValue": 100},
                    ]
                }
                for level, hp, attack in ((60, 358880, 1916), (90, 1329887, 3304))
            ],
            "physicalResistance": 0,
            "fireResistance": 16,
            "pulseResistance": 16,
            "crystResistance": 16,
            "naturalResistance": 16,
            "poiseKnotPctList": [0.5],
        }
    }
    return version, (series, dungeons, texts, rewards, items, enemies, displays, attributes)


def _shared_flavor_fixture() -> dict:
    """Depth flavor repeats the stage intro, exactly like the live FZ entries."""
    intro = "极度危险的协议空间。想要进入其中的话，就做好全方位的准备吧。"
    icon = "https://assets.fz.wiki/boss.png"
    rows = (("一级", 60, 247504, 2395, "“你将重逢毫无情感的毁灭。”"), ("二级", 80, 595357, 3604, "“你将窥见冰冷过去的一角。”"))
    depths = [
        {
            "dungeonId": f"trinity-{order}",
            "depthLabel": label,
            "recommendLv": level,
            "flavor": f"{intro}\n{quote}",
            "enemies": [
                {
                    "enemyId": "trinity",
                    "name": "三位一体",
                    "iconUrl": icon,
                    "level": level,
                    "hp": hp,
                    "atk": atk,
                    "def": 100,
                    "resistances": list(_RESISTANCE_ROWS),
                    "groups": [dict(_POISE_GROUP)],
                }
            ],
        }
        for order, (label, level, hp, atk, quote) in enumerate(rows, 1)
    ]
    return _article(
        "危境再现/三位一体",
        [
            {
                "type": "wikiTemplateInstance",
                "attrs": {
                    "hero": {
                        "bossName": "三位一体",
                        "intro": intro,
                        "seriesId": "trinity",
                        "seriesName": "危境再现·三位一体",
                        "depthCount": 2,
                        "iconUrl": icon,
                    },
                    "depths": {"depths": depths},
                },
            }
        ],
    )


def _energy_fixture(*, include_tables: bool = True) -> dict:
    content = [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "强度：普通 · 推荐等级：50 · 地区：四号谷地"}
            ],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "能量淤积于此，激活后会有敌人出现。"}],
        },
    ]
    if include_tables:
        content.extend(
            [
                {
                    "type": "wikiIndexTable",
                    "attrs": {
                        "columns": [
                            {"key": "name"},
                            {"key": "rarity"},
                            {"key": "skill"},
                        ],
                        "entries": [{"name": "无瑕基质·切骨", "rarity": 5, "skill": "切骨"}],
                    },
                },
                {
                    "type": "wikiIndexTable",
                    "attrs": {
                        "columns": [
                            {"key": "name"},
                            {"key": "weaponType"},
                            {"key": "maxLv"},
                        ],
                        "entries": [{"name": "不知归", "weaponType": "单手剑", "maxLv": 90}],
                    },
                },
                {
                    "type": "wikiIndexTable",
                    "attrs": {
                        "columns": [
                            {"key": "name"},
                            {"key": "wave"},
                            {"key": "cond"},
                            {"key": "count"},
                            {"key": "level"},
                        ],
                        "entries": [
                            {
                                "enemyId": "enemy-1",
                                "name": "碾骨先锋",
                                "title": "敌人/碾骨先锋",
                                "wave": 1,
                                "cond": "立即",
                                "time": 1,
                                "count": 2,
                                "level": 50,
                            }
                        ],
                    },
                },
            ]
        )
    return _article("能量淤积点·供能高地", content)


def _crisis_contract_fixture() -> dict:
    return _article(
        "危机合约",
        [
            {
                "type": "wikiTemplateInstance",
                "attrs": {
                    "templateName": "危机合约",
                    "overview": {"title": "危机合约 重燃测试作战", "intro": "仅展示客观活动规则。"},
                    "board": {
                        "activityId": "activity-contract-0",
                        "contracts": [
                            {
                                "tagId": 102801,
                                "name": {"zh": "队列：萎缩"},
                                "desc": {"zh": "干员主能力值降低。"},
                                "score": 1,
                                "level": 1,
                                "groupId": 1028,
                                "lockIds": ["102802"],
                                "conflictId": "c1028",
                            }
                        ],
                        "unlockRegions": [{"label": "指标集Ⅰ", "score": 1}],
                        "dungeon": {
                            "dungeonId": "indie-contract-1",
                            "recommendLv": 80,
                            "desc": "包含连续波次战斗。",
                            "featureDesc": "敌人由天使构成。",
                            "configs": [
                                {
                                    "configId": "standard",
                                    "label": "标准",
                                    "waves": [
                                        {
                                            "waveIdx": "001",
                                            "enemies": [
                                                {"enemyId": "angel", "name": "大角天使", "level": 60},
                                                {"enemyId": "angel", "name": "大角天使", "level": 60},
                                            ],
                                        }
                                    ],
                                    "enemies": [
                                        {
                                            "enemyId": "angel",
                                            "name": "大角天使",
                                            "level": 60,
                                            "baseAttrs": {"MaxHp": 24750, "Atk": 1198, "Def": 100},
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                    "levels": {
                        "title": "等级奖励",
                        "levels": [
                            {
                                "level": 1,
                                "score": 25,
                                "rewards": [{"itemId": "coin", "name": {"zh": "晶化硬币"}, "count": 15}],
                            }
                        ],
                    },
                    "tasks": {
                        "title": "作战任务",
                        "groups": [
                            {
                                "groupId": "weekly",
                                "name": {"zh": "每周任务"},
                                "tasks": [
                                    {
                                        "taskId": "task-1",
                                        "desc": {"zh": "完成指定指标组合。"},
                                        "rewards": [{"itemId": "coin", "name": {"zh": "晶化硬币"}, "count": 500}],
                                    }
                                ],
                            }
                        ],
                    },
                    "shop": {
                        "title": "机密圣所",
                        "currencyName": {"zh": "晶化硬币"},
                        "shops": [
                            {
                                "shopId": "shop-1",
                                "name": {"zh": "常设兑换"},
                                "goods": [
                                    {
                                        "items": [{"itemId": "sticker", "name": {"zh": "贴纸"}, "count": 1}],
                                        "actualPrice": 200,
                                        "currencyName": {"zh": "晶化硬币"},
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        ],
    )


def _war_echo_fixture() -> dict:
    return _article(
        "活动/战争回响/谵妄赛季",
        [
            {
                "type": "wikiTemplateInstance",
                "attrs": {
                    "templateName": "战争回响",
                    "overview": {
                        "name": {"zh": "谵妄赛季"},
                        "desc": "赛季制常驻战斗挑战。",
                        "ranks": [{"stars": 1}, {"stars": 9}],
                    },
                    "cycles": {
                        "cycles": [
                            {
                                "week": 1,
                                "name": {"zh": "谵妄轮换Ⅰ"},
                                "themes": [{"groupId": "tower-1", "name": {"zh": "野性旧事"}}],
                            }
                        ]
                    },
                    "stages": {
                        "themes": [
                            {
                                "groupId": "tower-1",
                                "name": {"zh": "野性旧事"},
                                "featureLines": ["击败敌人时恢复技力。"],
                                "difficulties": [
                                    {
                                        "gameId": "tower-normal",
                                        "star": 1,
                                        "label": "普通",
                                        "recommendLv": 60,
                                        "flavor": "野兽徘徊于战争遗迹。",
                                        "enemies": [
                                            {"enemyId": "beast", "name": "撕裂牙兽", "level": 60, "count": 2, "hp": 1000}
                                        ],
                                        "waves": [{"wave": 1, "entries": [{"name": "撕裂牙兽", "count": 2}]}],
                                        "rewards": [{"itemId": "gold", "name": {"zh": "折金票"}, "count": 25000}],
                                    },
                                    {
                                        "gameId": "tower-hard",
                                        "star": 2,
                                        "label": "困难",
                                        "recommendLv": 75,
                                        "enemies": [],
                                        "waves": [],
                                        "rewards": [],
                                    },
                                ],
                            }
                        ]
                    },
                },
            }
        ],
    )


class EndfieldStageCommandTests(unittest.TestCase):
    def test_stage_and_dungeon_aliases_are_equivalent(self):
        stage = commands.parse_command("关卡 罗丹 三级")
        dungeon = commands.parse_command("副本 罗丹 三级")
        self.assertEqual((stage.action, stage.scope, stage.query), ("query", "stage", "罗丹 三级"))
        self.assertEqual(dungeon, stage)

    def test_stage_catalog_and_scoped_search(self):
        catalog = commands.parse_command("副本")
        search = commands.parse_command("搜索 副本 罗丹")
        self.assertEqual((catalog.scope, catalog.query), ("stage", ""))
        self.assertEqual((search.action, search.scope, search.query), ("search", "stage", "罗丹"))

    def test_help_and_source_list_stage(self):
        self.assertIn("/ef 副本 <关卡名> [变体名|总览]", commands.format_help())
        self.assertIn("关卡：FZ Wiki、AkeData", commands.format_source())
        self.assertEqual(source_order("stage"), ("fz", "akedata"))

    def test_akedata_source_option_and_alias(self):
        parsed = commands.parse_command("副本 撼山雾火 --source ake")
        self.assertEqual((parsed.scope, parsed.query, parsed.source), ("stage", "撼山雾火", "akedata"))
        self.assertIn("fz、akedata、warfarin", commands.parse_command("副本 --source unknown").error)

    def test_longest_stage_name_does_not_become_a_variant(self):
        item = StageCatalogItem(
            "能量淤积点·训练三级",
            "训练三级",
            "energy_deposit",
            "能量淤积点",
            "r1",
            "2026-07-24",
        )
        match = _match_item("训练三级", item)
        self.assertEqual((match.display_name, match.selector, match.mode), ("训练三级", "", "detail"))


class EndfieldStageSourceTests(unittest.TestCase):
    def test_boss_depths_become_ordered_variants(self):
        stage = parse_fz_stage(_boss_fixture())
        self.assertEqual(stage.name, "罗丹")
        self.assertEqual([item.label for item in stage.variants], ["一级", "二级", "三级", "四级"])
        self.assertEqual([item.sort_order for item in stage.variants], [1, 2, 3, 4])
        self.assertEqual(stage.variants[-1].enemies[0].hp, 4000)
        self.assertIsNone(stage.variants[-1].rewards)

    def test_energy_tables_are_typed(self):
        stage = parse_fz_stage(_energy_fixture())
        variant = stage.variants[0]
        self.assertEqual(stage.location, "四号谷地")
        self.assertEqual(variant.recommended_level, 50)
        self.assertEqual(variant.rewards[0].name, "无瑕基质·切骨")
        self.assertEqual(variant.extension.weapon_references, ("不知归",))
        self.assertEqual(variant.extension.waves[0].enemy.count, 2)

    def test_missing_energy_tables_stay_unknown(self):
        stage = parse_fz_stage(_energy_fixture(include_tables=False))
        variant = stage.variants[0]
        self.assertIsNone(variant.rewards)
        self.assertIsNone(variant.enemies)
        self.assertIsNone(variant.extension.matrices)
        self.assertIsNone(variant.extension.waves)

    def test_boss_resistances_are_read_inline(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        rows = stage.variants[0].enemies[0].resistances
        self.assertEqual([(row.label, row.percent) for row in rows][:2], [("物理", 80.0), ("灼热", 100.0)])
        self.assertFalse(rows[0].is_standard)
        self.assertEqual(rows[0].reduction, 20.0)
        self.assertTrue(rows[1].is_standard)

    def test_absent_resistance_field_stays_unknown(self):
        stage = parse_fz_stage(_boss_fixture())
        self.assertIsNone(stage.variants[0].enemies[0].resistances)

    def test_enemy_article_resistance_card_is_parsed(self):
        rows = parse_enemy_resistances(_enemy_article_fixture())
        self.assertEqual([(row.label, row.percent) for row in rows][:2], [("物理", 80.0), ("灼热", 100.0)])
        self.assertIsNone(parse_enemy_resistances({"revision": {"contentJson": {"content": []}}}))

    def test_crisis_contract_sections_are_typed_and_enemy_counts_are_merged(self):
        stage = parse_fz_stage(_crisis_contract_fixture(), family_key="crisis_contract")
        self.assertIsInstance(stage.extension, CrisisContractStageDetails)
        self.assertEqual(stage.name, "危机合约 重燃测试作战")
        self.assertEqual(stage.extension.activity_id, "activity-contract-0")
        self.assertEqual(stage.extension.level_scores, (25,))
        self.assertEqual(stage.extension.metrics[0].lock_ids, ("102802",))
        self.assertEqual(stage.extension.task_groups[0].task_ids, ("task-1",))
        self.assertEqual(stage.extension.shops[0].goods_count, 1)
        self.assertEqual(stage.variants[0].enemies[0].count, 2)
        self.assertEqual(stage.variants[0].enemies[0].hp, 24750)
        self.assertEqual(stage.variants[0].waves[0].enemy.count, 2)
        self.assertEqual([block.key for block in stage.blocks], ["contracts", "levels", "tasks", "shop"])

    def test_war_echo_season_cycles_and_difficulties_are_typed(self):
        stage = parse_fz_stage(
            _war_echo_fixture(), entry_key="tower-1", family_key="war_echo"
        )
        self.assertIsInstance(stage.extension, WarEchoStageDetails)
        self.assertEqual(stage.name, "野性旧事")
        self.assertEqual(stage.extension.season_name, "谵妄赛季")
        self.assertEqual((stage.extension.week_count, stage.extension.max_stars), (1, 9))
        self.assertEqual(stage.extension.cycles[0].stage_group_ids, ("tower-1",))
        self.assertEqual([variant.label for variant in stage.variants], ["普通", "困难"])
        self.assertEqual(stage.variants[0].waves[0].enemy.name, "撕裂牙兽")
        self.assertEqual(stage.variants[0].rewards[0].quantity_text, "×25,000")

    def test_default_variant_uses_sort_order(self):
        stage = parse_fz_stage(_boss_fixture())
        shuffled = Stage(
            stage.id,
            stage.name,
            stage.aliases,
            stage.family_key,
            stage.family_name,
            stage.summary,
            stage.location,
            stage.unlock_condition,
            stage.source,
            (stage.variants[2], stage.variants[0], stage.variants[3], stage.variants[1]),
            stage.extension,
        )
        self.assertEqual(select_variant(shuffled).label, "四级")
        self.assertEqual(select_variant(shuffled, "三级").recommended_level, 80)

    def test_invalid_variant_lists_valid_labels(self):
        stage = parse_fz_stage(_boss_fixture())
        with self.assertRaises(StageVariantNotFound) as raised:
            select_variant(stage, "五级")
        self.assertEqual(raised.exception.valid_labels, ("一级", "二级", "三级", "四级"))
        self.assertIn("可选：一级、二级、三级、四级", str(raised.exception))


class EndfieldAkeDataStageSourceTests(unittest.TestCase):
    def test_localizes_object_shaped_stage_text(self):
        self.assertEqual(
            _translated(
                {"id": "stage-name"},
                {"stage-name": {"zh": "中文关卡", "en": "English Stage"}},
            ),
            "中文关卡",
        )
        self.assertEqual(_localized({"zh-CN": "中文标题", "en": "English Title"}), "中文标题")

    def test_catalog_discovers_monument_stages_and_keeps_series(self):
        version, tables = _akedata_fixture()
        catalog = parse_akedata_catalog(version, *tables[:3])
        self.assertEqual((catalog.source, catalog.revision), ("AkeData", version.id))
        self.assertEqual(catalog.groups[0].name, "影拓丰碑")
        item = catalog.groups[0].items[0]
        self.assertEqual((item.name, item.region, item.recommended_level), ("撼山雾火", "山中见犼", 90))
        self.assertEqual((item.stage_key, item.source), ("indie_hard022", "akedata"))

    def test_normal_and_hard_dungeons_become_typed_variants(self):
        version, tables = _akedata_fixture()
        stage = parse_akedata_stage(version, "indie_hard022", *tables)
        self.assertIsInstance(stage.extension, MonumentStageDetails)
        self.assertEqual((stage.name, stage.extension.series_name), ("撼山雾火", "山中见犼"))
        self.assertEqual([variant.label for variant in stage.variants], ["普通", "苦难"])
        self.assertEqual([variant.recommended_level for variant in stage.variants], [60, 90])
        hard = stage.variants[-1]
        self.assertEqual((hard.enemies[0].name, hard.enemies[0].hp), ("巨山犼兽", 1329887))
        self.assertEqual(hard.enemies[0].resistances[1].percent, 84.0)
        self.assertEqual(hard.enemies[0].poise.knots, (0.5,))
        self.assertEqual((hard.rewards[0].name, hard.rewards[0].quantity_text), ("存续的痕迹", "×1"))
        self.assertEqual(hard.reward_sets.title, "首通奖励")
        self.assertEqual(hard.mechanics, ("禁止使用战术物品。",))
        self.assertEqual(stage.source.revision, version.id)

    def test_enemy_stats_include_instance_and_spawner_buff_modifiers(self):
        version, tables = _akedata_fixture()
        series, dungeons, texts, rewards, items, enemies, displays, attributes = tables
        hard = dict(dungeons["indie_hard022_s"])
        hard["sceneId"] = "indie_hdg011"
        dungeons = {**dungeons, hard["dungeonId"]: hard}
        enemies = {
            **enemies,
            "eny_monument_fixture": {
                **enemies["eny_monument_fixture"],
                "bornBuffs": ["buff_poise_recover_fixture"],
                "attrModifiers": [
                    {"attrType": 1, "modifierType": 1, "attrValue": 0.5}
                ],
            },
        }
        attributes = {
            **attributes,
            "eny_monument_fixture": {
                **attributes["eny_monument_fixture"],
                "levelIndependentAttributes": {
                    "attrs": [
                        {"attrType": 20, "attrValue": 980},
                        {"attrType": 21, "attrValue": 10},
                        {"attrType": 27, "attrValue": 1.5},
                    ]
                },
                "poiseKnotPctList": [],
                "levelDependentAttributes": [
                    {
                        "attrs": [
                            {"attrType": 0, "attrValue": 90},
                            {"attrType": 1, "attrValue": 917164},
                            {"attrType": 2, "attrValue": 4130},
                            {"attrType": 3, "attrValue": 100},
                        ]
                    }
                ],
            },
        }
        spawners = {
            "indie_hdg011": (
                {
                    "enemyLibrary": [
                        {
                            "enemyId": "eny_monument_fixture",
                            "enemyLevel": 90,
                            "bornBuffList": [
                                {
                                    "buffId": "buff_dung_maxhp_01",
                                    "blackboard": [{"key": "ratio", "valueFloat": 0.8}],
                                }
                            ],
                        }
                    ]
                },
            )
        }
        buffs = {
            "buff_dung_maxhp_01": {
                "blackboard": [{"key": "ratio", "valueDouble": 0.1}],
                "attributeModifier": {
                    "attributeModifiers": [
                        {
                            "attributeType": "MaxHp",
                            "formulaItem": "Multiplier",
                            "param": {
                                "useBlackboardKey": True,
                                "blackboardKey": "ratio",
                                "value": -0.2,
                            },
                        }
                    ]
                },
            },
            "buff_poise_recover_fixture": {
                "attributeModifier": {
                    "attributeModifiers": [
                        {
                            "attributeType": "PoiseRecTimeScalar",
                            "formulaItem": "Multiplier",
                            "param": {"useBlackboardKey": False, "value": 0.8},
                        }
                    ]
                }
            },
        }
        stage = parse_akedata_stage(
            version,
            "indie_hard022",
            series,
            dungeons,
            texts,
            rewards,
            items,
            enemies,
            displays,
            attributes,
            spawners_by_scene=spawners,
            buff_table=buffs,
        )
        enemy = stage.variants[-1].enemies[0]
        self.assertEqual((enemy.hp, enemy.attack, enemy.defense), (2476342.8, 4130, 100))
        self.assertEqual((enemy.poise.max_value, enemy.poise.recover_seconds), (980.0, 10.0))
        self.assertEqual((enemy.poise.damage_scalar, enemy.poise.recover_scalar), (1.5, 1.8))
        self.assertIsNone(enemy.poise.knots)

        html = render_stage_card_html(StageCardView(stage, "detail", stage.variants[-1]))
        self.assertIn("<span>失衡值上限</span><b>980</b>", html)
        self.assertIn("<span>失衡恢复时间</span><b>10s</b>", html)
        self.assertIn("<span>处决承伤系数</span><b>1.50</b>", html)
        self.assertIn("<span>失衡恢复时间系数</span><b>1.80</b>", html)
        self.assertNotIn("失衡节点", html)

    def test_ritual_vortex_hard_applies_both_spawner_hp_buffs(self):
        enemy = {
            "attrModifiers": [
                {"attrType": 1, "modifierType": 4, "attrValue": 2.5}
            ],
            "bornBuffs": [],
        }
        library_buffs = (
            {"buffId": "buff_main_enemy", "blackboard": []},
            {
                "buffId": "buff_common_maxhpup",
                "blackboard": [{"key": "ratio", "valueFloat": 0.3}],
            },
        )
        buff_table = {
            "buff_main_enemy": {
                "attributeModifier": {
                    "attributeModifiers": [
                        {
                            "attributeType": "MaxHp",
                            "formulaItem": "BaseFinalMultiplier",
                            "param": {"useBlackboardKey": False, "value": 1.8},
                        },
                        {
                            "attributeType": "PoiseRecTime",
                            "formulaItem": "Addition",
                            "param": {"useBlackboardKey": False, "value": -3.0},
                        },
                    ]
                }
            },
            "buff_common_maxhpup": {
                "attributeModifier": {
                    "attributeModifiers": [
                        {
                            "attributeType": "MaxHp",
                            "formulaItem": "Multiplier",
                            "param": {
                                "useBlackboardKey": True,
                                "blackboardKey": "ratio",
                                "value": 0.5,
                            },
                        }
                    ]
                }
            },
        }
        attributes = {
            "levelDependentAttributes": [
                {
                    "attrs": [
                        {"attrType": 0, "attrValue": 90},
                        {"attrType": 1, "attrValue": 504440},
                        {"attrType": 2, "attrValue": 3097},
                        {"attrType": 3, "attrValue": 100},
                    ]
                }
            ],
            "levelIndependentAttributes": {
                "attrs": [
                    {"attrType": 20, "attrValue": 160},
                    {"attrType": 21, "attrValue": 7},
                    {"attrType": 27, "attrValue": 1.25},
                ]
            },
        }

        modifiers = _enemy_modifiers(enemy, library_buffs, buff_table)

        self.assertEqual(_enemy_metrics(attributes, 90, modifiers), (2950974, 3097, 100))
        self.assertEqual(_enemy_poise(attributes, modifiers).recover_seconds, 4.0)


class EndfieldAkeDataStageSourceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_resolves_manifest_path_and_reuses_loaded_tables(self):
        version, tables = _akedata_fixture()
        names = (
            "DungeonSeriesTable",
            "DungeonTable",
            "I18nTextTable_CN",
            "RewardTable",
            "ItemTable",
            "EnemyTable",
            "EnemyTemplateDisplayInfoTable",
            "EnemyAttributeTemplateTable",
        )
        table_map = dict(zip(names, tables))
        client = AsyncMock()
        client.akedata_manifest.return_value = {
            "latest": version.id,
            "updatedAt": version.updated_at,
            "versions": [{"id": version.id, "tableCfgPath": version.table_cfg_path}],
        }
        client.akedata_table.side_effect = lambda path, name: table_map[name]
        source = AkeDataStageSource(client)
        catalog = await source.catalog()
        stage, unreachable = await source.stage(catalog.groups[0].items[0].stage_key)
        self.assertEqual((stage.name, unreachable), ("撼山雾火", ()))
        self.assertEqual(client.akedata_table.await_count, len(names))
        self.assertTrue(
            all(call.args[0] == version.table_cfg_path for call in client.akedata_table.await_args_list)
        )

    async def test_source_loads_and_caches_spawner_and_buff_resources(self):
        version, fixture_tables = _akedata_fixture()
        tables = list(fixture_tables)
        dungeons = {
            dungeon_id: {**row, "sceneId": "indie_hdg011"}
            for dungeon_id, row in tables[1].items()
        }
        enemies = {
            **tables[5],
            "eny_monument_fixture": {
                **tables[5]["eny_monument_fixture"],
                "attrModifiers": [{"attrType": 1, "modifierType": 1, "attrValue": 0.5}],
            },
        }
        attributes = {
            **tables[7],
            "eny_monument_fixture": {
                **tables[7]["eny_monument_fixture"],
                "levelDependentAttributes": [
                    {
                        "attrs": [
                            {"attrType": 0, "attrValue": 60},
                            {"attrType": 1, "attrValue": 247504},
                            {"attrType": 2, "attrValue": 2395},
                            {"attrType": 3, "attrValue": 100},
                        ]
                    },
                    {
                        "attrs": [
                            {"attrType": 0, "attrValue": 90},
                            {"attrType": 1, "attrValue": 917164},
                            {"attrType": 2, "attrValue": 4130},
                            {"attrType": 3, "attrValue": 100},
                        ]
                    },
                ],
            },
        }
        tables[1], tables[5], tables[7] = dungeons, enemies, attributes
        names = (
            "DungeonSeriesTable",
            "DungeonTable",
            "I18nTextTable_CN",
            "RewardTable",
            "ItemTable",
            "EnemyTable",
            "EnemyTemplateDisplayInfoTable",
            "EnemyAttributeTemplateTable",
        )
        table_map = dict(zip(names, tables))
        resource_map = {
            "public/Json/SpawnerConfig/indie_hdg011/fixture.json": {
                "configId": "sc_indie_hdg011_fixture",
                "enemyLibrary": [
                    {
                        "enemyId": "eny_monument_fixture",
                        "enemyLevel": 60,
                        "bornBuffList": [
                            {
                                "buffId": "buff_dung_maxhp_01",
                                "blackboard": [{"key": "ratio", "valueFloat": 0.5}],
                            }
                        ],
                    },
                    {
                        "enemyId": "eny_monument_fixture",
                        "enemyLevel": 90,
                        "bornBuffList": [
                            {
                                "buffId": "buff_dung_maxhp_01",
                                "blackboard": [{"key": "ratio", "valueFloat": 0.8}],
                            }
                        ],
                    },
                ],
            },
            "public/Json/BuffData/buff_dung_maxhp_01.json": {
                "attributeModifier": {
                    "attributeModifiers": [
                        {
                            "attributeType": "MaxHp",
                            "formulaItem": "Multiplier",
                            "param": {
                                "useBlackboardKey": True,
                                "blackboardKey": "ratio",
                                "value": 0.1,
                            },
                        }
                    ]
                }
            },
        }
        client = AsyncMock()
        client.akedata_manifest.return_value = {
            "latest": version.id,
            "updatedAt": version.updated_at,
            "versions": [{"id": version.id, "tableCfgPath": version.table_cfg_path}],
        }
        client.akedata_table.side_effect = lambda path, name: table_map[name]
        client.akedata_asset_index.return_value = {
            "schemaVersion": 2,
            "datasets": {
                "json": {
                    "files": {
                        "SpawnerConfig/indie_hdg011/fixture.json": {
                            "size": 1,
                            "md5": "0" * 32,
                        }
                    }
                }
            },
        }
        client.akedata_public_json.side_effect = lambda path: resource_map[path]
        source = AkeDataStageSource(client)
        stage, _ = await source.stage("indie_hard022")
        self.assertEqual(stage.variants[-1].enemies[0].hp, 2476342.8)
        self.assertEqual(client.akedata_public_json.await_count, 2)
        await source.stage("indie_hard022")
        self.assertEqual(client.akedata_public_json.await_count, 2)
        self.assertEqual(client.akedata_asset_index.await_count, 1)

    async def test_missing_optional_spawner_files_keep_stage_query_available(self):
        version, fixture_tables = _akedata_fixture()
        tables = list(fixture_tables)
        tables[1] = {
            dungeon_id: {**row, "sceneId": "indie_hdg002"}
            for dungeon_id, row in tables[1].items()
        }
        names = (
            "DungeonSeriesTable",
            "DungeonTable",
            "I18nTextTable_CN",
            "RewardTable",
            "ItemTable",
            "EnemyTable",
            "EnemyTemplateDisplayInfoTable",
            "EnemyAttributeTemplateTable",
        )
        table_map = dict(zip(names, tables))
        client = AsyncMock()
        client.akedata_manifest.return_value = {
            "latest": version.id,
            "updatedAt": version.updated_at,
            "versions": [{"id": version.id, "tableCfgPath": version.table_cfg_path}],
        }
        client.akedata_table.side_effect = lambda path, name: table_map[name]
        client.akedata_asset_index.return_value = {
            "schemaVersion": 2,
            "datasets": {"json": {"files": {}}},
        }

        source = AkeDataStageSource(client)
        catalog = await source.catalog()
        stage, unreachable = await source.stage(catalog.groups[0].items[0].stage_key)

        self.assertEqual((stage.name, unreachable), ("撼山雾火", ()))
        client.akedata_public_json.assert_not_awaited()


class EndfieldStageCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_includes_registered_special_modes(self):
        client = AsyncMock()
        payloads = {
            "危境再现": {
                "articles": [
                    {
                        "title": "危境再现/罗丹",
                        "categories": ["危境再现"],
                        "currentRevisionId": "rodin-r1",
                        "updatedAt": "2026-07-21T00:00:00Z",
                    }
                ]
            },
            "能量淤积点": {
                "articles": [
                    {
                        "title": "能量淤积点·供能高地",
                        "categories": ["能量淤积点"],
                        "currentRevisionId": "energy-r1",
                        "updatedAt": "2026-07-23T00:00:00Z",
                    }
                ]
            },
            "协议空间": {
                "articles": [
                    {
                        "title": "协议空间·折金票",
                        "categories": ["协议空间"],
                        "currentRevisionId": "resource-r1",
                        "updatedAt": "2026-07-20T00:00:00Z",
                    }
                ]
            },
            "危机合约": {
                "articles": [
                    {
                        "title": "危机合约",
                        "categories": ["危机合约"],
                        "currentRevisionId": "contract-r1",
                        "updatedAt": "2026-07-22T00:00:00Z",
                    }
                ]
            },
            "战争回响": {
                "articles": [
                    {
                        "title": "活动/战争回响/谵妄赛季",
                        "categories": ["战争回响"],
                        "currentRevisionId": "war-r1",
                        "updatedAt": "2026-07-24T00:00:00Z",
                    }
                ]
            },
        }
        client.fz_articles.side_effect = lambda *, category, ns=0: payloads.get(category, {"articles": []})
        client.fz_article_by_title.return_value = _war_echo_fixture()
        catalog = await FZStageSource(client).catalog()
        names = [item.name for group in catalog.groups for item in group.items]
        self.assertEqual(names, ["罗丹", "供能高地", "折金票", "危机合约", "野性旧事"])
        self.assertEqual((catalog.queryable_count, catalog.pending_count), (5, 0))

    async def test_default_catalog_merges_sources_and_matches_keep_origin(self):
        version, tables = _akedata_fixture()
        akedata_catalog = parse_akedata_catalog(version, *tables[:3])
        fz_catalog = StageCatalogView(
            (
                StageCatalogGroup(
                    "boss_rush",
                    "危境再现",
                    (
                        StageCatalogItem(
                            "危境再现/罗丹",
                            "罗丹",
                            "boss_rush",
                            "危境再现",
                            "fz-r1",
                            "2026-07-24",
                            source="fz",
                        ),
                    ),
                ),
            ),
            "FZ Wiki",
            "fz-r1",
            "2026-07-24",
        )
        service = EndfieldStageService(AsyncMock())
        fz_source = AsyncMock()
        akedata_source = AsyncMock()
        fz_source.catalog.return_value = fz_catalog
        akedata_source.catalog.return_value = akedata_catalog
        service.sources = {"fz": fz_source, "akedata": akedata_source}
        merged = await service.get_catalog_view()
        match = next(
            item
            for item in await service.discover_matches("山中见犼 撼山雾火")
            if item.source == "akedata"
        )
        self.assertEqual((merged.source, len(merged.groups)), ("FZ Wiki、AkeData", 2))
        self.assertEqual((match.display_name, match.source), ("撼山雾火", "akedata"))


class EndfieldStageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await endfield._CARD_CACHE.clear()

    async def test_explicit_warfarin_stage_query_reports_unsupported(self):
        matcher = AsyncMock()
        command = commands.ParsedEndfieldCommand(
            "query", scope="stage", query="罗丹", source="warfarin"
        )
        await endfield._handle_command(matcher, None, command)
        matcher.finish.assert_awaited_once_with("Warfarin Wiki 暂不支持关卡资料。")

    async def test_stage_card_cache_key_includes_revision(self):
        renderer = AsyncMock(side_effect=(b"revision-one", b"revision-two"))
        first = commands.EndfieldCandidate(
            "stage_catalog", "", "关卡资料目录", 100, "fz", revision="r1"
        )
        second = commands.EndfieldCandidate(
            "stage_catalog", "", "关卡资料目录", 100, "fz", revision="r2"
        )
        with patch.dict(endfield.CONTENT_RENDERERS, {"stage_catalog": renderer}):
            self.assertEqual(await endfield._render_candidate(first, "fz"), (b"revision-one",))
            self.assertEqual(await endfield._render_candidate(first, "fz"), (b"revision-one",))
            self.assertEqual(await endfield._render_candidate(second, "fz"), (b"revision-two",))
        self.assertEqual(renderer.await_count, 2)

    async def test_auto_candidate_renders_from_its_discovered_source(self):
        candidate = commands.EndfieldCandidate(
            "stage",
            "indie_hard022",
            "撼山雾火",
            100,
            "akedata",
            revision="1.4.4@fixture-1",
        )
        renderer = AsyncMock(return_value=(b"akedata-stage", False))
        with patch.object(endfield, "_render_stage", renderer):
            self.assertEqual(await endfield._render_candidate(candidate), (b"akedata-stage",))
        renderer.assert_awaited_once_with(
            "indie_hard022",
            "akedata",
            mode="detail",
            selector="",
        )

    async def test_default_resolver_keeps_akedata_candidate_source(self):
        match = StageMatch(
            key="indie_hard022",
            title="indie_hard022",
            display_name="撼山雾火",
            query_text="撼山雾火",
            selector="",
            mode="detail",
            revision="1.4.4@fixture-1",
            updated_at="2026-07-27T00:00:00Z",
            queryable=True,
            source="akedata",
        )
        with patch.object(endfield.stage_service, "discover_matches", AsyncMock(return_value=(match,))):
            candidates = await endfield._resolve_stage_candidates("撼山雾火")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            (candidates[0].key, candidates[0].source, candidates[0].score),
            ("indie_hard022", "akedata", 100),
        )


class EndfieldStageResistanceSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_wave_enemies_get_resistances_from_their_own_article(self):
        client = AsyncMock()

        async def fetch(title, **kwargs):
            return _energy_fixture() if title.startswith("能量淤积点") else _enemy_article_fixture()

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, unreachable = await FZStageSource(client).stage("能量淤积点·供能高地")
        self.assertEqual(stage.variants[0].extension.waves[0].enemy.resistances[0].percent, 80.0)
        self.assertEqual(stage.variants[0].enemies[0].resistances[0].percent, 80.0)
        self.assertEqual(client.fz_article_by_title.await_count, 2)
        self.assertEqual(unreachable, ())

    async def test_enemy_article_failure_leaves_the_card_renderable(self):
        client = AsyncMock()

        async def fetch(title, **kwargs):
            if title.startswith("能量淤积点"):
                return _energy_fixture()
            raise WarfarinAPIError("FZ Wiki 请求超时")

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, unreachable = await FZStageSource(client).stage("能量淤积点·供能高地")
        self.assertIsNone(stage.variants[0].extension.waves[0].enemy.resistances)
        self.assertEqual(unreachable, ("敌人/碾骨先锋",))
        html = render_stage_card_html(
            StageCardView(stage, "detail", stage.variants[0], unreachable_enemies=unreachable)
        )
        # A failed fetch must not be reported as "the source does not publish this".
        self.assertIn("本次未能取得，稍后重试", html)
        self.assertNotIn("数据源暂未提供该项资料：敌人元素抗性", html)

    async def test_explicitly_empty_enemy_article_is_not_reported_as_missing(self):
        client = AsyncMock()
        empty = _enemy_article_fixture()
        for node in empty["revision"]["contentJson"]["content"]:
            if node["type"] == "endfieldCardEnemyResistances":
                node["attrs"]["rows"] = []

        async def fetch(title, **kwargs):
            return _energy_fixture() if title.startswith("能量淤积点") else empty

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, unreachable = await FZStageSource(client).stage("能量淤积点·供能高地")
        self.assertEqual(stage.variants[0].extension.waves[0].enemy.resistances, ())
        self.assertEqual(unreachable, ())
        html = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertIn("来源明确标注暂无元素抗性", html)
        self.assertNotIn("数据源暂未提供该项资料：敌人元素抗性", html)

    async def test_boss_reads_resistances_inline_and_fetches_only_for_knots(self):
        """Resistances and poise values are embedded; only the knots require the enemy article."""
        client = AsyncMock()

        async def fetch(title, **kwargs):
            return _shared_flavor_fixture() if title.startswith("危境再现") else _enemy_article_fixture()

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, unreachable = await FZStageSource(client).stage("危境再现/三位一体")
        boss = stage.variants[0].enemies[0]
        self.assertEqual(boss.article_title, "敌人/三位一体")
        self.assertEqual(boss.resistances[0].percent, 80.0)
        self.assertEqual(boss.poise.max_value, 280.0)
        self.assertEqual(boss.poise.knots, (0.25, 0.5, 0.75))
        self.assertEqual(unreachable, ())
        # one call for the stage, one for the single distinct enemy article
        self.assertEqual(client.fz_article_by_title.await_count, 2)

    async def test_boss_keeps_inline_values_when_the_enemy_article_is_unreachable(self):
        client = AsyncMock()

        async def fetch(title, **kwargs):
            if title.startswith("危境再现"):
                return _shared_flavor_fixture()
            raise WarfarinAPIError("FZ Wiki 请求超时")

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, unreachable = await FZStageSource(client).stage("危境再现/三位一体")
        boss = stage.variants[0].enemies[0]
        self.assertEqual(boss.resistances[0].percent, 80.0)
        self.assertEqual(boss.poise.max_value, 280.0)
        self.assertIsNone(boss.poise.knots)
        self.assertEqual(unreachable, ("敌人/三位一体",))

    async def test_enemy_article_with_no_knots_is_recorded_as_empty(self):
        client = AsyncMock()

        async def fetch(title, **kwargs):
            return _energy_fixture() if title.startswith("能量淤积点") else _enemy_article_fixture(knots=[])

        client.fz_article_by_title = AsyncMock(side_effect=fetch)
        stage, _ = await FZStageSource(client).stage("能量淤积点·供能高地")
        self.assertEqual(stage.variants[0].extension.waves[0].enemy.poise.knots, ())


class EndfieldStageDrawTests(unittest.TestCase):
    def test_detail_html_marks_unknown_fields_without_fabricating_zero(self):
        stage = parse_fz_stage(_energy_fixture(include_tables=False))
        html = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertIn("数据源暂未提供该项资料", html)
        self.assertNotIn("理智消耗</span><b>0", html)
        self.assertIn("revision-1", html)

    def test_detail_rail_lists_every_variant_and_marks_the_selected_one(self):
        stage = parse_fz_stage(_boss_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[-1]))
        for label in ("一级", "二级", "三级", "四级"):
            self.assertIn(f"<b>{label}</b>", detail)
        self.assertIn('<div class="variant-tab active"><b>四级</b>', detail)

    def test_detail_compares_variant_metrics_side_by_side(self):
        stage = parse_fz_stage(_boss_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[2]))
        self.assertIn("变体对比", detail)
        self.assertIn('<tr class="is-current"><th scope="row">三级</th>', detail)
        for hp in ("1,000", "2,000", "3,000", "4,000"):
            self.assertIn(f"<td>{hp}</td>", detail)

    def test_description_shared_with_the_stage_is_not_printed_twice(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertEqual(detail.count("极度危险的协议空间。"), 1)
        self.assertIn("“你将重逢毫无情感的毁灭。”", detail)

    def test_boss_icon_is_requested_and_rendered_when_available(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        self.assertIn("https://assets.fz.wiki/boss.png", _stage_icon_urls(stage))
        detail = render_stage_card_html(
            StageCardView(stage, "detail", stage.variants[0]),
            {"https://assets.fz.wiki/boss.png": "https://endfield.local/assets/boss"},
        )
        self.assertIn('<img class="hero-icon" src="https://endfield.local/assets/boss"', detail)

    def test_boss_card_shows_visible_elements_the_fz_way(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        for label in ("物理", "灼热", "电磁", "寒冷", "自然"):
            self.assertIn(f"<span>{label}</span>", detail)
        self.assertIn("<b>抗 20%</b>", detail)
        self.assertIn("<em>受伤 80%</em>", detail)
        self.assertIn("<b>标准</b>", detail)
        self.assertIn("<em>受伤 100%</em>", detail)

    def test_ether_resistance_is_hidden_everywhere(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertNotIn("超域", detail)
        self.assertEqual(detail.count('class="resist-cell'), 5)

        energy = parse_fz_stage(_energy_fixture())
        waves = tuple(
            replace(wave, enemy=replace(wave.enemy, resistances=_resistance_models()))
            for wave in energy.variants[0].extension.waves
        )
        variant = replace(
            energy.variants[0], extension=replace(energy.variants[0].extension, waves=waves)
        )
        matrix = render_stage_card_html(
            StageCardView(replace(energy, variants=(variant,)), "detail", variant)
        )
        self.assertNotIn("超域", matrix)
        self.assertEqual(matrix.count("<th style="), 5)

    def test_percent_is_never_relabelled_as_resistance(self):
        """FZ publishes damage taken; calling 80% a resistance would invert the meaning."""
        stage = parse_fz_stage(_shared_flavor_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertNotIn("抗性 80%", detail)
        self.assertNotIn("抗 80%", detail)

    def test_element_colour_from_source_is_sanitised(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        poisoned = replace(
            stage.variants[0].enemies[0].resistances[0], color="red;background:url(x)"
        )
        enemy = replace(stage.variants[0].enemies[0], resistances=(poisoned,))
        variant = replace(stage.variants[0], enemies=(enemy,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertNotIn("background:url", detail)
        self.assertIn("--el:#8a9296", detail)

    def test_boss_card_shows_poise_values_and_knots(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        boss = replace(
            stage.variants[0].enemies[0],
            poise=StageEnemyPoise(
                max_value=280.0, damage_scalar=1.75, recover_seconds=11.0, knots=(0.25, 0.5, 0.75)
            ),
        )
        variant = replace(stage.variants[0], enemies=(boss,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("<span>失衡值上限</span><b>280</b>", detail)
        self.assertIn("<span>处决承伤系数</span><b>1.75</b>", detail)
        self.assertIn("<span>失衡恢复时间</span><b>11s</b>", detail)
        self.assertIn("<span>失衡节点</span><b>25% · 50% · 75%</b>", detail)

    def test_enemy_without_knots_says_none_rather_than_omitting(self):
        """[] means the source says this enemy has no knots; None means it never said."""
        stage = parse_fz_stage(_shared_flavor_fixture())
        boss = replace(
            stage.variants[0].enemies[0], poise=StageEnemyPoise(max_value=80.0, knots=())
        )
        variant = replace(stage.variants[0], enemies=(boss,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("<span>失衡节点</span><b>无</b>", detail)

        silent = replace(stage.variants[0].enemies[0], poise=StageEnemyPoise(max_value=80.0))
        quiet_variant = replace(stage.variants[0], enemies=(silent,))
        quiet = render_stage_card_html(
            StageCardView(replace(stage, variants=(quiet_variant,)), "detail", quiet_variant)
        )
        self.assertNotIn("失衡节点", quiet)

    def test_multi_enemy_stage_gets_a_poise_table(self):
        stage = parse_fz_stage(_energy_fixture())
        base = stage.variants[0].extension.waves[0]
        waves = (
            replace(
                base,
                enemy=replace(
                    base.enemy,
                    name="甲",
                    poise=StageEnemyPoise(max_value=320.0, damage_scalar=1.5, knots=(0.5,)),
                ),
            ),
            replace(base, enemy=replace(base.enemy, enemy_id="e2", name="乙", poise=None)),
        )
        variant = replace(
            stage.variants[0], extension=replace(stage.variants[0].extension, waves=waves)
        )
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("敌人失衡", detail)
        self.assertIn("节点为失衡条上的刻度位置", detail)
        self.assertIn("<td>320</td>", detail)
        self.assertIn("<td>50%</td>", detail)
        self.assertIn("1 个敌人数据源暂未提供", detail)

    def test_vulnerable_and_unknown_rows_render_without_inverting_meaning(self):
        """percent > 100 is damage taken above normal, i.e. a weakness — never a resistance."""
        rows = (
            StageEnemyResistance("Fire", "灼热", percent=120.0, scalar=1.2, color="FF623D"),
            StageEnemyResistance("Cryst", "寒冷", percent=None, scalar=None, color="21C6D0"),
        )
        stage = parse_fz_stage(_shared_flavor_fixture())
        enemy = replace(stage.variants[0].enemies[0], resistances=rows)
        variant = replace(stage.variants[0], enemies=(enemy,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("<b>易伤 20%</b>", detail)
        self.assertIn("<em>受伤 120%</em>", detail)
        self.assertNotIn("抗 20%", detail)
        self.assertIn("<b>未提供</b>", detail)
        self.assertIn("<em>受伤未提供</em>", detail)

    def test_matrix_columns_join_on_element_code_not_label(self):
        stage = parse_fz_stage(_energy_fixture())
        base = stage.variants[0].extension.waves[0]
        labelled = StageEnemyResistance("Physical", "物理", percent=80.0, scalar=0.8, color="888888")
        unlabelled = StageEnemyResistance("Physical", "Physical", percent=100.0, scalar=1.0, color="888888")
        waves = (
            replace(base, enemy=replace(base.enemy, name="甲", resistances=(labelled,))),
            replace(base, enemy=replace(base.enemy, enemy_id="e2", name="乙", resistances=(unlabelled,))),
        )
        variant = replace(
            stage.variants[0], extension=replace(stage.variants[0].extension, waves=waves)
        )
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertEqual(detail.count("<th style="), 1)
        self.assertNotIn('class="resist-none"', detail)

    def test_boss_with_explicitly_empty_resistances_says_so(self):
        """() means the source published no rows; None means we never got an answer."""
        stage = parse_fz_stage(_shared_flavor_fixture())
        empty = replace(stage.variants[0].enemies[0], resistances=())
        variant = replace(stage.variants[0], enemies=(empty,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("来源明确标注暂无元素抗性", detail)
        self.assertNotIn("数据源暂未提供该项资料：敌人元素抗性", detail)

    def test_boss_with_absent_resistances_is_marked_unprovided(self):
        stage = parse_fz_stage(_shared_flavor_fixture())
        absent = replace(stage.variants[0].enemies[0], resistances=None)
        variant = replace(stage.variants[0], enemies=(absent,))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("数据源暂未提供该项资料：敌人元素抗性", detail)
        self.assertNotIn("来源明确标注暂无元素抗性", detail)

    def test_matrix_note_separates_empty_from_unprovided(self):
        stage = parse_fz_stage(_energy_fixture())
        base = stage.variants[0].extension.waves[0]
        waves = (
            replace(base, enemy=replace(base.enemy, name="有抗性", resistances=_resistance_models())),
            replace(base, enemy=replace(base.enemy, enemy_id="e2", name="来源为空", resistances=())),
            replace(base, enemy=replace(base.enemy, enemy_id="e3", name="未提供", resistances=None)),
        )
        variant = replace(
            stage.variants[0], extension=replace(stage.variants[0].extension, waves=waves)
        )
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("1 个敌人来源标注暂无抗性", detail)
        self.assertIn("1 个敌人数据源暂未提供", detail)

    def test_overview_renders_the_matrix_and_never_doubles_the_strip(self):
        multi = parse_fz_stage(_energy_fixture())
        waves = tuple(
            replace(wave, enemy=replace(wave.enemy, resistances=_resistance_models()))
            for wave in multi.variants[0].extension.waves
        )
        variant = replace(
            multi.variants[0], extension=replace(multi.variants[0].extension, waves=waves)
        )
        overview = render_stage_card_html(
            StageCardView(replace(multi, variants=(variant,)), "overview")
        )
        self.assertIn("敌人元素抗性", overview)
        self.assertEqual(overview.count('class="resist-table"'), 1)

        solo = parse_fz_stage(_shared_flavor_fixture())
        single = replace(solo, variants=(solo.variants[0],))
        card = render_stage_card_html(StageCardView(single, "overview"))
        self.assertEqual(card.count('class="resist-strip"'), 1)

    def test_resistances_appear_once_when_a_stage_has_several_bosses(self):
        """A lone boss shows a strip; more than one falls back to the matrix so nothing renders twice."""
        stage = parse_fz_stage(_shared_flavor_fixture())
        boss = stage.variants[0].enemies[0]
        variant = replace(stage.variants[0], enemies=(boss, replace(boss, name="第二头目")))
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertEqual(detail.count('class="resist-strip"'), 0)
        self.assertEqual(detail.count('class="resist-table"'), 1)
        self.assertIn('<th scope="row">第二头目</th>', detail)

        solo = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertEqual(solo.count('class="resist-strip"'), 1)
        self.assertEqual(solo.count('class="resist-table"'), 0)

    def test_multi_enemy_stage_gets_a_resistance_matrix(self):
        stage = parse_fz_stage(_energy_fixture())
        rows = tuple(
            replace(wave, enemy=replace(wave.enemy, resistances=_resistance_models()))
            for wave in stage.variants[0].extension.waves
        )
        extension = replace(stage.variants[0].extension, waves=rows)
        variant = replace(stage.variants[0], extension=extension)
        detail = render_stage_card_html(
            StageCardView(replace(stage, variants=(variant,)), "detail", variant)
        )
        self.assertIn("敌人元素抗性", detail)
        self.assertIn("数值为受到该元素伤害的比例", detail)
        self.assertIn('<th scope="row">碾骨先锋</th>', detail)
        self.assertIn('class="resist-lo"', detail)
        self.assertIn('class="resist-std"', detail)

    def test_energy_waves_are_grouped_by_wave(self):
        stage = parse_fz_stage(_energy_fixture())
        detail = render_stage_card_html(StageCardView(stage, "detail", stage.variants[0]))
        self.assertIn("第 1 波", detail)
        self.assertIn("立即", detail)
        self.assertIn("×2", detail)

    def test_overview_and_catalog_html(self):
        stage = parse_fz_stage(_boss_fixture())
        overview = render_stage_card_html(StageCardView(stage, "overview"))
        self.assertIn("一级", overview)
        self.assertIn("四级", overview)
        self.assertIn("各变体说明", overview)
        catalog = StageCatalogView(
            (
                StageCatalogGroup(
                    "resource",
                    "资源副本",
                    (
                        StageCatalogItem(
                            "副本/资源",
                            "资源副本",
                            "resource",
                            "资源副本",
                            "r1",
                            "2026-07-24",
                            queryable=False,
                        ),
                    ),
                ),
            ),
            "FZ Wiki",
            "catalog-r1",
            "2026-07-24",
        )
        catalog_html = render_stage_catalog_html(catalog)
        self.assertIn("资料待完善", catalog_html)
        self.assertIn("catalog-r1", catalog_html)


def _big_catalog(groups: int, per_group: int) -> StageCatalogView:
    return StageCatalogView(
        groups=tuple(
            StageCatalogGroup(
                f"family-{group}",
                f"玩法族 {group}",
                tuple(
                    StageCatalogItem(
                        f"副本/关卡 {group}-{index}",
                        f"关卡 {group}-{index}",
                        f"family-{group}",
                        f"玩法族 {group}",
                        "r1",
                        "2026-07-27",
                        queryable=index % 5 != 0,
                    )
                    for index in range(per_group)
                ),
            )
            for group in range(groups)
        ),
        source="FZ Wiki",
        revision="catalog-r1",
        updated_at="2026-07-27",
    )


class EndfieldStageCatalogPaginationTests(unittest.IsolatedAsyncioTestCase):
    def test_pages_stay_within_budget_and_keep_every_item(self):
        catalog = _big_catalog(4, 30)

        pages = stage_draw._paginate_catalog(catalog, 25)

        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(sum(len(group.items) for group in page.groups), 25)
        names = [item.name for page in pages for group in page.groups for item in group.items]
        self.assertEqual(names, [item.name for group in catalog.groups for item in group.items])

    def test_every_page_reports_whole_catalog_totals(self):
        catalog = _big_catalog(4, 30)

        pages = stage_draw._paginate_catalog(catalog, 25)

        for index, page in enumerate(pages, start=1):
            self.assertEqual(page.family_count, 4)
            self.assertEqual(page.queryable_count, catalog.queryable_count)
            self.assertEqual(page.pending_count, catalog.pending_count)
            self.assertEqual((page.page_number, page.page_count), (index, len(pages)))

    def test_a_family_split_across_pages_is_marked_continued(self):
        pages = stage_draw._paginate_catalog(_big_catalog(1, 60), 25)

        self.assertEqual(pages[0].groups[0].name, "玩法族 0")
        self.assertTrue(all(group.name.endswith("（续）") for group in pages[1].groups))

    def test_page_marker_only_appears_once_paginated(self):
        single = render_stage_catalog_html(_big_catalog(1, 4))
        self.assertNotIn("第 1 / ", single)

        pages = stage_draw._paginate_catalog(_big_catalog(1, 60), 25)
        self.assertIn(f"第 1 / {len(pages)} 张", render_stage_catalog_html(pages[0]))

    async def test_one_image_while_the_catalog_fits(self):
        renderer = AsyncMock(return_value=b"single")

        with patch.object(stage_draw, "draw_stage_catalog_card", renderer):
            pages = await stage_draw.draw_stage_catalog_cards(_big_catalog(2, 5))

        self.assertEqual(pages, (b"single",))
        self.assertEqual(renderer.await_count, 1)

    async def test_splits_only_after_a_height_overflow(self):
        renderer = AsyncMock(side_effect=[
            RuntimeError("Screenshot element height 14007px exceeds limit 12000px"),
            b"page-1",
            b"page-2",
        ])

        with patch.object(stage_draw, "draw_stage_catalog_card", renderer):
            pages = await stage_draw.draw_stage_catalog_cards(_big_catalog(2, 120))

        self.assertEqual(pages, (b"page-1", b"page-2"))
        self.assertEqual(renderer.await_args_list[1].args[0].page_count, 2)

    async def test_a_non_height_render_error_is_not_swallowed(self):
        renderer = AsyncMock(side_effect=RuntimeError("browser crashed"))

        with patch.object(stage_draw, "draw_stage_catalog_card", renderer):
            with self.assertRaisesRegex(RuntimeError, "browser crashed"):
                await stage_draw.draw_stage_catalog_cards(_big_catalog(2, 5))

        self.assertEqual(renderer.await_count, 1)

    async def test_a_budget_that_still_overflows_falls_through_to_a_smaller_one(self):
        overflow = RuntimeError("Screenshot element height 14007px exceeds limit 12000px")
        renderer = AsyncMock(side_effect=[overflow, overflow, overflow, b"a", b"b", b"c", b"d"])

        with patch.object(stage_draw, "draw_stage_catalog_card", renderer):
            pages = await stage_draw.draw_stage_catalog_cards(_big_catalog(1, 160))

        self.assertEqual(pages, (b"a", b"b", b"c", b"d"))


if __name__ == "__main__":
    unittest.main()
