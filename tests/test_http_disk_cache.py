"""公共图片 / 表格磁盘缓存：size 列与按需淘汰、旧库迁移、304 只刷新、后台写入。

只用临时目录里的 SQLite，不访问网络。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from otae_bot.infrastructure.http import asset_policy
from otae_bot.infrastructure.http import client as http_client
from otae_bot.infrastructure.http import disk as disk_module
from otae_bot.infrastructure.http.asset_policy import (
    AssetFetchSettings,
    configure_asset_settings,
    host_max_age,
    load_asset_settings,
)
from otae_bot.infrastructure.http.disk import DiskImage, DiskImageMeta, PublicImageDiskCache

NAMESPACE = "endfield-assets"
AKE_URL = "https://data.akedata.wiki/public/images/assets/test/{}.png"
HYCDN_URL = "https://bbs.hycdn.cn/image/2026/10/03/{}.png"

# be55f44 的建表语句与 10 列写入，原样保留用来造旧库。
OLD_SCHEMA = """CREATE TABLE IF NOT EXISTS public_images_v1 (
    key TEXT PRIMARY KEY, namespace TEXT NOT NULL, content BLOB NOT NULL,
    digest TEXT NOT NULL, content_type TEXT NOT NULL, etag TEXT NOT NULL,
    modified TEXT NOT NULL, validated_at REAL NOT NULL,
    max_age REAL NOT NULL, accessed_at REAL NOT NULL)"""


def _image(content: bytes, *, validated_at: float = 100.0, max_age: float = 600.0, etag: str = '"v1"'):
    return DiskImage(content, "image/png", etag, "", validated_at, max_age)


class _Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "images.sqlite3"
        self.caches: list[PublicImageDiskCache] = []

    def tearDown(self):
        for cache in self.caches:
            cache.close()
        self.directory.cleanup()

    def cache(self, budget: int = 1 << 20, **kwargs) -> PublicImageDiskCache:
        cache = PublicImageDiskCache(self.path, budget, **kwargs)
        self.caches.append(cache)
        return cache

    def traced(self, cache: PublicImageDiskCache) -> list[str]:
        statements: list[str] = []
        cache._connect().set_trace_callback(statements.append)
        return statements

    def rows(self) -> list[tuple]:
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT key, content, digest, size, validated_at, etag "
                "FROM public_images_v1 ORDER BY key"
            ).fetchall()


class QuotaTests(_Case):
    def test_put_under_budget_never_scans_for_eviction(self):
        cache = self.cache(budget=10_000)
        generation = cache.register(NAMESPACE)
        cache.put("warm", NAMESPACE, _image(b"w" * 100), generation)
        statements = self.traced(cache)
        for index in range(20):
            cache.put(f"k{index}", NAMESPACE, _image(bytes([index]) * 100), generation)
        for index in range(20):
            cache.put_later(f"q{index}", NAMESPACE, _image(bytes([index]) * 100), generation)
        self.assertTrue(cache.flush(5))
        joined = "\n".join(statements)
        self.assertIn("INSERT INTO public_images_v1", joined)
        self.assertNotIn("ORDER BY", joined)
        self.assertNotIn("length(content)", joined)
        # 超限判断走 (accessed_at, size) 覆盖索引，不读正文
        plan = cache._connect().execute(
            "EXPLAIN QUERY PLAN SELECT count(*), total(size) FROM public_images_v1"
        ).fetchall()
        self.assertIn("COVERING INDEX public_images_v1_lru", str(plan))
        self.assertEqual(len(self.rows()), 41)

    def test_over_budget_evicts_least_recently_used_first(self):
        cache = self.cache(budget=300)
        generation = cache.register(NAMESPACE)
        for stamp, key in enumerate(("a", "b", "c"), 1):
            with mock.patch.object(disk_module.time, "time", return_value=float(stamp)):
                cache.put(key, NAMESPACE, _image(key.encode() * 100), generation)
        with mock.patch.object(disk_module.time, "time", return_value=10.0):
            self.assertIsNotNone(cache.get("a", 1 << 20))  # a 变成最近使用
        self.assertTrue(cache.flush(5))
        statements = self.traced(cache)
        cache.put("d", NAMESPACE, _image(b"d" * 100), generation)
        self.assertEqual([row[0] for row in self.rows()], ["a", "c", "d"])
        self.assertTrue(any("ORDER BY accessed_at" in sql for sql in statements))
        self.assertFalse(any("length(content)" in sql for sql in statements))
        # 比预算大的值不写；替换已有键先扣掉旧行的大小
        cache.put("huge", NAMESPACE, _image(b"x" * 301), generation)
        cache.put("c", NAMESPACE, _image(b"C" * 100), generation)
        rows = self.rows()
        self.assertEqual([row[0] for row in rows], ["a", "c", "d"])
        self.assertLessEqual(sum(row[3] for row in rows), 300)
        self.assertEqual(dict((row[0], row[1]) for row in rows)["c"], b"C" * 100)

    def test_max_entries_is_enforced(self):
        cache = self.cache(budget=1 << 20, max_entries=3)
        generation = cache.register(NAMESPACE)
        for index in range(5):
            cache.put_later(f"k{index}", NAMESPACE, _image(bytes([index]) * 10), generation)
        self.assertTrue(cache.flush(5))
        self.assertEqual([row[0] for row in self.rows()], ["k2", "k3", "k4"])


class MigrationTests(_Case):
    def _old_database(self, count: int) -> dict[str, bytes]:
        rng = random.Random(1)
        bodies = {}
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA auto_vacuum=FULL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(OLD_SCHEMA)
            for index in range(count):
                content = rng.randbytes(rng.randint(500, 3000))
                key = f"old-{index:03d}"
                bodies[key] = content
                connection.execute(
                    "INSERT INTO public_images_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, NAMESPACE, content, hashlib.sha256(content).hexdigest(),
                     "image/png", f'"{index}"', "", 100.0, 600.0, float(index)),
                )
        return bodies

    def test_old_schema_gets_size_column_and_index_without_losing_rows(self):
        bodies = self._old_database(25)
        with mock.patch.object(disk_module, "BACKFILL_ROWS", 4):
            cache = self.cache()
            # 读不等回填：第一次连接只加列，正文照常读得到
            self.assertEqual(cache.get("old-003", 1 << 20).content, bodies["old-003"])
            self.assertTrue(cache.flush(10))
        with sqlite3.connect(self.path) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(public_images_v1)")]
            indexes = [row[1] for row in connection.execute("PRAGMA index_list(public_images_v1)")]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            rows = connection.execute(
                "SELECT key, content, digest, size, length(content) FROM public_images_v1"
            ).fetchall()
        self.assertIn("size", columns)
        self.assertIn("public_images_v1_lru", indexes)
        self.assertEqual(version, disk_module.SCHEMA_VERSION)
        self.assertEqual({row[0]: row[1] for row in rows}, bodies)
        for _key, content, digest, size, length in rows:
            self.assertEqual(size, length)
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        # 迁移完成后写入走便宜的超限判断
        generation = cache.register(NAMESPACE)
        statements = self.traced(cache)
        cache.put("new", NAMESPACE, _image(b"n" * 100), generation)
        self.assertFalse(any("ORDER BY" in sql for sql in statements))
        # 重新打开不再迁移
        cache.close()
        again = self.cache()
        self.assertIsNotNone(again.get("new", 1 << 20))
        self.assertIsNone(again._backfill_from)
        self.assertTrue(again._schema_ready)

    def test_close_abandons_the_backfill_and_the_next_open_resumes_it(self):
        bodies = self._old_database(20)
        original = PublicImageDiskCache._backfill_batch

        def slow_batch(cache, connection):
            time.sleep(0.05)
            return original(cache, connection)

        with mock.patch.object(disk_module, "BACKFILL_ROWS", 2), mock.patch.object(
            PublicImageDiskCache, "_backfill_batch", slow_batch
        ):
            cache = self.cache()
            generation = cache.register(NAMESPACE)
            cache.metadata("old-000")  # first connection schedules the backfill
            cache.put_later("queued", NAMESPACE, _image(b"queued"), generation)
            started = time.monotonic()
            cache.close()
        self.assertLess(time.monotonic() - started, 1.0)  # 不等 10 批回填跑完
        with sqlite3.connect(self.path) as connection:
            self.assertLess(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            unfilled = connection.execute(
                "SELECT count(*) FROM public_images_v1 WHERE size IS NULL"
            ).fetchone()[0]
        self.assertGreater(unfilled, 0)
        self.assertIn("queued", [row[0] for row in self.rows()])  # 排队的行照常落盘
        reopened = self.cache()
        self.assertEqual(reopened.get("old-007", 1 << 20).content, bodies["old-007"])
        self.assertTrue(reopened.flush(10))
        self.assertTrue(reopened._schema_ready)
        self.assertTrue(all(row[3] == len(row[1]) for row in self.rows()))

    def test_writes_before_the_backfill_finishes_still_respect_the_budget(self):
        bodies = self._old_database(10)
        total = sum(len(body) for body in bodies.values())
        cache = PublicImageDiskCache(self.path, total + 50)
        self.caches.append(cache)
        with cache._lock:
            cache._connect()
            # 回填尚未开始时同步写入：按 length(content) 慢路径算总量，照样淘汰
            self.assertFalse(cache._schema_ready)
            generation = cache.register(NAMESPACE)
            cache.put("new", NAMESPACE, _image(b"n" * 100), generation)
        self.assertTrue(cache.flush(10))
        rows = self.rows()
        self.assertLessEqual(sum(len(row[1]) for row in rows), total + 50)
        self.assertIn("new", [row[0] for row in rows])
        self.assertTrue(all(row[3] == len(row[1]) for row in rows))

    def test_failed_backfill_is_logged_and_keeps_the_cache_usable(self):
        self._old_database(3)
        with mock.patch.object(disk_module, "logger") as log, mock.patch.object(
            PublicImageDiskCache, "_backfill_batch", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            cache = self.cache()
            self.assertIsNotNone(cache.get("old-001", 1 << 20))
            self.assertTrue(cache.flush(5))
        self.assertIn("size backfill failed", log.warning.call_args[0][0])
        generation = cache.register(NAMESPACE)
        cache.put("new", NAMESPACE, _image(b"n" * 10), generation)
        self.assertEqual(cache.get("new", 1 << 20).content, b"n" * 10)


class BackgroundWriteTests(_Case):
    def test_queued_rows_are_readable_before_they_reach_disk(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        cache.put("seed", NAMESPACE, _image(b"seed"), generation)
        with cache._lock:  # 写线程拿不到连接，行只在队列里
            self.assertTrue(cache.put_later("row", NAMESPACE, _image(b"body", validated_at=5), generation))
            self.assertEqual(cache.get("row", 1 << 20).content, b"body")
            self.assertEqual(cache.metadata("row").validated_at, 5)
            self.assertIsNone(cache.get("row", 2))  # max_bytes 仍然生效
            self.assertNotIn("row", [row[0] for row in self.rows()])
        self.assertTrue(cache.flush(5))
        self.assertIn("row", [row[0] for row in self.rows()])

    def test_refresh_updates_validators_without_rewriting_the_body(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        cache.put("row", NAMESPACE, _image(b"original-body", validated_at=100, max_age=600), generation)
        before = self.rows()[0]
        statements = self.traced(cache)
        meta = DiskImageMeta(5000.0, 604800.0, '"v2"', "Sat, 03 Oct 2026 00:00:00 GMT")
        self.assertTrue(cache.refresh_later("row", NAMESPACE, meta, generation))
        self.assertEqual(cache.metadata("row"), meta)  # 落盘前就能读到新的校验时间
        self.assertTrue(cache.flush(5))
        writes = [sql for sql in statements if sql.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE"))]
        self.assertEqual(len(writes), 1)
        self.assertIn("UPDATE public_images_v1 SET validated_at=", writes[0])
        self.assertNotIn("content", writes[0])
        after = self.rows()[0]
        self.assertEqual(after[1:4], before[1:4])  # content、digest、size 不变
        self.assertEqual((after[4], after[5]), (5000.0, '"v2"'))
        image = cache.get("row", 1 << 20)
        self.assertEqual((image.content, image.max_age, image.modified), (b"original-body", 604800.0, meta.modified))

    def test_refresh_of_a_queued_row_updates_the_queued_copy(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        with cache._lock:
            cache.put_later("row", NAMESPACE, _image(b"body", validated_at=1), generation)
            cache.refresh_later("row", NAMESPACE, DiskImageMeta(9.0, 60.0, '"v9"', ""), generation)
        self.assertTrue(cache.flush(5))
        image = cache.get("row", 1 << 20)
        self.assertEqual((image.content, image.validated_at, image.etag), (b"body", 9.0, '"v9"'))

    def test_clear_drops_queued_rows_and_rejects_older_generations(self):
        cache = self.cache()
        old = cache.register(NAMESPACE)
        cache.register("endfield-other")
        with cache._lock:
            cache.put_later("a", NAMESPACE, _image(b"a"), old)
            cache.put_later("b", "endfield-other", _image(b"b"), cache.generation("endfield-other"))
            self.assertEqual(cache.clear("endfield-assets"), 1)
        self.assertFalse(cache.put_later("late", NAMESPACE, _image(b"late"), old))
        self.assertFalse(cache.refresh_later("b", NAMESPACE, DiskImageMeta(1, 1, "", ""), old))
        self.assertTrue(cache.flush(5))
        self.assertEqual([row[0] for row in self.rows()], ["b"])
        self.assertIsNone(cache.get("a", 1 << 20))

    def test_write_failure_is_logged_and_never_raises(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        with mock.patch.object(disk_module, "logger") as log, mock.patch.object(
            PublicImageDiskCache, "_store", side_effect=sqlite3.OperationalError("database or disk is full")
        ):
            self.assertTrue(cache.put_later("row", NAMESPACE, _image(b"body"), generation))
            self.assertTrue(cache.flush(5))
        self.assertIn("write failed", log.warning.call_args[0][0])
        self.assertIn("database or disk is full", log.warning.call_args[0][0])
        self.assertIsNone(cache.get("row", 1 << 20))
        # 之后的写入照常
        cache.put_later("next", NAMESPACE, _image(b"next"), generation)
        self.assertTrue(cache.flush(5))
        self.assertEqual(cache.get("next", 1 << 20).content, b"next")

    def test_queue_limit_drops_new_rows_instead_of_growing(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        with mock.patch.object(disk_module, "QUEUE_LIMIT_BYTES", 10), cache._lock:
            self.assertTrue(cache.put_later("a", NAMESPACE, _image(b"12345678"), generation))
            self.assertFalse(cache.put_later("b", NAMESPACE, _image(b"12345678"), generation))
        self.assertTrue(cache.flush(5))
        self.assertEqual([row[0] for row in self.rows()], ["a"])

    def test_close_flushes_queued_rows_for_the_next_process(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        cache.put_later("row", NAMESPACE, _image(b"persisted"), generation)
        cache.close()
        self.assertIsNone(cache._writer)
        reopened = self.cache()
        self.assertEqual(reopened.get("row", 1 << 20).content, b"persisted")

    def test_flush_writes_inline_when_no_thread_can_start(self):
        cache = self.cache()
        generation = cache.register(NAMESPACE)
        with mock.patch.object(disk_module.Thread, "start", side_effect=RuntimeError("shutdown")):
            cache.put_later("row", NAMESPACE, _image(b"inline"), generation)
            self.assertIsNone(cache._writer)
            self.assertTrue(cache.flush(5))
        self.assertEqual([row[0] for row in self.rows()], ["row"])

    def test_concurrent_readers_writers_and_clears_keep_the_database_consistent(self):
        budget = 64 * 1024
        cache = self.cache(budget=budget, max_entries=200)
        errors: list[BaseException] = []
        start = threading.Barrier(9)

        def writer(seed: int):
            rng = random.Random(seed)
            try:
                start.wait()
                for _ in range(150):
                    key = f"k{rng.randrange(120)}"
                    generation = cache.register(NAMESPACE)
                    if rng.random() < 0.2:
                        meta = DiskImageMeta(time.time(), 600.0, f'"{rng.random()}"', "")
                        cache.refresh_later(key, NAMESPACE, meta, generation)
                    else:
                        cache.put_later(key, NAMESPACE, _image(rng.randbytes(rng.randint(100, 2000))), generation)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def reader(seed: int):
            rng = random.Random(seed)
            try:
                start.wait()
                for _ in range(300):
                    key = f"k{rng.randrange(120)}"
                    image = cache.get(key, 1 << 20)
                    if image is not None:
                        self.assertIsInstance(image.content, bytes)
                    cache.metadata(key)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def clearer():
            try:
                start.wait()
                for _ in range(5):
                    time.sleep(0.01)
                    cache.clear("endfield-")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(seed,)) for seed in range(4)]
        threads += [threading.Thread(target=reader, args=(seed,)) for seed in range(10, 14)]
        threads.append(threading.Thread(target=clearer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(cache.flush(10))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            rows = connection.execute(
                "SELECT content, digest, size FROM public_images_v1"
            ).fetchall()
        self.assertLessEqual(sum(row[2] for row in rows), budget)
        self.assertLessEqual(len(rows), 200)
        for content, digest, size in rows:
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
            self.assertEqual(size, len(content))
        self.assertEqual(cache._queued_bytes, 0)


def _png(body: bytes, **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "image/png", **headers})


class ClientRevalidationTests(unittest.IsolatedAsyncioTestCase):
    """取图链路：304 只刷新、校验失败 / 截止时间用旧图、hycdn 长缓存。全部 MockTransport。"""

    async def asyncSetUp(self):
        await http_client.close_http_client()
        self.directory = tempfile.TemporaryDirectory()
        self.disk = PublicImageDiskCache(Path(self.directory.name) / "images.sqlite3", 8 << 20)
        self.patches = [
            mock.patch.object(http_client, "public_images", self.disk),
            mock.patch.object(http_client, "DEADLINE_GRACE_SECONDS", 0.05),
        ]
        for patcher in self.patches:
            patcher.start()

    async def asyncTearDown(self):
        await http_client.close_http_client()
        for patcher in self.patches:
            patcher.stop()
        self.disk.close()
        self.directory.cleanup()

    def install(self, handler, settings: AssetFetchSettings | None = None):
        configure_asset_settings(settings or AssetFetchSettings())
        for name in ("_asset_client", "_client"):
            setattr(
                http_client,
                name,
                httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
            )

    async def fetch(self, url: str):
        return await http_client.fetch_bytes(url, namespace=NAMESPACE, asset=True)

    async def forget_memory(self):
        self.assertTrue(self.disk.flush(5))
        await http_client.clear_http_cache(include_disk=False)

    def row(self, url_key: str):
        with sqlite3.connect(self.disk.path) as connection:
            return connection.execute(
                "SELECT content, digest, size, validated_at, max_age, etag FROM public_images_v1 "
                "WHERE key=?",
                (url_key,),
            ).fetchone()

    async def disk_key(self, url: str) -> str:
        _key, eligible, disk_key, _ttl = await http_client._cache_coordinates(
            url, namespace=NAMESPACE, response_kind="bytes", params=None, headers=None, ttl_seconds=600
        )
        self.assertTrue(eligible)
        return disk_key

    async def test_304_refreshes_validators_and_keeps_the_stored_body(self):
        seen = []

        def handler(request):
            seen.append(request.headers.get("if-none-match"))
            if len(seen) == 1:
                return _png(b"png-body", etag='"v1"', **{"cache-control": "max-age=0"})
            return httpx.Response(304, headers={"etag": '"v1"', "cache-control": "max-age=120"})

        self.install(handler)
        url = AKE_URL.format("revalidate")
        self.assertEqual((await self.fetch(url)).content, b"png-body")
        await self.forget_memory()
        key = await self.disk_key(url)
        before = self.row(key)
        statements: list[str] = []
        self.disk._connect().set_trace_callback(statements.append)
        again = await self.fetch(url)
        self.assertTrue(self.disk.flush(5))
        self.assertEqual((again.content, again.status_code, again.stale), (b"png-body", 200, False))
        self.assertEqual(seen, [None, '"v1"'])
        writes = [sql for sql in statements if sql.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE"))]
        self.assertTrue(any("SET validated_at=" in sql for sql in writes))
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "DELETE")) for sql in writes))
        self.assertFalse(any("content" in sql for sql in writes))
        after = self.row(key)
        self.assertEqual(after[:3], before[:3])  # content、digest、size 原样
        self.assertGreater(after[3], before[3] - 1)
        self.assertEqual((before[4], after[4]), (0.0, 120.0))

    async def test_failed_revalidation_serves_the_stale_copy(self):
        mode = "ok"

        def handler(request):
            if mode == "ok":
                return _png(b"old-but-fine", etag='"v1"', **{"cache-control": "max-age=0"})
            if mode == "refused":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(503, content=b"busy")

        self.install(handler)
        url = AKE_URL.format("stale")
        await self.fetch(url)
        for mode in ("503", "refused", "refused", "refused"):
            await self.forget_memory()
            resource = await self.fetch(url)
            self.assertEqual((resource.content, resource.stale), (b"old-but-fine", True), mode)
        # 熔断打开后也一样用旧图；没有旧图的照常失败
        self.assertTrue(asset_policy.asset_breaker().blocked("data.akedata.wiki"))
        await self.forget_memory()
        self.assertTrue((await self.fetch(url)).stale)
        with self.assertRaises(httpx.TransportError):
            await self.fetch(AKE_URL.format("never-cached"))
        await self.forget_memory()
        results, failures = await http_client.fetch_many_resilient(
            [url], namespace=NAMESPACE, base_delay_seconds=0
        )
        self.assertEqual(failures, {})
        self.assertEqual(results[url].content, b"old-but-fine")

    async def test_deadline_uses_stale_copies_instead_of_blanks(self):
        slow = False

        async def handler(request):
            if slow:
                await asyncio.sleep(30)
            return _png(request.url.path.encode(), etag='"v1"', **{"cache-control": "max-age=0"})

        self.install(handler, AssetFetchSettings(concurrency=2))
        urls = [AKE_URL.format(f"row-{index}") for index in range(12)]
        results, failures = await http_client.fetch_many_resilient(urls, namespace=NAMESPACE)
        self.assertEqual(failures, {})
        await self.forget_memory()
        slow = True
        missing = AKE_URL.format("not-on-disk")
        started = time.monotonic()
        with mock.patch.object(http_client, "logger") as log:
            results, failures = await http_client.fetch_many_resilient(
                [*urls, missing], namespace=NAMESPACE, budget_seconds=0.3
            )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(failures, {missing: "deadline"})
        for url in urls:
            self.assertTrue(results[url].stale)
            self.assertEqual(results[url].content, httpx.URL(url).path.encode())
        message = log.warning.call_args[0][0]
        self.assertIn("stale=12", message)
        self.assertIn("deadline=hit", message)

    async def test_hycdn_images_stay_fresh_for_a_week_and_other_hosts_do_not(self):
        calls: list[str] = []

        def handler(request):
            calls.append(request.url.host)
            return _png(b"img", etag='"v1"', **{"cache-control": "max-age=600"})

        self.install(handler)
        hycdn, ake = HYCDN_URL.format("0f3a9c"), AKE_URL.format("fresh")
        await self.fetch(hycdn)
        await self.fetch(ake)
        await self.forget_memory()
        self.assertEqual(self.row(await self.disk_key(hycdn))[4], 7 * 86400.0)
        self.assertEqual(self.row(await self.disk_key(ake))[4], 600.0)
        later = time.time() + 3 * 86400
        with mock.patch.object(http_client.time, "time", return_value=later):
            await self.fetch(hycdn)
            await self.fetch(ake)
        self.assertEqual(calls, ["bbs.hycdn.cn", "data.akedata.wiki", "data.akedata.wiki"])

    async def test_rows_written_with_the_old_600s_window_follow_the_hycdn_setting(self):
        self.install(lambda request: self.fail("network must not be used"))
        url = HYCDN_URL.format("legacy")
        generation = self.disk.register(NAMESPACE)
        self.disk.put(
            await self.disk_key(url),
            NAMESPACE,
            DiskImage(b"legacy", "image/png", '"v0"', "", time.time() - 3600, 600.0),
            generation,
        )
        self.assertEqual((await self.fetch(url)).content, b"legacy")
        self.assertTrue(
            await http_client.cached_public_resource(url, namespace=NAMESPACE, response_kind="bytes", asset=True)
        )

    async def test_host_max_age_can_be_configured_or_switched_off(self):
        self.assertEqual(load_asset_settings({}).host_max_age, (("hycdn.cn", 604800.0),))
        with mock.patch.object(asset_policy, "logger") as log:
            custom = load_asset_settings(
                {"OTAE_HTTP_ASSET_HOST_MAX_AGE": "example.test=60, *.cdn.test=120,broken,zero.test=0"}
            )
        self.assertEqual(custom.host_max_age, (("example.test", 60.0), ("cdn.test", 120.0)))
        log.warning.assert_called_once()
        self.assertEqual(load_asset_settings({"OTAE_HTTP_ASSET_HOST_MAX_AGE": "off"}).host_max_age, ())
        default = AssetFetchSettings()
        self.assertEqual(host_max_age("bbs.hycdn.cn", default), 604800.0)
        self.assertEqual(host_max_age("web.hycdn.cn", default), 604800.0)
        self.assertIsNone(host_max_age("nothycdn.cn", default))
        self.assertIsNone(host_max_age("hycdn.cn.evil.test", default))
        self.assertEqual(host_max_age("img.cdn.test", custom), 120.0)

        def handler(request):
            return _png(b"img", **{"cache-control": "max-age=600"})

        self.install(handler, AssetFetchSettings(host_max_age=()))
        url = HYCDN_URL.format("short")
        await self.fetch(url)
        self.assertTrue(self.disk.flush(5))
        self.assertEqual(self.row(await self.disk_key(url))[4], 600.0)


if __name__ == "__main__":
    unittest.main()
