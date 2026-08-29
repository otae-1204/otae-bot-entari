from __future__ import annotations

import asyncio
import unittest

from plugins.endfield import ownership_stats_draw
from plugins.endfield.ownership_stats import (
    CollectionSummary,
    OperatorOwnership,
    OwnershipRefreshResult,
    OwnershipStatsReport,
    OwnershipStatsSegment,
    PotentialBucket,
)


NOW = 1_756_300_000


def _buckets(counts: dict[str, int], sample: int) -> tuple[PotentialBucket, ...]:
    order = ["unowned", *[f"potential_{level}" for level in range(6)], "unknown"]
    labels = {"unowned": "未持有", "unknown": "未知", **{f"potential_{i}": f"潜能 {i}" for i in range(6)}}
    return tuple(
        PotentialBucket(
            key=key,
            label=labels[key],
            count=counts.get(key, 0),
            rate=counts.get(key, 0) / sample if sample else None,
        )
        for key in order
    )


def _operator(
    name: str,
    *,
    source_id: str = "chr_0001_demo",
    rarity: int = 6,
    profession: str = "先锋",
    owned: int = 8,
    sample: int = 12,
    potential_level: int = 2,
) -> OperatorOwnership:
    counts: dict[str, int] = {key: 0 for key in ("unowned", *[f"potential_{i}" for i in range(6)], "unknown")}
    counts["unowned"] = sample - owned
    if owned:
        counts[f"potential_{potential_level}"] = owned
    return OperatorOwnership(
        operator_key=f"key-{name}",
        source_id=source_id,
        name=name,
        rarity=rarity,
        profession=profession,
        sort_order=1,
        owned_count=owned,
        sample_count=sample,
        ownership_rate=owned / sample if sample else None,
        potential_buckets=_buckets(counts, sample),
    )


def _segment(
    region: str,
    operators: tuple[OperatorOwnership, ...],
    *,
    eligible: int | None = None,
) -> OwnershipStatsSegment:
    sample = operators[0].sample_count if operators else 0
    return OwnershipStatsSegment(
        region=region,
        eligible_sample_count=sample if eligible is None else eligible,
        valid_sample_count=sample,
        excluded_sample_count=(0 if eligible is None else max(0, eligible - sample)),
        operators=operators,
        professions=(
            CollectionSummary("profession", "先锋", len(operators), sum(op.owned_count for op in operators), sample * len(operators) if sample else 0, None if not sample else sum(op.owned_count for op in operators) / (sample * len(operators))),
        ),
        rarities=(
            CollectionSummary("rarity", "6", len(operators), sum(op.owned_count for op in operators), sample * len(operators) if sample else 0, None if not sample else sum(op.owned_count for op in operators) / (sample * len(operators))),
        ),
    )


def _report(segments: tuple[OwnershipStatsSegment, ...], **kwargs) -> OwnershipStatsReport:
    defaults = dict(
        scope="global",
        generated_at=NOW,
        catalog_version="v20260801",
        snapshot_updated_at=NOW - 3600,
        segments=segments,
        refresh=None,
    )
    defaults.update(kwargs)
    return OwnershipStatsReport(**defaults)


def _html(report: OwnershipStatsReport, **kwargs) -> str:
    # 默认空 icon_map:离线渲染,不触发头像下载;需要头像时显式传入映射。
    kwargs.setdefault("icon_map", {})
    return asyncio.run(ownership_stats_draw.render_ownership_stats_html(report, **kwargs))


class OwnershipStatsDrawTests(unittest.TestCase):
    def test_renders_scope_meta_and_samples(self):
        report = _report(
            (
                _segment("all", (_operator("甲", owned=8), _operator("乙", owned=4, rarity=5, profession="术师"))),
                _segment("cn", (_operator("甲", owned=8),)),
                _segment("asia", (), eligible=3),
            )
        )
        html = _html(report)
        self.assertIn("干员持有率统计", html)
        self.assertIn("全局统计", html)
        self.assertIn("AKEData 目录 v20260801", html)
        self.assertIn("快照更新", html)
        self.assertIn("合格 12 · 已排除 0", html)

    def test_header_shows_snapshot_time_and_drops_notes(self):
        report = _report((_segment("all", (_operator("甲",),)),))
        html = _html(report)
        # 页头显示快照时间而非注释;口径说明、排序说明、页脚均已移除。
        self.assertIn("快照更新", html)
        self.assertNotIn("个人身份信息", html)
        self.assertNotIn("先按稀有度分块", html)
        self.assertNotIn("有效快照 =", html)
        self.assertNotIn("ownership-footer", html)
        self.assertEqual(_html(_report((_segment("all", (_operator("甲",),)),), snapshot_updated_at=None)).count("暂无有效快照"), 1)

    def test_empty_catalog_version_falls_back(self):
        report = _report((_segment("all", (_operator("甲",),)),), catalog_version="")
        self.assertIn("AKEData 目录不可用", _html(report))

    def test_operators_keep_report_order_and_rates(self):
        operators = (
            _operator("甲", owned=10),
            _operator("乙", owned=6, rarity=5),
            _operator("丙", owned=2, rarity=5),
        )
        html = _html(_report((_segment("all", operators),)))
        first = html.index("甲")
        self.assertLess(first, html.index("乙"))
        self.assertLess(html.index("乙"), html.index("丙"))
        self.assertIn("83.3%", html)
        self.assertIn("50.0%", html)
        self.assertIn("16.7%", html)
        self.assertIn("10 人持有", html)
        # 列表按稀有度分块展示(顺序仍为后端排序)。
        self.assertIn("6星干员 · 1 名", html)
        self.assertIn("5星干员 · 2 名", html)

    def test_empty_segment_shows_note_without_operator_rows(self):
        html = _html(_report((_segment("asia", (), eligible=5),)))
        self.assertIn("亚服 · 暂无有效样本", html)
        self.assertIn("合格 5", html)
        self.assertNotIn('class="op-row', html)
        self.assertNotIn("0.0%", html)

    def test_none_rate_renders_placeholder_not_zero(self):
        operators = (_operator("甲", owned=0, sample=0),)
        html = _html(_report((_segment("all", operators),)))
        self.assertIn("无有效样本", html)
        self.assertNotIn("0.0%", html)
        self.assertNotIn("0 人持有", html)

    def test_refresh_strip_only_with_refresh(self):
        segment = _segment("all", (_operator("甲",),))
        with_refresh = _report(
            (segment,),
            refresh=OwnershipRefreshResult(
                attempted=12,
                succeeded=10,
                failed=1,
                skipped=1,
                catalog_updated=True,
                started_at=NOW,
                finished_at=NOW + 45,
            ),
        )
        html = _html(with_refresh)
        self.assertIn("刷新批次", html)
        self.assertIn("目录已更新", html)
        self.assertIn("45 秒", html)
        self.assertNotIn("刷新批次", _html(_report((segment,))))

    def test_avatars_use_akedata_cdn_and_initial_fallback(self):
        operators = (_operator("甲", source_id="chr_0001_demo"), _operator("乙", source_id=""))
        url = ownership_stats_draw.operator_avatar_url(operators[0])
        html = _html(_report((_segment("all", operators),)), icon_map={url: url})
        self.assertIn(f'src="{url}"', html)
        self.assertIn("op-avatar-fallback", html)

    def test_operator_avatar_url_requires_source_id(self):
        self.assertIn("icon_chr_0001_demo", ownership_stats_draw.operator_avatar_url(_operator("甲")))
        self.assertEqual(ownership_stats_draw.operator_avatar_url(_operator("甲", source_id="")), "")

    def test_escapes_operator_names(self):
        html = _html(_report((_segment("all", (_operator('<b>"x"&y</b>'),)),)))
        self.assertNotIn('<b>"x"&y</b></span>', html)
        self.assertIn("&lt;b&gt;", html)

    def test_profession_and_rarity_summaries(self):
        html = _html(_report((_segment("all", (_operator("甲", owned=6), _operator("乙", owned=3, rarity=5, profession="术师"))),)))
        self.assertIn("按职业平均收集率", html)
        self.assertIn("按稀有度平均收集率", html)
        self.assertIn("6★", html)
        self.assertIn("37.5%", html)

    def test_overview_six_star_collection_donuts(self):
        # 夹具名与真实目录对齐:管理员 6★、别礼/骏卫 6★常驻、汤汤 6★非常驻、佩丽卡 5★。
        operators = (
            _operator("管理员", owned=12, potential_level=1, rarity=6),
            _operator("别礼", owned=12, potential_level=1, rarity=6),
            _operator("骏卫", owned=6, potential_level=0, rarity=6),
            _operator("汤汤", owned=3, potential_level=2, rarity=6),
            _operator("佩丽卡", owned=4, rarity=5),
        )
        html = _html(_report((_segment("all", operators),)))
        for title in ("6星干员收集率", "6星非常驻干员收集率", "6星干员潜能分布", "6星非常驻干员潜能分布"):
            self.assertIn(title, html)
        # 6星不含管理员:21/36 = 58.3%(金色 210°);非常驻仅汤汤:3/12 = 25.0%(金色 90°)。
        self.assertIn("#ffd000 0.00deg 210.00deg", html)
        self.assertIn("#d9dde0 210.00deg 360.00deg", html)
        self.assertIn("58.3%", html)
        self.assertIn("#ffd000 0.00deg 90.00deg", html)
        self.assertIn("25.0%", html)
        # 管理员不参与饼图,但仍作为数据行展示在持有率列表中。
        self.assertIn("管理员", html)
        self.assertIn("100.0%", html)
        # 环形图下方的副标题注释已移除。
        self.assertNotIn("donut-sub", html)
        # 佩丽卡是 5★,不参与 6星概况。
        self.assertNotIn("5 名干员 · 60 槽位", html)

    def test_six_star_potential_donut_uses_owned_slots(self):
        operators = (
            _operator("管理员", owned=12, potential_level=1, rarity=6),
            _operator("别礼", owned=12, potential_level=1, rarity=6),
            _operator("骏卫", owned=6, potential_level=0, rarity=6),
            _operator("汤汤", owned=3, potential_level=2, rarity=6),
        )
        html = _html(_report((_segment("all", operators),)))
        # 管理员不计入;6星已持有 21 格:0潜 6/21、1潜 12/21、2潜 3/21,未持有格不参与潜能分布。
        self.assertIn("#c9d9e6 0.00deg 102.86deg", html)
        self.assertIn("#a6c4da 102.86deg 308.57deg", html)
        self.assertIn("#7fa9cc 308.57deg 360.00deg", html)
        self.assertIn("满潜率", html)
        # 所有标注外移并带引线,文本为"潜能名 + 占比";1潜、2潜扇区居左。
        self.assertEqual(html.count('class="donut-leader"'), 4)
        self.assertEqual(html.count('class="donut-slice-label"'), 2)
        self.assertEqual(html.count('class="donut-slice-label donut-slice-left"'), 2)
        self.assertIn("0潜 29%", html)
        self.assertIn("1潜 57%", html)
        self.assertIn("2潜 14%", html)
        self.assertIn("2潜 100%", html)
        # 环下注释已移除。
        self.assertNotIn("已持有 33 槽位", html)

    def test_single_region_report_hides_duplicate_all_section(self):
        cn_segment = _segment("cn", (_operator("甲",),))
        asia_empty = OwnershipStatsSegment("asia", 0, 0, 0, (), (), ())
        report = _report((_segment("all", (_operator("甲",),)), cn_segment, asia_empty))
        html = _html(report)
        # 亚服无样本时总计段与国服重复,只保留国服;亚服保留空态说明。
        self.assertNotIn("总计干员持有率", html)
        self.assertIn("国服干员持有率", html)
        self.assertIn("亚服 · 暂无有效样本", html)
        # 两个区域都有样本时,总计段照常展示。
        full = _report(
            (
                _segment("all", (_operator("甲",),)),
                cn_segment,
                _segment("asia", (_operator("乙", rarity=5),)),
            )
        )
        full_html = _html(full)
        self.assertIn("总计干员持有率", full_html)
        self.assertIn("国服干员持有率", full_html)
        self.assertIn("亚服干员持有率", full_html)

    def test_thin_potential_slices_get_outside_labels(self):
        buckets = _buckets({"unowned": 50, "potential_2": 47, "potential_5": 2, "unknown": 1}, 100)
        operator = OperatorOwnership(
            operator_key="k", source_id="chr_x", name="汤汤", rarity=6,
            profession="术师", sort_order=1, owned_count=50, sample_count=100,
            ownership_rate=0.5, potential_buckets=buckets,
        )
        html = _html(_report((_segment("all", (operator,)),)))
        # 标注全部外移:4% 与 2% 的薄扇区引线连到环外,文本附潜能名。
        self.assertIn("donut-slice-left", html)
        self.assertIn("5潜 4%", html)
        self.assertIn("未知 2%", html)

    def test_overview_donuts_placeholder_without_valid_samples(self):
        html = _html(_report((_segment("asia", (), eligible=5),)))
        self.assertEqual(html.count('class="donut donut-empty"'), 4)
        self.assertNotIn("conic-gradient", html)

    def test_legend_lists_all_buckets(self):
        html = _html(_report((_segment("all", (_operator("甲",),)),)))
        for label in ("未持有", "0潜", "1潜", "5潜", "未知"):
            self.assertIn(label, html)

    def test_draw_registers_renderer_without_recoupling_commands(self):
        # 展示层通过 register_ownership_stats_renderer 接入,命令层只认 render_ownership_stats。
        self.assertTrue(callable(ownership_stats_draw.draw_ownership_stats))


if __name__ == "__main__":
    unittest.main()
