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

    def test_medal_wall_orders_by_display_slot_and_picks_level_icon(self):
        """奖章墙：按 ``achieve.display`` 槽位序号排序，并按镀层/实际档位选图标。

        display 是「槽位 → hex」的映射而不是数组，故不能按 achieveMedals 的数组顺序渲染；
        initLevel>1 的章同样要按 real_level = level + initLevel - 1 选图标（否则会取错档）。
        """
        service = EndfieldService.__new__(EndfieldService)
        hex_a = hashlib.md5(b"achv_a").hexdigest()
        hex_b = hashlib.md5(b"achv_b").hexdigest()
        hex_c = hashlib.md5(b"achv_c").hexdigest()
        # 数组顺序刻意与槽位顺序相反，验证渲染顺序取自 display 而非数组。
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {
                    "id": hex_c, "name": "C", "initLevel": 2,
                    "initIcon": "i_c1", "reforge2Icon": "i_c2", "reforge3Icon": "i_c3",
                }, "level": 1, "isPlated": False},
                {"achievementData": {
                    "id": hex_b, "name": "B", "initLevel": 1,
                    "initIcon": "i_b1", "reforge2Icon": "i_b2",
                }, "level": 2, "isPlated": False},
                {"achievementData": {
                    "id": hex_a, "name": "A", "initLevel": 1,
                    "initIcon": "i_a1", "platedIcon": "i_a_plated",
                }, "level": 1, "isPlated": True},
            ],
            "display": {"3": hex_c, "1": hex_a, "2": hex_b},
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, self._snapshot([]), nickname="t", uid="u", server_name="s"
        )
        self.assertEqual([m.slot for m in view.wall], [1, 2, 3])
        self.assertEqual([m.name for m in view.wall], ["A", "B", "C"])
        # A 已镀层 → 镀层图标压过档位图标
        self.assertEqual(view.wall[0].icon_url, "i_a_plated")
        self.assertTrue(view.wall[0].plated)
        # B initLevel=1、level=2 → real=2 → 二档图标
        self.assertEqual(view.wall[1].icon_url, "i_b2")
        self.assertEqual(view.wall[1].level, 2)
        # C initLevel=2、level=1 → real=1+2-1=2 → 二档图标（不是初始档）
        self.assertEqual(view.wall[2].icon_url, "i_c2")
        self.assertEqual(view.wall[2].level, 2)

    def test_medal_wall_degrades_without_display(self):
        """没有 display / 槽位缺图 / 槽位指向未拥有的 hex 时都不抛异常，只少格。"""
        service = EndfieldService.__new__(EndfieldService)
        hex_a = hashlib.md5(b"achv_a").hexdigest()
        snapshot = self._snapshot([])

        for achieve in (
            {"achieveMedals": [{"achievementData": {"id": hex_a, "name": "A"}, "level": 1}]},
            {"achieveMedals": [], "display": None},
            {"achieveMedals": [], "display": {}},
            {"achieveMedals": [], "display": {"1": hex_a}},          # 指向未拥有的章
        ):
            view = service.build_medal_missing_view(
                {"data": {"detail": {"achieve": achieve}}},
                snapshot, nickname="t", uid="u", server_name="s",
            )
            self.assertEqual(view.wall, [], achieve)

        # 有章但两源都没图：仍占一格，图标留空交给渲染层标「图标暂缺」。
        view = service.build_medal_missing_view(
            {"data": {"detail": {"achieve": {
                "achieveMedals": [{"achievementData": {"id": hex_a}}], "display": {"1": hex_a},
            }}}},
            snapshot, nickname="t", uid="u", server_name="s",
        )
        self.assertEqual(len(view.wall), 1)
        self.assertEqual(view.wall[0].slot, 1)
        self.assertEqual(view.wall[0].icon_url, "")
        self.assertEqual(view.wall[0].fallback_icon_url, "")

    def test_medal_wall_accepts_list_display(self):
        """``display`` 若以数组形式返回，下标（1 起）即槽位序号。"""
        service = EndfieldService.__new__(EndfieldService)
        hex_a = hashlib.md5(b"achv_a").hexdigest()
        hex_b = hashlib.md5(b"achv_b").hexdigest()
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {"id": hex_a, "name": "A"}, "level": 1},
                {"achievementData": {"id": hex_b, "name": "B"}, "level": 1},
            ],
            "display": [hex_b, hex_a],
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, self._snapshot([]), nickname="t", uid="u", server_name="s"
        )
        self.assertEqual([(m.slot, m.name) for m in view.wall], [(1, "B"), (2, "A")])

    def test_medal_wall_prefers_canonical_slot_key(self):
        """``"01"`` 与 ``"1"`` 指向不同的章时，规范写法 ``"1"`` 占槽，与 hex 字典序无关。"""
        from plugins.endfield.catalog.models import MEDAL_WALL_MAX_SLOTS
        from plugins.endfield.catalog.views.medals import parse_player_medal_wall

        # 让非规范键的 hex 字典序更小，确认结果不是靠排序碰巧对的。
        hex_low, hex_high = "0" * 32, "f" * 32
        raw = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {"id": hex_low, "name": "非规范"}, "level": 1},
                {"achievementData": {"id": hex_high, "name": "规范"}, "level": 1},
            ],
            "display": {"01": hex_low, "1": hex_high},
        }}}}
        wall = parse_player_medal_wall(raw)
        self.assertEqual([(m.slot, m.name) for m in wall], [(1, "规范")])
        self.assertEqual(MEDAL_WALL_MAX_SLOTS, 10)

    def test_not_plated_next_icon_comes_from_skland_plated_icon(self):
        """回归：未镀层双卡右侧的「镀层后」图标取森空岛 ``platedIcon``，与当前是否已镀层无关。"""
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_p", name="P", max_level=1, can_be_plated=True),
            MedalItemView(medal_id="achv_q", name="Q", max_level=1, can_be_plated=True),
        ])
        raw_progress = {"data": {"detail": {"achieve": {"achieveMedals": [
            {"achievementData": {
                "id": hashlib.md5(b"achv_p").hexdigest(), "name": "P",
                "initIcon": "https://bbs.hycdn.cn/p.png",
                "platedIcon": "https://bbs.hycdn.cn/p_plated.png",
            }, "level": 1, "isPlated": False},
            {"achievementData": {
                "id": hashlib.md5(b"achv_q").hexdigest(), "name": "Q",
                "platedIcon": "https://bbs.hycdn.cn/q_plated.png",
            }, "level": 1, "isPlated": True},
        ]}}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        self.assertEqual([m.medal_id for m in view.not_plated], ["achv_p"])
        self.assertEqual(
            [m.next_icon_url for m in view.not_plated], ["https://bbs.hycdn.cn/p_plated.png"]
        )

    def test_medal_wall_absent_when_achieve_missing(self):
        """整个 achieve 段缺失（接口字段变动）时奖章墙为空，不影响缺章判定。"""
        service = EndfieldService.__new__(EndfieldService)
        view = service.build_medal_missing_view(
            {"data": {"detail": {}}}, self._snapshot([]),
            nickname="t", uid="u", server_name="s",
        )
        self.assertEqual(view.wall, [])

    def test_medal_wall_resolves_back_to_akedata_records(self):
        """奖章墙能按 id 还原出 AKEData 奖章记录（后续「展示奖章统计」依赖这条链路）。

        森空岛只回 hex id，hex == md5(achv_id)，故无需任何图像识别即可关联到奖章元数据。
        """
        from plugins.endfield.catalog.views.medals import (
            build_medal_id_index,
            resolve_medal_wall,
        )

        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_x", name="X 奖章", category_name="章节奖章", max_level=3),
            MedalItemView(medal_id="achv_y", name="Y 奖章", category_name="技艺奖章", max_level=1),
            MedalItemView(medal_id="achv_z", name="Z 奖章", max_level=1),
        ])
        hex_x = hashlib.md5(b"achv_x").hexdigest()
        hex_y = hashlib.md5(b"achv_y").hexdigest()
        # 快照里没有的章（活动已下架/新章）：应配出 None 而不是报错
        unknown = hashlib.md5(b"achv_not_in_snapshot").hexdigest()
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {"id": hex_x, "name": "X"}, "level": 1, "isPlated": True},
                {"achievementData": {"id": hex_y, "name": "Y"}, "level": 1, "isPlated": False},
                {"achievementData": {"id": unknown, "name": "?"}, "level": 1, "isPlated": False},
            ],
            "display": {"1": hex_x, "2": hex_y, "3": unknown},
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        resolved = resolve_medal_wall(view.wall, snapshot.medals)
        self.assertEqual(
            [(wall.slot, medal.medal_id if medal else None) for wall, medal in resolved],
            [(1, "achv_x"), (2, "achv_y"), (3, None)],
        )
        # 顺序与 wall 一致，便于按展示位统计
        self.assertEqual([wall.slot for wall, _ in resolved], [1, 2, 3])
        named = {medal.name for _, medal in resolved if medal}
        self.assertEqual(named, {"X 奖章", "Y 奖章"})
        # 索引忽略非 achv_ 前缀，避免把 FZ 兜底条目的 id 当 achv_id 哈希
        self.assertEqual(len(build_medal_id_index(snapshot.medals)), 3)
        self.assertEqual(len(build_medal_id_index([MedalItemView(medal_id="nomad_id", name="兜底")])), 0)

    def test_wall_icon_prefers_akedata_highres(self):
        """奖章墙图标优先 AKEData 高清图（400px），而不是森空岛回的 126px 小图。

        森空岛的 achievementData 自带 initIcon（126×126），但页头 2x 出图会发虚；
        AKEData 的 medaliconbig 是 400×400，且档位按 max(level, init_level) 兜底单档章。
        """
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_a", name="A", max_level=1, init_level=1),
            MedalItemView(medal_id="achv_b", name="B", max_level=3, init_level=3),
        ])
        hex_a = hashlib.md5(b"achv_a").hexdigest()
        hex_b = hashlib.md5(b"achv_b").hexdigest()
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {
                    "id": hex_a, "name": "A", "initLevel": 1, "initIcon": "https://bbs.hycdn.cn/a.png",
                }, "level": 1, "isPlated": False},
                {"achievementData": {
                    "id": hex_b, "name": "B", "initLevel": 3, "initIcon": "https://bbs.hycdn.cn/b.png",
                }, "level": 1, "isPlated": False},
            ],
            "display": {"1": hex_a, "2": hex_b},
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        by_slot = {item.slot: item for item in view.wall}
        self.assertIn("achv_a_lv01.png", by_slot[1].icon_url)
        self.assertNotIn("hycdn.cn", by_slot[1].icon_url)
        # 档位兜底：森空岛 lv=1 但章是单档 initLevel=3 → 取 lv03（否则 404）
        self.assertIn("achv_b_lv03.png", by_slot[2].icon_url)

    def test_wall_icon_prefers_highres_plating_and_retains_skland_fallback(self):
        """镀层走最高档位的 _plating 原图，并保留官方小图用于下载失败时回退。"""
        service = EndfieldService.__new__(EndfieldService)
        snapshot = self._snapshot([
            MedalItemView(medal_id="achv_p", name="P", max_level=3, init_level=3, can_be_plated=True),
        ])
        hex_p = hashlib.md5(b"achv_p").hexdigest()
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {
                    "id": hex_p, "name": "P", "initLevel": 3,
                    "initIcon": "https://bbs.hycdn.cn/p.png",
                    "platedIcon": "https://bbs.hycdn.cn/p_plated.png",
                }, "level": 1, "isPlated": True},
            ],
            "display": {"1": hex_p},
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, snapshot, nickname="t", uid="u", server_name="s"
        )
        self.assertTrue(view.wall[0].plated)
        self.assertTrue(view.wall[0].icon_url.endswith("/achv_p_lv03_plating.png"))
        self.assertEqual(view.wall[0].fallback_icon_url, "https://bbs.hycdn.cn/p_plated.png")

    def test_plating_uses_max_tier_instead_of_skland_relative_level(self):
        from plugins.endfield.catalog.views.medals import _wall_icon_url

        for initial, maximum in ((2, 2), (3, 3), (1, 3)):
            with self.subTest(initial=initial, maximum=maximum):
                medal = MedalItemView(
                    medal_id="achv_p", name="P", init_level=initial,
                    max_level=maximum, can_be_plated=True,
                )
                url = _wall_icon_url({}, medal, level=1, plated=True)
                self.assertTrue(url.endswith(f"/achv_p_lv{maximum:02d}_plating.png"))

    def test_plated_unknown_medal_never_falls_back_to_unplated_art(self):
        from plugins.endfield.catalog.views.medals import _wall_icon_url

        self.assertEqual(_wall_icon_url({"initIcon": "plain"}, None, level=3, plated=True), "")
        medal = MedalItemView(medal_id="achv_unknown_plating", name="new")
        self.assertEqual(_wall_icon_url(
            {"platedIcon": "plated"}, medal, level=3, plated=True,
        ), "plated")

    def test_invalid_display_slots_do_not_displace_valid_slots(self):
        from plugins.endfield.catalog.views.medals import parse_player_medal_wall

        raw = {"data": {"detail": {"achieve": {
            "achieveMedals": [{"achievementData": {"id": "owned", "name": "A"}}],
            "display": {key: "owned" for key in ("bad", "-1", "0", "1", "01", "3", "10", "11")},
        }}}}
        self.assertEqual([m.slot for m in parse_player_medal_wall(raw)], [1, 3, 10])
        self.assertEqual([m.slot for m in parse_player_medal_wall(raw, limit=2)], [1, 3])
        self.assertEqual(parse_player_medal_wall(raw, limit=0), [])
        self.assertEqual(parse_player_medal_wall(raw, limit=-1), [])

    def test_wall_icon_skips_akedata_when_snapshot_lacks_medal(self):
        """快照里没有这枚章（活动已下架）时不能拼 AKEData 路径，直接回退森空岛。"""
        service = EndfieldService.__new__(EndfieldService)
        hex_x = hashlib.md5(b"achv_gone").hexdigest()
        raw_progress = {"data": {"detail": {"achieve": {
            "achieveMedals": [
                {"achievementData": {
                    "id": hex_x, "name": "X", "initLevel": 1,
                    "initIcon": "https://bbs.hycdn.cn/x.png",
                }, "level": 1, "isPlated": False},
            ],
            "display": {"1": hex_x},
        }}}}
        view = service.build_medal_missing_view(
            raw_progress, self._snapshot([]), nickname="t", uid="u", server_name="s"
        )
        self.assertEqual(view.wall[0].icon_url, "https://bbs.hycdn.cn/x.png")

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
