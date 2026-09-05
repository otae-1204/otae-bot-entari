from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from plugins.endfield.catalog.akedata import AkeCatalog, effect_values
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.providers.repository import (
    AkeSnapshot,
    AkeDataIncomplete,
    localize,
    query_snapshot,
)
from plugins.endfield.providers import repository
from plugins.endfield.providers.assets import static_sprite_url, item_icon_urls
from otae_bot.infrastructure.http import client as http
from otae_bot.infrastructure.http.disk import PublicImageDiskCache, public_table_request


FIXTURE = Path(__file__).parent / "fixtures/endfield_akedata_1_5_3.json"


class NativeAkeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = AkeSnapshot(
            "1.5.3@9885010-4", "public/1.5.3/9885010-4/TableCfg", "fixture"
        )
        self.data._tables = json.loads(FIXTURE.read_bytes())
        self.catalog = AkeCatalog(self.data)
        self.network = patch.object(
            repository, "_get", side_effect=AssertionError("Unexpected network request")
        )
        self.network.start()

    async def asyncTearDown(self):
        self.network.stop()

    async def test_light_catalog_deduplicates_admin_without_reading_character_table(
        self,
    ):
        self.data._tables.pop("CharacterTable")
        view = await self.catalog.operator_catalog()
        items = [item for e in view.elements for p in e.professions for item in p.items]
        self.assertEqual(sum(item.name == "管理员" for item in items), 1)
        self.assertTrue(all(item.operator_id != "chr_9000_endmin" for item in items))
        self.assertEqual(view.source_name, "AKEData")
        self.assertEqual({i.weapon_type for i in items}, {"单手剑", "施术单元"})

    async def test_typhoea_all_variants_and_mastery_levels(self):
        view = await self.catalog.operator_view("提弗洛斯")
        self.assertEqual(view.operator_id, "chr_0034_typhoea")
        self.assertEqual(view.weapon_type, "施术单元")
        self.assertEqual(len(view.skills), 4)
        self.assertEqual(
            [skill.category for skill in view.skills],
            ["普攻", "战技", "连携技", "终结技"],
        )
        self.assertEqual(len(view.tags), 2)
        self.assertTrue(view.skills[0].preserve_metric_rows)
        self.assertGreaterEqual(len(view.skills[0].levels[0].values), 7)
        by_kind = {skill.category: skill for skill in view.skills}
        self.assertEqual(len(by_kind["战技"].extra_levels), 8)
        self.assertEqual(len(by_kind["连携技"].extra_levels), 2)
        for skill in view.skills:
            self.assertEqual([level.level for level in skill.levels], [9, 10, 11, 12])
            self.assertTrue(all(level.values for level in skill.levels))
            self.assertNotIn("{", skill.description)
        self.assertEqual(len(view.potentials), 5)
        self.assertIn("+20", view.potentials[1].description)
        self.assertIn("+16", view.potentials[1].description)
        self.assertTrue(view.term_styles)

    async def test_laevatain_wisd_is_not_will_and_attack_bonus_is_resolved(self):
        view = await self.catalog.operator_view("莱万汀")
        self.assertIn("智识", view.potentials[1].description)
        self.assertIn("+15%", view.potentials[1].description)
        self.assertNotIn("--", view.potentials[1].description)
        value = effect_values(
            {"dataList": [{"attrModifier": {"attrType": 41, "attrValue": 20}}]}
        )
        self.assertEqual(value["Wisd"], 20)
        self.assertNotIn("Will", value)

    async def test_missing_skill_level_fails_instead_of_empty_metrics(self):
        key = "chr_0034_typhoea_normal_skill_floating_start"
        rows = self.data._tables["SkillPatchTable"][key]["SkillPatchDataBundle"]
        self.data._tables["SkillPatchTable"][key]["SkillPatchDataBundle"] = [
            r for r in rows if r["level"] != 12
        ]
        with self.assertRaises(AkeDataIncomplete):
            await self.catalog.operator_view("提弗洛斯")

    async def test_weapon_skill_and_relation(self):
        view = await self.catalog.weapon_view("熔铸火焰")
        self.assertEqual(view.weapon_id, "wpn_sword_0006")
        self.assertEqual(view.source_name, "AKEData")
        self.assertEqual(len(view.skills), 3)
        self.assertEqual(view.skills[0].levels[-1].values["wisd"], 156)
        self.assertIn("莱万汀", view.operator_names)
        self.assertEqual(
            await self.catalog.recommended_weapon("莱万汀"), "武器/熔铸火焰"
        )

    async def test_equipment_forging_and_suit_are_not_rounded_in_inputs(self):
        raw = await self.catalog.equipment_raw("长息轻护甲")
        attrs = raw["revision"]["contentJson"]["content"][0]["attrs"]
        self.assertEqual(attrs["stats"]["rows"][1]["values"], [110, 121, 132, 143])
        self.assertAlmostEqual(
            attrs["stats"]["rows"][2]["values"][0], 0.1232142857142857
        )
        view = await self.catalog.equipment_view("长息轻护甲")
        self.assertEqual(view.suit_required_count, 3)
        self.assertIn("1000", view.suit_description)
        self.assertEqual(view.source_name, "AKEData")
        self.assertTrue(view.term_styles)
        self.assertEqual(view.acquisition, "装备制造")

    def test_known_shared_icon_precedes_guessed_variant_id(self):
        primary = item_icon_urls("item_equip_t4_suit_usp02_hand_01")[0]
        self.assertEqual(
            item_icon_urls("item_equip_t4_suit_usp02_hand_02", primary)[0], primary
        )

    async def test_composite_stat_is_one_forgeable_slot(self):
        raw = await self.catalog.equipment_raw("清波手甲")
        rows = raw["revision"]["contentJson"]["content"][0]["attrs"]["stats"]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["compositeAttr"], "AllSkillDamageIncrease")
        self.assertEqual(sum(row["enhances"] for row in rows), 3)
        with self.assertRaisesRegex(ValueError, "词条编号"):
            await self.catalog.loadout(
                "莱万汀", "熔铸火焰", [("清波手甲", 3, ((4, 3),))]
            )

    async def test_loadout_matches_recorded_legacy_panel_precision(self):
        view = await self.catalog.loadout("莱万汀", "熔铸火焰", [("长息轻护甲", 3, ())])
        self.assertEqual(
            {row.key: row.value for row in view.primary_stats},
            {"Atk": "3811", "MaxHp": "6100", "Def": "56"},
        )
        self.assertEqual(
            {row.key: row.value for row in view.ability_stats},
            {"Str": "121", "Agi": "100", "Wisd": "414", "Will": "232"},
        )
        advanced = {row.key: row.value for row in view.advanced_stats}
        self.assertEqual(advanced["CriticalDamageIncrease"], "50.0%")

    async def test_incomplete_legacy_admin_talent_requires_whole_view_fallback(self):
        with self.assertRaisesRegex(AkeDataIncomplete, "parameters unavailable"):
            await self.catalog.operator_view("管理员")

    async def test_deduplicated_female_admin_still_resolves_by_id(self):
        from plugins.endfield import handlers as end

        with patch.object(repository, "snapshot", AsyncMock(return_value=self.data)):
            rows = await end._resolve_candidates_akedata("operator", "chr_0003_endminf")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].key, "chr_0003_endminf")

    async def test_loadout_all_ake_and_potential_changes(self):
        zero = await self.catalog.loadout(
            "莱万汀", "熔铸火焰", [], operator_potential=0, weapon_potential=0
        )
        full = await self.catalog.loadout(
            "莱万汀", "熔铸火焰", [], operator_potential=5, weapon_potential=5
        )
        zero_ability = {v.key: int(v.value) for v in zero.ability_stats}
        full_ability = {v.key: int(v.value) for v in full.ability_stats}
        self.assertEqual(full_ability["Wisd"] - zero_ability["Wisd"], 20)
        self.assertEqual(full_ability["Will"], zero_ability["Will"])
        self.assertEqual(full.source_name, "AKEData")
        self.assertEqual(full.source_version, self.data.version)
        with self.assertRaisesRegex(ValueError, "武器类型不匹配"):
            await self.catalog.loadout("提弗洛斯", "熔铸火焰", [])

    async def test_native_service_cache_mutation_isolation_and_clear(self):
        service = EndfieldService(SimpleNamespace())
        with patch.object(service, "ake_catalog", AsyncMock(return_value=self.catalog)):
            one = await service.get_operator_view_from_akedata("提弗洛斯")
            one.skills[0].description = "mutated"
            two = await service.get_operator_view_from_akedata("提弗洛斯")
            self.assertNotEqual(two.skills[0].description, "mutated")
            await service.clear_query_caches()
            await service._ake_views.close()

    async def test_explicit_source_does_not_fallback(self):
        service = EndfieldService(SimpleNamespace())
        with (
            patch.object(
                service,
                "ake_catalog",
                AsyncMock(side_effect=AkeDataIncomplete("fixture")),
            ),
            patch.object(
                service,
                "get_weapon_catalog_view_from_fz",
                AsyncMock(return_value="fallback"),
            ) as fallback,
        ):
            with self.assertRaises(AkeDataIncomplete):
                await service.get_weapon_catalog_view(source="akedata")
            fallback.assert_not_awaited()
            self.assertEqual(await service.get_weapon_catalog_view(), "fallback")
            fallback.assert_awaited_once()

    async def test_default_handler_complete_path_uses_no_fz_or_warfarin(self):
        from plugins.endfield import handlers as end

        service = EndfieldService(SimpleNamespace())
        await end._CARD_CACHE.clear()
        with (
            patch.object(end, "service", service),
            patch.object(service, "ake_catalog", AsyncMock(return_value=self.catalog)),
            patch.object(repository, "snapshot", AsyncMock(return_value=self.data)),
            patch.object(
                end.client,
                "_get_json",
                AsyncMock(side_effect=AssertionError("Legacy source requested")),
            ) as legacy,
            patch.object(end, "draw_operator_card", AsyncMock(return_value=b"png")),
        ):
            candidates = await end._collect_candidates("operator", "提弗洛斯")
            candidate, ambiguous = end.choose_candidate(candidates)
            self.assertFalse(ambiguous)
            self.assertEqual(candidate.source, "akedata")
            self.assertEqual(candidate.key, "chr_0034_typhoea")
            self.assertEqual(candidate.revision, self.data.revision)
            self.assertEqual(await end._render_candidate(candidate), (b"png",))
            legacy.assert_not_awaited()
        await end._CARD_CACHE.clear()
        await service._ake_views.close()

    async def test_complete_default_view_reuses_png_without_rebuilding(self):
        from plugins.endfield import handlers as end

        service = EndfieldService(SimpleNamespace())
        candidate = end.EndfieldCandidate(
            "operator",
            "chr_0034_typhoea",
            "提弗洛斯",
            100,
            "akedata",
            revision=self.data.revision,
        )
        await end._CARD_CACHE.clear()
        with (
            patch.object(end, "service", service),
            patch.object(service, "ake_catalog", AsyncMock(return_value=self.catalog)),
            patch.object(repository, "snapshot", AsyncMock(return_value=self.data)),
            patch.object(
                end, "draw_operator_card", AsyncMock(return_value=b"png")
            ) as draw,
        ):
            self.assertEqual(await end._render_candidate(candidate), (b"png",))
            self.assertEqual(await end._render_candidate(candidate), (b"png",))
            draw.assert_awaited_once()
        await end._CARD_CACHE.clear()
        await service._ake_views.close()

    async def test_snapshot_context_pins_version_and_restores(self):
        manifest = {
            "latest": "1.5.3@9885010-4",
            "sharedRevision": "a",
            "versions": [
                {"id": "1.5.3@9885010-4", "tableCfgPath": self.data.table_path}
            ],
        }
        with patch.object(
            repository, "fetch_akedata_manifest", AsyncMock(return_value=manifest)
        ) as fetch:
            async with query_snapshot() as first:
                manifest["sharedRevision"] = "b"
                async with query_snapshot() as second:
                    self.assertIs(first, second)
                    self.assertEqual(second.shared_revision, "a")
            self.assertEqual((await repository.snapshot()).shared_revision, "b")
            self.assertEqual(fetch.await_count, 2)

    async def test_calendar_with_stale_coverage_is_not_current_ake(self):
        from plugins.endfield.calendar import akedata as calendar

        source = calendar.AkeDataVersionCalendarSource(SimpleNamespace())
        with (
            patch.object(
                calendar,
                "load_calendar_manifest",
                return_value=SimpleNamespace(version="1.4"),
            ),
            patch.object(
                source,
                "_latest_version",
                AsyncMock(return_value=SimpleNamespace(id="1.5.3@9913107-5")),
            ),
            patch.object(source, "_load_tables", AsyncMock()) as tables,
        ):
            with self.assertRaisesRegex(calendar.VersionCalendarError, "完整事件覆盖"):
                await source.current_ake_primary()
            tables.assert_not_awaited()

    async def test_shared_revision_invalidates_normalized_view_cache(self):
        service = EndfieldService(SimpleNamespace())
        with (
            patch.object(service, "ake_catalog", AsyncMock(return_value=self.catalog)),
            patch.object(
                self.catalog,
                "operator_view",
                AsyncMock(wraps=self.catalog.operator_view),
            ) as build,
        ):
            await service.get_operator_view_from_akedata("提弗洛斯")
            await service.get_operator_view_from_akedata("提弗洛斯")
            self.assertEqual(build.await_count, 1)
            self.data.shared_revision = "new-shared-assets"
            await service.get_operator_view_from_akedata("提弗洛斯")
            self.assertEqual(build.await_count, 2)
        await service._ake_views.close()

    def test_numeric_text_id_localization_does_not_mutate(self):
        raw = {"a": {"id": 1234567890123456789, "text": ""}}
        self.assertEqual(localize(raw, {"1234567890123456789": "正确"}), {"a": "正确"})
        self.assertEqual(raw["a"]["text"], "")

    def test_dto_memory_accounting_includes_tuple_form_descriptions(self):
        from otae_bot.infrastructure.http.json_values import json_memory_size

        self.assertGreater(json_memory_size([("form", "long text" * 2000)]), 18000)

    async def test_gacha_catalog_schema_and_revision_invalidate_local_metadata(self):
        from plugins.endfield.gacha.assets import EndfieldGachaAssetCache

        views = (
            await self.catalog.operator_catalog(),
            await self.catalog.weapon_catalog(),
        )
        service = SimpleNamespace(
            get_public_data_revision=AsyncMock(return_value="first"),
            get_gacha_catalog_views=AsyncMock(return_value=views),
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = EndfieldGachaAssetCache(service, cache_dir=directory)
            first = await cache._load_catalog()
            self.assertTrue(first)
            self.assertEqual(first, await cache._load_catalog())
            self.assertEqual(service.get_gacha_catalog_views.await_count, 1)
            service.get_public_data_revision.return_value = "second"
            await cache._load_catalog()
            self.assertEqual(service.get_gacha_catalog_views.await_count, 2)
            self.assertEqual(
                json.loads(cache.catalog_path.read_text())["revision"], "second"
            )
            cache._catalog_schema = 0  # A pre-migration file must be refreshed.
            await cache._load_catalog()
            self.assertEqual(service.get_gacha_catalog_views.await_count, 3)
            service.get_public_data_revision.side_effect = RuntimeError("outage")
            service.get_gacha_catalog_views.side_effect = RuntimeError("outage")
            self.assertEqual(first, await cache._load_catalog(force=True))

    async def test_gacha_native_keepsake_does_not_request_fz(self):
        from plugins.endfield.gacha.assets import (
            EndfieldGachaAssetCache,
            GachaItemMetadata,
            GachaPoolRule,
        )

        key = "chr_0016_laevat"
        service = SimpleNamespace(
            get_ake_items=AsyncMock(
                return_value={
                    f"item_charpotentialup_{key}": {
                        "name": "莱万汀的信物",
                        "iconId": "native_icon",
                        "rarity": 6,
                    }
                }
            ),
            client=SimpleNamespace(
                fz_article_by_title=AsyncMock(
                    side_effect=AssertionError("FZ requested")
                )
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = EndfieldGachaAssetCache(service, cache_dir=directory)
            with (
                patch.object(
                    cache,
                    "_load_catalog",
                    AsyncMock(
                        return_value={key: GachaItemMetadata(key, "莱万汀", 6, "角色")}
                    ),
                ),
                patch.object(cache, "_cache_images", AsyncMock(return_value={})),
            ):
                result = await cache.prepare_keepsakes(
                    {"pool": GachaPoolRule("pool", (key,))}
                )
            self.assertEqual(result[key].name, "莱万汀的信物")
            self.assertIn("native_icon.png", result[key].icon_url)
            service.client.fz_article_by_title.assert_not_awaited()

    async def test_gacha_native_pool_metadata_preserves_server_guarantee_fallback(self):
        from plugins.endfield.gacha.assets import EndfieldGachaAssetCache, GachaPoolRule

        native = {
            key: dict(up_item_ids=("new-up",), pool_name="新卡池", pool_kind="weapon")
            for key in ("known", "unknown")
        }
        cache = EndfieldGachaAssetCache(
            SimpleNamespace(get_ake_pool_metadata=AsyncMock(return_value=native))
        )
        with patch.object(
            cache,
            "_prepare_pool_rules_fz",
            AsyncMock(return_value={"known": GachaPoolRule("known", ("old-up",), 80)}),
        ):
            rows = await cache.prepare_pool_rules([])
        self.assertEqual(rows["known"].hard_guarantee, 80)
        self.assertEqual(rows["known"].up_item_ids, ("new-up",))
        self.assertEqual(rows["unknown"].hard_guarantee, 0)

    def test_client_sprite_folder_is_normalized_for_case_sensitive_cdn(self):
        self.assertIn(
            "/termicon/", static_sprite_url("TermIcon/icon_term_ba_naturalinflict.png")
        )


class TableDiskTests(unittest.IsolatedAsyncioTestCase):
    url = (
        "https://data.akedata.wiki/public/1.5.3/9885010-4/TableCfg/CharGrowthTable.json"
    )

    async def asyncSetUp(self):
        await http.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        self.disk = PublicImageDiskCache(
            Path(self.directory.name) / "tables.sqlite3", 4096
        )
        self.patcher = patch.object(http, "public_tables", self.disk)
        self.patcher.start()

    async def asyncTearDown(self):
        await http.close_http_client()
        self.patcher.stop()
        self.directory.cleanup()

    def test_public_table_whitelist(self):
        self.assertTrue(public_table_request(self.url, "akedata", {}, None, "json"))
        for url in (
            self.url + "?token=x",
            self.url.replace("https:", "http:"),
            self.url.replace("data.akedata.wiki", "evil.test"),
            "https://data.akedata.wiki/manifest.json",
        ):
            self.assertFalse(public_table_request(url, "akedata", {}, None, "json"))
        for headers in (
            {"Cookie": "secret"},
            {"Authorization": "secret"},
            {"Cred": "secret"},
        ):
            self.assertFalse(
                public_table_request(self.url, "akedata", headers, None, "json")
            )

    async def test_restart_reads_disk_without_table_network_and_respects_max_bytes(
        self,
    ):
        calls = []

        def handle(request):
            calls.append(request.url)
            return httpx.Response(
                200, json={"safe": [1, 2, 3]}, headers={"ETag": '"v1"'}
            )

        http._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        first = await http.fetch_json(self.url, namespace="akedata", read_only=True)
        await http.close_http_client()
        http._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.assertEqual(
            await http.fetch_json(self.url, namespace="akedata", read_only=True), first
        )
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            await http.fetch_json(self.url, namespace="akedata", max_bytes=2)

    async def test_invalid_json_is_never_persisted(self):
        http._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"not json")
            )
        )
        with self.assertRaises(ValueError):
            await http.fetch_json(self.url, namespace="akedata")
        self.assertEqual(self.disk.clear(), 0)

    async def test_stale_public_table_revalidates_without_replacing_body(self):
        calls = []

        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"safe": True},
                    headers={"ETag": '"v1"', "Cache-Control": "max-age=0"},
                )
            self.assertEqual(request.headers.get("If-None-Match"), '"v1"')
            return httpx.Response(304, headers={"Cache-Control": "max-age=600"})

        http._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.assertEqual(
            await http.fetch_json(self.url, namespace="akedata"), {"safe": True}
        )
        await http.clear_http_cache(include_disk=False)
        self.assertEqual(
            await http.fetch_json(self.url, namespace="akedata"), {"safe": True}
        )
        self.assertEqual(len(calls), 2)

    async def test_private_response_is_never_persisted(self):
        http._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"safe": True}, headers={"cache-control": "private"}
                )
            )
        )
        await http.fetch_json(self.url, namespace="akedata")
        self.assertEqual(self.disk.clear(), 0)
