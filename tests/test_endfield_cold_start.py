from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from otae_bot.infrastructure.cache import AsyncTTLCache
from otae_bot.infrastructure.http import client as http
from otae_bot.infrastructure.http.disk import DiskImage, PublicImageDiskCache
from plugins.endfield import cold_start, handlers as endfield
from plugins.endfield.providers import akedata


I18N_PATH = "/public/1.0.0/1-1/TableCfg/I18nTextTable_CN.json"
IMAGE_URL = "https://data.akedata.wiki/public/images/assets/beyond/test-icon.png"


class _Matcher:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message):
        self.sent.append(message)


class DiskMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.disk = PublicImageDiskCache(Path(self.directory.name) / "images.sqlite3", 8 * 1024 * 1024)

    def tearDown(self):
        self.disk.close()
        self.directory.cleanup()

    def _store(self, *, validated_at: float, max_age: float, etag: str = "", modified: str = "", body: bytes = b"png"):
        generation = self.disk.register("endfield-assets")
        self.disk.put(
            "row",
            "endfield-assets",
            DiskImage(body, "image/png", etag, modified, validated_at, max_age),
            generation,
        )

    def test_metadata_reads_freshness_fields_only(self):
        body = b"x" * 4096
        self._store(validated_at=10, max_age=20, etag='"abc"', body=body)
        connection = self.disk._connect()
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            meta = self.disk.metadata("row")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(meta.validated_at, 10)
        self.assertEqual(meta.max_age, 20)
        self.assertEqual(meta.etag, '"abc"')
        self.assertFalse(hasattr(meta, "content"))
        selected = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertTrue(selected)
        self.assertTrue(all("content" not in sql.lower() for sql in selected))
        self.assertIsNotNone(self.disk.get("row", 1024 * 1024))

    def test_metadata_does_not_reject_a_corrupt_body(self):
        self._store(validated_at=time.time(), max_age=60, body=b"intact")
        connection = self.disk._connect()
        connection.execute("UPDATE public_images_v1 SET content=? WHERE key=?", (b"damaged", "row"))
        connection.commit()
        self.assertIsNotNone(self.disk.metadata("row"))
        self.assertIsNone(self.disk.get("row", 1024 * 1024))
        self.assertIsNone(self.disk.metadata("row"))


class CacheContainsTests(unittest.IsolatedAsyncioTestCase):
    async def test_contains_ignores_inflight_and_expired_entries(self):
        cache = AsyncTTLCache[str, bytes](ttl_seconds=60, max_bytes=1024, sizeof=len)
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory():
            started.set()
            await release.wait()
            return b"value"

        task = asyncio.create_task(cache.get_or_create("key", factory))
        await started.wait()
        self.assertFalse(cache.contains("key"))
        release.set()
        self.assertEqual(await task, b"value")
        self.assertTrue(cache.contains("key"))

        expired = AsyncTTLCache[str, bytes](
            ttl_seconds=0, max_bytes=1024, sizeof=len, clock=lambda: 0
        )
        await expired.get_or_create("key", lambda: _ready(b"old"), ttl_seconds=1)
        expired._clock = lambda: 5
        self.assertFalse(expired.contains("key"))


class ColdStartNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._loaded = akedata.i18n_loaded_path()
        akedata.clear_i18n_process_warm()
        await http.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        self.images = PublicImageDiskCache(Path(self.directory.name) / "images.sqlite3", 8 * 1024 * 1024)
        self.tables = PublicImageDiskCache(Path(self.directory.name) / "tables.sqlite3", 8 * 1024 * 1024)
        self.image_patch = patch.object(http, "public_images", self.images)
        self.table_patch = patch.object(http, "public_tables", self.tables)
        self.image_patch.start()
        self.table_patch.start()

    async def asyncTearDown(self):
        await http.close_http_client()
        self.image_patch.stop()
        self.table_patch.stop()
        self.images.close()
        self.tables.close()
        self.directory.cleanup()
        if self._loaded:
            akedata.remember_i18n_loaded(self._loaded)
        else:
            akedata.clear_i18n_process_warm()

    def transport(self, handler):
        http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_unarmed_command_does_not_send_or_probe(self):
        matcher = _Matcher()
        with patch.object(cold_start, "remote_assets_cold", new=AsyncMock(return_value=True)) as probe:
            await cold_start.note_remote_assets([IMAGE_URL], namespace=cold_start.REMOTE_ASSET_NAMESPACE)
            await cold_start.notice_default_ake_public()
        probe.assert_not_awaited()
        self.assertEqual(matcher.sent, [])

    async def test_notice_is_once_per_command_and_each_session_sends(self):
        async def run():
            matcher = _Matcher()
            with cold_start.cold_start_command(matcher):
                with patch.object(cold_start, "ake_public_tables_cold", new=AsyncMock(return_value=True)):
                    await cold_start.notice_default_ake_public()
                    await cold_start.notice_default_ake_public()
                with patch.object(cold_start, "remote_assets_cold", new=AsyncMock(return_value=True)):
                    await cold_start.note_remote_assets(
                        [IMAGE_URL], namespace=cold_start.REMOTE_ASSET_NAMESPACE
                    )
            return matcher

        first, second = await asyncio.gather(run(), run())
        self.assertEqual(first.sent, [cold_start.COLD_START_NOTICE])
        self.assertEqual(second.sent, [cold_start.COLD_START_NOTICE])

    async def test_process_not_warm_is_cold_without_reading_manifest(self):
        manifest = AsyncMock(side_effect=AssertionError("manifest should not be fetched"))
        with patch.object(cold_start, "fetch_akedata_manifest", manifest):
            self.assertTrue(await cold_start.ake_public_tables_cold())
        manifest.assert_not_awaited()

    async def test_path_mismatch_is_cold_even_when_the_new_resource_is_cached(self):
        akedata.remember_i18n_loaded(I18N_PATH)
        manifest = {
            "latest": "9.9.9@1-1",
            "versions": [{"id": "9.9.9@1-1", "tableCfgPath": "public/9.9.9/1-1/TableCfg"}],
        }
        cached = AsyncMock(side_effect=AssertionError("cached resource should not be probed"))
        with (
            patch.object(cold_start, "fetch_akedata_manifest", AsyncMock(return_value=manifest)),
            patch.object(cold_start, "cached_public_resource", cached),
        ):
            self.assertTrue(await cold_start.ake_public_tables_cold())
        cached.assert_not_awaited()

    async def test_warm_process_uses_memory_or_disk_without_reading_the_table_body(self):
        akedata.remember_i18n_loaded(I18N_PATH)
        manifest = {
            "latest": "1.0.0@1-1",
            "versions": [{"id": "1.0.0@1-1", "tableCfgPath": "public/1.0.0/1-1/TableCfg"}],
        }
        with (
            patch.object(cold_start, "fetch_akedata_manifest", AsyncMock(return_value=manifest)),
            patch.object(cold_start, "cached_public_resource", AsyncMock(return_value=True)) as cached,
        ):
            self.assertFalse(await cold_start.ake_public_tables_cold())
        cached.assert_awaited()
        with (
            patch.object(cold_start, "fetch_akedata_manifest", AsyncMock(return_value=manifest)),
            patch.object(cold_start, "cached_public_resource", AsyncMock(return_value=False)),
        ):
            self.assertTrue(await cold_start.ake_public_tables_cold())

    async def test_explicit_fz_ignores_the_ake_warm_flag(self):
        matcher = _Matcher()
        akedata.remember_i18n_loaded(I18N_PATH)

        calls = {"count": 0}

        async def cached(url, **kwargs):
            calls["count"] += 1
            return calls["count"] == 1

        with (
            cold_start.cold_start_command(matcher),
            patch.object(cold_start, "cached_public_resource", cached),
            patch.object(cold_start, "ake_public_tables_cold", AsyncMock(side_effect=AssertionError("ake"))),
        ):
            await cold_start.notice_before_public_data(source="fz", scope="operator", query="莱万汀")
            self.assertEqual(matcher.sent, [])
            akedata.clear_i18n_process_warm()
            await cold_start.notice_before_public_data(source="fz", scope="operator", query="管理员")
            self.assertEqual(matcher.sent, [cold_start.COLD_START_NOTICE])

    async def test_fz_catalog_shortcut_does_not_look_cold(self):
        self.assertFalse(await cold_start.explicit_wiki_responses_cold("fz", "operator", "__all__"))
        self.assertFalse(await cold_start.explicit_wiki_responses_cold("fz", "operator", "干员/莱万汀"))
        self.assertFalse(await cold_start.explicit_wiki_responses_cold("fz", "equipment", "主力量"))

    async def test_remote_assets_ignore_local_urls_and_cached_images(self):
        self.transport(lambda request: httpx.Response(200, content=b"png", headers={"content-type": "image/png"}))
        await http.fetch_bytes(IMAGE_URL, namespace=cold_start.REMOTE_ASSET_NAMESPACE)
        self.assertFalse(
            await cold_start.remote_assets_cold(
                ["", "data:image/png;base64,aaaa", IMAGE_URL],
                namespace=cold_start.REMOTE_ASSET_NAMESPACE,
            )
        )
        self.assertTrue(
            await cold_start.remote_assets_cold(
                [IMAGE_URL, IMAGE_URL.replace("test-icon", "missing")],
                namespace=cold_start.REMOTE_ASSET_NAMESPACE,
            )
        )
        matcher = _Matcher()
        with cold_start.cold_start_command(matcher):
            await cold_start.note_remote_assets(
                ["https://example.test/gacha.png"], namespace="endfield-gacha-images"
            )
        self.assertEqual(matcher.sent, [])

    async def test_disk_hit_and_conditional_revalidation_count_as_cached(self):
        self.transport(
            lambda request: httpx.Response(
                200,
                content=b"png-body",
                headers={"content-type": "image/png", "etag": '"v1"'},
            )
        )
        await http.fetch_bytes(IMAGE_URL, namespace=cold_start.REMOTE_ASSET_NAMESPACE)
        await http.clear_http_cache(cold_start.REMOTE_ASSET_NAMESPACE, include_disk=False)
        gets = []
        original = self.images.get

        def spy_get(*args, **kwargs):
            gets.append(args)
            return original(*args, **kwargs)

        self.images.get = spy_get
        self.assertTrue(
            await http.cached_public_resource(
                IMAGE_URL, namespace=cold_start.REMOTE_ASSET_NAMESPACE, response_kind="bytes"
            )
        )
        self.assertEqual(gets, [])

        connection = self.images._connect()
        connection.execute(
            "UPDATE public_images_v1 SET validated_at=?, max_age=?, etag=?, modified=?",
            (0, 1, "", ""),
        )
        connection.commit()
        self.assertFalse(
            await http.cached_public_resource(
                IMAGE_URL, namespace=cold_start.REMOTE_ASSET_NAMESPACE, response_kind="bytes"
            )
        )
        connection.execute("UPDATE public_images_v1 SET etag=?", ('"stale"',))
        connection.commit()
        self.assertTrue(
            await http.cached_public_resource(
                IMAGE_URL, namespace=cold_start.REMOTE_ASSET_NAMESPACE, response_kind="bytes"
            )
        )

    async def test_i18n_warm_flag_follows_successful_reads_and_dev_clear(self):
        with patch.object(akedata, "fetch_json", AsyncMock(side_effect=RuntimeError("down"))):
            with self.assertRaises(RuntimeError):
                await akedata._get(I18N_PATH)
        self.assertFalse(akedata.i18n_process_warm())

        with patch.object(akedata, "fetch_json", AsyncMock(return_value={"1": "干员"})):
            await akedata._get("/public/1.0.0/1-1/TableCfg/CharacterTable.json")
            self.assertFalse(akedata.i18n_process_warm())
            await akedata._get(I18N_PATH)
        self.assertEqual(akedata.i18n_loaded_path(), I18N_PATH)

        async def zero(*args, **kwargs):
            return 0

        with (
            patch.object(endfield._ACCOUNT_PAGE_CACHE, "clear", zero),
            patch.object(endfield._LOADOUT_CACHE, "clear", zero),
            patch.object(endfield._CARD_CACHE, "clear", zero),
            patch.object(endfield.service, "clear_query_caches", zero),
            patch.object(endfield, "clear_http_cache", zero),
            patch.object(endfield, "clear_account_detail_name_map", return_value=0),
            patch.object(endfield, "clear_account_investment_catalog", return_value=0),
        ):
            await endfield._clear_endfield_caches("operator")
        self.assertFalse(akedata.i18n_process_warm())

        akedata.remember_i18n_loaded(I18N_PATH)
        with (
            patch.object(endfield._ACCOUNT_PAGE_CACHE, "clear", zero),
            patch.object(endfield, "clear_render_asset_caches", return_value=0),
            patch.object(endfield.gacha_asset_cache, "clear_caches", return_value=0),
            patch.object(endfield._CARD_CACHE, "clear", zero),
            patch.object(endfield._LOADOUT_CACHE, "clear", zero),
            patch.object(endfield._CALENDAR_CACHE, "clear", zero),
            patch.object(endfield._CHALLENGE_RENDER_CACHE, "clear", zero),
            patch.object(endfield, "clear_http_cache", zero),
        ):
            await endfield._clear_endfield_caches("icon")
        self.assertTrue(akedata.i18n_process_warm())


async def _ready(value):
    return value
