from __future__ import annotations

from fractions import Fraction
from hashlib import md5
from dataclasses import replace
import unittest

from plugins.endfield.account.detail.names import AccountDetailNameMap
from plugins.endfield.account.investment.draw import _render_detail_html, _render_summary_html
from plugins.endfield.account.investment.models import InvestmentResourceView
from plugins.endfield.account.investment.service import (
    InvestmentCatalog,
    _CharacterSpec,
    _Cost,
    _ItemMeta,
    _NodeCost,
    _WeaponSpec,
    _build_resource_rates,
    _build_characters,
    _build_weapons,
    build_account_investment_view,
)
from plugins.endfield.catalog.commands import format_help, parse_command


def _catalog() -> InvestmentCatalog:
    char = _CharacterSpec(
        char_id="char_a",
        break_nodes={
            "charBreak20": _NodeCost(
                "charBreak20", node_type=1, break_stage=1,
                cost=_Cost(resources={"mat_break": 2}, gold=10),
            ),
        },
        nodes={
            "attr_1": _NodeCost(
                "attr_1", node_type=3, index=0,
                cost=_Cost(resources={"mat_attr": 1}),
            ),
            "passive_1": _NodeCost(
                "passive_1", node_type=4, index=0, level=1,
                cost=_Cost(resources={"mat_passive": 2}),
            ),
            "passive_2": _NodeCost(
                "passive_2", node_type=4, index=0, level=2,
                cost=_Cost(resources={"mat_passive": 3}),
            ),
            "equipBreakT2": _NodeCost(
                "equipBreakT2", node_type=2, break_stage=1,
                cost=_Cost(resources={"mat_equip": 4}),
            ),
            "equipBreakT3": _NodeCost(
                "equipBreakT3", node_type=2, break_stage=2,
                cost=_Cost(resources={"mat_equip": 7}),
            ),
        },
        skill_groups={"skill_a": "group_a"},
        skill_rows={
            "group_a": (
                (2, _Cost(resources={"mat_skill": 2}, gold=5)),
                (3, _Cost(resources={"mat_skill": 3}, gold=8)),
            ),
        },
    )
    weapon = _WeaponSpec(
        weapon_id="weapon_a",
        level_costs={1: (0, 0), 2: (10, 1), 5: (50, 5)},
        breakthrough_costs=((1, _Cost(resources={"mat_weapon": 6}, gold=2)),),
    )
    item_ids = {
        "item_gold": "折金票",
        "mat_break": "突破材料",
        "mat_attr": "属性材料",
        "mat_passive": "被动材料",
        "mat_equip": "装备阶级材料",
        "mat_skill": "技能材料",
        "mat_weapon": "武器材料",
    }
    return InvestmentCatalog(
        version="test-revision",
        characters={"char_a": char},
        weapons={"weapon_a": weapon},
        items={key: _ItemMeta(key, value) for key, value in item_ids.items()},
        char_level_costs={1: (10, 1), 2: (20, 2)},
        resource_rates={
            "mat_break": Fraction(1),
            "mat_attr": Fraction(1),
            "mat_passive": Fraction(1),
            "mat_equip": Fraction(1),
            "mat_skill": Fraction(1),
            "mat_weapon": Fraction(1),
            "item_gold": Fraction(1, 100),
        },
        character_exp_rate=Fraction(1, 10),
        weapon_exp_rate=Fraction(1, 10),
        gold_rate=Fraction(1, 100),
    )


class InvestmentCommandTests(unittest.TestCase):
    def test_parser_supports_primary_aliases_and_selectors(self):
        for alias in ("养成统计", "资源消耗", "养成消耗"):
            parsed = parse_command(f"{alias} 小明")
            self.assertEqual(parsed.action, "account_investment")
            self.assertEqual(parsed.account_selector, "小明")
        self.assertEqual(parse_command("养成统计").account_selector, "")
        self.assertIn("/ef 养成统计 [编号]", format_help())


class InvestmentCalculationTests(unittest.TestCase):
    def _detail(self):
        return {
            "base": {"name": "测试账号", "saveTime": 0},
            "chars": [
                {
                    "charData": {"id": "char_a", "name": "测试干员", "rarity": 6},
                    "level": 3,
                    "evolvePhase": 1,
                    "userSkills": {"skill_a": {"skillId": "skill_a", "level": 3}},
                    "talent": {
                        "attrNodes": ["attr_1", "charBreak20"],
                        "latestPassiveSkillNodes": ["passive_2"],
                        "latestBreakNode": "equipBreakT2",
                    },
                    "weapon": {
                        "level": 5,
                        "breakthroughLevel": 1,
                        "weaponData": {"id": "weapon_a", "name": "测试武器"},
                    },
                },
            ],
        }

    def test_level_break_skill_nodes_and_weapon_are_accumulated_once(self):
        view = build_account_investment_view(
            self._detail(), uid="****1234", catalog=_catalog()
        )
        self.assertEqual(view.operator_count, 1)
        self.assertEqual(view.equipped_weapon_count, 1)
        self.assertEqual(view.character_exp, 30)
        self.assertEqual(view.weapon_exp, 50)
        self.assertEqual(view.resources[0].item_id, "item_gold")
        resource_counts = {item.item_id: item.count for item in view.resources}
        self.assertEqual(resource_counts["mat_break"], 2)
        self.assertEqual(resource_counts["mat_passive"], 5)
        self.assertEqual(resource_counts["mat_equip"], 4)
        self.assertEqual(resource_counts["mat_skill"], 5)
        self.assertEqual(resource_counts["mat_weapon"], 6)
        self.assertTrue(view.coverage_label.startswith("数据覆盖 "))
        self.assertTrue(view.complete)
        self.assertEqual(view.missing, ())
        self.assertEqual(view.contributions[0].name, "测试干员")
        self.assertGreater(view.contributions[0].total_stamina, 0)

    def test_localizes_account_identity_and_prefers_akedata_character_name(self):
        detail = self._detail()
        detail["base"]["name"] = {"zh": "中文账号", "en": "English Account"}
        detail["chars"][0]["charData"]["name"] = {
            "zh": "接口中文干员",
            "en": "English Operator",
        }
        name_map = AccountDetailNameMap(character_names={"char_a": "AKEData中文干员"})

        view = build_account_investment_view(
            detail,
            uid="****1234",
            server_name={"en": "China"},
            catalog=_catalog(),
            name_map=name_map,
        )

        self.assertEqual(view.nickname, "中文账号")
        self.assertEqual(view.server_name, "国服")
        self.assertEqual(view.contributions[0].name, "AKEData中文干员")

    def test_missing_character_static_row_is_a_lower_bound(self):
        detail = {"base": {}, "chars": [{"charData": {"id": "new_char", "name": "新干员"}, "level": 20}]}
        view = build_account_investment_view(detail, uid="1", catalog=_catalog())
        self.assertFalse(view.complete)
        self.assertEqual(view.total_label, "已知投入至少")
        self.assertTrue(any("干员成长表" in item for item in view.missing))

    def test_same_weapon_instances_are_counted_independently(self):
        detail = self._detail()
        detail["chars"].append(detail["chars"][0].copy())
        view = build_account_investment_view(detail, uid="1", catalog=_catalog())
        self.assertEqual(view.operator_count, 2)
        self.assertEqual(view.equipped_weapon_count, 2)
        self.assertEqual(view.character_exp, 60)
        self.assertEqual(view.weapon_exp, 100)
        self.assertEqual(len(view.contributions), 2)

    def test_static_ids_expose_md5_aliases_used_by_account_payload(self):
        characters = _build_characters(
            {
                "char_a": {
                    "charId": "char_a",
                    "talentNodeMap": {
                        "attr_1": {
                            "nodeId": "attr_1",
                            "nodeType": 3,
                            "requiredItem": [],
                        }
                    },
                    "skillGroupMap": {
                        "group_a": {
                            "skillGroupId": "group_a",
                            "skillIdList": ["skill_a"],
                        }
                    },
                    "skillLevelUp": [
                        {"skillGroupId": "group_a", "level": 2, "itemBundle": []}
                    ],
                }
            }
        )
        weapons = _build_weapons(
            {
                "weapon_a": {
                    "weaponId": "weapon_a",
                    "levelTemplateId": "level_a",
                    "breakthroughTemplateId": "break_a",
                }
            },
            {"level_a": {"list": [{"weaponLv": 1, "lvUpExp": 0, "lvUpGold": 0}]}},
            {},
            {"break_a": {"list": []}},
        )
        self.assertIs(
            characters[md5(b"char_a").hexdigest()], characters["char_a"]
        )
        spec = characters["char_a"]
        self.assertEqual(spec.skill_groups[md5(b"skill_a").hexdigest()], "group_a")
        self.assertIs(spec.nodes[md5(b"attr_1").hexdigest()], spec.nodes["attr_1"])
        self.assertIs(weapons[md5(b"weapon_a").hexdigest()], weapons["weapon_a"])

    def test_static_chinese_name_precedes_account_english_fallback(self):
        characters = _build_characters(
            {
                "char_a": {
                    "charId": "char_a",
                    "name": {"id": "name_a", "text": ""},
                    "engName": "English Name",
                }
            },
            translations={"name_a": "中文干员"},
        )
        self.assertEqual(characters["char_a"].name, "中文干员")

    def test_default_talent_placeholders_are_not_missing_costs(self):
        detail = {
            "base": {},
            "chars": [
                {
                    "charData": {"id": "char_a", "name": "English Name"},
                    "level": 1,
                    "evolvePhase": 0,
                    "talent": {
                        "latestPassiveSkillNodes": ["default_passive_skill_a"],
                        "latestFactorySkillNodes": ["default_factory_skill_a"],
                        "latestSpaceshipSkillNodes": [],
                        "latestBreakNode": "default_break_stage",
                    },
                    "weapon": {
                        "level": 1,
                        "breakthroughLevel": 0,
                        "weaponData": {"id": "weapon_a"},
                    },
                }
            ],
        }
        view = build_account_investment_view(detail, uid="1", catalog=_catalog())
        self.assertTrue(view.complete)
        self.assertEqual(view.missing, ())

    def test_equivalent_character_variants_share_node_and_skill_ids(self):
        def row(char_id: str) -> dict:
            group_id = f"{char_id}_NormalSkill"
            return {
                "charId": char_id,
                "engName": "Endministrator",
                "defaultWeaponId": "wpn_sword_0003",
                "charTypeId": "Physical",
                "rarity": 6,
                "weaponType": 1,
                "talentNodeMap": {
                    f"{char_id}_1": {
                        "nodeId": f"{char_id}_1",
                        "nodeType": 3,
                        "requiredItem": [{"id": "mat_attr", "count": 1}],
                    },
                    f"{char_id}_passive_skill_0_1": {
                        "nodeId": f"{char_id}_passive_skill_0_1",
                        "nodeType": 4,
                        "passiveSkillNodeInfo": {"index": 0, "level": 1},
                        "requiredItem": [{"id": "mat_passive", "count": 1}],
                    },
                },
                "skillGroupMap": {
                    group_id: {
                        "skillGroupId": group_id,
                        "skillIdList": [f"{char_id}_normal_skill"],
                    }
                },
                "skillLevelUp": [
                    {
                        "skillGroupId": group_id,
                        "level": 2,
                        "itemBundle": [{"id": "mat_skill", "count": 1}],
                    }
                ],
            }

        characters = _build_characters(
            {
                "chr_0003_endminf": row("chr_0003_endminf"),
                "chr_9000_endmin": row("chr_9000_endmin"),
            }
        )
        selected = characters["chr_9000_endmin"]
        self.assertEqual(
            selected.nodes["chr_0003_endminf_1"].node_id,
            "chr_9000_endmin_1",
        )
        self.assertEqual(
            selected.skill_groups[md5(b"chr_0003_endminf_normal_skill").hexdigest()],
            "chr_9000_endmin_NormalSkill",
        )

    def test_stamina_rates_choose_best_stage_and_safe_recipe_only(self):
        rates, char_rate, weapon_rate, gold_rate = _build_resource_rates(
            {
                "slow": {"dungeonCategory": "dungeon_resource", "costStamina": 10, "rewardId": "slow_reward"},
                "fast": {"dungeonCategory": "dungeon_resource", "costStamina": 8, "rewardId": "fast_reward"},
            },
            {
                "slow_reward": {"itemBundles": [{"id": "farm", "count": 1}, {"id": "item_expcard_stage1_low", "count": 1}]},
                "fast_reward": {"itemBundles": [{"id": "farm", "count": 2}, {"id": "item_expcard_stage2_high", "count": 1}, {"id": "item_gold", "count": 4}]},
            },
            {
                "item_expcard_stage1_low": {"expGain": 100, "expType": 0},
                "item_expcard_stage2_high": {"expGain": 200, "expType": 2},
            },
            {
                "safe": {"ingredients": [{"id": "farm", "count": 2}], "outcomes": [{"id": "crafted", "count": 1}]},
                "cycle": {"ingredients": [{"id": "cycle", "count": 1}], "outcomes": [{"id": "cycle", "count": 1}]},
                "paid": {"ingredients": [{"id": "farm", "count": 1}, {"id": "item_ap", "count": 1}], "outcomes": [{"id": "paid_out", "count": 1}]},
            },
        )
        self.assertEqual(rates["farm"], Fraction(4))
        self.assertEqual(rates["crafted"], Fraction(8))
        self.assertNotIn("cycle", rates)
        self.assertNotIn("paid_out", rates)
        self.assertEqual(char_rate, Fraction(1, 10))
        self.assertEqual(weapon_rate, Fraction(1, 25))
        self.assertEqual(gold_rate, Fraction(2))

    def test_protocol_space_fixed_reward_maps_six_count_special_material(self):
        rates, *_ = _build_resource_rates(
            {
                "dung_ss01": {
                    "dungeonCategory": "dungeon_ss",
                    "dungeonSeriesId": "dung_group_ss01",
                    "costStamina": 0,
                    "rewardId": "",
                    "hunterModeCostStamina": 80,
                    "hunterModeRewardId": "reward_dung_ss01",
                }
            },
            {
                "reward_dung_ss01": {
                    "itemBundles": [
                        {"id": "item_adventureexp", "count": 480},
                        {"id": "item_char_skill_specialize_1", "count": 0},
                    ],
                    "probItemBundles": [
                        {"id": "item_char_skill_specialize_4", "count": 0},
                    ],
                }
            },
            {},
            {},
            {
                "dung_group_ss01": {
                    "gameCategory": "dungeon_ss",
                    "dungeonImg": "item_char_skill_specialize_1",
                    "staminaText": {"text": "90"},
                }
            },
        )
        # DungeonTable.hunterModeCostStamina=80 is the repeatable reward-mode
        # cost; DungeonSeriesTable.staminaText is only a display tag (90 in
        # the fixture).  The fixed reward is six copies per run: 80 / 6.
        self.assertEqual(rates["item_char_skill_specialize_1"], Fraction(40, 3))
        self.assertNotIn("item_char_skill_specialize_4", rates)

    def test_spaceship_skill_alias_uses_factory_node_cost(self):
        characters = _build_characters(
            {
                "chr_0004_pelica": {
                    "charId": "chr_0004_pelica",
                    "talentNodeMap": {
                        "fac_chr_0004_pelica_0_1": {
                            "nodeId": "fac_chr_0004_pelica_0_1",
                            "nodeType": 5,
                            "factorySkillNodeInfo": {"breakStage": 1, "index": 0, "level": 1},
                            "requiredItem": [{"id": "mat_factory", "count": 6}],
                        }
                    },
                }
            }
        )
        spec = characters["chr_0004_pelica"]
        spaceship_id = "spaceship_skill_chr_0004_pelica_1_1"
        self.assertIs(spec.nodes[spaceship_id], spec.nodes["fac_chr_0004_pelica_0_1"])
        self.assertIs(spec.nodes[md5(spaceship_id.encode()).hexdigest()], spec.nodes[spaceship_id])


class InvestmentRenderTests(unittest.TestCase):
    def test_resource_tile_shows_per_item_rate(self):
        view = build_account_investment_view(
            {"base": {"name": "璐﹀彿"}, "chars": []}, uid="****1234", catalog=_catalog()
        )
        view = replace(
            view,
            resources=(
                InvestmentResourceView(
                    "item_char_skill_specialize_1",
                    "超距辉映管",
                    6,
                    stamina_cost=40 / 3,
                ),
            ),
        )
        detail = _render_detail_html(view, {})
        self.assertIn("13.3 理智/个", detail)
        self.assertNotIn("80.0 理智/份", detail)

    def test_both_cards_keep_materials_and_coverage_note(self):
        view = build_account_investment_view(
            {"base": {"name": "账号"}, "chars": []}, uid="****1234", catalog=_catalog()
        )
        summary = _render_summary_html(view, {})
        detail = _render_detail_html(view, {})
        self.assertIn("账号养成统计", summary)
        self.assertIn("材料明细 / 干员排行", detail)
        self.assertIn("当前档案没有可展示的资源投入", detail)
        self.assertIn("当前档案可见养成投入", summary)
