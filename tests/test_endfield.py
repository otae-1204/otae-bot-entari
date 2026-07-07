from __future__ import annotations

import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_endfield_module(module_name: str):
    pkg_name = "endfield_for_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[pkg_name] = pkg
    _load_module(f"{pkg_name}.models", "plugins/endfield/models.py")
    _load_module(f"{pkg_name}.client", "plugins/endfield/client.py")
    if module_name == "draw":
        return _load_module(f"{pkg_name}.draw", "plugins/endfield/draw.py")
    if module_name == "service":
        return _load_module(f"{pkg_name}.service", "plugins/endfield/service.py")
    raise ValueError(module_name)


draw = _load_endfield_module("draw")
service = _load_endfield_module("service")
models = sys.modules["endfield_for_test.models"]

render_operator_card_html = draw.render_operator_card_html
render_weapon_card_html = draw.render_weapon_card_html
LEVEL_COLUMNS = models.LEVEL_COLUMNS
build_operator_view = service.build_operator_view
build_weapon_view = service.build_weapon_view
clean_text = service.clean_text


def _sample_operator(levels: tuple[int, ...] = (9, 10, 11, 12)):
    def records(skill_id: str, names: list[str], values_for_level, *, cost: int = 0, cooldown: int = 0, blackboard=None):
        result = []
        for level in levels:
            result.append(
                {
                    "level": level,
                    "skillId": skill_id,
                    "iconId": "icon_skill_chen_01",
                    "skillName": "",
                    "description": "造成<#ba.damage>物理伤害</>。",
                    "coolDown": cooldown,
                    "costValue": cost,
                    "maxChargeTime": 1,
                    "subDescNameList": names,
                    "subDescList": values_for_level(level),
                    "blackboard": blackboard or [],
                }
            )
        return result

    normal_attack_records = records(
        "chr_0005_chen_attack5",
        ["普攻第五段倍率"],
        lambda level: [f"{level * 6}%"],
        blackboard=[{"key": "poise", "value": 16}],
    )
    execute_attack_records = records(
        "chr_0005_chen_power_attack",
        ["处决攻击倍率"],
        lambda level: [f"{level * 60}%"],
    )
    plunging_attack_records = records(
        "chr_0005_chen_plunging_attack_end",
        ["下落攻击倍率"],
        lambda level: [f"{level * 12}%"],
    )
    skill_records = records(
        "chr_0005_chen_normal_skill",
        ["伤害倍率", "失衡值"],
        lambda level: [f"{level * 10}%", str(level)],
        cost=100,
    )
    ultimate_records = records(
        "chr_0005_chen_ultimate_skill",
        ["斩击伤害倍率", "终结一击伤害倍率"],
        lambda level: [f"{level * 5}%", f"{level * 80}%"],
        cost=70,
        cooldown=10,
    )
    combo_records = records(
        "chr_0005_chen_combo_skill",
        ["伤害倍率", "获得终结技能量"],
        lambda level: [f"{level * 8}%", "10"],
        cooldown=16,
    )
    return {
        "meta": {
            "id": "chr_0005_chen",
            "slug": "chen-qianyu",
            "name": "陈千语",
            "version": "1.3",
        },
        "data": {
            "characterTable": {
                "charId": "chr_0005_chen",
                "name": "陈千语",
                "engName": "Chen Qianyu",
                "rarity": 5,
                "profession": 0,
                "weaponType": 1,
                "charTypeId": "Physical",
                "department": "ENDFIELD INDUSTRIES",
                "charBattleTagIds": ["tag_03"],
                "profileRecord": [{"recordDesc": "【种族】龙"}],
            },
            "itemTable": {
                "id": "chr_0005_chen",
                "rarity": 5,
                "desc": "陈千语是一名使用单手剑的近卫干员，可造成物理属性的伤害。",
            },
            "charGrowthTable": {
                "skillGroupMap": {
                    "chr_0005_chen_NormalAttack": {
                        "name": "破飞霞",
                        "desc": "普通攻击：对敌人进行至多5段攻击，造成物理伤害。作为主控干员时，重击会造成{poise:0}点失衡。",
                        "icon": "icon_attack_sword",
                        "skillGroupType": 0,
                        "skillIdList": [
                            "chr_0005_chen_attack5",
                            "chr_0005_chen_power_attack",
                            "chr_0005_chen_plunging_attack_end",
                        ],
                    },
                    "chr_0005_chen_NormalSkill": {
                        "name": "归穹宇",
                        "desc": "对目标敌人进行上挑攻击，造成物理伤害和击飞。",
                        "icon": "icon_skill_chen_01",
                        "skillGroupType": 1,
                        "skillIdList": ["chr_0005_chen_normal_skill"],
                    },
                    "chr_0005_chen_UltimateSkill": {
                        "name": "冽风霜",
                        "desc": "对目标敌人进行7段斩击，每次造成物理伤害。",
                        "icon": "icon_ultimate_skill_chen_01",
                        "skillGroupType": 2,
                        "skillIdList": ["chr_0005_chen_ultimate_skill"],
                    },
                    "chr_0005_chen_ComboSkill": {
                        "name": "见天河",
                        "desc": "当有敌人进入破防状态时可以发动。进行穿梭斩击。",
                        "icon": "icon_combo_skill_chen_01",
                        "skillGroupType": 3,
                        "skillIdList": ["chr_0005_chen_combo_skill"],
                    },
                },
                "talentNodeMap": {
                    "chr_0005_chen_passive_skill_0_1": {
                        "passiveSkillNodeInfo": {
                            "name": "斩锋",
                            "iconId": "icon_talent_chen_01",
                            "talentEffectId": "chr_0005_chen_talent_1_1",
                        }
                    },
                    "chr_0005_chen_passive_skill_1_1": {
                        "passiveSkillNodeInfo": {
                            "name": "破势",
                            "iconId": "icon_talent_chen_02",
                            "talentEffectId": "chr_0005_chen_talent_2_1",
                        }
                    },
                },
            },
            "skillPatchTable": {
                "chr_0005_chen_attack5": {"SkillPatchDataBundle": normal_attack_records},
                "chr_0005_chen_power_attack": {"SkillPatchDataBundle": execute_attack_records},
                "chr_0005_chen_plunging_attack_end": {"SkillPatchDataBundle": plunging_attack_records},
                "chr_0005_chen_normal_skill": {"SkillPatchDataBundle": skill_records},
                "chr_0005_chen_ultimate_skill": {"SkillPatchDataBundle": ultimate_records},
                "chr_0005_chen_combo_skill": {"SkillPatchDataBundle": combo_records},
            },
            "potentialTalentEffectTable": {
                "chr_0005_chen_talent_1_1": {
                    "desc": "技能每次命中敌人后，攻击力<@ba.vup>+{atk:0%}</>，持续{duration:0}秒。",
                    "dataList": [{"attachBuff": {"blackboard": [{"key": "atk", "value": 0.04}, {"key": "duration", "value": 10}]}}],
                },
                "chr_0005_chen_talent_2_1": {
                    "desc": "技能打断敌人蓄力时，额外对其造成<@ba.poise>{poise:0}</>点失衡。",
                    "dataList": [{"attachBuff": {"blackboard": [{"key": "poise", "value": 5}]}}],
                },
                "chr_0005_chen_potential_1": {
                    "desc": "归穹宇命中带有<#ba.crystinflict>寒冷附着</>的敌人时，对生命值少于{hp_remain:0%}的敌人造成的伤害<@ba.vup>+{extra_dmg:0%}</>。",
                    "dataList": [{"attachBuff": {"blackboard": [{"key": "extra_dmg", "value": 0.2}, {"key": "hp_remain", "value": 0.5}]}}],
                },
            },
            "characterPotentialTable": {
                "firstItemId": "item_charpotentialup_chr_0005_chen",
                "potentialUnlockBundle": [
                    {"level": 1, "name": "绝影", "potentialEffectId": "chr_0005_chen_potential_1"}
                ],
            },
        },
        "refs": {
            "charProfessionTable": {"0": {"name": "近卫"}},
            "charTypeTable": {"Physical": {"name": "物理"}},
            "tagDataTable": {"tag_03": {"tagName": "输出"}},
            "hyperlinkTextTable": {
                "ba.crystinflict": {
                    "desc": "<@ba.crystinflict>寒冷附着</>是一种<#ba.spellinflict>法术附着</>。",
                    "iconPath": "TermIcon/icon_term_ba_crystinflict",
                    "id": "ba.crystinflict",
                    "name": "法术附着 - 寒冷",
                    "richTextId": "ba.cryst",
                }
            },
            "richTextStyleTable": {
                "ba.cryst": {
                    "id": "ba.cryst",
                    "preDef": ["<color=#30d6e0>", "<color=#08edfb>", "<color=#009cad>"],
                    "postDef": ["</color>", "</color>", "</color>"],
                }
            },
        },
    }


def _sample_weapon():
    return {
        "article": {
            "title": "武器/赤缨",
            "updatedAt": "2026-07-02T00:00:00.000Z",
        },
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {
                                "name": "赤缨",
                                "nameEn": "Amaranthine Tassel",
                                "rarity": 6,
                                "weaponType": "双手剑",
                                "maxLv": 90,
                                "iconUrl": "https://assets.fz.wiki/c3338b6b5f3d4283/ddd4730dd6caaff8.png",
                            },
                            "stats": {
                                "curve": [{"lv": 1, "atk": 52}, {"lv": 90, "atk": 510}],
                            },
                            "skills": {
                                "skills": [
                                    {
                                        "name": "力量提升·大",
                                        "description": "力量 {str:0}",
                                        "levels": [{"level": i, "values": {"str": 20 + i}} for i in range(1, 10)],
                                    },
                                    {
                                        "name": "攻击提升·大",
                                        "description": "攻击力 {atk:0.0%}",
                                        "levels": [{"level": i, "values": {"atk": 0.04 + i / 100}} for i in range(1, 10)],
                                    },
                                    {
                                        "name": "巧技·赤断",
                                        "description": "物理伤害 {damage:0.0%}。装备者施加<#ba.physicalvul>物理脆弱</>时，造成<#ba.noguard>破防</>。",
                                        "levels": [{"level": i, "values": {"damage": 0.1 + i / 100}} for i in range(1, 10)],
                                    },
                                ]
                            },
                        }
                    }
                ]
            }
        },
    }


def _sample_richtext():
    return {
        "RICH_TEXT_STYLES": {
            "ba.phy": {"id": "ba.phy", "color": "#bd7f42"},
        },
        "HYPERLINK_TEXTS": {
            "ba.physicalvul": {
                "id": "ba.physicalvul",
                "iconPath": "https://assets.fz.wiki/c40f3979bc72cf80/e82f5eb3144df5e3.png",
                "richTextId": "ba.phy",
            },
            "ba.noguard": {
                "id": "ba.noguard",
                "iconPath": "https://assets.fz.wiki/c40f3979bc72cf80/817f9771dd684e27.png",
                "richTextId": "ba.phy",
            },
        },
    }


class EndfieldServiceTests(unittest.TestCase):
    def test_clean_text_removes_warfarin_rich_text_tags(self):
        self.assertEqual(clean_text("造成<#ba.damage>物理伤害</>。"), "造成物理伤害。")

    def test_build_operator_view_extracts_four_skill_levels(self):
        view = build_operator_view(_sample_operator())

        self.assertEqual(view.name, "陈千语")
        self.assertEqual(view.profession, "近卫")
        self.assertEqual(view.damage_type, "物理")
        self.assertEqual(view.tags, ["输出"])
        self.assertEqual(view.weapon_type, "单手剑")
        self.assertEqual(view.species, "龙")
        self.assertEqual(len(view.skills), 4)
        self.assertEqual(len(view.talents), 2)
        self.assertEqual(len(view.potentials), 1)
        self.assertEqual([skill.category for skill in view.skills], ["普攻", "战技", "终结技", "连携技"])
        self.assertEqual([skill.title for skill in view.skills], ["破飞霞", "归穹宇", "冽风霜", "见天河"])
        self.assertIn("16点失衡", view.skills[0].description)
        self.assertEqual(view.skills[0].levels[0].values["普攻倍率"], "54%")
        self.assertEqual(view.skills[0].extra_levels["chr_0005_chen_power_attack"][0].values["处决攻击倍率"], "540%")
        self.assertEqual(view.skills[0].extra_levels["chr_0005_chen_plunging_attack_end"][0].values["下落攻击倍率"], "108%")
        skill = view.skills[1]
        self.assertEqual(skill.title, "归穹宇")
        self.assertEqual([level.label for level in skill.levels], [label for _, label in LEVEL_COLUMNS])
        self.assertEqual(skill.levels[0].values["伤害倍率"], "90%")
        self.assertEqual(skill.levels[-1].values["伤害倍率"], "120%")
        self.assertEqual(view.skills[2].levels[0].cost, "70")
        self.assertEqual(view.talents[0].title, "斩锋")
        self.assertEqual(view.talents[1].title, "破势")
        self.assertIn("攻击力", view.talents[0].description)
        self.assertIn("生命值少于50%", view.potentials[0].description)
        self.assertIn("寒冷附着", view.term_styles)
        self.assertEqual(view.term_styles["寒冷附着"].color, "#30d6e0")

    def test_build_operator_view_marks_missing_mastery_values(self):
        view = build_operator_view(_sample_operator(levels=(9, 10)))
        values = [level.values.get("伤害倍率", "--") for level in view.skills[1].levels]
        self.assertEqual(values, ["90%", "100%", "--", "--"])

    def test_render_operator_card_html_contains_fixed_columns_and_values(self):
        view = build_operator_view(_sample_operator())
        html = asyncio.run(render_operator_card_html(view))

        for _, label in LEVEL_COLUMNS:
            self.assertIn(label, html)
        self.assertIn("干员数据详表", html)
        self.assertIn("技能效果与倍率", html)
        self.assertIn("破飞霞", html)
        self.assertIn("处决攻击倍率", html)
        self.assertIn("下落攻击倍率", html)
        self.assertIn("归穹宇", html)
        self.assertIn("冽风霜", html)
        self.assertIn("见天河", html)
        self.assertIn("所需能量", html)
        self.assertIn("冷却", html)
        self.assertIn("天赋效果", html)
        self.assertIn("潜能效果", html)
        self.assertIn("120%", html)
        self.assertIn("最后一段倍率", html)
        self.assertIn("term-icon", html)
        self.assertIn("物理伤害", html)
        self.assertIn("击飞", html)
        self.assertIn("寒冷附着", html)
        self.assertIn("#30d6e0", html)
        self.assertIn("归穹宇", html)
        self.assertNotIn("S01", html)

    def test_build_weapon_view_extracts_fz_wiki_weapon_data(self):
        view = build_weapon_view(_sample_weapon(), _sample_richtext())

        self.assertEqual(view.name, "赤缨")
        self.assertEqual(view.english_name, "Amaranthine Tassel")
        self.assertEqual(view.weapon_type, "双手剑")
        self.assertEqual(view.rarity, 6)
        self.assertEqual(view.max_atk, 510)
        self.assertEqual(len(view.skills), 3)
        self.assertEqual(view.skills[2].title, "巧技·赤断")
        self.assertIn("ba.physicalvul", view.rich_text_links)

    def test_render_weapon_card_html_contains_preview_layout_and_rich_icons(self):
        view = build_weapon_view(_sample_weapon(), _sample_richtext())
        html = asyncio.run(render_weapon_card_html(view))

        self.assertIn("weapon-card", html)
        self.assertIn("武器数据详表", html)
        self.assertIn("赤缨", html)
        self.assertIn("Amaranthine Tassel", html)
        self.assertIn("力量提升·大", html)
        self.assertIn("攻击提升·大", html)
        self.assertIn("巧技·赤断", html)
        self.assertIn("Lv9", html)
        self.assertIn("frontend-level-list long", html)
        self.assertIn("term-icon", html)
        self.assertNotIn(">S1<", html)

    def test_bieli_second_potential_cryst_damage_value_and_no_icon(self):
        raw_path = ROOT / ".runtime" / "bieli.json"
        if not raw_path.exists():
            self.skipTest("bieli runtime sample is not available")
        import json

        view = build_operator_view(json.loads(raw_path.read_text(encoding="utf-8")))
        styles = draw.merged_term_styles(view)
        rendered = draw.highlight_terms(view.potentials[1].description, styles, {})

        expected_desc = "\u529b\u91cf+20\uff0c\u5bd2\u51b7\u4f24\u5bb3+10%\u3002"
        marker = "\u5bd2\u51b7\u4f24\u5bb3</span><strong>+10%</strong>"
        term = "\u5bd2\u51b7\u4f24\u5bb3"
        self.assertEqual(view.potentials[1].description, expected_desc)
        self.assertIn(marker, rendered)
        term_fragment = rendered.split(marker, 1)[0].rsplit(term, 1)[-1]
        self.assertNotIn("term-icon", term_fragment)

    def test_gilberta_talent_charge_value_and_spell_vul_no_icon(self):
        raw_path = ROOT / ".runtime" / "gilberta.json"
        if not raw_path.exists():
            self.skipTest("gilberta runtime sample is not available")
        import json

        view = build_operator_view(json.loads(raw_path.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(view.potentials[1].description), 20)
        styles = draw.merged_term_styles(view)
        rendered = draw.highlight_terms(view.potentials[1].description, styles, {})

        self.assertIn("\u5145\u80fd\u6548\u7387+7%", view.talents[0].description)
        self.assertIn("\u6cd5\u672f\u8106\u5f31", rendered)
        self.assertNotIn('<span class="term" style="--term-color: #33c2ff">\u6cd5\u672f\u8106\u5f31</span>', rendered)
        self.assertNotIn('<span class="term-plain">\u6cd5\u672f\u8106\u5f31</span>', rendered)

    def test_natural_inflict_keeps_own_icon_not_corrupt_icon(self):
        raw_path = ROOT / ".runtime" / "gilberta.json"
        if not raw_path.exists():
            self.skipTest("gilberta runtime sample is not available")
        import json

        view = build_operator_view(json.loads(raw_path.read_text(encoding="utf-8")))
        style = view.term_styles.get("\u81ea\u7136\u9644\u7740")

        self.assertIsNotNone(style)
        self.assertIn("icon_term_ba_naturalinflict", style.icon_url)
        self.assertNotIn("icon_term_ba_corrupt", style.icon_url)


class _FakeWarfarinClient:
    def __init__(self, *, search_data=None, operators_data=None):
        self._search = search_data if search_data is not None else {}
        self._operators = operators_data if operators_data is not None else {}

    async def search(self, query, *, lang="cn"):
        return self._search

    async def operators(self, *, lang="cn"):
        return self._operators


class EndfieldSlugResolutionTests(unittest.TestCase):
    def test_slug_input_returns_directly(self):
        client = _FakeWarfarinClient()
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_operator_slug("camille")), "camille")

    def test_search_hit_returns_slug_directly(self):
        client = _FakeWarfarinClient(
            search_data={"results": [{"slug": "chen-qianyu", "type": "operators"}]},
            operators_data={"data": []},
        )
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_operator_slug("陈千语")), "chen-qianyu")

    def test_search_miss_falls_back_to_operators_list(self):
        client = _FakeWarfarinClient(
            search_data={
                "results": [
                    {"slug": "item_char_ap_supply_camille", "name": "魔甘草糖果", "type": "items"}
                ]
            },
            operators_data={"data": [{"slug": "camille", "name": "卡缪"}]},
        )
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_operator_slug("卡缪")), "camille")

    def test_no_match_returns_none(self):
        client = _FakeWarfarinClient(
            search_data={"results": []},
            operators_data={"data": [{"slug": "camille", "name": "卡缪"}]},
        )
        svc = service.EndfieldService(client)
        self.assertIsNone(asyncio.run(svc.find_operator_slug("不存在")))


if __name__ == "__main__":
    unittest.main()
