"""渲染队列、context 池、关闭重启、分页并行与路由前缀：假 Playwright，不启动 Chromium。"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright._impl._helper import url_matches

from otae_bot.config import settings
from otae_bot.infrastructure.rendering import browser
from otae_bot.infrastructure.rendering.browser import BrowserResource, RenderPriority
from otae_bot.infrastructure.rendering.pages import render_pages
from tests.support.browser import FakeBrowserHarness, FakeContext, FakePage


class Recorder:
    """Page work stand-in: records start order and overlap, sleeps, may fail by URL."""

    def __init__(self, delay: float = 0.0, delays: dict[str, float] | None = None):
        self.delay = delay
        self.delays = dict(delays or {})
        self.errors: dict[str, BaseException] = {}
        self.started: list[str] = []
        self.finished: list[str] = []
        self.active = 0
        self.max_active = 0

    async def screenshot(self, page, context, web_url, *_args):
        self.started.append(web_url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(web_url, self.delay))
            if web_url in self.errors:
                raise self.errors[web_url]
            self.finished.append(web_url)
            return web_url.encode()
        finally:
            self.active -= 1


async def _settle(seconds: float = 0.05) -> None:
    """Give the browser thread time to act on a cross-thread submit or cancel."""
    await asyncio.sleep(seconds)


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, *, concurrency: int, recorder: Recorder) -> FakeBrowserHarness:
        return await FakeBrowserHarness(self, concurrency=concurrency, screenshot=recorder.screenshot).start()

    async def test_interactive_renders_jump_ahead_of_background_ones(self):
        recorder = Recorder(delay=0.02, delays={"file:///blocker.html": 0.2})
        await self.start(concurrency=1, recorder=recorder)
        blocker = asyncio.create_task(browser.screenshot_web_element("file:///blocker.html"))
        await _settle()
        tasks = []
        for name, priority in (
            ("bg-1", RenderPriority.BACKGROUND),
            ("ui-1", RenderPriority.INTERACTIVE),
            ("bg-2", RenderPriority.BACKGROUND),
        ):
            tasks.append(
                asyncio.create_task(browser.screenshot_web_element(f"file:///{name}.html", priority=priority))
            )
            await _settle(0.02)
        snapshot = browser.browser_queue_snapshot()
        self.assertEqual(snapshot["inflight"], 1)
        self.assertEqual(snapshot["waiting"], {"interactive": 1, "background": 2})
        await asyncio.gather(blocker, *tasks)
        self.assertEqual(
            recorder.started,
            ["file:///blocker.html", "file:///ui-1.html", "file:///bg-1.html", "file:///bg-2.html"],
        )

    async def test_two_slots_render_in_parallel_and_reuse_contexts_by_scale_factor(self):
        recorder = Recorder(delay=0.3)
        harness = await self.start(concurrency=2, recorder=recorder)
        started = time.perf_counter()
        results = await asyncio.gather(
            *(browser.screenshot_web_element(f"file:///{index}.html") for index in range(3))
        )
        self.assertLess(time.perf_counter() - started, 0.8)  # two slots: 0.6 s; serial would take 0.9 s
        self.assertEqual(results, [b"file:///0.html", b"file:///1.html", b"file:///2.html"])
        self.assertEqual(recorder.max_active, 2)

        recorder.delay = 0.05
        await asyncio.gather(
            browser.screenshot_web_element("file:///a.html", device_scale_factor=1.0),
            browser.screenshot_web_element("file:///b.html", device_scale_factor=2.0),
        )
        await browser.screenshot_web_element("file:///c.html", device_scale_factor=1.0)
        pool = harness.host.pool
        self.assertEqual(sorted(pool.idle_scale_factors()), [1.0, 2.0])
        # Two 1.0 contexts from the first batch; the 2.0 render replaced the older idle one.
        self.assertEqual([context.device_scale_factor for context in harness.contexts], [1.0, 1.0, 2.0])
        self.assertEqual([context.closed for context in harness.contexts], [True, False, False])
        self.assertEqual(pool.reused, 3)
        self.assertEqual(browser.browser_queue_snapshot()["contexts"], 2)

    async def test_pool_stays_within_the_slot_count(self):
        recorder = Recorder()
        harness = await self.start(concurrency=2, recorder=recorder)
        for scale in (1.0, 2.0, 1.5):
            await browser.screenshot_web_element("file:///card.html", device_scale_factor=scale)
        pool = harness.host.pool
        self.assertEqual(sorted(pool.idle_scale_factors()), [1.5, 2.0])  # the oldest idle one made room
        self.assertEqual([context.closed for context in harness.contexts], [True, False, False])
        self.assertEqual(pool.held(), 2)

    async def test_failed_or_worn_out_contexts_are_replaced(self):
        recorder = Recorder()
        harness = await self.start(concurrency=1, recorder=recorder)
        recorder.errors["file:///timeout.html"] = TimeoutError("Timeout 25000ms exceeded")
        with self.assertRaises(TimeoutError):
            await browser.screenshot_web_element("file:///timeout.html")
        self.assertTrue(harness.contexts[0].closed)
        self.assertEqual(harness.host.pool.idle_scale_factors(), [])

        # A height rejection is an answer from a healthy page: keep the context.
        recorder.errors["file:///tall.html"] = RuntimeError("Screenshot element height 9000px exceeds limit 100px")
        with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
            await browser.screenshot_web_element("file:///tall.html")
        self.assertFalse(harness.contexts[1].closed)
        self.assertTrue(all(page.closed for context in harness.contexts for page in context.pages))

        with patch.object(browser, "CONTEXT_MAX_USES", 2):
            await browser.screenshot_web_element("file:///ok.html")  # second use of contexts[1]
            self.assertTrue(harness.contexts[1].closed)
            await browser.screenshot_web_element("file:///ok.html")
        self.assertEqual(len(harness.contexts), 3)

    async def test_cancelled_waiters_leave_the_queue_and_running_pages_still_close(self):
        recorder = Recorder(delays={"file:///long.html": 5.0, "file:///blocker.html": 0.3})
        harness = await self.start(concurrency=1, recorder=recorder)
        blocker = asyncio.create_task(browser.screenshot_web_element("file:///blocker.html"))
        await _settle()
        waiting = asyncio.create_task(browser.screenshot_web_element("file:///waiting.html"))
        await _settle()
        self.assertEqual(browser.browser_queue_snapshot()["waiting"]["interactive"], 1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await _settle()
        self.assertEqual(browser.browser_queue_snapshot()["waiting"]["interactive"], 0)
        self.assertEqual(await blocker, b"file:///blocker.html")
        self.assertEqual(await browser.screenshot_web_element("file:///after.html"), b"file:///after.html")
        self.assertNotIn("file:///waiting.html", recorder.started)

        running = asyncio.create_task(browser.screenshot_web_element("file:///long.html"))
        await _settle()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await _settle()
        last_page = harness.contexts[-1].pages[-1]
        self.assertTrue(last_page.closed)
        self.assertTrue(harness.contexts[-1].closed)  # an interrupted render does not go back to the pool
        self.assertEqual(browser.browser_queue_snapshot()["inflight"], 0)
        self.assertEqual(await browser.screenshot_web_element("file:///next.html"), b"file:///next.html")

    async def test_close_from_any_loop_then_restart(self):
        recorder = Recorder()
        harness = await self.start(concurrency=2, recorder=recorder)
        await browser.screenshot_web_element("file:///one.html")
        host = harness.host
        self.assertTrue(host.thread.is_alive())

        await asyncio.to_thread(asyncio.run, browser.close_browser())  # another thread, another loop
        self.assertIsNone(browser._host)
        self.assertFalse(host.thread.is_alive())
        self.assertTrue(harness.playwrights[0].stopped)
        self.assertTrue(all(context.closed for context in harness.contexts))
        self.assertEqual(
            browser.browser_queue_snapshot(),
            {"inflight": 0, "waiting": {"interactive": 0, "background": 0}, "concurrency": 2, "contexts": 0},
        )
        await browser.close_browser()  # idempotent

        self.assertEqual(await browser.screenshot_web_element("file:///two.html"), b"file:///two.html")
        self.assertEqual(len(harness.browsers), 2)
        self.assertIsNot(harness.host, host)

    async def test_close_cancels_renders_that_outlive_the_wait(self):
        recorder = Recorder(delays={"file:///stuck.html": 5.0})
        await self.start(concurrency=1, recorder=recorder)
        stuck = asyncio.create_task(browser.screenshot_web_element("file:///stuck.html"))
        await _settle()
        started = time.perf_counter()
        with patch.object(browser, "CLOSE_WAIT_SECONDS", 0.1):
            await browser.close_browser()
        self.assertLess(time.perf_counter() - started, 1.0)
        with self.assertRaisesRegex(RuntimeError, "browser closed"):
            await stuck

    async def test_failed_launch_stops_its_driver_and_the_next_render_retries(self):
        recorder = Recorder()
        harness = await self.start(concurrency=1, recorder=recorder)
        launch = browser._launch_browser  # the harness fake
        attempts = 0

        async def flaky(playwright, proxy_conf):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("No browser found")
            return await launch(playwright, proxy_conf)

        with patch.object(browser, "_launch_browser", flaky):
            with self.assertRaisesRegex(RuntimeError, "No browser found"):
                await browser.screenshot_web_element("file:///a.html")
            self.assertTrue(harness.playwrights[0].stopped)
            self.assertEqual(browser.browser_queue_snapshot()["inflight"], 0)
            self.assertEqual(await browser.screenshot_web_element("file:///b.html"), b"file:///b.html")
        self.assertEqual(len(harness.playwrights), 2)

    async def test_disconnected_browser_is_relaunched(self):
        recorder = Recorder()
        harness = await self.start(concurrency=1, recorder=recorder)
        await browser.screenshot_web_element("file:///one.html")
        harness.browsers[0].connected = False
        await browser.screenshot_web_element("file:///two.html")
        self.assertEqual(len(harness.browsers), 2)
        self.assertTrue(harness.playwrights[0].stopped)
        self.assertTrue(harness.contexts[0].closed)


class RenderPagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_keep_page_order(self):
        async def page(value: int, delay: float) -> int:
            await asyncio.sleep(delay)
            return value

        result = await render_pages([lambda: page(1, 0.05), lambda: page(2, 0.0), lambda: page(3, 0.02)])
        self.assertEqual(result, (1, 2, 3))
        self.assertEqual(await render_pages([]), ())

    async def test_first_failure_cancels_the_rest_and_propagates_unwrapped(self):
        from plugins.endfield.rendering.cards import is_height_limit_error

        cancelled: list[int] = []

        async def slow(index: int) -> bytes:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(index)
                raise
            return b"never"

        async def too_tall() -> bytes:
            await asyncio.sleep(0.01)
            raise RuntimeError("Screenshot element height 13000px exceeds limit 12000px")

        with self.assertRaises(RuntimeError) as caught:
            await render_pages([lambda: slow(1), too_tall, lambda: slow(3)])
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertTrue(is_height_limit_error(caught.exception))
        self.assertEqual(sorted(cancelled), [1, 3])


class RoutePatternTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_only_the_resource_directories(self):
        resources = {
            "https://endfield.local/assets/aa11": BrowserResource(b"a", "image/png"),
            "https://endfield.local/assets/bb22": BrowserResource(b"b", "image/png"),
            "https://endfield-account.local/fonts/Sans.ttf": BrowserResource(b"f", "font/ttf"),
            "file:///opt/bot/assets/help/fonts/Title.ttf": BrowserResource(b"t", "font/ttf"),
        }
        page = FakePage(FakeContext(1.0))
        await browser._install_routes(page, "file:///tmp/card.html", resources)
        self.assertEqual(
            page.routes,
            [
                "https://endfield.local/assets/*",
                "https://endfield-account.local/fonts/*",
                "file:///opt/bot/assets/help/fonts/*",
            ],
        )
        for url in resources:
            self.assertTrue(any(url_matches(None, url, pattern) for pattern in page.routes), url)
        self.assertFalse(any(url_matches(None, "file:///tmp/card.html", pattern) for pattern in page.routes))

        empty = FakePage(FakeContext(1.0))
        await browser._install_routes(empty, "file:///tmp/card.html", {})
        self.assertEqual(empty.routes, [])

        live = FakePage(FakeContext(1.0))
        await browser._install_routes(live, "https://zh.minecraft.wiki/w/Stone", {})
        self.assertEqual(live.routes, ["**/*"])  # live pages still skip fonts and media

    async def test_glob_characters_in_paths_are_escaped(self):
        url = "file:///C:/Bot%20Files/a[1]/{x}/fonts/Title.ttf"
        (pattern,) = browser._route_patterns({url: BrowserResource(b"t")})
        self.assertTrue(pattern.endswith("/*"))
        self.assertTrue(url_matches(None, url, pattern))
        self.assertFalse(url_matches(None, "file:///C:/Bot%20Files/a1/x/fonts/Title.ttf", pattern))

    async def test_handler_fulfills_resources_and_skips_other_fonts(self):
        resources = {"https://endfield-account.local/fonts/Sans.ttf": BrowserResource(b"font", "font/ttf")}
        page = FakePage(FakeContext(1.0))
        handlers = []

        async def route(pattern, handler):
            handlers.append(handler)

        page.route = route
        await browser._install_routes(page, "file:///tmp/card.html", resources)

        class Route:
            def __init__(self, url, resource_type):
                self.request = type("Request", (), {"url": url, "resource_type": resource_type})()
                self.calls = []

            async def fulfill(self, **kwargs):
                self.calls.append(("fulfill", kwargs))

            async def abort(self):
                self.calls.append(("abort", {}))

            async def continue_(self):
                self.calls.append(("continue", {}))

        hit = Route("https://endfield-account.local/fonts/Sans.ttf", "font")
        missing_font = Route("https://endfield-account.local/fonts/Missing.ttf", "font")
        other = Route("https://endfield-account.local/fonts/readme.txt", "fetch")
        for item in (hit, missing_font, other):
            await handlers[0](item)
        self.assertEqual(hit.calls, [("fulfill", {"status": 200, "body": b"font", "content_type": "font/ttf"})])
        self.assertEqual(missing_font.calls, [("abort", {})])
        self.assertEqual(other.calls, [("continue", {})])


class HostSettingsTests(unittest.TestCase):
    def test_concurrency_setting_is_clamped(self):
        self.assertEqual(browser._render_concurrency(), 2)
        self.assertEqual(browser._render_concurrency(3), 3)
        self.assertEqual(browser._render_concurrency(9), 4)
        self.assertEqual(browser._render_concurrency(0), 1)
        with settings.override({"otae": {"browser": {"concurrency": "3"}}}):
            self.assertEqual(browser._render_concurrency(), 3)
        with settings.override({"otae": {"browser": {"concurrency": "two"}}}):
            self.assertEqual(browser._render_concurrency(), 2)

    def test_windows_host_loop_is_proactor(self):
        class FakeProactor:
            pass

        with patch.object(browser, "sys", SimpleNamespace(platform="win32")), patch.object(
            asyncio, "ProactorEventLoop", FakeProactor, create=True
        ):
            self.assertIsInstance(browser._new_host_loop(), FakeProactor)
        loop = browser._new_host_loop()
        try:
            self.assertIsInstance(loop, asyncio.AbstractEventLoop)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
