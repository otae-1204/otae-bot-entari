from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from lxml import html

from plugins.endfield.catalog.models import MedalItemView, MedalMissingView
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
