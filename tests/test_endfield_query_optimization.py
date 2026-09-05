from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from dataclasses import asdict, replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import plugins.endfield.handlers as endfield
from plugins.endfield.catalog import commands
from plugins.endfield.catalog.models import (
    LoadoutEffectView,
    LoadoutEquipmentView,
    LoadoutPanelStatView,
    LoadoutView,
    WeaponView,
)
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.rendering import cards


def loadout_view():
    return LoadoutView("莱万汀", "熔铸火焰", 90, 5, 90, 5, "力量", "智识", "单手剑")


class SearchKeyTests(unittest.TestCase):
    def setUp(self):
        commands._cached_search_keys.cache_clear()
        commands._cached_pinyin_syllables.cache_clear()

    def test_cached_search_keys_preserve_output(self):
        for text in (
            "",
            "莱万汀",
            "长息轻护甲",
            "lwt",
            "赤缨",
            "Aa-BB 12",
            "未知💡",
            "甲" * 257,
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    commands._search_keys(text), commands._build_search_keys(text)
                )
                self.assertEqual(
                    commands._pinyin_syllables(text),
                    commands._build_pinyin_syllables(text),
                )

    def test_scoring_is_identical_with_and_without_cache(self):
        queries = (
            "莱万汀",
            "lwt",
            "laiwanting",
            "莱玩汀",
            "长息充能甲",
            "chixi",
            "Red Blade",
        )
        names = ("莱万汀", "长息轻护甲", "赤缨", "长息护手", "Red Blade")
        cached = [commands.score_candidate(query, *names) for query in queries]
        with (
            patch.object(commands, "_search_keys", commands._build_search_keys),
            patch.object(
                commands, "_pinyin_syllables", commands._build_pinyin_syllables
            ),
        ):
            uncached = [commands.score_candidate(query, *names) for query in queries]
        self.assertEqual(cached, uncached)

    def test_repeated_names_reuse_pinyin(self):
        with patch.object(
            commands, "lazy_pinyin", wraps=commands.lazy_pinyin
        ) as pinyin:
            commands._search_keys("莱万汀")
            commands._pinyin_syllables("莱万汀")
            count = pinyin.call_count
            for _ in range(10):
                commands._search_keys("莱万汀")
                commands._pinyin_syllables("莱万汀")
            self.assertEqual(pinyin.call_count, count)
            self.assertGreater(count, 0)

    def test_large_input_is_not_retained_and_cache_is_bounded(self):
        for index in range(4100):
            commands._search_keys(str(index))
            commands._pinyin_syllables(str(index))
        before = [
            cache.cache_info()
            for cache in (
                commands._cached_search_keys,
                commands._cached_pinyin_syllables,
            )
        ]
        commands._search_keys("x" * 257)
        commands._pinyin_syllables("x" * 257)
        for cache, previous in zip(
            (commands._cached_search_keys, commands._cached_pinyin_syllables), before
        ):
            self.assertEqual(cache.cache_info(), previous)
            self.assertEqual(previous.currsize, 4096)

    def test_alias_changes_apply_immediately(self):
        with patch.object(commands, "aliases_for", return_value=()):
            before = commands.score_entity_candidate(
                "operator", "new-alias-xyz", "莱万汀"
            )
        with patch.object(commands, "aliases_for", return_value=("new-alias-xyz",)):
            after = commands.score_entity_candidate(
                "operator", "new-alias-xyz", "莱万汀"
            )
        self.assertLess(before, 100)
        self.assertEqual(after, 100)


class EquipmentDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_light_directory_preserves_items_without_fetching_suit_details(self):
        entries = [
            {
                "name": "长息轻护甲",
                "title": "装备/长息轻护甲",
                "group": "长息装备组",
                "rarity": 5,
            },
            {
                "name": "长息护手",
                "title": "装备/长息护手",
                "group": "长息装备组",
                "rarity": 5,
            },
            {
                "name": "巡行信使护甲",
                "title": "装备/巡行信使护甲",
                "group": "巡行信使装备组",
                "rarity": 4,
            },
        ]
        raw = {
            "revision": {
                "contentJson": {
                    "content": [{"attrs": {"roster": {"entries": entries}}}]
                }
            }
        }
        client = SimpleNamespace(fz_article_by_title=AsyncMock(return_value=raw))
        service = EndfieldService(client)
        for rarity in ("gold", "all"):
            with self.subTest(rarity=rarity):
                client.fz_article_by_title.reset_mock()
                light = await service.get_equipment_catalog_view(
                    rarity_filter=rarity, include_details=False
                )
                client.fz_article_by_title.assert_awaited_once_with("装备")
                self.assertGreater(light.total_count, 0)
                client.fz_article_by_title.reset_mock()
                full = await service.get_equipment_catalog_view(rarity_filter=rarity)
                self.assertEqual(
                    client.fz_article_by_title.await_count, 1 + len(full.groups)
                )
                self.assertEqual(asdict(light), asdict(full))

    async def test_candidate_lookup_explicitly_requests_light_directory(self):
        from plugins.endfield.catalog.models import EquipmentCatalogView

        loader = AsyncMock(return_value=EquipmentCatalogView("装备"))
        with patch.object(endfield.service, "get_equipment_catalog_view", loader):
            self.assertEqual(
                await endfield._resolve_equipment_candidates_fz("不存在装备"), []
            )
        loader.assert_awaited_once_with(rarity_filter="gold", include_details=False)


class RelationClient:
    def __init__(self):
        self.calls = Counter()
        self.fail = set()
        self.records = [
            {"name": "甲", "slug": "a", "weaponType": 3},
            {"name": "乙", "slug": "b", "weaponType": 3},
            {"name": "丙", "slug": "c", "weaponType": 3},
            {"name": "不同武器", "slug": "other", "weaponType": 1},
        ]
        self.details = {
            "a": {
                "data": {
                    "characterTable": {"defaultWeaponId": "sword-a"},
                    "charWpnRecommendTable": {
                        "weaponIds1": ["sword-a", "sword-b", "sword-c"]
                    },
                }
            },
            "b": {
                "data": {
                    "characterTable": {"defaultWeaponId": "sword-b"},
                    "charWpnRecommendTable": {"weaponIds1": ["sword-a", "sword-c"]},
                }
            },
            "c": {"data": {"characterTable": {"defaultWeaponId": "sword-a"}}},
        }

    async def weapons(self):
        return {
            "data": [
                {"id": f"sword-{key}", "name": key, "weaponType": 3} for key in "abc"
            ]
        }

    async def operators(self):
        return {"data": self.records}

    async def operator_detail(self, slug):
        self.calls[slug] += 1
        await asyncio.sleep(0)
        if slug in self.fail:
            raise RuntimeError("temporary upstream failure")
        return self.details[slug]


class WeaponRelationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = RelationClient()
        self.service = EndfieldService(self.client)

    async def lookup(self, name="a"):
        return await self.service.find_weapon_operator_names(
            WeaponView(name, name, name, weapon_id=f"sword-{name}")
        )

    async def test_one_index_serves_multiple_weapons_with_default_precedence(self):
        self.assertEqual(await self.lookup("a"), ["甲", "丙"])
        self.assertEqual(await self.lookup("b"), ["乙"])
        self.assertEqual(await self.lookup("c"), ["甲", "乙"])
        self.assertEqual(self.client.calls, {"a": 1, "b": 1, "c": 1})

    async def test_callers_cannot_mutate_cached_names(self):
        names = await self.lookup()
        names.clear()
        self.assertEqual(await self.lookup(), ["甲", "丙"])

    async def test_concurrent_queries_build_one_index(self):
        outputs = await asyncio.gather(*(self.lookup() for _ in range(8)))
        self.assertEqual(outputs, [["甲", "丙"]] * 8)
        self.assertEqual(self.client.calls, {"a": 1, "b": 1, "c": 1})

    async def test_failed_detail_returns_partial_but_is_not_cached(self):
        self.client.fail.add("c")
        self.assertEqual(await self.lookup(), ["甲"])
        self.assertEqual((await self.service._weapon_relations.stats()).entries, 0)
        self.client.fail.clear()
        self.assertEqual(await self.lookup(), ["甲", "丙"])
        self.assertEqual(self.client.calls["c"], 2)

    async def test_all_failed_details_can_recover(self):
        self.client.fail.update("abc")
        self.assertEqual(await self.lookup(), [])
        self.client.fail.clear()
        self.assertEqual(await self.lookup(), ["甲", "丙"])

    async def test_catalog_change_invalidates_index_immediately(self):
        await self.lookup()
        self.client.records[0]["name"] = "甲改名"
        self.assertEqual(await self.lookup(), ["甲改名", "丙"])
        self.assertEqual(self.client.calls["a"], 2)

    async def test_ttl_expiry_refreshes_details(self):
        now = [100.0]
        self.service._weapon_relations._clock = lambda: now[0]
        self.assertEqual(await self.lookup(), ["甲", "丙"])
        self.client.details["a"]["data"]["characterTable"]["defaultWeaponId"] = (
            "sword-b"
        )
        now[0] += 61
        self.assertEqual(await self.lookup(), ["丙"])
        self.assertEqual(self.client.calls["a"], 2)

    async def test_manual_clear_invalidates_index(self):
        await self.lookup()
        self.assertEqual(await self.service.clear_query_caches(), 1)
        await self.lookup()
        self.assertEqual(self.client.calls["a"], 2)

    async def test_unknown_weapon_and_catalog_failure_still_return_empty(self):
        self.assertEqual(await self.lookup("missing"), [])
        with patch.object(
            self.client, "weapons", AsyncMock(side_effect=RuntimeError("offline"))
        ):
            self.assertEqual(await self.lookup(), [])
        self.assertEqual(self.client.calls, {})


class LoadoutCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await endfield._LOADOUT_CACHE.clear()

    async def asyncTearDown(self):
        await endfield._LOADOUT_CACHE.clear()

    async def test_repeated_and_concurrent_views_render_once(self):
        renderer = AsyncMock(return_value=(b"png", True))
        with patch.object(endfield, "draw_loadout_card_with_status", renderer):
            outputs = await asyncio.gather(
                *(endfield._render_loadout_view(loadout_view()) for _ in range(8))
            )
            self.assertEqual(outputs, [b"png"] * 8)
            self.assertEqual(
                await endfield._render_loadout_view(loadout_view()), b"png"
            )
        renderer.assert_awaited_once()

    async def test_any_rendered_view_change_invalidates_cache(self):
        view = loadout_view()
        changes = (
            {"operator_level": 80},
            {"operator_potential": 1},
            {"weapon_level": 80},
            {"weapon_potential": 1},
            {"source_version": "new"},
            {"operator_icon_url": "new-icon"},
            {"equipment": [LoadoutEquipmentView("长息护手", "护手", (3, 2, 1))]},
            {"primary_stats": [LoadoutPanelStatView("atk", "攻击力", "999")]},
            {"effects": [LoadoutEffectView("套装", "新效果", True)]},
        )
        renderer = AsyncMock(return_value=(b"png", True))
        with patch.object(endfield, "draw_loadout_card_with_status", renderer):
            await endfield._render_loadout_view(view)
            for change in changes:
                with self.subTest(change=change):
                    changed = replace(view, **change)
                    await endfield._render_loadout_view(changed)
                    await endfield._render_loadout_view(changed)
            await endfield._render_loadout_view(view)
        self.assertEqual(renderer.await_count, 1 + len(changes))

    async def test_renderer_version_invalidates_cache(self):
        renderer = AsyncMock(return_value=(b"png", True))
        with patch.object(endfield, "draw_loadout_card_with_status", renderer):
            await endfield._render_loadout_view(loadout_view())
            with patch.object(endfield, "CARD_RENDER_VERSION", "next-render-version"):
                await endfield._render_loadout_view(loadout_view())
        self.assertEqual(renderer.await_count, 2)

    async def test_incomplete_image_is_returned_but_not_cached(self):
        renderer = AsyncMock(
            side_effect=[(b"missing-icon", False), (b"complete", True)]
        )
        with patch.object(endfield, "draw_loadout_card_with_status", renderer):
            self.assertEqual(
                await endfield._render_loadout_view(loadout_view()), b"missing-icon"
            )
            self.assertEqual((await endfield._LOADOUT_CACHE.stats()).entries, 0)
            self.assertEqual(
                await endfield._render_loadout_view(loadout_view()), b"complete"
            )
            self.assertEqual(
                await endfield._render_loadout_view(loadout_view()), b"complete"
            )
        self.assertEqual(renderer.await_count, 2)

    async def test_failed_render_is_not_cached(self):
        renderer = AsyncMock(
            side_effect=[RuntimeError("browser unavailable"), (b"png", True)]
        )
        with patch.object(endfield, "draw_loadout_card_with_status", renderer):
            with self.assertRaisesRegex(RuntimeError, "browser unavailable"):
                await endfield._render_loadout_view(loadout_view())
            self.assertEqual(
                await endfield._render_loadout_view(loadout_view()), b"png"
            )
        self.assertEqual(renderer.await_count, 2)

    async def test_loadout_cache_expires_and_is_bounded(self):
        now = [100.0]
        renderer = AsyncMock(return_value=(b"png", True))
        with (
            patch.object(endfield._LOADOUT_CACHE, "_clock", lambda: now[0]),
            patch.object(endfield, "draw_loadout_card_with_status", renderer),
        ):
            await endfield._render_loadout_view(loadout_view())
            now[0] += 61
            await endfield._render_loadout_view(loadout_view())
        self.assertEqual(renderer.await_count, 2)
        self.assertEqual(endfield._LOADOUT_CACHE.max_entries, 32)
        self.assertEqual(endfield._LOADOUT_CACHE.max_bytes, 24 * 1024 * 1024)

    async def test_dev_clear_clears_dependent_caches(self):
        for scope in ("all", "icon", "operator", "weapon", "equipment", "stage"):
            with self.subTest(scope=scope):
                await endfield._LOADOUT_CACHE.get_or_create(
                    ("version", "hash"), AsyncMock(return_value=b"png")
                )
                clear_relations = AsyncMock(return_value=0)
                with (
                    patch.object(
                        endfield, "clear_http_cache", AsyncMock(return_value=0)
                    ),
                    patch.object(
                        endfield.service, "clear_query_caches", clear_relations
                    ),
                ):
                    await endfield._clear_endfield_caches(scope)
                self.assertEqual((await endfield._LOADOUT_CACHE.stats()).entries, 0)
                self.assertEqual(
                    clear_relations.await_count, 0 if scope == "icon" else 1
                )

    async def test_missing_required_assets_mark_image_incomplete(self):
        view = replace(loadout_view(), operator_id="char_test")
        resolver = AsyncMock(
            return_value=(cards._PreparedAssets({}, {}, {}, {}), {}, {})
        )
        with patch.object(cards, "_resolve_asset_groups", resolver):
            self.assertFalse((await cards.prepare_loadout_card_html(view)).complete)
            self.assertTrue(
                (await cards.prepare_loadout_card_html(loadout_view())).complete
            )

    async def test_successful_fallback_asset_is_complete(self):
        view = replace(loadout_view(), operator_id="char_test")
        resolver = AsyncMock(
            return_value=(
                cards._PreparedAssets({}, {}, {}, {}),
                {"operator": "fallback"},
                {},
            )
        )
        with patch.object(cards, "_resolve_asset_groups", resolver):
            self.assertTrue((await cards.prepare_loadout_card_html(view)).complete)

    async def test_existing_draw_api_still_returns_identical_bytes(self):
        prepared = cards.PreparedCardHtml("<div></div>", {}, 1500, complete=False)
        with (
            patch.object(
                cards, "prepare_loadout_card_html", AsyncMock(return_value=prepared)
            ),
            patch.object(
                cards, "_draw_gallery_catalog", AsyncMock(return_value=b"png")
            ),
        ):
            self.assertEqual(await cards.draw_loadout_card(loadout_view()), b"png")
            self.assertEqual(
                await cards.draw_loadout_card_with_status(loadout_view()),
                (b"png", False),
            )


class InvestmentConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_catalog_and_names_start_together(self):
        started = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event()

        async def loader(index, *_args):
            started[index].set()
            await release.wait()
            return {"source": index}

        role = SimpleNamespace(
            role_id="test",
            masked_uid="t***",
            nickname="测试",
            server_name="国服",
            server_id="1",
        )
        with (
            patch.object(
                endfield.account_store, "decrypt_token", return_value="test-only"
            ),
            patch.object(
                endfield,
                "decode_account_credential",
                return_value=("skland", "test-only"),
            ),
            patch.object(endfield, "is_asia_role", return_value=False),
            patch.object(
                endfield, "_card_detail_with_snapshot", side_effect=partial(loader, 0)
            ),
            patch.object(
                endfield,
                "fetch_account_investment_catalog",
                side_effect=partial(loader, 1),
            ),
            patch.object(
                endfield,
                "fetch_account_detail_name_map",
                side_effect=partial(loader, 2),
            ),
            patch.object(
                endfield, "build_account_investment_view", return_value="view"
            ) as build,
            patch.object(
                endfield,
                "draw_account_investment_cards",
                AsyncMock(return_value=[b"png"]),
            ),
            patch.object(endfield, "_finish_pngs", AsyncMock(return_value="done")),
        ):
            task = asyncio.create_task(
                endfield._render_account_investment(None, role, None, group=True)
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in started)), 1
                )
            finally:
                release.set()
                result = await task
        self.assertEqual(result, "done")
        self.assertEqual(build.call_args.args, ({"source": 0},))
        self.assertEqual(build.call_args.kwargs["catalog"], {"source": 1})
        self.assertEqual(build.call_args.kwargs["name_map"], {"source": 2})
        self.assertEqual(build.call_args.kwargs["uid"], "t***")
