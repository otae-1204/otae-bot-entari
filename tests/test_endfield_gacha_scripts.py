"""批量脚本与 `gacha/pools.py` 注册表的口径一致性（不联网、不读库）。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative_path: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


aggregate = _load_script("endfield_aggregate_script_for_test", "scripts/aggregate_endfield_gacha_stats.py")
pools = aggregate.pools_module
GachaRecord = aggregate.GachaRecord


def _record(pool_id: str, pool_type: str, pool_name: str = "池", item_type: str = "角色") -> GachaRecord:
    return GachaRecord("role", "server", pool_id, pool_name, pool_type, "1", 1, "item", "名", 4, item_type)


class AggregateScriptConsistencyTests(unittest.TestCase):
    def test_pity_family_matches_registry(self):
        samples = [
            _record("special_1_5_1", "E_CharacterGachaPoolType_Special"),
            _record("rerun_chr_yvonne", "E_CharacterGachaPoolType_Rerun", "绚丽异彩"),
            _record("rerun_chr_other", "E_CharacterGachaPoolType_Rerun", "另一期"),
            _record("joint_1_3_1", "E_CharacterGachaPoolType_Joint", "特殊寻访"),
            _record("beginner_1", "E_CharacterGachaPoolType_Beginner", "启程寻访"),
            _record("standard_1", "E_CharacterGachaPoolType_Standard", "基础寻访"),
            _record("legacy", "x", "旧池"),
        ]
        families = [aggregate._character_pity_family(record) for record in samples]
        self.assertEqual(
            families,
            ["special", "rerun", "rerun", "joint:joint_1_3_1", "beginner:beginner_1", "standard", "special"],
        )
        for record, family in zip(samples, families):
            self.assertEqual(
                family, pools.pity_family(pools.kind_for_record(record, None), record.pool_id),
            )

    def test_miss_up_scope_includes_rerun_and_excludes_isolated_pools(self):
        self.assertTrue(aggregate._xhh_pool_has_up("角色", "special", "special_1"))
        self.assertTrue(aggregate._xhh_pool_has_up("角色", "E_CharacterGachaPoolType_Rerun", "rerun_chr_x"))
        self.assertFalse(aggregate._xhh_pool_has_up("角色", "joint", "joint_1"))
        self.assertFalse(aggregate._xhh_pool_has_up("角色", "E_CharacterGachaPoolType_Beginner", "beginner"))
        self.assertFalse(aggregate._xhh_pool_has_up("角色", "standard", "standard"))
        self.assertFalse(aggregate._xhh_pool_has_up("武器", "weapon", "weponbox_1"))

    def test_script_loads_only_store_and_registry(self):
        package = aggregate._PACKAGE
        loaded = {name for name in sys.modules if name.startswith(f"{package}.")}
        self.assertEqual(loaded, {f"{package}.account", f"{package}.account.store", f"{package}.account.crypto",
                                  f"{package}.gacha", f"{package}.gacha.pools"})


if __name__ == "__main__":
    unittest.main()
