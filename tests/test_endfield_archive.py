from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.endfield.archives.store import (
    ArchiveSnapshotStore,
    _dict_to_snapshot,
    _snapshot_to_dict,
)
from plugins.endfield.catalog.models import (
    ArchiveBaselineView,
    ArchiveDiffView,
    ArchiveItemView,
    ArchiveSnapshotView,
)
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.catalog.views.archives import build_akedata_archive_snapshot

endfield_service_module = importlib.import_module("plugins.endfield.catalog.service")


def _text_id(text: str) -> int:
    # 模拟 I18nTextTable_CN 的 int text-id 键（真实表含负数 id）
    return abs(hash(text)) or 1


def _i18n_entry(text: str) -> dict:
    return {"id": _text_id(text), "text": ""}


def _mini_tables():
    """迷你档案表：3 页签、4 分类、4 组、5 条目（含 1 条虚拟分类残留应被排除）。"""
    pages = {
        "page_document": {"pageType": "document", "name": _i18n_entry("中枢档案"), "icon": "a"},
        "page_media": {"pageType": "multi_media", "name": _i18n_entry("音像存档"), "icon": "b"},
        "page_text": {"pageType": "text", "name": _i18n_entry("见闻辑录"), "icon": "c"},
    }
    categories = {
        "cat_report": {"categoryId": "report", "name": _i18n_entry("调查报告"), "order": 2, "tabIcon": ""},
        "cat_document": {"categoryId": "document", "name": _i18n_entry("中枢档案"), "order": 1, "tabIcon": ""},
        "cat_media": {"categoryId": "media", "name": _i18n_entry("多媒体"), "order": 1, "tabIcon": ""},
        "cat_paper": {"categoryId": "paper", "name": _i18n_entry("纸质记录"), "order": 1, "tabIcon": ""},
    }
    first_lv = {
        "report_1": {
            "firstLvId": "report_1", "categoryId": "report", "itemIds": ["nar_r1", "nar_r2"],
            "name": _i18n_entry("萨米维格"), "subName": {"id": 0, "text": ""}, "order": 10, "icon": "g1",
        },
        "document_1": {
            "firstLvId": "document_1", "categoryId": "document", "itemIds": ["nar_d1"],
            "name": _i18n_entry("终末地物资准备纲要"), "subName": {"id": 0, "text": ""}, "order": 20, "icon": "g2",
        },
        "media_1": {
            "firstLvId": "media_1", "categoryId": "media", "itemIds": ["nar_m1"],
            "name": _i18n_entry("巡视记录影像"), "subName": _i18n_entry("第一辑"), "order": 30, "icon": "g3",
        },
        "paper_1": {
            "firstLvId": "paper_1", "categoryId": "paper", "itemIds": ["nar_p1"],
            "name": _i18n_entry("雪祀的信件草稿"), "subName": {"id": 0, "text": ""}, "order": 40, "icon": "g4",
        },
    }
    all_item = {
        "nar_r1": {"id": "nar_r1", "firstLvId": "report_1", "type": "document",
                   "name": _i18n_entry("萨米维格"), "order": 1, "desc": {"id": 0, "text": ""}},
        "nar_r2": {"id": "nar_r2", "firstLvId": "report_1", "type": "document",
                   "name": _i18n_entry("萨米维格·续"), "order": 2, "desc": {"id": 0, "text": ""}},
        "nar_d1": {"id": "nar_d1", "firstLvId": "document_1", "type": "document",
                   "name": _i18n_entry("终末地物资准备纲要"), "order": 1, "desc": {"id": 0, "text": ""}},
        "nar_m1": {"id": "nar_m1", "firstLvId": "media_1", "type": "multi_media",
                   "name": _i18n_entry("巡视记录影像·一"), "order": 1, "desc": {"id": 0, "text": ""}},
        "nar_p1": {"id": "nar_p1", "firstLvId": "paper_1", "type": "text",
                   "name": _i18n_entry("雪祀的信件草稿"), "order": 1, "desc": {"id": 0, "text": ""}},
        # type 不在 PrtsPage 三大页签内：虚拟分类残留，必须被排除
        "nar_virtual": {"id": "nar_virtual", "firstLvId": "paper_1", "type": "dialog",
                        "name": _i18n_entry("任务文本残留"), "order": 99, "desc": {"id": 0, "text": ""}},
    }
    i18n = {
        str(_text_id("中枢档案")): "中枢档案",
        str(_text_id("音像存档")): "音像存档",
        str(_text_id("见闻辑录")): "见闻辑录",
        str(_text_id("调查报告")): "调查报告",
        str(_text_id("多媒体")): "多媒体",
        str(_text_id("纸质记录")): "纸质记录",
        str(_text_id("萨米维格")): "萨米维格",
        str(_text_id("萨米维格·续")): "萨米维格·续",
        str(_text_id("终末地物资准备纲要")): "终末地物资准备纲要",
        str(_text_id("巡视记录影像")): "巡视记录影像",
        str(_text_id("巡视记录影像·一")): "巡视记录影像·一",
        str(_text_id("第一辑")): "第一辑",
        str(_text_id("雪祀的信件草稿")): "雪祀的信件草稿",
        str(_text_id("任务文本残留")): "任务文本残留",
    }
    return pages, categories, first_lv, all_item, i18n


def _make_item(item_id: str, *, name: str = "", page_name: str = "见闻辑录", **kw) -> ArchiveItemView:
    return ArchiveItemView(item_id=item_id, name=name or item_id, page_name=page_name, **kw)


def _make_snapshot(ids: list[str], *, version: str = "v") -> ArchiveSnapshotView:
    items = [_make_item(i) for i in ids]
    return ArchiveSnapshotView(
        items=items, version=version, total_count=len(items),
        page_counts={"见闻辑录": len(items)}, category_counts={"纸质记录": len(items)},
        group_count=len(items),
    )


class ArchiveSnapshotBuildTest(unittest.TestCase):
    def test_build_snapshot_localizes_and_filters(self):
        pages, categories, first_lv, all_item, i18n = _mini_tables()
        snap = build_akedata_archive_snapshot(
            pages, categories, first_lv, all_item, i18n,
            fetched_at=123, version_label="1.5",
        )
        # 虚拟分类残留（type=dialog）被排除，其余 5 条全部入册
        self.assertEqual(snap.total_count, 5)
        self.assertEqual(snap.version, "1.5")
        self.assertEqual(snap.fetched_at, 123)
        self.assertEqual(snap.source, "akedata")
        # 页签计数：中枢档案 = document(1) + report(2)；音像存档 = media(1)；见闻辑录 = paper(1)
        self.assertEqual(snap.page_counts, {"中枢档案": 3, "音像存档": 1, "见闻辑录": 1})
        self.assertEqual(snap.category_counts, {"中枢档案": 1, "调查报告": 2, "多媒体": 1, "纸质记录": 1})
        self.assertEqual(snap.group_count, 4)
        by_id = {item.item_id: item for item in snap.items}
        self.assertEqual(by_id["nar_r1"].page_name, "中枢档案")
        self.assertEqual(by_id["nar_r1"].category_name, "调查报告")
        self.assertEqual(by_id["nar_r1"].group_name, "萨米维格")
        self.assertEqual(by_id["nar_m1"].group_sub_name, "第一辑")
        self.assertNotIn("nar_virtual", by_id)
        # 游戏顺序：见闻辑录 → 音像存档 → 中枢档案；页内分类 order 优先于组 order。
        self.assertEqual(
            [item.item_id for item in snap.items],
            ["nar_p1", "nar_m1", "nar_d1", "nar_r1", "nar_r2"],
        )

    def test_build_snapshot_empty_tables(self):
        snap = build_akedata_archive_snapshot({}, {}, {}, {}, {}, version_label="1.5")
        self.assertEqual(snap.total_count, 0)
        self.assertEqual(snap.items, [])
        self.assertEqual(snap.page_counts, {})


class ArchiveStoreRoundTripTest(unittest.TestCase):
    def test_snapshot_dict_round_trip(self):
        pages, categories, first_lv, all_item, i18n = _mini_tables()
        snap = build_akedata_archive_snapshot(
            pages, categories, first_lv, all_item, i18n, version_label="1.5",
        )
        d = _snapshot_to_dict(snap)
        back = _dict_to_snapshot(d)
        self.assertEqual(back.version, "1.5")
        self.assertEqual(back.total_count, snap.total_count)
        self.assertEqual(back.page_counts, snap.page_counts)
        self.assertEqual(back.category_counts, snap.category_counts)
        self.assertEqual(back.group_count, snap.group_count)
        self.assertEqual(len(back.items), len(snap.items))
        self.assertEqual(back.items[0], snap.items[0])

    def test_field_filtering_ignores_unknown_keys(self):
        raw = {"items": [{"item_id": "a", "name": "A", "future_field": "x"}],
               "version": "v", "total_count": 1, "page_counts": {"见闻辑录": 1}}
        snap = _dict_to_snapshot(raw)
        self.assertEqual(len(snap.items), 1)
        self.assertEqual(snap.items[0].item_id, "a")
        self.assertEqual(snap.page_counts, {"见闻辑录": 1})

    def test_empty_dict(self):
        snap = _dict_to_snapshot({})
        self.assertEqual(snap.items, [])
        self.assertEqual(snap.version, "")


class ArchiveSnapshotStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_and_baseline_stored_independently(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "snap.json")
            store = ArchiveSnapshotStore(path)
            self.assertIsNone(store.load_current_view())
            self.assertIsNone(store.load_baseline_view())

            await store.replace_current(_make_snapshot(["nar_a", "nar_b"], version="1.5"))
            await store.replace_current(_make_snapshot(["nar_a", "nar_b", "nar_c"], version="1.5"))
            cur = store.load_current_view()
            self.assertEqual(cur.version, "1.5")
            self.assertEqual({i.item_id for i in cur.items}, {"nar_a", "nar_b", "nar_c"})

            await store.replace_baseline(ArchiveBaselineView(version="1.4", ids=["nar_a", "nar_b"]))
            bl = store.load_baseline_view()
            self.assertIsNotNone(bl)
            self.assertEqual(bl.version, "1.4")
            self.assertEqual(set(bl.ids), {"nar_a", "nar_b"})
            self.assertEqual(cur.total_count, 3)

            await store.replace_baseline(None)
            self.assertIsNone(store.load_baseline_view())

    async def test_current_and_baseline_persisted_together(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "snap.json")
            store = ArchiveSnapshotStore(path)
            await store.replace_current_and_baseline(
                _make_snapshot(["nar_new"], version="1.5"),
                ArchiveBaselineView(version="1.4", ids=["nar_old"]),
            )
            reopened = ArchiveSnapshotStore(path)  # 模拟进程重启
            self.assertEqual(reopened.load_current_view().version, "1.5")
            self.assertEqual(reopened.load_baseline_view().version, "1.4")


class ArchiveServiceTest(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> EndfieldService:
        return EndfieldService(AsyncMock())

    def test_build_archive_diff(self):
        service = self._service()
        current = _make_snapshot(["nar_a", "nar_b", "nar_c"], version="1.5")
        baseline = ArchiveBaselineView(version="1.4", ids=["nar_a"])
        diff = service.build_archive_diff(current, baseline)
        self.assertEqual(diff.previous_version, "1.4")
        self.assertEqual([i.item_id for i in diff.new_items], ["nar_b", "nar_c"])

        # 无基线时新增为空
        empty = service.build_archive_diff(current, None)
        self.assertEqual(empty.previous_version, "")
        self.assertEqual(empty.new_items, [])

    def test_build_archive_progress_view(self):
        service = self._service()
        snapshot = _make_snapshot(["nar_a", "nar_b", "nar_c"], version="1.5")
        raw = {"data": {"detail": {"base": {"docNum": 2}}}}
        view = service.build_archive_progress_view(
            raw, snapshot, nickname="管理员", uid="12****89", server_name="国服",
        )
        self.assertEqual(view.collected, 2)
        self.assertEqual(view.total_count, 3)
        self.assertEqual(view.missing, 1)
        self.assertFalse(view.over_total)
        self.assertEqual(view.page_counts, {"见闻辑录": 3})

    def test_build_archive_progress_view_over_total(self):
        service = self._service()
        snapshot = _make_snapshot(["nar_a"], version="1.5")
        raw = {"data": {"detail": {"base": {"docNum": 9}}}}
        view = service.build_archive_progress_view(raw, snapshot, nickname="x", uid="y", server_name="国服")
        self.assertTrue(view.over_total)
        self.assertEqual(view.missing, 0)  # 不输出负数

    async def test_fetch_archive_snapshot_rejects_incomplete(self):
        service = self._service()
        pages, categories, first_lv, all_item, i18n = _mini_tables()
        # 表里塞 3 条虚拟分类残留（type 不在任何页签）：5/8 = 62.5% < 80%，必须拒绝落盘
        for index in range(3):
            virtual_id = f"nar_virtual_{index}"
            all_item[virtual_id] = {
                "id": virtual_id, "firstLvId": "paper_1", "type": "dialog",
                "name": _i18n_entry("任务文本残留"), "order": 90 + index,
                "desc": {"id": 0, "text": ""},
            }
        tables = (pages, categories, first_lv, all_item, i18n, "1.5.3@test-1")
        with patch.object(
            endfield_service_module, "fetch_akedata_archive_tables", new=AsyncMock(return_value=tables)
        ), patch.object(
            endfield_service_module, "game_version_label", return_value="1.5",
        ):
            with self.assertRaises(ValueError):
                await service.fetch_archive_snapshot_akedata()

    async def test_fetch_archive_snapshot_ok(self):
        service = self._service()
        pages, categories, first_lv, all_item, i18n = _mini_tables()
        tables = (pages, categories, first_lv, all_item, i18n, "1.5.3@test-1")
        with patch.object(
            endfield_service_module, "fetch_akedata_archive_tables", new=AsyncMock(return_value=tables)
        ), patch.object(
            endfield_service_module, "game_version_label", return_value="1.5",
        ):
            snap = await service.fetch_archive_snapshot_akedata()
        self.assertEqual(snap.total_count, 5)
        self.assertEqual(snap.version, "1.5")
        self.assertEqual(snap.page_counts, {"中枢档案": 3, "音像存档": 1, "见闻辑录": 1})

    async def test_fetch_archive_baseline(self):
        service = self._service()
        manifest = {
            "latest": "1.5.3@9885010-4",
            "versions": [
                {"id": "1.5.3@9885010-4", "tableCfgPath": "public/1.5.3/9885010-4/TableCfg"},
                {"id": "1.5.1@9880010-2", "tableCfgPath": "public/1.5.1/9880010-2/TableCfg"},
                {"id": "1.4.4@9599201-14", "tableCfgPath": "public/1.4.4/9599201-14/TableCfg"},
            ],
        }
        prev_table = {"nar_old": {"id": "nar_old"}, "nar_keep": {"id": "nar_keep"}}
        with patch.object(
            endfield_service_module, "fetch_akedata_manifest", new=AsyncMock(return_value=manifest)
        ), patch.object(
            endfield_service_module, "fetch_akedata_prts_all_item", new=AsyncMock(return_value=prev_table)
        ):
            baseline = await service.fetch_archive_baseline()
        self.assertEqual(baseline.version, "1.4")
        self.assertEqual(baseline.version_id, "1.4.4@9599201-14")
        self.assertEqual(set(baseline.ids), {"nar_old", "nar_keep"})


class ArchivePreviewTest(unittest.IsolatedAsyncioTestCase):
    def test_unused_budget_completes_other_page_row(self):
        from plugins.endfield.rendering import cards

        items = []
        for page, category, count in (
            ("见闻辑录", "paper", 26), ("见闻辑录", "digital", 10), ("见闻辑录", "collection", 2),
            ("音像存档", "media", 6), ("中枢档案", "document", 12), ("中枢档案", "report", 2),
        ):
            items.extend(_make_item(f"{category}_{i}", page_name=page, category_id=category) for i in range(count))
        with patch.object(cards, "ARCHIVE_GRID_COLUMNS", 6):
            selected = cards._archive_preview_items(items, 24)
        self.assertEqual(len(selected), 24)
        self.assertEqual(
            [sum(item.page_name == page for item in selected) for page in ("见闻辑录", "音像存档", "中枢档案")],
            [12, 6, 6],
        )

    async def test_partial_pages_fit_rows_without_losing_small_categories(self):
        from plugins.endfield.rendering import cards

        items = [_make_item(f"paper_{i}", category_id="paper") for i in range(26)]
        items += [_make_item("digital", category_id="digital"), _make_item("collection", category_id="collection")]
        items += [_make_item("audio", page_name="音像存档", category_id="media")]
        items += [_make_item(f"document_{i}", page_name="中枢档案", category_id="document") for i in range(3)]
        items += [_make_item("report", page_name="中枢档案", category_id="report")]
        for columns, expected in ((4, 16), (6, 18), (8, 16)):
            with self.subTest(columns=columns), patch.object(cards, "ARCHIVE_GRID_COLUMNS", columns):
                selected = cards._archive_preview_items(items, 24)
                self.assertEqual(sum(item.page_name == "见闻辑录" for item in selected), expected)
                self.assertEqual(len(selected), expected + 5)
                self.assertIn(items[26], selected)  # 电子档案
                self.assertIn(items[27], selected)  # 藏品
                self.assertEqual(selected, [item for item in items if item in selected])
                # 全量已能展示时，不为凑整行隐藏真实新增。
                self.assertEqual(cards._archive_preview_items(items, 40), items)

        renderer = AsyncMock(return_value=b"png")
        view = ArchiveDiffView(current=_make_snapshot([item.item_id for item in items]), previous_version="1.4", new_items=items)
        with (
            patch.object(cards, "ARCHIVE_PREVIEW_LIMIT", 24),
            patch.object(cards, "ARCHIVE_GRID_COLUMNS", 6),
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})),
            patch.object(cards, "_draw_neutral_card", renderer),
        ):
            await cards.draw_archive_stats_card(view)
        body = renderer.call_args.args[1]
        self.assertIn("展示 23 / 33 条", body)
        self.assertIn("其余 10 条未展示", body)
        self.assertEqual(body.count('class="archive-item"'), 23)

    def test_preview_keeps_small_categories_and_original_order(self):
        from plugins.endfield.rendering.cards import _archive_preview_items

        items = [_make_item(f"paper_{i}", category_id="paper") for i in range(30)]
        items += [_make_item("audio", page_name="音像存档", category_id="media")]
        items += [_make_item("report", page_name="中枢档案", category_id="report")]
        selected = _archive_preview_items(items, 6)
        self.assertEqual([item.item_id for item in selected], ["paper_0", "paper_1", "paper_2", "paper_3", "audio", "report"])
        self.assertEqual(len(items), 32)
        self.assertEqual(_archive_preview_items(items, 0), [])
        self.assertEqual(_archive_preview_items(items, 100), items)

    async def test_truncated_preview_keeps_totals_and_handles_missing_icons(self):
        from plugins.endfield.rendering import cards

        items = [_make_item(f"id_{i}", name=f"档案名称{i}", icon_url=f"https://example.test/{i}.png") for i in range(5)]
        # 名称即组名：组名不可再次出现在物品卡中。
        items[0].group_name = items[0].name
        view = ArchiveDiffView(current=_make_snapshot([f"id_{i}" for i in range(10)]), previous_version="1.4", new_items=items)
        renderer = AsyncMock(return_value=b"png")
        with (
            patch.object(cards, "ARCHIVE_PREVIEW_LIMIT", 2),
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={items[0].icon_url: "data:image/png;base64,test"})) as assets,
            patch.object(cards, "_draw_neutral_card", renderer),
        ):
            self.assertEqual(await cards.draw_archive_stats_card(view), (b"png",))
        body = renderer.call_args.args[1]
        self.assertIn("档案总数</span><strong>10</strong>", body)
        self.assertIn("本版本新增</span><strong>5</strong>", body)
        self.assertIn("展示 2 / 5 条", body)
        self.assertIn("其余 3 条未展示", body)
        self.assertEqual(body.count(items[0].name), 1)
        self.assertNotIn(items[2].name, body)
        self.assertIn("暂无图像", body)
        self.assertIn("data:image/png;base64,test", body)
        requested = assets.call_args.args[0]
        self.assertNotIn(items[2].icon_url, requested)

    async def test_pagination_fetches_icons_once_and_preserves_preview(self):
        from plugins.endfield.rendering import cards

        items = [_make_item(f"id_{i}") for i in range(5)]
        view = ArchiveDiffView(new_items=items)
        failure = RuntimeError("Screenshot element height 999 exceeds limit 100")
        with (
            patch.object(cards, "ARCHIVE_PREVIEW_LIMIT", 5),
            patch.object(cards, "ARCHIVE_PAGE_BUDGETS", (3, 2)),
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})) as assets,
            patch.object(cards, "_draw_archive_stats_page", AsyncMock(side_effect=[failure, failure, b"1", b"2", b"3"])) as draw,
        ):
            self.assertEqual(await cards.draw_archive_stats_card(view), (b"1", b"2", b"3"))
        assets.assert_awaited_once()
        rendered = [item for call in draw.await_args_list[-3:] for item in call.args[1]]
        self.assertEqual(rendered, items)


if __name__ == "__main__":
    unittest.main()
