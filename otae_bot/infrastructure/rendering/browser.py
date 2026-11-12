"""Reusable browser, render queue, resource loading and HTML screenshots.

Playwright runs on its async API, on a dedicated thread with its own event loop:
Playwright objects stay bound to the loop that created them, while callers (the bot
loop, ``asyncio.run`` in scripts and tests) come and go. One browser serves every
card. A priority queue hands out ``OTAE_BROWSER_CONCURRENCY`` render slots (default 2);
each render opens a page in a pooled context keyed by device scale factor, so renders
in different slots run in parallel and a slow card no longer holds up everyone else.
"""

from typing import Any, Literal, TypeVar
from otae_bot.config.paths import TEMP_PATH
from otae_bot.config.settings import core_settings, system_proxy
import base64
from pathlib import Path
import asyncio
import heapq
import itertools
import sys
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence

from loguru import logger


# 一次截图等渲染槽位超过这么久就记一条告警（槽位是跨用户、跨插件共用的）。
QUEUE_WAIT_WARNING_SECONDS = 1.0
DEFAULT_RENDER_CONCURRENCY = 2
MAX_RENDER_CONCURRENCY = 4
# 一个 context 用满这么多次就关掉换新，避免长期复用累积状态与内存。
CONTEXT_MAX_USES = 200
# 关闭浏览器时等在途截图的秒数，过后取消。
CLOSE_WAIT_SECONDS = 10.0
_DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
_ABORTED_RESOURCE_TYPES = frozenset({"media", "font"})
_T = TypeVar("_T")

_host = None
_host_lock = threading.Lock()


class RenderPriority(IntEnum):
    INTERACTIVE = 0  # a user is waiting for this image
    BACKGROUND = 1  # warm-ups; always yields to interactive renders


@dataclass(frozen=True, slots=True)
class BrowserResource:
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(slots=True)
class ScreenshotTiming:
    """Render-slot time of the screenshots and evaluations in one scope."""

    queue_wait: float = 0.0  # waiting for a render slot
    run: float = 0.0  # holding a render slot
    calls: int = 0


_timing: ContextVar[ScreenshotTiming | None] = ContextVar("browser_screenshot_timing", default=None)


@contextmanager
def screenshot_timing() -> Iterator[ScreenshotTiming]:
    """Collect queue wait and run time of browser calls made inside this block."""
    timing = ScreenshotTiming()
    token = _timing.set(timing)
    try:
        yield timing
    finally:
        _timing.reset(token)


def _render_concurrency(configured: int | None = None) -> int:
    """``otae.browser.concurrency`` clamped to 1..MAX_RENDER_CONCURRENCY."""
    value = core_settings().browser.concurrency if configured is None else configured
    return max(1, min(MAX_RENDER_CONCURRENCY, int(value)))


def _playwright_proxy_conf():
    proxy = system_proxy()
    if proxy.get("http"):
        proxy_url = proxy["http"].rstrip("/")
        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        return {"server": proxy_url}
    return None


class _RenderQueue:
    """Render slots handed out by priority (FIFO within one priority). Host loop only."""

    def __init__(self, concurrency: int):
        self.concurrency = concurrency
        self.inflight = 0
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def waiting(self) -> dict[str, int]:
        counts = {priority.name.lower(): 0 for priority in RenderPriority}
        for priority, _seq, waiter in list(self._heap):
            if not waiter.done():
                counts[RenderPriority(priority).name.lower()] += 1
        return counts

    async def acquire(self, priority: RenderPriority) -> None:
        if self.inflight < self.concurrency and not self._heap:
            self.inflight += 1
            return
        entry = (int(priority), next(self._seq), asyncio.get_running_loop().create_future())
        heapq.heappush(self._heap, entry)
        try:
            await entry[2]
        except asyncio.CancelledError:
            if entry[2].done() and not entry[2].cancelled():
                self.release()  # the slot arrived together with the cancellation
            else:
                with suppress(ValueError):
                    self._heap.remove(entry)
                    heapq.heapify(self._heap)
            raise

    def release(self) -> None:
        self.inflight -= 1
        while self._heap and self.inflight < self.concurrency:
            _priority, _seq, waiter = heapq.heappop(self._heap)
            if waiter.done():
                continue
            self.inflight += 1
            waiter.set_result(None)


@dataclass(eq=False, slots=True)
class _PooledContext:
    context: Any
    device_scale_factor: float
    generation: int
    uses: int = 0


class _ContextPool:
    """Idle browser contexts by device scale factor, at most ``limit`` held in total.

    The scale factor is fixed when a context is created, the viewport is not, so one
    context per slot and scale factor is enough. Host loop only.
    """

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.busy = 0
        self.created = 0
        self.reused = 0
        self.closed = False
        self._generation = 0
        self._idle: list[_PooledContext] = []  # oldest release first

    def held(self) -> int:
        return self.busy + len(self._idle)

    def idle_scale_factors(self) -> list[float]:
        return [entry.device_scale_factor for entry in self._idle]

    async def acquire(self, browser, device_scale_factor: float) -> _PooledContext:
        for index in range(len(self._idle) - 1, -1, -1):
            if self._idle[index].device_scale_factor == device_scale_factor:
                entry = self._idle.pop(index)
                self.busy += 1
                self.reused += 1
                return entry
        victims = []
        while self._idle and self.held() >= self.limit:
            victims.append(self._idle.pop(0))
        self.busy += 1
        try:
            for victim in victims:
                await _close_quietly(victim.context)
            context = await browser.new_context(
                viewport=dict(_DEFAULT_VIEWPORT),
                device_scale_factor=device_scale_factor,
            )
        except BaseException:
            self.busy -= 1
            raise
        self.created += 1
        return _PooledContext(context, device_scale_factor, self._generation)

    async def release(self, entry: _PooledContext, *, keep: bool) -> None:
        self.busy -= 1
        entry.uses += 1
        if (
            keep
            and not self.closed
            and entry.generation == self._generation
            and entry.uses < CONTEXT_MAX_USES
        ):
            self._idle.append(entry)
            return
        await _close_quietly(entry.context)

    async def drop_all(self, *, close: bool = False) -> None:
        """Forget every context, e.g. after the browser died; busy ones close on release."""
        self.closed = self.closed or close
        self._generation += 1
        idle, self._idle = self._idle, []
        for entry in idle:
            await _close_quietly(entry.context)


async def _close_quietly(target) -> None:
    """Close a page or context; survives a second cancellation and a dead browser."""
    with suppress(Exception):
        await asyncio.shield(target.close())


def _new_host_loop() -> asyncio.AbstractEventLoop:
    # The Playwright driver is a subprocess; on Windows only the Proactor loop can start
    # one, whatever loop policy the bot process uses.
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


async def _start_playwright():
    from playwright.async_api import async_playwright

    return await async_playwright().start()


class _BrowserHost:
    """The browser thread: its loop owns Playwright, the render queue and the context pool."""

    def __init__(self, concurrency: int):
        self.concurrency = concurrency
        self.closing = False
        self.queue = _RenderQueue(concurrency)
        self.pool = _ContextPool(concurrency)
        self.loop = _new_host_loop()
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._jobs: set[asyncio.Task] = set()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="playwright", daemon=True)
        self.thread.start()
        self._ready.wait()

    def alive(self) -> bool:
        return self.thread.is_alive() and not self.loop.is_closed() and not self.closing

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            leftovers = [task for task in asyncio.all_tasks(self.loop) if not task.done()]
            for task in leftovers:
                task.cancel()
            if leftovers:
                self.loop.run_until_complete(asyncio.gather(*leftovers, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    async def run(
        self,
        job: Callable[[Any, Any], Awaitable[_T]],
        device_scale_factor: float,
        priority: RenderPriority,
        marks: list[float],
    ) -> _T:
        if self.closing:
            raise RuntimeError("browser is closing")
        task = asyncio.current_task()
        self._jobs.add(task)
        try:
            await self.queue.acquire(priority)
            marks.append(time.perf_counter())
            try:
                return await self._run_in_context(job, device_scale_factor)
            finally:
                marks.append(time.perf_counter())
                self.queue.release()
        except asyncio.CancelledError:
            if self.closing:
                raise RuntimeError("browser closed while rendering") from None
            raise
        finally:
            self._jobs.discard(task)

    async def _run_in_context(self, job, device_scale_factor: float):
        browser = await self._ensure_browser()
        entry = await self.pool.acquire(browser, device_scale_factor)
        keep = False
        try:
            page = await entry.context.new_page()
            try:
                result = await job(page, entry.context)
            except RuntimeError:
                # Height / overflow rejections: the page answered, the context is clean.
                keep = True
                raise
            finally:
                await _close_quietly(page)
            keep = True
            return result
        finally:
            await self.pool.release(entry, keep=keep and browser.is_connected())

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                logger.warning("[browser] browser disconnected; relaunching")
            await self.pool.drop_all()
            await self._stop_playwright()
            self._playwright = await _start_playwright()
            try:
                self._browser = await _launch_browser(self._playwright, _playwright_proxy_conf())
            except BaseException:
                await self._stop_playwright()  # no driver left behind for the next attempt
                raise
            return self._browser

    async def _stop_playwright(self) -> None:
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    async def shutdown(self, wait_seconds: float) -> None:
        self.closing = True
        jobs = [task for task in self._jobs if not task.done()]
        if jobs:
            _done, pending = await asyncio.wait(jobs, timeout=wait_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=wait_seconds)
        async with self._browser_lock:
            await self.pool.drop_all(close=True)
            await self._stop_playwright()

    def snapshot(self) -> dict:
        return {
            "inflight": self.queue.inflight,
            "waiting": self.queue.waiting(),
            "concurrency": self.concurrency,
            "contexts": self.pool.held(),
        }


def _ensure_host() -> _BrowserHost:
    global _host
    with _host_lock:
        if _host is None or not _host.alive():
            _host = _BrowserHost(_render_concurrency())
        return _host


async def _run_on_browser(
    job: Callable[[Any, Any], Awaitable[_T]],
    *,
    device_scale_factor: float,
    priority: RenderPriority,
    name: str,
) -> _T:
    """Run ``job(page, context)`` on the browser loop once a render slot is free."""
    host = _ensure_host()
    queued = time.perf_counter()
    marks: list[float] = []
    future = asyncio.run_coroutine_threadsafe(
        host.run(job, float(device_scale_factor), RenderPriority(priority), marks),
        host.loop,
    )
    ok = False
    try:
        result = await asyncio.wrap_future(future)
        ok = True
        return result
    finally:
        _render_log.append((time.time(), time.perf_counter() - queued, ok))
        if marks:
            waited = marks[0] - queued
            timing = _timing.get()
            if timing is not None:
                timing.queue_wait += waited
                timing.run += (marks[1] if len(marks) > 1 else time.perf_counter()) - marks[0]
                timing.calls += 1
            if waited > QUEUE_WAIT_WARNING_SECONDS:
                logger.warning(f"[browser] queue_wait={waited:.3f}s before {name}")


#: (wall time, seconds including queue wait, succeeded) of recent browser renders.
_render_log: deque[tuple[float, float, bool]] = deque(maxlen=4000)


def render_timing_snapshot(since: float = 0.0) -> dict:
    """Count, mean and p95 seconds of browser renders finished after ``since`` (wall time)."""
    durations = sorted(seconds for at, seconds, ok in _render_log if at >= since and ok)
    failures = sum(1 for at, _, ok in _render_log if at >= since and not ok)
    if not durations:
        return {"count": 0, "failures": failures, "mean": None, "p95": None}
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
    return {"count": len(durations), "failures": failures, "mean": sum(durations) / len(durations), "p95": p95}


def browser_queue_snapshot() -> dict:
    """Render slots in use, renders waiting by priority, concurrency and pooled contexts."""
    host = _host
    if host is None or not host.alive():
        return {
            "inflight": 0,
            "waiting": {priority.name.lower(): 0 for priority in RenderPriority},
            "concurrency": _render_concurrency(),
            "contexts": 0,
        }
    return host.snapshot()


async def close_browser():
    """Close the reusable browser from any loop; the next render starts a new one."""
    global _host
    with _host_lock:
        host, _host = _host, None
    if host is None:
        return
    if host.thread.is_alive() and not host.loop.is_closed():
        try:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(host.shutdown(CLOSE_WAIT_SECONDS), host.loop)
            )
        finally:
            with suppress(RuntimeError):
                host.loop.call_soon_threadsafe(host.loop.stop)
    await asyncio.to_thread(host.thread.join, CLOSE_WAIT_SECONDS)


def _glob_escape(text: str) -> str:
    return "".join(f"\\{char}" if char in "\\*?{}[]" else char for char in text)


def _route_patterns(resources: Mapping[str, BrowserResource]) -> list[str]:
    """One glob per directory of the virtual resource URLs.

    String globs are matched by the Playwright driver, so only requests for these
    directories come back to Python; everything else loads without a round trip.
    """
    prefixes = dict.fromkeys(url.rsplit("/", 1)[0] for url in resources if "/" in url)
    return [f"{_glob_escape(prefix)}/*" for prefix in prefixes]


async def _install_routes(page, web_url: str, resources: Mapping[str, BrowserResource]) -> None:
    async def handler(route):
        request = route.request
        resource = resources.get(request.url)
        if resource is not None:
            await route.fulfill(status=200, body=resource.content, content_type=resource.content_type)
        elif request.resource_type in _ABORTED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    if not web_url.startswith(("file:", "data:")):
        # A live web page (wiki screenshots): keep skipping its fonts and media.
        await page.route("**/*", handler)
        return
    for pattern in _route_patterns(resources):
        await page.route(pattern, handler)


async def screenshot_web_element(
    web_url: str,
    selector: str = "body",
    *,
    viewport: tuple[int, int] = (1280, 900),
    timeout_ms: int = 15000,
    max_height: int = 20000,
    device_scale_factor: float = 1.0,
    settle_ms: int = 300,
    resources: Mapping[str, BrowserResource] | None = None,
    wait_for_images: bool = False,
    strict_max_height: bool = False,
    overflow_selectors: Sequence[str] = (),
    wait_for_fonts: bool = False,
    resource_wait_timeout_ms: int = 5000,
    screenshot_timeout_ms: int | None = None,
    screenshot_backend: Literal["playwright", "cdp"] = "playwright",
    priority: RenderPriority = RenderPriority.INTERACTIVE,
) -> bytes:
    """Screenshot a single page element using the shared browser."""
    if screenshot_backend not in {"playwright", "cdp"}:
        raise ValueError(f"Unsupported screenshot backend: {screenshot_backend}")
    resource_map = dict(resources or {})
    overflow = tuple(overflow_selectors)

    async def job(page, context):
        return await _screenshot_on_page(
            page,
            context,
            web_url,
            selector,
            viewport,
            timeout_ms,
            max_height,
            device_scale_factor,
            settle_ms,
            resource_map,
            wait_for_images,
            strict_max_height,
            overflow,
            wait_for_fonts,
            resource_wait_timeout_ms,
            screenshot_timeout_ms,
            screenshot_backend,
        )

    return await _run_on_browser(
        job,
        device_scale_factor=device_scale_factor,
        priority=priority,
        name="screenshot_web_element",
    )


async def _screenshot_on_page(
    page,
    context,
    web_url: str,
    selector: str,
    viewport: tuple[int, int],
    timeout_ms: int,
    max_height: int,
    device_scale_factor: float,
    settle_ms: int,
    resources: dict[str, BrowserResource],
    wait_for_images: bool,
    strict_max_height: bool,
    overflow_selectors: tuple[str, ...],
    wait_for_fonts: bool,
    resource_wait_timeout_ms: int,
    screenshot_timeout_ms: int | None,
    screenshot_backend: Literal["playwright", "cdp"],
) -> bytes:
    await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    await _install_routes(page, web_url, resources)
    await page.goto(web_url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        locator = page.locator(selector).first
    except Exception:
        await page.wait_for_selector("body", timeout=timeout_ms)
        locator = page.locator("body").first

    if wait_for_images or wait_for_fonts:
        await _settle_page_resources(
            page,
            wait_for_images=wait_for_images,
            wait_for_fonts=wait_for_fonts,
            timeout_ms=resource_wait_timeout_ms,
        )

    size = await locator.evaluate(
        """el => ({
            width: Math.ceil(Math.max(
                el.scrollWidth,
                el.offsetWidth,
                el.getBoundingClientRect().width
            )),
            height: Math.ceil(Math.max(
                el.scrollHeight,
                el.offsetHeight,
                el.getBoundingClientRect().height
            ))
        })"""
    )
    measured_height = max(1, int(size["height"]))
    if strict_max_height and measured_height > max_height:
        raise RuntimeError(
            f"Screenshot element height {measured_height}px exceeds limit {max_height}px"
        )
    target_width = max(viewport[0], min(max(1, int(size["width"])), 4000))
    target_height = max(viewport[1], min(measured_height, max_height))
    await page.set_viewport_size({"width": target_width, "height": target_height})
    if settle_ms > 0:
        await page.wait_for_timeout(settle_ms)

    if overflow_selectors:
        overflows = await page.evaluate(
            """selectors => selectors.flatMap(selector =>
                Array.from(document.querySelectorAll(selector)).flatMap((element, index) => {
                    const vertical = element.scrollHeight > element.clientHeight + 1;
                    const horizontal = element.scrollWidth > element.clientWidth + 1;
                    return vertical || horizontal ? [{selector, index, vertical, horizontal}] : [];
                })
            )""",
            list(overflow_selectors),
        )
        if overflows:
            raise RuntimeError(f"Screenshot layout overflow detected: {overflows}")

    box = await locator.bounding_box()
    if not box:
        if screenshot_backend == "cdp":
            return await _capture_cdp_screenshot(
                context,
                page,
                {
                    "x": 0,
                    "y": 0,
                    "width": target_width,
                    "height": target_height,
                },
                device_scale_factor,
            )
        kwargs = {"type": "png", "full_page": True}
        if screenshot_timeout_ms is not None:
            kwargs["timeout"] = max(1, int(screenshot_timeout_ms))
        return await page.screenshot(**kwargs)

    clip = {
        "x": max(0, box["x"]),
        "y": max(0, box["y"]),
        "width": max(1, min(box["width"], target_width - max(0, box["x"]))),
        "height": max(1, min(box["height"], target_height)),
    }
    if screenshot_backend == "cdp":
        return await _capture_cdp_screenshot(context, page, clip, device_scale_factor)
    kwargs = {"clip": clip, "type": "png"}
    if screenshot_timeout_ms is not None:
        kwargs["timeout"] = max(1, int(screenshot_timeout_ms))
    return await page.screenshot(**kwargs)


async def evaluate_web_page(
    web_url: str,
    script: str,
    *,
    viewport: tuple[int, int] = (1280, 900),
    device_scale_factor: float = 2.0,
    timeout_ms: int = 15000,
    wait_for_images: bool = True,
    wait_for_fonts: bool = True,
    resource_wait_timeout_ms: int = 5000,
    priority: RenderPriority = RenderPriority.INTERACTIVE,
) -> Any:
    """Load a page in the shared browser and return the JSON result of ``script``.

    Meant for layout measurement before screenshots: it takes a render slot like
    :func:`screenshot_web_element` and applies the same request routing, so measured
    sizes match what a screenshot would see.
    """

    async def job(page, _context):
        return await _evaluate_on_page(
            page,
            web_url,
            script,
            viewport,
            timeout_ms,
            wait_for_images,
            wait_for_fonts,
            resource_wait_timeout_ms,
        )

    return await _run_on_browser(
        job,
        device_scale_factor=device_scale_factor,
        priority=priority,
        name="evaluate_web_page",
    )


async def _evaluate_on_page(
    page,
    web_url: str,
    script: str,
    viewport: tuple[int, int],
    timeout_ms: int,
    wait_for_images: bool,
    wait_for_fonts: bool,
    resource_wait_timeout_ms: int,
) -> Any:
    await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    await _install_routes(page, web_url, {})
    await page.goto(web_url, wait_until="domcontentloaded", timeout=timeout_ms)
    if wait_for_images or wait_for_fonts:
        await _settle_page_resources(
            page,
            wait_for_images=wait_for_images,
            wait_for_fonts=wait_for_fonts,
            timeout_ms=resource_wait_timeout_ms,
        )
    return await page.evaluate(script)


async def _settle_page_resources(
    page,
    *,
    wait_for_images: bool,
    wait_for_fonts: bool,
    timeout_ms: int,
) -> dict:
    """Wait for export resources without allowing one broken asset to hang forever."""
    return await page.evaluate(
        """async options => {
            const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
            const tasks = [];
            if (options.waitForFonts && document.fonts?.ready) {
                tasks.push(document.fonts.ready.catch(() => undefined));
            }
            if (options.waitForImages) {
                const pending = Array.from(document.images)
                    .filter(image => !image.complete)
                    .map(image => new Promise(resolve => {
                        image.addEventListener('load', resolve, {once: true});
                        image.addEventListener('error', resolve, {once: true});
                    }));
                tasks.push(Promise.all(pending));
            }
            let timedOut = false;
            await Promise.race([
                Promise.all(tasks),
                delay(options.timeoutMs).then(() => { timedOut = true; }),
            ]);
            const failedImages = Array.from(document.images)
                .filter(image => image.complete && !image.naturalWidth).length;
            const failedRequiredImages = Array.from(document.images)
                .filter(image => image.dataset.required === 'true' && (!image.complete || !image.naturalWidth)).length;
            return {
                timedOut,
                fontsReady: !options.waitForFonts || !document.fonts || document.fonts.status === 'loaded',
                failedImages,
                failedRequiredImages,
            };
        }""",
        {
            "waitForImages": bool(wait_for_images),
            "waitForFonts": bool(wait_for_fonts),
            "timeoutMs": max(1, int(timeout_ms)),
        },
    )


async def _capture_cdp_screenshot(context, page, clip: dict[str, float], device_scale_factor: float) -> bytes:
    """Capture Chromium pixels without Playwright's unbounded document.fonts.ready wait."""
    session = await context.new_cdp_session(page)
    try:
        result = await session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": True,
                "clip": {
                    "x": float(clip["x"]),
                    "y": float(clip["y"]),
                    "width": float(clip["width"]),
                    "height": float(clip["height"]),
                    "scale": max(0.1, float(device_scale_factor)),
                },
            },
        )
        return base64.b64decode(result["data"])
    finally:
        await session.detach()


def _launch_candidates(proxy_conf) -> list[dict]:
    """Launch options in order: Playwright's pinned Chromium, then system Chrome / Edge."""
    candidates = []
    for channel in (None, "chrome", "msedge"):
        kwargs = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        if proxy_conf:
            kwargs["proxy"] = proxy_conf
        candidates.append(kwargs)
    return candidates


async def _launch_browser(playwright, proxy_conf):
    """Prefer Playwright's pinned Chromium, then fall back to system browsers."""
    for kwargs in _launch_candidates(proxy_conf):
        try:
            return await playwright.chromium.launch(**kwargs)
        except Exception:
            continue
    raise RuntimeError("No browser found. Install Chrome/Edge or run: playwright install chromium")


async def WebImageBuilders(fill_name: str = None, web_url: str = None, **kwargs):
    """截图指定网页并保存（使用系统 Chrome/Edge，无需额外安装浏览器）."""
    fill_name = fill_name or kwargs.get("fillName")
    web_url = web_url or kwargs.get("webUrl")
    if not fill_name or not web_url:
        raise TypeError("WebImageBuilders requires fill_name/web_url")

    png = await screenshot_web_element(web_url, "body")
    Path(TEMP_PATH).mkdir(parents=True, exist_ok=True)
    (Path(TEMP_PATH) / f"{fill_name}.png").write_bytes(png)
