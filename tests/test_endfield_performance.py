from __future__ import annotations

import asyncio
import importlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import plugins.endfield as endfield
from plugins.endfield import draw
from plugins.endfield.client import WarfarinAPIError
from plugins.endfield.commands import EndfieldCandidate, ParsedEndfieldCommand
from plugins.endfield.models import (
    EffectView,
    EquipmentCatalogGroupView,
    EquipmentCatalogItemView,
    EquipmentCatalogView,
    EquipmentView,
    OperatorView,
)
from utils.http_client import HttpResource, clear_http_cache


service_module = importlib.import_module("plugins.endfield.service")


class EndfieldPerformanceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await endfield._CARD_CACHE.clear()
        await clear_http_cache("endfield-")

    async def test_fz_summary_hit_skips_search_request(self):
        class FakeClient:
            search_calls = 0

            async def fz_article_summaries(self, prefix):
                return {"articles": [{"title": "干员/莱万汀"}]}

            async def fz_search(self, query):
                self.search_calls += 1
                return {"hits": []}

        fake = FakeClient()
        with patch.object(endfield, "client", fake):
            candidates = await endfield._resolve_operator_candidates_fz("lwt")
        self.assertEqual(fake.search_calls, 0)
        self.assertEqual(candidates[0].key, "干员/莱万汀")
        self.assertEqual(candidates[0].score, 100)

    async def test_fz_equipment_candidates_prefer_group_then_exact_item(self):
        catalog = EquipmentCatalogView(
            "全部装备套组",
            groups=[
                EquipmentCatalogGroupView(
                    "长息装备组",
                    [
                        EquipmentCatalogItemView(
                            "长息轻护甲",
                            "装备/长息轻护甲",
                            "长息装备组",
                            rarity=5,
                        )
                    ],
                )
            ],
            total_count=1,
        )
        with patch.object(
            endfield.service,
            "get_equipment_catalog_view",
            AsyncMock(return_value=catalog),
        ) as loader:
            group_candidates = await endfield._resolve_equipment_candidates_fz("长息")
            item_candidates = await endfield._resolve_equipment_candidates_fz("长息轻护甲")
            alias_candidates = await endfield._resolve_equipment_candidates_fz("长息充能甲")

        loader.assert_awaited()
        selected_group, _ = endfield.choose_candidate(group_candidates)
        selected_item, _ = endfield.choose_candidate(item_candidates)
        selected_alias, _ = endfield.choose_candidate(alias_candidates)
        self.assertEqual(selected_group.kind, "equipment_catalog")
        self.assertEqual(selected_group.key, "gold::长息装备组")
        self.assertEqual(selected_item.kind, "equipment")
        self.assertEqual(selected_item.key, "装备/长息轻护甲")
        self.assertEqual(selected_alias.key, "装备/长息轻护甲")

    async def test_fz_equipment_catalog_defaults_gold_and_accepts_all_filter(self):
        catalog = EquipmentCatalogView("全部装备套组")
        loader = AsyncMock(return_value=catalog)
        with patch.object(endfield.service, "get_equipment_catalog_view", loader):
            default = await endfield._resolve_equipment_candidates_fz("__all__")
            all_items = await endfield._resolve_equipment_candidates_fz("__all__", "all")

        self.assertEqual(default[0].key, "gold::")
        self.assertEqual(all_items[0].key, "all::")

    async def test_fz_article_and_richtext_start_concurrently(self):
        article_started = asyncio.Event()
        richtext_started = asyncio.Event()
        release = asyncio.Event()

        class FakeClient:
            async def fz_article_by_title(self, title):
                article_started.set()
                await release.wait()
                return {"article": {"title": title}}

            async def fz_game_richtext(self):
                richtext_started.set()
                await release.wait()
                return {"styles": {}}

        task = asyncio.create_task(service_module._fz_article_and_richtext(FakeClient(), "干员/莱万汀"))
        await asyncio.wait_for(asyncio.gather(article_started.wait(), richtext_started.wait()), timeout=1)
        release.set()
        article, richtext = await task
        self.assertEqual(article["article"]["title"], "干员/莱万汀")
        self.assertEqual(richtext, {"styles": {}})

    async def test_fz_richtext_failure_still_returns_article(self):
        class FakeClient:
            async def fz_article_by_title(self, title):
                return {"article": {"title": title}}

            async def fz_game_richtext(self):
                raise WarfarinAPIError("offline")

        article, richtext = await service_module._fz_article_and_richtext(FakeClient(), "干员/莱万汀")
        self.assertEqual(article["article"]["title"], "干员/莱万汀")
        self.assertEqual(richtext, {})

    async def test_remote_image_batch_deduplicates_urls(self):
        seen = []

        async def fake_fetch_many(urls, **kwargs):
            seen.extend(urls)
            return {
                url: HttpResource(b"image", "image/png", 200, url)
                for url in urls
            }

        with patch.object(draw, "fetch_many", fake_fetch_many):
            results = await draw._image_data_urls(["https://asset/a.png", "https://asset/a.png"])
        self.assertEqual(seen, ["https://asset/a.png"])
        self.assertTrue(results["https://asset/a.png"].startswith("data:image/png;base64,"))

    async def test_endfield_screenshot_keeps_two_x_and_short_settle(self):
        view = OperatorView(name="测试", slug="干员/测试", operator_id="test")
        screenshot = AsyncMock(return_value=b"png")
        prepared = draw.PreparedCardHtml("<div class='endfield-card'></div>", {}, draw.OPERATOR_CARD_WIDTH)
        with patch.object(draw, "prepare_operator_card_html", AsyncMock(return_value=prepared)):
            with patch.object(draw, "screenshot_web_element", screenshot):
                output = await draw.draw_operator_card(view)
        self.assertEqual(output, b"png")
        kwargs = screenshot.await_args.kwargs
        self.assertEqual(kwargs["device_scale_factor"], 2.0)
        self.assertEqual(kwargs["settle_ms"], 50)
        self.assertTrue(kwargs["strict_max_height"])
        self.assertTrue(kwargs["wait_for_images"])

    async def test_production_card_resources_use_virtual_urls_without_remote_base64(self):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        async def fake_fetch_many(urls, **kwargs):
            return {
                url: HttpResource(image_bytes, "image/png", 200, url)
                for url in urls
            }

        view = OperatorView(
            name="测试",
            slug="干员/测试",
            operator_id="test",
            portrait_url="https://asset/portrait.png",
        )
        with patch.object(draw, "fetch_many", fake_fetch_many):
            prepared = await draw.prepare_operator_card_html(view)
        virtual_url = next(iter(prepared.resources))
        self.assertIn(f'src="{virtual_url}" alt="测试"', prepared.html)
        self.assertNotIn('src="data:image/png;base64,', prepared.html.split('<div class="portrait">', 1)[1].split('</div>', 2)[0])
        self.assertEqual(len(prepared.resources), 1)

    async def test_operator_portrait_falls_back_to_vertical_icon_when_primary_asset_fails(self):
        buffer = io.BytesIO()
        Image.new("RGB", (152, 212), "white").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        portrait_url = "https://asset/missing-portrait.webp"
        icon_url = "https://asset/operator-icon.webp"

        async def fake_fetch_many(urls, **kwargs):
            return {
                url: None if url == portrait_url else HttpResource(image_bytes, "image/png", 200, url)
                for url in urls
            }

        view = OperatorView(
            name="测试",
            slug="test",
            operator_id="test",
            portrait_url=portrait_url,
            icon_url=icon_url,
        )
        with patch.object(draw, "fetch_many", fake_fetch_many):
            prepared = await draw.prepare_operator_card_html(view)

        virtual_url = next(iter(prepared.resources))
        portrait_html = prepared.html.split('<div class="portrait">', 1)[1].split('<section class="panel">', 1)[0]
        self.assertIn(f'src="{virtual_url}" alt="测试"', portrait_html)
        self.assertNotIn(portrait_url, prepared.html)

    async def test_operator_card_allows_five_long_potentials_to_expand_rail(self):
        view = OperatorView(
            "诀",
            "arcane",
            "Warfarin/arcane",
            potentials=[
                EffectView(
                    f"potential-{index}",
                    f"P{index} 潜能效果",
                    "长文本效果说明" * (9 if index in {1, 5} else 3),
                    "potential",
                )
                for index in range(1, 6)
            ],
        )
        with patch.object(draw, "fetch_many", AsyncMock(return_value={})):
            output = await draw.draw_operator_card(view)

        self.assertTrue(output.startswith(b"\x89PNG"))

    async def test_rendered_card_cache_singleflights_and_dev_clear_works(self):
        calls = 0

        rendered_sources = []

        async def renderer(key, source):
            nonlocal calls
            calls += 1
            rendered_sources.append(source)
            await asyncio.sleep(0.02)
            return b"png-data"

        candidate = EndfieldCandidate("operator", "干员/莱万汀", "莱万汀", 100, "fz")
        with patch.dict(endfield.CONTENT_RENDERERS, {"operator": renderer}, clear=False):
            first, second = await asyncio.gather(
                endfield._render_candidate(candidate, "fz"),
                endfield._render_candidate(candidate, "fz"),
            )
            third = await endfield._render_candidate(candidate, "fz")
        self.assertEqual((first, second, third), (b"png-data", b"png-data", b"png-data"))
        self.assertEqual(calls, 1)
        self.assertEqual(rendered_sources, ["fz"])

        command = ParsedEndfieldCommand("dev", dev_action="cache", args=("clear", "operator"))
        message = await endfield._handle_dev_command(command)
        self.assertIn("已清理 operator 缓存", message)
        self.assertEqual((await endfield._CARD_CACHE.stats()).entries, 0)

    async def test_requested_source_skips_other_candidate_resolvers(self):
        calls = []

        async def fz_resolver(query):
            calls.append(("fz", query))
            return [EndfieldCandidate("operator", "干员/莱万汀", "莱万汀", 100, "fz")]

        async def warfarin_resolver(query):
            calls.append(("warfarin", query))
            return [EndfieldCandidate("operator", "laevatain", "莱万汀", 100, "warfarin")]

        resolvers = {"operator": {"fz": fz_resolver, "warfarin": warfarin_resolver}}
        with patch.dict(endfield.SOURCE_CANDIDATE_RESOLVERS, resolvers, clear=True):
            candidates = await endfield._resolve_candidates_from_sources(
                "operator", "莱万汀", "warfarin"
            )

        self.assertEqual(calls, [("warfarin", "莱万汀")])
        self.assertEqual([candidate.source for candidate in candidates], ["warfarin"])

    async def test_render_operator_uses_candidate_source_without_fallback(self):
        view = OperatorView("莱万汀", "laevatain", "Warfarin/laevatain")
        with (
            patch.object(endfield.service, "get_operator_view_from_warfarin", AsyncMock(return_value=view)) as selected,
            patch.object(endfield.service, "get_operator_view", AsyncMock()) as fallback,
            patch.object(endfield, "draw_operator_card", AsyncMock(return_value=b"png-data")),
        ):
            output = await endfield._render_operator("laevatain", "warfarin")

        self.assertEqual(output, b"png-data")
        selected.assert_awaited_once_with("laevatain")
        fallback.assert_not_awaited()

    async def test_render_equipment_uses_fz_source(self):
        view = EquipmentView("长息轻护甲", "装备/长息轻护甲")
        with (
            patch.object(endfield.service, "get_equipment_view_from_fz", AsyncMock(return_value=view)) as selected,
            patch.object(endfield.service, "get_equipment_view", AsyncMock()) as fallback,
            patch.object(endfield, "draw_equipment_card", AsyncMock(return_value=b"png-data")),
        ):
            output = await endfield._render_equipment("装备/长息轻护甲", "fz")

        self.assertEqual(output, b"png-data")
        selected.assert_awaited_once_with("装备/长息轻护甲")
        fallback.assert_not_awaited()

    async def test_render_equipment_catalog_uses_group_and_rarity_key(self):
        view = EquipmentCatalogView("长息装备组", rarity_filter="purple")
        with (
            patch.object(endfield.service, "get_equipment_catalog_view", AsyncMock(return_value=view)) as selected,
            patch.object(endfield, "draw_equipment_catalog_card", AsyncMock(return_value=b"png-data")),
        ):
            output = await endfield._render_equipment_catalog("purple::长息装备组", "fz")

        self.assertEqual(output, b"png-data")
        selected.assert_awaited_once_with("长息装备组", "purple")


class BilibiliSharedAssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_cover_and_avatar_fetch_concurrently_and_tolerate_one_failure(self):
        from tests.test_core_logic import _load_bili_new_module

        bili_draw = _load_bili_new_module("draw")
        bili_models = __import__(bili_draw.__package__ + ".models", fromlist=["BiliCard"])
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        active = 0
        maximum = 0

        async def fake_fetch(url, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            if url.endswith("avatar.png"):
                raise RuntimeError("avatar unavailable")
            return HttpResource(image_bytes, "image/png", 200, url)

        card = bili_models.BiliCard(
            "video",
            "标题",
            cover_url="https://asset/cover.png",
            avatar_url="https://asset/avatar.png",
        )
        with patch.object(bili_draw, "fetch_bytes", fake_fetch):
            png = await bili_draw.draw_bili_card(card)
        self.assertEqual(maximum, 2)
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
