"""抽卡分析图 v3 前端：宽度、重构侧栏与两栏折叠、排序、保底格、记录行、按实测高度分页、渲染入口与回退。"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import os
import re
import sys
import types
import unittest
from dataclasses import replace
from io import BytesIO
from itertools import accumulate
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "endfield_gacha_layout_for_test"


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

store_module = _load(f"{PACKAGE}.account.store", "plugins/endfield/account/store.py")
gacha_module = _load(f"{PACKAGE}.gacha.service", "plugins/endfield/gacha/service.py")
cards_module = _load(f"{PACKAGE}.rendering.cards", "plugins/endfield/rendering/cards.py")
draw = _load(f"{PACKAGE}.gacha.draw", "plugins/endfield/gacha/draw.py")
models = sys.modules[f"{PACKAGE}.gacha.models"]

PoolAnalysis = models.PoolAnalysis
SixStarEvent = models.SixStarEvent
KeepsakeGift = models.KeepsakeGift
FreePullBatch = models.FreePullBatch
NextReward = models.NextReward
GachaRecord = store_module.GachaRecord
TS = 1_790_000_000
DAY = 86_400
# 三个池格的标题（不含卡池总数格）：kicker 用 <small>，标题是 .metric-title 里唯一的 <span>
SUMMARY_TITLE = r'<div class="metric-head"><div class="metric-title"><small>[^<]*</small><span>([^<]+)</span>'


def _role(nickname: str = "示例玩家"):
    return store_module.EndfieldRole(1, 1, "qq", "bind", "100000001234", "1", nickname, "示例服务器", True)


def _six(name: str, position: int, *, interval: int = 50, ts: int = TS, labels=(), up: str = ""):
    return SixStarEvent(name, "池", "角色", ts, item_id=name, interval=interval, pool_position=position,
                        pity_labels=tuple(labels), up_status=up)


def _pool(card_key: str, name: str, kind: str, *, item_type: str = "角色", current: bool = False,
          latest: int = TS, sixes: int = 0, series_key: str = "", **fields) -> PoolAnalysis:
    pool_id, _, version = card_key.partition("#")
    return PoolAnalysis(
        pool_id, name, item_type, fields.pop("total", 100), fields.pop("since", 5),
        latest_ts=latest, is_current=current, paid_total=fields.pop("paid", 100),
        six_stars=fields.pop("six_stars", tuple(_six(f"{name}六星{index}", 100 - index) for index in range(sixes))),
        card_key=card_key, pool_version=int(version or 0), kind_key=kind,
        series_key=series_key or f"pool:{pool_id}", **fields,
    )


def _view(pools, **fields):
    return gacha_module.GachaAnalysis(
        role=fields.pop("role", _role()), total=sum(pool.total for pool in pools), rarity_counts={6: 1},
        pools=tuple(pools), six_stars=(), intervals=(), average_interval=None, last_sync_at=TS,
        complete=True, errors=(), **fields,
    )


def _layout(view, **kwargs):
    columns = draw.build_gacha_columns(view, **kwargs)
    rows = {card.key: draw.render_pool_rows(card) for column in columns for section in column.sections
            for card in section.cards}
    return columns, rows


def _column(columns, key: str):
    return next(column for column in columns if column.spec.key == key)


def _other_section(columns):
    return next(section for column in columns for section in column.sections if section.spec.divider)


def _fake_measure(columns, rows, *, row=50.0, head=110.0, head_cont=60.0, base=20.0,
                  overhead_first=520.0, overhead_cont=260.0):
    pools = {
        key: draw.PoolMeasure(head, head_cont, base, tuple(accumulate([0.0] + [row] * len(items))))
        for key, items in rows.items()
    }
    heads = {(index, cont): 70.0 for index in range(len(columns)) for cont in (False, True)}
    return draw.GachaMeasure(pools, 6.0, 60.0, 60.0, 40.0, 50.0, 30.0, 50.0, heads, overhead_first, overhead_cont)


def _raw_measure(view, *, row=50.0):
    """MEASURE_JS 形状的合成结果（不启动浏览器）。"""
    columns, rows = _layout(view)
    full = {key: {"total": 20 + 110 + row * len(items) + 6 * max(len(items) - 1, 0), "head": 110.0,
                  "rows": [row] * len(items)} for key, items in rows.items()}
    count = len(columns)
    return {
        "first": {"card": 3000.0, "cols": [2480.0, 2300.0, 2200.0][:count], "heads": [70.0] * count},
        "cont": {"card": 500.0, "cols": [240.0] * count, "heads": [72.0] * count},
        "blocks": {"divider": 60.0, "divider_cont": 60.0, "note": 40.0, "empty": 50.0, "hint": 30.0, "done": 50.0},
        "full": full, "cont_heads": {key: 60.0 for key in rows}, "row_gap": 6.0,
    }


def _heavy_view(*, pools_per_column: int = 14, sixes: int = 9, rerun: bool = True):
    pools = [_pool("special_cur#0", "特许当期", "special", current=True, sixes=sixes, latest=TS + 99 * DAY,
                   small_pity_limit=80, small_pity_progress=10)]
    pools += [_pool(f"special_{index}#0", f"特许历史{index}", "special", sixes=sixes, latest=TS + index * DAY)
              for index in range(pools_per_column)]
    if rerun:
        pools += [_pool(f"rerun_{index}#1", f"重构{index}", "rerun", sixes=sixes // 2, latest=TS + index * DAY,
                        series_key=f"rerun:{index}") for index in range(pools_per_column // 2)]
    pools += [_pool("joint_a#0", "特殊寻访甲", "joint", sixes=3, latest=TS),
              _pool("standard#0", "基础寻访", "standard", sixes=sixes * 3, latest=TS + DAY)]
    pools += [_pool(f"weponbox_{index}#0", f"武库{index}", "weapon_limited", item_type="武器", sixes=sixes,
                    latest=TS + index * DAY) for index in range(pools_per_column)]
    return _view(pools)


class GachaColumnLayoutTests(unittest.TestCase):
    ALL_KINDS = [
        _pool("special_a#0", "特许甲", "special"),
        _pool("rerun_a#1", "重构甲", "rerun", series_key="rerun:a"),
        _pool("joint_a#0", "特殊甲", "joint"),
        _pool("standard#0", "基础寻访", "standard"),
        _pool("beginner#0", "启程寻访", "beginner"),
        _pool("collab#0", "联动寻访", "unknown_char"),
        _pool("weponbox_a#0", "限时甲", "weapon_limited", item_type="武器"),
        _pool("rerun_wpn_a#1", "点绘申领", "weapon_rerun", item_type="武器", series_key="weapon_rerun:a"),
        _pool("weaponbox_constant_a#0", "常驻申领", "weapon_constant", item_type="武器"),
        _pool("odd#0", "奇怪申领", "unknown_weapon", item_type="武器"),
    ]

    def _keys(self, columns):
        return [[[card.pool.pool_id for card in section.cards] for section in column.sections] for column in columns]

    def test_rerun_side_right_puts_rerun_and_other_in_the_last_column(self):
        columns = draw.build_gacha_columns(_view(self.ALL_KINDS), rerun_side="right")
        self.assertEqual([column.spec.key for column in columns], ["special", "weapon", "rerun"])
        self.assertEqual([column.spec.title for column in columns], ["特许寻访", "武器申领", "重构寻访"])
        keys = self._keys(columns)
        self.assertEqual(keys[0], [["special_a"]])
        self.assertEqual(set(keys[1][0]), {"weponbox_a", "rerun_wpn_a", "weaponbox_constant_a", "odd"})
        self.assertEqual(keys[2][0], ["rerun_a"])
        self.assertEqual(set(keys[2][1]), {"joint_a", "standard", "beginner", "collab"})
        self.assertEqual(columns[2].sections[1].spec.divider, "其他寻访")
        self.assertEqual([column.index for column in columns], [0, 1, 2])

    def test_rerun_side_left_puts_rerun_and_other_in_the_first_column(self):
        columns = draw.build_gacha_columns(_view(self.ALL_KINDS), rerun_side="left")
        self.assertEqual([column.spec.key for column in columns], ["rerun", "special", "weapon"])
        keys = self._keys(columns)
        self.assertEqual(keys[0][0], ["rerun_a"])
        self.assertEqual(set(keys[0][1]), {"joint_a", "standard", "beginner", "collab"})
        self.assertEqual(keys[1], [["special_a"]])
        self.assertEqual(set(keys[2][0]), {"weponbox_a", "rerun_wpn_a", "weaponbox_constant_a", "odd"})

    def test_rerun_side_comes_from_env_and_defaults_to_right(self):
        view = _view(self.ALL_KINDS)
        cases = {"": "special", "right": "special", " LEFT ": "rerun", "middle": "special"}
        for raw, first in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {draw.GACHA_RERUN_SIDE_ENV: raw}):
                self.assertEqual(draw.build_gacha_columns(view)[0].spec.key, first)
        self.assertEqual(draw.GACHA_RERUN_SIDE_DEFAULT, "right")
        with self.assertRaises(ValueError):
            draw.build_gacha_columns(view, rerun_side="middle")

    def test_side_column_is_narrower_than_the_main_columns(self):
        for side in draw.GACHA_RERUN_SIDES:
            columns = draw.build_gacha_columns(_view(self.ALL_KINDS), rerun_side=side)
            shares = {column.spec.key: column.spec.share for column in columns}
            self.assertEqual(shares["special"], shares["weapon"])
            self.assertLess(shares["rerun"], shares["special"])
            style = draw._grid_style(columns)
            self.assertEqual(style.count("minmax(0,"), 3)
            self.assertIn(f"minmax(0,{draw.SIDE_COLUMN_SHARE:g}fr)", style)

    def test_no_rerun_collapses_to_two_columns_on_both_sides(self):
        pools = [pool for pool in self.ALL_KINDS if pool.kind_key != "rerun"]
        for side in draw.GACHA_RERUN_SIDES:
            with self.subTest(side=side):
                columns = draw.build_gacha_columns(_view(pools), rerun_side=side)
                self.assertEqual([column.spec.key for column in columns], ["special", "weapon"])
                keys = self._keys(columns)
                # 「其他寻访」接在特许寻访下方，两栏宽度相同、没有空的重构栏
                self.assertEqual(keys[0][0], ["special_a"])
                self.assertEqual(set(keys[0][1]), {"joint_a", "standard", "beginner", "collab"})
                self.assertEqual(columns[0].sections[1].spec.divider, "其他寻访")
                self.assertEqual(set(keys[1][0]), {"weponbox_a", "rerun_wpn_a", "weaponbox_constant_a", "odd"})
                self.assertEqual(draw._grid_style(columns), "grid-template-columns:minmax(0,1fr) minmax(0,1fr)")
                self.assertFalse(any(section.spec.empty == "暂无重构寻访记录"
                                     for column in columns for section in column.sections))

    def test_weapon_rerun_alone_does_not_make_a_rerun_column(self):
        view = _view([_pool("rerun_wpn_a#1", "点绘申领", "weapon_rerun", item_type="武器",
                            series_key="weapon_rerun:a")])
        self.assertEqual([column.spec.key for column in draw.build_gacha_columns(view)], ["special", "weapon"])

    def test_every_registry_kind_has_a_column(self):
        registry = set(sys.modules[f"{PACKAGE}.gacha.pools"].KINDS_BY_KEY)
        for name, columns in draw.GACHA_LAYOUTS.items():
            mapped = {kind for column in columns for section in column.sections for kind in section.kinds}
            self.assertEqual(mapped, registry - ({"rerun"} if name == "two" else set()), name)

    def test_kind_label_is_hidden_only_for_special_pools(self):
        columns = draw.build_gacha_columns(_view([pool for pool in self.ALL_KINDS if pool.kind_key != "rerun"]))
        special, other = columns[0].sections
        self.assertFalse(special.spec.show_kind)
        self.assertTrue(other.spec.show_kind)
        self.assertTrue(all(section.spec.show_kind for section in columns[1].sections))

    def test_current_first_then_series_adjacent_newest_period_first(self):
        view = _view([
            _pool("rerun_old#1", "流光回响", "rerun", latest=TS + 50 * DAY, series_key="rerun:old"),
            _pool("rerun_x#1", "绚丽异彩", "rerun", latest=TS + 10 * DAY, series_key="rerun:x",
                  series_index=1, series_run_count=2, series_inherited_to=2),
            _pool("rerun_x#2", "绚丽异彩", "rerun", latest=TS + 90 * DAY, series_key="rerun:x", current=True,
                  series_index=2, series_run_count=2),
            _pool("rerun_long#1", "名字很长的重构", "rerun", latest=TS + 30 * DAY, series_key="rerun:long"),
        ])
        cards = _column(draw.build_gacha_columns(view), "rerun").sections[0].cards
        self.assertEqual([card.key for card in cards], ["rerun_x#2", "rerun_x#1", "rerun_old#1", "rerun_long#1"])
        self.assertEqual([card.featured for card in cards], [True, False, False, False])

    def test_rerun_same_pool_id_and_new_pool_id_give_the_same_layout(self):
        def records(second_pool_id: str, second_version: int):
            items = [GachaRecord("role", "server", "rerun_chr_x", "绚丽异彩", "E_CharacterGachaPoolType_Rerun",
                                 str(seq), seq, f"r{seq}", "结果", 4, "角色", pool_version=1) for seq in range(1, 41)]
            items += [GachaRecord("role", "server", second_pool_id, "绚丽异彩", "E_CharacterGachaPoolType_Rerun",
                                  str(seq), 10_000 + seq, f"r{seq}", "结果", 4, "角色", pool_version=second_version)
                      for seq in range(41, 81)]
            return items

        rules = {
            pool_id: sys.modules[f"{PACKAGE}.gacha.assets"].GachaPoolRule(pool_id, ("chr_up",), 0)
            for pool_id in ("rerun_chr_x", "rerun_chr_x_b")
        }
        layouts = []
        for pool_id, version in (("rerun_chr_x", 2), ("rerun_chr_x_b", 1)):
            view = gacha_module.build_gacha_analysis(_role(), records(pool_id, version), [], pool_rules=rules)
            cards = _column(draw.build_gacha_columns(view), "rerun").sections[0].cards
            layouts.append([(card.pool.series_index, card.featured, card.pool.series_inherited_to) for card in cards])
            pieces = [draw.render_pool_piece(card, draw.render_pool_rows(card), 0, 1, cont=False, open_end=False,
                                             show_kind=True) for card in cards]
            self.assertIn("第 2 期", pieces[0])
            self.assertIn("累计已继承至第 2 期", pieces[1])
            self.assertEqual(pieces[0].count("CURRENT"), 1)
            self.assertNotIn("CURRENT", pieces[1])
        self.assertEqual(layouts[0], layouts[1])
        self.assertEqual(layouts[0], [(2, True, 0), (1, False, 2)])

    def test_show_standard_off_leaves_only_a_note_and_keeps_totals(self):
        view = _view([
            _pool("standard#0", "基础寻访", "standard", total=210, sixes=2),
            _pool("joint_a#0", "特殊甲", "joint"),
        ], show_standard_pools=False)
        columns, rows = _layout(view)
        other = _other_section(columns)
        self.assertEqual([card.pool.pool_id for card in other.cards], ["joint_a"])
        self.assertEqual([card.pool.pool_id for card in other.hidden], ["standard"])
        # 没有重构池 → 两栏，「其他寻访」在特许寻访栏：暂无特许 → 分组标题 → 卡片 → 隐藏说明
        units = draw.column_units(_column(columns, "special"), {key: len(items) for key, items in rows.items()})
        self.assertEqual([unit.kind for unit in units], ["empty", "divider", "card", "note"])
        self.assertIn("基础寻访已按设置隐藏：210 抽 · 2 个六星，仍计入总数与角色寻访", units[-1].text)
        self.assertEqual(view.total, 310)
        shown = _other_section(draw.build_gacha_columns(view, show_standard=True))
        self.assertEqual({card.pool.pool_id for card in shown.cards}, {"joint_a", "standard"})

    def test_only_hidden_standard_keeps_the_other_section(self):
        view = _view([_pool("standard#0", "基础寻访", "standard", sixes=1)], show_standard_pools=False)
        columns, rows = _layout(view)
        units = draw.column_units(_column(columns, "special"), {key: len(items) for key, items in rows.items()})
        self.assertEqual([unit.kind for unit in units], ["empty", "divider", "note"])
        rerun_view = _view([_pool("standard#0", "基础寻访", "standard", sixes=1),
                            _pool("rerun_a#1", "重构甲", "rerun", series_key="rerun:a")], show_standard_pools=False)
        columns, rows = _layout(rerun_view)
        units = draw.column_units(_column(columns, "rerun"), {key: len(items) for key, items in rows.items()})
        self.assertEqual([unit.kind for unit in units], ["card", "divider", "note"])

    def test_missing_kind_key_falls_back_to_registry_detection(self):
        view = _view([
            PoolAnalysis("special_old", "旧限定", "角色", 10, 0, is_current=True),
            PoolAnalysis("weapon", "武器池", "武器", 20, 0),
            PoolAnalysis("pool", "联合寻访", "角色", 12, 4),
        ])
        columns = draw.build_gacha_columns(view)
        self.assertEqual([card.pool.pool_id for card in columns[0].sections[0].cards], ["special_old"])
        self.assertTrue(columns[0].sections[0].cards[0].featured)
        self.assertEqual([card.pool.pool_id for card in columns[0].sections[1].cards], ["pool"])
        self.assertEqual([card.pool.pool_id for card in columns[1].sections[0].cards], ["weapon"])

    def test_duplicate_card_keys_are_made_unique(self):
        view = _view([PoolAnalysis("same", "甲", "角色", 1, 0), PoolAnalysis("same", "乙", "武器", 1, 0)])
        keys = [card.key for column in draw.build_gacha_columns(view) for section in column.sections
                for card in section.cards]
        self.assertEqual(len(keys), len(set(keys)))


class GachaPityCellTests(unittest.TestCase):
    def _cells(self, pool):
        return draw.build_pity_cells(draw._gacha_cards([pool])[0])

    def test_special_has_three_cells(self):
        cells = self._cells(_pool("special_a#0", "特许", "special", current=True, small_pity_limit=80,
                                  small_pity_progress=67, soft_pity_active=True, soft_pity_start=66,
                                  large_pity_limit=120, large_pity_known=True, large_pity_progress=67,
                                  keepsake_progress=20, keepsake_claims=1))
        self.assertEqual([cell.label for cell in cells], ["距小保底", "距大保底", "距下次信物"])
        self.assertEqual(cells[0].value, "13 抽")
        self.assertIn("特许间共享继承", cells[0].notes)
        self.assertTrue(any("软保底" in note for note in cells[0].notes))
        self.assertEqual((cells[1].value, cells[1].notes[0]), ("53 抽", "进度 67/120"))
        self.assertEqual((cells[2].value, cells[2].notes), ("220 抽", ("进度 260/480", "已赠 1 次")))

    def test_rerun_is_two_by_two_with_large_pity_rush_and_inherited_keepsake(self):
        cells = self._cells(_pool(
            "rerun_x#2", "绚丽异彩", "rerun", current=True, small_pity_limit=80, small_pity_progress=31,
            large_pity_limit=120, large_pity_known=True, large_pity_consumed=True, large_pity_consumed_at=120,
            large_pity_up_name="示例重构UP", rush_thresholds=(30, 60, 90), rush_claimed=3, rush_used=2,
            series_total=260, series_inherited_total=200, keepsake_progress=20, keepsake_claims=1,
        ))
        self.assertEqual([cell.label for cell in cells], ["距六星保底", "距大保底", "加急招募", "距下次信物"])
        self.assertEqual(cells[1].value, "已消耗")
        self.assertIn("第120抽获得示例重构UP", cells[1].notes)
        self.assertEqual(cells[2].value, "已领完")
        self.assertIn("已获 3/3 次 · 未用 1", cells[2].notes)
        self.assertEqual(cells[3].notes, ("累计 260/480", "含继承 200 抽"))
        html = draw._pity_grid(cells)
        self.assertIn('class="pity-grid pity-four"', html)
        self.assertNotIn("待确认", html)

    def test_rerun_rush_countdown(self):
        cells = self._cells(_pool("rerun_x#1", "绚丽异彩", "rerun", current=True, small_pity_limit=80,
                                  rush_thresholds=(30, 60, 90), rush_claimed=2, rush_used=2, series_total=64,
                                  rush_next_threshold=90, rush_next_remaining=26))
        self.assertEqual((cells[2].label, cells[2].value), ("距加急招募", "26 抽"))
        self.assertEqual(cells[2].notes, ("累计 64/90", "已获 2/3 次"))

    def test_weapon_limited_two_cells_and_weapon_rerun_two_by_two(self):
        limited = self._cells(_pool(
            "weponbox_a#0", "限时", "weapon_limited", item_type="武器", current=True, small_pity_limit=4,
            small_pity_progress=1, large_pity_limit=80, large_pity_known=True, large_pity_progress=30,
        ))
        self.assertEqual([(cell.label, cell.value) for cell in limited], [("距小保底", "3 次十连"), ("距大保底", "50 抽")])
        rerun = self._cells(_pool(
            "rerun_wpn_a#2", "点绘申领", "weapon_rerun", item_type="武器", current=True, small_pity_limit=4,
            small_pity_progress=2, large_pity_limit=80, large_pity_known=True, large_pity_progress=80,
            six_stars=(SixStarEvent("艺术暴君", "点绘申领", "武器", TS, pool_position=40, up_status="up"),),
            series_claims=26, series_inherited_claims=10,
            next_rewards=(NextReward("up_weapon", "点绘赠礼", 34, 8, "次申领"),
                          NextReward("weapon_box", "武库赠礼", 42, 16, "次申领")),
        ))
        self.assertEqual([cell.label for cell in rerun], ["距小保底", "距大保底", "距武库赠礼", "距点绘赠礼"])
        self.assertEqual((rerun[0].value, rerun[0].notes[0]), ("2 次十连", "进度 2/4"))
        self.assertEqual((rerun[1].value, rerun[1].notes[0]), ("已触发", "第40抽获得UP"))
        self.assertEqual((rerun[2].value, rerun[2].notes), ("16 次申领", ("累计 26/42", "含继承 10 次")))
        self.assertEqual((rerun[3].value, rerun[3].notes[0]), ("8 次申领", "累计 26/34"))

    def test_history_constant_and_other_pools_have_no_cells(self):
        self.assertEqual(self._cells(_pool("special_b#0", "特许旧", "special")), [])
        self.assertEqual(self._cells(_pool("weaponbox_constant_a#0", "常驻", "weapon_constant", item_type="武器",
                                           current=True, small_pity_limit=4)), [])
        self.assertEqual(self._cells(_pool("joint#0", "特殊", "joint", current=True, small_pity_limit=80)), [])

    def test_missing_progress_fields_render_placeholders(self):
        cells = self._cells(_pool("rerun_x#1", "重构", "rerun", current=True))
        self.assertEqual([cell.value for cell in cells], ["--", "无", "--", "240 抽"])


class GachaRowTests(unittest.TestCase):
    def test_rows_order_labels_and_one_data_row_per_record(self):
        pool = _pool(
            "rerun_x#2", "绚丽异彩", "rerun", current=True, since=7,
            six_stars=(_six("示例重构UP", 120, labels=("大保底",), up="up"), _six("示例常驻角色", 60, labels=("歪",))),
            keepsake_gifts=(KeepsakeGift("示例重构UP的信物", "k", TS, 40, gift_kind="keepsake", series_position=240),),
            free_batches=(FreePullBatch(TS, 10, (), source="rush", threshold=60),
                          FreePullBatch(TS - DAY, 10, (_six("免费六星", 0),), source="rush", threshold=30)),
        )
        card = draw._gacha_cards([pool])[0]
        rows = draw.render_pool_rows(card)
        kinds = [re.search(r'data-kind="(\w+)"', row).group(1) for row in rows]
        self.assertEqual(kinds, ["current", "six", "six", "gift", "free", "free"])
        self.assertEqual(len({re.search(r'data-row="([^"]+)"', row).group(1) for row in rows}), len(rows))
        self.assertIn("累计第240抽赠送信物", rows[3])
        self.assertIn("加急招募 · 未出六星", rows[4])
        self.assertIn("累计 60 抽获得 · 不计保底", rows[4])
        self.assertIn("加急招募 · 免费六星", rows[5])
        self.assertIn("pity-hit-large", rows[1])
        self.assertIn("pity-hit-miss", rows[2])

    def test_weapon_gift_labels_follow_kind_and_gift_kind(self):
        gifts = (KeepsakeGift("艺术暴君", "w", TS, 180, gift_kind="up_weapon", claim_count=18),
                 KeepsakeGift("自选武库箱", "", TS, 100, gift_kind="weapon_box", claim_count=10))
        rerun = draw._gacha_cards([_pool("rerun_wpn#1", "点绘申领", "weapon_rerun", item_type="武器",
                                         keepsake_gifts=gifts)])[0]
        limited = draw._gacha_cards([_pool("weponbox#0", "限时", "weapon_limited", item_type="武器",
                                           keepsake_gifts=gifts[:1])])[0]
        rerun_rows = "".join(draw.render_pool_rows(rerun))
        self.assertIn("累计第18次申领赠送点绘赠礼", rerun_rows)
        self.assertIn("累计第10次申领赠送武库赠礼", rerun_rows)
        self.assertIn("累计第18次申领赠送UP武器", "".join(draw.render_pool_rows(limited)))

    def test_empty_pool_has_a_placeholder_row(self):
        rows = draw.render_pool_rows(draw._gacha_cards([_pool("beginner#0", "启程寻访", "beginner")])[0])
        self.assertEqual(len(rows), 1)
        self.assertIn("本池记录中尚无六星", rows[0])

    def test_long_names_wrap_instead_of_ellipsis(self):
        self.assertNotIn("text-overflow", draw.GACHA_CSS)
        self.assertNotIn("line-clamp", draw.GACHA_CSS)
        self.assertIn("overflow-wrap:anywhere", draw.GACHA_CSS)
        meta = draw._meta(("重构寻访", "第 2 期", "12 个付费六星"))
        self.assertEqual(meta.count('class="mp"'), 3)
        self.assertNotIn(" · <", meta)       # 「·」贴着前一段，不会出现在行首


class GachaTypographyTests(unittest.TestCase):
    def test_css_only_uses_weights_the_bundled_font_has(self):
        weights = set(re.findall(r"font-weight:\s*(\d+)", draw.GACHA_CSS))
        self.assertTrue(weights)
        self.assertLessEqual(weights, {"400", "500", "700"})
        self.assertIsNone(re.search(r"font-weight:\s*(800|850|900|950)", draw.GACHA_CSS))
        self.assertIn("font-synthesis:none", draw.GACHA_CSS)

    def test_harmonyos_font_face_is_embedded_and_first_in_the_stack(self):
        family = draw.GACHA_FONT_FAMILY
        self.assertIn(f"font-family:'{family}','Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif",
                      draw.GACHA_CSS)
        faces = draw.gacha_font_face_css()
        self.assertEqual(re.findall(rf'@font-face\{{font-family:"{family}";font-weight:(\d+)', faces),
                         ["400", "500", "700"])
        for _weight, name in draw.GACHA_FONT_FILES:
            path = draw.GACHA_FONT_DIR / name
            self.assertTrue(name.startswith("HarmonyOS_Sans_SC_") and path.is_file(), path)
            head = base64.b64encode(path.read_bytes()[:48]).decode("ascii")
            self.assertIn(f"url(data:font/ttf;base64,{head}", faces)
        document = draw._document("<div></div>")
        self.assertLess(document.index("@font-face"), document.index(draw.GACHA_CSS))

    def test_missing_font_files_fall_back_to_the_system_stack(self):
        draw.gacha_font_face_css.cache_clear()
        try:
            with mock.patch.object(draw, "GACHA_FONT_DIR", Path("/nonexistent/fonts")):
                self.assertEqual(draw.gacha_font_face_css(), "")
                self.assertIn("'Microsoft YaHei'", draw._document("<div></div>"))
        finally:
            draw.gacha_font_face_css.cache_clear()

    def test_summary_small_text_is_at_least_12px(self):
        sizes = [
            float(size)
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", draw.GACHA_CSS)
            if re.search(r"\.(total|metric|expectation)", selectors)
            for size in re.findall(r"font-size:([\d.]+)px", body)
        ]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12)

    def test_summary_titles_are_dark_bold_and_share_one_heading_block(self):
        rules = {selectors.strip(): body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", draw.GACHA_CSS)}

        def size(selector: str) -> float:
            return float(re.search(r"font-size:([\d.]+)px", rules[selector]).group(1))

        title = rules[".metric-title span"]
        self.assertIn("font-weight:700", title)
        self.assertIn("color:#181818", title)
        self.assertGreaterEqual(size(".metric-title span"), 18)
        # 层级：标题明显大于下方副标题与期望行标签，kicker 最小
        self.assertGreater(size(".metric-title span"), size(".total>small,.metric>small") + 4)
        self.assertGreater(size(".metric-title span"), size(".expectation-label strong"))
        self.assertLess(size(".metric-title small"), size(".metric-title span"))
        # 总数与标题底边对齐，而不是标题贴在大数字底下当说明文字
        self.assertIn("align-items:flex-end", rules[".total-head,.metric-head"])
        self.assertIn("line-height:1", rules[".total-head strong,.metric-head strong"])

        view = _view([
            _pool("special_a#0", "特许", "special", current=True),
            _pool("rerun_a#1", "重构", "rerun", series_key="rerun:a"),
            _pool("weponbox_a#0", "限时", "weapon_limited", item_type="武器"),
        ])
        columns, _ = _layout(view)
        summary = draw._summary_html(view, columns)
        self.assertEqual(re.findall(r'<div class="metric-title"><small>[^<]+</small><span>([^<]+)</span>', summary),
                         ["卡池总数", "特许寻访", "武器申领", "重构寻访"])
        self.assertIn('<div class="total-head"><div class="metric-title">', summary)

    def test_total_tile_is_built_like_the_pool_tiles(self):
        """卡池总数格与池格同构：总数在标题行右侧，下面一行两段小字，再下面是角色 / 武器两个分项块；没有沉底的大数字。"""
        free_six = FreePullBatch(TS, 10, (_six("免费六星", 0),), source="free_ten")
        view = _view([
            _pool("special_a#0", "特许", "special", current=True, total=130, paid=120, free_pull_count=10,
                  sixes=3, free_batches=(free_six,)),
            _pool("rerun_a#1", "重构", "rerun", series_key="rerun:a", total=60, paid=60, sixes=1),
            _pool("standard#0", "基础寻访", "standard", total=50, paid=50, sixes=2),
            _pool("weponbox_a#0", "限时", "weapon_limited", item_type="武器", total=40, paid=40, sixes=2),
        ], paid_total=270, free_pull_count=10)
        columns, _ = _layout(view)
        history = replace(view, xhh_imported_at=TS, recorded_total=250, history_missing_count=30)
        cases = {"plain": (view, ["付费 270", "免费 10"]), "xhh": (history, ["逐抽明细 250", "统计补齐 30"])}
        for name, (case, caption) in cases.items():
            with self.subTest(name):
                summary = draw._summary_html(case, columns)
                total = summary.split('<div class="total">', 1)[1].split('<div class="metric">', 1)[0]
                self.assertIn('<div class="total-head"><div class="metric-title"><small>全部卡池 · 累计抽数</small>'
                              '<span>卡池总数</span></div><strong>280</strong></div>', total)
                self.assertEqual(total.count("<strong>280</strong>"), 1)
                small = re.search(r"</div><small>(.*?)</small><div class=\"expectation-summary\">", total).group(1)
                self.assertEqual(re.findall(r'<span class="mp">([^<]+)</span>', small), caption)
                rows = re.findall(r'<div class="expectation-label"><strong>([^<]+)</strong><small>([^<]+)</small></div>'
                                  r'<div class="expectation-values">(.*?)</div>', total)
                self.assertEqual(
                    [(label, scope, [re.sub(r"<[^>]+>", "", span) for span in re.findall(r"<span>(.*?)</span>", body)])
                     for label, scope, body in rows],
                    [("角色寻访", "全部角色池", ["累计 240 抽", "六星 7 个"]),       # 特许 3+免费 1、重构 1、常驻 2
                     ("武器申领", "全部武器池", ["累计 40 抽", "六星 2 个"])],
                )
        rules = {selectors.strip(): body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", draw.GACHA_CSS)}
        self.assertNotIn(".total>strong", rules)
        self.assertNotIn("margin-top:auto", rules[".total"])


class GachaPaginationTests(unittest.TestCase):
    def _paginate(self, view, **measure_kwargs):
        columns, rows = _layout(view)
        measure = _fake_measure(columns, rows, **measure_kwargs)
        units = [draw.column_units(column, {key: len(items) for key, items in rows.items()}) for column in columns]
        pages, info = draw.paginate_gacha(columns, units, measure)
        return columns, rows, measure, pages, info

    def _check_pages(self, rows, measure, pages, cap):
        """每页（开销 + 最高一栏）≤ cap − 余量；每条记录行恰好出现一次。"""
        seen = []
        for page_index, page in enumerate(pages):
            heights = [draw.column_height(units, measure.heads[(index, page_index > 0)], measure)
                       for index, units in enumerate(page)]
            overhead = measure.overhead_cont if page_index else measure.overhead_first
            self.assertLessEqual(overhead + max(heights), cap - draw.GACHA_PAGE_SAFETY + 0.01)
            seen.extend((unit.card.key, row) for units in page for unit in units if unit.kind == "card"
                        for row in range(unit.start, unit.end))
        self.assertEqual(sorted(seen), sorted((key, row) for key, items in rows.items() for row in range(len(items))))
        self.assertEqual(len(seen), len(set(seen)))

    def test_page_height_cap_comes_from_the_module_constant(self):
        columns, rows = _layout(_heavy_view())
        measure = _fake_measure(columns, rows)
        units = [draw.column_units(column, {key: len(items) for key, items in rows.items()}) for column in columns]
        default_pages, _ = draw.paginate_gacha(columns, units, measure)
        self.assertGreater(len(default_pages), 2)
        self._check_pages(rows, measure, default_pages, draw.GACHA_PAGE_MAX_HEIGHT)
        low_pages, info = draw.paginate_gacha(columns, units, measure, max_height=3000)
        self.assertGreater(len(low_pages), len(default_pages))
        self.assertEqual(info["budget_first"], round(3000 - measure.overhead_first - draw.GACHA_PAGE_SAFETY, 1))
        self._check_pages(rows, measure, low_pages, 3000)
        with mock.patch.object(draw, "GACHA_PAGE_MAX_HEIGHT", 3000):
            patched_pages, _ = draw.paginate_gacha(columns, units, measure)
        self.assertEqual(len(patched_pages), len(low_pages))

    def test_two_column_layout_paginates_with_other_section_under_special(self):
        columns, rows, measure, pages, info = self._paginate(_heavy_view(rerun=False))
        self.assertEqual([column.spec.key for column in columns], ["special", "weapon"])
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) == 2 for page in pages))
        self._check_pages(rows, measure, pages, draw.GACHA_PAGE_MAX_HEIGHT)
        dividers = [unit for page in pages for unit in page[0] if unit.kind == "divider"]
        self.assertEqual([unit.section.spec.divider for unit in dividers if not unit.cont], ["其他寻访"])
        self.assertFalse(any(unit.kind == "divider" for page in pages for unit in page[1]))

    def test_single_page_when_it_fits(self):
        view = _view([_pool("special_a#0", "特许", "special", current=True, sixes=3)])
        _, _, _, pages, info = self._paginate(view)
        self.assertEqual(len(pages), 1)
        self.assertEqual(info["tries"], 0)
        self.assertFalse(any(unit.kind in ("hint", "done") for column in pages[0] for unit in column))

    def test_minimum_page_count_and_balanced_heights(self):
        columns, rows, measure, pages, info = self._paginate(_heavy_view())
        self.assertEqual(len(pages), max(info["column_pages"]))
        # 用最宽松的栏高也装不进 N−1 页：N 是最少页数
        budget_first = draw.GACHA_PAGE_MAX_HEIGHT - measure.overhead_first - draw.GACHA_PAGE_SAFETY
        budget_cont = draw.GACHA_PAGE_MAX_HEIGHT - measure.overhead_cont - draw.GACHA_PAGE_SAFETY
        units = [draw.column_units(column, {key: len(items) for key, items in rows.items()}) for column in columns]
        fewer = [
            draw._pack_column(column, column_units, measure,
                              lambda page: budget_first if page == 0 else budget_cont, len(pages) - 1)
            for column, column_units in zip(columns, units)
        ]
        self.assertIn(None, fewer)
        totals = []
        for page_index, page in enumerate(pages):
            overhead = measure.overhead_cont if page_index else measure.overhead_first
            totals.append(overhead + max(
                draw.column_height(units, measure.heads[(index, page_index > 0)], measure)
                for index, units in enumerate(page)
            ))
        # 统一栏高：各页总高度相差不超过一行记录加一个池头
        self.assertLessEqual(max(totals) - min(totals), 110 + 50 + 6)

    def test_split_rules_continuation_heads_hints_and_dividers(self):
        columns, rows, measure, pages, info = self._paginate(_heavy_view())
        for column_index, column_pages in enumerate(info["column_pages"]):
            for page_index, page in enumerate(pages):
                units = page[column_index]
                kinds = [unit.kind for unit in units]
                if page_index >= column_pages:
                    self.assertEqual(kinds, ["done"])
                    self.assertIn(f"本栏已在第 {column_pages} 页展示完毕", units[0].text)
                    continue
                if page_index < column_pages - 1:
                    self.assertEqual(kinds[-1], "hint")
                    self.assertRegex(units[-1].text, rf"续见第 {page_index + 2} 页")
                    if units[-2].open_end:
                        self.assertIn("本池未完", units[-1].text)
                else:
                    self.assertNotIn("hint", kinds)
                for position, unit in enumerate(units):
                    if unit.kind == "card" and (unit.cont or unit.open_end):
                        self.assertGreaterEqual(len(rows[unit.card.key]), draw.SPLIT_MIN_ROWS)
                        self.assertGreaterEqual(unit.end - unit.start, draw.SPLIT_KEEP_ROWS)
                    if unit.kind == "card" and unit.cont:
                        self.assertGreater(page_index, 0)
                        self.assertEqual(position, 1 if kinds[0] == "divider" else 0)
                    if unit.kind == "divider":       # 分组标题不落单：后面紧跟本组的卡片或说明
                        self.assertLess(position + 1, len(units))
                        self.assertIn(kinds[position + 1], ("card", "note"))
                if page_index and units[0].kind in ("card", "note") and units[0].section.spec.divider:
                    self.fail("续页从「其他寻访」中间开始时必须先补「其他寻访（续）」")
        side = _column(columns, "rerun").index
        split_standard = any(unit.cont for page in pages[1:] for unit in page[side]
                             if unit.kind == "card" and unit.card.kind.key == "standard")
        self.assertTrue(split_standard)
        self.assertTrue(any(page[side][0].kind == "divider" and page[side][0].cont for page in pages[1:]))

    def test_small_cards_move_whole_and_pagination_is_deterministic(self):
        view = _heavy_view(sixes=2)
        first = self._paginate(view)[3]
        second = self._paginate(view)[3]
        self.assertEqual(
            [[[(u.kind, u.card.key if u.card else u.text, u.start, u.end) for u in col] for col in page] for page in first],
            [[[(u.kind, u.card.key if u.card else u.text, u.start, u.end) for u in col] for col in page] for page in second],
        )
        rows = _layout(view)[1]
        for page in first:
            for units in page:
                for unit in units:
                    if unit.kind == "card" and unit.end - unit.start < len(rows[unit.card.key]):
                        self.assertGreaterEqual(len(rows[unit.card.key]), draw.SPLIT_MIN_ROWS)


class GachaRenderTests(unittest.IsolatedAsyncioTestCase):
    async def _render(self, view, *, shell=None, measure=None):
        documents = []

        async def fake_shell(document):
            documents.append(document)
            return b"png"

        evaluate = mock.AsyncMock(return_value=measure if measure is not None else _raw_measure(view))
        shell_mock = mock.AsyncMock(side_effect=shell or fake_shell)
        with (
            mock.patch.object(draw, "evaluate_web_page", evaluate),
            mock.patch.object(draw, "_draw_gacha_shell", shell_mock),
            mock.patch.object(draw, "_write_temp_html", return_value=Path("/nonexistent/gacha-measure.html")),
            mock.patch.object(draw, "schedule_temp_file_cleanup"),
            # 浏览器已打桩：不往每页 HTML 里塞约 33 MB 的 base64 字体（字体嵌入由 GachaTypographyTests 覆盖）。
            mock.patch.object(draw, "gacha_font_face_css", return_value=""),
        ):
            pages = await draw.draw_gacha_analysis_cards(view, uid="****1234")
        return pages, documents, evaluate, shell_mock

    async def test_measures_once_then_one_screenshot_per_page(self):
        view = _heavy_view()
        pages, documents, evaluate, shell = await self._render(view)
        self.assertGreater(len(pages), 1)
        evaluate.assert_awaited_once()
        self.assertEqual(shell.await_count, len(pages))
        expected_records = sum(len(pool.six_stars) + len(pool.keepsake_gifts) + len(pool.free_batches)
                               for pool in view.pools)
        rendered = [match for document in documents for match in re.findall(r'data-kind="(six|gift|free)"', document)]
        self.assertEqual(len(rendered), expected_records)
        for number, document in enumerate(documents, start=1):
            self.assertNotIn("text-overflow", document)
            self.assertNotIn("pool-more", document)
            self.assertNotIn("fold-", document)
            self.assertIn(f"第 {number}/{len(documents)} 页", document)
            if number == 1:
                self.assertIn('class="summary"', document)
                self.assertNotIn("抽卡分析（续）", document)
            else:
                self.assertNotIn('class="summary"', document)
                self.assertIn("抽卡分析（续）", document)
                self.assertIn("（续）</em>", document)

    async def test_summary_has_four_tiles_rerun_expectation_and_weapon_constant(self):
        view = _view([
            _pool("special_a#0", "特许", "special", current=True),
            _pool("rerun_a#1", "重构", "rerun", series_key="rerun:a"),
            _pool("weaponbox_constant_a#0", "常驻", "weapon_constant", item_type="武器", total=30),
        ])
        _, documents, _, _ = await self._render(view)
        summary = documents[0].split('class="summary"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(summary.count('class="metric"'), 3)
        self.assertIn("重构寻访", summary)
        self.assertIn("含常驻 30", summary)
        self.assertNotIn("待确认", summary)
        # 总览格顺序跟随栏序（默认重构在右）
        self.assertEqual(re.findall(SUMMARY_TITLE, summary), ["特许寻访", "武器申领", "重构寻访"])

    async def test_pages_use_the_gacha_width_and_both_rerun_sides(self):
        view = _heavy_view()
        self.assertEqual(draw.GACHA_CARD_WIDTH, 1600)
        self.assertNotIn("1280px", draw.GACHA_CSS)
        self.assertIn("width:var(--gacha-width)", draw.GACHA_CSS)
        expected = {"right": ["special", "weapon", "rerun"], "left": ["rerun", "special", "weapon"]}
        for side, order in expected.items():
            with self.subTest(side=side), mock.patch.dict(os.environ, {draw.GACHA_RERUN_SIDE_ENV: side}):
                pages, documents, evaluate, _ = await self._render(view)
                self.assertGreater(len(pages), 1)
                self.assertEqual(evaluate.await_args.kwargs["viewport"][0], draw.GACHA_CARD_WIDTH)
                for document in documents:
                    self.assertIn(f"--gacha-width:{draw.GACHA_CARD_WIDTH}px", document)
                    self.assertIn('data-cols="3"', document)
                    self.assertEqual(re.findall(r'data-key="(\w+)"', document), order)
                titles = re.findall(SUMMARY_TITLE, documents[0])
                self.assertEqual(titles, [{"special": "特许寻访", "weapon": "武器申领", "rerun": "重构寻访"}[key]
                                          for key in order])

    async def test_no_rerun_pages_have_two_columns_on_both_sides(self):
        view = _view([
            _pool("special_a#0", "特许", "special", current=True, sixes=2),
            _pool("standard#0", "基础寻访", "standard", sixes=3),
            _pool("weponbox_a#0", "限时", "weapon_limited", item_type="武器", sixes=1),
        ])
        for side in draw.GACHA_RERUN_SIDES:
            with self.subTest(side=side), mock.patch.dict(os.environ, {draw.GACHA_RERUN_SIDE_ENV: side}):
                pages, documents, _, _ = await self._render(view)
                self.assertEqual(len(pages), 1)
                document = documents[0]
                self.assertEqual(re.findall(r'data-key="(\w+)"', document), ["special", "weapon"])
                self.assertIn('data-cols="2"', document)
                self.assertNotIn("暂无重构寻访记录", document)
                summary = document.split('class="summary"', 1)[1].split("</section>", 1)[0]
                self.assertIn("repeat(2,minmax(0,1fr))", summary)
                self.assertEqual(summary.count('class="metric"'), 2)
                self.assertNotIn("重构寻访", summary)
                special = document.split('data-key="special"', 1)[1].split('data-key="weapon"', 1)[0]
                self.assertIn("<h3>其他寻访</h3>", special)
                self.assertIn('data-pool="standard#0"', special)

    async def test_height_overflow_repacks_once_with_larger_safety(self):
        view = _heavy_view()
        calls = {"count": 0}

        async def shell(document):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("Screenshot element height 4200px exceeds limit 4096px")
            return b"png"

        with mock.patch.object(draw, "paginate_gacha", wraps=draw.paginate_gacha) as paginate:
            pages, _, _, _ = await self._render(view, shell=shell)
        self.assertTrue(pages)
        self.assertEqual([call.kwargs["safety"] for call in paginate.call_args_list],
                         [draw.GACHA_PAGE_SAFETY, draw.GACHA_RETRY_SAFETY])

    async def test_measure_or_layout_failure_falls_back_to_v1(self):
        view = _view([_pool("special_a#0", "特许", "special", current=True)])
        legacy = mock.AsyncMock(return_value=(b"v1",))
        with mock.patch.object(cards_module, "_draw_gacha_analysis_cards_v1", legacy):
            pages, _, _, _ = await self._render(view, measure={"broken": True})
            self.assertEqual(pages, (b"v1",))

            async def overflow(document):
                raise RuntimeError("Screenshot layout overflow detected: [...]")

            pages, _, _, _ = await self._render(view, shell=overflow)
            self.assertEqual(pages, (b"v1",))
        self.assertEqual(legacy.await_count, 2)

    async def test_entry_switch_and_size_constants(self):
        self.assertEqual(draw.GACHA_CARD_WIDTH, 1600)
        self.assertEqual(draw.GACHA_PAGE_MAX_HEIGHT, 4096)
        self.assertEqual(cards_module.CARD_MAX_HEIGHT, 6144)
        view = _view([_pool("special_a#0", "特许", "special")])
        v1 = mock.AsyncMock(return_value=(b"v1",))
        v3 = mock.AsyncMock(return_value=(b"v3",))
        with mock.patch.object(cards_module, "_draw_gacha_analysis_cards_v1", v1), \
                mock.patch.object(draw, "draw_gacha_analysis_cards", v3):
            with mock.patch.dict(os.environ, {"ENDFIELD_GACHA_LAYOUT": "v1"}):
                self.assertEqual(await cards_module.draw_gacha_analysis_cards(view, uid="u"), (b"v1",))
            with mock.patch.dict(os.environ, {"ENDFIELD_GACHA_LAYOUT": ""}):
                self.assertEqual(await cards_module.draw_gacha_analysis_cards(view, uid="u"), (b"v3",))

    async def test_real_browser_page_is_card_width_wide_and_within_limit(self):
        view = _view([
            _pool("special_a#0", "特许寻访·示例名称很长很长的卡池用于换行测试", "special", current=True, sixes=4,
                  small_pity_limit=80, small_pity_progress=12),
            _pool("rerun_a#1", "绚丽异彩", "rerun", current=True, sixes=2, series_key="rerun:a"),
            _pool("weponbox_a#0", "武库申领", "weapon_limited", item_type="武器", current=True, sixes=2,
                  small_pity_limit=4, large_pity_limit=80, large_pity_known=True),
        ], role=_role("示例玩家名字特别特别长的重度测试账号昵称再长一点"))
        from otae_bot.infrastructure.rendering.browser import close_browser

        try:
            pages = await draw.draw_gacha_analysis_cards(view, uid="****1234")
        finally:
            await close_browser()
        self.assertEqual(len(pages), 1)
        image = Image.open(BytesIO(pages[0])).convert("RGB")
        self.assertEqual(image.width, 2 * draw.GACHA_CARD_WIDTH)
        self.assertLessEqual(image.height, 2 * draw.GACHA_PAGE_MAX_HEIGHT)
        extrema = image.getextrema()
        self.assertEqual(extrema[0], extrema[1])      # 无保底芯片时整张图仍是家族 B 灰阶
        self.assertEqual(extrema[1], extrema[2])


if __name__ == "__main__":
    unittest.main()
