from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from otae_bot.infrastructure.cache import AsyncTTLCache
from otae_bot.infrastructure.http import assets as http_assets
from otae_bot.infrastructure.http import client as http_client
from otae_bot.infrastructure.http import tls
from otae_bot.infrastructure.http.asset_policy import (
    AssetFetchSettings,
    configure_asset_settings,
)
from otae_bot.infrastructure.http.disk import PublicImageDiskCache


class SharedSslContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = dict(tls._contexts)
        tls._contexts.clear()

    def tearDown(self):
        tls._contexts.clear()
        tls._contexts.update(self._saved)

    async def test_context_is_built_once_off_the_event_loop(self):
        built = []

        def fake_create(**kwargs):
            built.append(kwargs)
            return object()

        with mock.patch.object(tls.httpx, "create_ssl_context", side_effect=fake_create):
            first, second = await asyncio.gather(
                tls.ashared_ssl_context(trust_env=False),
                tls.ashared_ssl_context(trust_env=False),
            )
            self.assertIs(tls.shared_ssl_context(trust_env=False), first)
        self.assertIs(first, second)
        self.assertEqual(built, [{"verify": True, "trust_env": False}])

    def test_cert_env_only_affects_trust_env_contexts(self):
        with mock.patch.object(tls.httpx, "create_ssl_context", side_effect=lambda **_: object()):
            with mock.patch.dict(os.environ):
                os.environ.pop("SSL_CERT_FILE", None)
                plain = tls.shared_ssl_context(trust_env=True)
                ignored = tls.shared_ssl_context(trust_env=False)
            with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "custom.pem"}):
                custom = tls.shared_ssl_context(trust_env=True)
                self.assertIs(tls.shared_ssl_context(trust_env=False), ignored)
        self.assertIsNot(plain, custom)


class AsyncTTLCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_expiration_and_lru_byte_eviction(self):
        now = [10.0]
        cache = AsyncTTLCache[str, bytes](
            ttl_seconds=5,
            max_bytes=5,
            sizeof=len,
            clock=lambda: now[0],
        )
        calls = {"a": 0, "b": 0}

        async def make(key: str, value: bytes):
            calls[key] += 1
            return value

        await cache.get_or_create("a", lambda: make("a", b"aaa"))
        await cache.get_or_create("b", lambda: make("b", b"bbb"))
        await cache.get_or_create("a", lambda: make("a", b"aaa"))
        self.assertEqual(calls["a"], 2)
        now[0] += 6
        await cache.get_or_create("a", lambda: make("a", b"aaa"))
        self.assertEqual(calls["a"], 3)
        self.assertGreaterEqual((await cache.stats()).evictions, 2)

    async def test_clear_predicate_prevents_inflight_repopulation(self):
        cache = AsyncTTLCache[str, bytes](ttl_seconds=60, max_bytes=1024, sizeof=len)
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory():
            started.set()
            await release.wait()
            return b"late"

        task = asyncio.create_task(cache.get_or_create("operator:key", factory))
        await started.wait()
        await cache.clear(lambda key: key.startswith("operator:"))
        release.set()
        self.assertEqual(await task, b"late")
        self.assertEqual((await cache.stats()).entries, 0)


class SharedHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http_client.close_http_client()

    async def asyncTearDown(self):
        await http_client.close_http_client()

    def _install_transport(self, handler):
        http_client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            trust_env=False,
        )
        http_client._request_semaphore = None
        http_client._semaphore_loop = None

    async def test_same_request_is_cached_and_singleflighted(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(200, content=b"payload", headers={"content-type": "image/png"})

        self._install_transport(handler)
        results = await asyncio.gather(
            *(
                http_client.fetch_bytes("https://example.test/a", namespace="test-assets")
                for _ in range(5)
            )
        )
        self.assertEqual(calls, 1)
        self.assertTrue(all(result.content == b"payload" for result in results))
        stats = await http_client.get_http_cache_stats("test-assets")
        self.assertEqual(stats.hits, 4)
        self.assertEqual(stats.misses, 1)

    async def test_fetch_many_caps_concurrency_at_eight(self):
        active = 0
        maximum = 0
        lock = asyncio.Lock()

        async def handler(request):
            nonlocal active, maximum
            async with lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return httpx.Response(200, content=b"ok")

        self._install_transport(handler)
        urls = [f"https://example.test/{index}" for index in range(20)]
        results = await http_client.fetch_many(urls, namespace="batch-assets")
        self.assertEqual(len(results), 20)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 8)

    async def test_failures_are_not_cached_and_namespace_clear_is_scoped(self):
        calls = {"bad": 0, "a": 0, "b": 0}

        async def handler(request):
            key = request.url.path.strip("/")
            calls[key] += 1
            if key == "bad":
                return httpx.Response(503, content=b"no")
            return httpx.Response(200, content=key.encode())

        self._install_transport(handler)
        for _ in range(2):
            with self.assertRaises(httpx.HTTPStatusError):
                await http_client.fetch_bytes("https://example.test/bad", namespace="bad-assets")
        self.assertEqual(calls["bad"], 2)

        await http_client.fetch_bytes("https://example.test/a", namespace="alpha-assets")
        await http_client.fetch_bytes("https://example.test/b", namespace="beta-assets")
        self.assertEqual(await http_client.clear_http_cache("alpha-"), 1)
        self.assertEqual((await http_client.get_http_cache_stats("alpha-")).entries, 0)
        self.assertEqual((await http_client.get_http_cache_stats("beta-")).entries, 1)

    async def test_invalid_json_is_not_cached(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, content=b"not-json")
            return httpx.Response(200, json={"ok": True})

        self._install_transport(handler)
        with self.assertRaises(ValueError):
            await http_client.fetch_json("https://example.test/data", namespace="json-api")
        data = await http_client.fetch_json("https://example.test/data", namespace="json-api")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(calls, 2)

    async def test_empty_response_is_not_cached(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"" if calls == 1 else b"ok")

        self._install_transport(handler)
        with self.assertRaises(ValueError):
            await http_client.fetch_bytes("https://example.test/empty", namespace="empty-assets")
        resource = await http_client.fetch_bytes("https://example.test/empty", namespace="empty-assets")
        self.assertEqual(resource.content, b"ok")
        self.assertEqual(calls, 2)

    async def test_close_allows_a_fresh_client(self):
        async def handler(request):
            return httpx.Response(200, content=b"one")

        self._install_transport(handler)
        await http_client.fetch_bytes("https://example.test/one", namespace="close-test")
        await http_client.close_http_client()
        self.assertIsNone(http_client._client)

        async def handler_two(request):
            return httpx.Response(200, content=b"two")

        self._install_transport(handler_two)
        resource = await http_client.fetch_bytes("https://example.test/two", namespace="close-test")
        self.assertEqual(resource.content, b"two")


class AssetFetchResilienceTests(unittest.IsolatedAsyncioTestCase):
    """图床的 404 与超时都是间歇的，取图链路必须重试并说清原因。"""

    @staticmethod
    def _status_error(code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "https://assets.invalid/a.png")
        return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))

    def test_404_is_retryable_but_oversize_is_not(self):
        # 实测：同一批头像 URL 一组 15 次全 404、另一组全 200，所以 404 不能当永久失败
        self.assertEqual(http_client.classify_failure(self._status_error(404)), (False, "http 404"))
        self.assertEqual(http_client.classify_failure(self._status_error(403)), (False, "http 403"))
        self.assertEqual(http_client.classify_failure(self._status_error(429)), (False, "http 429"))
        self.assertEqual(http_client.classify_failure(self._status_error(503)), (False, "http 503"))
        self.assertEqual(http_client.classify_failure(httpx.ConnectTimeout("t")), (False, "connecttimeout"))
        self.assertEqual(http_client.classify_failure(httpx.ReadTimeout("t")), (False, "readtimeout"))
        self.assertEqual(
            http_client.classify_failure(ValueError("HTTP resource exceeds 10 bytes: u")),
            (True, "too_large"),
        )
        self.assertEqual(
            http_client.classify_failure(ValueError("HTTP resource is empty: u")),
            (False, "empty_body"),
        )

    async def test_flaky_404_recovers_on_retry(self):
        calls = {"n": 0}

        async def flaky(url, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._status_error(404)
            return http_client.HttpResource(b"png", "image/png", 200, url)

        with mock.patch.object(http_client, "fetch_bytes", side_effect=flaky):
            results, failures = await http_client.fetch_many_resilient(
                ["https://assets.invalid/a.png"], namespace="t", base_delay_seconds=0, attempts=3
            )
        self.assertEqual(calls["n"], 3)
        self.assertEqual(failures, {})
        self.assertIsNotNone(results["https://assets.invalid/a.png"])

    async def test_every_url_gets_a_slot_and_a_reason(self):
        async def always_404(url, **_kwargs):
            raise self._status_error(404)

        with mock.patch.object(http_client, "fetch_bytes", side_effect=always_404):
            results, failures = await http_client.fetch_many_resilient(
                ["https://assets.invalid/a.png", "https://assets.invalid/b.png"],
                namespace="t",
                base_delay_seconds=0,
            )
        # 调用方不能悄悄少图：失败的也要在映射里，值为 None
        self.assertEqual(set(results), {"https://assets.invalid/a.png", "https://assets.invalid/b.png"})
        self.assertEqual(list(results.values()), [None, None])
        self.assertEqual(set(failures.values()), {"http 404"})
        self.assertEqual(len(failures), 2)

    async def test_oversize_is_not_retried(self):
        calls = {"n": 0}

        async def too_big(url, **_kwargs):
            calls["n"] += 1
            raise ValueError("HTTP resource exceeds 10 bytes: u")

        with mock.patch.object(http_client, "fetch_bytes", side_effect=too_big):
            _results, failures = await http_client.fetch_many_resilient(
                ["https://assets.invalid/a.png"], namespace="t", base_delay_seconds=0
            )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(failures, {"https://assets.invalid/a.png": "too_large"})

    async def test_transient_failures_back_off_between_rounds(self):
        async def always_timeout(url, **_kwargs):
            raise httpx.ConnectTimeout("t")

        delays: list[float] = []

        async def record(gap):
            delays.append(gap)

        with mock.patch.object(http_client, "fetch_bytes", side_effect=always_timeout):
            with mock.patch.object(http_client.asyncio, "sleep", new=record):
                # 素材通道默认只重试 1 次（OTAE_HTTP_ASSET_RETRIES=1）
                await http_client.fetch_many_resilient(["https://assets.invalid/a.png"], namespace="t")
                self.assertEqual(delays, [0.25])
                delays.clear()
                await http_client.fetch_many_resilient(
                    ["https://assets.invalid/a.png"], namespace="t", attempts=3
                )
        self.assertEqual(delays, [0.25, 0.5])

    async def test_resilient_fetch_uses_the_asset_lane(self):
        seen = []

        async def ok(url, **kwargs):
            seen.append(kwargs)
            return http_client.HttpResource(b"png", "image/png", 200, url)

        with mock.patch.object(http_client, "fetch_bytes", side_effect=ok):
            await http_client.fetch_many_resilient(["https://assets.invalid/a.png"], namespace="t")
        self.assertIs(seen[0]["asset"], True)

    def test_circuit_open_is_given_up_immediately(self):
        self.assertEqual(
            http_client.classify_failure(http_client.HostCircuitOpen("cooling down")),
            (True, "circuit_open"),
        )


AKE_IMAGE = "https://data.akedata.wiki/public/images/assets/test/{}.png"
I18N_URL = "https://data.akedata.wiki/public/1.5.3/1-1/TableCfg/I18nTextTable_CN.json"


class CacheKeyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """磁盘 key 由请求头指纹组成：不再构造请求后必须与 build_request 逐字节一致，否则线上整库失配。"""

    async def asyncSetUp(self):
        await http_client.close_http_client()

    async def asyncTearDown(self):
        await http_client.close_http_client()

    async def legacy_coordinates(self, url, *, namespace, kind, params=None, headers=None):
        client = await http_client._get_owned_client()
        built = client.build_request("GET", url, params=params, headers=headers).headers
        key = (
            namespace,
            url,
            http_client._normalized_params(params),
            http_client._header_fingerprint(built),
            kind,
        )
        return key, hashlib.sha256(repr(key).encode()).hexdigest()

    async def test_disk_keys_match_the_request_builder(self):
        cases = [
            (AKE_IMAGE.format("plain"), "endfield-assets", "bytes", None, None),
            (
                "https://data.akedata.wiki/public/1.5.3/1-1/TableCfg/CharacterTable.json",
                "akedata",
                "json",
                None,
                {
                    "User-Agent": "otae-bot-entari/1.0",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://cf.akedata.top/",
                },
            ),
            ("https://bbs.hycdn.cn:443/image/a.png", "endfield-gacha-images", "bytes", None, {"accept": "image/*"}),
            ("https://example.test:8443/api", "example", "json", {"q": "x"}, {"Host": "override.test"}),
        ]
        for url, namespace, kind, params, headers in cases:
            with self.subTest(url=url):
                key, _eligible, disk_key, _ttl = await http_client._cache_coordinates(
                    url,
                    namespace=namespace,
                    response_kind=kind,
                    params=params,
                    headers=headers,
                    ttl_seconds=600,
                )
                expected_key, expected_disk_key = await self.legacy_coordinates(
                    url, namespace=namespace, kind=kind, params=params, headers=headers
                )
                self.assertEqual(key, expected_key)
                self.assertEqual(disk_key, expected_disk_key)

    async def test_client_cookies_still_use_the_builder_and_stay_off_disk(self):
        client = await http_client._get_owned_client()
        client.cookies.set("session", "secret")
        url = "https://assets.fz.wiki/a.png"
        key, eligible, _disk_key, _ttl = await http_client._cache_coordinates(
            url, namespace="endfield-assets", response_kind="bytes", params=None, headers=None, ttl_seconds=600
        )
        self.assertFalse(eligible)
        self.assertEqual(key, (await self.legacy_coordinates(url, namespace="endfield-assets", kind="bytes"))[0])

    def test_pool_routing(self):
        self.assertIs(http_client._pool_for(I18N_URL, "json"), http_client._i18n_cache)
        self.assertIs(
            http_client._pool_for(I18N_URL.replace("_CN", "_EN"), "json"), http_client._table_cache
        )
        self.assertIs(
            http_client._pool_for("https://data.akedata.wiki/manifest.json?t=1", "json"),
            http_client._response_cache,
        )
        self.assertIs(http_client._pool_for(AKE_IMAGE.format("a"), "bytes"), http_client._asset_cache)
        self.assertEqual(http_client._i18n_cache.max_entries, 1)


class _DiskBackedCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.images = PublicImageDiskCache(root / "images.sqlite3", 64 << 20)
        self.tables = PublicImageDiskCache(root / "tables.sqlite3", 64 << 20)
        self.patches = [
            mock.patch.object(http_client, "public_images", self.images),
            mock.patch.object(http_client, "public_tables", self.tables),
        ]
        for patcher in self.patches:
            patcher.start()
        self.requests: list[httpx.Request] = []

    async def asyncTearDown(self):
        await http_client.close_http_client()
        for patcher in self.patches:
            patcher.stop()
        self.images.close()
        self.tables.close()
        self.directory.cleanup()

    def install(self, respond):
        def handler(request):
            self.requests.append(request)
            return respond(request)

        configure_asset_settings(AssetFetchSettings())
        for name in ("_asset_client", "_client"):
            setattr(http_client, name, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def forget_memory(self):
        self.assertTrue(self.images.flush(5))
        self.assertTrue(self.tables.flush(5))
        await http_client.clear_http_cache(include_disk=False)

    @staticmethod
    def png(body: bytes, **headers):
        return httpx.Response(200, content=body, headers={"content-type": "image/png", **headers})


class LargeImageBypassTests(_DiskBackedCase):
    async def test_large_disk_backed_images_skip_memory_and_come_back_from_disk(self):
        body = b"P" * (2 << 20)
        self.install(lambda request: self.png(body, etag='"v1"', **{"cache-control": "max-age=600"}))
        url = AKE_IMAGE.format("portrait")
        first = await http_client.fetch_bytes(url, namespace="endfield-assets", asset=True)
        self.assertEqual(first.digest, hashlib.sha256(body).hexdigest())
        self.assertTrue(first.disk_backed)
        self.assertEqual((await http_client._asset_cache.stats()).entries, 0)
        metrics = (await http_client.get_http_cache_diagnostics())["namespaces"]["endfield-assets"]
        self.assertEqual(metrics["bypassed"], 1)
        await self.forget_memory()
        again = await http_client.fetch_bytes(url, namespace="endfield-assets", asset=True)
        self.assertEqual(again.content, body)
        self.assertEqual(again.digest, first.digest)
        self.assertEqual(len(self.requests), 1)
        metrics = (await http_client.get_http_cache_diagnostics())["namespaces"]["endfield-assets"]
        self.assertEqual((metrics["large_disk_hits"], metrics["bypassed"]), (1, 1))

    async def test_large_images_without_a_disk_copy_stay_in_memory(self):
        self.install(lambda request: self.png(b"C" * (2 << 20)))
        url = "https://example.test/calendar.png"  # not a whitelisted public image host
        await http_client.fetch_bytes(url, namespace="endfield-official-calendar", max_bytes=4 << 20)
        await http_client.fetch_bytes(url, namespace="endfield-official-calendar", max_bytes=4 << 20)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((await http_client._asset_cache.stats()).entries, 1)


class BatchPartitionTests(_DiskBackedCase):
    async def test_hot_batch_reads_disk_in_one_call_then_memory(self):
        self.install(
            lambda request: self.png(request.url.path.encode(), etag='"v1"', **{"cache-control": "max-age=600"})
        )
        urls = [AKE_IMAGE.format(f"icon-{index}") for index in range(12)]
        await http_client.fetch_many_resilient(urls, namespace="endfield-assets")
        self.assertEqual(len(self.requests), 12)
        await self.forget_memory()
        real_get_many = self.images.get_many
        with (
            mock.patch.object(self.images, "get", side_effect=AssertionError("per-key disk read")),
            mock.patch.object(self.images, "get_many", side_effect=real_get_many) as get_many,
            mock.patch.object(http_assets, "logger") as log,
        ):
            results, failures = await http_client.fetch_many_resilient(urls, namespace="endfield-assets")
            third, _ = await http_client.fetch_many_resilient(urls, namespace="endfield-assets")
        self.assertEqual(failures, {})
        self.assertEqual(len(self.requests), 12)  # nothing new from the network
        get_many.assert_called_once()
        for url in urls:
            self.assertEqual(results[url].content, httpx.URL(url).path.encode())
            self.assertEqual(results[url].digest, hashlib.sha256(results[url].content).hexdigest())
            self.assertIs(third[url], results[url])  # published to the memory pool
        batches = [call.args[0] for call in log.info.call_args_list if "fetch batch" in call.args[0]]
        self.assertIn("mem=0 disk=12 net=0", batches[0])
        self.assertIn("mem=12 disk=0 net=0", batches[1])
        log.warning.assert_not_called()

    async def test_expired_rows_and_misses_still_go_to_the_network(self):
        self.install(lambda request: self.png(b"img", etag='"v1"', **{"cache-control": "max-age=0"}))
        cached, missing = AKE_IMAGE.format("expired"), AKE_IMAGE.format("missing")
        await http_client.fetch_bytes(cached, namespace="endfield-assets", asset=True)
        await self.forget_memory()
        with mock.patch.object(http_assets, "logger") as log:
            results, failures = await http_client.fetch_many_resilient(
                [cached, missing, "https://assets.invalid/x.png"], namespace="endfield-assets", attempts=1
            )
        self.assertEqual(failures, {})
        self.assertTrue(all(resource is not None for resource in results.values()))
        revalidation = [
            request for request in self.requests[1:] if request.url.path.endswith("/expired.png")
        ]
        self.assertEqual(revalidation[0].headers.get("if-none-match"), '"v1"')  # revalidated, not reused
        batch = next(call.args[0] for call in log.info.call_args_list if "fetch batch" in call.args[0])
        self.assertIn("mem=0 disk=0 net=3", batch)


class RevalidateTests(_DiskBackedCase):
    async def test_revalidate_sends_a_conditional_request_despite_fresh_copies(self):
        def respond(request):
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304, headers={"etag": '"v1"', "cache-control": "max-age=600"})
            return httpx.Response(
                200,
                content=json.dumps({"k": "v"}).encode(),
                headers={"etag": '"v1"', "cache-control": "max-age=600", "content-type": "application/json"},
            )

        self.install(respond)
        first = await http_client.fetch_json(I18N_URL, namespace="akedata", ttl_seconds=86400)
        self.assertEqual(await http_client.fetch_json(I18N_URL, namespace="akedata", ttl_seconds=86400), first)
        self.assertEqual(len(self.requests), 1)  # memory serves the second read
        self.assertTrue(self.tables.flush(5))
        again = await http_client.fetch_json(I18N_URL, namespace="akedata", ttl_seconds=86400, revalidate=True)
        self.assertEqual(again, {"k": "v"})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1].headers.get("if-none-match"), '"v1"')
        self.assertTrue(self.tables.flush(5))
        with sqlite3.connect(self.tables.path) as connection:
            (stored,) = connection.execute("SELECT content FROM public_images_v1").fetchone()
        self.assertEqual(json.loads(stored), {"k": "v"})  # 304 kept the stored body
        await http_client.fetch_json(I18N_URL, namespace="akedata", ttl_seconds=86400)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((await http_client._i18n_cache.stats()).entries, 1)

    async def test_i18n_slot_outlives_the_server_max_age_but_not_private_copies(self):
        now = [1000.0]
        self.install(
            lambda request: httpx.Response(
                200, content=b"{}", headers={"cache-control": "max-age=60", "content-type": "application/json"}
            )
        )
        with mock.patch.object(http_client.time, "time", side_effect=lambda: now[0]):
            await http_client.fetch_json(I18N_URL, namespace="akedata", ttl_seconds=86400)
            fresh = http_client._i18n_cache._entries
            (entry,) = fresh.values()
        self.assertGreater(entry.expires_at - http_client._i18n_cache._clock(), 3600)
        self.assertEqual(http_client._resident_ttl(http_client.HttpResource(b"", "", 200, "", expires_at=now[0] - 1)), 0)
        stale = http_client.HttpResource(b"", "", 200, "", expires_at=now[0] + 60, stale=True)
        with mock.patch.object(http_client.time, "time", return_value=now[0]):
            self.assertEqual(http_client._resident_ttl(stale), 60)


if __name__ == "__main__":
    unittest.main()
