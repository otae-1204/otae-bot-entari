from __future__ import annotations

import hashlib
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.endfield.medals.store import (
    MedalSnapshotStore,
    _dict_to_snapshot,
    _snapshot_to_dict,
)
from plugins.endfield.providers.akedata import game_version_label, pick_previous_game_version
from plugins.endfield.catalog.models import (
    MedalBaselineView,
    MedalDiffView,
    MedalItemView,
    MedalSnapshotView,
)
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.providers.registry import source_order

endfield_service_module = importlib.import_module("plugins.endfield.catalog.service")


def _make_medal(medal_id: str, *, name: str = "", max_level: int = 1, **kw) -> MedalItemView:
    return MedalItemView(medal_id=medal_id, name=name or medal_id, max_level=max_level, **kw)


def _make_snapshot(ids: list[str], *, version: str = "v") -> MedalSnapshotView:
    medals = [_make_medal(i, max_level=1 if i.endswith("1") else 3) for i in ids]
    snap = MedalSnapshotView(medals=medals, version=version, total_count=len(medals))
    snap.level_counts = {1: sum(1 for m in medals if m.max_level == 1),
                         3: sum(1 for m in medals if m.max_level == 3)}
    snap.category_counts = {"地区奖章": len(medals)}
    snap.platable_count = 0
    snap.upgradable_count = sum(1 for m in medals if m.max_level > 1)
    return snap


class MedalStoreRoundTripTest(unittest.TestCase):
    def test_level_counts_int_keys_survive_json(self):
        # JSON 会把 int 键转成 str；转换层必须还原
        snap = MedalSnapshotView(level_counts={1: 24, 2: 58, 3: 58}, total_count=140)
        d = _snapshot_to_dict(snap)
        self.assertEqual(d["level_counts"], {"1": 24, "2": 58, "3": 58})
        back = _dict_to_snapshot(d)
        self.assertEqual(back.level_counts, {1: 24, 2: 58, 3: 58})

    def test_field_filtering_ignores_unknown_keys(self):
        raw = {"medals": [{"medal_id": "a", "name": "A", "future_field": "x"}],
               "version": "v", "total_count": 1, "level_counts": {"2": 1}}
        snap = _dict_to_snapshot(raw)
        self.assertEqual(len(snap.medals), 1)
        self.assertEqual(snap.medals[0].medal_id, "a")
        self.assertEqual(snap.medals[0].name, "A")
        self.assertEqual(snap.level_counts, {2: 1})

    def test_empty_and_partial_dict(self):
        snap = _dict_to_snapshot({})
        self.assertEqual(snap.medals, [])
        self.assertEqual(snap.version, "")
        self.assertEqual(snap.level_counts, {})


class MedalSnapshotStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_and_baseline_stored_independently(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "snap.json")
            store = MedalSnapshotStore(path)
            self.assertIsNone(store.load_current_view())
            self.assertIsNone(store.load_baseline_view())

            # current 不再滚动 previous：两次 replace_current 只保留最后一次
            await store.replace_current(_make_snapshot(["a", "b"], version="1.4"))
            await store.replace_current(_make_snapshot(["a", "b", "c"], version="1.4"))
            cur = store.load_current_view()
            self.assertEqual(cur.version, "1.4")
            self.assertEqual({m.medal_id for m in cur.medals}, {"a", "b", "c"})

            # baseline 独立存取（akedata 上一版本 achv_id 集合），不影响 current
            await store.replace_baseline(MedalBaselineView(version="1.3", ids=["a", "b"]))
            bl = store.load_baseline_view()
            self.assertIsNotNone(bl)
            self.assertEqual(bl.version, "1.3")
            self.assertEqual(set(bl.ids), {"a", "b"})
            self.assertEqual(cur.total_count, 3)

            # baseline 可清空
            await store.replace_baseline(None)
            self.assertIsNone(store.load_baseline_view())

    async def test_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "snap.json")
            store = MedalSnapshotStore(path)
            await store.replace_current(_make_snapshot(["x"], version="restart-test"))
            await store.replace_baseline(MedalBaselineView(version="1.3", ids=["x"]))
            reopened = MedalSnapshotStore(path)  # 模拟进程重启重新 _load
            cur = reopened.load_current_view()
            self.assertEqual(cur.version, "restart-test")
            self.assertEqual([m.medal_id for m in cur.medals], ["x"])
            bl = reopened.load_baseline_view()
            self.assertEqual(bl.version, "1.3")
            self.assertEqual(set(bl.ids), {"x"})

    async def test_current_and_baseline_can_be_persisted_together(self):
        with tempfile.TemporaryDirectory() as d:
            store = MedalSnapshotStore(str(Path(d) / "snap.json"))
            await store.replace_current_and_baseline(
                _make_snapshot(["new"], version="1.4"),
                MedalBaselineView(version="1.3", ids=["old"]),
            )
            reopened = MedalSnapshotStore(str(Path(d) / "snap.json"))
            self.assertEqual(reopened.load_current_view().version, "1.4")
            self.assertEqual(reopened.load_baseline_view().version, "1.3")


class MedalDiffTest(unittest.TestCase):
    def test_diff_finds_only_new_ids(self):
        service = EndfieldService.__new__(EndfieldService)  # 不触发 __init__ 的依赖
        current = _make_snapshot(["a", "b", "c", "d"], version="1.4")
        baseline = MedalBaselineView(version="1.3", ids=["a", "b"])
        diff: MedalDiffView = service.build_medal_diff(current, baseline)
        self.assertEqual({m.medal_id for m in diff.new_medals}, {"c", "d"})
        self.assertEqual(diff.previous_version, "1.3")

    def test_diff_against_none_is_empty(self):
        # 无更早版本时 new_medals 为空（只展示总数统计）
        service = EndfieldService.__new__(EndfieldService)
        current = _make_snapshot(["a", "b"], version="1.4")
        diff = service.build_medal_diff(current, None)
        self.assertEqual(diff.new_medals, [])
        self.assertEqual(diff.previous_version, "")

    def test_diff_against_baseline_covering_all_is_empty(self):
        # baseline 含 current 全部 id（自比/本版本无新增）
        service = EndfieldService.__new__(EndfieldService)
        current = _make_snapshot(["a", "b", "c"], version="1.4")
        diff = service.build_medal_diff(
            current, MedalBaselineView(version="1.3", ids=["a", "b", "c"])
        )
        self.assertEqual(diff.new_medals, [])


class MedalMissingTest(unittest.TestCase):
    def _snapshot(self, medals):
        return MedalSnapshotView(medals=medals, total_count=len(medals))

    def test_cross_reference_categories(self):
        service = EndfieldService.__new__(EndfieldService)
        # FZ 快照条目用 achv_ id（与游戏客户端一致）
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_a", name="A", max_level=1),                       # 已集齐
            MedalItemView(medal_id="achv_b", name="B", max_level=3, can_be_upgraded=True),  # 未升满
            MedalItemView(medal_id="achv_c", name="C", max_level=1, can_be_plated=True),    # 未镀层
            MedalItemView(medal_id="achv_d", name="D"),                                     # 未获得
        ])
        # 森空岛 achievementData.id == md5(achv_id)（2026-07-28 实测 115/115）。
        # skland 名故意写成不同名字，验证关联走 md5-id 而非 name。
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {"id": hashlib.md5(b"achv_a").hexdigest(), "name": "森空岛·A"}, "level": 1, "isPlated": True},
            {"achievementData": {"id": hashlib.md5(b"achv_b").hexdigest(), "name": "森空岛·B"}, "level": 1, "isPlated": False},
            {"achievementData": {"id": hashlib.md5(b"achv_c").hexdigest(), "name": "森空岛·C"}, "level": 1, "isPlated": False},
        ]}}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="测试", uid="***1234", server_name="测试服"
        )
        self.assertEqual([m.medal_id for m in view.not_obtained], ["achv_d"])
        self.assertEqual([m.medal_id for m in view.not_maxed], ["achv_b"])
        self.assertEqual([m.medal_id for m in view.not_plated], ["achv_c"])
        self.assertEqual(view.owned_count, 3)
        self.assertFalse(view.truncated)
        # 等级分布按账号已拥有奖章的「当前档位」(real_level = skland level + initLevel - 1) 统计。
        # 本例三枚均 level=1、无 initLevel → real=1；d 未获得不计。
        self.assertEqual(view.level_counts, {1: 3})

    def test_init_level_offset_for_2_to_3_medal(self):
        """森空岛 level 对 initLevel>1 的章有偏移：实际档位 = skland level + initLevel - 1。

        复现「谷地调查者奖章」（全游戏唯一 2→3 升级章，initLevel=2）：AKEData max_level=3 正确；
        但森空岛把银(实际2)记为 level=1、金(实际3)记为 level=2。玩家拿到金色(level=2)时
        real_level=2+2-1=3=max → 已升满，不该进未升满，且按 3 档（金）计数。
        对照「潜能解放奖章」（initLevel=1、1→2→3）：level=2 → real=2<3 → 真未升满、按 2 档计数。
        """
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_g", name="G", max_level=3, can_be_upgraded=True),  # 谷地调查者型
            MedalItemView(medal_id="achv_h", name="H", max_level=3, can_be_upgraded=True),  # 潜能解放型
        ])
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {"id": hashlib.md5(b"achv_g").hexdigest(), "name": "G", "initLevel": 2},
                             "level": 2, "isPlated": False},
            {"achievementData": {"id": hashlib.md5(b"achv_h").hexdigest(), "name": "H", "initLevel": 1},
                             "level": 2, "isPlated": False},
        ]}}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        # G: real=2+2-1=3=max → 已升满；只有 H（real=2<3）未升满
        self.assertEqual([m.medal_id for m in view.not_maxed], ["achv_h"])
        # 当前档位：G→3 档（金）、H→2 档（银）
        self.assertEqual(view.level_counts, {3: 1, 2: 1})

    def test_md5_id_resolves_name_collision(self):
        """武陵·Ⅳ/·Ⅴ 命名撞名：森空岛两枚同名(hex 不同)，md5-id 能精确归属。"""
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_wuling_4", name="武陵调度专家奖章·Ⅳ", max_level=1),
            MedalItemView(medal_id="achv_wuling_5", name="武陵调度专家奖章·Ⅴ", max_level=1),
        ])
        # 玩家拥有 _4 和 _5，但森空岛把两枚都标成「·Ⅳ」（命名滞后）
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {"id": hashlib.md5(b"achv_wuling_4").hexdigest(), "name": "武陵调度专家奖章·Ⅳ"}, "level": 1, "isPlated": False},
            {"achievementData": {"id": hashlib.md5(b"achv_wuling_5").hexdigest(), "name": "武陵调度专家奖章·Ⅳ"}, "level": 1, "isPlated": False},
        ]}}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="测试", uid="***1", server_name="测试服"
        )
        # 两枚都应判为已获得（按 md5-id），未获得为空——按 name 会漏判一枚
        self.assertEqual(view.not_obtained, [])
        self.assertEqual(view.owned_count, 2)

    def test_name_fallback_when_medal_lacks_achv_id(self):
        """FZ 条目无 achv_ id 时，回退按规范化 name 关联。"""
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="无id条目", name="某章", max_level=1),  # medal_id 非 achv_
        ])
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {"id": "deadbeef", "name": "某章"}, "level": 1, "isPlated": False},
        ]}}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        self.assertEqual(view.not_obtained, [])
        self.assertEqual(view.owned_count, 1)

    def test_truncation_when_too_many(self):
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([MedalItemView(medal_id=f"m{i}", name=f"M{i}") for i in range(40)])
        view = service.build_medal_missing_view(
            {}, snapshot, nickname="x", uid="y", server_name="z", limit=30
        )
        self.assertTrue(view.truncated)
        self.assertLessEqual(len(view.not_obtained), 10)  # limit // 3


class AkedataVersionSelectTest(unittest.TestCase):
    """版本对比的上一版本选择：major.minor 粒度，跳过同版本 revision。"""

    def test_game_version_label(self):
        self.assertEqual(game_version_label("1.4.4@8764515-7"), "1.4")
        self.assertEqual(game_version_label("1.3.3@8190425-29"), "1.3")
        self.assertEqual(game_version_label("1.0.14@5793042-32"), "1.0")

    def _manifest(self, ids):
        return {"latest": ids[0], "versions": [{"id": i, "tableCfgPath": f"p/{i}"} for i in ids]}

    def test_pick_previous_skips_same_game_version_revisions(self):
        # 1.4.4 下三个 revision，上一游戏版本应是 1.3.3
        m = self._manifest([
            "1.4.4@8764515-7", "1.4.4@8692565-6", "1.4.4@8618533-5",
            "1.3.3@8190425-29", "1.2.5@7215718-17",
        ])
        prev = pick_previous_game_version(m)
        self.assertIsNotNone(prev)
        self.assertEqual(prev["id"], "1.3.3@8190425-29")

    def test_pick_previous_none_when_only_one_game_version(self):
        m = self._manifest(["1.4.4@8764515-7", "1.4.4@8692565-6"])
        self.assertIsNone(pick_previous_game_version(m))

    def test_medal_source_is_akedata(self):
        self.assertEqual(source_order("medal"), ("akedata",))


class AkedataFetchValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tables_are_rejected_before_persisting(self):
        service = EndfieldService.__new__(EndfieldService)
        with patch(
            "plugins.endfield.providers.akedata.fetch_akedata_medal_tables",
            new=AsyncMock(return_value=({}, {}, {}, "1.4.4@test")),
        ):
            with self.assertRaisesRegex(ValueError, "AchievementTable 为空"):
                await service.fetch_medal_snapshot_akedata()

    async def test_empty_historical_table_is_rejected(self):
        service = EndfieldService.__new__(EndfieldService)
        with patch.object(
            endfield_service_module,
            "fetch_akedata_manifest",
            new=AsyncMock(return_value={
                "latest": "1.4.4@test",
                "versions": [
                    {"id": "1.4.4@test", "tableCfgPath": "current"},
                    {"id": "1.3.3@test", "tableCfgPath": "previous"},
                ],
            }),
        ), patch.object(
            endfield_service_module,
            "fetch_akedata_achievement_table",
            new=AsyncMock(return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "历史 AchievementTable 为空"):
                await service.fetch_akedata_baseline()


class AkedataMedalSnapshotTest(unittest.TestCase):
    """AKEData（TableCfg）源快照构建：achv_id 当主键、名字按 text-id 解析、max 来自 levelInfos、
    图标路径规则、分类/组解析。"""

    def _tables(self):
        i18n = {
            "1001": "苏醒测试章",
            "1002": "谷地测试章",
            "2001": "测试分类",
            "2002": "测试组",
        }
        achievement = {
            "achv_test_single": {
                "name": {"id": "1001", "text": ""},
                "desc": {"id": "0", "text": ""},
                "canBeUpgraded": False,
                "canBePlated": False,
                "initLevel": 3,
                "groupId": "achv_group_test",
                "order": 1,
                "levelInfos": {"3": {"achieveLevel": 3}},
            },
            "achv_test_multi": {
                "name": {"id": "1002", "text": ""},
                "canBeUpgraded": True,
                "canBePlated": True,
                "initLevel": 2,
                "groupId": "achv_group_test",
                "order": 2,
                "levelInfos": {"2": {"achieveLevel": 2}, "3": {"achieveLevel": 3}},
            },
        }
        type_table = {
            "achv_type_test": {
                "categoryName": {"id": "2001", "text": ""},
                "categoryPriority": 1,
                "achievementGroupData": [
                    {"groupId": "achv_group_test", "groupName": {"id": "2002", "text": ""}}
                ],
            }
        }
        return achievement, type_table, i18n

    def test_medal_i18n_accepts_object_shaped_translation_rows(self):
        self.assertEqual(
            endfield_service_module._i18n_text(
                {"1001": {"zh": "中文奖章", "en": "English Medal"}},
                {"id": "1001"},
            ),
            "中文奖章",
        )

    def test_build_akedata_snapshot_fields(self):
        from plugins.endfield.providers.akedata import AKEDATA_ICON_BASE
        from plugins.endfield.catalog.service import build_akedata_medal_snapshot

        achievement, type_table, i18n = self._tables()
        snap = build_akedata_medal_snapshot(
            achievement, type_table, i18n, fetched_at=1, version_label="vtest"
        )
        by_id = {m.medal_id: m for m in snap.medals}

        single = by_id["achv_test_single"]
        self.assertEqual(single.name, "苏醒测试章")
        self.assertEqual(single.max_level, 3)  # levelInfos 仅有档 3
        self.assertFalse(single.can_be_upgraded)
        self.assertEqual(single.category_name, "测试分类")
        self.assertEqual(single.group_name, "测试组")
        self.assertEqual(single.icon_url, f"{AKEDATA_ICON_BASE}/achv_test_single_lv03.png")

        multi = by_id["achv_test_multi"]
        self.assertEqual(multi.max_level, 3)  # levelInfos 档 2、3 → max 3
        self.assertTrue(multi.can_be_upgraded)
        self.assertTrue(multi.can_be_plated)
        self.assertEqual(multi.icon_url, f"{AKEDATA_ICON_BASE}/achv_test_multi_lv03.png")

        self.assertEqual(snap.source, "akedata")
        self.assertEqual(snap.total_count, 2)
        self.assertEqual(snap.level_counts, {3: 2})
        self.assertEqual(snap.upgradable_count, 1)
        self.assertEqual(snap.platable_count, 1)

    def test_akedata_snapshot_plugs_into_md5_association(self):
        """AKEData 快照（achv_id 主键）与森空岛 hex 经 md5 关联，缺章判定正确。"""
        from plugins.endfield.catalog.service import build_akedata_medal_snapshot

        achievement, type_table, i18n = self._tables()
        snap = build_akedata_medal_snapshot(achievement, type_table, i18n)
        # 玩家拥有 achv_test_single（hex=md5），未拥有 achv_test_multi
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {"id": hashlib.md5(b"achv_test_single").hexdigest(),
                                 "name": "别的名字也行"}, "level": 1, "isPlated": False},
        ]}}}}
        service = EndfieldService.__new__(EndfieldService)
        view = service.build_medal_missing_view(
            raw_progress, snap, nickname="t", uid="u", server_name="s"
        )
        self.assertEqual([m.medal_id for m in view.not_obtained], ["achv_test_multi"])
        self.assertEqual(view.owned_count, 1)


if __name__ == "__main__":
    unittest.main()
