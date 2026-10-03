"""素材通道：独立并发、按主机熔断、整卡截止时间、代理与主机改写（默认关闭）。

全部走 httpx.MockTransport，不访问外网。
"""

from __future__ import annotations

import asyncio
import re
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpcore
import httpx

from otae_bot.infrastructure.http import asset_policy
from otae_bot.infrastructure.http import client as http_client
from otae_bot.infrastructure.http.asset_policy import (
    AssetFetchSettings,
    DirectRoutes,
    HostCircuitBreaker,
    HostCircuitOpen,
    asset_render_budget,
    configure_asset_settings,
    load_asset_settings,
    proxied_host,
    rewrite_asset_url,
)


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://img.test/a.png")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HostCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.breaker = HostCircuitBreaker(threshold=3, cooldown_seconds=300, clock=self.clock)
        self.log = mock.patch.object(asset_policy, "logger").start()
        self.addCleanup(mock.patch.stopall)

    def _fail(self, times: int, host: str = "bbs.hycdn.cn", error=None):
        for _ in range(times):
            self.assertTrue(self.breaker.acquire(host))
            self.breaker.record(host, error or httpx.ConnectTimeout("t"))

    def test_opens_after_consecutive_connect_failures_and_skips_during_cooldown(self):
        self._fail(2)
        self.assertFalse(self.breaker.blocked("bbs.hycdn.cn"))
        self._fail(1, error=httpx.ConnectError("refused"))
        self.assertTrue(self.breaker.blocked("bbs.hycdn.cn"))
        self.assertFalse(self.breaker.acquire("bbs.hycdn.cn"))
        # 其它主机不受影响
        self.assertFalse(self.breaker.blocked("web.hycdn.cn"))
        self.assertTrue(self.breaker.acquire("web.hycdn.cn"))
        self.clock.now += 299
        self.assertTrue(self.breaker.blocked("bbs.hycdn.cn"))
        self.assertEqual(self.log.warning.call_count, 1)

    def test_http_status_does_not_count_and_breaks_the_streak(self):
        self._fail(2)
        self.breaker.record("bbs.hycdn.cn", _status_error(404))
        self._fail(2)
        self.assertFalse(self.breaker.blocked("bbs.hycdn.cn"))
        for _ in range(10):
            self.breaker.record("bbs.hycdn.cn", _status_error(404))
        self.assertFalse(self.breaker.blocked("bbs.hycdn.cn"))
        self.log.warning.assert_not_called()

    def test_half_open_allows_one_probe_and_success_closes(self):
        self._fail(3)
        self.clock.now += 300
        self.assertFalse(self.breaker.blocked("bbs.hycdn.cn"))
        self.assertTrue(self.breaker.acquire("bbs.hycdn.cn"))
        # 探测请求在途时，其余请求仍跳过
        self.assertTrue(self.breaker.blocked("bbs.hycdn.cn"))
        self.assertFalse(self.breaker.acquire("bbs.hycdn.cn"))
        self.breaker.record("bbs.hycdn.cn", None)
        self.assertFalse(self.breaker.blocked("bbs.hycdn.cn"))
        self.assertEqual(self.breaker.snapshot(), {})
        self.assertEqual(self.log.warning.call_count, 1)
        self.assertEqual(self.log.info.call_count, 1)

    def test_failed_probe_rearms_cooldown_without_new_warning(self):
        self._fail(3)
        self.clock.now += 300
        self._fail(1)
        self.assertTrue(self.breaker.blocked("bbs.hycdn.cn"))
        self.clock.now += 299
        self.assertTrue(self.breaker.blocked("bbs.hycdn.cn"))
        self.clock.now += 1
        self.assertTrue(self.breaker.acquire("bbs.hycdn.cn"))
        self.assertEqual(self.log.warning.call_count, 1)

    def test_cancelled_probe_frees_the_slot(self):
        self._fail(3)
        self.clock.now += 300
        self.assertTrue(self.breaker.acquire("bbs.hycdn.cn"))
        self.breaker.record("bbs.hycdn.cn", asyncio.CancelledError())
        self.assertTrue(self.breaker.acquire("bbs.hycdn.cn"))

    def test_zero_threshold_disables_breaker(self):
        breaker = HostCircuitBreaker(threshold=0, cooldown_seconds=300, clock=self.clock)
        for _ in range(10):
            breaker.record("bbs.hycdn.cn", httpx.ConnectTimeout("t"))
        self.assertFalse(breaker.blocked("bbs.hycdn.cn"))
        self.assertTrue(breaker.acquire("bbs.hycdn.cn"))


class AssetSettingsTests(unittest.TestCase):
    def test_defaults_keep_proxy_rewrite_and_trust_env_off(self):
        settings = load_asset_settings({})
        self.assertEqual(settings.proxy, "")
        self.assertEqual(settings.proxy_hosts, ())
        self.assertFalse(settings.trust_env)
        self.assertEqual(settings.host_rewrites, ())
        self.assertEqual(settings.connect_timeout, 4.0)
        self.assertEqual(settings.read_timeout, 10.0)
        self.assertEqual(settings.retries, 1)
        self.assertEqual(settings.attempts, 2)
        self.assertEqual(settings.breaker_threshold, 3)
        self.assertEqual(settings.breaker_cooldown, 300.0)
        self.assertEqual(settings.render_budget, 25.0)
        url = "https://bbs.hycdn.cn/asset/endfield_attendance/921A397E2765462C009B939E0CD92606.png"
        self.assertEqual(rewrite_asset_url(url, settings), url)

    def test_default_asset_client_is_direct_and_ignores_env_proxy(self):
        with mock.patch.dict(
            "os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7897", "HTTP_PROXY": "http://127.0.0.1:7897"}
        ):
            client = http_client._build_asset_client(load_asset_settings({}))
        try:
            self.assertFalse(client.trust_env)
            self.assertEqual(client._mounts, {})
            transport = client._transport_for_url(httpx.URL("https://bbs.hycdn.cn/a.png"))
            self.assertIs(transport, client._transport)
            self.assertNotIsInstance(transport._pool, httpcore.AsyncHTTPProxy)
        finally:
            asyncio.run(client.aclose())

    def test_env_parsing_and_bounds(self):
        settings = load_asset_settings(
            {
                "OTAE_HTTP_ASSET_CONNECT_TIMEOUT": "3",
                "OTAE_HTTP_ASSET_READ_TIMEOUT": "abc",
                "OTAE_HTTP_ASSET_RETRIES": "99",
                "OTAE_HTTP_ASSET_CONCURRENCY": "0",
                "OTAE_HTTP_ASSET_BREAKER_THRESHOLD": "5",
                "OTAE_HTTP_ASSET_BREAKER_COOLDOWN": "60",
                "OTAE_HTTP_ASSET_RENDER_BUDGET": "0",
                "OTAE_HTTP_ASSET_PROXY": "http://127.0.0.1:7897",
                "OTAE_HTTP_ASSET_PROXY_HOSTS": "BBS.hycdn.cn, *.example.test",
                "OTAE_HTTP_ASSET_TRUST_ENV": "true",
                "OTAE_HTTP_ASSET_HOST_REWRITE": "bbs.hycdn.cn=mirror.test",
            }
        )
        self.assertEqual(settings.connect_timeout, 3.0)
        self.assertEqual(settings.read_timeout, 10.0)
        self.assertEqual(settings.retries, 5)
        self.assertEqual(settings.concurrency, 1)
        self.assertEqual(settings.breaker_threshold, 5)
        self.assertEqual(settings.breaker_cooldown, 60.0)
        self.assertEqual(settings.render_budget, 0.0)
        self.assertEqual(settings.proxy_hosts, ("bbs.hycdn.cn", "*.example.test"))
        self.assertTrue(settings.trust_env)
        self.assertEqual(settings.proxy, "http://127.0.0.1:7897")
        self.assertEqual(
            rewrite_asset_url("https://bbs.hycdn.cn:8443/image/a.png?x=1", settings),
            "https://mirror.test:8443/image/a.png?x=1",
        )

    def test_every_setting_is_documented_in_env_example(self):
        class Recorder(dict):
            def __init__(self):
                super().__init__()
                self.read: set[str] = set()

            def get(self, key, default=None):
                self.read.add(key)
                return default

        environ = Recorder()
        load_asset_settings(environ)
        template = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
        documented = set(re.findall(r"^(OTAE_HTTP_ASSET_[A-Z_]+)=", template, re.MULTILINE))
        self.assertGreaterEqual(len(environ.read), 11)
        self.assertEqual(sorted(environ.read - documented), [])

    def test_rewrite_accepts_json_and_rejects_bad_proxy_scheme(self):
        settings = load_asset_settings(
            {
                "OTAE_HTTP_ASSET_HOST_REWRITE": '{"bbs.hycdn.cn": "mirror.test"}',
                "OTAE_HTTP_ASSET_PROXY": "ftp://127.0.0.1:21",
            }
        )
        self.assertEqual(settings.host_rewrites, (("bbs.hycdn.cn", "mirror.test"),))
        self.assertEqual(settings.proxy, "")
        bare = load_asset_settings({"OTAE_HTTP_ASSET_PROXY": "127.0.0.1:7897"})
        self.assertEqual(bare.proxy, "http://127.0.0.1:7897")

    def test_proxy_hosts_route_only_listed_hosts_through_proxy(self):
        settings = load_asset_settings(
            {
                "OTAE_HTTP_ASSET_PROXY": "http://127.0.0.1:7897",
                "OTAE_HTTP_ASSET_PROXY_HOSTS": "bbs.hycdn.cn,*.example.test",
            }
        )
        client = http_client._build_asset_client(settings)
        try:
            for url in ("https://bbs.hycdn.cn/a.png", "http://bbs.hycdn.cn/a.png", "https://cdn.example.test/a.png"):
                proxied = client._transport_for_url(httpx.URL(url))
                self.assertIsInstance(proxied._pool, httpcore.AsyncHTTPProxy, url)
            for url in ("https://web.hycdn.cn/a.png", "https://ak.hycdn.cn/a.png"):
                direct = client._transport_for_url(httpx.URL(url))
                self.assertIs(direct, client._transport, url)
                self.assertNotIsInstance(direct._pool, httpcore.AsyncHTTPProxy)
            self.assertFalse(client.trust_env)
        finally:
            asyncio.run(client.aclose())

    def test_proxy_without_hosts_routes_every_asset_through_proxy(self):
        client = http_client._build_asset_client(
            load_asset_settings({"OTAE_HTTP_ASSET_PROXY": "http://127.0.0.1:7897"})
        )
        try:
            transport = client._transport_for_url(httpx.URL("https://web.hycdn.cn/a.png"))
            self.assertIsInstance(transport._pool, httpcore.AsyncHTTPProxy)
        finally:
            asyncio.run(client.aclose())


class ProxyModeSettingsTests(unittest.TestCase):
    def test_fallback_is_the_default_and_modes_parse(self):
        defaults = load_asset_settings({})
        self.assertEqual((defaults.proxy_mode, defaults.direct_cooldown), ("fallback", 300.0))
        self.assertFalse(proxied_host("bbs.hycdn.cn", defaults))  # 没配代理：行为不变
        always = load_asset_settings(
            {"OTAE_HTTP_ASSET_PROXY_MODE": "ALWAYS", "OTAE_HTTP_ASSET_DIRECT_COOLDOWN": "60"}
        )
        self.assertEqual((always.proxy_mode, always.direct_cooldown), ("always", 60.0))
        with mock.patch.object(asset_policy, "logger") as log:
            bad = load_asset_settings({"OTAE_HTTP_ASSET_PROXY_MODE": "sometimes"})
        self.assertEqual(bad.proxy_mode, "fallback")
        log.warning.assert_called_once()

    def test_proxied_host_matches_httpx_mount_patterns(self):
        settings = AssetFetchSettings(
            proxy="http://127.0.0.1:7897", proxy_hosts=("bbs.hycdn.cn", "*.sub.test", "*apex.test")
        )
        for host in ("bbs.hycdn.cn", "a.sub.test", "apex.test", "www.apex.test"):
            self.assertTrue(proxied_host(host, settings), host)
        for host in ("web.hycdn.cn", "sub.test", "notapex.test"):
            self.assertFalse(proxied_host(host, settings), host)
        self.assertTrue(proxied_host("any.test", AssetFetchSettings(proxy="http://127.0.0.1:7897")))

    def test_fallback_mode_builds_a_direct_client_and_a_separate_proxy_client(self):
        async def run():
            proxy = "http://127.0.0.1:7897"
            url = httpx.URL("https://bbs.hycdn.cn/a.png")
            try:
                configure_asset_settings(AssetFetchSettings(proxy=proxy, proxy_hosts=("bbs.hycdn.cn",)))
                direct = http_client._get_asset_client()
                self.assertNotIsInstance(direct._transport_for_url(url)._pool, httpcore.AsyncHTTPProxy)
                routed = http_client._get_asset_client(proxy=True)
                self.assertIsInstance(routed._transport_for_url(url)._pool, httpcore.AsyncHTTPProxy)
                await http_client.close_http_client()
                configure_asset_settings(
                    AssetFetchSettings(proxy=proxy, proxy_hosts=("bbs.hycdn.cn",), proxy_mode="always")
                )
                always = http_client._get_asset_client()
                self.assertIsInstance(always._transport_for_url(url)._pool, httpcore.AsyncHTTPProxy)
            finally:
                await http_client.close_http_client()

        asyncio.run(run())


class DirectRoutesTests(unittest.TestCase):
    def test_failure_routes_to_proxy_until_one_probe_succeeds(self):
        clock = FakeClock()
        routes = DirectRoutes(cooldown_seconds=300, clock=clock)
        with mock.patch.object(asset_policy, "logger") as log:
            self.assertTrue(routes.try_direct("bbs.hycdn.cn"))
            routes.record("bbs.hycdn.cn", False)
            routes.record("bbs.hycdn.cn", False)  # 并发的另一个失败不重复告警
            self.assertFalse(routes.try_direct("bbs.hycdn.cn"))
            self.assertTrue(routes.try_direct("web.hycdn.cn"))
            clock.now += 300
            self.assertTrue(routes.try_direct("bbs.hycdn.cn"))  # 一个探测
            self.assertFalse(routes.try_direct("bbs.hycdn.cn"))  # 其余继续走代理
            routes.record("bbs.hycdn.cn", None)  # 探测被取消：释放名额，结论不变
            self.assertTrue(routes.try_direct("bbs.hycdn.cn"))
            routes.record("bbs.hycdn.cn", True)
            self.assertTrue(routes.try_direct("bbs.hycdn.cn"))
            self.assertEqual(routes.snapshot(), {})
        self.assertEqual(log.warning.call_count, 1)
        self.assertEqual(log.info.call_count, 1)


class ProxyFallbackTests(unittest.IsolatedAsyncioTestCase):
    """PROXY_MODE=fallback：先直连，连接失败才走代理；熔断只看最终结果。"""

    proxy_settings = dict(proxy="http://127.0.0.1:7897", proxy_hosts=("bbs.hycdn.cn",))

    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.direct_calls: list[str] = []
        self.proxy_calls: list[str] = []

    async def asyncTearDown(self):
        await http_client.close_http_client()

    def _install(self, *, direct, proxy, **settings):
        configure_asset_settings(AssetFetchSettings(**{**self.proxy_settings, **settings}))

        async def direct_handler(request):
            self.direct_calls.append(request.url.path)
            return await direct(request)

        async def proxy_handler(request):
            self.proxy_calls.append(request.url.path)
            return await proxy(request)

        http_client._asset_client = httpx.AsyncClient(transport=httpx.MockTransport(direct_handler))
        http_client._asset_proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))

    async def _get(self, url: str):
        return await http_client.fetch_bytes(url, namespace="t-assets", asset=True)

    async def test_direct_first_then_proxy_after_a_connect_failure(self):
        reachable = True

        async def direct(request):
            if not reachable:
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, content=b"direct")

        async def proxy(request):
            return httpx.Response(200, content=b"proxy")

        self._install(direct=direct, proxy=proxy)
        self.assertEqual((await self._get("https://bbs.hycdn.cn/image/a.png")).content, b"direct")
        self.assertEqual(self.proxy_calls, [])
        reachable = False
        self.assertEqual((await self._get("https://bbs.hycdn.cn/image/b.png")).content, b"proxy")
        self.assertEqual(self.direct_calls, ["/image/a.png", "/image/b.png"])
        # 记住了直连不通：冷却期内不再先试直连
        self.assertEqual((await self._get("https://bbs.hycdn.cn/image/c.png")).content, b"proxy")
        self.assertEqual(self.direct_calls, ["/image/a.png", "/image/b.png"])
        self.assertEqual(self.proxy_calls, ["/image/b.png", "/image/c.png"])
        self.assertEqual(asset_policy.asset_breaker().snapshot(), {})

    async def test_direct_failures_rescued_by_the_proxy_never_open_the_breaker(self):
        async def direct(request):
            raise httpx.ConnectError("refused", request=request)

        async def proxy(request):
            return httpx.Response(200, content=b"proxy")

        # cooldown 0：每个请求都先试直连，连续失败远超熔断阈值
        self._install(direct=direct, proxy=proxy, breaker_threshold=2, direct_cooldown=0.0)
        results, failures = await http_client.fetch_many_resilient(
            [f"https://bbs.hycdn.cn/image/{index}.png" for index in range(6)],
            namespace="t-assets",
            base_delay_seconds=0,
        )
        self.assertEqual(failures, {})
        self.assertTrue(all(resource.content == b"proxy" for resource in results.values()))
        self.assertGreaterEqual(len(self.direct_calls), 6)
        self.assertFalse(asset_policy.asset_breaker().blocked("bbs.hycdn.cn"))
        self.assertEqual(asset_policy.asset_breaker().snapshot(), {})

    async def test_direct_and_proxy_both_failing_still_opens_the_breaker(self):
        async def direct(request):
            raise httpx.ConnectError("refused", request=request)

        async def proxy(request):
            raise httpx.ProxyError("proxy down", request=request)

        self._install(direct=direct, proxy=proxy, breaker_threshold=2)
        for name in ("a", "b"):
            with self.assertRaises(httpx.ProxyError):
                await self._get(f"https://bbs.hycdn.cn/image/{name}.png")
        self.assertTrue(asset_policy.asset_breaker().blocked("bbs.hycdn.cn"))

    async def test_read_timeout_and_unlisted_hosts_do_not_switch_to_the_proxy(self):
        async def direct(request):
            if request.url.host == "web.hycdn.cn":
                raise httpx.ConnectError("refused", request=request)
            raise httpx.ReadTimeout("slow", request=request)

        async def proxy(request):
            return httpx.Response(200, content=b"proxy")

        self._install(direct=direct, proxy=proxy)
        with self.assertRaises(httpx.ReadTimeout):
            await self._get("https://bbs.hycdn.cn/image/slow.png")
        with self.assertRaises(httpx.ConnectError):
            await self._get("https://web.hycdn.cn/a.png")
        self.assertEqual(self.proxy_calls, [])
        self.assertEqual(asset_policy.asset_direct_routes().snapshot(), {})

    async def test_always_mode_never_tries_direct(self):
        async def direct(request):
            return httpx.Response(200, content=b"via-asset-client")

        async def proxy(request):
            raise AssertionError("always mode routes through the asset client's proxy mounts")

        self._install(direct=direct, proxy=proxy, proxy_mode="always")
        self.assertEqual((await self._get("https://bbs.hycdn.cn/image/a.png")).content, b"via-asset-client")
        self.assertEqual(self.proxy_calls, [])
        self.assertEqual(asset_policy.asset_direct_routes().snapshot(), {})


class AssetLaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.grace = mock.patch.object(http_client, "DEADLINE_GRACE_SECONDS", 0.05)
        self.grace.start()

    async def asyncTearDown(self):
        self.grace.stop()
        await http_client.close_http_client()

    def _install(self, *, asset_handler, api_handler=None, settings: AssetFetchSettings | None = None):
        configure_asset_settings(settings or AssetFetchSettings())
        http_client._asset_client = httpx.AsyncClient(
            transport=httpx.MockTransport(asset_handler), follow_redirects=True
        )
        http_client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                api_handler or (lambda request: httpx.Response(200, json={"ok": True}))
            ),
            follow_redirects=True,
        )

    async def test_unreachable_host_opens_breaker_and_later_batches_send_nothing(self):
        calls: dict[str, int] = {}

        async def handler(request):
            host = request.url.host
            calls[host] = calls.get(host, 0) + 1
            if host == "dead.test":
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, content=b"img", headers={"content-type": "image/png"})

        self._install(
            asset_handler=handler,
            settings=AssetFetchSettings(concurrency=2, breaker_threshold=3),
        )
        urls = [f"https://dead.test/{index}.png" for index in range(40)]
        urls.append("https://ok.test/a.png")
        results, failures = await http_client.fetch_many_resilient(
            urls, namespace="t-assets", base_delay_seconds=0
        )
        self.assertIsNotNone(results["https://ok.test/a.png"])
        self.assertEqual(sum(1 for value in results.values() if value is None), 40)
        # 熔断打开后不再发请求：只有打开前在途的少量请求真正连了一次
        self.assertLessEqual(calls["dead.test"], 4)
        self.assertIn("circuit_open", failures.values())
        self.assertTrue(asset_policy.asset_breaker().blocked("dead.test"))

        before = calls["dead.test"]
        _results, again = await http_client.fetch_many_resilient(
            [f"https://dead.test/more-{index}.png" for index in range(20)],
            namespace="t-assets",
            base_delay_seconds=0,
        )
        self.assertEqual(calls["dead.test"], before)
        self.assertEqual(set(again.values()), {"circuit_open"})

    async def test_404_never_opens_the_breaker(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(404, content=b"missing")

        self._install(asset_handler=handler, settings=AssetFetchSettings(breaker_threshold=2))
        urls = [f"https://img.test/{index}.png" for index in range(6)]
        _results, failures = await http_client.fetch_many_resilient(
            urls, namespace="t-assets", base_delay_seconds=0
        )
        self.assertEqual(set(failures.values()), {"http 404"})
        # 默认重试 1 次：每个 URL 最多 2 次尝试
        self.assertEqual(calls, 12)
        self.assertFalse(asset_policy.asset_breaker().blocked("img.test"))

    async def test_half_open_probe_recovers_host(self):
        clock = FakeClock()
        reachable = False
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if not reachable:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, content=b"img")

        self._install(asset_handler=handler)
        asset_policy._breaker = HostCircuitBreaker(threshold=1, cooldown_seconds=300, clock=clock)
        _results, failures = await http_client.fetch_many_resilient(
            ["https://flaky.test/a.png"], namespace="t-assets", attempts=1
        )
        self.assertEqual(failures, {"https://flaky.test/a.png": "connecterror"})
        with self.assertRaises(HostCircuitOpen):
            await http_client.fetch_bytes("https://flaky.test/b.png", namespace="t-assets", asset=True)
        self.assertEqual(calls, 1)

        reachable = True
        clock.now += 301
        resource = await http_client.fetch_bytes(
            "https://flaky.test/c.png", namespace="t-assets", asset=True
        )
        self.assertEqual(resource.content, b"img")
        self.assertFalse(asset_policy._breaker.blocked("flaky.test"))
        self.assertEqual(calls, 2)

    async def test_saturated_asset_lane_does_not_queue_api_requests(self):
        release = asyncio.Event()
        started = 0

        async def asset_handler(request):
            nonlocal started
            started += 1
            await release.wait()
            return httpx.Response(200, content=b"img")

        self._install(
            asset_handler=asset_handler,
            settings=AssetFetchSettings(concurrency=2, render_budget=0),
        )
        assets = asyncio.create_task(
            http_client.fetch_many_resilient(
                [f"https://img.test/{index}.png" for index in range(20)], namespace="t-assets"
            )
        )
        for _ in range(50):
            if started >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(started, 2)
        # 素材通道占满时，API 请求不排队；共享的 8 槽 API 信号量一个也没被占
        api = await asyncio.wait_for(
            http_client.fetch_json("https://api.test/attendance", namespace="t-api", ttl_seconds=0),
            timeout=1,
        )
        self.assertEqual(api, {"ok": True})
        self.assertEqual(http_client._get_semaphore()._value, http_client.DEFAULT_CONCURRENCY)
        release.set()
        results, failures = await assets
        self.assertEqual(failures, {})
        self.assertEqual(len(results), 20)

    async def test_asset_requests_use_short_connect_timeout(self):
        seen = []

        async def handler(request):
            seen.append(request.extensions["timeout"])
            return httpx.Response(200, content=b"img")

        self._install(asset_handler=handler)
        await http_client.fetch_bytes("https://img.test/t.png", namespace="t-assets", asset=True)
        await http_client.fetch_bytes(
            "https://img.test/u.png", namespace="t-assets", asset=True, timeout_seconds=2.0
        )
        self.assertEqual(seen[0]["connect"], 4.0)
        self.assertEqual(seen[0]["read"], 10.0)
        self.assertEqual(seen[1]["connect"], 2.0)

    async def test_host_rewrite_is_applied_only_when_configured(self):
        hosts = []

        async def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, content=b"img")

        self._install(asset_handler=handler)
        await http_client.fetch_bytes("https://bbs.hycdn.cn/asset/a.png", namespace="t-assets", asset=True)
        configure_asset_settings(
            AssetFetchSettings(host_rewrites=(("bbs.hycdn.cn", "mirror.test"),))
        )
        resource = await http_client.fetch_bytes(
            "https://bbs.hycdn.cn/asset/b.png", namespace="t-assets", asset=True
        )
        self.assertEqual(hosts, ["bbs.hycdn.cn", "mirror.test"])
        self.assertEqual(resource.url, "https://mirror.test/asset/b.png")

    async def test_batch_budget_returns_resolved_images_and_marks_the_rest(self):
        async def handler(request):
            if request.url.host == "slow.test":
                await asyncio.sleep(30)
            return httpx.Response(200, content=b"img")

        self._install(asset_handler=handler)
        started = time.monotonic()
        results, failures = await http_client.fetch_many_resilient(
            ["https://fast.test/a.png", "https://slow.test/b.png"],
            namespace="t-assets",
            budget_seconds=0.2,
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(results["https://fast.test/a.png"])
        self.assertIsNone(results["https://slow.test/b.png"])
        self.assertEqual(failures, {"https://slow.test/b.png": "deadline"})

    async def test_card_budget_is_shared_across_batches_but_cache_hits_still_resolve(self):
        async def handler(request):
            if request.url.host == "slow.test":
                await asyncio.sleep(30)
            return httpx.Response(200, content=b"img")

        self._install(asset_handler=handler)
        await http_client.fetch_bytes("https://fast.test/cached.png", namespace="t-assets", asset=True)
        started = time.monotonic()
        with asset_render_budget(0.2):
            await http_client.fetch_many_resilient(["https://slow.test/1.png"], namespace="t-assets")
            results, failures = await http_client.fetch_many_resilient(
                ["https://slow.test/2.png", "https://fast.test/cached.png"], namespace="t-assets"
            )
        # 第二批不会再等一整个预算；内存缓存命中仍然拿得到
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNotNone(results["https://fast.test/cached.png"])
        self.assertEqual(failures, {"https://slow.test/2.png": "deadline"})


class CardDeadlineTests(unittest.IsolatedAsyncioTestCase):
    """到点停止等待，用已拿到的图出图，结果不报错。"""

    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.grace = mock.patch.object(http_client, "DEADLINE_GRACE_SECONDS", 0.05)
        self.grace.start()

        async def handler(request):
            if request.url.host == "slow.test":
                await asyncio.sleep(30)
            return httpx.Response(200, content=b"\x89PNG-ok", headers={"content-type": "image/png"})

        configure_asset_settings(AssetFetchSettings(render_budget=0.3))
        http_client._asset_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        self.grace.stop()
        await http_client.close_http_client()

    async def test_endfield_cards_prepare_assets_stops_at_budget(self):
        from plugins.endfield.rendering import cards

        started = time.monotonic()
        prepared = await cards._prepare_assets(
            ["https://fast.test/icon.png", "https://slow.test/portrait.png"], inline=True
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(prepared.urls["https://fast.test/icon.png"].startswith("data:image/png;base64,"))
        self.assertEqual(prepared.urls["https://slow.test/portrait.png"], "")
        self.assertEqual(prepared.failures, {"https://slow.test/portrait.png": "deadline"})

    async def test_endfield_account_render_finishes_with_partial_assets(self):
        from plugins.endfield import handlers as endfield
        from plugins.endfield.account import draw as account_draw

        @dataclass
        class View:
            name: str = "deadline-test"

        async def render():
            assets = await account_draw._prepare_assets(
                ["https://fast.test/avatar.png", *(f"https://slow.test/{i}.png" for i in range(30))],
                inline=True,
            )
            loaded = sum(1 for url, mapped in assets.urls.items() if mapped and "test/" in url)
            return (f"page loaded={loaded}".encode(),)

        role = SimpleNamespace(role_id="deadline-role", server_id="1")
        started = time.monotonic()
        pages = await endfield._render_account_pages("deadline", role, False, View(), render)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(pages, (b"page loaded=1",))


if __name__ == "__main__":
    unittest.main()
