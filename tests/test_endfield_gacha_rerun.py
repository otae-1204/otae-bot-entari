"""抽卡分析 v3 后端：重构寻访保底链、加急招募、系列累计（复刻两种情况）、点绘申领、期望分组、小黑盒与同步。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "endfield_gacha_v3_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


import importlib  # noqa: E402

if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[PACKAGE] = package

crypto = _load(f"{PACKAGE}.account.crypto", "plugins/endfield/account/crypto.py")
store_module = _load(f"{PACKAGE}.account.store", "plugins/endfield/account/store.py")
xhh_module = _load(f"{PACKAGE}.gacha.xhh", "plugins/endfield/gacha/xhh.py")
client_module = _load(f"{PACKAGE}.account.client", "plugins/endfield/account/client.py")
gacha_module = _load(f"{PACKAGE}.gacha.service", "plugins/endfield/gacha/service.py")
assets_module = sys.modules[f"{PACKAGE}.gacha.assets"]
pools_module = sys.modules[f"{PACKAGE}.gacha.pools"]

GachaRecord = store_module.GachaRecord
GachaPoolRule = assets_module.GachaPoolRule
GachaItemMetadata = assets_module.GachaItemMetadata
RERUN = "E_CharacterGachaPoolType_Rerun"
SPECIAL = "E_CharacterGachaPoolType_Special"
STANDARD = "E_CharacterGachaPoolType_Standard"


def _role():
    return store_module.EndfieldRole(1, 1, "qq", "bind", "role", "server", "甲", "一区", True)


def _rerun_record(
    seq: int,
    rarity: int = 4,
    *,
    is_free: bool = False,
    pool_id: str = "rerun_a",
    version: int = 1,
    item_id: str = "",
    ts: int | None = None,
    pool_type: str = RERUN,
    name: str = "绚丽异彩",
    item_type: str = "角色",
) -> GachaRecord:
    return GachaRecord(
        "role", "server", pool_id, name, pool_type, str(seq), ts if ts is not None else seq,
        item_id or f"{pool_id}-{seq}", "六星" if rarity >= 6 else "结果", rarity, item_type,
        is_free=is_free, pool_version=version,
    )


def _weapon_record(
    seq: int,
    rarity: int = 4,
    *,
    pool_id: str = "rerun_wpn_a",
    version: int = 1,
    item_id: str = "",
    batch: int | None = None,
    name: str = "点绘申领",
) -> GachaRecord:
    return GachaRecord(
        "role", "server", pool_id, name, "weapon", str(seq), batch if batch is not None else (seq - 1) // 10 + 1,
        item_id or f"{pool_id}-{seq}", "六星武器" if rarity >= 6 else "武器", rarity, "武器",
        pool_version=version,
    )


def _pools_by_card(result) -> dict[str, object]:
    return {pool.card_key: pool for pool in result.pools}


class RerunPityChainTests(unittest.TestCase):
    def setUp(self):
        self.role = _role()

    def test_rerun_chain_is_shared_across_rerun_pools_and_isolated_from_special(self):
        records = [_rerun_record(seq, pool_id="rerun_a") for seq in range(1, 41)]
        records.append(_rerun_record(41, 4, pool_id="special_x", pool_type=SPECIAL, name="特许", ts=41))
        records.append(_rerun_record(42, 6, pool_id="special_x", pool_type=SPECIAL, name="特许", ts=42))
        # rerun_a 垫 40 + rerun_b 垫 39，第 80 抽（rerun_b 第 40 抽）出六星：链跨池共享，特许六星不重置
        records.extend(_rerun_record(seq, pool_id="rerun_b", name="另一重构") for seq in range(43, 82))
        records.append(_rerun_record(82, 6, pool_id="rerun_b", name="另一重构"))

        result = gacha_module.build_gacha_analysis(self.role, records, [])
        pools = {pool.pool_id: pool for pool in result.pools}

        six = pools["rerun_b"].six_stars[0]
        self.assertEqual((six.interval, six.pity_labels), (80, ("小保底",)))
        self.assertEqual(pools["special_x"].six_stars[0].interval, 2)
        self.assertEqual({pools["rerun_a"].pity_family, pools["rerun_b"].pity_family}, {"rerun"})
        self.assertEqual(pools["special_x"].pity_family, "special")
        self.assertEqual({pools["rerun_a"].kind_key, pools["rerun_b"].kind_key}, {"rerun"})
        self.assertEqual(pools["rerun_b"].kind_label, "重构寻访")
        chains = {chain.family: chain for chain in result.chains}
        self.assertEqual(chains["rerun"].label, "重构间共享继承")
        self.assertEqual(chains["rerun"].pool_ids, ("rerun_b", "rerun_a"))
        self.assertEqual(chains["rerun"].progress, 0)
        self.assertEqual(chains["special"].progress, 0)
        self.assertTrue(pools["rerun_b"].is_current)
        self.assertTrue(pools["special_x"].is_current)
        self.assertFalse(pools["rerun_a"].is_current)

    def test_rush_free_pulls_stay_out_of_chain_and_cumulative(self):
        records = [_rerun_record(seq, ts=100 + (seq - 1) // 10) for seq in range(1, 61)]
        records.extend(_rerun_record(seq, is_free=True, ts=200) for seq in range(61, 71))
        records.extend(
            _rerun_record(seq, 6 if seq == 77 else 4, is_free=True, ts=300, item_id="chr_up" if seq == 77 else "")
            for seq in range(71, 81)
        )
        rules = {"rerun_a": GachaPoolRule("rerun_a", ("chr_up",), 0)}
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules)
        pool = result.pools[0]

        self.assertEqual((pool.since_six_star, pool.small_pity_progress, pool.small_pity_limit), (60, 60, 80))
        self.assertEqual((pool.paid_total, pool.free_pull_count, pool.total), (60, 20, 80))
        self.assertEqual((pool.rush_claimed, pool.rush_used), (2, 2))
        self.assertEqual((pool.rush_next_threshold, pool.rush_next_remaining), (90, 30))
        self.assertEqual(pool.rush_thresholds, (30, 60, 90))
        self.assertEqual((pool.keepsake_progress, pool.keepsake_claims), (60, 0))
        self.assertEqual((pool.series_total, pool.series_inherited_total, pool.series_index), (60, 0, 1))
        self.assertEqual(pool.six_stars, ())
        self.assertFalse(pool.large_pity_consumed)
        self.assertEqual([batch.threshold for batch in pool.free_batches], [60, 30])
        self.assertEqual({batch.source for batch in pool.free_batches}, {"rush"})
        free_six = pool.free_batches[0].six_stars[0]
        self.assertEqual((free_six.is_free, free_six.up_status, free_six.pity_labels), (True, "up", ()))
        self.assertEqual(
            [(item.kind, item.at_position, item.remaining) for item in pool.next_rewards],
            [("rush", 90, 30), ("keepsake", 240, 180)],
        )
        expectation = result.expectations["rerun"]
        self.assertEqual((expectation.paid_pulls, expectation.free_pulls, expectation.up_outcomes), (60, 20, 1))
        self.assertEqual(result.expectations["special"].paid_pulls, 0)
        self.assertEqual(expectation.group, "rerun")
        self.assertTrue(expectation.up_known)
        series = result.series[0]
        self.assertEqual((series.paid_total, series.rush_claimed, series.rush_used, series.rush_next_threshold), (60, 2, 2, 90))
        self.assertEqual(series.runs[0].free_batches, 2)

    def test_soft_pity_fields(self):
        records = [_rerun_record(seq, 6 if seq in {70, 150} else 4) for seq in range(1, 216)]
        pool = gacha_module.build_gacha_analysis(self.role, records, []).pools[0]
        newest, older = pool.six_stars

        self.assertEqual((newest.interval, newest.soft_pity_hit, newest.pity_labels), (80, False, ("小保底",)))
        self.assertEqual((older.interval, older.soft_pity_hit), (70, True))
        self.assertEqual((pool.small_pity_progress, pool.soft_pity_start, pool.soft_pity_active), (65, 66, True))
        self.assertEqual(pool.five_star_pity_limit, 10)

    def test_rerun_large_pity_defaults_to_120_and_marks_off_banner(self):
        records = [
            _rerun_record(seq, 6 if seq in {40, 120} else 4, item_id="chr_up" if seq == 120 else "")
            for seq in range(1, 121)
        ]
        rules = {"rerun_a": GachaPoolRule("rerun_a", ("chr_up",), 0)}
        pool = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules).pools[0]
        events = {item.pool_position: item for item in pool.six_stars}

        self.assertEqual(events[120].pity_labels, ("小保底", "大保底"))
        self.assertEqual((events[40].up_status, events[40].pity_labels), ("off", ("歪",)))
        self.assertEqual((pool.large_pity_limit, pool.large_pity_source), (120, "default"))
        self.assertTrue(pool.large_pity_known and pool.large_pity_consumed)
        self.assertEqual((pool.large_pity_consumed_at, pool.large_pity_scope), (120, "run"))
        self.assertTrue(pool.up_status_known)

    def test_five_star_pity_progress(self):
        records = [_rerun_record(seq, 5 if seq == 12 else 4) for seq in range(1, 20)]
        pool = gacha_module.build_gacha_analysis(self.role, records, []).pools[0]
        self.assertEqual((pool.five_star_pity_progress, pool.five_star_pity_limit), (7, 10))
        xhh_only = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=30, imported_at=5,
            pools=(store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 30, is_current=True),),
            six_stars=(),
        )
        xhh_pool = gacha_module.build_gacha_analysis(self.role, [], [], xhh_import=xhh_only).pools[0]
        self.assertIsNone(xhh_pool.five_star_pity_progress)
        self.assertEqual(xhh_pool.kind_key, "rerun")


class SyntheticSampleTests(unittest.TestCase):
    """按真实抽取节奏合成的样本（tests/fixtures/endfield_gacha_rerun_synthetic.json）：手算 60 / 2 / 2 / 90。"""

    def test_synthetic_rerun_sample_matches_hand_calculation(self):
        import json

        payload = json.loads((ROOT / "tests/fixtures/endfield_gacha_rerun_synthetic.json").read_text(encoding="utf-8"))
        pool = payload["pool"]
        records: list[GachaRecord] = []
        seq = 1300
        for batch in payload["batches"]:
            for position in range(1, batch["count"] + 1):
                seq += 1
                six = position in batch["six_star_at"]
                records.append(
                    GachaRecord(
                        "role", "server", pool["pool_id"], pool["pool_name"], pool["pool_type"], str(seq),
                        batch["gacha_ts"], pool["up_item_ids"][0] if six else f"chr_filler_{seq}",
                        "示例UP" if six else "结果", 6 if six else 4, "角色",
                        is_free=batch["is_free"], pool_version=pool["pool_version"],
                    )
                )
        rules = {pool["pool_id"]: GachaPoolRule(pool["pool_id"], tuple(pool["up_item_ids"]), 0)}
        analysis = gacha_module.build_gacha_analysis(_role(), records, [], pool_rules=rules)
        view = analysis.pools[0]
        expected = payload["expected"]
        actual = {key: getattr(view, key) for key in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(view.six_stars, ())
        self.assertEqual([len(batch.six_stars) for batch in view.free_batches], [1, 0])
        self.assertEqual(view.free_batches[0].six_stars[0].up_status, "up")
        self.assertEqual((view.large_pity_consumed, view.large_pity_progress, view.large_pity_limit), (False, 60, 120))
        self.assertEqual(analysis.total, 80)


class RerunSeriesTests(unittest.TestCase):
    """复刻两种情况（同 poolId 不同 version / 新 poolId）必须得到完全相同的系列结论。"""

    def setUp(self):
        self.role = _role()

    def _records(self, *, same_pool_id: bool):
        first_id = "rerun_x"
        second_id = "rerun_x" if same_pool_id else "rerun_x_2"
        records = [_rerun_record(seq, pool_id=first_id, version=1, ts=seq) for seq in range(1, 201)]
        records.extend(_rerun_record(seq, is_free=True, pool_id=first_id, version=1, ts=150) for seq in range(201, 211))
        records.extend(
            _rerun_record(seq, pool_id=second_id, version=2 if same_pool_id else 0, ts=1000 + seq)
            for seq in range(211, 271)
        )
        records.extend(
            _rerun_record(seq, is_free=True, pool_id=second_id, version=2 if same_pool_id else 0, ts=2000)
            for seq in range(271, 281)
        )
        rules = {
            first_id: GachaPoolRule(first_id, ("chr_up",), 0),
            second_id: GachaPoolRule(second_id, ("chr_up",), 0),
        }
        return records, rules

    def _assert_series(self, result, first_key: str, second_key: str):
        pools = _pools_by_card(result)
        first, second = pools[first_key], pools[second_key]
        self.assertEqual(len(result.pools), 2)
        self.assertTrue(second.is_current)
        self.assertFalse(first.is_current)
        self.assertEqual((first.series_index, second.series_index), (1, 2))
        self.assertEqual((first.series_run_count, second.series_run_count), (2, 2))
        self.assertEqual((first.series_inherited_to, second.series_inherited_to), (2, 0))
        self.assertEqual((first.run_label, second.run_label), ("#1", "#2"))
        self.assertEqual(first.series_key, second.series_key)
        self.assertEqual(second.series_key, "rerun:chr_up")
        self.assertEqual((second.series_inherited_total, second.series_total), (200, 260))
        self.assertEqual((first.series_inherited_total, first.series_total), (0, 260))
        self.assertEqual((second.rush_claimed, second.rush_used, second.rush_next_threshold), (3, 2, 0))
        self.assertEqual((first.rush_claimed, first.rush_used), (0, 0))
        self.assertEqual((second.keepsake_progress, second.keepsake_claims), (20, 1))
        self.assertEqual((first.keepsake_progress, first.keepsake_claims), (0, 0))
        self.assertEqual(len(second.keepsake_gifts), 1)
        gift = second.keepsake_gifts[0]
        self.assertEqual((gift.pool_position, gift.series_position, gift.gift_kind, gift.estimated), (40, 240, "keepsake", False))
        self.assertEqual(gift.gacha_ts, 1000 + 250)
        self.assertEqual(first.keepsake_gifts, ())
        self.assertEqual([batch.threshold for batch in first.free_batches], [30])
        self.assertEqual([batch.threshold for batch in second.free_batches], [60])
        self.assertEqual([item.kind for item in second.next_rewards], ["keepsake"])
        self.assertEqual(first.next_rewards, ())
        self.assertEqual(len(result.series), 1)
        series = result.series[0]
        self.assertEqual((series.name, series.paid_total, series.rush_claimed), ("绚丽异彩", 260, 3))
        self.assertEqual([run.run_label for run in series.runs], ["#1", "#2"])
        self.assertEqual([run.is_current for run in series.runs], [False, True])
        self.assertFalse(series.estimated)
        summary = next(item for item in result.kind_summaries if item.key == "rerun")
        self.assertEqual((summary.pool_count, summary.paid_total, summary.free_pull_count), (2, 260, 20))
        self.assertEqual(summary.current_card_key, second_key)
        self.assertIs(summary.series, series)
        self.assertEqual(summary.chain.family, "rerun")

    def test_case_a_same_pool_id_with_versions(self):
        records, rules = self._records(same_pool_id=True)
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules)
        self._assert_series(result, "rerun_x#1", "rerun_x#2")
        self.assertEqual({pool.pool_id for pool in result.pools}, {"rerun_x"})
        self.assertEqual({pool.pool_version for pool in result.pools}, {1, 2})

    def test_case_b_new_pool_id(self):
        records, rules = self._records(same_pool_id=False)
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules)
        self._assert_series(result, "rerun_x#1", "rerun_x_2#0")

    def test_same_pool_id_without_version_merges_into_single_run(self):
        records = [_rerun_record(seq, pool_id="rerun_x", version=0, ts=seq) for seq in range(1, 201)]
        records.extend(_rerun_record(seq, pool_id="rerun_x", version=0, ts=1000 + seq) for seq in range(201, 261))
        pool = gacha_module.build_gacha_analysis(self.role, records, []).pools[0]
        self.assertEqual((pool.series_run_count, pool.series_index, pool.series_total), (1, 1, 260))
        self.assertEqual(pool.card_key, "rerun_x#0")
        self.assertEqual((pool.rush_claimed, pool.keepsake_claims), (3, 1))

    def test_series_key_falls_back_to_name_without_rules(self):
        records = [_rerun_record(seq, pool_id="rerun_x", version=1, name="绚丽异彩", ts=seq) for seq in range(1, 11)]
        records.extend(_rerun_record(seq, pool_id="rerun_y", version=0, name="绚丽异彩#2", ts=100 + seq) for seq in range(11, 21))
        result = gacha_module.build_gacha_analysis(self.role, records, [])
        pools = _pools_by_card(result)
        self.assertEqual(pools["rerun_x#1"].series_key, "rerun:name:绚丽异彩")
        self.assertEqual(pools["rerun_y#0"].series_key, "rerun:name:绚丽异彩")
        self.assertEqual(pools["rerun_y#0"].series_name, "绚丽异彩")
        self.assertEqual(pools["rerun_y#0"].series_inherited_total, 10)


class WeaponRerunTests(unittest.TestCase):
    def setUp(self):
        self.role = _role()
        self.metadata = {
            "wpn_up": GachaItemMetadata("wpn_up", "艺术暴君", 6, "武器", icon_path="C:/cache/up.png"),
        }

    def test_single_period_rewards_and_pity_labels(self):
        records = [
            _weapon_record(seq, 6 if seq in {40, 80} else 4, item_id="wpn_up" if seq == 80 else "")
            for seq in range(1, 181)
        ]
        rules = {"rerun_wpn_a": GachaPoolRule("rerun_wpn_a", ("wpn_up",), 0)}
        pool = gacha_module.build_gacha_analysis(self.role, records, [], self.metadata, rules).pools[0]

        self.assertEqual((pool.kind_key, pool.kind_label, pool.kind_short), ("weapon_rerun", "重构申领", "点绘"))
        self.assertEqual(
            [(gift.claim_count, gift.gift_kind, gift.gift_type, gift.pool_position) for gift in pool.keepsake_gifts],
            [(18, "up_weapon", "武器", 180), (10, "weapon_box", "武库箱", 100)],
        )
        self.assertEqual(pool.keepsake_gifts[0].name, "艺术暴君")
        self.assertEqual(pool.keepsake_gifts[1].name, "自选武库箱")
        events = {item.pool_position: item for item in pool.six_stars}
        self.assertEqual(events[40].pity_labels, ("小保底", "歪"))
        self.assertEqual(events[80].pity_labels, ("小保底", "大保底"))
        self.assertEqual((pool.large_pity_limit, pool.large_pity_source, pool.small_pity_limit), (80, "default", 4))
        self.assertEqual((pool.weapon_claims, pool.series_claims, pool.series_inherited_claims), (18, 18, 0))
        self.assertEqual(
            [(item.kind, item.label, item.at_position, item.remaining, item.unit) for item in pool.next_rewards],
            [("weapon_box", "武库赠礼", 26, 8, "次申领"), ("up_weapon", "点绘赠礼", 34, 16, "次申领")],
        )

    def _two_periods(self, *, same_pool_id: bool):
        second_id = "rerun_wpn_a" if same_pool_id else "rerun_wpn_b"
        records = [_weapon_record(seq, pool_id="rerun_wpn_a", version=1) for seq in range(1, 101)]
        records.extend(
            _weapon_record(seq, pool_id=second_id, version=2 if same_pool_id else 0, batch=100 + (seq - 101) // 10 + 1)
            for seq in range(101, 261)
        )
        rules = {
            "rerun_wpn_a": GachaPoolRule("rerun_wpn_a", ("wpn_up",), 0),
            second_id: GachaPoolRule(second_id, ("wpn_up",), 0),
        }
        return records, rules

    def _assert_two_periods(self, result, first_key, second_key):
        pools = _pools_by_card(result)
        first, second = pools[first_key], pools[second_key]
        self.assertTrue(second.is_current)
        self.assertEqual((second.weapon_claims, second.series_claims, second.series_inherited_claims), (16, 26, 10))
        self.assertEqual((first.weapon_claims, first.series_claims), (10, 10))
        self.assertEqual(
            [(gift.claim_count, gift.gift_kind, gift.pool_position, gift.series_position) for gift in second.keepsake_gifts],
            [(26, "weapon_box", 160, 260), (18, "up_weapon", 80, 180)],
        )
        self.assertEqual(
            [(gift.claim_count, gift.gift_kind, gift.pool_position) for gift in first.keepsake_gifts],
            [(10, "weapon_box", 100)],
        )
        self.assertEqual(
            [(item.kind, item.at_position, item.remaining) for item in second.next_rewards],
            [("up_weapon", 34, 8), ("weapon_box", 42, 16)],
        )
        self.assertEqual(result.series[0].claims, 26)
        self.assertEqual(result.series[0].kind_key, "weapon_rerun")

    def test_two_periods_same_pool_id(self):
        records, rules = self._two_periods(same_pool_id=True)
        result = gacha_module.build_gacha_analysis(self.role, records, [], self.metadata, rules)
        self._assert_two_periods(result, "rerun_wpn_a#1", "rerun_wpn_a#2")

    def test_two_periods_new_pool_id(self):
        records, rules = self._two_periods(same_pool_id=False)
        result = gacha_module.build_gacha_analysis(self.role, records, [], self.metadata, rules)
        self._assert_two_periods(result, "rerun_wpn_a#1", "rerun_wpn_b#0")

    def test_limited_weapon_keeps_18_plus_16_schedule_and_next_reward(self):
        records = [_weapon_record(seq, pool_id="weponbox_1", name="限时申领") for seq in range(1, 201)]
        rules = {"weponbox_1": GachaPoolRule("weponbox_1", ("wpn_up",), 80)}
        pool = gacha_module.build_gacha_analysis(self.role, records, [], self.metadata, rules).pools[0]
        self.assertEqual(pool.kind_key, "weapon_limited")
        self.assertEqual([(gift.claim_count, gift.gift_kind) for gift in pool.keepsake_gifts], [(18, "up_weapon")])
        self.assertEqual(
            [(item.kind, item.label, item.at_position, item.remaining) for item in pool.next_rewards],
            [("up_weapon", "UP武器", 34, 14)],
        )
        self.assertEqual((pool.large_pity_limit, pool.large_pity_source), (80, "fz"))


class ExpectationAndCurrentPoolTests(unittest.TestCase):
    def setUp(self):
        self.role = _role()

    def _mixed_records(self):
        records = [_rerun_record(seq, pool_id="special_1", pool_type=SPECIAL, name="特许", ts=seq) for seq in range(1, 31)]
        records.extend(_rerun_record(seq, pool_id="rerun_1", ts=seq) for seq in range(31, 51))
        records.extend(_weapon_record(seq, pool_id="weponbox_1", name="限时申领", batch=seq) for seq in range(51, 81))
        records.extend(_weapon_record(seq, pool_id="weaponbox_constant_1", name="常驻申领", batch=seq) for seq in range(81, 101))
        records.extend(_weapon_record(seq, pool_id="rerun_wpn_1", batch=seq) for seq in range(101, 111))
        records.extend(_rerun_record(seq, pool_id="standard_1", pool_type=STANDARD, name="基础寻访", ts=seq) for seq in range(111, 121))
        return records

    def test_constant_weapon_counts_toward_weapon_expectation_and_groups_are_disjoint(self):
        result = gacha_module.build_gacha_analysis(self.role, self._mixed_records(), [])
        self.assertEqual(result.expectations["weapon"].paid_pulls, 60)
        self.assertEqual(result.expectations["rerun"].paid_pulls, 20)
        self.assertEqual(result.expectations["special"].paid_pulls, 30)
        legacy = gacha_module.calculate_six_star_expectation(result.pools, "角色")
        self.assertEqual(legacy, result.expectations["special"])
        self.assertEqual(gacha_module.calculate_six_star_expectation(result.pools, "武器"), result.expectations["weapon"])

    def test_one_current_pool_per_kind(self):
        result = gacha_module.build_gacha_analysis(self.role, self._mixed_records(), [])
        current = {pool.kind_key for pool in result.pools if pool.is_current}
        self.assertEqual(
            current,
            {"special", "rerun", "weapon_limited", "weapon_constant", "weapon_rerun", "standard"},
        )
        self.assertEqual(len([pool for pool in result.pools if pool.is_current]), 6)
        summaries = {item.key: item for item in result.kind_summaries}
        self.assertEqual(list(summaries), ["special", "rerun", "standard", "weapon_limited", "weapon_rerun", "weapon_constant"])
        self.assertEqual(summaries["weapon_constant"].label, "常驻申领")
        self.assertEqual(summaries["weapon_limited"].expectation_group, "weapon")

    def test_show_standard_flag_only_marks_hidden_by_default(self):
        records = self._mixed_records()
        hidden = gacha_module.build_gacha_analysis(self.role, records, [], show_standard=False)
        shown = gacha_module.build_gacha_analysis(self.role, records, [], show_standard=True)
        hidden_pool = next(pool for pool in hidden.pools if pool.pool_id == "standard_1")
        shown_pool = next(pool for pool in shown.pools if pool.pool_id == "standard_1")
        self.assertTrue(hidden_pool.hidden_by_default)
        self.assertFalse(shown_pool.hidden_by_default)
        self.assertEqual(hidden.total, shown.total)
        self.assertEqual(len(hidden.pools), len(shown.pools))
        self.assertEqual((hidden.show_standard_pools, shown.show_standard_pools), (False, True))
        with mock.patch.dict("os.environ", {"ENDFIELD_GACHA_SHOW_STANDARD": "0"}):
            from_env = gacha_module.build_gacha_analysis(self.role, records, [])
        self.assertFalse(from_env.show_standard_pools)
        self.assertTrue(next(item for item in from_env.kind_summaries if item.key == "standard").hidden_by_default)

    def test_unknown_enum_pool_keeps_records_but_no_rules(self):
        records = [
            _rerun_record(seq, 6 if seq == 5 else 4, pool_id="collab_1", pool_type="E_CharacterGachaPoolType_Collab", name="联动寻访", ts=seq)
            for seq in range(1, 11)
        ]
        rules = {"collab_1": GachaPoolRule("collab_1", ("chr_up",), 0)}
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules)
        pool = result.pools[0]
        self.assertTrue(pool.is_unknown_kind)
        self.assertEqual((pool.kind_key, pool.kind_label), ("unknown_char", "其他寻访"))
        self.assertEqual((pool.small_pity_limit, pool.large_pity_limit, pool.large_pity_known), (0, 0, False))
        self.assertEqual(pool.six_stars[0].pity_labels, ())
        self.assertEqual(pool.six_stars[0].up_status, "")
        self.assertFalse(pool.up_status_known)
        self.assertEqual(len(pool.six_stars), 1)
        for group in ("special", "rerun", "weapon"):
            self.assertEqual(result.expectations[group].paid_pulls, 0)
        self.assertTrue(next(item for item in result.kind_summaries if item.key == "unknown_char").is_unknown)

    def test_stream_errors_map_to_pools_and_summaries(self):
        records = self._mixed_records()
        states = [
            store_module.SyncState("role", "server", "char:E_CharacterGachaPoolType_Rerun", last_error="重构失败", last_sync_at=5),
            store_module.SyncState("role", "server", "weapon:all", last_error="武器失败", last_sync_at=5),
        ]
        result = gacha_module.build_gacha_analysis(self.role, records, states)
        pools = {pool.pool_id: pool for pool in result.pools}
        self.assertEqual(pools["rerun_1"].sync_error, "重构失败")
        self.assertEqual(pools["special_1"].sync_error, "")
        self.assertEqual(pools["weponbox_1"].sync_error, "武器失败")
        self.assertEqual(pools["weaponbox_constant_1"].sync_error, "武器失败")
        self.assertEqual(result.stream_errors, (("char:E_CharacterGachaPoolType_Rerun", "重构失败"), ("weapon:all", "武器失败")))
        summaries = {item.key: item for item in result.kind_summaries}
        self.assertEqual(summaries["rerun"].sync_error, "重构失败")
        self.assertEqual(summaries["special"].sync_error, "")
        self.assertFalse(result.complete)

    def test_card_keys_are_unique_and_pool_version_flows_through(self):
        records = [_rerun_record(seq, pool_id="rerun_1", version=3, ts=seq) for seq in range(1, 4)]
        records.extend(_rerun_record(seq, pool_id="special_1", pool_type=SPECIAL, version=9, ts=seq) for seq in range(4, 7))
        result = gacha_module.build_gacha_analysis(self.role, records, [])
        pools = {pool.pool_id: pool for pool in result.pools}
        self.assertEqual(pools["rerun_1"].card_key, "rerun_1#3")
        # 非系列 kind 不按版本拆期，card_key 固定 #0
        self.assertEqual((pools["special_1"].card_key, pools["special_1"].pool_version), ("special_1#0", 0))
        self.assertEqual(len({pool.card_key for pool in result.pools}), 2)

    def test_default_pool_analysis_construction_keeps_working(self):
        pool = gacha_module.PoolAnalysis("rerun_chr_x", "绚丽异彩", "角色", 10, 0)
        self.assertEqual((pool.kind_key, pool.card_key, pool.series_index, pool.next_rewards), ("", "", 1, ()))
        self.assertEqual(gacha_module.calculate_group_expectation([pool], "rerun").paid_pulls, 10)
        self.assertEqual(gacha_module.calculate_group_expectation([pool], "special").paid_pulls, 0)


class XhhRerunTests(unittest.TestCase):
    def setUp(self):
        self.role = _role()

    def test_infer_pool_type_and_item_type(self):
        self.assertEqual(xhh_module._infer_pool_type("rerun_chr_x", "绚丽异彩", "角色"), pools_module.XHH_RERUN_POOL_TYPE)
        self.assertEqual(xhh_module._infer_pool_type("p", "重构寻访·绚丽异彩", "角色"), pools_module.XHH_RERUN_POOL_TYPE)
        self.assertEqual(xhh_module._infer_pool_type("joint_1", "辉光庆典", "角色"), "E_CharacterGachaPoolType_Joint")
        self.assertEqual(xhh_module._infer_pool_type("rerun_wpn_x", "点绘申领", "武器"), "weapon")
        self.assertEqual(xhh_module._infer_item_type({}, "p", "补充武库"), "武器")

    def test_mark_current_pools_keeps_one_special_and_one_rerun(self):
        pools = [
            store_module.XhhGachaPool("special_new", "特许新", "special", "角色", 10, latest_ts=300),
            store_module.XhhGachaPool("special_old", "特许旧", "special", "角色", 10, latest_ts=100),
            store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 10, latest_ts=200),
            store_module.XhhGachaPool("rerun_wpn_x", "点绘申领", "weapon", "武器", 10, latest_ts=50),
            store_module.XhhGachaPool("weponbox_1", "限时申领", "weapon", "武器", 10, latest_ts=40),
        ]
        marked = xhh_module._mark_current_pools(pools, {pool.pool_id: index for index, pool in enumerate(pools)})
        self.assertEqual(
            [pool.pool_id for pool in marked if pool.is_current],
            ["special_new", "rerun_chr_x", "rerun_wpn_x", "weponbox_1"],
        )

    def test_character_pity_state_builds_one_chain_per_family(self):
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=160, imported_at=5,
            pools=(
                store_module.XhhGachaPool("special_a", "特许", "special", "角色", 50, sort_order=0),
                store_module.XhhGachaPool("rerun_b", "绚丽异彩", RERUN, "角色", 60, sort_order=1),
                store_module.XhhGachaPool("rerun_a", "旧重构", RERUN, "角色", 50, sort_order=2),
            ),
            six_stars=(
                store_module.XhhSixStar("rerun_b", "rb", "重构六星", "角色", 20, 30, 30),
            ),
        )
        intervals, progress = gacha_module._xhh_character_pity_state(imported)
        self.assertEqual(intervals[("rerun_b", "rb")], 80)
        self.assertEqual(progress["rerun_b"], 30)
        self.assertEqual(progress["special_a"], 50)

    def test_expected_free_pull_count_for_rerun_uses_rush_thresholds(self):
        snapshot = store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 70)
        self.assertEqual(gacha_module._xhh_expected_free_pull_count(snapshot), 20)
        self.assertEqual(gacha_module._xhh_expected_free_pull_count(store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 29)), 0)
        special = store_module.XhhGachaPool("special", "特许", "special", "角色", 30)
        self.assertEqual(gacha_module._xhh_expected_free_pull_count(special), 10)

    def test_rerun_snapshot_merge_marks_series_estimated(self):
        records = [_rerun_record(seq, pool_id="rerun_chr_x", version=1, ts=seq) for seq in range(1, 31)]
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=250, imported_at=5,
            pools=(store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 250, is_current=True, sort_order=0),),
            six_stars=(),
        )
        rules = {"rerun_chr_x": GachaPoolRule("rerun_chr_x", ("chr_up",), 0)}
        result = gacha_module.build_gacha_analysis(self.role, records, [], pool_rules=rules, xhh_import=imported)
        pool = result.pools[0]
        # 快照 250 抽 ⇒ 30/60/90 三档加急招募都已解锁，推算 3 次免费十连
        self.assertEqual((pool.paid_total, pool.free_pull_count, pool.recorded_total), (250, 30, 30))
        self.assertEqual(len(pool.free_batches), 3)
        self.assertTrue(pool.series_estimated)
        self.assertEqual((pool.rush_claimed, pool.keepsake_claims), (3, 1))
        self.assertTrue(pool.keepsake_gifts[0].estimated)
        self.assertEqual(pool.keepsake_gifts[0].series_position, 240)
        self.assertEqual({batch.source for batch in pool.free_batches}, {"rush"})
        self.assertTrue(result.series[0].estimated)

    def test_snapshot_total_is_split_between_versions_of_the_same_pool(self):
        records = [_rerun_record(seq, pool_id="rerun_chr_x", version=1, ts=seq) for seq in range(1, 201)]
        records.extend(_rerun_record(seq, pool_id="rerun_chr_x", version=2, ts=1000 + seq) for seq in range(201, 261))
        imported = store_module.XhhGachaImport(
            source_uid="role", nickname="甲", total_count=260, imported_at=5,
            pools=(store_module.XhhGachaPool("rerun_chr_x", "绚丽异彩", RERUN, "角色", 260, is_current=True, sort_order=0),),
            six_stars=(),
        )
        result = gacha_module.build_gacha_analysis(self.role, records, [], xhh_import=imported)
        pools = _pools_by_card(result)
        self.assertEqual((pools["rerun_chr_x#1"].paid_total, pools["rerun_chr_x#2"].paid_total), (200, 60))
        self.assertEqual(pools["rerun_chr_x#2"].series_total, 260)


class _FakeClient:
    def __init__(self, *, names=None, fail_pool: str = ""):
        self.names = names if names is not None else {pool: pool.rsplit("_", 1)[-1] for pool in pools_module.DEFAULT_CHARACTER_POOL_TYPES}
        self.fail_pool = fail_pool
        self.calls: list[tuple[str, str]] = []

    async def get_u8_token(self, token, binding_uid):
        return "u8"

    async def character_pool_names(self, token, server_id):
        return dict(self.names)

    async def weapon_pools(self, token, server_id):
        return [("weapon-1", "武器池")]

    async def character_records(self, role, token, pool_type, *, seq_id="", pool_name=""):
        self.calls.append((pool_type, seq_id))
        if pool_type == self.fail_pool:
            raise client_module.EndfieldAPIError("同步角色抽卡", "404", "不支持")
        records = () if seq_id else (
            GachaRecord(role.role_id, role.server_id, pool_type, pool_name, pool_type, f"{pool_type}-2", 20, "c2", "六星", 6, "角色", pool_version=2),
            GachaRecord(role.role_id, role.server_id, pool_type, pool_name, pool_type, f"{pool_type}-1", 10, "c1", "五星", 5, "角色", pool_version=2),
        )
        return client_module.GachaPage(records, False, records[-1].seq_id if records else "")

    async def weapon_records(self, role, token, pool_id="", *, seq_id="", pool_name=""):
        record = GachaRecord(role.role_id, role.server_id, "weapon-1", "武器池", "weapon", "w-1", 15, "w", "武器", 5, "武器")
        return client_module.GachaPage((record,), False, record.seq_id)


class SyncTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_sync_builds_five_character_streams_and_persists_pool_version(self):
        fake = _FakeClient()
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)
        result = await service.sync(self.role, full=True)
        self.assertEqual(result.inserted, 11)
        self.assertEqual({pool_type for pool_type, _ in fake.calls}, set(pools_module.DEFAULT_CHARACTER_POOL_TYPES))
        keys = {state.stream_key for state in self.store.list_sync_states(self.role)}
        self.assertIn("char:E_CharacterGachaPoolType_Rerun", keys)
        rerun = [item for item in self.store.list_gacha_records(self.role, limit=100) if item.pool_type == RERUN]
        self.assertEqual({item.pool_version for item in rerun}, {2})
        self.assertEqual(result.streams[0].label, "Special")

    async def test_sync_adds_api_enum_and_ignores_non_enum_keys(self):
        fake = _FakeClient(names={"E_CharacterGachaPoolType_Foo": "新池", "Bogus": "x"})
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)
        await service.sync(self.role, full=True)
        called = {pool_type for pool_type, _ in fake.calls}
        self.assertIn("E_CharacterGachaPoolType_Foo", called)
        self.assertNotIn("Bogus", called)
        self.assertEqual(len(called), 6)

    async def test_skport_optional_rerun_failure_is_skipped(self):
        credential = client_module.encode_account_credential("global-token", client_module.ACCOUNT_PROVIDER_SKPORT)
        global_role = self.store.bind_roles(
            "global-qq", credential,
            [store_module.RoleCandidate("global-bind", "global-role", "2", "亚服", "Asia")], self.cipher,
        )[0]
        fake = _FakeClient(names={}, fail_pool=RERUN)
        service = gacha_module.EndfieldGachaService(self.store, fake, self.cipher)
        result = await service.sync(global_role, full=True)
        self.assertEqual(result.failed, ())
        skipped = [item for item in result.streams if item.skipped]
        self.assertEqual([item.stream_key for item in skipped], ["char:E_CharacterGachaPoolType_Rerun"])
        self.assertTrue(service.analysis(global_role).complete)
        self.assertEqual(len({pool_type for pool_type, _ in fake.calls}), 5)

        failing = _FakeClient(names={}, fail_pool="E_CharacterGachaPoolType_Special")
        service = gacha_module.EndfieldGachaService(self.store, failing, self.cipher)
        result = await service.sync(global_role, full=True)
        self.assertEqual([item.stream_key for item in result.failed], ["char:E_CharacterGachaPoolType_Special"])

    def test_default_pool_types_are_shared_with_client(self):
        self.assertEqual(client_module.CHARACTER_POOL_TYPES, pools_module.DEFAULT_CHARACTER_POOL_TYPES)
        self.assertEqual(client_module.CHARACTER_POOL_TYPES[-1], RERUN)


class StoreMigrationTests(unittest.TestCase):
    def test_legacy_database_gains_pool_version_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endfield.db"
            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE gacha_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role_id TEXT NOT NULL, server_id TEXT NOT NULL, pool_id TEXT NOT NULL,
                    pool_name TEXT NOT NULL DEFAULT '', pool_type TEXT NOT NULL DEFAULT '',
                    seq_id TEXT NOT NULL, gacha_ts INTEGER NOT NULL DEFAULT 0,
                    item_id TEXT NOT NULL DEFAULT '', item_name TEXT NOT NULL DEFAULT '',
                    rarity INTEGER NOT NULL DEFAULT 0, item_type TEXT NOT NULL,
                    weapon_type TEXT NOT NULL DEFAULT '', is_new INTEGER NOT NULL DEFAULT 0,
                    is_free INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                    UNIQUE(role_id, server_id, pool_id, seq_id)
                );
                INSERT INTO gacha_records(role_id, server_id, pool_id, pool_name, pool_type, seq_id, gacha_ts,
                    item_id, item_name, rarity, item_type, created_at)
                VALUES('role', 'server', 'rerun_wpn_x', '点绘申领', 'weapon', '1', 10, 'w', '武器', 4, '武器', 1);
                """
            )
            legacy.commit()
            legacy.close()
            store = store_module.EndfieldStore(path)
            try:
                columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(gacha_records)")}
                self.assertIn("pool_version", columns)
                role = _role()
                old = store.list_gacha_records(role, limit=10)[0]
                self.assertEqual(old.pool_version, 0)
                updated = GachaRecord("role", "server", "rerun_wpn_x", "点绘申领", "weapon", "1", 10, "w", "武器", 4, "武器", pool_version=2)
                self.assertEqual(store.insert_gacha_records([updated]), 0)
                self.assertEqual(store.list_gacha_records(role, limit=10)[0].pool_version, 2)
                self.assertEqual(store.count_gacha_records(role), 1)
            finally:
                store.close()

    def test_seq_collision_probe_logs_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_module.EndfieldStore(Path(directory) / "endfield.db")
            try:
                first = GachaRecord("role", "server", "rerun_chr_x", "绚丽异彩", RERUN, "7", 100, "a", "甲", 4, "角色", pool_version=1)
                store.insert_gacha_records([first])
                same_day = GachaRecord("role", "server", "rerun_chr_x", "绚丽异彩", RERUN, "7", 100 + 3600, "a", "甲", 4, "角色", pool_version=1)
                far_apart = GachaRecord("role", "server", "rerun_chr_x", "绚丽异彩", RERUN, "7", 100 + 10 * 86_400, "b", "乙", 4, "角色", pool_version=2)
                with mock.patch.object(store_module, "logger") as logger:
                    store.insert_gacha_records([same_day])
                    self.assertEqual(logger.warning.call_count, 0)
                    store.insert_gacha_records([far_apart])
                    self.assertEqual(logger.warning.call_count, 1)
                self.assertEqual(store.count_gacha_records(_role()), 1)
            finally:
                store.close()


class PoolRuleTests(unittest.TestCase):
    def test_classify_pool_rule_caches_kind_and_series(self):
        rule = GachaPoolRule(
            "rerun_chr_yvonne", ("chr_0017_yvonne",), 0, "绚丽异彩", "char",
            type_code=4, table="char", pool_version=1,
        )
        classified = assets_module.classify_pool_rule(rule)
        self.assertEqual((classified.kind_key, classified.series_key), ("rerun", "rerun:chr_0017_yvonne"))
        weapon = assets_module.classify_pool_rule(GachaPoolRule("weaponbox_constant_1", ("w",), 0, "常驻申领", "weapon"))
        self.assertEqual((weapon.kind_key, weapon.table), ("weapon_constant", "weapon"))
        positional = GachaPoolRule("p", ("x",), 0)
        self.assertEqual((positional.type_code, positional.kind_key), (-1, ""))

    def test_extract_gacha_pool_rules_marks_rerun_weapon_table(self):
        rule = assets_module.extract_gacha_pool_rules({"poolId": "rerun_wpn_yvonne", "upItemIds": ["wpn_pistol_0010"]})["rerun_wpn_yvonne"]
        self.assertEqual((rule.pool_kind, rule.table), ("weapon", "weapon"))


if __name__ == "__main__":
    unittest.main()
