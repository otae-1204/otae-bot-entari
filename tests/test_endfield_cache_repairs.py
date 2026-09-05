from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from otae_bot.infrastructure.http import client as http
from otae_bot.infrastructure.http.disk import (
    DiskImage,
    PublicImageDiskCache,
    public_image_request,
)
from plugins.endfield import handlers as endfield
from plugins.endfield.rendering.health import record_assets, record_fallback_success


class HttpCacheRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        self.disk = PublicImageDiskCache(
            Path(self.directory.name) / "images.sqlite3", 1024
        )
        self.patcher = patch.object(http, "public_images", self.disk)
        self.patcher.start()

    async def asyncTearDown(self):
        await http.close_http_client()
        self.patcher.stop()
        self.directory.cleanup()

    def transport(self, handler):
        http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_json_parse_once_shared_readonly_and_mutable_copies(self):
        self.transport(lambda request: httpx.Response(200, content=b'{"a":[{"x":1}]}'))
        with patch.object(http.json, "loads", wraps=json.loads) as parse:
            first = await http.fetch_json(
                "https://example.test/a", namespace="test", read_only=True
            )
            second = await http.fetch_json(
                "https://example.test/a", namespace="test", read_only=True
            )
            mutable = await http.fetch_json("https://example.test/a", namespace="test")
            self.assertEqual(parse.call_count, 1)
        self.assertIs(first, second)
        self.assertIsInstance(first, dict)
        self.assertIsInstance(first["a"], list)
        with self.assertRaises(TypeError):
            first["a"][0]["x"] = 2
        with self.assertRaises(TypeError):
            first["a"].append(3)
        mutable["a"][0]["x"] = 9
        self.assertEqual(first["a"][0]["x"], 1)

    async def test_auth_context_is_not_coalesced_and_hit_rechecks_limit(self):
        calls = []

        def handle(request):
            calls.append(request.headers["authorization"])
            return httpx.Response(200, content=b"12345")

        self.transport(handle)
        for credential in ("test-a", "test-b"):
            await http.fetch_bytes(
                "https://example.test/a",
                namespace="test",
                headers={"authorization": credential},
            )
        self.assertEqual(calls, ["test-a", "test-b"])
        with self.assertRaises(ValueError):
            await http.fetch_bytes(
                "https://example.test/a",
                namespace="test",
                headers={"authorization": "test-a"},
                max_bytes=3,
            )
        self.assertEqual(len(calls), 2)

    async def test_metrics_are_consistent_while_waiters_are_inflight(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def handle(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True})

        self.transport(handle)
        one = asyncio.create_task(
            http.fetch_json("https://example.test/a", namespace="metrics")
        )
        await started.wait()
        two = asyncio.create_task(
            http.fetch_json("https://example.test/a", namespace="metrics")
        )
        await asyncio.sleep(0)
        stats = await http.get_http_cache_stats("metrics")
        self.assertEqual((stats.direct_hits, stats.coalesced, stats.misses), (0, 1, 1))
        release.set()
        await asyncio.gather(one, two)

    async def test_implicit_cookie_and_client_auth_never_enter_public_disk(self):
        calls = []

        def handle(request):
            identity = (
                request.headers.get("cookie")
                or request.headers.get("authorization")
                or "public"
            )
            calls.append(identity)
            return httpx.Response(
                200, content=identity.encode(), headers={"content-type": "image/png"}
            )

        self.transport(handle)
        url = "https://assets.fz.wiki/a.png"
        for identity in ("one", "two"):
            http._client.cookies.set("session", identity)
            response = await http.fetch_bytes(url, namespace="endfield-assets")
            self.assertEqual(response.content, f"session={identity}".encode())
        self.assertFalse(self.disk.path.exists())
        http._client.cookies.clear()
        for identity in ("one", "two"):
            http._client.auth = httpx.BasicAuth(identity, "test-password")
            await http.fetch_bytes(url, namespace="endfield-assets")
        self.assertEqual(len(set(calls)), 4)
        self.assertFalse(self.disk.path.exists())

    async def test_table_and_asset_eviction_does_not_displace_api_pool(self):
        self.transport(lambda request: httpx.Response(200, json={"value": 1}))
        await http.fetch_json("https://example.test/api", namespace="api-test")
        await http.fetch_json(
            "https://data.akedata.wiki/data/table.json", namespace="table-test"
        )
        await http.fetch_bytes("https://example.test/icon", namespace="asset-test")
        self.assertEqual((await http._response_cache.stats()).entries, 1)
        self.assertEqual((await http._table_cache.stats()).entries, 1)
        self.assertEqual((await http._asset_cache.stats()).entries, 1)
        await http._table_cache.clear()
        await http._asset_cache.clear()
        await http.fetch_json("https://example.test/api", namespace="api-test")
        self.assertEqual((await http.get_http_cache_stats("api-test")).direct_hits, 1)

    async def test_disk_restart_and_expired_etag_revalidation(self):
        calls = []

        def handle(request):
            calls.append(request.headers.get("if-none-match"))
            if len(calls) > 1:
                return httpx.Response(304, headers={"cache-control": "max-age=60"})
            return httpx.Response(
                200,
                content=b"image",
                headers={
                    "content-type": "image/png",
                    "etag": '"v1"',
                    "cache-control": "max-age=60",
                },
            )

        self.transport(handle)
        url = "https://data.akedata.wiki/public/images/a.png"
        with patch.object(http.time, "time", return_value=1000):
            first = await http.fetch_bytes(url, namespace="endfield-assets")
            await http.close_http_client()
            self.transport(handle)
            again = await http.fetch_bytes(url, namespace="endfield-assets")
        self.assertEqual(first.content, again.content)
        self.assertEqual(len(calls), 1)
        await http.clear_http_cache(include_disk=False)
        with patch.object(http.time, "time", return_value=1061):
            refreshed = await http.fetch_bytes(url, namespace="endfield-assets")
        self.assertEqual(refreshed.content, b"image")
        self.assertEqual(calls, [None, '"v1"'])
        self.assertEqual(refreshed.status_code, 200)

    async def test_clear_blocks_inflight_public_disk_republication(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handle(request):
            nonlocal calls
            calls += 1
            content = b"old" if calls == 1 else b"new"
            if calls == 1:
                started.set()
                await release.wait()
            return httpx.Response(
                200, content=content, headers={"content-type": "image/png"}
            )

        self.transport(handle)
        url = "https://assets.fz.wiki/a.png"
        old = asyncio.create_task(http.fetch_bytes(url, namespace="endfield-assets"))
        await started.wait()
        await http.clear_http_cache("endfield-")
        new = await http.fetch_bytes(url, namespace="endfield-assets")
        release.set()
        self.assertEqual((await old).content, b"old")
        self.assertEqual(new.content, b"new")
        await http.clear_http_cache(include_disk=False)
        self.assertEqual(
            (await http.fetch_bytes(url, namespace="endfield-assets")).content, b"new"
        )
        self.assertEqual(calls, 2)

    async def test_disk_does_not_extend_original_expiry_in_memory(self):
        self.transport(
            lambda request: httpx.Response(
                200,
                content=b"x",
                headers={"content-type": "image/png", "cache-control": "max-age=10"},
            )
        )
        with patch.object(http.time, "time", return_value=100):
            await http.fetch_bytes(
                "https://assets.fz.wiki/a.png", namespace="endfield-assets"
            )
        await http.clear_http_cache(include_disk=False)
        with patch.object(http.time, "time", return_value=109):
            result = await http.fetch_bytes(
                "https://assets.fz.wiki/a.png", namespace="endfield-assets"
            )
        self.assertEqual(result.expires_at, 110)

    async def test_private_response_and_api_are_never_persisted(self):
        self.transport(
            lambda request: httpx.Response(
                200,
                content=b"private",
                headers={
                    "content-type": "image/png",
                    "cache-control": "private, no-store",
                },
            )
        )
        await http.fetch_bytes(
            "https://assets.fz.wiki/a.png", namespace="endfield-assets"
        )
        self.assertFalse(self.disk.path.exists())
        self.assertEqual((await http.get_http_cache_stats()).entries, 0)


class DiskCacheTests(unittest.TestCase):
    def test_public_policy_rejects_credentials_query_and_unknown_hosts(self):
        url = "https://assets.fz.wiki/a.png"
        self.assertTrue(public_image_request(url, "endfield-assets", {}, None, "bytes"))
        for request_url, headers, kind in (
            (url + "?token=test", {}, "bytes"),
            (url, {"authorization": "test"}, "bytes"),
            ("https://zonai.skland.com/api/v1/game/player/info", {}, "bytes"),
            (url, {}, "json"),
        ):
            self.assertFalse(
                public_image_request(
                    request_url, "endfield-assets", headers, None, kind
                )
            )

    def test_budget_integrity_and_clear_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PublicImageDiskCache(Path(directory) / "public.sqlite3", 8)
            generation = cache.register("endfield-assets")
            value = DiskImage(b"12345", "image/png", "tag", "", 100, 60)
            cache.put("a", "endfield-assets", value, generation)
            cache.put("b", "endfield-assets", value, generation)
            self.assertIsNone(cache.get("a", 10))
            self.assertEqual(cache.get("b", 10), value)
            connection = cache._connect()
            connection.execute(
                "UPDATE public_images_v1 SET content=? WHERE key='b'", (b"wrong",)
            )
            connection.commit()
            self.assertIsNone(cache.get("b", 10))
            cache.clear("endfield-")
            cache.put("old", "endfield-assets", value, generation)
            self.assertIsNone(cache.get("old", 10))
            cache.close()


@dataclass
class PageView:
    nickname: str = "test"
    balance: int = 1


class AccountPageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await endfield._ACCOUNT_PAGE_CACHE.clear()
        self.role = SimpleNamespace(role_id="test-role", server_id="1")

    async def asyncTearDown(self):
        await endfield._ACCOUNT_PAGE_CACHE.close()

    async def test_identical_views_reuse_and_balance_identity_scope_invalidate(self):
        render = AsyncMock(return_value=(b"png",))

        async def page(role=self.role, group=True, view=PageView()):
            return await endfield._render_account_pages(
                "detail", role, group, view, render
            )

        await page()
        await page()
        self.assertEqual(render.await_count, 1)
        await page(group=False)
        await page(view=PageView(balance=2))
        await page(role=SimpleNamespace(role_id="another-role", server_id="1"))
        self.assertEqual(render.await_count, 4)

    async def test_missing_asset_is_not_cached_but_recovered_fallback_is(self):
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            record_assets(["primary"], {})
            if calls > 1:
                record_fallback_success(["primary", "fallback"])
            return (b"png",)

        for _ in range(3):
            await endfield._render_account_pages(
                "detail", self.role, True, PageView(), render
            )
        self.assertEqual(calls, 2)

    async def test_detail_still_fetches_data_on_every_png_hit(self):
        self.role.masked_uid = "masked"
        self.role.nickname = "test"
        self.role.server_name = "test"
        with (
            patch.object(endfield.account_store, "decrypt_token", return_value="test"),
            patch.object(
                endfield, "_card_detail_with_snapshot", AsyncMock(return_value={})
            ) as detail,
            patch.object(
                endfield.official_client,
                "currency_balances",
                AsyncMock(return_value={}),
            ) as balance,
            patch.object(
                endfield, "fetch_account_detail_name_map", AsyncMock(return_value=None)
            ),
            patch.object(
                endfield, "build_account_detail_view", return_value=PageView()
            ),
            patch.object(
                endfield, "draw_account_detail_cards", AsyncMock(return_value=(b"png",))
            ) as draw,
            patch.object(endfield, "_finish_pngs", AsyncMock()),
        ):
            for _ in range(2):
                await endfield._render_account_detail(None, self.role, None, group=True)
        self.assertEqual(
            (detail.await_count, balance.await_count, draw.await_count), (2, 2, 1)
        )


class MedalPaginationRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_height_limit_retries_smaller_pages_and_other_errors_propagate(self):
        from plugins.endfield.rendering import cards

        view = SimpleNamespace(new_medals=list(range(5)))
        failure = RuntimeError("Screenshot element height 999 exceeds limit 100")
        with (
            patch.object(cards, "MEDAL_PAGE_BUDGETS", (3, 2)),
            patch.object(
                cards,
                "_draw_medal_stats_page",
                AsyncMock(side_effect=[failure, failure, b"1", b"2", b"3"]),
            ) as draw,
        ):
            self.assertEqual(
                await cards.draw_medal_stats_card(view), (b"1", b"2", b"3")
            )
            self.assertEqual(draw.await_count, 5)
        with patch.object(
            cards,
            "_draw_medal_stats_page",
            AsyncMock(side_effect=RuntimeError("browser crashed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "browser crashed"):
                await cards.draw_medal_stats_card(view)


class DerivedCacheRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_and_calendar_old_manifest_cannot_repopulate_after_clear(self):
        from plugins.endfield.stages.akedata import AkeDataStageSource
        from plugins.endfield.calendar.akedata import AkeDataVersionCalendarSource

        for source_type in (AkeDataStageSource, AkeDataVersionCalendarSource):
            started, release = asyncio.Event(), asyncio.Event()

            async def manifest():
                started.set()
                await release.wait()
                return {
                    "latest": "old",
                    "versions": [{"id": "old", "tableCfgPath": "old/tables"}],
                }

            source = source_type(SimpleNamespace(akedata_manifest=manifest))
            task = asyncio.create_task(source._latest_version())
            await started.wait()
            source.clear_caches()
            release.set()
            self.assertEqual((await task).id, "old")
            self.assertIsNone(source._version)
            self.assertEqual(source._tables, {})

    async def test_global_clear_reaches_public_and_derived_dependencies(self):
        with (
            patch.object(
                endfield, "clear_http_cache", AsyncMock(return_value=0)
            ) as http_clear,
            patch.object(
                endfield.gacha_asset_cache, "clear_caches", return_value=0
            ) as gacha,
            patch.object(
                endfield.stage_service, "clear_caches", return_value=0
            ) as stages,
            patch.object(
                endfield.calendar_source, "clear_caches", return_value=0
            ) as calendar,
            patch.object(
                endfield, "clear_account_detail_name_map", return_value=0
            ) as names,
            patch.object(
                endfield, "clear_account_investment_catalog", return_value=0
            ) as investment,
            patch.object(endfield, "clear_challenge_locale", return_value=0) as locale,
        ):
            await endfield._clear_endfield_caches("all")
        self.assertEqual(
            [call.args for call in http_clear.await_args_list],
            [("endfield-",), ("akedata",)],
        )
        for cleared in (gacha, stages, calendar, names, investment, locale):
            cleared.assert_called_once()


class GachaBatchRepairTests(unittest.TestCase):
    def test_duplicates_corrected_metadata_and_rollback(self):
        from plugins.endfield.account.store import EndfieldStore, GachaRecord

        store = EndfieldStore(":memory:")
        try:
            role = SimpleNamespace(role_id="test", server_id="1")
            row = GachaRecord(
                "test", "1", "pool", "pool", "type", "1", 100, "item", "old", 5, "角色"
            )
            corrected = replace(row, item_name="new", rarity=6)
            self.assertEqual(store.insert_gacha_records([row, row, corrected]), 1)
            self.assertEqual(store.insert_gacha_records([row, corrected]), 0)
            self.assertEqual(store.list_gacha_records(role)[0].item_name, "new")
            self.assertEqual(store.list_gacha_records(role)[0].rarity, 6)
            with self.assertRaises((ValueError, TypeError)):
                store.insert_gacha_records(
                    [replace(row, seq_id="2"), replace(row, seq_id="3", rarity=None)]
                )
            self.assertEqual(store.count_gacha_records(role), 1)
            self.assertFalse(store.conn.in_transaction)
        finally:
            store.close()


class PersonalQueryCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_reuse_context_but_request_fresh_payloads(self):
        from plugins.endfield.account.client import EndfieldOfficialClient

        client = EndfieldOfficialClient(http=SimpleNamespace())
        context = SimpleNamespace(provider="hypergryph")
        client._skland_context = AsyncMock(return_value=context)
        client._signed_skland_request = AsyncMock(
            side_effect=[
                {"data": {"indieHard": {"value": 1}}},
                {"data": {"indieHard": {"value": 2}}},
            ]
        )
        role = SimpleNamespace(role_id="role", server_id="1")
        self.assertEqual(await client.indie_hard("test-only", role), {"value": 1})
        self.assertEqual(await client.indie_hard("test-only", role), {"value": 2})
        self.assertEqual(client._signed_skland_request.await_count, 2)
        self.assertTrue(
            all(not call.kwargs for call in client._skland_context.await_args_list)
        )

    async def test_expired_context_refreshes_once_and_retains_role_scope(self):
        from plugins.endfield.account.client import (
            EndfieldOfficialClient,
            EndfieldAPIError,
        )

        client = EndfieldOfficialClient(http=SimpleNamespace())
        old, new = (
            SimpleNamespace(provider="gryphline"),
            SimpleNamespace(provider="gryphline"),
        )
        client._skland_context = AsyncMock(side_effect=[old, new])
        client._signed_skland_request = AsyncMock(
            side_effect=[
                EndfieldAPIError("社区请求", "401"),
                {"data": {"warEchoes": {"ok": True}}},
            ]
        )
        role = SimpleNamespace(role_id="role", server_id="2")
        self.assertEqual(
            await client.war_echoes("test-only", role, season_id="3"), {"ok": True}
        )
        client._skland_context.assert_awaited_with(
            "test-only", refresh=True, stale_context=old
        )
        for call in client._signed_skland_request.await_args_list:
            self.assertEqual(
                call.kwargs["params"],
                {"roleId": "role", "serverId": "2", "seasonId": "3"},
            )
            self.assertEqual(call.kwargs["extra_headers"], {"sk-game-role": "3_role_2"})

    async def test_non_auth_errors_do_not_trigger_refresh_loops(self):
        from plugins.endfield.account.client import (
            EndfieldOfficialClient,
            EndfieldAPIError,
        )

        client = EndfieldOfficialClient(http=SimpleNamespace())
        client._skland_context = AsyncMock(
            return_value=SimpleNamespace(provider="hypergryph")
        )
        client._signed_skland_request = AsyncMock(
            side_effect=EndfieldAPIError("社区请求", "429")
        )
        with self.assertRaises(EndfieldAPIError):
            await client.endfield_card_detail(
                "test-only", SimpleNamespace(role_id="role", server_id="1")
            )
        self.assertEqual(client._skland_context.await_count, 1)
        self.assertEqual(client._signed_skland_request.await_count, 1)
