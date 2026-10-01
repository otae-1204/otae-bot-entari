"""池类型注册表 `plugins/endfield/gacha/pools.py` 的识别矩阵与规则参数。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "endfield_gacha_pool_tables.json"


def _load_pools():
    name = "endfield_gacha_pools_for_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins/endfield/gacha/pools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pools = _load_pools()


def _rule(row: dict, *, with_type: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        pool_id=row["id"],
        up_item_ids=tuple(row["upIds"]),
        hard_guarantee=0,
        pool_name=row["name"],
        pool_kind=row["table"],
        type_code=row["type"] if with_type else -1,
        table=row["table"],
        sort_id=row["sortId"],
        pool_version=row["gachaPoolVersion"],
        client_top_time_id=row["clientTopTimeId"],
    )


def _record(pool_id: str, pool_type: str, pool_name: str, item_type: str = "角色") -> SimpleNamespace:
    return SimpleNamespace(pool_id=pool_id, pool_type=pool_type, pool_name=pool_name, item_type=item_type)


class PoolKindRegistryTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]

    def test_ake_table_matrix_resolves_every_row_without_unknown(self):
        resolved = {}
        for row in self.rows:
            item_type = "武器" if row["table"] == "weapon" else "角色"
            kind = pools.resolve_pool_kind(
                pool_id=row["id"], pool_type="", pool_name=row["name"], item_type=item_type, rule=_rule(row),
            )
            self.assertEqual(kind.key, row["expected_kind"], row["id"])
            self.assertFalse(kind.is_unknown, row["id"])
            resolved[row["id"]] = kind.key
            # AKE 不可用（type_code=-1）时，poolId 前缀必须给出同样的结论。
            fallback = pools.resolve_pool_kind(
                pool_id=row["id"], pool_type="", pool_name="", item_type=item_type,
                rule=_rule(row, with_type=False),
            )
            self.assertEqual(fallback.key, row["expected_kind"], f"prefix fallback {row['id']}")
        counts = {}
        for key in resolved.values():
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(counts["special"], 11)
        self.assertEqual(counts["beginner"], 1)
        self.assertEqual(counts["standard"], 1)
        self.assertEqual(counts["joint"], 1)
        self.assertEqual(counts["rerun"], 1)
        self.assertEqual(counts["weapon_constant"], 5)
        self.assertEqual(counts["weapon_limited"], 13)
        self.assertEqual(counts["weapon_rerun"], 1)

    def test_weapon_table_without_prefix_uses_client_top_time_and_sort_id(self):
        constant = SimpleNamespace(type_code=0, table="weapon", sort_id=0, client_top_time_id="", up_item_ids=())
        limited = SimpleNamespace(type_code=0, table="weapon", sort_id=3, client_top_time_id="t1", up_item_ids=())
        rerun = SimpleNamespace(type_code=1, table="weapon", sort_id=0, client_top_time_id="", up_item_ids=())
        self.assertEqual(pools.resolve_pool_kind(pool_id="x1", item_type="武器", rule=constant).key, "weapon_constant")
        self.assertEqual(pools.resolve_pool_kind(pool_id="x2", item_type="武器", rule=limited).key, "weapon_limited")
        self.assertEqual(pools.resolve_pool_kind(pool_id="x3", item_type="武器", rule=rerun).key, "weapon_rerun")

    def test_official_enum_path(self):
        expected = {
            "E_CharacterGachaPoolType_Special": "special",
            "E_CharacterGachaPoolType_Joint": "joint",
            "E_CharacterGachaPoolType_Standard": "standard",
            "E_CharacterGachaPoolType_Beginner": "beginner",
            "E_CharacterGachaPoolType_Rerun": "rerun",
        }
        for enum, key in expected.items():
            self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type=enum).key, key)
            self.assertEqual(pools.kind_from_enum(enum).key, key)
        with mock.patch.object(pools, "logger") as logger:
            pools._warned.clear()
            first = pools.resolve_pool_kind(pool_id="collab_1", pool_type="E_CharacterGachaPoolType_Collab")
            second = pools.resolve_pool_kind(pool_id="collab_2", pool_type="E_CharacterGachaPoolType_Collab")
        self.assertEqual((first.key, second.key), ("unknown_char", "unknown_char"))
        self.assertTrue(first.is_unknown)
        self.assertEqual(logger.warning.call_count, 1)
        self.assertTrue(pools.kind_from_enum("E_CharacterGachaPoolType_Collab").is_unknown)
        self.assertTrue(pools.kind_from_enum("foo").is_unknown)

    def test_prefix_path_without_rule(self):
        self.assertEqual(pools.resolve_pool_kind(pool_id="rerun_chr_x").key, "rerun")
        self.assertEqual(pools.resolve_pool_kind(pool_id="weaponbox_constant_9", item_type="武器").key, "weapon_constant")
        self.assertEqual(pools.resolve_pool_kind(pool_id="weponbox_2_0_1", item_type="武器").key, "weapon_limited")
        self.assertEqual(pools.resolve_pool_kind(pool_id="rerun_wpn_x", pool_type="weapon", item_type="武器").key, "weapon_rerun")

    def test_name_heuristics(self):
        self.assertEqual(
            pools.resolve_pool_kind(pool_id="p1", pool_type="weapon", pool_name="点绘申领", item_type="武器").key,
            "weapon_rerun",
        )
        self.assertEqual(
            pools.resolve_pool_kind(pool_id="p2", pool_type="重构寻访", pool_name="绚丽异彩").key, "rerun",
        )
        self.assertEqual(pools.resolve_pool_kind(pool_id="p3", pool_type="x", pool_name="辉光庆典").key, "joint")
        self.assertEqual(pools.resolve_pool_kind(pool_id="p4", pool_type="x", pool_name="基础寻访").key, "standard")
        self.assertEqual(
            pools.resolve_pool_kind(pool_id="p5", pool_type="weapon", pool_name="常驻申领", item_type="武器").key,
            "weapon_constant",
        )

    def test_legacy_defaults_keep_old_behaviour(self):
        self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type="x").key, "special")
        self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type="special").key, "special")
        self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type="").key, "special")
        self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type="weapon", item_type="武器").key, "weapon_limited")
        self.assertEqual(pools.resolve_pool_kind(pool_id="p", pool_type="", item_type="武器").key, "weapon_limited")
        self.assertEqual(
            pools.resolve_pool_kind(pool_id="p", pool_type="E_WeaponGachaPoolType_New", item_type="武器").key,
            "unknown_weapon",
        )

    def test_labels_follow_item_type_naming_rule(self):
        for kind in pools.KINDS:
            self.assertEqual(kind.label.endswith("寻访"), kind.item_type == "角色", kind.key)
            self.assertEqual(kind.label.endswith("申领"), kind.item_type == "武器", kind.key)
        self.assertEqual(
            [kind.key for kind in pools.KINDS],
            [
                "special", "rerun", "joint", "beginner", "standard", "unknown_char",
                "weapon_limited", "weapon_rerun", "weapon_constant", "unknown_weapon",
            ],
        )

    def test_registry_parameters(self):
        rerun, special, weapon_rerun = pools.RERUN, pools.SPECIAL, pools.WEAPON_RERUN
        self.assertEqual((rerun.small_pity, rerun.soft_pity_start, rerun.hard_guarantee_default), (80, 66, 120))
        self.assertEqual((rerun.rush_thresholds, rerun.keepsake_cycle, rerun.cumulative_scope), ((30, 60, 90), 240, "series"))
        self.assertEqual((special.free_ten_unlock, special.keepsake_cycle, special.cumulative_scope), (30, 240, "pool"))
        self.assertEqual((weapon_rerun.small_pity, weapon_rerun.hard_guarantee_default), (4, 80))
        self.assertEqual(pools.BEGINNER.fixed_guarantee_position, 40)
        self.assertFalse(pools.JOINT.has_up)
        self.assertTrue(pools.WEAPON_CONSTANT.in_expectation)
        self.assertEqual(pools.EXPECTATION_GROUPS["weapon"], ("weapon_limited", "weapon_rerun", "weapon_constant"))

    def test_pity_family(self):
        self.assertEqual(pools.pity_family(pools.SPECIAL, "special_1"), "special")
        self.assertEqual(pools.pity_family(pools.RERUN, "rerun_chr_a"), "rerun")
        self.assertEqual(pools.pity_family(pools.RERUN, "rerun_chr_b"), "rerun")
        self.assertNotEqual(pools.pity_family(pools.SPECIAL, "x"), pools.pity_family(pools.RERUN, "x"))
        self.assertEqual(pools.pity_family(pools.JOINT, "Joint_1"), "joint:joint_1")
        self.assertEqual(pools.pity_family(pools.BEGINNER, "beginner_1"), "beginner:beginner_1")
        self.assertEqual(pools.pity_family(pools.STANDARD, "whatever"), "standard")
        self.assertEqual(pools.chain_label(pools.RERUN), "重构间共享继承")

    def test_series_key(self):
        rule = SimpleNamespace(up_item_ids=("chr_0017_yvonne",))
        self.assertEqual(pools.series_key(pools.RERUN, rule, "rerun_chr_yvonne", "绚丽异彩"), "rerun:chr_0017_yvonne")
        self.assertEqual(pools.series_key(pools.RERUN, None, "rerun_chr_yvonne", "绚丽异彩#2"), "rerun:name:绚丽异彩")
        self.assertEqual(pools.series_key(pools.RERUN, None, "rerun_chr_yvonne_2", ""), "rerun:id:rerun_chr_yvonne")
        self.assertEqual(pools.series_key(pools.SPECIAL, rule, "special_1", "特许"), "pool:special_1")
        self.assertEqual(pools.series_base_name("绚丽异彩 #2"), "绚丽异彩")

    def test_weapon_reward_claims(self):
        self.assertEqual(
            list(pools.weapon_reward_claims(pools.WEAPON_RERUN_SCHEDULE, 50)),
            [(10, "weapon_box"), (18, "up_weapon"), (26, "weapon_box"), (34, "up_weapon"), (42, "weapon_box"), (50, "up_weapon")],
        )
        self.assertEqual(
            list(pools.weapon_reward_claims(pools.WEAPON_LIMITED_SCHEDULE, 50)),
            [(18, "up_weapon"), (34, "up_weapon"), (50, "up_weapon")],
        )
        self.assertEqual(list(pools.weapon_reward_claims(pools.WEAPON_RERUN_SCHEDULE, 17)), [(10, "weapon_box")])
        self.assertEqual(list(pools.weapon_reward_claims(None, 100)), [])
        self.assertEqual(
            pools.upcoming_weapon_rewards(pools.WEAPON_RERUN_SCHEDULE, 26),
            ((34, "up_weapon"), (42, "weapon_box")),
        )
        self.assertEqual(pools.upcoming_weapon_rewards(pools.WEAPON_LIMITED_SCHEDULE, 0), ((18, "up_weapon"),))

    def test_resolve_character_pool_types(self):
        result = pools.resolve_character_pool_types(
            {"E_CharacterGachaPoolType_Special": "特许", "Foo": "x"},
        )
        self.assertEqual(
            result,
            (
                "E_CharacterGachaPoolType_Special",
                "E_CharacterGachaPoolType_Joint",
                "E_CharacterGachaPoolType_Standard",
                "E_CharacterGachaPoolType_Beginner",
                "E_CharacterGachaPoolType_Rerun",
            ),
        )
        with_collab = pools.resolve_character_pool_types({"E_CharacterGachaPoolType_Collab": "联动"})
        self.assertEqual(with_collab[0], "E_CharacterGachaPoolType_Collab")
        self.assertIn("E_CharacterGachaPoolType_Rerun", with_collab)
        self.assertEqual(pools.resolve_character_pool_types({}), pools.DEFAULT_CHARACTER_POOL_TYPES)
        self.assertEqual(pools.resolve_character_pool_types(None), pools.DEFAULT_CHARACTER_POOL_TYPES)

    def test_kind_from_stream_key(self):
        kind, target = pools.kind_from_stream_key("char:E_CharacterGachaPoolType_Rerun")
        self.assertEqual((kind.key, target), ("rerun", ""))
        self.assertEqual(pools.kind_from_stream_key("weapon:all"), (None, "weapon"))
        self.assertEqual(pools.kind_from_stream_key("weapon:rerun_wpn_x"), (None, "rerun_wpn_x"))
        self.assertEqual(pools.kind_from_stream_key(""), (None, ""))

    def test_show_standard_pools_flag_defaults_to_true(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in pools.SHOW_STANDARD_ENV_KEYS:
                os.environ.pop(key, None)
            self.assertTrue(pools.show_standard_pools())
        with mock.patch.dict(os.environ, {"ENDFIELD_GACHA_SHOW_STANDARD": "0"}):
            self.assertFalse(pools.show_standard_pools())
        with mock.patch.dict(os.environ, {"GACHA_SHOW_STANDARD": "false"}):
            os.environ.pop("ENDFIELD_GACHA_SHOW_STANDARD", None)
            self.assertFalse(pools.show_standard_pools())
        with mock.patch.dict(os.environ, {"ENDFIELD_GACHA_SHOW_STANDARD": "1"}):
            self.assertTrue(pools.show_standard_pools())


if __name__ == "__main__":
    unittest.main()
