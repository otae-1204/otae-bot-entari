from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import date
from unittest import mock
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "endfield_account_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[PACKAGE] = package

crypto = _load(f"{PACKAGE}.account.crypto", "plugins/endfield/account/crypto.py")
store_module = _load(f"{PACKAGE}.account.store", "plugins/endfield/account/store.py")
xhh_module = _load(f"{PACKAGE}.gacha.xhh", "plugins/endfield/gacha/xhh.py")
client_module = _load(f"{PACKAGE}.account.client", "plugins/endfield/account/client.py")
currency_module = _load(f"{PACKAGE}.account.currency.service", "plugins/endfield/account/currency/service.py")
gacha_module = _load(f"{PACKAGE}.gacha.service", "plugins/endfield/gacha/service.py")
gacha_assets_module = sys.modules[f"{PACKAGE}.gacha.assets"]
models_module = _load(f"{PACKAGE}.catalog.models", "plugins/endfield/catalog/models.py")
aliases_module = _load(f"{PACKAGE}.catalog.aliases", "plugins/endfield/catalog/aliases.py")
sources_module = _load(f"{PACKAGE}.providers.registry", "plugins/endfield/providers/registry.py")
commands_module = _load(f"{PACKAGE}.catalog.commands", "plugins/endfield/catalog/commands.py")
draw_module = _load(f"{PACKAGE}.rendering.cards", "plugins/endfield/rendering/cards.py")
account_detail_models_module = _load(
    f"{PACKAGE}.account.detail.models", "plugins/endfield/account/detail/models.py"
)
account_detail_module = _load(
    f"{PACKAGE}.account.detail.service", "plugins/endfield/account/detail/service.py"
)
account_detail_names_module = sys.modules[f"{PACKAGE}.account.detail.names"]
account_base_models_module = _load(
    f"{PACKAGE}.account.base.models", "plugins/endfield/account/base/models.py"
)
account_base_service_module = _load(
    f"{PACKAGE}.account.base.service", "plugins/endfield/account/base/service.py"
)
account_base_draw_module = _load(
    f"{PACKAGE}.account.base.draw", "plugins/endfield/account/base/draw.py"
)
COMPACT_THRESHOLD = account_detail_models_module.COMPACT_THRESHOLD


class EndfieldPersonalCommandTests(unittest.TestCase):
    def test_parses_account_and_attendance_commands(self):
        self.assertEqual(commands_module.parse_command("绑定").action, "bind")
        self.assertEqual(commands_module.parse_command("添加账号").action, "bind")
        self.assertEqual(commands_module.parse_command("add-account").action, "bind")
        self.assertEqual(commands_module.parse_command("账号").action, "accounts")
        primary = commands_module.parse_command("主账号 2")
        self.assertEqual((primary.action, primary.account_selector), ("primary", "2"))
        attendance = commands_module.parse_command("签到 UID1234")
        self.assertEqual((attendance.action, attendance.account_selector), ("attendance", "UID1234"))

    def test_parses_currency_log_filters_and_dates(self):
        parsed = commands_module.parse_command("资源流水 2 2026-08-01 2026-08-12 源石 消耗")
        self.assertEqual(parsed.action, "currency_log")
        self.assertEqual(parsed.account_selector, "2")
        self.assertEqual((parsed.currency_types, parsed.change_type), ((1,), 2))
        self.assertEqual((parsed.start_date, parsed.end_date), ("2026-08-01", "2026-08-12"))

        ranged = commands_module.parse_command("流水 --资源 源石,嵌晶玉 --类型 获取 2026/08/01~2026/08/12")
        self.assertEqual((ranged.currency_types, ranged.change_type), ((1, 2), 1))
        self.assertEqual((ranged.start_date, ranged.end_date), ("2026-08-01", "2026-08-12"))

        all_history = commands_module.parse_command("流水 2 -a --资源 源石")
        self.assertEqual((all_history.account_selector, all_history.currency_types), ("2", (1,)))
        self.assertTrue(all_history.all_history)

        days = commands_module.parse_command("流水 2 -d 7 --资源 源石")
        self.assertEqual((days.account_selector, days.currency_types, days.days), ("2", (1,), 7))
        self.assertEqual(commands_module.parse_command("流水 --天数=30").days, 30)

    def test_currency_log_defaults_to_one_calendar_month(self):
        start, end = currency_module.resolve_query_dates(today=date(2026, 8, 31))
        self.assertEqual((start, end), (date(2026, 7, 31), date(2026, 8, 31)))
        self.assertEqual(
            currency_module.resolve_query_dates(days=7, today=date(2026, 8, 31)),
            (date(2026, 8, 25), date(2026, 8, 31)),
        )

    def test_all_history_period_label_uses_earliest_backed_up_record(self):
        earliest, _ = currency_module.date_bounds(date(2026, 5, 14), date(2026, 5, 14))
        later, _ = currency_module.date_bounds(date(2026, 8, 13), date(2026, 8, 13))
        records = (
            client_module.CurrencyLogItem(1, 1, "13", 100, 200, later, 2),
            client_module.CurrencyLogItem(1, 2, "10", 20, 180, earliest, 1),
        )

        self.assertEqual(
            currency_module.format_all_history_period_label(
                records,
                end=date(2026, 8, 13),
            ),
            "2026/05/14-如今",
        )
        self.assertIn(
            "武库配额最早：2026/07/16；接口约30天，更早仅限本地备份",
            currency_module.format_all_history_period_label(
                records,
                end=date(2026, 8, 13),
                quota_start=date(2026, 7, 16),
            ),
        )

    def test_rejects_invalid_currency_log_options(self):
        self.assertTrue(commands_module.parse_command("流水 --资源").error)
        self.assertTrue(commands_module.parse_command("流水 2026-99-99").error)
        self.assertTrue(commands_module.parse_command("流水 -d 0").error)
        self.assertTrue(commands_module.parse_command("流水 -d 7 2026-08-01").error)
        self.assertTrue(commands_module.parse_command("流水 -d 7 -a").error)

    def test_parses_gacha_history_page_pool_and_full(self):
        history = commands_module.parse_command("抽卡记录 小明 3 --池 联合寻访")
        self.assertEqual(history.action, "gacha_history")
        self.assertEqual(history.account_selector, "小明")
        self.assertEqual(history.page, 3)
        self.assertEqual(history.pool_filter, "联合寻访")
        sync = commands_module.parse_command("抽卡同步 2 --full")
        self.assertEqual((sync.action, sync.account_selector, sync.full), ("gacha_sync", "2", True))
        account_only = commands_module.parse_command("抽卡记录 2")
        self.assertEqual((account_only.account_selector, account_only.page), ("2", 1))
        imported = commands_module.parse_command("小黑盒导入 2")
        self.assertEqual((imported.action, imported.account_selector), ("gacha_import", "2"))

    def test_rejects_invalid_page_and_missing_pool(self):
        self.assertTrue(commands_module.parse_command("抽卡记录 主账号 0").error)
        self.assertTrue(commands_module.parse_command("抽卡记录 --池").error)

    def test_account_command_keeps_optional_selector_without_error(self):
        bare = commands_module.parse_command("账号")
        self.assertEqual((bare.action, bare.account_selector, bare.error), ("accounts", "", ""))
        numbered = commands_module.parse_command("账号 2")
        self.assertEqual((numbered.action, numbered.account_selector), ("accounts", "2"))
        named = commands_module.parse_command("账户 小明")
        self.assertEqual((named.action, named.account_selector), ("accounts", "小明"))
        self.assertEqual(commands_module.parse_command("accounts").account_selector, "")

    def test_parses_account_base_command_and_selector(self):
        bare = commands_module.parse_command("账号 基建")
        self.assertEqual((bare.action, bare.account_selector), ("account_base", ""))
        numbered = commands_module.parse_command("账号 基建 2")
        self.assertEqual((numbered.action, numbered.account_selector), ("account_base", "2"))
        named = commands_module.parse_command("账户 帝江号 小明")
        self.assertEqual((named.action, named.account_selector), ("account_base", "小明"))

    def test_help_documents_account_detail_command(self):
        help_text = commands_module.format_help()
        self.assertIn("/ef 账号 [编号]", help_text)
        self.assertIn("/ef 账号 基建 [账号]", help_text)
        self.assertIn("/ef 添加账号", help_text)
        self.assertIn("可重复追加多个账号", help_text)


def account_detail_fixture() -> dict:
    def skill(
        skill_id: str,
        name: str,
        type_key: str,
        type_value: str,
        property_key: str,
        property_value: str,
    ) -> dict:
        return {
            "id": skill_id,
            "name": name,
            "type": {"key": type_key, "value": type_value},
            "property": {"key": property_key, "value": property_value},
            "iconUrl": f"https://assets.invalid/skill-{skill_id}.png",
        }

    skills = [
        skill(
            "normal", "普通攻击", "skill_type_normal_attack", "普通攻击",
            "skill_property_physical", "物理",
        ),
        skill(
            "battle", "战技", "skill_type_normal_skill", "战技",
            "skill_property_fire", "灼热",
        ),
        skill(
            "combo", "连携技", "skill_type_combo_skill", "连携技",
            "skill_property_natural", "自然",
        ),
        skill(
            "ultimate", "终结技", "skill_type_ultimate_skill", "终结技",
            "skill_property_pulse", "电磁",
        ),
    ]

    def equip(name: str, rarity_key: str, type_value: str, suit: dict | None) -> dict:
        return {
            "equipData": {
                "name": name,
                "iconUrl": f"https://assets.invalid/{name}.png",
                "rarity": {"key": rarity_key, "value": "金色品质"},
                "type": {"key": "equip_type_body", "value": type_value},
                "level": {"key": "equip_level_70", "value": "70"},
                "suit": suit,
            }
        }

    suit = {"id": "suit-a", "name": "测试套组"}
    veteran = {
        "id": "char-veteran",
        "level": 90,
        "evolvePhase": 4,
        "potentialLevel": 5,
        "charData": {
            "id": "char-veteran",
            "name": "测试老兵",
            "rarity": {"key": "rarity_6", "value": "6"},
            "profession": {"key": "profession_guard", "value": "近卫"},
            "property": {"key": "char_property_cryst", "value": "寒冷"},
            "weaponType": {"key": "weapon_type_sword", "value": "单手剑"},
            "avatarSqUrl": "https://assets.invalid/veteran-square.png",
            "skills": skills,
        },
        # userSkills is emitted in a different order than charData.skills on purpose.
        "userSkills": {
            "ultimate": {"level": 12, "maxLevel": 12},
            "normal": {"level": 9, "maxLevel": 12},
            "combo": {"level": 10, "maxLevel": 12},
            "battle": {"level": 11, "maxLevel": 12},
        },
        "bodyEquip": equip("测试护甲", "equip_rarity_5", "护甲", suit),
        "armEquip": equip("测试护手", "equip_rarity_5", "护手", suit),
        "secondAccessory": equip("测试配件", "equip_rarity_4", "配件", None),
        "tacticalItem": {
            "tacticalItemData": {
                "name": "测试软饮",
                "iconUrl": "https://assets.invalid/tactical.png",
            }
        },
        "weapon": {
            "level": 90,
            "refineLevel": 5,
            "breakthroughLevel": 4,
            "weaponData": {
                "name": "测试武器",
                "iconUrl": "https://assets.invalid/weapon.png",
                "rarity": {"key": "rarity_6", "value": "6"},
                "type": {"key": "weapon_type_sword", "value": "单手剑"},
                "skills": [
                    {"key": "wpn_attr_main_high", "value": "主能力提升"},
                    {"key": "wpn_sp_attr_atk_high", "value": "攻击提升"},
                    {"key": "sk_wpn_sword_0001", "value": "测试专属技能"},
                ],
            },
            "gem": {"icon": "", "gemData": {"name": "测试基质", "icon": "https://assets.invalid/gem.png"}},
        },
    }
    rookie = {
        "id": "char-rookie",
        "level": 1,
        "evolvePhase": 0,
        "potentialLevel": 0,
        "charData": {
            "id": "char-rookie",
            "name": "测试新兵",
            "rarity": {"key": "rarity_4", "value": "4"},
            "profession": {"key": "profession_caster", "value": "术师"},
            "property": {"key": "char_property_fire", "value": "灼热"},
            "weaponType": {"key": "weapon_type_pistol", "value": "手铳"},
            "avatarSqUrl": "https://assets.invalid/rookie-square.png",
            "skills": skills,
        },
        "userSkills": {},
        "weapon": {
            "level": 1,
            "refineLevel": 0,
            "breakthroughLevel": 0,
            "weaponData": {"name": "初始武器", "rarity": {"key": "rarity_3", "value": "3"}},
            "gem": None,
        },
    }
    return {
        "base": {
            "serverName": "",
            "name": "测试管理员",
            "level": 60,
            "worldLevel": 7,
            "mainMission": {"id": "", "description": "余晖未却"},
            "avatarUrl": "https://assets.invalid/avatar.png",
            "charNum": 2,
            "weaponNum": 56,
            "docNum": 255,
            "saveTime": "1784509224",
        },
        "chars": [rookie, veteran],
        "achieve": {"count": 111},
        "dungeon": {"curStamina": "269", "maxStamina": "360", "maxTs": "1784548592"},
        "bpSystem": {"curLevel": 36, "maxLevel": 60},
        "dailyMission": {"dailyActivation": 0, "maxDailyActivation": 100},
        "weeklyMission": {"score": 0, "total": 10},
        "domain": [{"name": "武陵", "level": 23, "factory": None}],
        "crisisContract": [{"highest": 44}],
        "currentTs": "1784509280",
    }


class EndfieldAccountDetailViewTests(unittest.TestCase):
    def build(self) -> object:
        return account_detail_module.build_account_detail_view(
            account_detail_fixture(), uid="****1234", server_name="China",
            currency_balances={1: 297, 2: 52780, 3: 7500},
        )

    def test_reads_base_identity_and_account_stats(self):
        view = self.build()
        self.assertEqual(
            (view.nickname, view.uid, view.server_name, view.level, view.world_level, view.main_mission),
            ("测试管理员", "****1234", "国服", 60, 7, "余晖未却"),
        )
        stats = {stat.label: stat.value for stat in view.stats}
        self.assertEqual(
            [stat.label for stat in view.stats],
            ["理智", "日常", "周常", "通行证", "嵌晶玉", "源石", "武库配额"],
        )
        self.assertEqual(stats["理智"], "269 / 360")
        self.assertEqual(stats["日常"], "0 / 100")
        self.assertEqual(stats["周常"], "0 / 10")
        self.assertEqual(stats["通行证"], "36 / 60")
        self.assertEqual(stats["嵌晶玉"], "52780")
        self.assertEqual(stats["源石"], "297")
        self.assertEqual(stats["武库配额"], "7500")

    def test_sorts_operators_by_rarity_then_level(self):
        view = self.build()
        self.assertEqual([operator.name for operator in view.operators], ["测试老兵", "测试新兵"])
        self.assertEqual(view.operators[0].rarity, 6)
        self.assertFalse(view.compact)

    def test_sorts_equal_rarity_and_level_by_character_number(self):
        detail = account_detail_fixture()
        first = detail["chars"][0]
        second = detail["chars"][1]
        for character in (first, second):
            character["level"] = 90
            character["charData"]["rarity"] = {"key": "rarity_6", "value": "6"}
        first["charData"].update(
            id="chr_0032_lizhiyan",
            name="编号三十二",
            profession={"key": "profession_vanguard", "value": "先锋"},
        )
        second["charData"].update(
            id="chr_0005_chen",
            name="编号五",
            profession={"key": "profession_guard", "value": "近卫"},
        )

        view = account_detail_module.build_account_detail_view(detail, uid="x")

        self.assertEqual([operator.name for operator in view.operators], ["编号五", "编号三十二"])

    def test_reads_string_typed_numerics_and_progression(self):
        veteran = self.build().operators[0]
        self.assertEqual(
            (veteran.level, veteran.evolve_phase, veteran.potential_level, veteran.rarity),
            (90, 4, 5, 6),
        )
        self.assertEqual((veteran.profession, veteran.element, veteran.weapon_type), ("近卫", "寒冷", "单手剑"))
        self.assertEqual(veteran.element_color, account_detail_module.ELEMENT_COLORS["char_property_cryst"])

    def test_localizes_semantic_labels_from_keys(self):
        detail = account_detail_fixture()
        veteran = detail["chars"][1]
        veteran["charData"]["profession"]["value"] = "Guard"
        veteran["charData"]["property"]["value"] = "Cryst"
        veteran["charData"]["weaponType"]["value"] = "Sword"
        veteran["charData"]["skills"][0]["type"]["value"] = "Normal Attack"
        veteran["charData"]["skills"][0]["property"]["value"] = "Physical"
        veteran["weapon"]["weaponData"]["type"] = {
            "key": "weapon_type_sword", "value": "Sword"
        }
        veteran["bodyEquip"]["equipData"]["type"]["value"] = "Body"

        view = account_detail_module.build_account_detail_view(
            detail, uid="uid", server_name="China"
        )
        operator = view.operators[0]
        self.assertEqual(
            (view.server_name, operator.profession, operator.element, operator.weapon_type),
            ("国服", "近卫", "寒冷", "单手剑"),
        )
        self.assertEqual(operator.skills[0].type_label, "普通攻击")
        self.assertEqual(operator.skills[0].damage_type, "物理")
        self.assertEqual(operator.weapon.type_label, "单手剑")
        self.assertEqual(operator.equips[0].type_label, "护甲")

    def test_pairs_skill_levels_by_chardata_order(self):
        veteran = self.build().operators[0]
        self.assertEqual([skill.name for skill in veteran.skills], ["普通攻击", "战技", "连携技", "终结技"])
        self.assertEqual([skill.level for skill in veteran.skills], [9, 11, 10, 12])
        self.assertEqual([skill.max_level for skill in veteran.skills], [12, 12, 12, 12])

    def test_reads_per_skill_damage_property_and_ultimate_type(self):
        skills = self.build().operators[0].skills
        self.assertEqual(account_detail_module.ELEMENT_COLORS["char_property_pulse"], "#f0cf2f")
        self.assertEqual([skill.damage_type for skill in skills], ["物理", "灼热", "自然", "电磁"])
        self.assertEqual(
            [skill.damage_color for skill in skills],
            ["#969a99", "#ec654d", "#77c92f", "#f0cf2f"],
        )
        self.assertEqual([skill.is_ultimate for skill in skills], [False, False, False, True])
        self.assertEqual([skill.mastery_level for skill in skills], [0, 2, 1, 3])

    def test_keeps_four_positional_equip_slots_with_holes(self):
        veteran, rookie = self.build().operators
        self.assertEqual(len(veteran.equips), 4)
        self.assertEqual(
            [equip.name if equip else None for equip in veteran.equips],
            ["测试护甲", "测试护手", None, "测试配件"],
        )
        self.assertEqual([equip.slot_label for equip in veteran.equips if equip], ["护甲", "护手", "配件Ⅱ"])
        self.assertEqual(veteran.equips[0].rarity, 5)
        self.assertEqual(veteran.equips[3].suit_name, "")
        self.assertEqual(rookie.equips, (None, None, None, None))
        self.assertEqual(rookie.equipped_count, 0)

    def test_reads_weapon_potential_and_prefers_gem_data_icon(self):
        veteran, rookie = self.build().operators
        self.assertEqual(
            (veteran.weapon.name, veteran.weapon.level, veteran.weapon.potential_level, veteran.weapon.breakthrough_level),
            ("测试武器", 90, 5, 4),
        )
        self.assertEqual(veteran.weapon.gem_icon_url, "https://assets.invalid/gem.png")
        self.assertEqual(veteran.weapon.rarity, 6)
        self.assertEqual(rookie.weapon.gem_icon_url, "")
        self.assertEqual(rookie.tactical_name, "")

    def test_tolerates_missing_sections_without_fabricating_zero(self):
        for detail in ({}, {"base": None, "chars": None}, {"base": {}, "chars": [{}]}):
            view = account_detail_module.build_account_detail_view(detail, uid="****0000")
            self.assertIsNone(view.level)
            self.assertIsNone(view.world_level)
            self.assertEqual(view.uid, "****0000")
        sparse = account_detail_module.build_account_detail_view({"chars": [{}]}, uid="x").operators[0]
        self.assertEqual((sparse.level, sparse.evolve_phase, sparse.potential_level), (None, None, None))
        self.assertIsNone(sparse.weapon)
        self.assertEqual(sparse.equips, (None, None, None, None))
        self.assertEqual(sparse.skills, ())

    def test_compact_layout_switches_above_threshold(self):
        detail = account_detail_fixture()
        template = detail["chars"][0]
        roster = COMPACT_THRESHOLD + 1
        detail["chars"] = [dict(template, id=f"char-{index}") for index in range(roster)]
        view = account_detail_module.build_account_detail_view(detail, uid="x")
        self.assertTrue(view.compact)
        self.assertEqual(
            (view.identity_column, view.portrait_size, view.slot_size, view.skill_icon_size),
            (300, 68, 54, 44),
        )
        detail["chars"] = detail["chars"][:COMPACT_THRESHOLD]
        self.assertFalse(account_detail_module.build_account_detail_view(detail, uid="x").compact)


def account_base_fixture(*, current_ts: int = 1600, money: int = 1600) -> dict:
    mood_talent = {
        "id": "spaceship_skill_test_2_2",
        "name": "节律工作·γ",
        "desc": "进驻制造舱时，舱室内干员心情消耗降低18%",
        "iconUrl": "",
    }
    recovery_talent = {
        "id": "spaceship_skill_control_1_2",
        "name": "温柔中枢·γ",
        "desc": "进驻总控中枢时，所有干员心情恢复提升12%",
        "iconUrl": "",
    }
    return {
        "base": {"name": "测试管理员", "serverName": "China"},
        "currentTs": current_ts,
        "domain": [
            {
                "domainId": "domain_2",
                "name": "武陵",
                "settlements": [
                    {
                        "id": "stm-test",
                        "name": "天王坪援建点",
                        "level": 4,
                        "remainMoney": str(money),
                        "moneyMax": "10000",
                        "officerCharIds": "officer-hash",
                    }
                ],
            }
        ],
        "chars": [
            {
                "id": "officer-hash",
                "charData": {
                    "id": "chr_officer",
                    "name": "派驻员",
                    "avatarSqUrl": "",
                    "cultivationTalents": [],
                },
                "talent": {"latestSpaceshipSkillNodes": []},
            },
            {
                "id": "worker-hash",
                "charData": {
                    "id": "chr_worker",
                    "name": "工作员",
                    "avatarSqUrl": "",
                    "cultivationTalents": [mood_talent],
                },
                "talent": {"latestSpaceshipSkillNodes": [mood_talent["id"]]},
            },
            {
                "id": "control-hash",
                "charData": {
                    "id": "chr_control",
                    "name": "中枢员",
                    "avatarSqUrl": "",
                    "cultivationTalents": [recovery_talent],
                },
                "talent": {"latestSpaceshipSkillNodes": [recovery_talent["id"]]},
            },
        ],
        "spaceShip": {
            "rooms": [
                {
                    "type": 1,
                    "level": 3,
                    "chars": [
                        {
                            "charId": "chr_worker",
                            "physicalStrength": 5000,
                            "avatarUrl": "",
                        }
                    ],
                },
                {
                    "type": 0,
                    "level": 5,
                    "chars": [
                        {
                            "charId": "chr_control",
                            "physicalStrength": 8000,
                            "avatarUrl": "",
                        }
                    ],
                },
                {"type": 3, "level": 1, "id": "", "chars": []},
            ]
        },
    }


class EndfieldAccountBaseViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_module.EndfieldStore(Path(self.temp.name) / "base.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def build(self, detail: dict | None = None, *, name_map=None):
        return account_base_service_module.build_account_base_view(
            detail or account_base_fixture(),
            uid="****1234",
            role_id="role",
            server_id="1",
            nickname="备用昵称",
            store=self.store,
            name_map=name_map,
        )

    def test_reads_settlements_rooms_and_active_mood_nodes(self):
        view = self.build()
        settlement = view.regions[0].settlements[0]
        self.assertEqual(
            (view.nickname, view.uid, settlement.name, settlement.officer_name),
            ("测试管理员", "****1234", "天王坪援建点", "派驻员"),
        )
        self.assertEqual(view.server_name, "国服")
        self.assertEqual([room.name for room in view.rooms], ["总控中枢", "制造舱 I"])
        worker = view.rooms[1].operators[0]
        self.assertEqual(worker.name, "工作员")
        self.assertAlmostEqual(worker.mood_percent, 50.0)
        self.assertEqual(worker.skills[0].name, "节律工作·γ")
        self.assertAlmostEqual(worker.drain_percent_per_hour, 7.2 * 0.82)
        self.assertAlmostEqual(worker.recovery_percent_per_hour, 12.0 * 1.12)

    def test_localizes_mapping_shaped_base_fields(self):
        detail = account_base_fixture()
        detail["base"]["name"] = {"zh-CN": "中文管理员", "en": "English Admin"}
        detail["base"]["serverName"] = {"en": "China"}
        detail["domain"][0]["name"] = {"zh": "中文地区", "en": "English Region"}
        detail["domain"][0]["settlements"][0]["name"] = {
            "zh": "中文据点",
            "en": "English Settlement",
        }
        detail["chars"][0]["charData"]["name"] = {
            "zh": "中文派遣员",
            "en": "English Officer",
        }

        view = self.build(detail)

        self.assertEqual(view.nickname, "中文管理员")
        self.assertEqual(view.server_name, "国服")
        self.assertEqual(view.regions[0].name, "中文地区")
        self.assertEqual(view.regions[0].settlements[0].name, "中文据点")
        self.assertEqual(view.regions[0].settlements[0].officer_name, "中文派遣员")

    def test_maps_room_character_id_from_spaceship_skill_node(self):
        self.assertEqual(
            account_base_service_module._spaceship_skill_char_id(
                "spaceship_skill_chr_0017_yvonne_2_2"
            ),
            "chr_0017_yvonne",
        )

    def test_prefers_akedata_i18n_for_operator_and_spaceship_skill_labels(self):
        names = account_detail_names_module.AccountDetailNameMap(
            character_names={
                "chr_worker": "本地化干员",
                "chr_control": "本地化中枢干员",
            },
            spaceship_skill_names={
                "spaceship_skill_test_2_2": "本地化制造技能",
                "spaceship_skill_control_1_2": "本地化中枢技能",
            },
            spaceship_skill_descriptions={
                "spaceship_skill_test_2_2": "进驻制造舱时，舱室内干员心情消耗降低 8%",
                "spaceship_skill_control_1_2": "进驻总控中枢时，所有干员心情恢复提升 12%",
            },
            domain_names={"domain_2": "本地化地区"},
            settlement_names={"stm-test": "本地化据点"},
            spaceship_room_names={"0": "本地化中枢", "1": "本地化制造舱"},
        )

        view = self.build(name_map=names)
        worker = view.rooms[1].operators[0]
        control = view.rooms[0].operators[0]

        self.assertEqual(view.regions[0].name, "本地化地区")
        self.assertEqual(view.regions[0].settlements[0].name, "本地化据点")
        self.assertEqual([room.name for room in view.rooms], ["本地化中枢", "本地化制造舱 I"])
        self.assertEqual(worker.name, "本地化干员")
        self.assertEqual(worker.skills[0].name, "本地化制造技能")
        self.assertEqual(worker.skills[0].description, "进驻制造舱时，舱室内干员心情消耗降低 8%")
        self.assertAlmostEqual(worker.drain_percent_per_hour, 7.2 * 0.92)
        self.assertEqual(control.name, "本地化中枢干员")
        self.assertAlmostEqual(control.recovery_percent_per_hour, 12.0 * 1.12)

    def test_first_snapshot_is_pending_then_rate_is_sampled(self):
        first = self.build(account_base_fixture(current_ts=1000, money=1000))
        self.assertFalse(first.regions[0].settlements[0].rate.available)
        second = self.build(account_base_fixture(current_ts=1600, money=1600))
        rate = second.regions[0].settlements[0].rate
        self.assertTrue(rate.available)
        self.assertEqual(rate.confidence, "low")
        self.assertAlmostEqual(rate.value_per_hour, 3600.0)
        self.assertAlmostEqual(second.regions[0].settlements[0].hours_to_full, 8400 / 3600)

    def test_collection_and_configuration_change_reset_sampling(self):
        self.build(account_base_fixture(current_ts=1000, money=5000))
        collected = self.build(account_base_fixture(current_ts=1600, money=100))
        self.assertFalse(collected.regions[0].settlements[0].rate.available)

        detail = account_base_fixture(current_ts=2200, money=700)
        detail["domain"][0]["settlements"][0]["level"] = 3
        changed = self.build(detail)
        self.assertFalse(changed.regions[0].settlements[0].rate.available)

    def test_snapshot_retention_and_roundtrip(self):
        old = store_module.SettlementSnapshot(
            "role", "1", "stm", 1, 100, "", 10, 100
        )
        fresh = replace(old, captured_at=1000, remain_money=20)
        self.store.add_settlement_snapshots([old, fresh], retention_seconds=500, now=1000)
        rows = self.store.list_settlement_snapshots("role", "1", "stm")
        self.assertEqual([(row.captured_at, row.remain_money) for row in rows], [(1000, 20)])

    def test_renderer_uses_approved_content_without_defense_or_assignment_icon_row(self):
        view = self.build()
        html = asyncio.run(account_base_draw_module.render_account_base_card_html(view))
        self.assertIn("width:1550px", html)
        self.assertIn("ENDFIELD / ACCOUNT INFRASTRUCTURE", html)
        self.assertIn("基建与帝江号", html)
        self.assertIn("据点存票", html)
        self.assertIn("帝江号心情", html)
        self.assertIn("--region-color:#6bffff", html)
        self.assertNotIn("区域防护", html)
        self.assertNotIn(">派驻干员<", html)


class EndfieldCredentialAndStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_module.EndfieldStore(Path(self.temp.name) / "endfield.db")
        self.cipher = crypto.CredentialCipher(b"k" * 32)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_aes_gcm_roundtrip_and_wrong_key(self):
        encrypted = self.cipher.encrypt("sensitive-account-token")
        self.assertNotIn(b"sensitive-account-token", encrypted.ciphertext)
        self.assertEqual(self.cipher.decrypt(encrypted), "sensitive-account-token")
        with self.assertRaises(crypto.CredentialKeyError):
            crypto.CredentialCipher(b"x" * 32).decrypt(encrypted)

    def test_base64_key_requires_exactly_32_bytes(self):
        value = base64.b64encode(b"short").decode()
        with mock.patch.dict("os.environ", {crypto.KEY_ENV_NAME: value}, clear=False):
            with self.assertRaises(crypto.CredentialKeyError):
                crypto.CredentialCipher.from_env()

    def test_binding_primary_switch_resolution_and_unbind(self):
        roles = [
            store_module.RoleCandidate("bind-a", "10001234", "1", "甲", "一区"),
            store_module.RoleCandidate("bind-b", "20005678", "2", "乙", "二区"),
        ]
        bound = self.store.bind_roles("qq", "token", roles, self.cipher)
        self.assertTrue(bound[0].is_primary)
        self.assertEqual(self.store.resolve_role("qq", "5678").nickname, "乙")
        self.assertIsNone(self.store.resolve_role("qq", "0002"))
        primary = self.store.set_primary("qq", "2")
        self.assertEqual(primary.nickname, "乙")
        removed = self.store.unbind("qq", "2")
        self.assertEqual(removed.nickname, "乙")
        self.assertTrue(self.store.list_roles("qq")[0].is_primary)

    def test_one_user_can_bind_multiple_login_accounts(self):
        first = self.store.bind_roles(
            "qq", "token-a", [store_module.RoleCandidate("bind-a", "10001234", "1", "甲")], self.cipher
        )[0]
        roles = self.store.bind_roles(
            "qq", "token-b", [store_module.RoleCandidate("bind-b", "20005678", "2", "乙")], self.cipher
        )

        self.assertEqual([role.nickname for role in roles], ["甲", "乙"])
        self.assertNotEqual(roles[0].credential_id, roles[1].credential_id)
        self.assertEqual(self.store.decrypt_token(roles[0], self.cipher), "token-a")
        self.assertEqual(self.store.decrypt_token(roles[1], self.cipher), "token-b")
        self.assertEqual(sum(role.is_primary for role in roles), 1)
        self.assertEqual(self.store.resolve_role("qq").id, first.id)

        self.store.unbind("qq", "1")
        remaining = self.store.list_roles("qq")
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].is_primary)
        self.assertEqual(self.store.decrypt_token(remaining[0], self.cipher), "token-b")
        credential_count = self.store.conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
        self.assertEqual(credential_count, 1)

    def test_gacha_deduplicates_and_filters_history(self):
        role = self.store.bind_roles(
            "qq", "token", [store_module.RoleCandidate("bind", "1000", "1", "甲")], self.cipher
        )[0]
        record = store_module.GachaRecord(
            role.role_id, role.server_id, "pool", "联合寻访", "joint", "seq-1", 10,
            "char", "角色甲", 6, "角色",
        )
        self.assertEqual(self.store.insert_gacha_records([record, record]), 1)
        self.assertEqual(self.store.count_gacha_records(role, "联合"), 1)
        self.assertEqual(self.store.list_gacha_records(role, pool_filter="联合")[0].item_name, "角色甲")

    def test_currency_log_backup_upserts_by_seq_id_and_filters_display(self):
        role = self.store.bind_roles(
            "qq", "token", [store_module.RoleCandidate("bind", "1000", "1", "甲")], self.cipher
        )[0]
        records = (
            client_module.CurrencyLogItem(1, 1, "13", 100, 100, 200, 10),
            client_module.CurrencyLogItem(1, 2, "10", 30, 70, 300, 11),
            client_module.CurrencyLogItem(1, 1, "13", 100, 100, 200, 10),
        )
        self.store.upsert_currency_logs(role, records)
        stored = self.store.list_currency_logs(role, (1,), start_ts=200, end_ts=300)
        self.assertEqual([item.seq_id for item in stored[1]], [10, 11])
        gained = self.store.list_currency_logs(role, (1,), change_type=1)
        self.assertEqual([item.seq_id for item in gained[1]], [10])
        count = self.store.conn.execute("SELECT COUNT(*) FROM currency_logs").fetchone()[0]
        self.assertEqual(count, 2)

    def test_gacha_upsert_backfills_free_pull_flag(self):
        role = self.store.bind_roles(
            "qq", "token", [store_module.RoleCandidate("bind", "1000", "1", "甲")], self.cipher
        )[0]
        original = store_module.GachaRecord(
            role.role_id, role.server_id, "pool", "特许寻访", "special", "seq-1", 10,
            "char", "角色甲", 6, "角色",
        )
        corrected = store_module.GachaRecord(
            role.role_id, role.server_id, "pool", "特许寻访", "special", "seq-1", 10,
            "char", "角色甲", 6, "角色", is_free=True,
        )

        self.assertEqual(self.store.insert_gacha_records([original]), 1)
        self.assertEqual(self.store.insert_gacha_records([corrected]), 0)
        self.assertTrue(self.store.list_gacha_records(role)[0].is_free)

    def test_gacha_pool_total_override_roundtrip(self):
        role = self.store.bind_roles(
            "qq", "token", [store_module.RoleCandidate("bind", "1000", "1", "甲")], self.cipher
        )[0]
        self.store.set_gacha_pool_total(role, "special-old", 120)
        self.assertEqual(self.store.list_gacha_pool_totals(role), {"special-old": 120})

    def test_xhh_snapshot_replacement_roundtrip_and_role_isolation(self):
        roles = self.store.bind_roles(
            "qq", "token",
            [
                store_module.RoleCandidate("bind-a", "10001234", "1", "甲"),
                store_module.RoleCandidate("bind-b", "20005678", "1", "乙"),
            ],
            self.cipher,
        )
        imported = store_module.XhhGachaImport(
            source_uid="10001234", nickname="甲", total_count=120, imported_at=123,
            pools=(
                store_module.XhhGachaPool(
                    "special-1", "春雷动万物生", "special", "角色", 120, 20, 10, 100, True, 3
                ),
            ),
            six_stars=(
                store_module.XhhSixStar(
                    "special-1", "unique", "42式", "角色", 90, 80, 80, is_free=True
                ),
            ),
        )
        self.store.replace_xhh_gacha_import(roles[0], imported)
        saved = self.store.get_xhh_gacha_import(roles[0])
        self.assertEqual((saved.source_uid, saved.total_count), ("10001234", 120))
        self.assertEqual((saved.pools[0].current_count, saved.pools[0].free_count), (20, 10))
        self.assertEqual(saved.pools[0].sort_order, 3)
        self.assertEqual(saved.six_stars[0].item_name, "42式")
        self.assertTrue(saved.six_stars[0].is_free)
        self.assertIsNone(self.store.get_xhh_gacha_import(roles[1]))

        mismatched = store_module.XhhGachaImport(
            source_uid="wrong", nickname="甲", total_count=0, imported_at=124,
            pools=(), six_stars=(),
        )
        with self.assertRaisesRegex(ValueError, "UID"):
            self.store.replace_xhh_gacha_import(roles[0], mismatched)
        self.assertEqual(self.store.get_xhh_gacha_import(roles[0]).imported_at, 123)

    def test_xhh_pool_order_migrates_from_legacy_insertion_order(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE xhh_gacha_pools (
                role_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                pool_name TEXT NOT NULL DEFAULT '',
                pool_type TEXT NOT NULL DEFAULT '',
                item_type TEXT NOT NULL,
                total_count INTEGER NOT NULL DEFAULT 0,
                current_count INTEGER NOT NULL DEFAULT 0,
                free_count INTEGER NOT NULL DEFAULT 0,
                latest_ts INTEGER NOT NULL DEFAULT 0,
                is_current INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(role_id, server_id, pool_id)
            );
            INSERT INTO xhh_gacha_pools VALUES
                ('role', '1', 'new', '最新池', 'special', '角色', 1, 0, 0, 0, 1),
                ('role', '1', 'old', '旧池', 'special', '角色', 1, 0, 0, 0, 0);
            """
        )
        connection.commit()
        connection.close()

        migrated = store_module.EndfieldStore(legacy_path)
        rows = migrated.conn.execute(
            "SELECT pool_id, sort_order FROM xhh_gacha_pools ORDER BY sort_order"
        ).fetchall()
        migrated.close()

        self.assertEqual([(row["pool_id"], row["sort_order"]) for row in rows], [("new", 0), ("old", 1)])


class XiaoheiheClientTests(unittest.TestCase):
    def test_hkey_known_vector(self):
        self.assertEqual(
            xhh_module.make_xhh_hkey(
                "/account/get_login_code/", 1784767656,
                "B8C30A9A0ACBD6615A78670E31CB5615",
            ),
            "3X7Y367",
        )

    def test_parses_overview_snapshot(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234", "nickname": "甲"},
                    "statistic_info": {"total_count": 120},
                    "gacha_record": [
                        {
                            "pool_id": "special-1", "pool_name": "春雷动万物生",
                            "pool_type": "special", "item_type": "角色",
                            "total_count": 120, "current_count": 20,
                            "free_count": 10, "is_current": True,
                            "six_star_record": [
                                {
                                    "name": "42式", "diff": 80,
                                    "date": "2026-02-01", "pool_position": 80,
                                }
                            ],
                        }
                    ],
                },
            }
        )
        self.assertEqual((imported.source_uid, imported.total_count), ("10001234", 120))
        self.assertEqual(len(imported.pools), 1)
        self.assertEqual((imported.pools[0].current_count, imported.pools[0].free_count), (20, 10))
        self.assertEqual((imported.six_stars[0].item_name, imported.six_stars[0].interval), ("42式", 80))

    def test_parses_nested_xhh_six_stars_and_preserves_pool_order(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234"},
                    "gacha_record": [
                        {
                            "pool_id": "new", "pool_name": "最新池", "total_count": 65,
                            "last_diff": 5,
                            "unknown_summary": {
                                "six_detail_list": [
                                    {"name": "最新六星", "date": "2026-02-01", "diff": 20, "miss_up": 0},
                                    {"name": "中间六星", "date": "2026-02-01", "diff": 30, "miss_up": 1},
                                    {"name": "最早六星", "date": "2026-01-01", "diff": 10, "miss_up": 0},
                                ],
                                "five_star_list": [
                                    {"name": "五星", "date": "2026-02-01", "diff": 1, "rarity": 5}
                                ],
                            },
                        },
                        {"pool_id": "old", "pool_name": "旧池", "total_count": 1},
                    ],
                },
            }
        )

        self.assertEqual([item.pool_id for item in imported.pools], ["new", "old"])
        self.assertEqual([item.sort_order for item in imported.pools], [0, 1])
        self.assertEqual(imported.pools[0].current_count, 5)
        self.assertEqual(
            [(item.item_name, item.interval, item.pool_position) for item in imported.six_stars],
            [("最新六星", 20, 60), ("中间六星", 30, 40), ("最早六星", 10, 10)],
        )

    def test_parses_free_ten_six_star_source(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234"},
                    "gacha_record": [
                        {
                            "pool_id": "special", "pool_name": "限定池", "total_count": 35,
                            "free_ten": {
                                "six_detail": [
                                    {"name": "莱万汀", "date": "2026-01-27", "diff": 1}
                                ]
                            },
                        }
                    ],
                },
            }
        )

        self.assertEqual(len(imported.six_stars), 1)
        self.assertTrue(imported.six_stars[0].is_free)

    def test_keeps_legacy_six_star_records_with_rarity_five_for_metadata_validation(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234"},
                    "gacha_record": [
                        {
                            "pool_id": "early",
                            "pool_name": "early",
                            "total_count": 40,
                            "six_star_record": [
                                {"name": "legacy-six", "rarity": 5, "diff": 20}
                            ],
                        }
                    ],
                },
            }
        )

        self.assertEqual([item.item_name for item in imported.six_stars], ["legacy-six"])

    def test_explicit_six_star_history_ignores_unrelated_nested_items(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234"},
                    "gacha_record": [
                        {
                            "pool_id": "weapon", "pool_name": "武器池", "total_count": 40,
                            "six_star_record": [
                                {"name": "实际产出", "date": "2026-02-01", "diff": 20}
                            ],
                            "up_items": [
                                {"name": "卡池展示武器", "rarity": 6, "date": "2026-02-01"}
                            ],
                        }
                    ],
                },
            }
        )

        self.assertEqual([item.item_name for item in imported.six_stars], ["实际产出"])

    def test_discovers_paid_history_outside_six_star_named_paths(self):
        imported = xhh_module.parse_xhh_overview(
            {
                "status": "ok",
                "result": {
                    "is_bind": True,
                    "user_info": {"uid": "10001234"},
                    "gacha_record": [
                        {
                            "pool_id": "early",
                            "pool_name": "早期池",
                            "total_count": 81,
                            "history": {
                                "free_ten": [
                                    {"name": "免费六星", "rarity": 5, "diff": 1}
                                ]
                            },
                            "legacy_paid_records": [
                                {"name": "付费六星一", "rarity": 5, "diff": 40},
                                {"name": "付费六星二", "rarity": 5, "diff": 40},
                            ],
                            "up_items": [
                                {"name": "卡池展示六星", "rarity": 6, "date": "2026-01-01"}
                            ],
                        }
                    ],
                },
            }
        )

        self.assertEqual(
            [(item.item_name, item.is_free) for item in imported.six_stars],
            [("免费六星", True), ("付费六星一", False), ("付费六星二", False)],
        )

    def test_rejects_unbound_or_uidless_overview(self):
        with self.assertRaisesRegex(xhh_module.XhhAPIError, "未绑定"):
            xhh_module.parse_xhh_overview({"status": "ok", "result": {"is_bind": False}})
        with self.assertRaisesRegex(xhh_module.XhhAPIError, "UID"):
            xhh_module.parse_xhh_overview(
                {
                    "status": "ok",
                    "result": {
                        "is_bind": True,
                        "gacha_record": [
                            {"pool_id": "p", "pool_name": "池", "total_count": 1}
                        ],
                    },
                }
            )


class XiaoheiheBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_launch_falls_back_to_system_edge(self):
        edge_browser = object()
        chromium = types.SimpleNamespace(
            launch=mock.AsyncMock(side_effect=[RuntimeError("missing bundled browser"), edge_browser])
        )

        result = await xhh_module._launch_xhh_browser(types.SimpleNamespace(chromium=chromium))

        self.assertIs(result, edge_browser)
        self.assertEqual(chromium.launch.await_count, 2)
        self.assertNotIn("channel", chromium.launch.await_args_list[0].kwargs)
        self.assertEqual(chromium.launch.await_args_list[1].kwargs["channel"], "msedge")
        self.assertEqual(
            chromium.launch.await_args_list[1].kwargs["args"], ["--no-proxy-server"]
        )


class EndfieldOfficialClientTests(unittest.IsolatedAsyncioTestCase):
    def test_default_client_ignores_environment_proxy(self):
        with mock.patch.object(client_module.httpx, "AsyncClient") as async_client:
            client = client_module.EndfieldOfficialClient()

        async_client.assert_called_once_with(timeout=25.0, follow_redirects=True, trust_env=False)
        self.assertIs(client.http, async_client.return_value)
        self.assertTrue(client._owns_http)

    def test_http_client_request_logs_are_suppressed(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_error_sanitizer_removes_phone_code_token_and_url(self):
        message = "phone=13800138000 code=123456 token=secret https://example.test/a?token=secret"
        sanitized = client_module._sanitize_message(message)
        self.assertNotIn("13800138000", sanitized)
        self.assertNotIn("123456", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("https://", sanitized)

    async def test_cn_qr_login_uses_official_scan_flow(self):
        requests: list[httpx.Request] = []
        scan_statuses = iter([
            {"status": 100, "msg": "未扫码"},
            {"status": 101, "msg": "已扫码，待确认"},
            {"status": 0, "data": {"scanCode": "confirmed-scan-code"}},
        ])

        async def handler(request: httpx.Request):
            requests.append(request)
            if request.url.path == "/general/v1/gen_scan/login":
                return httpx.Response(200, json={
                    "status": 0,
                    "data": {
                        "scanId": "scan-id",
                        "scanUrl": "hypergryph://scan_login?scanId=scan-id",
                    },
                })
            if request.url.path == "/general/v1/scan_status":
                return httpx.Response(200, json=next(scan_statuses))
            if request.url.path == "/user/auth/v1/token_by_scan_code":
                self.assertEqual(json.loads(request.content), {"scanCode": "confirmed-scan-code"})
                return httpx.Response(200, json={"status": 0, "data": {"token": "account-token"}})
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)

        ticket = await client.create_qr_login()
        pending = await client.check_qr_login(ticket.scan_id)
        scanned = await client.check_qr_login(ticket.scan_id)
        confirmed = await client.check_qr_login(ticket.scan_id)
        token = await client.token_by_scan_code(confirmed.scan_code)

        self.assertEqual(ticket, client_module.QrLoginTicket(
            "scan-id", "hypergryph://scan_login?scanId=scan-id"
        ))
        self.assertEqual(pending, client_module.QrLoginStatus("pending"))
        self.assertEqual(scanned, client_module.QrLoginStatus("scanned"))
        self.assertEqual(confirmed, client_module.QrLoginStatus("confirmed", "confirmed-scan-code"))
        self.assertEqual(token, "account-token")
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/general/v1/gen_scan/login",
                "/general/v1/scan_status",
                "/general/v1/scan_status",
                "/general/v1/scan_status",
                "/user/auth/v1/token_by_scan_code",
            ],
        )
        self.assertEqual(requests[1].url.params["scanId"], "scan-id")
        await http.aclose()

    async def test_cn_qr_login_reports_expired_status(self):
        async def handler(_request: httpx.Request):
            return httpx.Response(200, json={"status": 102, "msg": "二维码已过期"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)

        self.assertEqual(await client.check_qr_login("scan-id"), client_module.QrLoginStatus("expired"))
        await http.aclose()

    async def test_cn_qr_login_rejects_unknown_scan_status(self):
        async def handler(_request: httpx.Request):
            return httpx.Response(200, json={"status": 103, "msg": "状态异常"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)

        with self.assertRaises(client_module.EndfieldAPIError):
            await client.check_qr_login("scan-id")
        await http.aclose()

    async def test_account_oauth_retries_one_transient_network_failure(self):
        http = mock.AsyncMock()
        http.request = mock.AsyncMock(side_effect=[
            httpx.ConnectError("temporary connection failure"),
            httpx.Response(200, json={"status": 0, "data": {"token": "oauth-token"}}),
        ])
        client = client_module.EndfieldOfficialClient(http)

        with mock.patch.object(client_module.asyncio, "sleep", mock.AsyncMock()) as sleep:
            token = await client._oauth_token(
                "account-token", client_module.SKLAND_APP_CODE, grant_type=0
            )

        self.assertEqual(token, "oauth-token")
        self.assertEqual(http.request.await_count, 2)
        sleep.assert_awaited_once_with(0.4)

    def test_account_credential_keeps_legacy_cn_tokens_and_wraps_skport_tokens(self):
        self.assertEqual(
            client_module.decode_account_credential("legacy-cn-token"),
            (client_module.ACCOUNT_PROVIDER_CN, "legacy-cn-token"),
        )
        credential = client_module.encode_account_credential(
            '{"data":{"content":"global-token"}}',
            client_module.ACCOUNT_PROVIDER_SKPORT,
        )
        self.assertEqual(
            client_module.decode_account_credential(credential),
            (client_module.ACCOUNT_PROVIDER_SKPORT, "global-token"),
        )

    def test_asia_role_detection_uses_server_metadata(self):
        role = store_module.RoleCandidate("binding", "role", "2", "测试", "Asia")
        europe = store_module.RoleCandidate("binding", "role", "3", "测试", "Americas / Europe")
        self.assertTrue(client_module.is_asia_role(role))
        self.assertFalse(client_module.is_asia_role(europe))

    def test_skland_binding_parser_keeps_binding_uid(self):
        payload = {
            "code": 0,
            "data": {"list": [{
                "appCode": "endfield",
                "bindingList": [{
                    "uid": "binding-uid",
                    "nickName": "账号昵称",
                    "roles": [{"roleId": "role", "serverId": "1", "nickname": "角色昵称"}],
                }],
            }]},
        }
        roles = client_module._extract_endfield_roles(payload)
        self.assertEqual((roles[0].binding_uid, roles[0].role_id), ("binding-uid", "role"))

    def test_skland_binding_parser_localizes_role_fields(self):
        payload = {
            "code": 0,
            "data": {"list": [{
                "appCode": "endfield",
                "bindingList": [{
                    "uid": "binding-uid",
                    "roles": [{
                        "roleId": "role",
                        "serverId": "1",
                        "nickname": {"zh": "中文角色", "en": "English Role"},
                        "serverName": {"en": "China"},
                    }],
                }],
            }]},
        }

        roles = client_module._extract_endfield_roles(payload)

        self.assertEqual((roles[0].nickname, roles[0].server_name), ("中文角色", "China"))

    def test_gacha_binding_parser_keeps_binding_uid_for_each_server_role(self):
        payload = {
            "status": 0,
            "data": {
                "list": [{
                    "appCode": "endfield",
                    "bindingList": [{
                        "uid": "binding-uid",
                        "roles": [
                            {"roleId": "role-a", "serverId": "1", "nickName": "甲"},
                            {"roleId": "role-b", "serverId": "2", "nickName": "乙"},
                        ],
                    }],
                }]
            },
        }
        roles = client_module._extract_gacha_binding_roles(payload)
        self.assertEqual([(item.binding_uid, item.role_id) for item in roles], [("binding-uid", "role-a"), ("binding-uid", "role-b")])

    async def test_cn_skland_context_uses_generated_sign_token_without_refresh(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={
                    "status": 0,
                    "data": {"code": "oauth-code"},
                })
            if request.url.path == "/api/v1/user/auth/generate_cred_by_code":
                return httpx.Response(200, json={
                    "code": 0,
                    "timestamp": 1000,
                    "data": {"cred": "cred", "token": "sign-token"},
                })
            if request.url.path == "/web/v1/auth/refresh":
                raise AssertionError("generated sign token should avoid refresh")
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            context = await client._skland_context("account-token")

        self.assertEqual(
            (context.cred, context.sign_token, context.server_time),
            ("cred", "sign-token", 1000),
        )
        await http.aclose()

    async def test_skport_role_discovery_uses_global_account_and_community_hosts(self):
        requests = []

        async def handler(request: httpx.Request):
            requests.append(request)
            if request.url.host == "as.gryphline.com":
                body = json.loads(request.content)
                field = "code" if body["type"] == 0 else "token"
                return httpx.Response(200, json={"status": 0, "data": {field: f"oauth-{field}"}})
            if request.url.path == "/web/v1/user/auth/generate_cred_by_code":
                return httpx.Response(200, json={"code": 0, "data": {"cred": "cred"}})
            if request.url.path == "/web/v1/auth/refresh":
                self.assertEqual(
                    request.headers["user-agent"],
                    client_module.SKLAND_REFRESH_USER_AGENT,
                )
                self.assertEqual(request.headers["content-type"], "application/json")
                return httpx.Response(200, json={"code": 0, "timestamp": 1000, "data": {"salt": "salt"}})
            if request.url.host == "zonai.skport.com":
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {"list": [{
                        "appCode": "endfield",
                        "bindingList": [{
                            "uid": "binding-global",
                            "defaultRole": {
                                "roleId": "role-asia",
                                "serverId": "2",
                                "nickname": "亚服角色",
                                "serverName": "Asia",
                            },
                            "roles": [],
                        }],
                    }]},
                })
            if request.url.host == "binding-api-account-prod.gryphline.com":
                return httpx.Response(200, json={
                    "status": 0,
                    "data": {"list": [{
                        "appCode": "endfield",
                        "bindingList": [{
                            "uid": "binding-global",
                            "roles": [{
                                "roleId": "role-asia", "serverId": "2",
                                "nickName": "亚服角色", "serverName": "Asia",
                            }],
                        }],
                    }]},
                })
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        credential = client_module.encode_account_credential(
            "global-account-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )
        with mock.patch.object(client_module.time, "time", return_value=1000):
            roles = await client.discover_roles(credential)

        self.assertEqual(
            [(role.binding_uid, role.role_id, role.server_name) for role in roles],
            [("binding-global", "role-asia", "Asia")],
        )
        self.assertTrue(any(
            request.url.host == "as.gryphline.com"
            and json.loads(request.content)["appCode"] == "6eb76d4e13aa36e6"
            for request in requests
        ))
        self.assertTrue(any(
            request.url.host == "as.gryphline.com"
            and json.loads(request.content)["appCode"] == "3dacefa138426cfe"
            for request in requests
        ))
        self.assertTrue(any(request.url.host == "zonai.skport.com" for request in requests))
        self.assertTrue(any(
            request.url.host == "binding-api-account-prod.gryphline.com" for request in requests
        ))
        await http.aclose()

    async def test_skport_card_detail_uses_global_host_and_role_header(self):
        captured = {}

        async def handler(request: httpx.Request):
            captured["request"] = request
            return httpx.Response(200, json={"code": 0, "data": {"detail": {"base": {"name": "Asia"}}}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        client._skland_context = mock.AsyncMock(return_value=client_module._SklandContext(
            "cred", "salt", 1000, 1000, 99999, client_module.ACCOUNT_PROVIDER_SKPORT
        ))
        role = mock.Mock(role_id="role-asia", server_id="2")
        with mock.patch.object(client_module.time, "time", return_value=1000):
            detail = await client.card_detail("credential", role)

        request = captured["request"]
        self.assertEqual(request.url.host, "zonai.skport.com")
        self.assertEqual(request.headers["sk-game-role"], "3_role-asia_2")
        self.assertEqual(request.headers["origin"], "https://game.skport.com")
        self.assertEqual(detail["base"]["name"], "Asia")
        await http.aclose()

    async def test_skport_credential_falls_back_from_web_to_api_with_fresh_oauth_code(self):
        requests: list[httpx.Request] = []
        grant_count = 0

        async def handler(request: httpx.Request):
            nonlocal grant_count
            requests.append(request)
            if request.url.host == "as.gryphline.com":
                grant_count += 1
                return httpx.Response(200, json={
                    "status": 0,
                    "data": {"code": f"oauth-code-{grant_count}"},
                })
            if request.url.path == "/web/v1/user/auth/generate_cred_by_code":
                return httpx.Response(200, json={"code": 404, "message": "Not Found"})
            if request.url.path == "/api/v1/user/auth/generate_cred_by_code":
                self.assertEqual(json.loads(request.content)["code"], "oauth-code-2")
                return httpx.Response(200, json={"code": 0, "data": {"cred": "global-cred"}})
            if request.url.path == "/web/v1/auth/refresh":
                return httpx.Response(200, json={
                    "code": 0,
                    "timestamp": 1000,
                    "data": {"salt": "global-salt"},
                })
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        credential = client_module.encode_account_credential(
            "global-account-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )

        with mock.patch.object(client_module.time, "time", return_value=1000):
            context = await client._skland_context(credential)

        self.assertEqual((context.cred, context.sign_token), ("global-cred", "global-salt"))
        self.assertEqual(grant_count, 2)
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/user/oauth2/v2/grant",
                "/web/v1/user/auth/generate_cred_by_code",
                "/user/oauth2/v2/grant",
                "/api/v1/user/auth/generate_cred_by_code",
                "/web/v1/auth/refresh",
            ],
        )
        await http.aclose()

    async def test_skport_uses_oauth_code_as_cred_when_exchange_routes_are_unavailable(self):
        requests: list[httpx.Request] = []
        grant_count = 0

        async def handler(request: httpx.Request):
            nonlocal grant_count
            requests.append(request)
            if request.url.host == "as.gryphline.com":
                grant_count += 1
                return httpx.Response(200, json={
                    "status": 0,
                    "data": {"code": f"oauth-code-{grant_count}"},
                })
            if request.url.path in {
                "/web/v1/user/auth/generate_cred_by_code",
                "/api/v1/user/auth/generate_cred_by_code",
            }:
                return httpx.Response(200, json={"code": 404, "message": "Not Found"})
            if request.url.path == "/web/v1/auth/refresh":
                self.assertEqual(request.headers["cred"], "oauth-code-2")
                return httpx.Response(200, json={
                    "code": 0,
                    "timestamp": 1000,
                    "data": {"token": "global-sign-token"},
                })
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        credential = client_module.encode_account_credential(
            "global-account-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )

        with mock.patch.object(client_module.time, "time", return_value=1000):
            context = await client._skland_context(credential)

        self.assertEqual((context.cred, context.sign_token), ("oauth-code-2", "global-sign-token"))
        self.assertEqual(grant_count, 2)
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/user/oauth2/v2/grant",
                "/web/v1/user/auth/generate_cred_by_code",
                "/user/oauth2/v2/grant",
                "/api/v1/user/auth/generate_cred_by_code",
                "/web/v1/auth/refresh",
            ],
        )
        await http.aclose()

    async def test_skport_gacha_token_and_records_stay_on_global_hosts(self):
        requests = []

        async def handler(request: httpx.Request):
            requests.append(request)
            if request.url.host == "as.gryphline.com":
                return httpx.Response(200, json={"status": 0, "data": {"token": "oauth"}})
            if request.url.host == "binding-api-account-prod.gryphline.com":
                return httpx.Response(200, json={"status": 0, "data": {"token": "u8-global"}})
            if request.url.path == "/api/record/char":
                return httpx.Response(200, json={"code": 0, "data": {"list": []}})
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        credential = client_module.encode_account_credential(
            "global-account-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )
        scoped_u8 = await client.get_u8_token(credential, "binding-global")
        await client.character_records(mock.Mock(role_id="role", server_id="2"), scoped_u8, "special")

        self.assertEqual(
            client_module.decode_service_token(scoped_u8),
            (client_module.ACCOUNT_PROVIDER_SKPORT, "u8-global"),
        )
        self.assertEqual(
            [request.url.host for request in requests],
            [
                "as.gryphline.com",
                "binding-api-account-prod.gryphline.com",
                "ef-webview.gryphline.com",
            ],
        )
        await http.aclose()

    async def test_skport_currency_query_fails_before_contacting_cn_service(self):
        http = mock.AsyncMock()
        client = client_module.EndfieldOfficialClient(http)
        credential = client_module.encode_account_credential(
            "global-account-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )
        with self.assertRaises(client_module.EndfieldAPIError):
            await client.currency_balances(credential, mock.Mock())
        http.request.assert_not_awaited()

    async def test_attendance_reads_object_award_ids(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(return_value={
            "code": 0,
            "data": {
                "awardIds": [{"id": "attendance_reward", "type": 3}],
                "resourceInfoMap": {
                    "attendance_reward": {"name": "嵌晶玉", "count": 80},
                },
            },
        })
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.rewards, (client_module.AttendanceReward("嵌晶玉", 80),))

    async def test_attendance_keeps_scalar_award_id_compatibility(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(return_value={
            "code": 0,
            "data": {
                "awardIds": ["attendance_reward"],
                "resourceInfoMap": {
                    "attendance_reward": {"name": "奖励", "quantity": 2},
                },
            },
        })
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.rewards, (client_module.AttendanceReward("奖励", 2),))

    async def test_attendance_uses_calendar_resource_map_when_post_map_has_no_name(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {
                "code": 0,
                "data": {
                    "awardIds": [{"id": "endfield_attendance_4_2", "type": 3}],
                    "resourceInfoMap": {"endfield_attendance_4_2": {"count": 2}},
                },
            },
            {
                "code": 0,
                "data": {
                    "calendar": [{"awardId": "endfield_attendance_4_2", "done": True}],
                    "resourceInfoMap": {
                        "endfield_attendance_4_2": {"name": "签到道具", "count": 2},
                    },
                },
            },
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.rewards, (client_module.AttendanceReward("签到道具", 2),))

    async def test_attendance_maps_skland_alias_with_akedata_icon_id(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {
                "code": 0,
                "data": {
                    "awardIds": [{"id": "endfield_attendance_4_2", "type": 3}],
                    "resourceInfoMap": {
                        "endfield_attendance_4_2": {
                            "id": "endfield_attendance_4_2",
                            "name": "endfield_attendance_4_2",
                            "icon": "https://data.akedata.wiki/itemiconbig/item_diamond.png",
                            "count": 2,
                        },
                    },
                },
            },
            {"code": 0, "data": {"calendar": []}},
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(
            result.rewards,
            (
                client_module.AttendanceReward(
                    "嵌晶玉", 2, "https://data.akedata.wiki/itemiconbig/item_diamond.png"
                ),
            ),
        )

    async def test_attendance_builds_akedata_icon_url_from_icon_id(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {
                "code": 0,
                "data": {
                    "awardIds": [{"id": "endfield_attendance_4_2", "type": 3}],
                    "resourceInfoMap": {
                        "endfield_attendance_4_2": {
                            "name": "endfield_attendance_4_2",
                            "icon": "item_diamond",
                            "count": 2,
                        },
                    },
                },
            },
            {"code": 0, "data": {"calendar": []}},
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.rewards[0].name, "嵌晶玉")
        self.assertEqual(
            result.rewards[0].icon_url,
            "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/"
            "gameplay/ui/sprites/itemiconbig/item_diamond.png",
        )

    async def test_attendance_replaces_generic_service_name_with_akedata_name(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {
                "code": 0,
                "data": {
                    "awardIds": [{"id": "endfield_attendance_4_2", "type": 3}],
                    "resourceInfoMap": {
                        "endfield_attendance_4_2": {
                            "name": "签到奖励",
                            "icon": "item_gold",
                            "count": 2000,
                        },
                    },
                },
            },
            {"code": 0, "data": {"calendar": []}},
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.rewards[0].name, "折金票")
        self.assertEqual(result.rewards[0].count, 2000)

    async def test_attendance_does_not_expose_unresolved_award_id(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {
                "code": 0,
                "data": {"awardIds": ["endfield_attendance_unknown"]},
            },
            {"code": 0, "data": {"calendar": []}},
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.rewards, (client_module.AttendanceReward("签到奖励", 1),))

    async def test_attendance_counts_completed_days_from_month_calendar(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {"code": 0, "data": {"awardIds": []}},
            {
                "code": 0,
                "data": {
                    "calendar": [
                        {"awardId": "day-1", "done": True},
                        {"awardId": "day-2", "done": False},
                        {"awardId": "day-3", "done": True},
                    ],
                },
            },
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.monthly_count, 2)
        self.assertEqual(
            [call.args[1] for call in client._signed_skland_request.await_args_list],
            ["POST", "GET"],
        )

    async def test_attendance_keeps_success_when_month_calendar_fails(self):
        client = client_module.EndfieldOfficialClient(mock.AsyncMock())
        client._skland_context = mock.AsyncMock(return_value=object())
        client._signed_skland_request = mock.AsyncMock(side_effect=[
            {"code": 0, "data": {"awardIds": []}},
            client_module.EndfieldAPIError("森空岛请求", message="网络请求失败"),
        ])
        role = mock.Mock(role_id="role", server_id="1")

        result = await client.attendance("account-token", role)

        self.assertEqual(result.status, "success")
        self.assertIsNone(result.monthly_count)

    async def test_attendance_treats_http_403_business_code_as_already_signed(self):
        async def handler(request: httpx.Request):
            if request.method == "POST":
                return httpx.Response(403, json={"code": 10001, "message": "already signed"})
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "calendar": [
                        {"awardId": "day-1", "done": True},
                        {"awardId": "day-2", "done": True},
                    ],
                },
            })

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        client._skland_context = mock.AsyncMock(
            return_value=client_module._SklandContext("cred", "sign-token", 1000, 1000, 99999)
        )
        role = mock.Mock(role_id="role", server_id="1")

        with mock.patch.object(client_module.time, "time", return_value=1000):
            result = await client.attendance("account-token", role)

        self.assertEqual(result.status, "already")
        self.assertEqual(result.rewards, ())
        self.assertEqual(result.monthly_count, 2)
        await http.aclose()

    async def test_signed_empty_post_matches_documented_algorithm(self):
        captured = {}

        async def handler(request: httpx.Request):
            captured["headers"] = request.headers
            captured["content"] = request.content
            return httpx.Response(200, json={"code": 0, "data": {}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        context = client_module._SklandContext("cred", "sign-token", 1000, 1000, 99999)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            await client._signed_skland_request(
                context,
                "POST",
                "/web/v1/game/endfield/attendance",
                raw_body="",
                extra_headers={"sk-game-role": "3_role_server"},
            )
        timestamp = "1000"
        sign_headers = {"platform": "3", "timestamp": timestamp, "dId": "", "vName": "1.0.0"}
        canonical = "/web/v1/game/endfield/attendance" + timestamp + json.dumps(
            sign_headers, ensure_ascii=False, separators=(",", ":")
        )
        digest = hmac.new(b"sign-token", canonical.encode(), hashlib.sha256).hexdigest()
        expected = hashlib.md5(digest.encode()).hexdigest()
        self.assertEqual(captured["headers"]["sign"], expected)
        self.assertEqual(captured["headers"]["sk-game-role"], "3_role_server")
        self.assertEqual(captured["content"], b"")
        await http.aclose()

    async def test_card_detail_signs_the_urlencoded_get_query(self):
        captured = {}

        async def handler(request: httpx.Request):
            captured["headers"] = request.headers
            captured["url"] = request.url
            captured["content"] = request.content
            return httpx.Response(200, json={"code": 0, "data": {"detail": {"base": {"name": "otae"}}}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        context = client_module._SklandContext("cred", "sign-token", 1000, 1000, 99999)
        client._skland_context = mock.AsyncMock(return_value=context)
        role = mock.Mock(role_id="role-a", server_id="1")
        with mock.patch.object(client_module.time, "time", return_value=1000):
            detail = await client.card_detail("account-token", role)

        timestamp = "1000"
        query = "roleId=role-a&serverId=1"
        sign_headers = {"platform": "3", "timestamp": timestamp, "dId": "", "vName": "1.0.0"}
        canonical = "/api/v1/game/endfield/card/detail" + query + timestamp + json.dumps(
            sign_headers, ensure_ascii=False, separators=(",", ":")
        )
        digest = hmac.new(b"sign-token", canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(captured["headers"]["sign"], hashlib.md5(digest.encode()).hexdigest())
        self.assertEqual(captured["url"].query, query.encode())
        self.assertNotIn("sk-game-role", captured["headers"])
        self.assertEqual(captured["content"], b"")
        self.assertEqual(detail, {"base": {"name": "otae"}})
        client._skland_context.assert_awaited_once_with("account-token")
        await http.aclose()

    async def test_card_detail_reuses_cached_community_context(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        client = client_module.EndfieldOfficialClient(
            http,
            community_exchange_interval_seconds=0,
        )
        context = client_module._SklandContext(
            "cred",
            "sign-token",
            1000,
            1000,
            float("inf"),
        )
        client._create_skland_context = mock.AsyncMock(return_value=context)
        client._signed_skland_request = mock.AsyncMock(
            return_value={"code": 0, "data": {"detail": {"base": {"name": "otae"}}}}
        )
        role = mock.Mock(role_id="role-a", server_id="1")

        first = await client.card_detail("account-token", role)
        second = await client.card_detail("account-token", role)

        self.assertEqual(first, second)
        self.assertEqual(client._create_skland_context.await_count, 1)
        self.assertEqual(client._signed_skland_request.await_count, 2)
        self.assertEqual(
            client.community_context_metrics(),
            {
                "cache_hits": 1,
                "singleflight_reuses": 0,
                "exchange_attempts": 1,
                "exchange_succeeded": 1,
                "exchange_failed": 0,
                "circuit_rejections": 0,
            },
        )
        await http.aclose()

    async def test_card_detail_refreshes_context_once_after_stale_signature(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        client = client_module.EndfieldOfficialClient(http)
        stale = client_module._SklandContext("old", "old-sign", 1000, 1000, float("inf"))
        fresh = client_module._SklandContext("new", "new-sign", 1000, 1000, float("inf"))
        client._skland_context = mock.AsyncMock(side_effect=[stale, fresh])
        client._signed_skland_request = mock.AsyncMock(
            side_effect=[
                client_module.EndfieldAPIError("社区请求", "401", "签名已失效"),
                {"code": 0, "data": {"detail": {"base": {"name": "otae"}}}},
            ]
        )

        detail = await client.card_detail(
            "account-token",
            mock.Mock(role_id="role-a", server_id="1"),
        )

        self.assertEqual(detail, {"base": {"name": "otae"}})
        self.assertEqual(
            client._skland_context.await_args_list,
            [
                mock.call("account-token"),
                mock.call(
                    "account-token",
                    refresh=True,
                    stale_context=stale,
                ),
            ],
        )
        self.assertEqual(client._signed_skland_request.await_count, 2)
        await http.aclose()

    async def test_forced_context_refresh_reuses_newer_singleflight_generation(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        client = client_module.EndfieldOfficialClient(
            http,
            community_exchange_interval_seconds=0,
        )
        stale = client_module._SklandContext("old", "old", 1000, 1000, float("inf"))
        fresh = client_module._SklandContext("new", "new", 1000, 1000, float("inf"))
        provider, raw_token = client_module.decode_account_credential("account-token")
        key = hashlib.sha256(f"{provider}:{raw_token}".encode()).hexdigest()[:24]
        client._skland_cache[key] = stale
        client._create_skland_context = mock.AsyncMock(return_value=fresh)

        first, second = await asyncio.gather(
            client._skland_context(
                "account-token",
                refresh=True,
                stale_context=stale,
                background=True,
            ),
            client._skland_context(
                "account-token",
                refresh=True,
                stale_context=stale,
                background=True,
            ),
        )

        self.assertIs(first, fresh)
        self.assertIs(second, fresh)
        client._create_skland_context.assert_awaited_once()
        metrics = client.community_context_metrics()
        self.assertEqual(metrics["singleflight_reuses"], 1)
        self.assertEqual(metrics["exchange_attempts"], 1)
        await http.aclose()

    async def test_foreground_context_exchange_overtakes_waiting_background_exchange(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        client = client_module.EndfieldOfficialClient(
            http,
            community_exchange_interval_seconds=0,
        )
        order = []

        async def create(token, provider, _config):
            order.append(token)
            return client_module._SklandContext(
                token,
                token,
                1000,
                1000,
                float("inf"),
                provider,
            )

        client._create_skland_context = mock.AsyncMock(side_effect=create)
        await client._skland_exchange_lock.acquire()
        background = asyncio.create_task(
            client._skland_context("background-token", background=True)
        )
        await asyncio.sleep(0)
        foreground = asyncio.create_task(client._skland_context("foreground-token"))
        await asyncio.sleep(0)
        client._skland_exchange_lock.release()

        await asyncio.gather(foreground, background)
        self.assertEqual(order, ["foreground-token", "background-token"])
        await http.aclose()

    async def test_systemic_context_failure_opens_shared_exchange_circuit(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        client = client_module.EndfieldOfficialClient(
            http,
            community_exchange_interval_seconds=0,
        )
        client._create_skland_context = mock.AsyncMock(
            side_effect=client_module.EndfieldAPIError(
                "获取社区凭据",
                "405",
                "unavailable",
            )
        )

        with self.assertRaises(client_module.EndfieldAPIError):
            await client._skland_context("first", background=True)
        with self.assertRaises(client_module.EndfieldAPIError) as caught:
            await client._skland_context("second", background=True)

        self.assertEqual(caught.exception.code, "405")
        self.assertGreater(caught.exception.retry_after_seconds, 0)
        client._create_skland_context.assert_awaited_once()
        metrics = client.community_context_metrics()
        self.assertEqual(metrics["exchange_failed"], 1)
        self.assertEqual(metrics["circuit_rejections"], 1)
        await http.aclose()

    async def test_json_request_preserves_retry_after_header(self):
        async def handler(_request: httpx.Request):
            return httpx.Response(
                429,
                headers={"Retry-After": "17"},
                json={"code": 429, "message": "slow down"},
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        with self.assertRaises(client_module.EndfieldAPIError) as caught:
            await client._json_request("获取社区凭据", "GET", "https://example.test")

        self.assertEqual(caught.exception.code, "429")
        self.assertEqual(caught.exception.retry_after_seconds, 17)
        await http.aclose()

    async def test_card_detail_rejects_missing_detail_payload(self):
        async def handler(request: httpx.Request):
            return httpx.Response(200, json={"code": 0, "data": {}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        client._skland_context = mock.AsyncMock(
            return_value=client_module._SklandContext("cred", "sign-token", 1000, 1000, 99999)
        )
        with mock.patch.object(client_module.time, "time", return_value=1000):
            with self.assertRaises(client_module.EndfieldAPIError) as caught:
                await client.card_detail("account-token", mock.Mock(role_id="role-a", server_id="1"))
        self.assertIn("查询终末地档案", str(caught.exception))
        await http.aclose()

    async def test_currency_balances_reuse_account_and_u8_tokens(self):
        captured = []

        async def handler(request: httpx.Request):
            body = json.loads(request.content)
            captured.append((body["currencyType"], request.headers))
            return httpx.Response(200, json={
                "code": 0,
                "data": {"list": [{"currencyType": body["currencyType"], "after": body["currencyType"] * 100}]},
            })

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        client.get_u8_token = mock.AsyncMock(return_value="u8-token")
        role = mock.Mock(binding_uid="binding", server_id="1")

        balances = await client.currency_balances("account-token", role)

        self.assertEqual(balances, {1: 100, 2: 200, 3: 300})
        self.assertEqual({currency_type for currency_type, _headers in captured}, {1, 2, 3})
        for _currency_type, headers in captured:
            self.assertEqual(headers["x-account-token"], "account-token")
            self.assertEqual(headers["x-role-token"], "u8-token")
            self.assertEqual(headers["x-role-server-id"], "1")
        client.get_u8_token.assert_awaited_once_with("account-token", "binding")
        await http.aclose()

    async def test_currency_logs_page_through_seq_id_and_stop_at_start(self):
        captured = []

        async def handler(request: httpx.Request):
            body = json.loads(request.content)
            captured.append(body)
            if "seqId" not in body:
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"currencyType": 1, "changeType": 1, "changeReason": "13", "changeNum": 100, "after": 300, "changeTime": 300, "seqId": 2},
                            {"currencyType": 1, "changeType": 2, "changeReason": "10", "changeNum": 20, "after": 200, "changeTime": 200, "seqId": 1},
                        ],
                        "hasNext": True,
                    },
                })
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "list": [
                        {"currencyType": 1, "changeType": 1, "changeReason": "14", "changeNum": 5, "after": 105, "changeTime": 50, "seqId": 0},
                    ],
                    "hasNext": False,
                },
            })

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        client.get_u8_token = mock.AsyncMock(return_value="u8-token")
        role = mock.Mock(binding_uid="binding", server_id="1")
        with mock.patch.object(client_module.asyncio, "sleep", new=mock.AsyncMock()):
            result = await client.currency_logs(
                "account-token", role, currency_types=(1,), start_ts=100, end_ts=400, limit=2
            )

        self.assertEqual([item.seq_id for item in result[1]], [2, 1])
        self.assertEqual(captured[1]["seqId"], 1)
        self.assertEqual(captured[0]["currencyType"], 1)
        self.assertEqual(captured[0]["limit"], 2)
        client.get_u8_token.assert_awaited_once_with("account-token", "binding")
        await http.aclose()

    def test_aggregates_currency_logs_and_formats_reason_totals(self):
        records = (
            client_module.CurrencyLogItem(1, 1, "13", 100, 200, 1000, 1),
            client_module.CurrencyLogItem(1, 2, "25", 30, 170, 1010, 2),
        )
        summary = currency_module.aggregate_currency_logs(records, 1)
        self.assertEqual((summary.opening, summary.closing, summary.gain, summary.consume, summary.net), (100, 170, 100, 30, 70))
        report = currency_module.format_currency_log_report(
            (summary,), role_label="测试角色", start=currency_module.date(1970, 1, 1), end=currency_module.date(1970, 1, 1), change_type=0
        )
        self.assertIn("任务奖励", report)
        self.assertIn("武库交易所消耗", report)
        self.assertIn("类型汇总", report)
        self.assertNotIn("变动后", report)

    async def test_character_and_weapon_record_mapping(self):
        captured_queries = []
        responses = {
            "/api/record/char": {
                "code": 0,
                "data": {"list": [
                    {"poolId": "p", "poolName": "角色池", "charId": "c", "charName": "角色", "rarity": 5, "gachaTs": 20, "seqId": "2", "isFree": True},
                    {"kind": "gift_intel_book", "nameText": "寻访情报书", "seqId": "1"},
                ], "hasMore": True},
            },
            "/api/record/weapon": {
                "code": 0,
                "data": {"list": [{"poolId": "w", "poolName": "武器池", "weaponId": "x", "weaponName": "武器", "weaponType": "剑", "rarity": 4, "gachaTs": 10, "seqId": "1"}], "hasMore": False},
            },
        }

        async def handler(request: httpx.Request):
            captured_queries.append(request.url.params)
            return httpx.Response(200, json=responses[request.url.path])

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.EndfieldOfficialClient(http)
        role = types.SimpleNamespace(role_id="role", server_id="server")
        chars = await client.character_records(role, "u8", "special")
        weapons = await client.weapon_records(role, "u8")
        self.assertEqual([query["lang"] for query in captured_queries], ["zh-cn", "zh-cn"])
        self.assertNotIn("pool_id", captured_queries[1])
        self.assertEqual((len(chars.records), chars.records[0].rarity, chars.next_seq_id, chars.has_more), (1, 5, "1", True))
        self.assertTrue(chars.records[0].is_free)
        self.assertEqual(weapons.records[0].pool_id, "w")
        self.assertEqual((weapons.records[0].rarity, weapons.records[0].weapon_type), (4, "剑"))
        await http.aclose()


class EndfieldGachaAssetCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_names_matches_catalog_and_caches_six_star_icon(self):
        cache = gacha_assets_module.EndfieldGachaAssetCache(types.SimpleNamespace())
        item = gacha_assets_module.GachaItemMetadata(
            "char_42", "42 式", 6, "角色", icon_url="https://assets.fz.wiki/42.png@raw"
        )
        with (
            mock.patch.object(cache, "_load_catalog", mock.AsyncMock(return_value={item.item_id: item})),
            mock.patch.object(
                cache, "_cache_images", mock.AsyncMock(return_value={item.item_id: "C:/cache/42.png"})
            ) as cache_images,
        ):
            result = await cache.prepare_names(["42式"])

        self.assertEqual(result["42式"].icon_path, "C:/cache/42.png")
        cache_images.assert_awaited_once()

    async def test_prepare_pool_banners_passes_portrait_and_weapon_candidates(self):
        operator = gacha_assets_module.GachaItemMetadata(
            "chr_up", "UP干员", 6, "角色", icon_url="https://assets.fz.wiki/operator.png",
        )
        weapon = gacha_assets_module.GachaItemMetadata(
            "wpn_up", "UP武器", 6, "武器", icon_url="https://assets.fz.wiki/weapon.png",
        )
        rules = {
            "character-pool": gacha_assets_module.GachaPoolRule(
                "character-pool", (operator.item_id,), 120,
            ),
            "weapon-pool": gacha_assets_module.GachaPoolRule(
                "weapon-pool", (weapon.item_id,), 80,
            ),
        }
        cache = gacha_assets_module.EndfieldGachaAssetCache(types.SimpleNamespace())
        with (
            mock.patch.object(
                cache, "_load_catalog",
                mock.AsyncMock(return_value={operator.item_id: operator, weapon.item_id: weapon}),
            ),
            mock.patch.object(
                cache, "_cache_images",
                mock.AsyncMock(return_value={
                    "banner_chr_up": "C:/cache/banner_chr_up.webp",
                    "wpn_up": "C:/cache/wpn_up.png",
                }),
            ) as cache_images,
            mock.patch.object(
                cache, "_crop_character_banner",
                return_value="C:/cache/banner_bust_chr_up.webp",
            ) as crop_character_banner,
        ):
            result = await cache.prepare_pool_banners(rules)

        requested = {item.item_id: item.icon_url for item in cache_images.await_args.args[0]}
        self.assertEqual(
            cache_images.await_args.kwargs["candidates"]["banner_chr_up"][0],
            "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/characterportrait/chr_up.png",
        )
        self.assertEqual(requested["wpn_up"], weapon.icon_url)
        self.assertEqual(result["character-pool"][0].name, "UP干员")
        self.assertEqual(result["character-pool"][0].image_path, "C:/cache/banner_bust_chr_up.webp")
        crop_character_banner.assert_called_once_with("chr_up", "C:/cache/banner_chr_up.webp")

    def test_character_banner_crop_keeps_centered_upper_body_region(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = gacha_assets_module.EndfieldGachaAssetCache(
                types.SimpleNamespace(), cache_dir=directory,
            )
            source = Path(directory) / "source.png"
            Image.new("RGBA", (1000, 1000), (120, 80, 40, 255)).save(source)

            result = Path(cache._crop_character_banner("chr_up", str(source)))

            with Image.open(result) as cropped:
                self.assertEqual(cropped.size, (500, 338))
            self.assertEqual(result.name, "banner_bust_v1_chr_up.webp")

    async def test_prepare_keepsakes_uses_fz_item_name_icon_and_cache(self):
        operator = gacha_assets_module.GachaItemMetadata(
            "chr_0017_yvonne", "伊冯", 6, "角色",
        )
        payload = {
            "revision": {
                "contentJson": {
                    "content": [{
                        "attrs": {
                            "hero": {
                                "id": "item_charpotentialup_chr_0017_yvonne",
                                "name": "伊冯的信物",
                                "rarity": 6,
                                "typeCode": "CharPotentialUp",
                                "iconUrl": "https://assets.fz.wiki/yvonne-keepsake.png",
                            },
                        },
                    }],
                },
            },
        }
        client = types.SimpleNamespace(
            fz_article_by_title=mock.AsyncMock(return_value=payload),
        )
        cache = gacha_assets_module.EndfieldGachaAssetCache(
            types.SimpleNamespace(client=client),
        )
        rules = {
            "special": gacha_assets_module.GachaPoolRule(
                "special", (operator.item_id,), 120,
            ),
        }
        with (
            mock.patch.object(cache, "_load_catalog", mock.AsyncMock(return_value={operator.item_id: operator})),
            mock.patch.object(cache, "_existing_icon_path", return_value=""),
            mock.patch.object(cache, "_normalize_keepsake_icon", side_effect=lambda _item_id, path: path),
            mock.patch.object(
                cache,
                "_cache_images",
                mock.AsyncMock(return_value={
                    "item_charpotentialup_chr_0017_yvonne": "C:/cache/yvonne-keepsake.png",
                }),
            ) as cache_images,
        ):
            result = await cache.prepare_keepsakes(rules)

        keepsake = result[operator.item_id]
        self.assertEqual(keepsake.name, "伊冯的信物")
        self.assertEqual(keepsake.item_id, "item_charpotentialup_chr_0017_yvonne")
        self.assertEqual(keepsake.icon_path, "C:/cache/yvonne-keepsake.png")
        client.fz_article_by_title.assert_awaited_once_with("物品/干员信物/伊冯的信物")
        self.assertEqual(cache_images.await_args.args[0][0].icon_url, payload["revision"]["contentJson"]["content"][0]["attrs"]["hero"]["iconUrl"])

    def test_normalize_keepsake_icon_crops_transparent_canvas(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = gacha_assets_module.EndfieldGachaAssetCache(
                types.SimpleNamespace(), cache_dir=directory,
            )
            source = Path(directory) / "source.webp"
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            image.paste((255, 255, 255, 255), (20, 70, 155, 210))
            image.save(source, format="WEBP")

            output = Path(cache._normalize_keepsake_icon("keepsake", str(source)))
            normalized = Image.open(output).convert("RGBA")

        self.assertEqual(output.name, "keepsake_v1_keepsake.png")
        self.assertEqual(normalized.width, normalized.height)
        self.assertLess(normalized.width, 180)
        self.assertGreater(normalized.getchannel("A").getbbox()[0], 0)

    def test_normalize_keepsake_icon_recenters_akedata_png_already_in_cache(self):
        item_id = "item_charpotentialup_chr_0034_typhoea"
        with tempfile.TemporaryDirectory() as directory:
            cache = gacha_assets_module.EndfieldGachaAssetCache(
                types.SimpleNamespace(), cache_dir=directory,
            )
            source = Path(directory) / f"{item_id}.png"
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            image.paste((200, 160, 180, 255), (15, 68, 155, 210))
            image.save(source, format="PNG")

            output = Path(cache._normalize_keepsake_icon(item_id, str(source)))
            with Image.open(output) as normalized:
                normalized = normalized.convert("RGBA")
                bbox = normalized.getchannel("A").getbbox()
                width, height = normalized.size

            again = Path(cache._normalize_keepsake_icon(item_id, str(source)))

        self.assertEqual(output.name, f"keepsake_v1_{item_id}.png")
        self.assertNotEqual(output.resolve(), source.resolve())
        self.assertEqual(width, height)
        self.assertLess(width, 180)
        self.assertGreater(bbox[0], 0)
        self.assertGreater(bbox[1], 0)
        self.assertGreater(bbox[2] - bbox[0], width * 0.7)
        self.assertAlmostEqual(bbox[0], width - bbox[2], delta=2)
        self.assertAlmostEqual(bbox[1], height - bbox[3], delta=2)
        self.assertEqual(again.resolve(), output.resolve())

    async def test_prepare_pool_rules_loads_entire_directory_without_existing_records(self):
        class FakeClient:
            def __init__(self):
                self.requested_titles = []

            async def fz_article_summaries(self, prefix):
                self.prefix = prefix
                return {
                    "articles": [
                        {"title": "卡池/历史角色池"},
                        {"title": "卡池/历史武器池"},
                    ]
                }

            async def fz_article_by_title(self, title):
                self.requested_titles.append(title)
                if title.endswith("角色池"):
                    return {
                        "poolId": "special-old",
                        "poolName": "历史角色池",
                        "poolKind": "char",
                        "upItemIds": ["chr-old"],
                    }
                return {
                    "poolId": "weapon-old",
                    "poolName": "历史武器池",
                    "poolKind": "weapon",
                    "upItemIds": ["wpn-old"],
                }

        client = FakeClient()
        service = types.SimpleNamespace(client=client)
        rules = await gacha_assets_module.EndfieldGachaAssetCache(service).prepare_pool_rules([])

        self.assertEqual(client.prefix, "卡池/")
        self.assertEqual(
            set(client.requested_titles),
            {"卡池/历史角色池", "卡池/历史武器池"},
        )
        self.assertEqual(set(rules), {"special-old", "weapon-old"})

    async def test_fz_catalog_corrects_rarity_and_caches_high_star_images(self):
        operator = types.SimpleNamespace(
            operator_id="chr_0005_chen",
            name="陈千语",
            rarity=5,
            icon_url="https://assets.fz.wiki/chen.png@raw",
        )
        weapon = types.SimpleNamespace(
            weapon_id="wpn_lance_0011",
            name="J.E.T.",
            rarity=6,
            weapon_type="长柄武器",
            icon_url="https://assets.fz.wiki/jet.png@raw",
        )

        class FakeService:
            async def get_operator_catalog_view(self):
                profession = types.SimpleNamespace(items=[operator])
                return types.SimpleNamespace(elements=[types.SimpleNamespace(professions=[profession])])

            async def get_weapon_catalog_view(self):
                return types.SimpleNamespace(groups=[types.SimpleNamespace(items=[weapon])])

        records = [
            store_module.GachaRecord("role", "server", "p", "池", "x", "1", 1, operator.operator_id, "错误六星", 6, "角色"),
            store_module.GachaRecord("role", "server", "p", "池", "x", "2", 2, weapon.weapon_id, weapon.name, 5, "武器"),
        ]
        resource = types.SimpleNamespace(content=b"image-bytes", content_type="image/png")
        with tempfile.TemporaryDirectory() as directory:
            cache = gacha_assets_module.EndfieldGachaAssetCache(FakeService(), cache_dir=directory)
            with mock.patch.object(
                gacha_assets_module,
                "fetch_many_resilient",
                mock.AsyncMock(side_effect=lambda urls, **kwargs: {url: resource for url in urls}),
            ) as fetch:
                metadata = await cache.prepare(records)
                second = await cache.prepare(records)
            self.assertEqual(metadata[operator.operator_id].rarity, 5)
            self.assertEqual(metadata[weapon.weapon_id].rarity, 6)
            self.assertTrue(Path(metadata[weapon.weapon_id].icon_path).is_file())
            self.assertEqual(second[weapon.weapon_id].icon_path, metadata[weapon.weapon_id].icon_path)
            self.assertEqual(fetch.await_count, 1)
            corrected = gacha_assets_module.apply_gacha_metadata(records, metadata)
            self.assertEqual((corrected[0].item_name, corrected[0].rarity), ("陈千语", 5))
            self.assertEqual((corrected[1].item_name, corrected[1].rarity), ("J.E.T.", 6))

    async def test_image_download_failure_keeps_metadata_without_icon(self):
        operator = types.SimpleNamespace(
            operator_id="chr_test",
            name="测试六星",
            rarity=6,
            icon_url="https://assets.fz.wiki/missing.png@raw",
        )

        class FakeService:
            async def get_operator_catalog_view(self):
                profession = types.SimpleNamespace(items=[operator])
                return types.SimpleNamespace(elements=[types.SimpleNamespace(professions=[profession])])

            async def get_weapon_catalog_view(self):
                return types.SimpleNamespace(groups=[])

        record = store_module.GachaRecord("role", "server", "p", "池", "x", "1", 1, operator.operator_id, operator.name, 6, "角色")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            gacha_assets_module,
            "fetch_many_resilient",
            mock.AsyncMock(return_value={operator.icon_url: None}),
        ):
            metadata = await gacha_assets_module.EndfieldGachaAssetCache(
                FakeService(), cache_dir=directory
            ).prepare([record])
        self.assertEqual(metadata[operator.operator_id].rarity, 6)
        self.assertEqual(metadata[operator.operator_id].icon_path, "")

    def test_extract_gacha_pool_rules_reads_up_ids_and_hard_guarantee(self):
        payload = {
            "revision": {
                "contentJson": {
                    "content": [{
                        "attrs": {
                            "hero": {
                                "poolId": "special_1_4_1",
                                "upItems": [{
                                    "id": "chr_up",
                                    "name": "当期UP",
                                    "iconUrl": "https://assets.fz.wiki/up.png",
                                }],
                                "simulator": {
                                    "pools": [{
                                        "poolId": "special_1_4_1",
                                        "upItemIds": "chr_up chr_second",
                                        "hardGuarantee": 120,
                                    }]
                                }
                            }
                        }
                    }]
                }
            }
        }

        rules = gacha_assets_module.extract_gacha_pool_rules(payload)

        self.assertEqual(rules["special_1_4_1"].up_item_ids, ("chr_up", "chr_second"))
        self.assertEqual(rules["special_1_4_1"].hard_guarantee, 120)
        self.assertEqual(rules["special_1_4_1"].pool_kind, "char")

    def test_extract_gacha_pool_rules_keeps_weapon_catalog_identity(self):
        payload = {
            "poolId": "weponbox_1_0_1",
            "poolName": "熔铸申领",
            "poolKind": "weapon",
            "upItemIds": ["wpn_up"],
            "hardGuarantee": 80,
        }

        rule = gacha_assets_module.extract_gacha_pool_rules(payload)["weponbox_1_0_1"]

        self.assertEqual((rule.pool_name, rule.pool_kind), ("熔铸申领", "weapon"))


class _FakeGachaClient:
    def __init__(self, *, fail_pool: str = ""):
        self.fail_pool = fail_pool
        self.calls = []
        self.weapon_calls = []

    async def get_u8_token(self, token, binding_uid):
        return "u8"

    async def character_pool_names(self, token, server_id):
        return {pool: pool.rsplit("_", 1)[-1] for pool in client_module.CHARACTER_POOL_TYPES}

    async def weapon_pools(self, token, server_id):
        return [("weapon-1", "武器池")]

    async def character_records(self, role, token, pool_type, *, seq_id="", pool_name=""):
        self.calls.append((pool_type, seq_id))
        if pool_type == self.fail_pool:
            raise client_module.EndfieldAPIError("同步角色抽卡", "500", "失败")
        records = () if seq_id else (
            store_module.GachaRecord(role.role_id, role.server_id, pool_type, pool_name, pool_type, f"{pool_type}-2", 20, "c2", "六星", 6, "角色"),
            store_module.GachaRecord(role.role_id, role.server_id, pool_type, pool_name, pool_type, f"{pool_type}-1", 10, "c1", "五星", 5, "角色"),
        )
        return client_module.GachaPage(records, False, records[-1].seq_id if records else "")

    async def weapon_records(self, role, token, pool_id="", *, seq_id="", pool_name=""):
        self.weapon_calls.append((pool_id, pool_name, seq_id))
        record = store_module.GachaRecord(
            role.role_id, role.server_id, "weapon-1", "武器池", "weapon", "w-1", 15,
            "w", "武器", 5, "武器",
        )
        return client_module.GachaPage((record,), False, record.seq_id)


class EndfieldGachaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_module.EndfieldStore(Path(self.temp.name) / "endfield.db")
        self.cipher = crypto.CredentialCipher(b"k" * 32)
        self.role = self.store.bind_roles(
            "qq", "token", [store_module.RoleCandidate("bind", "role", "server", "甲")], self.cipher
        )[0]

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_full_then_incremental_stops_at_saved_boundary(self):
        fake = _FakeGachaClient()
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)
        first = await service.sync(self.role, full=True)
        second = await service.sync(self.role, full=False)
        self.assertEqual(first.inserted, 9)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(self.store.count_gacha_records(self.role), 9)

    async def test_partial_pool_failure_keeps_successful_records(self):
        failed_pool = client_module.CHARACTER_POOL_TYPES[1]
        service = gacha_module.EndfieldGachaService(self.store, _FakeGachaClient(fail_pool=failed_pool), self.cipher)
        result = await service.sync(self.role, full=True)
        self.assertEqual(len(result.failed), 1)
        self.assertGreater(result.inserted, 0)
        analysis = service.analysis(self.role)
        self.assertFalse(analysis.complete)
        self.assertTrue(analysis.errors)

    async def test_sync_uses_one_global_weapon_stream(self):
        fake = _FakeGachaClient()
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)
        rules = {
            "weapon-old": gacha_assets_module.GachaPoolRule(
                "weapon-old", ("wpn-old",), 80, "历史武器池", "weapon",
            ),
            "character-old": gacha_assets_module.GachaPoolRule(
                "character-old", ("chr-old",), 120, "历史角色池", "char",
            ),
        }

        result = await service.sync(self.role, full=True, pool_rules=rules)

        self.assertFalse(result.failed)
        self.assertEqual(fake.weapon_calls, [("", "", "")])

    async def test_skport_sync_fetches_each_required_weapon_pool(self):
        credential = client_module.encode_account_credential(
            "global-token", client_module.ACCOUNT_PROVIDER_SKPORT
        )
        global_role = self.store.bind_roles(
            "global-qq",
            credential,
            [store_module.RoleCandidate("global-bind", "global-role", "2", "亚服角色", "Asia")],
            self.cipher,
        )[0]
        fake = _FakeGachaClient()
        fake.weapon_pools = mock.AsyncMock(return_value=[
            ("weapon-1", "武器池一"),
            ("weapon-2", "武器池二"),
        ])
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)

        result = await service.sync(global_role, full=True)

        self.assertFalse(result.failed)
        self.assertEqual(
            sorted(fake.weapon_calls),
            [
                ("weapon-1", "武器池一", ""),
                ("weapon-2", "武器池二", ""),
            ],
        )

    async def test_task_registry_rejects_duplicate_role(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with gacha_module.ROLE_TASKS.claim(self.role):
                entered.set()
                await release.wait()

        task = asyncio.create_task(holder())
        await entered.wait()
        with self.assertRaises(gacha_module.TaskAlreadyRunning):
            async with gacha_module.ROLE_TASKS.claim(self.role):
                pass
        release.set()
        await task

    def test_analysis_handles_empty_single_and_multiple_six_stars(self):
        empty = gacha_module.build_gacha_analysis(self.role, [], [])
        self.assertIsNone(empty.average_interval)
        records = [
            store_module.GachaRecord("role", "server", "p", "池", "x", "1", 1, "a", "甲", 6, "角色"),
            store_module.GachaRecord("role", "server", "p", "池", "x", "2", 2, "b", "乙", 5, "角色"),
            store_module.GachaRecord("role", "server", "p", "池", "x", "3", 3, "c", "丙", 6, "角色"),
        ]
        result = gacha_module.build_gacha_analysis(self.role, records, [])
        self.assertEqual(result.intervals, (2,))
        self.assertEqual(result.average_interval, 2.0)

    def test_analysis_calculates_character_six_star_intervals_across_pools(self):
        records = [
            store_module.GachaRecord("role", "server", "a", "甲池", "x", "a1", 1, "a1", "甲一", 6, "角色"),
            store_module.GachaRecord("role", "server", "b", "乙池", "x", "b1", 2, "b1", "乙一", 6, "角色"),
            store_module.GachaRecord("role", "server", "a", "甲池", "x", "a2", 3, "a2", "甲垫", 5, "角色"),
            store_module.GachaRecord("role", "server", "b", "乙池", "x", "b2", 4, "b2", "乙垫", 5, "角色"),
            store_module.GachaRecord("role", "server", "a", "甲池", "x", "a3", 5, "a3", "甲二", 6, "角色"),
            store_module.GachaRecord("role", "server", "b", "乙池", "x", "b3", 6, "b3", "乙二", 6, "角色"),
        ]
        result = gacha_module.build_gacha_analysis(self.role, records, [])
        self.assertEqual(result.intervals, (1, 3, 1))
        self.assertEqual([item.interval for item in result.six_stars[:2]], [1, 3])
        pools = {item.pool_id: item for item in result.pools}
        self.assertEqual([item.interval for item in pools["a"].six_stars], [3, 1])
        self.assertEqual([item.interval for item in pools["b"].six_stars], [1, 1])

    def test_six_star_expectation_uses_official_rates_and_matching_account_samples(self):
        character = gacha_module.PoolAnalysis(
            "special", "限定池", "角色", 110, 0, paid_total=100,
            six_stars=(
                gacha_module.SixStarEvent("角色一", "限定池", "角色", 1, item_id="up-role"),
                gacha_module.SixStarEvent("角色二", "限定池", "角色", 2, item_id="off-role"),
            ),
            free_pull_count=10,
            free_batches=(gacha_module.FreePullBatch(
                3, 10, (gacha_module.SixStarEvent(
                    "免费UP角色", "限定池", "角色", 3, item_id="up-role",
                ),),
            ),),
            keepsake_gifts=(
                gacha_module.KeepsakeGift("角色二的信物", "token", 2, 240),
            ),
            up_item_ids=("up-role",),
        )
        joint = gacha_module.PoolAnalysis(
            "joint", "辉光庆典", "角色", 100, 0, paid_total=100,
            six_stars=(gacha_module.SixStarEvent("庆典", "辉光庆典", "角色", 3),),
        )
        standard = gacha_module.PoolAnalysis(
            "standard", "基础寻访", "角色", 200, 0, paid_total=200,
            six_stars=(gacha_module.SixStarEvent("常驻六星", "基础寻访", "角色", 3),),
        )
        weapon = gacha_module.PoolAnalysis(
            "weapon", "武器池", "武器", 80, 0, paid_total=80,
            six_stars=tuple(
                gacha_module.SixStarEvent(
                    f"武器{index}", "武器池", "武器", index,
                    item_id="up-weapon" if index % 2 else "off-weapon",
                )
                for index in range(4)
            ),
            up_item_ids=("up-weapon",),
        )

        role_value = gacha_module.calculate_six_star_expectation(
            (character, joint, standard, weapon), "角色",
        )
        weapon_value = gacha_module.calculate_six_star_expectation(
            (character, joint, weapon), "武器",
        )

        self.assertAlmostEqual(role_value.before_up, 49.0509, places=4)
        self.assertAlmostEqual(role_value.after_up, 44.0141, places=4)
        self.assertAlmostEqual(role_value.combined_before_up, 51.9186, places=4)
        self.assertAlmostEqual(role_value.combined_after_up, 46.7687, places=4)
        self.assertAlmostEqual(role_value.actual, 110 / 4)
        self.assertEqual(
            (role_value.paid_pulls, role_value.free_pulls, role_value.account_pulls, role_value.outcomes),
            (100, 10, 110, 4),
        )
        self.assertAlmostEqual(role_value.up_before, 81.37567692616722)
        self.assertIsNone(role_value.up_after)
        self.assertAlmostEqual(role_value.up_combined_before, 86.6914, places=4)
        self.assertIsNone(role_value.up_combined_after)
        self.assertAlmostEqual(role_value.actual_up, 110 / 3)
        self.assertEqual(role_value.up_outcomes, 3)
        self.assertAlmostEqual(weapon_value.before_up, 18.6755, places=4)
        self.assertAlmostEqual(weapon_value.after_up, 16.0741, places=4)
        self.assertEqual(weapon_value.actual, 20)
        self.assertAlmostEqual(weapon_value.up_before, 53.9578, places=4)
        self.assertAlmostEqual(weapon_value.up_after, 53.5389, places=4)
        self.assertEqual((weapon_value.actual_up, weapon_value.up_outcomes), (40, 2))

    def test_analysis_hides_standard_from_card_but_keeps_it_in_totals(self):
        records = [
            store_module.GachaRecord("role", "server", "standard", "基础寻访", "E_CharacterGachaPoolType_Standard", "s1", 1, "s", "基础角色", 6, "角色"),
            store_module.GachaRecord("role", "server", "old", "旧限定", "special", "o1", 10, "o1", "旧六星", 6, "角色"),
            store_module.GachaRecord("role", "server", "old", "旧限定", "special", "o2", 11, "o2", "垫抽", 4, "角色"),
            store_module.GachaRecord("role", "server", "old", "旧限定", "special", "o3", 12, "o3", "垫抽", 4, "角色"),
        ]
        records.extend(
            store_module.GachaRecord(
                "role", "server", "current", "当前限定", "special", f"f{index}", 20,
                f"free-{index}", "免费六星" if index == 5 else "免费结果", 6 if index == 5 else 4,
                "角色", is_free=True,
            )
            for index in range(10)
        )
        records.extend(
            store_module.GachaRecord(
                "role", "server", "current", "当前限定", "special", f"c{index}", 30 + index,
                f"paid-{index}", "计费结果", 4, "角色",
            )
            for index in range(3)
        )

        rules = {
            "current": gacha_assets_module.GachaPoolRule("current", ("free-5",), 120),
        }
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules)
        pools = {item.pool_id: item for item in result.pools}

        self.assertIn("standard", pools)
        self.assertNotIn(
            "standard",
            {item.pool_id for item in draw_module._recent_gacha_pools(result, "角色")},
        )
        self.assertEqual((result.total, result.paid_total, result.free_pull_count, result.free_ten_count), (17, 7, 10, 1))
        self.assertFalse(pools["old"].is_current)
        self.assertTrue(pools["current"].is_current)
        self.assertEqual(pools["current"].small_pity_progress, 5)
        self.assertEqual(pools["current"].large_pity_progress, 3)
        self.assertTrue(pools["current"].large_pity_known)
        self.assertFalse(pools["current"].large_pity_consumed)
        self.assertEqual(len(pools["current"].free_batches), 1)
        self.assertEqual(len(pools["current"].free_batches[0].six_stars), 1)

    def test_analysis_does_not_reset_special_pity_on_standard_six_star(self):
        records: list[store_module.GachaRecord] = []
        seq = 1

        def add(pool_id: str, pool_name: str, pool_type: str, rarity: int, name: str) -> None:
            nonlocal seq
            records.append(
                store_module.GachaRecord(
                    "role", "server", pool_id, pool_name, pool_type,
                    str(seq), seq, f"{pool_id}-{seq}", name, rarity, "角色",
                )
            )
            seq += 1

        special = ("winter", "冬猎", "E_CharacterGachaPoolType_Special")
        standard = ("standard", "基础寻访", "E_CharacterGachaPoolType_Standard")
        for _ in range(3):
            add(*special, 4, "垫抽")
        add(*special, 6, "骏卫")
        for _ in range(6):
            add(*special, 4, "垫抽")
        add(*standard, 6, "艾尔黛拉")
        add(*standard, 4, "常驻垫")
        add(*standard, 4, "常驻垫")
        add(*special, 4, "垫抽")
        add(*special, 4, "垫抽")
        add(*special, 6, "提弗洛斯")
        for _ in range(3):
            add(*special, 4, "垫抽")

        result = gacha_module.build_gacha_analysis(self.role, records, [])
        winter = {item.pool_id: item for item in result.pools}["winter"]
        typhoea, junwei = winter.six_stars

        self.assertEqual([item.name for item in winter.six_stars], ["提弗洛斯", "骏卫"])
        self.assertEqual((junwei.pool_position, junwei.interval), (4, 4))
        self.assertEqual((typhoea.pool_position, typhoea.interval), (13, 9))
        self.assertEqual((winter.since_six_star, winter.small_pity_progress), (3, 3))
        self.assertEqual(result.intervals, (9,))

    def test_analysis_consumes_first_large_pity_when_paid_pull_gets_current_up(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "current", "当前限定", "special", str(index), index,
                "chr_up" if index == 4 else f"filler-{index}",
                "当期UP" if index == 4 else "垫抽", 6 if index == 4 else 4, "角色",
            )
            for index in range(1, 7)
        ]
        metadata = {
            "chr_up": gacha_assets_module.GachaItemMetadata(
                "chr_up", "当期UP", 6, "角色",
            ),
        }
        rules = {
            "current": gacha_assets_module.GachaPoolRule("current", ("chr_up",), 120),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], metadata, rules,
        ).pools[0]

        self.assertTrue(pool.large_pity_known)
        self.assertTrue(pool.large_pity_consumed)
        self.assertEqual(pool.large_pity_consumed_at, 4)
        self.assertEqual(pool.large_pity_progress, 4)
        self.assertEqual(pool.large_pity_up_name, "当期UP")
        self.assertEqual(pool.six_stars[0].pity_labels, ())

    def test_analysis_marks_character_six_star_that_triggers_guarantees(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "current", "当前限定", "special", str(index), index,
                "chr_up" if index == 120 else f"filler-{index}",
                "当期UP" if index == 120 else "垫抽", 6 if index in {40, 120} else 4, "角色",
            )
            for index in range(1, 121)
        ]
        rules = {
            "current": gacha_assets_module.GachaPoolRule("current", ("chr_up",), 120),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], pool_rules=rules,
        ).pools[0]
        events = {item.pool_position: item for item in pool.six_stars}

        self.assertEqual(events[40].pity_labels, ())
        self.assertEqual(events[120].pity_labels, ("小保底", "大保底"))

    def test_joint_pool_does_not_advance_special_pool_pity(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "joint", "辉光庆典", "E_CharacterGachaPoolType_Joint",
                str(index), index, f"joint-{index}", "庆典结果", 4, "角色",
            )
            for index in range(1, 11)
        ] + [
            store_module.GachaRecord(
                "role", "server", "special", "春雷动，万物生", "E_CharacterGachaPoolType_Special",
                str(10 + index), 10 + index, f"special-{index}",
                "UP六星" if index == 80 else "UP池结果", 6 if index == 80 else 4, "角色",
            )
            for index in range(1, 81)
        ]

        pools = {
            item.pool_id: item
            for item in gacha_module.build_gacha_analysis(self.role, records, []).pools
        }

        self.assertEqual(pools["special"].six_stars[0].interval, 80)
        self.assertEqual(pools["special"].six_stars[0].pity_labels, ("小保底",))
        self.assertEqual(pools["joint"].since_six_star, 10)

    def test_beginner_pool_does_not_advance_special_pool_pity(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "beginner", "新手池", "E_CharacterGachaPoolType_Beginner",
                str(index), index, f"beginner-{index}", "新手结果", 4, "角色",
            )
            for index in range(1, 41)
        ] + [
            store_module.GachaRecord(
                "role", "server", "special", "限定池", "E_CharacterGachaPoolType_Special",
                str(40 + index), 40 + index, f"special-{index}",
                "UP六星" if index == 80 else "限定结果", 6 if index == 80 else 4, "角色",
            )
            for index in range(1, 81)
        ]

        pools = {
            item.pool_id: item
            for item in gacha_module.build_gacha_analysis(self.role, records, []).pools
        }

        self.assertEqual(pools["special"].six_stars[0].interval, 80)
        self.assertEqual(pools["special"].six_stars[0].pity_labels, ("小保底",))
        self.assertEqual(pools["beginner"].since_six_star, 40)

    def test_analysis_uses_pool_total_override_without_faking_records(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "spring", "春雷动，万物生", "special", str(index), index,
                f"item-{index}", "结果", 4, "角色",
            )
            for index in range(1, 91)
        ]
        result = gacha_module.build_gacha_analysis(
            self.role, records, [], pool_total_overrides={"spring": 120},
        )
        pool = result.pools[0]

        self.assertEqual((pool.total, pool.recorded_total, pool.history_missing_count), (120, 90, 30))
        self.assertEqual((result.total, result.recorded_total, result.history_missing_count), (120, 90, 30))
        self.assertEqual(result.paid_total, 90)
        self.assertFalse(result.complete)

    def test_analysis_tracks_weapon_pity_by_ten_pull_batches(self):
        records = []
        seq = 0
        for batch in range(1, 7):
            for position in range(1, 11):
                seq += 1
                rarity = 6 if seq == 25 else 4
                records.append(
                    store_module.GachaRecord(
                        "role", "server", "weapon", "武器限定", "weapon", str(seq), batch,
                        f"weapon-{seq}", "六星武器" if rarity == 6 else "武器", rarity, "武器",
                    )
                )

        pool = gacha_module.build_gacha_analysis(self.role, records, []).pools[0]

        self.assertTrue(pool.is_current)
        self.assertEqual(pool.small_pity_progress, 3)
        self.assertEqual(pool.small_pity_limit, 4)
        self.assertEqual(pool.large_pity_progress, 60)
        self.assertEqual(pool.large_pity_limit, 80)

    def test_analysis_marks_weapon_guarantee_products(self):
        records = []
        for position in range(1, 81):
            batch = (position - 1) // 10 + 1
            rarity = 6 if position in {40, 80} else 4
            records.append(
                store_module.GachaRecord(
                    "role", "server", "weapon", "武器限定", "weapon", str(position), batch,
                    "weapon-up" if position == 80 else f"weapon-{position}",
                    "当期UP武器" if position == 80 else "武器", rarity, "武器",
                )
            )
        rules = {
            "weapon": gacha_assets_module.GachaPoolRule("weapon", ("weapon-up",), 80),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], pool_rules=rules,
        ).pools[0]
        events = {item.pool_position: item for item in pool.six_stars}

        self.assertEqual(events[40].pity_labels, ("小保底",))
        self.assertEqual(
            events[80].pity_labels,
            ("小保底", "大保底"),
        )

    def test_analysis_adds_keepsake_gift_marker_at_240_pulls(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "current", "当前限定", "special", str(index), index,
                f"item-{index}", f"六星{index}" if index in {200, 300} else "结果",
                6 if index in {200, 300} else 4, "角色",
            )
            for index in range(1, 301)
        ]
        rules = {"current": gacha_assets_module.GachaPoolRule("current", ("up",), 120)}
        metadata = {"up": gacha_assets_module.GachaItemMetadata("up", "当期UP", 6, "角色")}
        keepsake_metadata = {
            "up": gacha_assets_module.GachaItemMetadata(
                "item_charpotentialup_up", "当期UP的信物", 6, "信物",
                icon_path="C:/cache/up-keepsake.png",
            ),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], metadata, rules,
            keepsake_metadata=keepsake_metadata,
        ).pools[0]

        self.assertEqual(pool.keepsake_claims, 1)
        self.assertEqual(pool.keepsake_gifts[0].name, "当期UP的信物")
        self.assertEqual(pool.keepsake_gifts[0].item_id, "item_charpotentialup_up")
        self.assertEqual(pool.keepsake_gifts[0].icon_path, "C:/cache/up-keepsake.png")
        self.assertEqual(pool.keepsake_gifts[0].pool_position, 240)
        gift_html = draw_module._draw_gacha_pool(pool)
        self.assertIn("第240抽", gift_html)
        self.assertIn("赠送", gift_html)
        self.assertLess(gift_html.index("六星300"), gift_html.index("当期UP的信物"))
        self.assertLess(gift_html.index("当期UP的信物"), gift_html.index("六星200"))

    def test_analysis_adds_weapon_gift_marker_at_18th_claim(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "weapon", "武器池", "weapon", str(index), (index - 1) // 10 + 1,
                f"item-{index}", "结果", 4, "武器",
            )
            for index in range(1, 181)
        ]
        rules = {"weapon": gacha_assets_module.GachaPoolRule("weapon", ("wpn-up",), 80)}
        metadata = {
            "wpn-up": gacha_assets_module.GachaItemMetadata(
                "wpn-up", "赠送武器", 6, "武器", icon_path="C:/cache/gift-weapon.png",
            ),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], metadata, rules,
        ).pools[0]

        self.assertEqual(len(pool.keepsake_gifts), 1)
        self.assertEqual(pool.keepsake_gifts[0].gift_type, "武器")
        self.assertEqual(pool.keepsake_gifts[0].name, "赠送武器")
        gift_html = draw_module._draw_gacha_pool(pool)
        self.assertIn("第18次申领赠送武器", gift_html)
        self.assertIn("赠送武器", gift_html)

    def test_xhh_weapon_gifts_use_ten_pull_claim_positions_and_real_up_name(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="tester", total_count=280, imported_at=456,
            pools=(store_module.XhhGachaPool(
                "weapon", "武器池", "weapon", "武器", 280,
            ),),
            six_stars=(store_module.XhhSixStar(
                "weapon", "up", "UP Weapon", "武器", 100, 20, 20,
            ),),
        )
        metadata = {
            "upweapon": gacha_assets_module.GachaItemMetadata(
                "actual-up", "UP Weapon", 6, "武器",
            ),
        }
        rules = {
            "weapon": gacha_assets_module.GachaPoolRule(
                "weapon", ("rule-up",), 80, pool_kind="weapon",
            ),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, [], [], pool_rules=rules,
            xhh_import=imported, xhh_metadata=metadata,
        ).pools[0]

        self.assertEqual(
            [(gift.name, gift.item_id, gift.pool_position, gift.claim_count)
             for gift in pool.keepsake_gifts],
            [("UP Weapon", "actual-up", 180, 18)],
        )

        short_import = replace(
            imported,
            total_count=80,
            pools=(replace(imported.pools[0], total_count=80),),
        )
        short_pool = gacha_module.build_gacha_analysis(
            self.role, [], [], pool_rules=rules,
            xhh_import=short_import, xhh_metadata=metadata,
        ).pools[0]
        self.assertEqual(short_pool.keepsake_gifts, ())

    def test_analysis_orders_pools_by_latest_record(self):
        records = [
            store_module.GachaRecord("role", "server", "older", "旧池", "x", "1", 10, "a", "甲", 6, "角色"),
            store_module.GachaRecord("role", "server", "newer", "新池", "x", "2", 20, "b", "乙", 4, "角色"),
            store_module.GachaRecord("role", "server", "weapon", "武器池", "x", "3", 30, "c", "丙", 6, "武器"),
        ]
        result = gacha_module.build_gacha_analysis(self.role, records, [])
        self.assertEqual([item.pool_id for item in result.pools], ["weapon", "newer", "older"])
        self.assertEqual(result.pools[1].since_six_star, 1)
        self.assertEqual(result.pools[0].six_stars[0].interval, 1)

    def test_analysis_uses_seq_id_order_for_same_timestamp(self):
        records = [
            store_module.GachaRecord("role", "server", "pool", "同秒池", "x", "4", 10, "d", "丁", 4, "角色"),
            store_module.GachaRecord("role", "server", "pool", "同秒池", "x", "2", 10, "b", "乙", 6, "角色"),
            store_module.GachaRecord("role", "server", "pool", "同秒池", "x", "1", 10, "a", "甲", 4, "角色"),
            store_module.GachaRecord("role", "server", "pool", "同秒池", "x", "3", 10, "c", "丙", 5, "角色"),
        ]
        pool = gacha_module.build_gacha_analysis(self.role, records, []).pools[0]
        self.assertEqual(pool.six_stars[0].interval, 2)
        self.assertEqual(pool.since_six_star, 2)

    def test_analysis_uses_xhh_snapshot_for_full_pool_stats_and_images(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=150, imported_at=456,
            pools=(
                store_module.XhhGachaPool(
                    "spring", "春雷动万物生", "special", "角色", 120, 20, 10, 200, True
                ),
                store_module.XhhGachaPool(
                    "standard", "基础寻访", "standard", "角色", 30, 0, 0, 100, False
                ),
            ),
            six_stars=(
                store_module.XhhSixStar(
                    "spring", "six-1", "42式", "角色", 180, 80, 80
                ),
            ),
        )
        item = gacha_assets_module.GachaItemMetadata(
            "char_42", "42式", 6, "角色", icon_path="C:/cache/42.png"
        )
        result = gacha_module.build_gacha_analysis(
            self.role, [], [],
            pool_rules={"spring": gacha_assets_module.GachaPoolRule("spring", ("char_42",), 120)},
            xhh_import=imported,
            xhh_metadata={"42式": item},
        )

        self.assertEqual([pool.pool_id for pool in result.pools], ["spring", "standard"])
        self.assertEqual(
            [pool.pool_id for pool in draw_module._recent_gacha_pools(result, "角色")],
            ["spring"],
        )
        pool = result.pools[0]
        self.assertEqual((pool.total, pool.paid_total, pool.free_pull_count), (130, 120, 10))
        self.assertEqual((pool.since_six_star, pool.small_pity_progress), (20, 20))
        self.assertEqual(len(pool.free_batches), 1)
        self.assertEqual(pool.six_stars[0].icon_path, "C:/cache/42.png")
        self.assertEqual(pool.six_stars[0].pity_labels, ("小保底",))
        self.assertTrue(pool.large_pity_consumed)
        self.assertEqual(pool.large_pity_consumed_at, 80)
        self.assertEqual((result.total, result.recorded_total, result.history_missing_count), (160, 0, 160))
        self.assertTrue(result.complete)
        self.assertEqual(result.xhh_imported_at, 456)

    def test_analysis_uses_xhh_pool_order_instead_of_name_or_timestamp(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=3, imported_at=456,
            pools=(
                store_module.XhhGachaPool("new", "乙池", "special", "角色", 1, sort_order=0),
                store_module.XhhGachaPool("middle", "丙池", "special", "角色", 1, sort_order=1),
                store_module.XhhGachaPool("old", "甲池", "special", "角色", 1, sort_order=2),
            ),
            six_stars=(),
        )

        result = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported,
        )

        self.assertEqual([item.pool_id for item in result.pools], ["new", "middle", "old"])
        self.assertEqual(
            [item.pool_id for item in draw_module._recent_gacha_pools(result, "角色")],
            ["new", "middle", "old"],
        )

    def test_analysis_orders_mixed_sources_by_time_and_recomputes_current_pool(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "official-new", "新官方池", "special", "1",
                300, "item", "结果", 4, "角色",
            ),
            store_module.GachaRecord(
                "role", "server", "official-middle", "中间官方池", "special", "2",
                200, "item", "结果", 4, "角色",
            ),
        ]
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=2, imported_at=456,
            pools=(
                store_module.XhhGachaPool(
                    "xhh-new", "旧小黑盒池", "special", "角色", 1,
                    latest_ts=100, is_current=True, sort_order=0,
                ),
                store_module.XhhGachaPool(
                    "xhh-old", "更旧小黑盒池", "special", "角色", 1,
                    latest_ts=50, sort_order=1,
                ),
            ),
            six_stars=(),
        )

        result = gacha_module.build_gacha_analysis(
            self.role, records, [], xhh_import=imported,
        )
        pools = draw_module._recent_gacha_pools(result, "角色")

        self.assertEqual(
            [item.pool_id for item in pools],
            ["official-new", "official-middle", "xhh-new", "xhh-old"],
        )
        self.assertEqual([item.pool_id for item in pools if item.is_current], ["official-new"])

    def test_xhh_character_intervals_inherit_pity_across_special_pools(self):
        pools = (
            store_module.XhhGachaPool(
                "current", "临渊望北", "special", "角色", 60,
                is_current=True, sort_order=0,
            ),
            store_module.XhhGachaPool(
                "camille", "逐罪者", "special", "角色", 60, sort_order=1,
            ),
            store_module.XhhGachaPool(
                "fist", "拳出无悔", "special", "角色", 60, sort_order=2,
            ),
            store_module.XhhGachaPool(
                "spring", "春雷动，万物生", "special", "角色", 63, sort_order=3,
            ),
            store_module.XhhGachaPool(
                "wolf", "狼珀", "special", "角色", 60, sort_order=4,
            ),
            store_module.XhhGachaPool(
                "river", "河流的女儿", "special", "角色", 78, sort_order=5,
            ),
            store_module.XhhGachaPool(
                "joint", "辉光庆典", "joint", "角色", 145, sort_order=6,
            ),
        )
        six_stars = (
            store_module.XhhSixStar("current", "current-six", "当前六星", "角色", 60, 10, 10),
            store_module.XhhSixStar("camille", "camille-six", "逐罪六星", "角色", 50, 54, 54),
            store_module.XhhSixStar("fist", "fist-one", "拳池一", "角色", 40, 17, 17),
            store_module.XhhSixStar("fist", "fist-two", "拳池二", "角色", 41, 3, 20),
            store_module.XhhSixStar("fist", "fist-three", "拳池三", "角色", 42, 25, 45),
            store_module.XhhSixStar("spring", "spring-six", "庄方宜", "角色", 30, 17, 17),
            store_module.XhhSixStar("wolf", "wolf-six", "洛茜", "角色", 20, 6, 6),
            store_module.XhhSixStar("river", "river-six", "汤汤", "角色", 10, 65, 65),
            store_module.XhhSixStar("joint", "joint-six", "庆典六星", "角色", 25, 74, 74),
        )
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=508, imported_at=100,
            pools=pools, six_stars=six_stars,
        )
        metadata = {
            item.item_name: gacha_assets_module.GachaItemMetadata(
                item.unique_key, item.item_name, 6, "角色",
            )
            for item in six_stars
        }

        result = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported, xhh_metadata=metadata,
        )
        analyzed = {
            event.name: event.interval
            for pool in result.pools
            for event in pool.six_stars
        }
        current = next(pool for pool in result.pools if pool.pool_id == "current")

        self.assertEqual(analyzed["汤汤"], 65)
        self.assertEqual(analyzed["洛茜"], 19)
        self.assertEqual(analyzed["庄方宜"], 71)
        self.assertEqual(analyzed["拳池一"], 63)
        self.assertEqual(analyzed["拳池二"], 3)
        self.assertEqual(analyzed["拳池三"], 25)
        self.assertEqual(analyzed["逐罪六星"], 69)
        self.assertEqual(analyzed["当前六星"], 16)
        self.assertEqual(analyzed["庆典六星"], 74)
        self.assertEqual(current.since_six_star, 50)

    def test_xhh_metadata_filters_five_stars_and_rebuilds_six_star_positions(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=100, imported_at=456,
            pools=(
                store_module.XhhGachaPool(
                    "pool", "角色池", "special", "角色", 100, current_count=10,
                ),
            ),
            six_stars=(
                store_module.XhhSixStar("pool", "new-six", "六星乙", "角色", 30, 40, 95),
                store_module.XhhSixStar("pool", "five", "五星甲", "角色", 20, 1, 55),
                store_module.XhhSixStar("pool", "old-six", "六星甲", "角色", 10, 50, 50),
            ),
        )
        metadata = {
            "六星甲": gacha_assets_module.GachaItemMetadata("six-a", "六星甲", 6, "角色"),
            "六星乙": gacha_assets_module.GachaItemMetadata("six-b", "六星乙", 6, "角色"),
            "五星甲": gacha_assets_module.GachaItemMetadata("five", "五星甲", 5, "角色"),
        }

        filtered = gacha_module.filter_xhh_import_six_stars(imported, metadata)
        result = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported, xhh_metadata=metadata,
        )

        self.assertEqual(
            [(item.item_name, item.pool_position) for item in filtered.six_stars],
            [("六星乙", 90), ("六星甲", 50)],
        )
        self.assertEqual([item.name for item in result.pools[0].six_stars], ["六星乙", "六星甲"])
        self.assertEqual(result.rarity_counts[6], 2)

    def test_analysis_prefers_official_six_star_when_xhh_event_matches(self):
        record = store_module.GachaRecord(
            "role", "server", "spring", "春雷动万物生", "special", "1", 180,
            "char_42", "42式", 6, "角色",
        )
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=1, imported_at=456,
            pools=(store_module.XhhGachaPool("spring", "春雷动万物生", "special", "角色", 1, 0, 0, 180, True),),
            six_stars=(store_module.XhhSixStar("spring", "six-1", "42式", "角色", 180, 1, 1),),
        )
        metadata = {
            "char_42": gacha_assets_module.GachaItemMetadata(
                "char_42", "42式", 6, "角色", icon_path="C:/cache/official.png"
            )
        }
        result = gacha_module.build_gacha_analysis(
            self.role, [record], [], metadata, xhh_import=imported,
            xhh_metadata={"42式": metadata["char_42"]},
        )

        self.assertEqual(len(result.pools[0].six_stars), 1)
        self.assertEqual(result.pools[0].six_stars[0].icon_path, "C:/cache/official.png")

    def test_xhh_miss_up_marks_standalone_six_star(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=20, imported_at=456,
            pools=(
                store_module.XhhGachaPool("special", "限定池", "special", "角色", 20),
            ),
            six_stars=(
                store_module.XhhSixStar(
                    "special", "miss-up", "歪出的角色", "角色", 100, 20, 20,
                    miss_up=True,
                ),
            ),
        )
        metadata = {
            "歪出的角色": gacha_assets_module.GachaItemMetadata(
                "miss-up", "歪出的角色", 6, "角色",
            ),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported, xhh_metadata=metadata,
        ).pools[0]

        self.assertEqual(pool.six_stars[0].pity_labels, ("歪",))

    def test_xhh_miss_up_label_survives_official_deduplication(self):
        official = gacha_module.SixStarEvent(
            "歪出的角色", "限定池", "角色", 100, interval=80, pool_position=120,
            pity_labels=("小保底", "大保底"),
        )
        imported = gacha_module.SixStarEvent(
            "歪出的角色", "限定池", "角色", 100, interval=20, pool_position=20,
            pity_labels=("歪",),
        )

        merged = gacha_module._merge_xhh_six_star_events(
            (official,), (), [imported],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].pity_labels, ("小保底", "大保底", "歪"))
        self.assertEqual((merged[0].interval, merged[0].pool_position), (80, 120))

    def test_xhh_cross_pool_interval_repairs_partial_official_window(self):
        official = gacha_module.SixStarEvent(
            "弭弗", "拳出无悔", "角色", 100, interval=17, pool_position=17,
        )
        imported = replace(official, interval=63)

        merged = gacha_module._merge_xhh_six_star_events(
            (official,), (), [imported],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0].interval, merged[0].pool_position), (63, 17))

    def test_xhh_miss_up_label_survives_free_pull_deduplication(self):
        official = gacha_module.SixStarEvent(
            "歪出的角色", "限定池", "角色", 100,
        )
        imported = replace(official, pity_labels=("歪",))

        batches = gacha_module._merge_xhh_free_batches(
            (gacha_module.FreePullBatch(100, 10, (official,)),),
            10,
            100,
            [imported],
        )

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].six_stars[0].pity_labels, ("歪",))

    def test_analysis_deduplicates_xhh_paid_and_free_six_stars_one_to_one(self):
        records = [
            store_module.GachaRecord(
                "role", "server", "spring", "春雷动，万物生", "special", "1", 100,
                "six-a", "艾尔黛拉", 6, "角色",
            ),
            *[
                store_module.GachaRecord(
                    "role", "server", "camille", "逐罪者", "special", str(index), 200,
                    "camille", "卡缪" if index in {8, 10} else "免费结果",
                    6 if index in {8, 10} else 4, "角色", is_free=True,
                )
                for index in range(1, 11)
            ],
        ]
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=150, imported_at=456,
            pools=(
                store_module.XhhGachaPool("spring", "春雷动，万物生", "special", "角色", 120),
                store_module.XhhGachaPool("camille", "逐罪者", "special", "角色", 30),
            ),
            six_stars=(
                store_module.XhhSixStar("spring", "xhh-a", "艾尔黛拉", "角色", 1_000, 69, 69),
                store_module.XhhSixStar("camille", "xhh-c1", "卡缪", "角色", 2_000, 8, 8),
                store_module.XhhSixStar("camille", "xhh-c2", "卡缪", "角色", 2_000, 2, 10),
            ),
        )
        metadata = {
            "艾尔黛拉": gacha_assets_module.GachaItemMetadata("six-a", "艾尔黛拉", 6, "角色"),
            "卡缪": gacha_assets_module.GachaItemMetadata("camille", "卡缪", 6, "角色"),
        }

        result = gacha_module.build_gacha_analysis(
            self.role, records, [], metadata, xhh_import=imported, xhh_metadata=metadata,
        )
        pools = {item.pool_id: item for item in result.pools}

        self.assertEqual([item.name for item in pools["spring"].six_stars], ["艾尔黛拉"])
        self.assertEqual(pools["camille"].six_stars, ())
        self.assertEqual(len(pools["camille"].free_batches), 1)
        self.assertEqual(len(pools["camille"].free_batches[0].six_stars), 2)
        self.assertEqual(
            (pools["camille"].total, pools["camille"].paid_total, pools["camille"].free_pull_count),
            (40, 30, 10),
        )

    def test_analysis_prefers_xhh_pull_values_near_official_window_boundary(self):
        pool_day = 35 * 86_400
        imported_at = 100 * 86_400
        records = [
            store_module.GachaRecord(
                "role", "server", "spring", "春雷动，万物生", "special", str(index),
                pool_day + index, "sheep" if index == 39 else ("up" if index == 90 else f"item-{index}"),
                "艾尔黛拉" if index == 39 else ("庄方宜" if index == 90 else "结果"),
                6 if index in {39, 90} else 4, "角色",
            )
            for index in range(1, 91)
        ]
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=120, imported_at=imported_at,
            pools=(
                store_module.XhhGachaPool(
                    "spring", "春雷动，万物生", "special", "角色", 120,
                    latest_ts=pool_day,
                ),
            ),
            six_stars=(
                store_module.XhhSixStar("spring", "sheep", "艾尔黛拉", "角色", pool_day, 69, 69),
                store_module.XhhSixStar("spring", "up", "庄方宜", "角色", pool_day, 51, 120),
            ),
        )
        metadata = {
            "艾尔黛拉": gacha_assets_module.GachaItemMetadata("sheep", "艾尔黛拉", 6, "角色"),
            "庄方宜": gacha_assets_module.GachaItemMetadata("up", "庄方宜", 6, "角色"),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, records, [], metadata, xhh_import=imported, xhh_metadata=metadata,
        ).pools[0]
        events = {item.name: item for item in pool.six_stars}

        self.assertEqual((events["艾尔黛拉"].interval, events["艾尔黛拉"].pool_position), (69, 69))
        self.assertEqual((events["庄方宜"].interval, events["庄方宜"].pool_position), (51, 120))
        self.assertEqual(events["庄方宜"].pity_labels, ("大保底",))

    def test_analysis_places_xhh_free_six_star_in_free_ten_batch(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=35, imported_at=456,
            pools=(
                store_module.XhhGachaPool("special", "熔火灼痕", "special", "角色", 35),
            ),
            six_stars=(
                store_module.XhhSixStar(
                    "special", "free-up", "莱万汀", "角色", 100, 1, 0, is_free=True,
                ),
            ),
        )
        metadata = {
            "莱万汀": gacha_assets_module.GachaItemMetadata("levatain", "莱万汀", 6, "角色"),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported, xhh_metadata=metadata,
        ).pools[0]

        self.assertEqual(pool.six_stars, ())
        self.assertEqual((pool.total, pool.paid_total, pool.free_pull_count), (45, 35, 10))
        self.assertEqual([item.name for item in pool.free_batches[0].six_stars], ["莱万汀"])

    def test_xhh_special_free_ten_requires_thirty_paid_pulls(self):
        def snapshot(total_count: int, *, free_count: int = 0):
            return store_module.XhhGachaPool(
                "special", "熔火灼痕", "special", "角色", total_count,
                free_count=free_count,
            )

        self.assertEqual(gacha_module._xhh_expected_free_pull_count(snapshot(10)), 0)
        self.assertEqual(gacha_module._xhh_expected_free_pull_count(snapshot(29)), 0)
        self.assertEqual(gacha_module._xhh_expected_free_pull_count(snapshot(30)), 10)
        self.assertEqual(
            gacha_module._xhh_expected_free_pull_count(snapshot(10, free_count=10)),
            10,
        )

        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=10, imported_at=456,
            pools=(snapshot(10),), six_stars=(),
        )
        pool = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported,
        ).pools[0]

        self.assertEqual((pool.total, pool.paid_total, pool.free_pull_count), (10, 10, 0))
        self.assertEqual(pool.free_batches, ())

    def test_beginner_pool_marks_fortieth_pull_as_large_guarantee(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=40, imported_at=456,
            pools=(
                store_module.XhhGachaPool(
                    "beginner", "新手池", "E_CharacterGachaPoolType_Beginner", "角色", 40,
                ),
            ),
            six_stars=(
                store_module.XhhSixStar("beginner", "six", "艾尔黛拉", "角色", 100, 40, 40),
            ),
        )
        metadata = {
            "艾尔黛拉": gacha_assets_module.GachaItemMetadata("ardelia", "艾尔黛拉", 6, "角色"),
        }

        pool = gacha_module.build_gacha_analysis(
            self.role, [], [], xhh_import=imported, xhh_metadata=metadata,
        ).pools[0]

        self.assertEqual(pool.six_stars[0].pity_labels, ("大保底",))


class EndfieldNeutralCardTests(unittest.IsolatedAsyncioTestCase):
    def test_gacha_pool_header_renders_left_character_banner_without_label(self):
        pool = gacha_module.PoolAnalysis(
            "special", "限定池", "角色", 10, 0,
            banners=(gacha_assets_module.GachaPoolBanner(
                "chr_up", "UP干员", "角色", "C:/cache/up.webp",
            ),),
        )

        with mock.patch.object(draw_module, "_local_image_data_url", return_value="data:image/webp;base64,AAA"):
            body = draw_module._draw_gacha_pool(pool)

        self.assertIn('class="pool-head has-banner character-banner-head"', body)
        self.assertIn('class="pool-banner character-pool-banner"', body)
        self.assertIn('class="character-banner"', body)
        self.assertNotIn("UP ·", body)
        self.assertNotIn('class="pool-up-name"', body)
        self.assertIn("data:image/webp;base64,AAA", body)

    def test_gacha_pool_header_renders_left_weapon_banner(self):
        pool = gacha_module.PoolAnalysis(
            "weapon", "武器池", "武器", 20, 0,
            banners=(gacha_assets_module.GachaPoolBanner(
                "wpn_up", "UP武器", "武器", "C:/cache/up.png",
            ),),
        )

        with mock.patch.object(draw_module, "_local_image_data_url", return_value="data:image/png;base64,AAA"):
            body = draw_module._draw_gacha_pool(pool)

        self.assertIn('class="pool-head has-banner weapon-banner-head"', body)
        self.assertIn('class="pool-banner weapon-pool-banner"', body)
        self.assertIn('class="weapon-banner"', body)
        self.assertNotIn('class="pool-up-name"', body)

    def test_gacha_summary_splits_up_and_six_star_expectations(self):
        character = gacha_module.PoolAnalysis(
            "special", "限定池", "角色", 100, 0, paid_total=100,
            six_stars=(gacha_module.SixStarEvent(
                "六星", "限定池", "角色", 1, item_id="up-role",
            ),),
            up_item_ids=("up-role",),
        )
        weapon = gacha_module.PoolAnalysis(
            "weapon", "武器池", "武器", 40, 0, paid_total=40,
            six_stars=(gacha_module.SixStarEvent(
                "六星武器", "武器池", "武器", 1, item_id="up-weapon",
            ),),
            up_item_ids=("up-weapon",),
        )

        character_html = draw_module._draw_gacha_expectation_summary(
            gacha_module.calculate_six_star_expectation([character], "角色"), "角色",
        )
        weapon_html = draw_module._draw_gacha_expectation_summary(
            gacha_module.calculate_six_star_expectation([weapon], "武器"), "武器",
        )

        self.assertIn("<strong>UP角色</strong><small>含120抽保底</small>", character_html)
        self.assertIn("综合期望 <b>81.4</b> 抽", character_html)
        self.assertIn("<strong>6星角色</strong><small>首个UP前 → 后</small>", character_html)
        self.assertIn("综合期望 <b>49.1 → 44.0</b> 抽", character_html)
        self.assertIn("<strong>UP武器</strong><small>首个UP前 → 后</small>", weapon_html)
        self.assertIn("综合期望 <b>54.0 → 53.5</b> 抽", weapon_html)
        self.assertIn("账号当前 <b>40.0</b> 抽", weapon_html)

    def test_gacha_summary_blends_free_pulls_at_base_rates(self):
        character = gacha_module.PoolAnalysis(
            "special", "限定池", "角色", 110, 0, paid_total=100,
            six_stars=(gacha_module.SixStarEvent(
                "UP角色", "限定池", "角色", 1, item_id="up-role",
            ),),
            free_pull_count=10,
            free_batches=(gacha_module.FreePullBatch(
                2, 10, (gacha_module.SixStarEvent(
                    "免费UP角色", "限定池", "角色", 2, item_id="up-role",
                ),),
            ),),
            up_item_ids=("up-role",),
        )

        html = draw_module._draw_gacha_expectation_summary(
            gacha_module.calculate_six_star_expectation([character], "角色"), "角色",
        )

        self.assertIn("综合期望 <b>86.7</b> 抽", html)
        self.assertIn("综合期望 <b>51.9 → 46.8</b> 抽", html)
        self.assertIn("账号当前 <b>55.0</b> 抽", html)

    async def test_gacha_analysis_moves_expectations_to_three_card_summary(self):
        pools = (
            gacha_module.PoolAnalysis(
                "special", "限定池", "角色", 100, 0, paid_total=90,
                six_stars=(gacha_module.SixStarEvent(
                    "UP角色", "限定池", "角色", 1, item_id="up-role",
                ),),
                up_item_ids=("up-role",),
            ),
            gacha_module.PoolAnalysis(
                "weapon", "武器池", "武器", 40, 0, paid_total=40,
                six_stars=(gacha_module.SixStarEvent(
                    "UP武器", "武器池", "武器", 1, item_id="up-weapon",
                ),),
                up_item_ids=("up-weapon",),
            ),
        )
        view = types.SimpleNamespace(
            role=types.SimpleNamespace(nickname="甲", server_name="一区"),
            total=140, pools=pools, free_pull_count=10, paid_total=130,
            xhh_imported_at=1, recorded_total=120, history_missing_count=20,
            rarity_counts={6: 2}, complete=True, errors=(), last_sync_at=1,
        )
        renderer = mock.AsyncMock(return_value=b"png")

        with mock.patch.object(draw_module, "_draw_neutral_card", renderer):
            await draw_module.draw_gacha_analysis_card(view, uid="****1234")

        body = renderer.await_args.args[1]
        css = renderer.await_args.kwargs["extra_css"]
        self.assertIn("六星记录 2", body)
        self.assertIn("角色寻访", body)
        self.assertIn("武器申领", body)
        self.assertEqual(body.count('class="metric"'), 2)
        self.assertNotIn("<span>六星记录</span>", body)
        self.assertNotIn("expectation-strip", body + css)

    def test_gacha_pagination_splits_large_pool_without_losing_rows(self):
        events = tuple(
            gacha_module.SixStarEvent(
                f"六星 {index}", "超长角色池", "角色", index,
                interval=10, pool_position=100 - index,
            )
            for index in range(90)
        )
        gifts = tuple(
            gacha_module.KeepsakeGift(f"信物 {index}", f"gift-{index}", index, 240 * (index + 1))
            for index in range(2)
        )
        free_batches = tuple(
            gacha_module.FreePullBatch(index, 10)
            for index in range(3)
        )
        pool = gacha_module.PoolAnalysis(
            "large", "超长角色池", "角色", 1000, 5,
            is_current=True, six_stars=events, keepsake_gifts=gifts,
            free_batches=free_batches,
        )

        pieces = draw_module._split_gacha_pool(pool, 35)

        self.assertGreater(len(pieces), 1)
        self.assertTrue(pieces[0].is_current)
        self.assertTrue(all(not item.is_current for item in pieces[1:]))
        self.assertTrue(all(draw_module._gacha_pool_page_weight(item) <= 35 for item in pieces))
        self.assertEqual(sum(len(item.six_stars) for item in pieces), len(events))
        self.assertEqual(sum(len(item.keepsake_gifts) for item in pieces), len(gifts))
        self.assertEqual(sum(len(item.free_batches) for item in pieces), len(free_batches))
        self.assertIn("（续 2）", pieces[1].name)

    async def test_gacha_cards_fall_back_to_multiple_pages_only_after_height_overflow(self):
        events = tuple(
            gacha_module.SixStarEvent(f"六星 {index}", "超长武器池", "武器", index)
            for index in range(90)
        )
        view = types.SimpleNamespace(
            pools=(gacha_module.PoolAnalysis(
                "large", "超长武器池", "武器", 900, 0, six_stars=events,
            ),),
        )
        renderer = mock.AsyncMock(side_effect=[
            RuntimeError("Screenshot element height 7000px exceeds limit 6144px"),
            b"page-1",
            b"page-2",
        ])

        with mock.patch.object(draw_module, "draw_gacha_analysis_card", renderer):
            pages = await draw_module.draw_gacha_analysis_cards(view, uid="****1234")

        self.assertEqual(pages, (b"page-1", b"page-2"))
        self.assertEqual(renderer.await_count, 3)
        self.assertEqual(renderer.await_args_list[1].kwargs["page_number"], 1)
        self.assertEqual(renderer.await_args_list[1].kwargs["page_count"], 2)
        self.assertTrue(renderer.await_args_list[1].kwargs["show_summary"])
        self.assertEqual(renderer.await_args_list[2].kwargs["page_number"], 2)
        self.assertFalse(renderer.await_args_list[2].kwargs["show_summary"])

    def test_analysis_keeps_every_pool_in_each_column(self):
        pools = tuple(
            gacha_module.PoolAnalysis(f"role-{index}", f"角色池 {index}", "角色", 10, 0, index)
            for index in range(10)
        ) + tuple(
            gacha_module.PoolAnalysis(f"weapon-{index}", f"武器池 {index}", "武器", 10, 0, index)
            for index in range(9)
        )
        view = types.SimpleNamespace(pools=pools)
        role_pools = draw_module._recent_gacha_pools(view, "角色")
        weapon_pools = draw_module._recent_gacha_pools(view, "武器")
        self.assertEqual([item.pool_id for item in role_pools], [f"role-{index}" for index in range(9, -1, -1)])
        self.assertEqual([item.pool_id for item in weapon_pools], [f"weapon-{index}" for index in range(8, -1, -1)])

    async def test_gacha_analysis_uses_compact_layout_for_dense_column(self):
        events = tuple(
            gacha_module.SixStarEvent(f"六星 {index}", "池", "武器", index, interval=10)
            for index in range(81)
        )
        pools = [
            gacha_module.PoolAnalysis(
                "weapon", "武器池", "武器", 810, 0, six_stars=events,
            )
        ]

        view = types.SimpleNamespace(
            role=types.SimpleNamespace(nickname="甲", server_name="一区"),
            total=810, pools=tuple(pools), free_pull_count=0, paid_total=810,
            xhh_imported_at=1, recorded_total=0, history_missing_count=810,
            rarity_counts={6: 81}, complete=True, errors=(), last_sync_at=1,
        )
        renderer = mock.AsyncMock(return_value=b"png")

        with mock.patch.object(draw_module, "_draw_neutral_card", renderer):
            await draw_module.draw_gacha_analysis_card(view, uid="****1234")

        self.assertEqual(draw_module._gacha_column_render_rows(pools), 81)
        self.assertIn("grid-template-columns:38px", renderer.await_args.kwargs["extra_css"])

    def test_gacha_pool_html_only_shows_current_progress_for_current_pool(self):
        free_batch = gacha_module.FreePullBatch(gacha_ts=1_753_156_800, pull_count=10)
        historical = gacha_module.PoolAnalysis(
            "historical", "历史角色池", "角色", 10, 10,
            latest_ts=1_753_156_800, paid_total=0, free_pull_count=10,
            free_batches=(free_batch,),
        )
        current = gacha_module.PoolAnalysis(
            "current", "当前角色池", "角色", 25, 5,
            latest_ts=1_753_243_200, is_current=True, paid_total=25,
            small_pity_progress=5, small_pity_limit=80,
            large_pity_progress=25, large_pity_limit=120, large_pity_known=True,
            keepsake_progress=25,
        )
        consumed = gacha_module.PoolAnalysis(
            "consumed", "已出UP角色池", "角色", 25, 5,
            latest_ts=1_753_243_200, is_current=True, paid_total=25,
            small_pity_progress=5, small_pity_limit=80,
            large_pity_progress=17, large_pity_limit=120, large_pity_known=True,
            large_pity_consumed=True, large_pity_consumed_at=17,
            large_pity_up_name="当期UP", keepsake_progress=25,
        )

        historical_html = draw_module._draw_gacha_pool(historical)
        current_html = draw_module._draw_gacha_pool(current)
        consumed_html = draw_module._draw_gacha_pool(consumed)

        self.assertNotIn("当前累计", historical_html)
        self.assertIn("免费十连 · 未出六星", historical_html)
        self.assertIn("不受且不影响任何保底", historical_html)
        self.assertIn("付费 0 · 垫抽 10 · 免费 10", historical_html)
        self.assertIn("当前累计", current_html)
        self.assertIn("付费 25 · 垫抽 5 · 免费 0", current_html)
        self.assertIn("距小保底", current_html)
        self.assertIn("75 抽", current_html)
        self.assertIn("距大保底", current_html)
        self.assertIn("95 抽", current_html)
        self.assertIn("距下次信物", current_html)
        self.assertIn("215 抽", current_html)
        self.assertIn("进度 25/240", current_html)
        self.assertIn("已消耗", consumed_html)
        self.assertIn("第17抽获得 当期UP · 本期无下次", consumed_html)

    def test_gacha_bar_scale_is_eighty_for_characters_and_forty_for_weapons(self):
        character = gacha_module.PoolAnalysis(
            "character", "角色池", "角色", 40, 0,
            six_stars=(gacha_module.SixStarEvent("角色", "角色池", "角色", 1, interval=40),),
        )
        weapon = gacha_module.PoolAnalysis(
            "weapon", "武器池", "武器", 40, 0,
            is_current=True, small_pity_limit=4,
            large_pity_progress=80, large_pity_limit=80,
            six_stars=(gacha_module.SixStarEvent(
                "武器", "武器池", "武器", 1, interval=40, pool_position=80,
                pity_labels=("小保底", "大保底", "歪"),
            ),),
        )

        self.assertIn('style="width:50.0%"', draw_module._draw_gacha_pool(character))
        weapon_html = draw_module._draw_gacha_pool(weapon)
        self.assertNotIn("CURRENT", weapon_html)
        self.assertIn('style="width:100.0%"', weapon_html)
        self.assertIn("第80抽", weapon_html)
        self.assertIn("小保底", weapon_html)
        self.assertIn("大保底", weapon_html)
        self.assertIn('<span class="pity-hit pity-hit-guarantee pity-hit-small">小保底</span>', weapon_html)
        self.assertIn('<span class="pity-hit pity-hit-guarantee pity-hit-large">大保底</span>', weapon_html)
        self.assertIn('<span class="pity-hit pity-hit-miss">歪</span>', weapon_html)
        self.assertIn('<b>40 抽</b><div class="pity-hits">', weapon_html)
        self.assertIn("距大保底", weapon_html)
        self.assertIn("已触发", weapon_html)

    def assert_grayscale_png(self, content: bytes, *, minimum_height: int = 400):
        image = Image.open(BytesIO(content)).convert("RGB")
        self.assertGreater(image.width, 1000)
        self.assertGreater(image.height, minimum_height)
        extrema = image.getextrema()
        self.assertEqual(extrema[0], extrema[1])
        self.assertEqual(extrema[1], extrema[2])

    async def test_attendance_card_shows_monthly_count_when_available(self):
        view = models_module.AttendanceCardView(
            roles=[
                models_module.AttendanceRoleView(
                    nickname="测试角色",
                    uid="****1234",
                    server_name="测试服务器",
                    status="success",
                    message="签到成功",
                    monthly_count=12,
                ),
                models_module.AttendanceRoleView(
                    nickname="失败角色",
                    uid="****5678",
                    server_name="测试服务器",
                    status="failed",
                    message="签到失败",
                ),
            ],
        )
        renderer = mock.AsyncMock(return_value=b"png")

        with mock.patch.object(draw_module, "_draw_neutral_card", renderer):
            await draw_module.draw_attendance_card(view)

        body = renderer.await_args.args[1]
        self.assertEqual(body.count("当月累签"), 1)
        self.assertIn("12 天", body)
        self.assertNotIn("None 天", body)

    async def test_attendance_card_embeds_reward_icon(self):
        icon_url = "data:image/png;base64,AA=="
        view = models_module.AttendanceCardView(
            roles=[
                models_module.AttendanceRoleView(
                    nickname="测试角色",
                    uid="****1234",
                    server_name="测试服务器",
                    status="success",
                    message="签到成功",
                    rewards=[models_module.AttendanceRewardView("嵌晶玉", 2, icon_url)],
                )
            ],
        )
        renderer = mock.AsyncMock(return_value=b"png")
        image_urls = mock.AsyncMock(return_value={icon_url: icon_url})

        with (
            mock.patch.object(draw_module, "_image_data_urls", image_urls),
            mock.patch.object(draw_module, "_draw_neutral_card", renderer),
        ):
            await draw_module.draw_attendance_card(view)

        image_urls.assert_awaited_once_with((icon_url,))
        body = renderer.await_args.args[1]
        self.assertIn('class="attendance-reward-icon"', body)
        self.assertIn(f'src="{icon_url}"', body)
        self.assertIn("嵌晶玉", body)

    async def test_attendance_and_analysis_cards_are_grayscale(self):
        attendance = models_module.AttendanceCardView(
            roles=[
                models_module.AttendanceRoleView(
                    nickname="测试角色",
                    uid="****1234",
                    server_name="测试服务器",
                    status="success",
                    message="签到成功",
                    rewards=[models_module.AttendanceRewardView("奖励", 2)],
                )
            ],
            generated_at="2026-07-22 10:00",
        )
        role = store_module.EndfieldRole(1, 1, "qq", "binding", "role", "server", "测试角色", "测试服务器", True)
        analysis = gacha_module.GachaAnalysis(
            role=role,
            total=12,
            rarity_counts={6: 1, 5: 3, 4: 8},
            pools=(gacha_module.PoolAnalysis("pool", "联合寻访", "角色", 12, 4),),
            six_stars=(gacha_module.SixStarEvent("六星角色", "联合寻访", "角色", 1_753_156_800),),
            intervals=(),
            average_interval=None,
            last_sync_at=1_753_156_800,
            complete=True,
            errors=(),
        )
        self.assert_grayscale_png(await draw_module.draw_attendance_card(attendance))
        self.assert_grayscale_png(await draw_module.draw_gacha_analysis_card(analysis, uid="****1234"), minimum_height=700)

    async def test_history_card_is_grayscale_and_handles_twenty_rows(self):
        view = models_module.GachaHistoryView(
            nickname="很长的终末地角色昵称用于布局测试",
            uid="****1234",
            server_name="测试服务器",
            page=1,
            total_pages=1,
            total=20,
            items=[
                models_module.GachaHistoryItemView(
                    time="2026-07-22 10:00",
                    pool_name="联合寻访卡池名称",
                    item_name=f"测试角色或武器名称 {index}",
                    rarity=6 if index % 7 == 0 else 5,
                    item_type="角色" if index % 2 else "武器",
                )
                for index in range(20)
            ],
        )
        content = await draw_module.draw_gacha_history_card(view)
        self.assert_grayscale_png(content, minimum_height=1000)


if __name__ == "__main__":
    unittest.main()
