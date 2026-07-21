from __future__ import annotations

import importlib.util
import asyncio
import io
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

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
    if module_name == "commands":
        return _load_module(f"{pkg_name}.commands", "plugins/endfield/commands.py")
    if module_name == "draw":
        return _load_module(f"{pkg_name}.draw", "plugins/endfield/draw.py")
    if module_name == "service":
        return _load_module(f"{pkg_name}.service", "plugins/endfield/service.py")
    raise ValueError(module_name)


draw = _load_endfield_module("draw")
service = _load_endfield_module("service")
commands = _load_endfield_module("commands")
models = sys.modules["endfield_for_test.models"]
aliases = sys.modules["endfield_for_test.aliases"]

render_operator_card_html = draw.render_operator_card_html
render_weapon_card_html = draw.render_weapon_card_html
render_equipment_card_html = draw.render_equipment_card_html
render_equipment_catalog_card_html = draw.render_equipment_catalog_card_html
render_operator_catalog_card_html = draw.render_operator_catalog_card_html
render_weapon_catalog_card_html = draw.render_weapon_catalog_card_html
render_loadout_card_html = draw.render_loadout_card_html
LEVEL_COLUMNS = models.LEVEL_COLUMNS
build_operator_view = service.build_operator_view
build_weapon_view = service.build_weapon_view
build_fz_equipment_view = service.build_fz_equipment_view
build_fz_equipment_catalog_view = service.build_fz_equipment_catalog_view
build_fz_operator_catalog_view = service.build_fz_operator_catalog_view
build_fz_weapon_catalog_view = service.build_fz_weapon_catalog_view
build_fz_loadout_view = service.build_fz_loadout_view
clean_text = service.clean_text


class EndfieldCommandParserTests(unittest.TestCase):
    def test_handler_does_not_report_session_stop_as_failure(self):
        source = (ROOT / "plugins/endfield/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _handle_command")
        end = source.index("async def _collect_candidates", start)
        handler_source = source[start:end]

        self.assertIn("from arclet.letoderea.exceptions import _ExitException", source)
        exit_index = handler_source.index("except _ExitException:")
        api_error_index = handler_source.index("except WarfarinAPIError as exc:", exit_index)
        generic_index = handler_source.index("except Exception as exc:", api_error_index)
        self.assertLess(exit_index, generic_index)
        self.assertIn("raise", handler_source[exit_index:api_error_index])
        self.assertIn('command.scope in {"operator", "weapon", "equipment"}', handler_source)

    def test_handler_reports_image_send_failure_separately(self):
        source = (ROOT / "plugins/endfield/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _handle_command")
        end = source.index("async def _collect_candidates", start)
        handler_source = source[start:end]

        self.assertIn("[endfield] send failed", handler_source)
        self.assertIn("图片发送失败，请稍后重试", handler_source)

    def test_root_aliases_include_zmd(self):
        self.assertIn("zmd", commands.ROOT_ALIASES)
        self.assertIn("终末地", commands.ROOT_ALIASES)

    def test_parse_loadout_command_with_levels_and_slots(self):
        parsed = commands.parse_command(
            "配装 佩丽卡 | 赤缨 | 长息轻护甲@2 | 长息护手 | 无 | 长息蓄电核@1 "
            "--char-level 80 --weapon-level=70 --potential 3 --enhance 2"
        )
        self.assertEqual(parsed.action, "loadout")
        self.assertEqual((parsed.char_level, parsed.weapon_level, parsed.potential, parsed.enhance), (80, 70, 3, 2))
        spec, error = commands.parse_loadout_spec(parsed.query, parsed.enhance)
        self.assertEqual(error, "")
        self.assertEqual(spec.operator, "佩丽卡")
        self.assertEqual(spec.body.enhance, 2)
        self.assertEqual(spec.hand.enhance, 2)
        self.assertEqual(spec.accessory_1.name, "")
        self.assertEqual(spec.accessory_2.enhance, 1)

    def test_parse_loadout_rejects_invalid_enhance(self):
        spec, error = commands.parse_loadout_spec("佩丽卡 | 赤缨 | 护甲@4 | 无 | 无 | 无")
        self.assertIsNone(spec)
        self.assertIn("0–3", error)

    def test_score_candidate_handles_typo_and_pinyin(self):
        exact = commands.score_candidate("弭弗", "弭弗")
        typo = commands.score_candidate("弥弗", "弭弗")
        weak = commands.score_candidate("陈千语", "弭弗")

        self.assertGreaterEqual(typo, commands.CANDIDATE_SCORE_THRESHOLD)
        self.assertGreater(exact, typo)
        self.assertGreater(typo, weak)
        self.assertLess(weak, commands.CLEAR_SCORE)
        self.assertGreaterEqual(commands.score_candidate("赤樱", "赤缨"), commands.CANDIDATE_SCORE_THRESHOLD)

    def test_alias_library_scores_entities_and_preserves_ambiguity(self):
        self.assertEqual(commands.score_entity_candidate("operator", "lzy", "诀"), 100)
        self.assertEqual(commands.score_entity_candidate("weapon", "TRASH", "作品：蚀迹"), 100)
        self.assertEqual(commands.score_entity_candidate("equipment", "拓荒终结技甲", "拓荒护甲"), 100)
        self.assertEqual(aliases.alias_targets("operator", "管"), ("管理员(男)", "管理员(女)"))
        self.assertEqual(
            aliases.alias_targets("equipment", "拓荒战技甲"),
            ("拓荒护甲·壹型", "拓荒护甲·贰型"),
        )
        self.assertEqual(aliases.alias_targets("weapon", "228"), ())
        self.assertEqual(aliases.aliases_for("weapon", "不存在的武器"), ())

    def test_candidate_resolvers_use_alias_scoring_before_slug_fallback(self):
        source = (ROOT / "plugins/endfield/__init__.py").read_text(encoding="utf-8")

        self.assertIn('score_entity_candidate("operator", query, item.name', source)
        self.assertIn('score_entity_candidate("weapon", query, item.name', source)
        self.assertIn('score_entity_candidate("equipment", query, item.name', source)
        self.assertIn('_looks_like_operator_slug(query) and not alias_targets("operator", query)', source)
        self.assertIn('_looks_like_operator_slug(query) and not alias_targets("weapon", query)', source)

    def test_default_query_uses_all_scope(self):
        parsed = commands.parse_command("陈千语")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "all")
        self.assertEqual(parsed.query, "陈千语")

    def test_operator_query_aliases(self):
        parsed = commands.parse_command("干员 陈千语")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "operator")
        self.assertEqual(parsed.query, "陈千语")

        parsed = commands.parse_command("op 陈千语")
        self.assertEqual(parsed.scope, "operator")
        self.assertEqual(parsed.query, "陈千语")

    def test_weapon_query_aliases(self):
        parsed = commands.parse_command("武器 赤缨")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "weapon")
        self.assertEqual(parsed.query, "赤缨")

    def test_equipment_query_aliases(self):
        parsed = commands.parse_command("装备 长息轻护甲")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "equipment")
        self.assertEqual(parsed.query, "长息轻护甲")

        role_catalog = commands.parse_command("角色")
        self.assertEqual(role_catalog.scope, "operator")
        self.assertEqual(role_catalog.query, "")

    def test_equipment_catalog_rarity_options(self):
        default = commands.parse_command("装备")
        self.assertEqual(default.scope, "equipment")
        self.assertEqual(default.query, "")
        self.assertEqual(default.rarity, "")

        all_items = commands.parse_command("装备 --all")
        self.assertEqual(all_items.scope, "equipment")
        self.assertEqual(all_items.query, "")
        self.assertEqual(all_items.rarity, "all")

        purple = commands.parse_command("装备 长息 --rarity purple")
        self.assertEqual(purple.query, "长息")
        self.assertEqual(purple.rarity, "purple")

        blue = commands.parse_shortcut_command("efeq", "巡行信使 --rarity=blue")
        self.assertEqual(blue.query, "巡行信使")
        self.assertEqual(blue.rarity, "blue")

        invalid = commands.parse_command("装备 --rarity orange")
        self.assertEqual(invalid.action, "invalid")
        self.assertIn("装备稀有度", invalid.error)

        parsed = commands.parse_command("eq 长息轻护甲")
        self.assertEqual(parsed.scope, "equipment")
        self.assertEqual(parsed.query, "长息轻护甲")

        parsed = commands.parse_command("wp 赤缨")
        self.assertEqual(parsed.scope, "weapon")
        self.assertEqual(parsed.query, "赤缨")

    def test_search_aliases_and_scopes(self):
        parsed = commands.parse_command("搜索 陈")
        self.assertEqual(parsed.action, "search")
        self.assertEqual(parsed.scope, "all")
        self.assertEqual(parsed.query, "陈")

        parsed = commands.parse_command("搜索 干员 陈")
        self.assertEqual(parsed.action, "search")
        self.assertEqual(parsed.scope, "operator")
        self.assertEqual(parsed.query, "陈")

    def test_query_accepts_source_option_before_or_after_query(self):
        parsed = commands.parse_command("--source warfarin 干员 陈千语")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "operator")
        self.assertEqual(parsed.query, "陈千语")
        self.assertEqual(parsed.source, "warfarin")

        parsed = commands.parse_command("武器 赤缨 --source=fz")
        self.assertEqual(parsed.scope, "weapon")
        self.assertEqual(parsed.query, "赤缨")
        self.assertEqual(parsed.source, "fz")

    def test_source_option_supports_short_form_and_alias(self):
        parsed = commands.parse_command("搜索 陈 -s wf")
        self.assertEqual(parsed.action, "search")
        self.assertEqual(parsed.query, "陈")
        self.assertEqual(parsed.source, "warfarin")

        shortcut = commands.parse_shortcut_command("efop", "陈千语 -s fz-wiki")
        self.assertEqual(shortcut.query, "陈千语")
        self.assertEqual(shortcut.source, "fz")

    def test_source_option_rejects_missing_unknown_and_conflicting_values(self):
        missing = commands.parse_command("陈千语 --source")
        self.assertEqual(missing.action, "invalid")
        self.assertIn("需要数据源名称", missing.error)

        unknown = commands.parse_command("陈千语 --source skland")
        self.assertEqual(unknown.action, "invalid")
        self.assertIn("不支持的数据源", unknown.error)

        conflicting = commands.parse_command("陈千语 -s fz --source warfarin")
        self.assertEqual(conflicting.action, "invalid")
        self.assertIn("只能指定一个数据源", conflicting.error)

    def test_shortcuts_map_to_internal_commands(self):
        parsed = commands.parse_shortcut_command("efop", "陈千语")
        self.assertEqual(parsed.action, "query")
        self.assertEqual(parsed.scope, "operator")
        self.assertEqual(parsed.query, "陈千语")

        parsed = commands.parse_shortcut_command("efwp", "赤缨")
        self.assertEqual(parsed.scope, "weapon")
        self.assertEqual(parsed.query, "赤缨")

        parsed = commands.parse_shortcut_command("efeq", "长息轻护甲")
        self.assertEqual(parsed.scope, "equipment")
        self.assertEqual(parsed.query, "长息轻护甲")

        parsed = commands.parse_shortcut_command("efs", "陈")
        self.assertEqual(parsed.action, "search")
        self.assertEqual(parsed.scope, "all")
        self.assertEqual(parsed.query, "陈")

    def test_choose_candidate_clear_and_ambiguous_and_missing(self):
        best = commands.EndfieldCandidate("operator", "chen-qianyu", "陈千语", 100)
        weak = commands.EndfieldCandidate("weapon", "武器/赤缨", "赤缨", 70)
        selected, ambiguous = commands.choose_candidate([weak, best])
        self.assertEqual(selected, best)
        self.assertEqual(ambiguous, [])

        other = commands.EndfieldCandidate("weapon", "武器/陈千语", "陈千语", 95)
        selected, ambiguous = commands.choose_candidate([best, other])
        self.assertIsNone(selected)
        self.assertEqual(ambiguous, [best, other])

        selected, ambiguous = commands.choose_candidate([
            commands.EndfieldCandidate("operator", "x", "x", 20)
        ])
        self.assertIsNone(selected)
        self.assertEqual(len(ambiguous), 1)

    def test_dev_visibility_uses_superusers(self):
        self.assertTrue(commands.dev_visible_for_user("246", ["100", "246"]))
        self.assertFalse(commands.dev_visible_for_user("135", ["100", "246"]))

    def test_dev_command_parses_action_and_args(self):
        parsed = commands.parse_command("dev resolve 陈千语")
        self.assertEqual(parsed.action, "dev")
        self.assertEqual(parsed.dev_action, "resolve")
        self.assertEqual(parsed.args, ("陈千语",))

    def test_source_help_lists_warfarin_weapon_fallback(self):
        text = commands.format_source()
        self.assertIn("武器：FZ Wiki、Warfarin Wiki", text)
        self.assertIn("装备：FZ Wiki", text)

    def test_help_documents_source_option(self):
        text = commands.format_help()
        self.assertIn("--source <fz|warfarin>", text)
        self.assertIn("-s/--source", text)


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


def _sample_fz_operator():
    data_url = "data:image/png;base64,"

    def levels(*, cooldown=0, cost=0):
        return [
            {
                "level": level,
                "desc": "攻击倍率 {display_atk_scale:0%}，失衡值 {poise:0}。",
                "values": {
                    "atb": 0,
                    "poise": 8 + level,
                    "atk_scale": 0.2 + level / 100,
                    "display_atk_scale": 0.3 + level / 100,
                    "usp": 2 + level,
                    "duration": 1 + level / 10,
                    "CoolDown": cooldown,
                },
                "cooldown": cooldown,
                "cost": cost,
            }
            for level in range(1, 11)
        ]

    return {
        "article": {"title": "干员/佩丽卡", "updatedAt": "2026-07-02T11:55:21.758Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {
                                "name": "佩丽卡",
                                "nameEn": "Perlica",
                                "rarity": 5,
                                "profession": "术师",
                                "element": "电磁",
                                "faction": "终末地工业",
                                "meta": [{"label": "所属", "value": "终末地工业"}],
                                "weaponType": "施术单元",
                                "tags": ["电磁附着", "导电"],
                                "iconUrl": data_url,
                                "portraitFile": data_url,
                            },
                            "archive": {
                                "archive": [
                                    {
                                        "body": "【代号】佩丽卡\n【性别】女\n【身份认证】终末地工业\n【生日】3月16日\n【种族】黎博利\n【矿石病感染情况】\n参照医学检测报告，确认为非感染者。"
                                    }
                                ]
                            },
                            "skills": {
                                "skills": [
                                    {
                                        "name": "协议α·突破",
                                        "desc": "普通攻击造成<#ba.pulse>电磁伤害</>。",
                                        "icon": {"glyph": {"url": data_url}},
                                        "levels": levels(),
                                    },
                                    {
                                        "name": "协议ω·雷击",
                                        "desc": "攻击倍率 {display_atk_scale:0%}。",
                                        "icon": {"glyph": {"url": data_url}},
                                        "levels": levels(),
                                        "paramTable": {
                                            "rows": [
                                                {"label": "技力消耗", "values": ["100"] * 10},
                                                {"label": "攻击倍率", "values": ["200%"] * 10},
                                                {"label": "消耗一层破防时技力恢复", "values": ["5"] * 10},
                                                {"label": "消耗二层破防时技力恢复", "values": ["15"] * 10},
                                            ]
                                        },
                                    },
                                    {
                                        "name": "即时协议·闪链",
                                        "desc": "造成<#ba.conduct>导电</>，持续{duration:0}秒。",
                                        "icon": {"glyph": {"url": data_url}},
                                        "levels": levels(cooldown=15),
                                        "paramTable": {
                                            "rows": [
                                                {"label": "第一段技力恢复", "values": ["5"] * 10},
                                                {"label": "第二段技力恢复", "values": ["7"] * 10},
                                            ]
                                        },
                                    },
                                    {
                                        "name": "协议ε·70.41κ",
                                        "desc": "终结技能量 {usp:0}。",
                                        "icon": {"glyph": {"url": data_url}},
                                        "levels": levels(cooldown=20, cost=80),
                                        "paramTable": {
                                            "rows": [
                                                {"label": "所需终结技能量", "values": ["80"] * 10},
                                                {"label": "冷却", "values": ["10s"] * 10},
                                                {"label": "伤害倍率", "values": [f"{100 + i * 100}%" for i in range(10)]},
                                                {"label": "失衡值", "values": ["20"] * 10},
                                            ]
                                        },
                                    },
                                ]
                            },
                            "talents": {
                                "talents": [
                                    {
                                        "name": "歼灭协议",
                                        "level": 1,
                                        "desc": "对<@ba.poise>失衡</>的敌人造成的伤害<@ba.vup>+{dmg:0%}</>。",
                                        "values": {"dmg": 0.2},
                                        "iconUrl": data_url,
                                    },
                                    {
                                        "name": "歼灭协议",
                                        "level": 2,
                                        "desc": "对<@ba.poise>失衡</>的敌人造成的伤害<@ba.vup>+{dmg:0%}</>。",
                                        "values": {"dmg": 0.3},
                                        "iconUrl": data_url,
                                    },
                                ]
                            },
                            "potentials": {
                                "potentials": [
                                    {
                                        "name": "危机处理",
                                        "level": 1,
                                        "desc": "连携技<@ba.key>即时协议·闪链</>施加的<#ba.conduct>导电</>持续时间<@ba.vup>+{duration-1:0%}</>。",
                                        "values": {"duration": 1.75},
                                        "iconUrl": data_url,
                                    },
                                    {
                                        "name": "谈判策略",
                                        "level": 2,
                                        "desc": "终结技<@ba.key>协议ε·70.41κ</>所需的终结技能量<@ba.vup>-{1-costvalue:0%}</>。",
                                        "values": {"CostValue": 0.85},
                                        "iconUrl": data_url,
                                    },
                                ]
                            },
                        }
                    }
                ]
            }
        },
    }


def _sample_fz_equipment():
    return {
        "article": {
            "title": "装备/长息轻护甲",
            "updatedAt": "2026-07-19T15:05:51.395Z",
        },
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {
                                "name": "长息轻护甲",
                                "level": 70,
                                "flavor": "某位疯狂天师的遗世之作。",
                                "rarity": 5,
                                "iconUrl": "https://assets.fz.wiki/equipment.png",
                                "partType": "Body",
                                "slotType": "护甲",
                                "suitName": "长息",
                                "groupName": "长息装备组",
                                "description": "本装备由宏山选剑局设计。",
                            },
                            "suit": {
                                "bonus": {
                                    "name": "长息",
                                    "levels": [
                                        {
                                            "level": 1,
                                            "values": {"hp_up": 1000, "dmg_up": 0.16, "duration": 15},
                                        }
                                    ],
                                    "description": "3件套组效果：生命值<@ba.vup>+{hp_up}</>，伤害<@ba.vup>+{dmg_up:0%}</>，持续{duration}秒。",
                                },
                                "pieces": [
                                    {
                                        "name": "长息轻护甲",
                                        "equipId": "equip_self",
                                        "slotType": "护甲",
                                        "iconUrl": "https://assets.fz.wiki/equipment.png",
                                    },
                                    {
                                        "name": "长息护手",
                                        "equipId": "equip_hand",
                                        "slotType": "护手",
                                        "iconUrl": "https://assets.fz.wiki/hand.png",
                                    },
                                ],
                                "equipCnt": 3,
                                "suitName": "长息",
                                "groupName": "长息装备组",
                                "selfEquipId": "equip_self",
                            },
                            "stats": {
                                "rows": [
                                    {"label": "防御力", "values": [56, 56, 56, 56], "isPercent": False, "attrType": "Def"},
                                    {"label": "意志", "values": [110, 121, 132, 143], "isPercent": False, "attrType": "Will"},
                                    {"label": "终结技充能效率", "values": [0.123214, 0.1355, 0.1479, 0.1602], "isPercent": True, "attrType": "UltimateSpGainScalar"},
                                ]
                            },
                            "materials": {"unlockType": "EquipFormulaChest"},
                        }
                    }
                ]
            }
        },
    }


def _sample_fz_equipment_catalog():
    def entry(name, group, rarity, slot, icon, attributes):
        return {
            "name": name,
            "group": group,
            "level": 70,
            "title": f"装备/{name}",
            "rarity": rarity,
            "equipId": f"equip_{name}",
            "iconUrl": icon,
            "slotType": slot,
            "attrList": [
                {"label": label, "value": value}
                for label, value in attributes
            ],
        }

    return {
        "article": {"title": "装备", "updatedAt": "2026-07-20T00:00:00.000Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "roster": {
                                "entries": [
                                    entry(
                                        "长息轻护甲",
                                        "长息装备组",
                                        5,
                                        "护甲",
                                        "https://assets.fz.wiki/gold-body.png",
                                        [("防御力", "56"), ("意志", "110→143"), ("终结技充能效率", "12.3%→16.0%")],
                                    ),
                                    entry(
                                        "长息护手",
                                        "长息装备组",
                                        5,
                                        "护手",
                                        "https://assets.fz.wiki/gold-hand.png",
                                        [("攻击力", "42"), ("力量", "87→113")],
                                    ),
                                    entry(
                                        "巡行信使护甲",
                                        "巡行信使装备组",
                                        4,
                                        "护甲",
                                        "https://assets.fz.wiki/purple-body.png",
                                        [("防御力", "48"), ("敏捷", "74→96")],
                                    ),
                                    entry(
                                        "巡行信使护手",
                                        "巡行信使装备组",
                                        3,
                                        "护手",
                                        "https://assets.fz.wiki/blue-hand.png",
                                        [("攻击力", "30"), ("智识", "40→52")],
                                    ),
                                ]
                            }
                        }
                    }
                ]
            }
        },
    }


def _sample_fz_operator_catalog():
    return {
        "article": {"title": "干员", "updatedAt": "2026-07-17T02:35:44.255Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "roster": {
                                "entries": [
                                    {
                                        "name": "陈千语",
                                        "title": "干员/陈千语",
                                        "charId": "chr_0005_chen",
                                        "nameEn": "Chen Qianyu",
                                        "rarity": 5,
                                        "element": "物理",
                                        "elementColor": "#888888",
                                        "profession": "近卫",
                                        "weaponType": "单手剑",
                                        "iconUrl": "https://assets.fz.wiki/chen.png",
                                        "elementIconUrl": "https://assets.fz.wiki/physical.png",
                                        "professionIconUrl": "https://assets.fz.wiki/guard.png",
                                        "weaponTypeIconUrl": "https://assets.fz.wiki/sword.png",
                                    },
                                    {
                                        "name": "狼卫",
                                        "title": "干员/狼卫",
                                        "charId": "chr_0006_wolfgd",
                                        "nameEn": "Wulfgard",
                                        "rarity": 5,
                                        "element": "灼热",
                                        "elementColor": "#FF623D",
                                        "profession": "术师",
                                        "weaponType": "手铳",
                                        "iconUrl": "https://assets.fz.wiki/wolfgd.png",
                                        "elementIconUrl": "https://assets.fz.wiki/fire.png",
                                        "professionIconUrl": "https://assets.fz.wiki/caster.png",
                                        "weaponTypeIconUrl": "https://assets.fz.wiki/pistol.png",
                                    },
                                    {
                                        "name": "秋栗",
                                        "title": "干员/秋栗",
                                        "charId": "chr_0019_karin",
                                        "nameEn": "Akekuri",
                                        "rarity": 4,
                                        "element": "灼热",
                                        "elementColor": "#FF623D",
                                        "profession": "先锋",
                                        "weaponType": "单手剑",
                                        "iconUrl": "https://assets.fz.wiki/karin.png",
                                        "elementIconUrl": "https://assets.fz.wiki/fire.png",
                                        "professionIconUrl": "https://assets.fz.wiki/vanguard.png",
                                        "weaponTypeIconUrl": "https://assets.fz.wiki/sword.png",
                                    },
                                ]
                            }
                        }
                    }
                ]
            }
        },
    }


def _sample_fz_weapon_catalog():
    return {
        "article": {"title": "武器", "updatedAt": "2026-07-17T02:37:36.799Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "roster": {
                                "entries": [
                                    {
                                        "name": "狼之绯",
                                        "title": "武器/狼之绯",
                                        "weaponId": "wpn_sword_0022",
                                        "nameEn": "Lupine Scarlet",
                                        "rarity": 6,
                                        "weaponType": "单手剑",
                                        "maxLv": 90,
                                        "maxAtk": 505,
                                        "iconUrl": "https://assets.fz.wiki/lupine.png",
                                        "weaponTypeIconUrl": "https://assets.fz.wiki/sword.png",
                                        "substrateIconUrl": "https://assets.fz.wiki/crit.png",
                                        "termsMain": ["敏捷提升"],
                                        "termsSub": ["暴击率提升"],
                                        "termsSkill": ["切骨"],
                                    },
                                    {
                                        "name": "工业零点一",
                                        "title": "武器/工业零点一",
                                        "weaponId": "wpn_claym_0003",
                                        "nameEn": "Industry 0.1",
                                        "rarity": 4,
                                        "weaponType": "双手剑",
                                        "maxLv": 70,
                                        "maxAtk": 410,
                                        "iconUrl": "https://assets.fz.wiki/industry.png",
                                        "weaponTypeIconUrl": "https://assets.fz.wiki/claymore.png",
                                        "termsMain": ["力量提升"],
                                        "termsSub": ["攻击提升"],
                                        "termsSkill": [],
                                    },
                                ]
                            }
                        }
                    }
                ]
            }
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
                                        "skillId": "sk_wpn_claym_0017",
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


def _sample_loadout_operator():
    rows = []
    values = {
        "MaxHp": ("生命值", "1000"),
        "Atk": ("攻击力", "100"),
        "Def": ("防御力", "0"),
        "Str": ("力量", "20"),
        "Agi": ("敏捷", "30"),
        "Wisd": ("智识", "40"),
        "Will": ("意志", "50"),
        "CriticalRate": ("暴击率", "5%"),
        "CriticalDamageIncrease": ("暴击伤害", "50%"),
        "UltimateSpGainScalar": ("终结技充能效率", "100%"),
    }
    for key, (label, value) in values.items():
        rows.append({"key": key, "label": label, "cells": [[value]]})
    return {
        "article": {"title": "干员/测试干员", "updatedAt": "2026-07-20T00:00:00.000Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {
                                "name": "测试干员",
                                "weaponType": "双手剑",
                                "meta": [{"label": "主 / 副属性", "value": "力量 / 意志"}],
                            },
                            "attributes": {"breaks": [{"levels": [90], "breakStage": 4}], "rows": rows},
                            "talents": {"talents": []},
                            "potentials": {"potentials": []},
                        }
                    }
                ]
            }
        },
    }


def _sample_loadout_weapon():
    return {
        "article": {"title": "武器/测试武器", "updatedAt": "2026-07-20T00:00:00.000Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {"name": "测试武器", "weaponType": "双手剑", "maxLv": 90},
                            "stats": {"curve": [{"lv": 90, "atk": 200}]},
                            "skills": {
                                "skills": [
                                    {
                                        "name": "力量提升",
                                        "description": "力量+{str}",
                                        "zeroPotentialMaxLevel": 9,
                                        "levels": [{"level": 9, "values": {"str": 10}}],
                                    },
                                    {
                                        "name": "攻击提升",
                                        "description": "攻击力+{atk:0%}",
                                        "zeroPotentialMaxLevel": 9,
                                        "levels": [{"level": 9, "values": {"atk": 0.2}}],
                                    },
                                    {
                                        "name": "条件效果",
                                        "description": "物理伤害+{phy:0%}。攻击命中时，攻击力+{conditional_atk:0%}。",
                                        "zeroPotentialMaxLevel": 0,
                                        "levels": [{"level": 5, "values": {"phy": 0.1, "conditional_atk": 0.2}}],
                                    },
                                ]
                            },
                        }
                    }
                ]
            }
        },
    }


def _sample_loadout_equipment(part_type="Body"):
    slot_type = {"Body": "护甲", "Hand": "护手", "EDC": "配件"}[part_type]
    return {
        "article": {"title": f"装备/测试{slot_type}", "updatedAt": "2026-07-20T00:00:00.000Z"},
        "revision": {
            "contentJson": {
                "content": [
                    {
                        "attrs": {
                            "hero": {"name": f"测试{slot_type}", "partType": part_type, "slotType": slot_type},
                            "stats": {
                                "rows": [
                                    {
                                        "label": "防御力",
                                        "values": [50, 50, 50, 50],
                                        "attrType": "Def",
                                        "modifierType": "BaseAddition",
                                    },
                                    {
                                        "label": "力量",
                                        "values": [0, 10, 20, 30],
                                        "attrType": "Str",
                                        "modifierType": "BaseAddition",
                                    },
                                    {
                                        "label": "攻击力",
                                        "values": [10, 10, 10, 10],
                                        "attrType": "Atk",
                                        "modifierType": "BaseFinalAddition",
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
            "ba.vup": {"id": "ba.vup", "color": "#9eb7ff"},
            "ba.key": {"id": "ba.key", "color": "#33c2ff"},
            "ba.pulse": {"id": "ba.pulse", "color": "#ffcc00"},
            "ba.poise": {"id": "ba.poise", "color": "#ffd399"},
        },
        "HYPERLINK_TEXTS": {
            "ba.physicalvul": {
                "id": "ba.physicalvul",
                "iconPath": "https://assets.fz.wiki/c40f3979bc72cf80/e82f5eb3144df5e3.png",
                "richTextId": "ba.phy",
            },
            "ba.conduct": {
                "id": "ba.conduct",
                "iconPath": "data:image/png;base64,",
                "richTextId": "ba.pulse",
                "name": "导电",
            },
            "ba.noguard": {
                "id": "ba.noguard",
                "iconPath": "https://assets.fz.wiki/c40f3979bc72cf80/817f9771dd684e27.png",
                "richTextId": "ba.phy",
            },
        },
    }


def _sample_warfarin_weapon():
    return {
        "meta": {
            "id": "wpn_claym_0004",
            "slug": "exemplar",
            "name": "典范",
            "version": "1.3",
        },
        "data": {
            "weaponBasicTable": {
                "engName": "Exemplar",
                "maxLv": 90,
                "rarity": 6,
                "weaponId": "wpn_claym_0004",
                "weaponSkillList": ["wpn_attr_main_high", "sk_wpn_claym_0004"],
                "weaponType": 3,
            },
            "itemTable": {
                "iconId": "wpn_claym_0004",
                "name": "典范",
                "rarity": 6,
            },
            "weaponUpgradeTemplateTable": {
                "list": [{"weaponLv": 1, "baseAtk": 51}, {"weaponLv": 90, "baseAtk": 512}],
            },
            "skillPatchTable": {
                "wpn_attr_main_high": {
                    "SkillPatchDataBundle": [
                        {
                            "level": 1,
                            "skillName": "主能力提升·大",
                            "description": "主能力值<@ba.vup>+{mainattr}</>",
                            "blackboard": [{"key": "mainattr", "value": 17, "valueStr": ""}],
                        },
                        {
                            "level": 9,
                            "skillName": "主能力提升·大",
                            "description": "主能力值<@ba.vup>+{mainattr}</>",
                            "blackboard": [{"key": "mainattr", "value": 132, "valueStr": ""}],
                        },
                    ]
                },
                "sk_wpn_claym_0004": {
                    "SkillPatchDataBundle": [
                        {
                            "level": 1,
                            "skillName": "典雅准则",
                            "description": "物理伤害<#ba.physicalvul>提升</> {damage:0.0%}",
                            "blackboard": [{"key": "damage", "value": 0.1, "valueStr": ""}],
                        }
                    ]
                },
            },
        },
        "refs": {
            "weaponTypes": {"3": "双手剑"},
            "richTextStyleTable": {
                "ba.vup": {"id": "ba.vup", "preDef": ["<color=#24a148>"], "postDef": ["</color>"]},
                "ba.phy": {"id": "ba.phy", "preDef": ["<color=#bd7f42>"], "postDef": ["</color>"]},
            },
            "hyperlinkTextTable": {
                "ba.physicalvul": {
                    "id": "ba.physicalvul",
                    "iconPath": "https://assets.example/physicalvul.png",
                    "richTextId": "ba.phy",
                }
            },
        },
    }


class EndfieldServiceTests(unittest.TestCase):
    def test_loadout_calculates_attack_and_derived_panel(self):
        view = build_fz_loadout_view(
            _sample_loadout_operator(),
            _sample_loadout_weapon(),
            [(_sample_loadout_equipment("Body"), 2, "Body")],
            operator_level=90,
            weapon_level=90,
            weapon_potential=5,
        )
        primary = {row.key: row.value for row in view.primary_stats}
        abilities = {row.key: row.value for row in view.ability_stats}
        advanced = {row.key: row.value for row in view.advanced_stats}
        self.assertEqual(primary["Atk"], "499")
        self.assertEqual(primary["MaxHp"], "1250")
        self.assertEqual(primary["Def"], "50")
        self.assertEqual(abilities["Str"], "50")
        self.assertEqual(advanced["PhysicalDamageIncrease"], "10.0%")
        self.assertTrue(any(effect.active and "物理伤害" in effect.description for effect in view.effects))
        self.assertTrue(any(not effect.active and "攻击命中时" in effect.description for effect in view.effects))

    def test_loadout_rejects_wrong_equipment_slot(self):
        with self.assertRaisesRegex(ValueError, "装备槽位不匹配"):
            build_fz_loadout_view(
                _sample_loadout_operator(),
                _sample_loadout_weapon(),
                [(_sample_loadout_equipment("Body"), 3, "Hand")],
            )

    def test_loadout_card_html_shows_static_and_triggered_effects(self):
        view = build_fz_loadout_view(
            _sample_loadout_operator(),
            _sample_loadout_weapon(),
            [(_sample_loadout_equipment("Body"), 2, "Body")],
        )
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_loadout_card_html(view))
        self.assertIn("终末地 · 配装模拟器", html)
        self.assertIn("已计入面板的常驻效果", html)
        self.assertIn("条件 / 触发效果", html)
        self.assertIn("499", html)

    def test_clean_text_removes_warfarin_rich_text_tags(self):
        self.assertEqual(clean_text("造成<#ba.damage>物理伤害</>。"), "造成物理伤害。")

    def test_fz_rich_text_preserves_adjacent_high_index_tags(self):
        prefix = "".join(f"<@ba.key>{index}</>" for index in range(7))
        text = prefix + "额外<#ba.return>返还</><@ba.vup>5</>点技力。"
        cleaned = service._clean_fz_rich_text(text)

        self.assertIn("<#ba.return>返还</><@ba.vup>5</>点技力。", cleaned)
        self.assertNotIn("\x00", cleaned)

    def test_fz_template_supports_scalar_multiplication(self):
        rendered = service._format_fz_template(
            "无视<@ba.vup>{100*ignore_fire_resist:0}</>点抗性。",
            {"ignore_fire_resist": 0.2},
        )

        self.assertEqual(rendered, "无视<@ba.vup>20</>点抗性。")

    def test_fz_template_supports_constant_minus_negative_value(self):
        rendered = service._format_fz_template(
            "虚弱效果<@ba.vup>+{0-weak_scale:0%}</>。",
            {"weak_scale": -0.05},
        )

        self.assertEqual(rendered, "虚弱效果<@ba.vup>+5%</>。")

    def test_build_operator_view_extracts_four_skill_levels(self):
        view = build_operator_view(_sample_operator())

        self.assertEqual(view.name, "陈千语")
        self.assertEqual(view.profession, "近卫")
        self.assertEqual(view.damage_type, "物理")
        self.assertEqual(view.tags, ["输出"])
        self.assertEqual(view.weapon_type, "单手剑")
        self.assertEqual(view.species, "龙")
        self.assertEqual(
            view.portrait_url,
            "https://static.warfarin.wiki/v4/characterportrait/chr_0005_chen.webp",
        )
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

    def test_fz_skill_levels_use_mastery_labels_for_levels_ten_to_twelve(self):
        levels = service._build_fz_skill_levels(
            [{"level": level, "values": {}} for level in (9, 10, 11, 12)]
        )

        self.assertEqual([level.label for level in levels], ["Lv9", "M1", "M2", "M3"])

    def test_warfarin_arcane_blackboard_metrics_are_localized_with_correct_units(self):
        normal = service._extract_values(
            {
                "skillId": "chr_0032_lizhiyan_attack5",
                "blackboard": [{"key": "atk_scale", "value": 0.85}],
            },
            "普攻",
        )
        ultimate = service._extract_values(
            {
                "skillId": "chr_0032_lizhiyan_ultimate_skill",
                "blackboard": [
                    {"key": "atk_scale", "value": 1.44},
                    {"key": "duration2", "value": 15},
                ],
            },
            "终结技",
        )
        combo = service._extract_values(
            {
                "skillId": "chr_0032_lizhiyan_combo_skill",
                "blackboard": [
                    {"key": "duration_will", "value": 6},
                    {"key": "spell_vul_per_will", "value": 0.000125},
                    {"key": "max_spell_vul_will", "value": 0.075},
                    {"key": "poise_laser", "value": 0},
                ],
            },
            "连携技",
        )

        self.assertEqual(normal, {"普攻倍率": "85%"})
        self.assertEqual(ultimate["破晦阵伤害倍率"], "144%")
        self.assertEqual(ultimate["阵法持续时间（秒）"], "15")
        self.assertEqual(combo["阵诀·意持续时间（秒）"], "6")
        self.assertEqual(combo["每点意志脆弱效果"], "0.0125%")
        self.assertEqual(combo["阵诀·意最大脆弱效果"], "7.5%")
        self.assertEqual(combo["集束打击失衡值"], "--")

    def test_warfarin_arcane_ultimate_merges_wisd_and_will_second_stage_metrics(self):
        def records(skill_id, values):
            return {
                "SkillPatchDataBundle": [
                    {
                        "level": level,
                        "skillId": skill_id,
                        "blackboard": [
                            {"key": key, "value": level_values[index]}
                            for key, level_values in values.items()
                        ],
                    }
                    for index, (level, _) in enumerate(LEVEL_COLUMNS)
                ]
            }

        primary_id = "chr_0032_lizhiyan_ultimate_skill"
        second_id = "chr_0032_lizhiyan_ultimate_skill2"
        skill_table = {
            primary_id: records(
                primary_id,
                {
                    "atk_scale": [1.44, 1.54, 1.66, 1.8],
                    "atk_scale_laser": [0.36, 0.38, 0.41, 0.45],
                    "atk_scale_laser_will": [0.36, 0.38, 0.41, 0.45],
                },
            ),
            second_id: records(
                second_id,
                {
                    "atk_scale": [11.52, 12.32, 13.28, 14.4],
                    "atk_scale_will": [2.88, 3.08, 3.32, 3.6],
                },
            ),
        }
        group_map = {
            "ultimate": {
                "skillGroupType": 2,
                "name": "破晦",
                "skillIdList": [primary_id, second_id],
            }
        }

        skill = service._build_skills(skill_table, group_map)[0]
        rows = dict(draw.skill_metric_rows(skill))

        self.assertEqual(rows["阵诀·智集束打击倍率"], ["36%", "38%", "41%", "45%"])
        self.assertEqual(rows["阵诀·意集束打击倍率"], ["36%", "38%", "41%", "45%"])
        self.assertEqual(rows["阵诀·智诀明伤害倍率"], ["1152%", "1232%", "1328%", "1440%"])
        self.assertEqual(rows["阵诀·意诀明伤害倍率"], ["288%", "308%", "332%", "360%"])

        common, groups = draw.skill_metric_row_groups(skill)
        self.assertEqual([group[0] for group in groups], ["阵诀·智", "阵诀·意"])
        self.assertEqual([row[1] for row in groups[0][2]], ["集束打击倍率", "诀明伤害倍率"])
        self.assertEqual([row[1] for row in groups[1][2]], ["集束打击倍率", "诀明伤害倍率"])
        self.assertNotIn("阵诀·智集束打击倍率", [name for name, _ in common])

        html = draw.metric_table(skill)
        self.assertIn('<span class="metric-group-name">阵诀·智</span>', html)
        self.assertIn('<span class="metric-group-note">智识值 ≥ 意志值</span>', html)
        self.assertIn('<span class="metric-group-name">阵诀·意</span>', html)
        self.assertIn('<div class="metric-name">诀明伤害倍率</div>', html)

    def test_single_form_skill_keeps_flat_metric_table(self):
        skill = models.SkillView(
            "single",
            "单形态技能",
            category="战技",
            levels=[
                models.SkillLevelView("Lv9", 9, {"阵诀·智伤害倍率": "100%", "失衡值": "10"})
            ],
        )

        common, groups = draw.skill_metric_row_groups(skill)

        self.assertEqual(common, draw.skill_metric_rows(skill))
        self.assertEqual(groups, [])
        self.assertNotIn("metric-group", draw.metric_table(skill))

    def test_warfarin_combo_uses_condition_descriptions_when_group_desc_is_empty(self):
        skill_id = "chr_0032_lizhiyan_combo_skill"
        skill_table = {
            skill_id: {
                "SkillPatchDataBundle": [
                    {
                        "level": 9,
                        "skillId": skill_id,
                        "blackboard": [],
                    }
                ]
            }
        }
        group_map = {
            "combo": {
                "skillGroupType": 3,
                "name": "应龙四式",
                "desc": "",
                "skillIdList": [skill_id],
                "conditionName1": "阵诀·智",
                "conditionPostDesc1": "<@ba.key>阵诀·智</>：\n- 命中后返还技力。",
                "conditionName2": "阵诀·意",
                "conditionPostDesc2": "<@ba.key>阵诀·意</>：\n- 施放时牵引敌人。",
            }
        }

        skill = service._build_skills(skill_table, group_map)[0]

        self.assertEqual(skill.description, "")
        self.assertEqual(
            skill.form_descriptions,
            [("阵诀·智", "命中后返还技力。"), ("阵诀·意", "施放时牵引敌人。")],
        )
        html = draw.skill_form_descriptions(skill, {}, {})
        self.assertIn('<div class="skill-form-name">阵诀·智</div>', html)
        self.assertIn('<div class="skill-form-text">命中后返还技力。</div>', html)
        self.assertIn('<div class="skill-form-name">阵诀·意</div>', html)
        self.assertIn('<div class="skill-form-text">施放时牵引敌人。</div>', html)
        self.assertNotIn("- ", html)

    def test_warfarin_arcane_effect_placeholders_and_expressions_are_resolved(self):
        potential = {
            "desc": "智识和意志+{Will:0}，源石技艺强度+{PhysicalAndSpellInflictionEnhance:0}。",
            "dataList": [
                {"attrModifier": {"attrType": 41, "attrValue": 15}},
                {"attrModifier": {"attrType": 42, "attrValue": 15}},
                {"attrModifier": {"attrType": 87, "attrValue": 16}},
            ],
        }
        talent = {
            "desc": "\n- 最大抗性提升至原本的{1+corrupt_rate:0.00}倍。\n- 每点意志施加{spell_vul_rate_per_will:0.00%}脆弱。",
            "dataList": [
                {
                    "attachSkill": {
                        "blackboard": [
                            {"key": "corrupt_rate", "value": 0.1},
                            {"key": "spell_vul_rate_per_will", "value": 0.0002},
                        ]
                    }
                }
            ],
        }

        self.assertEqual(service._format_effect_desc(potential), "智识和意志+15，源石技艺强度+16。")
        rendered = service._format_effect_desc(talent)
        self.assertEqual(rendered, "最大抗性提升至原本的1.10倍。 每点意志施加0.02%脆弱。")
        self.assertNotIn("--", rendered)
        self.assertNotIn("- ", rendered)

    def test_warfarin_skill_template_prefers_nonzero_group_value(self):
        records = [
            {"level": 9, "blackboard": [{"key": "poise", "value": 0}]},
            {"level": 9, "blackboard": [{"key": "poise", "value": 17}]},
        ]

        rendered = service._format_skill_desc("重击会造成{poise:0}点失衡。", records, "普攻")

        self.assertEqual(rendered, "重击会造成17点失衡。")

    def test_render_operator_card_html_contains_fixed_columns_and_values(self):
        view = build_operator_view(_sample_operator())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
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
        self.assertIn(draw.normalize_rich_color("#30d6e0"), html)
        self.assertIn("归穹宇", html)
        self.assertNotIn("S01", html)

    def test_build_fz_equipment_view_extracts_reference_card_fields(self):
        view = build_fz_equipment_view(_sample_fz_equipment(), _sample_richtext())

        self.assertEqual(view.name, "长息轻护甲")
        self.assertEqual(view.max_level, 70)
        self.assertEqual(view.rarity, 5)
        self.assertEqual(view.slot_type, "护甲")
        self.assertEqual(view.suit_name, "长息")
        self.assertEqual(
            [(stat.label, stat.value) for stat in view.stats],
            [("防御力", "56"), ("意志", "110"), ("终结技充能效率", "12.3%")],
        )
        self.assertEqual(view.stats[1].values, ["110", "121", "132", "143"])
        self.assertEqual(view.stats[2].values, ["12.3%", "13.6%", "14.8%", "16%"])
        self.assertIn("+1000", service.clean_text(view.suit_description))
        self.assertIn("+16%", service.clean_text(view.suit_description))
        self.assertEqual(view.suit_required_count, 3)
        self.assertEqual([piece.name for piece in view.suit_pieces], ["长息护手"])
        self.assertEqual(view.acquisition, "装备制造")
        self.assertEqual(view.source_version, "2026-07-19")

    def test_render_equipment_card_html_contains_reference_layout(self):
        view = build_fz_equipment_view(_sample_fz_equipment(), _sample_richtext())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_equipment_card_html(view))

        self.assertIn('class="equipment-card"', html)
        self.assertIn("装备属性", html)
        self.assertIn("装备套组效果", html)
        self.assertIn("长息轻护甲", html)
        self.assertIn("+56", html)
        self.assertIn("+12.3%", html)
        self.assertIn("0锻", html)
        self.assertIn("1锻", html)
        self.assertIn("2锻", html)
        self.assertIn("3锻", html)
        self.assertIn("+143", html)
        self.assertIn("+16%", html)
        self.assertIn('class="equipment-stat-value strong">+143</strong>', html)
        self.assertIn('class="equipment-stat-value strong">+16%</strong>', html)
        self.assertIn("margin-top:auto", html)
        self.assertIn("长息护手", html)
        self.assertIn("font-size:60px", html)
        self.assertIn("font-size:25px", html)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", html)
        self.assertEqual(html.count('class="equipment-stat-icon-img"'), 3)
        self.assertIn("data:image/png;base64,", draw.equipment_stat_icon("Def", "防御力"))
        self.assertTrue((ROOT / "assets/image/endfield/equipment/icon_attribute_will.png").exists())
        self.assertNotIn("获取方式", html)
        self.assertNotIn("终末地百科", html)
        self.assertNotIn("equipment-id", html)
        self.assertNotIn('class="rarity-star"', html)

    def test_build_fz_equipment_view_groups_equipment_without_suit_effect(self):
        raw = _sample_fz_equipment()
        attrs = raw["revision"]["contentJson"]["content"][0]["attrs"]
        attrs["suit"]["bonus"]["description"] = ""

        view = build_fz_equipment_view(raw)

        self.assertEqual(view.suit_name, "\u72ec\u7acb\u88c5\u5907")
        self.assertEqual(view.group_name, "\u72ec\u7acb\u88c5\u5907\u5957\u7ec4")
        self.assertEqual(view.suit_required_count, 0)
        self.assertEqual(view.suit_pieces, [])

    def test_build_fz_equipment_catalog_defaults_to_gold_and_filters_groups(self):
        raw = _sample_fz_equipment_catalog()

        default = build_fz_equipment_catalog_view(raw)
        self.assertEqual(default.rarity_filter, "gold")
        self.assertEqual(default.total_count, 2)
        self.assertEqual([group.name for group in default.groups], ["长息装备组"])
        self.assertEqual([item.name for item in default.groups[0].items], ["长息轻护甲", "长息护手"])

        all_items = build_fz_equipment_catalog_view(raw, rarity_filter="all")
        self.assertEqual(all_items.total_count, 4)
        self.assertEqual(len(all_items.groups), 2)

        purple = build_fz_equipment_catalog_view(raw, "巡行信使装备组", "purple")
        self.assertEqual(purple.total_count, 1)
        self.assertEqual(purple.groups[0].items[0].name, "巡行信使护甲")

        blue = build_fz_equipment_catalog_view(raw, "巡行信使装备组", "blue")
        self.assertEqual(blue.total_count, 1)
        self.assertEqual(blue.groups[0].items[0].name, "巡行信使护手")

    def test_build_fz_equipment_catalog_merges_independent_groups(self):
        raw = _sample_fz_equipment_catalog()
        entries = raw["revision"]["contentJson"]["content"][0]["attrs"]["roster"]["entries"]
        entries.extend(
            [
                {
                    "name": "\u7ebe\u96be\u91cd\u7532",
                    "group": "\u7ebe\u96be\u88c5\u5907\u7ec4",
                    "level": 70,
                    "title": "\u88c5\u5907/\u7ebe\u96be\u91cd\u7532",
                    "rarity": 5,
                    "slotType": "\u62a4\u7532",
                    "attrList": [],
                },
                {
                    "name": "\u6d89\u6e0a\u91cd\u7532",
                    "group": "\u6d89\u6e0a\u88c5\u5907\u7ec4",
                    "level": 70,
                    "title": "\u88c5\u5907/\u6d89\u6e0a\u91cd\u7532",
                    "rarity": 5,
                    "slotType": "\u62a4\u7532",
                    "attrList": [],
                },
            ]
        )

        view = build_fz_equipment_catalog_view(raw, "\u72ec\u7acb\u88c5\u5907\u5957\u7ec4", "gold")

        self.assertEqual([group.name for group in view.groups], ["\u72ec\u7acb\u88c5\u5907\u5957\u7ec4"])
        self.assertEqual(view.total_count, 2)

    def test_render_equipment_catalog_card_html_has_groups_and_attribute_icons(self):
        view = build_fz_equipment_catalog_view(_sample_fz_equipment_catalog())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_equipment_catalog_card_html(view))

        self.assertIn('class="equipment-catalog-card"', html)
        self.assertIn("全部装备套组", html)
        self.assertIn("金色装备", html)
        self.assertIn("长息装备组", html)
        self.assertIn("长息轻护甲", html)
        self.assertNotIn("巡行信使护甲", html)
        self.assertIn("catalog-attr-icon", html)
        self.assertEqual(html.count('class="equipment-catalog-item rarity-5"'), 2)
        self.assertNotIn('title="\u9632\u5fa1\u529b ', html)
        self.assertNotIn('<span>\u9632\u5fa1\u529b</span>', html)

    def test_render_specific_equipment_catalog_uses_compact_four_column_layout(self):
        view = build_fz_equipment_catalog_view(_sample_fz_equipment_catalog())
        view.title = view.groups[0].name
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_equipment_catalog_card_html(view))

        self.assertIn("width:1040px", html)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", html)

    def test_build_fz_operator_catalog_groups_by_element_then_profession(self):
        view = build_fz_operator_catalog_view(_sample_fz_operator_catalog())

        self.assertEqual(view.total_count, 3)
        self.assertEqual([element.name for element in view.elements], ["物理", "灼热"])
        self.assertEqual([group.name for group in view.elements[1].professions], ["术师", "先锋"])
        self.assertEqual(view.elements[1].professions[0].items[0].name, "狼卫")

        filtered = build_fz_operator_catalog_view(_sample_fz_operator_catalog(), "灼热", "先锋")
        self.assertEqual(filtered.title, "灼热 · 先锋")
        self.assertEqual(filtered.total_count, 1)
        self.assertEqual(filtered.elements[0].professions[0].items[0].name, "秋栗")

    def test_render_operator_catalog_card_html_has_reference_gallery_structure(self):
        view = build_fz_operator_catalog_view(_sample_fz_operator_catalog())
        view.elements[1].professions[1].items[0].rarity = 6
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_operator_catalog_card_html(view))

        self.assertIn('class="operator-catalog-card"', html)
        self.assertIn("默认按元素分类", html)
        self.assertIn("物理", html)
        self.assertIn("灼热", html)
        self.assertIn("术师", html)
        self.assertIn("object-fit:contain", html)
        self.assertNotIn(".operator-portrait::after", html)
        self.assertIn("background:transparent", html)
        self.assertIn("#f8f9f6", html)
        self.assertLess(html.index("秋栗"), html.index("狼卫"))
        self.assertEqual(html.count('class="operator-catalog-item rarity-'), 3)

    def test_build_fz_weapon_catalog_groups_by_weapon_type(self):
        view = build_fz_weapon_catalog_view(_sample_fz_weapon_catalog())

        self.assertEqual(view.total_count, 2)
        self.assertEqual([group.name for group in view.groups], ["单手剑", "双手剑"])
        self.assertEqual(view.groups[0].items[0].terms_skill, ["切骨"])

        filtered = build_fz_weapon_catalog_view(_sample_fz_weapon_catalog(), "双手剑")
        self.assertEqual(filtered.title, "双手剑武器")
        self.assertEqual(filtered.total_count, 1)
        self.assertEqual(filtered.groups[0].items[0].name, "工业零点一")

    def test_render_weapon_catalog_card_html_has_reference_gallery_structure(self):
        view = build_fz_weapon_catalog_view(_sample_fz_weapon_catalog())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_weapon_catalog_card_html(view))

        self.assertIn('class="weapon-catalog-card"', html)
        self.assertIn("按武器类型分类", html)
        self.assertIn("ATK 505", html)
        self.assertIn("切骨", html)
        self.assertIn("#f8f9f6", html)
        self.assertNotIn('class="weapon-type-icon"', html)
        self.assertNotIn("weapon-substrate-icon", html)
        self.assertEqual(html.count('class="weapon-catalog-item rarity-'), 2)

    def test_build_weapon_view_extracts_fz_wiki_weapon_data(self):
        view = build_weapon_view(_sample_weapon(), _sample_richtext())

        self.assertEqual(view.name, "赤缨")
        self.assertEqual(view.weapon_id, "wpn_claym_0017")
        self.assertEqual(view.english_name, "Amaranthine Tassel")
        self.assertEqual(view.weapon_type, "双手剑")
        self.assertEqual(view.rarity, 6)
        self.assertEqual(view.max_atk, 510)
        self.assertEqual(view.icon_url, "https://assets.fz.wiki/c3338b6b5f3d4283/ddd4730dd6caaff8.png@raw")
        self.assertEqual(
            view.rich_text_links["ba.physicalvul"]["iconPath"],
            "https://assets.fz.wiki/c40f3979bc72cf80/e82f5eb3144df5e3.png@raw",
        )
        self.assertEqual(view.rich_text_links["ba.conduct"]["iconPath"], "data:image/png;base64,")
        self.assertEqual(len(view.skills), 3)
        self.assertEqual(view.skills[2].title, "巧技·赤断")
        self.assertIn("ba.physicalvul", view.rich_text_links)

    def test_build_warfarin_weapon_view_extracts_backup_data(self):
        view = service.build_warfarin_weapon_view(_sample_warfarin_weapon())

        self.assertEqual(view.name, "典范")
        self.assertEqual(view.source_name, "Warfarin Wiki")
        self.assertEqual(view.weapon_type, "双手剑")
        self.assertEqual(view.max_atk, 512)
        self.assertEqual(view.icon_url, "https://static.warfarin.wiki/v4/itemicon/wpn_claym_0004.webp")
        self.assertEqual(view.skills[0].levels[-1].values["mainattr"], 132)
        self.assertIn("ba.physicalvul", view.rich_text_links)

    def test_render_weapon_card_html_contains_preview_layout_and_rich_icons(self):
        view = build_weapon_view(_sample_weapon(), _sample_richtext())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
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

    def test_build_fz_operator_view_extracts_supported_schema(self):
        view = service.build_fz_operator_view(
            {
                "article": {"title": "干员/陈千语", "updatedAt": "2026-07-08T00:00:00.000Z"},
                "revision": {
                    "contentJson": {
                        "content": [
                            {
                                "attrs": {
                                    "hero": {
                                        "name": "陈千语",
                                        "nameEn": "Chen Qianyu",
                                        "rarity": 5,
                                        "profession": "近卫",
                                        "damageType": "物理",
                                        "weaponType": "单手剑",
                                        "tags": ["输出"],
                                    },
                                    "skills": {
                                        "skills": [
                                            {
                                                "name": "归穹宇",
                                                "description": "造成物理伤害。",
                                                "levels": [
                                                    {
                                                        "level": 9,
                                                        "values": {"damage": "100%"},
                                                        "cooldown": 12,
                                                        "cost": 18,
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                }
                            }
                        ]
                    }
                },
            }
        )

        self.assertEqual(view.name, "陈千语")
        self.assertEqual(view.profession, "近卫")
        self.assertEqual(view.source_version, "2026-07-08")
        self.assertEqual(view.skills[0].title, "归穹宇")

    def test_build_fz_operator_view_maps_perlica_schema(self):
        view = service.build_fz_operator_view(_sample_fz_operator(), _sample_richtext())

        self.assertEqual(view.name, "佩丽卡")
        self.assertEqual(view.english_name, "Perlica")
        self.assertEqual(view.profession, "术师")
        self.assertEqual(view.damage_type, "电磁")
        self.assertEqual(view.weapon_type, "施术单元")
        self.assertEqual(view.species_label, "种族")
        self.assertEqual(view.species, "黎博利")
        self.assertEqual(view.tags, ["电磁附着", "导电"])
        self.assertEqual(view.portrait_url, "data:image/png;base64,")
        self.assertEqual([skill.category for skill in view.skills], ["普攻", "战技", "连携技", "终结技"])
        self.assertEqual(view.skills[0].icon_id, "data:image/png;base64,")
        self.assertEqual([level.label for level in view.skills[0].levels], ["Lv7", "Lv8", "Lv9", "M1"])

        metric_names = set()
        for skill in view.skills:
            for level in skill.levels:
                metric_names.update(level.values)
        self.assertIn("攻击倍率", metric_names)
        self.assertIn("失衡值", metric_names)
        self.assertIn("持续时间", metric_names)
        self.assertIn("技力", metric_names)
        self.assertNotIn("atk_scale", metric_names)
        self.assertNotIn("poise", metric_names)
        self.assertNotIn("usp", metric_names)
        self.assertNotIn("atb", metric_names)
        self.assertIn("37%", view.skills[0].levels[0].values["攻击倍率"])
        self.assertEqual(view.skills[3].levels[-1].cost, "80")
        self.assertEqual(view.skills[3].levels[-1].cooldown, "10s")
        self.assertEqual(view.skills[3].levels[-1].values["所需能量"], "80")
        self.assertEqual(view.skills[3].levels[-1].values["冷却"], "10s")
        self.assertEqual(view.skills[3].levels[-1].values["攻击倍率"], "1000%")
        self.assertNotIn("关键数值", view.skills[1].description)
        combat_rows = dict(draw.skill_metric_rows(view.skills[1]))
        self.assertEqual(combat_rows["消耗一层破防时技力恢复"][-1], "5")
        self.assertEqual(combat_rows["消耗二层破防时技力恢复"][-1], "15")
        self.assertNotIn("关键数值", view.skills[2].description)
        combo_rows = dict(draw.skill_metric_rows(view.skills[2]))
        self.assertEqual(combo_rows["第一段技力恢复"][-1], "5")
        self.assertEqual(combo_rows["第二段技力恢复"][-1], "7")
        self.assertEqual(view.term_styles["ba.vup"].color, "#9eb7ff")
        self.assertEqual(view.term_styles["ba.conduct"].icon_url, "data:image/png;base64,")

    def test_build_fz_operator_view_uses_condition_descriptions_for_multiform_skill(self):
        raw = _sample_fz_operator()
        combo = raw["revision"]["contentJson"]["content"][0]["attrs"]["skills"]["skills"][2]
        combo["desc"] = ""
        combo["conditions"] = [
            {
                "name": "阵诀·智",
                "postDesc": "<@ba.key>阵诀·智</>：\n- 命中<@ba.key>囹圄</>目标时返还技力。",
            },
            {
                "name": "阵诀·意",
                "postDesc": "<@ba.key>阵诀·意</>：\n- 施放时牵引敌人。",
            },
        ]

        view = service.build_fz_operator_view(raw, _sample_richtext())
        skill = view.skills[2]

        self.assertEqual(skill.description, "")
        self.assertEqual(
            skill.form_descriptions,
            [
                ("阵诀·智", "命中<@ba.key>囹圄</>目标时返还技力。"),
                ("阵诀·意", "施放时牵引敌人。"),
            ],
        )
        html = draw.skill_form_descriptions(skill, draw.merged_term_styles(view), {})
        self.assertIn('<div class="skill-form-name">阵诀·智</div>', html)
        self.assertIn("命中", html)
        self.assertIn("囹圄", html)
        self.assertIn('<div class="skill-form-name">阵诀·意</div>', html)
        self.assertNotIn("- ", html)

    def test_fz_skill_metric_rows_keep_specific_param_table_rows(self):
        raw = _sample_fz_operator()
        skills = raw["revision"]["contentJson"]["content"][0]["attrs"]["skills"]["skills"]
        skills[0]["paramTable"] = {
            "rows": [
                {"label": "普攻第一段倍率", "values": ["36%"] * 10},
                {"label": "普攻第二段倍率", "values": ["54%"] * 10},
                {"label": "普攻第三段倍率", "values": ["56%"] * 10},
                {"label": "普攻第四段倍率", "values": ["88%"] * 10},
                {"label": "普攻第五段倍率", "values": ["119%"] * 10},
                {"label": "处决攻击倍率", "values": ["900%"] * 10},
                {"label": "下落攻击倍率", "values": ["180%"] * 10},
            ]
        }
        skills[1]["paramTable"] = {
            "rows": [
                {"label": "技力消耗", "values": ["100"] * 10},
                {"label": "初始爆炸伤害倍率", "values": ["140%"] * 10},
                {"label": "初始爆炸失衡值", "values": ["10"] * 10},
                {"label": "持续伤害每段倍率", "values": ["14%"] * 10},
                {"label": "追加伤害倍率", "values": ["770%"] * 10},
                {"label": "追加攻击获得终结技能量", "values": ["100"] * 10},
            ]
        }
        skills[2]["paramTable"] = {
            "rows": [
                {"label": "冷却", "values": ["9s"] * 10},
                {"label": "攻击倍率", "values": ["540%"] * 10},
                {"label": "失衡值", "values": ["10"] * 10},
                {"label": "命中1个敌人获得终结技能量", "values": ["25"] * 10},
            ]
        }
        skills[3]["paramTable"] = {
            "rows": [
                {"label": "所需终结技能量", "values": ["300"] * 10},
                {"label": "冷却", "values": ["10s"] * 10},
                {"label": "持续时间（秒）", "values": ["15"] * 10},
                {"label": "强化普攻第一段倍率", "values": ["146%"] * 10},
            ]
        }
        view = service.build_fz_operator_view(raw, _sample_richtext())

        normal_rows = dict(draw.skill_metric_rows(view.skills[0]))
        self.assertIn("普攻第五段倍率", normal_rows)
        self.assertIn("处决攻击倍率", normal_rows)
        self.assertIn("下落攻击倍率", normal_rows)
        self.assertNotIn("攻击倍率", normal_rows)

        combat_rows = dict(draw.skill_metric_rows(view.skills[1]))
        self.assertIn("初始爆炸伤害倍率", combat_rows)
        self.assertIn("持续伤害每段倍率", combat_rows)
        self.assertIn("追加伤害倍率", combat_rows)
        self.assertIn("追加攻击获得终结技能量", combat_rows)
        self.assertNotIn("攻击倍率", combat_rows)

        combo_row_list = draw.skill_metric_rows(view.skills[2])
        self.assertEqual(combo_row_list[0][0], "冷却")
        combo_rows = dict(combo_row_list)
        self.assertEqual(combo_rows["冷却"][-1], "9s")
        self.assertEqual(combo_rows["攻击倍率"][-1], "540%")
        self.assertEqual(combo_rows["失衡值"][-1], "10")
        self.assertEqual(combo_rows["命中1个敌人获得终结技能量"][-1], "25")

        ultimate_rows = dict(draw.skill_metric_rows(view.skills[3]))
        self.assertIn("持续时间（秒）", ultimate_rows)
        self.assertIn("强化普攻第一段倍率", ultimate_rows)
        self.assertNotIn("所需能量", ultimate_rows)
        self.assertNotIn("冷却", ultimate_rows)
        self.assertNotIn("持续时间", ultimate_rows)

    def test_build_fz_operator_view_uses_raw_fz_asset_urls(self):
        raw = _sample_fz_operator()
        attrs = raw["revision"]["contentJson"]["content"][0]["attrs"]
        hero = attrs["hero"]
        hero["iconUrl"] = "https://assets.fz.wiki/upload/characters/icon/yvonne.png"
        hero["roundIconUrl"] = "https://assets.fz.wiki/upload/characters/round/yvonne.png?size=round"
        hero["portraitFile"] = "https://assets.fz.wiki/upload/characters/illust/yvonne.png@raw"
        attrs["skills"]["skills"][0]["icon"]["glyph"]["url"] = "https://assets.fz.wiki/upload/skills/yvonne_s1.png"
        for talent in attrs["talents"]["talents"]:
            talent["iconUrl"] = "https://assets.fz.wiki/upload/talents/yvonne_talent.png"
        attrs["potentials"]["potentials"][0]["iconUrl"] = "https://assets.fz.wiki/upload/potentials/yvonne_p1.png"

        view = service.build_fz_operator_view(raw, _sample_richtext())

        self.assertEqual(view.icon_url, "https://assets.fz.wiki/upload/characters/icon/yvonne.png@raw")
        self.assertEqual(view.round_icon_url, "https://assets.fz.wiki/upload/characters/round/yvonne.png@raw?size=round")
        self.assertEqual(view.portrait_url, "https://assets.fz.wiki/upload/characters/illust/yvonne.png@raw")
        self.assertEqual(view.skills[0].icon_id, "https://assets.fz.wiki/upload/skills/yvonne_s1.png@raw")
        self.assertEqual(view.talents[0].icon_url, "https://assets.fz.wiki/upload/talents/yvonne_talent.png@raw")
        self.assertEqual(view.potentials[0].icon_url, "https://assets.fz.wiki/upload/potentials/yvonne_p1.png@raw")
        self.assertEqual(
            view.term_styles["ba.physicalvul"].icon_url,
            "https://assets.fz.wiki/c40f3979bc72cf80/e82f5eb3144df5e3.png@raw",
        )
        self.assertEqual(view.term_styles["ba.conduct"].icon_url, "data:image/png;base64,")

    def test_build_fz_operator_view_falls_back_to_faction_without_archive_species(self):
        raw = _sample_fz_operator()
        attrs = raw["revision"]["contentJson"]["content"][0]["attrs"]
        attrs.pop("archive")

        view = service.build_fz_operator_view(raw)

        self.assertEqual(view.species_label, "所属")
        self.assertEqual(view.species, "终末地工业")

    def test_build_fz_operator_view_formats_talents_and_potentials(self):
        view = service.build_fz_operator_view(_sample_fz_operator())

        self.assertEqual([talent.title for talent in view.talents], ["歼灭协议"])
        self.assertIn("+30%", view.talents[0].description)
        self.assertNotIn("{dmg:0%}", view.talents[0].description)
        self.assertEqual(view.potentials[0].title, "P1 危机处理")
        self.assertEqual(view.potentials[1].title, "P2 谈判策略")
        self.assertIn("+75%", view.potentials[0].description)
        self.assertIn("-15%", view.potentials[1].description)
        for effect in [*view.talents, *view.potentials]:
            self.assertNotIn("{", effect.description)
            self.assertNotIn("}", effect.description)

    def test_render_fz_operator_card_html_uses_mastery_level_labels(self):
        view = service.build_fz_operator_view(_sample_fz_operator(), _sample_richtext())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_operator_card_html(view))

        self.assertIn("Lv7", html)
        self.assertIn("Lv8", html)
        self.assertIn("Lv9", html)
        self.assertNotIn("Lv10", html)
        self.assertIn("<span>M1</span>", html)
        self.assertNotIn("<span>M2</span>", html)
        self.assertNotIn("<span>M3</span>", html)
        self.assertIn("攻击倍率", html)
        self.assertNotIn("atk_scale", html)
        self.assertIn("所需能量 <strong>80</strong>", html)
        self.assertIn("冷却 <strong>10s</strong>", html)
        self.assertIn("种族", html)
        self.assertIn("黎博利", html)
        ultimate_html = html[html.index('alt="协议ε·70.41κ"'):]
        ultimate_html = ultimate_html[: ultimate_html.index("</article>")]
        self.assertNotIn('<div class="metric-name">所需能量</div>', ultimate_html)
        self.assertNotIn('<div class="metric-name">冷却</div>', ultimate_html)
        self.assertIn(draw.normalize_rich_color("#9eb7ff"), html)
        self.assertIn(draw.normalize_rich_color("#33c2ff"), html)
        self.assertIn(draw.normalize_rich_color("#ffcc00"), html)
        self.assertIn("term-icon", html)
        self.assertNotIn("&lt;@ba.vup", html)
        self.assertNotIn("<@ba.vup", html)
        self.assertNotIn("margin-top: auto", html)
        self.assertIn(f"left: {draw.OPERATOR_ACCENT_LEFT}px", html)
        self.assertIn(f"min-height: {draw.OPERATOR_RAIL_HEIGHT}px", html)
        self.assertIn("height: auto", html)
        self.assertIn("align-content: start", html)
        self.assertNotIn("align-content: space-between", html)
        self.assertNotIn("flex: 1 1 auto", html)
        self.assertIn("align-self: start", html)
        self.assertIn("Math.ceil(rail.scrollHeight) + 56", html)
        self.assertNotIn("max-height: calc(100% - 56px)", html)
        self.assertNotIn("min-height: 244px", html)

    def test_rich_colors_meet_card_contrast_requirement(self):
        normalized = draw.normalize_rich_color("#9eb7ff")
        red, green, blue = (int(normalized[index:index + 2], 16) / 255 for index in (1, 3, 5))
        background = (247 / 255, 248 / 255, 246 / 255)
        self.assertGreaterEqual(draw._contrast_ratio((red, green, blue), background), 4.49)

    def test_metric_label_width_uses_three_display_width_buckets(self):
        short = models.SkillView("short", "短", levels=[models.SkillLevelView("Lv1", 1, {"倍率": "1"})])
        medium = models.SkillView("medium", "中", levels=[models.SkillLevelView("Lv1", 1, {"处决攻击倍率": "1"})])
        long = models.SkillView("long", "长", levels=[models.SkillLevelView("Lv1", 1, {"终结技期间燃烧失衡值": "1"})])
        self.assertEqual(draw.metric_label_width(short), 92)
        self.assertEqual(draw.metric_label_width(medium), 108)
        self.assertEqual(draw.metric_label_width(long), 124)

    def test_weapon_width_uses_all_four_complexity_buckets(self):
        def weapon(description, *, skills=3):
            return models.WeaponView(
                name="测试",
                slug="test",
                title="武器/测试",
                skills=[
                    models.WeaponSkillView(
                        f"技能{index}",
                        description,
                        [models.WeaponSkillLevelView(level, {}) for level in range(1, 10)],
                    )
                    for index in range(skills)
                ],
            )

        self.assertEqual(draw.weapon_card_width(weapon("短文本", skills=2)), 1360)
        self.assertEqual(draw.weapon_card_width(weapon("中" * 30)), 1440)
        self.assertEqual(draw.weapon_card_width(weapon("长" * 40)), 1520)
        self.assertEqual(draw.weapon_card_width(weapon("极长" * 25)), 1600)

    def test_weapon_operator_names_keep_owner_box_compact(self):
        self.assertEqual(draw.weapon_operator_names([]), "通用")
        self.assertEqual(draw.weapon_operator_names(["弭弗"]), "弭弗")
        self.assertEqual(draw.weapon_operator_names(["弭弗", "余烬", "昼雪"]), "弭弗、余烬、昼雪")
        self.assertEqual(draw.weapon_operator_names(["余烬", "昼雪", "别礼", "大潘"]), "余烬、昼雪等4名")

    def test_portrait_override_and_analysis_stay_within_bounds(self):
        override = asyncio.run(draw._portrait_layout(models.OperatorView("莱万汀", "", ""), b"ignored"))
        self.assertEqual(override, draw.PortraitLayout(50.0, 46.0, 1.12))
        buffer = io.BytesIO()
        Image.new("RGBA", (120, 200), (0, 0, 0, 0)).save(buffer, format="PNG")
        image = Image.open(io.BytesIO(buffer.getvalue()))
        for x in range(35, 85):
            for y in range(30, 180):
                image.putpixel((x, y), (255, 80, 60, 255))
        rendered = io.BytesIO()
        image.save(rendered, format="PNG")
        layout = draw._analyze_portrait_layout(rendered.getvalue())
        self.assertTrue(35 <= layout.x <= 65)
        self.assertTrue(30 <= layout.y <= 58)
        self.assertTrue(1.05 <= layout.scale <= 1.18)

    def test_png_container_optimization_is_lossless(self):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), "#f5c900").save(buffer, format="PNG", pnginfo=None)
        original = buffer.getvalue()
        optimized = draw.optimize_png_container(original)
        self.assertEqual(Image.open(io.BytesIO(original)).tobytes(), Image.open(io.BytesIO(optimized)).tobytes())
        self.assertTrue(optimized.startswith(b"\x89PNG"))


class _FakeWarfarinClient:
    def __init__(
        self,
        *,
        search_data=None,
        operators_data=None,
        weapons_data=None,
        operator_detail_data=None,
        operator_details_data=None,
        weapon_detail_data=None,
        fz_summaries_data=None,
        fz_search_data=None,
        fz_article_data=None,
        fz_richtext_data=None,
    ):
        self._search = search_data if search_data is not None else {}
        self._operators = operators_data if operators_data is not None else {}
        self._weapons = weapons_data if weapons_data is not None else {}
        self._operator_detail = operator_detail_data if operator_detail_data is not None else {}
        self._operator_details = operator_details_data if operator_details_data is not None else {}
        self._weapon_detail = weapon_detail_data if weapon_detail_data is not None else {}
        self._fz_summaries = fz_summaries_data if fz_summaries_data is not None else {}
        self._fz_search = fz_search_data if fz_search_data is not None else {}
        self._fz_article = fz_article_data if fz_article_data is not None else {}
        self._fz_richtext = fz_richtext_data if fz_richtext_data is not None else {}

    async def search(self, query, *, lang="cn"):
        return self._search

    async def operator_detail(self, slug, *, lang="cn"):
        if slug in self._operator_details:
            detail = self._operator_details[slug]
            if isinstance(detail, Exception):
                raise detail
            return detail
        return self._operator_detail

    async def weapon_detail(self, slug, *, lang="cn"):
        return self._weapon_detail

    async def operators(self, *, lang="cn"):
        return self._operators

    async def weapons(self, *, lang="cn"):
        return self._weapons

    async def fz_article_summaries(self, prefix, *, ns=0):
        if isinstance(self._fz_summaries, Exception):
            raise self._fz_summaries
        return self._fz_summaries

    async def fz_search(self, query, *, limit=8):
        if isinstance(self._fz_search, Exception):
            raise self._fz_search
        return self._fz_search

    async def fz_article_by_title(self, title, *, ns=0, with_revision=True):
        if isinstance(self._fz_article, Exception):
            raise self._fz_article
        return self._fz_article

    async def fz_game_richtext(self):
        if isinstance(self._fz_richtext, Exception):
            raise self._fz_richtext
        return self._fz_richtext


class EndfieldSlugResolutionTests(unittest.TestCase):
    @staticmethod
    def _weapon_operator_detail(name, *, default_weapon="", recommended=()):
        return {
            "meta": {"name": name},
            "data": {
                "characterTable": {"defaultWeaponId": default_weapon},
                "charWpnRecommendTable": {"weaponIds1": list(recommended)},
            },
        }

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

    def test_operator_list_fallback_accepts_fuzzy_name(self):
        client = _FakeWarfarinClient(
            search_data={"results": []},
            operators_data={"data": [{"slug": "mifu", "name": "弭弗"}]},
        )
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_operator_slug("弥弗")), "mifu")

    def test_weapon_list_fallback_accepts_fuzzy_name(self):
        client = _FakeWarfarinClient(
            search_data={"results": []},
            weapons_data={"data": [{"slug": "chiying", "name": "赤缨"}]},
        )
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_weapon_slug("赤樱")), "chiying")

    def test_list_fallback_rejects_ambiguous_fuzzy_name(self):
        client = _FakeWarfarinClient(
            search_data={"results": []},
            operators_data={"data": [{"slug": "mifu", "name": "弭弗"}, {"slug": "mifu-alt", "name": "米弗"}]},
        )
        svc = service.EndfieldService(client)
        self.assertIsNone(asyncio.run(svc.find_operator_slug("弥弗")))

    def test_no_match_returns_none(self):
        client = _FakeWarfarinClient(
            search_data={"results": []},
            operators_data={"data": [{"slug": "camille", "name": "卡缪"}]},
        )
        svc = service.EndfieldService(client)
        self.assertIsNone(asyncio.run(svc.find_operator_slug("不存在")))

    def test_fz_operator_title_prefers_fz_summaries(self):
        client = _FakeWarfarinClient(
            fz_summaries_data={"articles": [{"title": "干员/陈千语"}]},
            fz_search_data={"hits": []},
        )
        svc = service.EndfieldService(client)
        self.assertEqual(asyncio.run(svc.find_fz_operator_title("陈千语")), "干员/陈千语")

    def test_fz_operator_view_loads_richtext_styles(self):
        client = _FakeWarfarinClient(
            fz_article_data=_sample_fz_operator(),
            fz_richtext_data=_sample_richtext(),
        )
        svc = service.EndfieldService(client)
        view = asyncio.run(svc.get_operator_view_from_fz("佩丽卡"))

        self.assertIsNotNone(view)
        self.assertEqual(view.term_styles["ba.key"].color, "#33c2ff")
        self.assertEqual(view.term_styles["ba.conduct"].icon_url, "data:image/png;base64,")

    def test_weapon_operator_names_prefer_default_weapon_users(self):
        client = _FakeWarfarinClient(
            weapons_data={"data": [{"id": "wpn_claym_0017", "name": "赤缨", "weaponType": 3}]},
            operators_data={
                "data": [
                    {"slug": "ember", "name": "余烬", "weaponType": 3},
                    {"slug": "mifu", "name": "弭弗", "weaponType": 3},
                    {"slug": "perlica", "name": "佩丽卡", "weaponType": 1},
                ]
            },
            operator_details_data={
                "ember": self._weapon_operator_detail("余烬", default_weapon="wpn_claym_0017"),
                "mifu": self._weapon_operator_detail("弭弗", recommended=("wpn_claym_0017",)),
            },
        )
        svc = service.EndfieldService(client)
        view = models.WeaponView("赤缨", "amaranthine-tassel", "武器/赤缨", weapon_id="wpn_claym_0017")

        self.assertEqual(asyncio.run(svc.find_weapon_operator_names(view)), ["余烬"])

    def test_weapon_operator_names_fall_back_to_recommendations(self):
        client = _FakeWarfarinClient(
            weapons_data={"data": [{"id": "wpn_claym_0017", "name": "赤缨", "weaponType": 3}]},
            operators_data={"data": [{"slug": "mifu", "name": "弭弗", "weaponType": 3}]},
            operator_details_data={
                "mifu": self._weapon_operator_detail("弭弗", recommended=("wpn_claym_0017",)),
            },
        )
        svc = service.EndfieldService(client)
        view = models.WeaponView("赤缨", "amaranthine-tassel", "武器/赤缨")

        self.assertEqual(asyncio.run(svc.find_weapon_operator_names(view)), ["弭弗"])

    def test_weapon_card_uses_generic_owner_when_relation_lookup_fails(self):
        view = build_weapon_view(_sample_weapon(), _sample_richtext())
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            html = asyncio.run(render_weapon_card_html(view))

        self.assertIn("所属干员", html)
        self.assertIn("通用", html)
        self.assertNotIn("稀有度", html)
        self.assertEqual(html.count('class="rarity-star"'), view.rarity)

    def test_operator_view_falls_back_to_warfarin_when_fz_fails(self):
        client = _FakeWarfarinClient(
            search_data={"results": [{"slug": "chen-qianyu", "type": "operators"}]},
            operators_data={"data": []},
            operator_detail_data=_sample_operator(),
            fz_article_data=service.WarfarinAPIError("FZ down"),
        )
        svc = service.EndfieldService(client)
        view = asyncio.run(svc.get_operator_view("干员/陈千语"))

        self.assertIsNotNone(view)
        self.assertEqual(view.name, "陈千语")

    def test_weapon_view_falls_back_to_warfarin_when_fz_fails(self):
        client = _FakeWarfarinClient(
            search_data={"results": [{"slug": "exemplar", "name": "典范", "type": "weapons"}]},
            weapons_data={"data": []},
            weapon_detail_data=_sample_warfarin_weapon(),
            fz_article_data=service.WarfarinAPIError("FZ down"),
        )
        svc = service.EndfieldService(client)
        view = asyncio.run(svc.get_weapon_view("武器/典范"))

        self.assertIsNotNone(view)
        self.assertEqual(view.name, "典范")
        self.assertEqual(view.source_name, "Warfarin Wiki")


if __name__ == "__main__":
    unittest.main()
