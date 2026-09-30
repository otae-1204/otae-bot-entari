from __future__ import annotations

import re
import unittest
from unittest.mock import AsyncMock, patch

from lxml import html

from plugins.endfield.catalog.models import (
    MedalDiffView,
    MedalItemView,
    MedalMissingView,
    MedalSnapshotView,
    MedalWallItemView,
)
from plugins.endfield.rendering import cards


class MedalMissingPaginationTest(unittest.IsolatedAsyncioTestCase):
    async def test_height_fallback_preserves_group_order_images_and_full_copy(self):
        medals = [
            MedalItemView(
                medal_id=f"medal-{index}",
                name=f"奖章 {index}",
                icon_url=f"icon-{index}",
                description=f"描述 <{index}> & " + "长文案。" * 20,
                condition=f"条件 {index}：" + "完成指定任务；" * 20,
                next_icon_url=f"next-{index}" if index >= 4 else "",
                next_description=f"下一档描述 {index}" if index >= 4 else "",
                next_condition=f"下一档条件 {index}" if index >= 4 else "",
            )
            for index in range(12)
        ]
        view = MedalMissingView(
            not_obtained=medals[:4], not_maxed=medals[4:8], not_plated=medals[8:],
            not_obtained_count=4, not_maxed_count=4, not_plated_count=4,
            total_count=100, owned_count=96, shown_count=12,
        )
        icons = {url: f"cached-{url}" for medal in medals for url in (medal.icon_url, medal.next_icon_url) if url}

        async def render(_selector, body, **_kwargs):
            document = html.fromstring(body)
            item_count = len(document.xpath('//*[@class="medal-item" or @class="medal-upgrade"]'))
            if item_count > 5:
                raise RuntimeError("Screenshot element height 7000px exceeds limit 6144px")
            return body.encode()

        with (
            patch.object(cards, "_image_data_urls", AsyncMock(return_value=icons)) as assets,
            patch.object(cards, "_draw_neutral_card", side_effect=render),
        ):
            pages = await cards.draw_medal_missing_card(view)

        self.assertEqual(len(pages), 3)
        assets.assert_awaited_once()
        descriptions, conditions, displayed_icons = [], [], []
        for index, page in enumerate(pages, start=1):
            document = html.fromstring(page.decode())
            self.assertIn(f"第 {index}/3 页", document.text_content())
            self.assertIn("已展示 12", document.text_content())
            self.assertEqual(document.xpath('//*[@class="tile primary"]/strong/text()'), ["96"])
            descriptions.extend(node.text_content() for node in document.xpath('//*[@class="medal-desc"]'))
            conditions.extend(node.text_content() for node in document.xpath('//*[@class="medal-cond"]'))
            displayed_icons.extend(document.xpath('//*[@class="medal-icon"]/img/@src'))

        self.assertEqual(descriptions, [text for medal in medals for text in (medal.description, medal.next_description) if text])
        self.assertEqual(conditions, [text for medal in medals for text in (medal.condition, medal.next_condition) if text])
        self.assertEqual(displayed_icons, [icons[url] for medal in medals for url in (medal.icon_url, medal.next_icon_url) if url])
        self.assertIn("镀层后", pages[-1].decode())
        last = html.fromstring(pages[-1].decode())
        self.assertEqual(set(last.xpath('//*[@class="medal-upgrade"]/@data-kind')), {"plating"})
        self.assertEqual(last.xpath('//section[@data-group="plate"]/h2/text()'), ["未镀层"])
        self.assertNotIn("获取条件", b"".join(pages).decode())
        self.assertNotIn("镀层条件", b"".join(pages).decode())
        self.assertEqual(view.not_obtained, medals[:4])
        self.assertEqual(view.not_maxed, medals[4:8])
        self.assertEqual(view.not_plated, medals[8:])

    async def test_other_render_errors_do_not_trigger_pagination(self):
        with (
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})),
            patch.object(cards, "_draw_neutral_card", AsyncMock(side_effect=RuntimeError("browser closed"))) as render,
            self.assertRaisesRegex(RuntimeError, "browser closed"),
        ):
            await cards.draw_medal_missing_card(MedalMissingView())
        render.assert_awaited_once()

    async def test_single_oversize_medal_raises_without_dropping_text(self):
        view = MedalMissingView(not_obtained=[MedalItemView(medal_id="one", name="单枚")])
        with (
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})),
            patch.object(cards, "_draw_neutral_card", AsyncMock(side_effect=RuntimeError(
                "Screenshot element height 7000px exceeds limit 6144px",
            ))) as render,
            self.assertRaisesRegex(RuntimeError, "exceeds limit"),
        ):
            await cards.draw_medal_missing_card(view)
        render.assert_awaited_once()


class MedalHeaderScopeTest(unittest.IsolatedAsyncioTestCase):
    """F1/F2 共享 .medal-header 骨架；单卡专属样式必须挂在各自修饰类下。"""

    async def test_stats_header_uses_its_own_modifier(self):
        view = MedalDiffView(current=MedalSnapshotView(version="1.5", total_count=3))
        with (
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})),
            patch.object(cards, "_draw_neutral_card", AsyncMock(return_value=b"page")) as render,
        ):
            await cards.draw_medal_stats_card(view)
        header = html.fromstring(render.await_args.args[1]).xpath("//header")[0]
        self.assertEqual(header.get("class"), "medal-header medal-header--stats")
        self.assertEqual(header.xpath('.//*[@class="medal-head-version"]/strong/text()'), ["1.5"])

    def test_card_specific_rules_are_scoped(self):
        rules = [rule.split("{", 1)[0] for rule in cards.MEDAL_CARD_CSS.splitlines() if "{" in rule]
        for selector in rules:
            if "medal-head-version" in selector:
                self.assertIn(".medal-header--stats", selector)
            if "medal-chip" in selector or "medal-gaps" in selector:
                self.assertIn(".medal-header--missing", selector)
        base = next(r for r in cards.MEDAL_CARD_CSS.splitlines() if r.startswith(".medal-header{"))
        self.assertIn("border-radius:0", base)
        self.assertNotIn("text-shadow", cards.MEDAL_CARD_CSS)


class MedalWallLayoutTest(unittest.IsolatedAsyncioTestCase):
    """奖章墙蜂窝排布：槽位序号即坐标，奇数槽位上排、偶数槽位下排（2026-09-29 逐格核对游戏截图）。

    坐标由内联几何给出，墙面/空槽样式由 MEDAL_CARD_CSS 注入。
    """

    def _wall(self, slots) -> list[MedalWallItemView]:
        return [
            MedalWallItemView(slot=slot, name=f"章{slot}", icon_url=f"icon-{slot}")
            for slot in slots
        ]

    @staticmethod
    def _cells(markup: str):
        return html.fromstring(markup).xpath('//ul[@class="medal-wall-grid"]/li')

    @staticmethod
    def _style_value(cell, prop: str) -> str:
        match = re.search(rf"(?:^|;){prop}:([^;]+)", cell.get("style") or "")
        return match.group(1) if match else ""

    async def test_odd_slots_are_upper_row_and_even_slots_lower_row(self):
        wall = self._wall(range(1, 11))
        markup = cards._medal_wall_html(wall, {f"icon-{s}": f"cached-{s}" for s in range(1, 11)})
        cells = self._cells(markup)

        self.assertEqual(len(cells), 10)
        # 奇上偶下：上排 top=0，下排 top=行间距
        for cell, item in zip(cells, wall):
            expected = "0px" if item.slot % 2 == 1 else f"{cards.MEDAL_WALL_ROW_HEIGHT}px"
            self.assertEqual(
                self._style_value(cell, "top"), expected, f"槽位 {item.slot} 的排位置不对"
            )
        # 列号 = (slot-1)//2，下排再右错半个步距（同列两格是相邻的一对）
        self.assertEqual(
            [self._style_value(cell, "left") for cell in cells],
            [
                f"{cards.MEDAL_WALL_STRIDE * ((s - 1) // 2) + cards.MEDAL_WALL_ROW_INDENT * ((s - 1) % 2)}px"
                for s in range(1, 11)
            ],
        )

    async def test_wall_icon_keeps_square_canvas(self):
        """方形原图居中，不能被拉伸到非方形格子；六边形只用于凹槽背景。"""
        wall = self._wall([1])
        markup = cards._medal_wall_html(wall, {"icon-1": "cached-1"})
        icon = self._cells(markup)[0].xpath(".//img")[0]
        self.assertEqual(self._style_value(icon, "width"), self._style_value(icon, "height"))
        self.assertIn("clip-path:polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%)", cards.MEDAL_CARD_CSS)
        # 尖顶六边形高大于宽。
        self.assertGreater(
            cards.MEDAL_WALL_ITEM_HEIGHT / cards.MEDAL_WALL_ITEM_WIDTH, 1.0
        )

    async def test_wall_grid_scales_with_constants_and_has_no_label(self):
        markup = cards._medal_wall_html(self._wall(range(1, 11)), {})
        grid = html.fromstring(markup).xpath('//ul[@class="medal-wall-grid"]')[0]
        style = grid.get("style")
        expected_width = (
            cards.MEDAL_WALL_STRIDE * (cards.MEDAL_WALL_COLUMNS - 1)
            + cards.MEDAL_WALL_ROW_INDENT
            + cards.MEDAL_WALL_ITEM_WIDTH
        )
        self.assertIn(f"width:{expected_width}px", style)
        # 两排：高度 = 行间距 + 单格高
        self.assertIn(f"height:{cards.MEDAL_WALL_ROW_HEIGHT + cards.MEDAL_WALL_ITEM_HEIGHT}px", style)
        # 需求：卡片里不加「勋章展示墙」标题
        self.assertNotIn("勋章展示墙", markup)

    async def test_wall_fills_all_display_slots_and_labels_medals(self):
        """展示位上限 10：只配 3 枚时其余 7 格为凹槽，禁止再次使用错误截图。"""
        wall = self._wall([1, 2, 3])
        markup = cards._medal_wall_html(
            wall,
            {
                "icon-1": "data:image/png;base64,AAA",
                "icon-2": "cached-2",
                "icon-3": "cached-3",
            },
        )
        cells = self._cells(markup)
        self.assertEqual(len(cells), 10)
        self.assertEqual(
            [cell.get("title") for cell in cells], ["章1", "章2", "章3"] + ["未设置奖章"] * 7
        )
        images = [cell.xpath(".//img/@src") for cell in cells]
        self.assertEqual(images[0], ["data:image/png;base64,AAA"])
        self.assertEqual(images[1], ["cached-2"])
        self.assertTrue(all(not src for src in images[3:]))
        self.assertTrue(all(cell.get("data-state") == "empty" for cell in cells[3:]))
        self.assertTrue(all(cell.xpath('.//*[@class="medal-wall-recess"]') for cell in cells[3:]))

    async def test_missing_medal_icon_is_distinct_from_unused_slot(self):
        """已设置的章缺图时仍保留槽位和名字，并标记缺图，不伪装成未设置。"""
        markup = cards._medal_wall_html(self._wall([1]), {"icon-1": ""})
        first = self._cells(markup)[0]
        self.assertFalse(first.xpath(".//img"))
        self.assertEqual(first.get("data-state"), "unavailable")
        self.assertEqual(first.get("title"), "章1")
        self.assertIn("图标暂缺", first.text_content())

    async def test_out_of_range_slot_cannot_collapse_wall_height(self):
        markup = cards._medal_wall_html(self._wall([1, 11]), {})
        grid = html.fromstring(markup).xpath('//ul[@class="medal-wall-grid"]')[0]
        self.assertEqual(len(self._cells(markup)), 10)
        self.assertIn(f"height:{cards.MEDAL_WALL_ROW_HEIGHT + cards.MEDAL_WALL_ITEM_HEIGHT}px", grid.get("style"))

    async def test_only_failed_primary_icons_fetch_fallback(self):
        wall = [MedalWallItemView(slot=i, icon_url=f"ake-{i}", fallback_icon_url=f"sk-{i}") for i in (1, 2)]
        with (
            patch.object(cards, "_image_data_urls", AsyncMock(side_effect=[
                {"ake-1": "highres", "ake-2": ""}, {"sk-2": "plated-fallback"},
            ])) as assets,
            patch.object(cards, "_draw_neutral_card", AsyncMock(return_value=b"page")) as render,
        ):
            await cards.draw_medal_missing_card(MedalMissingView(wall=wall))
        self.assertEqual(assets.await_args_list[1].args[0], ["sk-2"])
        cells = self._cells(render.await_args.args[1])
        self.assertEqual(cells[0].xpath(".//img/@src"), ["highres"])
        self.assertEqual(cells[1].xpath(".//img/@src"), ["plated-fallback"])

    async def test_empty_wall_and_followup_page_use_compact_header(self):
        for view, page in ((MedalMissingView(), 1), (MedalMissingView(wall=self._wall([1])), 2)):
            with patch.object(cards, "_draw_neutral_card", AsyncMock(return_value=b"page")) as render:
                await cards._draw_medal_missing_page(view, {}, page_number=page)
            self.assertNotIn('class="medal-wall"', render.await_args.args[1])
            self.assertNotIn('medal-header--wall', render.await_args.args[1])
            self.assertIn('class="medal-gaps"', render.await_args.args[1])

    async def test_wall_header_shows_gap_chips_and_marks_plated(self):
        wall = [
            MedalWallItemView(slot=1, name="镀", icon_url="icon-1", plated=True),
            MedalWallItemView(slot=2, name="普", icon_url="icon-2"),
            MedalWallItemView(slot=11, name="越界", icon_url="icon-11", plated=True),
        ]
        view = MedalMissingView(wall=wall, not_obtained_count=4, not_maxed_count=0, not_plated_count=2)
        with patch.object(cards, "_draw_neutral_card", AsyncMock(return_value=b"page")) as render:
            await cards._draw_medal_missing_page(view, {"icon-1": "a", "icon-2": "b"})
        document = html.fromstring(render.await_args.args[1])
        header = document.xpath("//header")[0]
        self.assertEqual(
            header.get("class"), "medal-header medal-header--missing medal-header--wall"
        )
        chips = header.xpath('.//*[contains(@class,"medal-chip")]')
        self.assertEqual([chip.text_content() for chip in chips], ["未获得4", "未升满0", "未镀层2"])
        self.assertEqual([chip.get("data-zero") is not None for chip in chips], [False, True, False])
        cells = self._cells(render.await_args.args[1])
        self.assertEqual([c.get("data-plated") for c in cells[:2]], ["1", None])

    async def test_unavailable_slot_has_dashed_outline(self):
        markup = cards._medal_wall_html(self._wall([1]), {})
        first = self._cells(markup)[0]
        self.assertTrue(first.xpath('.//*[local-name()="svg"][@class="medal-wall-outline"]'))
        empty = self._cells(markup)[1]
        self.assertFalse(empty.xpath('.//*[local-name()="svg"][@class="medal-wall-outline"]'))

    async def test_wall_slot_limit_shared_with_model_layer(self):
        from plugins.endfield.catalog import models

        self.assertIs(cards.MEDAL_WALL_MAX_SLOTS, models.MEDAL_WALL_MAX_SLOTS)

    async def test_wall_icons_are_loaded_and_wall_only_on_first_page(self):
        wall = self._wall(range(1, 11))
        view = MedalMissingView(
            not_obtained=[MedalItemView(medal_id="a", name="A", icon_url="list-icon")],
            wall=wall,
        )
        with (
            patch.object(cards, "_image_data_urls", AsyncMock(return_value={})) as assets,
            patch.object(cards, "_draw_neutral_card", AsyncMock(return_value=b"page")),
        ):
            await cards.draw_medal_missing_card(view)
        loaded = assets.await_args.args[0]
        self.assertIn("list-icon", loaded)
        for slot in range(1, 11):
            self.assertIn(f"icon-{slot}", loaded)
