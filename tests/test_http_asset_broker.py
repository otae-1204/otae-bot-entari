"""AssetBroker：软/硬截止续传、优先级与按主机并发、去重、预取（流量上限、代理主机、热集）。

全部走 httpx.MockTransport 与临时磁盘缓存，不访问外网。
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx

from otae_bot.infrastructure.http import assets
from otae_bot.infrastructure.http import client as http_client
from otae_bot.infrastructure.http.asset_policy import (
    AssetFetchSettings,
    configure_asset_settings,
    host_concurrency,
    load_asset_settings,
)
from otae_bot.infrastructure.http.disk import PublicImageDiskCache

AKE = "https://data.akedata.wiki/public/images/test/{}.png"
NAMESPACE = "endfield-assets"


def _png(body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "image/png", "cache-control": "max-age=600"}
    )


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


class _BrokerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.disk = PublicImageDiskCache(self.root / "images.sqlite3", 64 << 20)
        self.patches = [
            mock.patch.object(http_client, "public_images", self.disk),
            mock.patch.object(http_client, "DEADLINE_GRACE_SECONDS", 0.05),
            mock.patch.object(assets, "CLOSE_WAIT_SECONDS", 0.05),
        ]
        for patcher in self.patches:
            patcher.start()
        self.requests: list[str] = []
        self.broker = assets.broker

    async def asyncTearDown(self):
        await http_client.close_http_client()
        for patcher in self.patches:
            patcher.stop()
        self.disk.close()

    def install(self, respond, **settings):
        async def handler(request):
            self.requests.append(_name(str(request.url)))
            response = respond(request)
            return await response if inspect.isawaitable(response) else response

        configure_asset_settings(AssetFetchSettings(**settings))
        for name in ("_asset_client", "_client"):
            setattr(http_client, name, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def until(self, condition, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "condition not reached")
            await asyncio.sleep(0.005)

    async def disk_key(self, url: str, namespace: str = NAMESPACE) -> str:
        _key, eligible, disk_key, _ttl = await http_client._cache_coordinates(
            url, namespace=namespace, response_kind="bytes", params=None, headers=None, ttl_seconds=600
        )
        self.assertTrue(eligible)
        return disk_key


class SoftDeadlineRefillTests(_BrokerCase):
    async def test_soft_deadline_keeps_downloads_running_and_the_next_query_is_hot(self):
        async def slow(request):
            await asyncio.sleep(0.02)
            return _png(request.url.path.encode())

        self.install(slow, concurrency=4)
        urls = [AKE.format(f"icon-{index}") for index in range(120)]
        started = time.monotonic()
        with mock.patch.object(assets, "logger") as log:
            batch = await http_client.fetch_many_resilient(urls, namespace=NAMESPACE, budget_seconds=0.2)
            elapsed = time.monotonic() - started
            results, failures = batch
            await self.broker.drain()
        self.assertLess(elapsed, 0.6)
        self.assertTrue(batch.stats.soft_hit)
        self.assertGreater(batch.stats.refill, 0)
        self.assertEqual(batch.stats.refill, len(failures))
        self.assertEqual(set(failures.values()), {"deadline"})
        self.assertEqual(sum(1 for value in results.values() if value is not None), 120 - batch.stats.refill)
        batch_line = next(call.args[0] for call in log.info.call_args_list if "fetch batch" in call.args[0])
        self.assertIn(f"soft_hit=true refill_queued={batch.stats.refill}", batch_line)
        refill_line = next(call.args[0] for call in log.info.call_args_list if "refill done" in call.args[0])
        self.assertIn(f"url_count={batch.stats.refill} ", refill_line)
        self.assertEqual(len(self.requests), 120)  # nothing was cancelled and sent again

        started = time.monotonic()
        again = await http_client.fetch_many_resilient(urls, namespace=NAMESPACE, budget_seconds=0.2)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(again[1], {})
        self.assertEqual(again.stats.network, 0)
        self.assertEqual(again.stats.memory + again.stats.disk, 120)

    async def test_a_cancelled_caller_does_not_cancel_the_downloads(self):
        async def slow(request):
            await asyncio.sleep(0.05)
            return _png(b"img")

        self.install(slow, concurrency=2)
        urls = [AKE.format(f"cancel-{index}") for index in range(6)]
        query = asyncio.create_task(http_client.fetch_many_resilient(urls, namespace=NAMESPACE, budget_seconds=0))
        await self.until(lambda: len(self.requests) >= 2)
        query.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await query
        await self.broker.drain()
        self.assertTrue(self.disk.flush(5))
        for url in urls:
            self.assertIsNotNone(self.disk.metadata(await self.disk_key(url)), url)
        self.assertEqual(len(self.requests), 6)

    async def test_hard_deadline_cancels_a_stuck_refill(self):
        async def stuck(request):
            await asyncio.sleep(30)

        self.install(stuck, hard_deadline=0.3)
        url = AKE.format("stuck")
        with mock.patch.object(assets, "logger") as log:
            _results, failures = await http_client.fetch_many_resilient(
                [url], namespace=NAMESPACE, budget_seconds=0.1
            )
            self.assertEqual(failures, {url: "deadline"})
            self.assertEqual(self.broker.snapshot()["refill"], 1)
            await self.broker.drain()
        snapshot = self.broker.snapshot()
        self.assertEqual((snapshot["jobs"], snapshot["refill"], snapshot["inflight"]), (0, 0, 0))
        message = log.warning.call_args[0][0]
        self.assertIn("refill abandoned", message)
        self.assertIn("reason=hard_deadline", message)

    async def test_close_waits_briefly_then_cancels_without_orphan_errors(self):
        async def stuck(request):
            await asyncio.sleep(30)

        self.install(stuck)
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
        await http_client.fetch_many_resilient([AKE.format("closing")], namespace=NAMESPACE, budget_seconds=0.05)
        await self.broker.prefetch([AKE.format("closing-prefetch")], namespace=NAMESPACE, source="test")
        self.assertEqual(self.broker.snapshot()["jobs"], 2)
        started = time.monotonic()
        await http_client.close_http_client()
        self.assertLess(time.monotonic() - started, 1)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual(errors, [])
        self.assertEqual(self.broker.snapshot()["jobs"], 0)


class QueueTests(_BrokerCase):
    async def test_interactive_requests_jump_queued_prefetches(self):
        release = asyncio.Event()

        async def gated(request):
            if len(self.requests) == 1:
                await release.wait()
            return _png(b"img")

        self.install(gated, concurrency=1)
        background = [AKE.format(f"bg-{index}") for index in range(5)]
        self.assertEqual(await self.broker.prefetch(background, namespace=NAMESPACE, source="test"), 5)
        await self.until(lambda: self.broker.snapshot()["waiting"]["background"] == 4)
        query = asyncio.create_task(
            http_client.fetch_many_resilient(["https://img.test/front.png"], namespace="t-assets", budget_seconds=0)
        )
        await self.until(lambda: self.broker.snapshot()["waiting"]["interactive"] == 1)
        release.set()
        _results, failures = await query
        await self.broker.drain()
        self.assertEqual(failures, {})
        self.assertEqual(self.requests[1], "front.png")
        self.assertEqual(len(self.requests), 6)

    async def test_a_query_for_a_queued_prefetch_bumps_it_and_downloads_once(self):
        release = asyncio.Event()

        async def gated(request):
            if len(self.requests) == 1:
                await release.wait()
            return _png(b"img")

        self.install(gated, concurrency=1)
        background = [AKE.format(f"bump-{index}") for index in range(5)]
        await self.broker.prefetch(background, namespace=NAMESPACE, source="test")
        await self.until(lambda: self.broker.snapshot()["waiting"]["background"] == 4)
        target = next(url for url in background if _name(url) not in self.requests)
        query = asyncio.create_task(http_client.fetch_many_resilient([target], namespace=NAMESPACE, budget_seconds=0))
        await self.until(lambda: self.broker.snapshot()["waiting"]["interactive"] == 1)
        release.set()
        results, failures = await query
        await self.broker.drain()
        self.assertEqual(failures, {})
        self.assertEqual(results[target].content, b"img")
        self.assertEqual(self.requests[1], _name(target))
        self.assertEqual(self.requests.count(_name(target)), 1)
        self.assertEqual(len(self.requests), 5)

    async def test_per_host_limit_leaves_room_for_other_hosts(self):
        active: Counter[str] = Counter()
        peak: Counter[str] = Counter()

        async def timed(request):
            host = request.url.host
            active[host] += 1
            peak[host] = max(peak[host], active[host])
            await asyncio.sleep(0.05 if host == "slow.test" else 0.01)
            active[host] -= 1
            return _png(b"img")

        self.install(timed, concurrency=8, host_concurrency=(("slow.test", 2),))
        urls = [f"https://slow.test/{index}.png" for index in range(20)]
        urls += [f"https://fast.test/{index}.png" for index in range(20)]
        _results, failures = await http_client.fetch_many_resilient(urls, namespace="t-assets", budget_seconds=0)
        self.assertEqual(failures, {})
        self.assertEqual(peak["slow.test"], 2)
        self.assertGreaterEqual(peak["fast.test"], 4)

    async def test_concurrent_batches_share_one_download(self):
        async def slow(request):
            await asyncio.sleep(0.05)
            return _png(b"img")

        self.install(slow)
        url = "https://img.test/shared.png"
        batches = await asyncio.gather(
            *(http_client.fetch_many_resilient([url], namespace="t-assets", budget_seconds=0) for _ in range(10))
        )
        self.assertEqual(self.requests, ["shared.png"])
        self.assertTrue(all(results[url].content == b"img" for results, _failures in batches))


class PrefetchTests(_BrokerCase):
    async def test_prefetch_fills_disk_but_not_memory(self):
        self.install(lambda request: _png(b"img"))
        url = AKE.format("prefetched")
        self.assertEqual(await self.broker.prefetch([url], namespace=NAMESPACE, source="test"), 1)
        await self.broker.drain()
        key, *_rest = await http_client._cache_coordinates(
            url, namespace=NAMESPACE, response_kind="bytes", params=None, headers=None, ttl_seconds=600
        )
        self.assertFalse(http_client._asset_cache.contains(key))
        self.assertIsNotNone(self.disk.metadata(await self.disk_key(url)))
        batch = await http_client.fetch_many_resilient([url], namespace=NAMESPACE)
        self.assertEqual((batch.stats.disk, batch.stats.network), (1, 0))
        self.assertEqual(self.requests, ["prefetched.png"])

    async def test_fresh_and_in_flight_urls_are_not_queued_again(self):
        self.install(lambda request: _png(b"img"))
        url = AKE.format("fresh")
        await http_client.fetch_many_resilient([url], namespace=NAMESPACE)
        with mock.patch.object(assets, "logger") as log:
            self.assertEqual(await self.broker.prefetch([url], namespace=NAMESPACE, source="test"), 0)
        self.assertIn("skipped_fresh=1", log.info.call_args[0][0])
        self.assertEqual(self.requests, ["fresh.png"])

    async def test_daily_budget_stops_prefetch_and_resets_the_next_day(self):
        queued = asyncio.Event()

        async def big(request):
            await queued.wait()
            return _png(b"x" * (400 * 1024))

        self.install(big, concurrency=1, prefetch_daily_mb=1)
        today = ["2026-10-17"]
        budget_path = self.root / "budget.json"
        self.broker._budget = assets.PrefetchBudget(
            budget_path, clock=lambda: datetime.fromisoformat(f"{today[0]}T12:00:00+08:00")
        )
        urls = [AKE.format(f"big-{index}") for index in range(10)]
        with mock.patch.object(assets, "logger") as log:
            self.assertEqual(await self.broker.prefetch(urls, namespace=NAMESPACE, source="test"), 10)
            queued.set()
            await self.broker.drain()
            self.assertEqual(await self.broker.prefetch([AKE.format("later")], namespace=NAMESPACE, source="test"), 0)
        # Charged as each body arrives: the third one crosses 1 MiB, the rest never start.
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.broker.budget.exhausted())
        exhausted = [call for call in log.warning.call_args_list if "prefetch budget exhausted" in call.args[0]]
        self.assertEqual(len(exhausted), 1)
        self.assertIn("skipped_budget=1", log.info.call_args[0][0])
        await self.broker.close()
        self.assertEqual(
            json.loads(budget_path.read_text(encoding="utf-8")),
            {"day": "2026-10-17", "bytes": 3 * 400 * 1024},
        )
        budget = assets.PrefetchBudget(
            budget_path, clock=lambda: datetime.fromisoformat(f"{today[0]}T12:00:00+08:00")
        )
        self.assertTrue(budget.exhausted())  # survives a restart
        today[0] = "2026-10-18"
        self.assertEqual((budget.used, budget.exhausted()), (0, False))

    async def test_hosts_behind_the_asset_proxy_are_not_prefetched(self):
        self.install(lambda request: _png(b"img"), proxy="http://127.0.0.1:1", proxy_hosts=("bbs.hycdn.cn",))
        hycdn, ake = "https://bbs.hycdn.cn/image/0f3a9c.png", AKE.format("direct")
        with mock.patch.object(assets, "logger") as log:
            self.assertEqual(await self.broker.prefetch([hycdn, ake], namespace=NAMESPACE, source="test"), 1)
            await self.broker.drain()
        self.assertIn("skipped_proxy=1", log.info.call_args[0][0])
        self.assertEqual(self.requests, ["direct.png"])
        _results, failures = await http_client.fetch_many_resilient([hycdn], namespace=NAMESPACE)
        self.assertEqual(failures, {})  # queries still fetch it
        self.assertEqual(self.requests, ["direct.png", "0f3a9c.png"])

    async def test_queue_limit_drops_prefetch_but_not_queries(self):
        release = asyncio.Event()

        async def gated(request):
            await release.wait()
            return _png(b"img")

        self.install(gated, queue_limit=5)
        prefetch = [AKE.format(f"queued-{index}") for index in range(20)]
        self.assertEqual(await self.broker.prefetch(prefetch, namespace=NAMESPACE, source="test"), 5)
        query = asyncio.create_task(
            http_client.fetch_many_resilient(
                [f"https://img.test/{index}.png" for index in range(10)], namespace="t-assets", budget_seconds=0
            )
        )
        await self.until(lambda: self.broker.snapshot()["jobs"] == 15)
        release.set()
        _results, failures = await query
        self.assertEqual(failures, {})

    async def test_disabled_prefetch_queues_nothing(self):
        self.install(lambda request: _png(b"img"), prefetch=False)
        self.assertEqual(await self.broker.prefetch([AKE.format("off")], namespace=NAMESPACE, source="test"), 0)
        self.assertEqual(self.requests, [])


class HotSetTests(_BrokerCase):
    async def test_queries_record_the_hot_set_and_a_restart_prefetches_only_stale_rows(self):
        sizes = {"small": 10, "medium": 2000, "huge": 2 << 20}
        self.install(lambda request: _png(b"x" * sizes[_name(str(request.url)).split(".")[0]]))
        urls = {name: AKE.format(name) for name in sizes}
        await http_client.fetch_many_resilient([urls["medium"], urls["huge"]], namespace=NAMESPACE, max_bytes=8 << 20)
        await http_client.fetch_many_resilient(
            [urls["small"]], namespace=NAMESPACE, disk_aliases=("endfield-gacha-images",)
        )
        groups = self.broker.hotset.startup_groups()
        self.assertEqual(
            groups,
            {
                (NAMESPACE, ()): [urls["medium"]],  # the 2 MiB image is not prefetched at startup
                (NAMESPACE, ("endfield-gacha-images",)): [urls["small"]],
            },
        )
        # Saved with the image cache; a restart reads it back and finds every row still fresh.
        await http_client.close_http_client()
        self.assertTrue((self.root / assets.HOTSET_FILE).exists())
        self.assertEqual(len(self.broker.hotset), 3)
        self.assertEqual(await self.broker.prefetch_hotset(), 0)
        self.broker.hotset.clear()
        self.assertEqual(self.broker.hotset.startup_groups(), {})

    def test_hot_set_keeps_the_most_recent_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            hotset = assets.HotSet(Path(directory) / "hot.json", max_entries=2)
            hotset.note(NAMESPACE, {"a": 1, "b": 2})
            hotset.note(NAMESPACE, {"c": 3, "a": 1})
            self.assertEqual(hotset.startup_groups(), {(NAMESPACE, ()): ["a", "c"]})
            self.assertEqual(hotset.startup_groups(now=time.time() + 2 * 86400), {})


class AssetQueueSettingsTests(unittest.TestCase):
    def test_defaults_and_env_parsing(self):
        defaults = load_asset_settings({})
        self.assertEqual(
            defaults.host_concurrency, (("hycdn.cn", 4), ("fz.wiki", 2), ("warfarin.wiki", 2))
        )
        self.assertEqual(
            (defaults.hard_deadline, defaults.queue_limit, defaults.prefetch_daily_mb, defaults.prefetch),
            (180.0, 2000, 300, True),
        )
        settings = load_asset_settings(
            {
                "host_concurrency": "slow.test=3, *.cdn.test=1, bad, zero.test=0",
                "hard_deadline": "0.1",
                "queue_limit": "50",
                "prefetch_daily_mb": "64",
                "prefetch": "off",
            }
        )
        self.assertEqual(settings.host_concurrency, (("slow.test", 3), ("cdn.test", 1)))
        self.assertEqual(
            (settings.hard_deadline, settings.queue_limit, settings.prefetch_daily_mb, settings.prefetch),
            (1.0, 50, 64, False),
        )
        self.assertEqual(load_asset_settings({"host_concurrency": "off"}).host_concurrency, ())

    def test_host_limit_uses_the_longest_suffix_within_the_total(self):
        settings = AssetFetchSettings(
            concurrency=6, host_concurrency=(("hycdn.cn", 4), ("bbs.hycdn.cn", 1), ("big.test", 20))
        )
        self.assertEqual(host_concurrency("bbs.hycdn.cn", settings), 1)
        self.assertEqual(host_concurrency("web.hycdn.cn", settings), 4)
        self.assertEqual(host_concurrency("big.test", settings), 6)
        self.assertEqual(host_concurrency("data.akedata.wiki", settings), 6)


if __name__ == "__main__":
    unittest.main()
