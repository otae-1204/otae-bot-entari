from __future__ import annotations

import asyncio
import unittest

import httpx

from utils.async_cache import AsyncTTLCache
from utils import http_client


class AsyncTTLCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_misses_are_coalesced(self):
        cache = AsyncTTLCache[str, bytes](ttl_seconds=60, max_bytes=1024, sizeof=len)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return b"value"

        values = await asyncio.gather(*(cache.get_or_create("same", factory) for _ in range(6)))
        self.assertEqual(values, [b"value"] * 6)
        self.assertEqual(calls, 1)
        stats = await cache.stats()
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.coalesced, 5)

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


if __name__ == "__main__":
    unittest.main()
