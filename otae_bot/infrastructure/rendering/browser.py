"""Reusable browser process, resource loading and HTML screenshots."""

from typing import Literal
from otae_bot.config.paths import TEMP_PATH
from otae_bot.config.settings import SYSTEM_PROXY
import base64
from pathlib import Path
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from collections.abc import Mapping, Sequence


_playwright_manager = None
_browser = None
_browser_lock = threading.Lock()
_browser_executor = None
_browser_executor_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class BrowserResource:
    content: bytes
    content_type: str = "application/octet-stream"


def _playwright_proxy_conf():
    if isinstance(SYSTEM_PROXY, dict) and SYSTEM_PROXY.get("http"):
        proxy_url = SYSTEM_PROXY["http"].rstrip("/")
        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        return {"server": proxy_url}
    return None


def _get_browser_executor():
    global _browser_executor
    with _browser_executor_lock:
        if _browser_executor is None:
            _browser_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="playwright",
            )
        return _browser_executor


def _get_browser():
    """Return a reusable sync Playwright browser instance."""
    global _playwright_manager, _browser
    with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser

        from playwright.sync_api import sync_playwright

        _playwright_manager = sync_playwright().start()
        _browser = _launch_browser(_playwright_manager, _playwright_proxy_conf())
        return _browser


async def close_browser():
    """Close the reusable browser, useful for tests or graceful shutdown."""
    global _browser_executor
    executor = _get_browser_executor()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, _close_browser_sync)
    with _browser_executor_lock:
        if _browser_executor is executor:
            executor.shutdown(wait=False, cancel_futures=True)
            _browser_executor = None


def _close_browser_sync():
    global _playwright_manager, _browser
    with _browser_lock:
        if _browser is not None:
            _browser.close()
            _browser = None
        if _playwright_manager is not None:
            _playwright_manager.stop()
            _playwright_manager = None


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
) -> bytes:
    """Screenshot a single page element using a reusable browser."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_browser_executor(),
        _screenshot_web_element_sync,
        web_url,
        selector,
        viewport,
        timeout_ms,
        max_height,
        device_scale_factor,
        settle_ms,
        dict(resources or {}),
        wait_for_images,
        strict_max_height,
        tuple(overflow_selectors),
        wait_for_fonts,
        resource_wait_timeout_ms,
        screenshot_timeout_ms,
        screenshot_backend,
    )


def _screenshot_web_element_sync(
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
    if screenshot_backend not in {"playwright", "cdp"}:
        raise ValueError(f"Unsupported screenshot backend: {screenshot_backend}")
    browser = _get_browser()
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=device_scale_factor,
    )
    page = context.new_page()
    try:
        def _route_handler(route):
            resource = resources.get(route.request.url)
            if resource is not None:
                route.fulfill(
                    status=200,
                    body=resource.content,
                    content_type=resource.content_type,
                )
            elif route.request.resource_type in {"media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", _route_handler)
        page.goto(web_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            locator = page.locator(selector).first
        except Exception:
            page.wait_for_selector("body", timeout=timeout_ms)
            locator = page.locator("body").first

        if wait_for_images or wait_for_fonts:
            _settle_page_resources(
                page,
                wait_for_images=wait_for_images,
                wait_for_fonts=wait_for_fonts,
                timeout_ms=resource_wait_timeout_ms,
            )

        size = locator.evaluate(
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
        page.set_viewport_size({"width": target_width, "height": target_height})
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)

        if overflow_selectors:
            overflows = page.evaluate(
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

        box = locator.bounding_box()
        if not box:
            if screenshot_backend == "cdp":
                return _capture_cdp_screenshot(
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
            return page.screenshot(**kwargs)

        clip = {
            "x": max(0, box["x"]),
            "y": max(0, box["y"]),
            "width": max(1, min(box["width"], target_width - max(0, box["x"]))),
            "height": max(1, min(box["height"], target_height)),
        }
        if screenshot_backend == "cdp":
            return _capture_cdp_screenshot(context, page, clip, device_scale_factor)
        kwargs = {"clip": clip, "type": "png"}
        if screenshot_timeout_ms is not None:
            kwargs["timeout"] = max(1, int(screenshot_timeout_ms))
        return page.screenshot(**kwargs)
    finally:
        context.close()


def _settle_page_resources(
    page,
    *,
    wait_for_images: bool,
    wait_for_fonts: bool,
    timeout_ms: int,
) -> dict:
    """Wait for export resources without allowing one broken asset to hang forever."""
    return page.evaluate(
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


def _capture_cdp_screenshot(context, page, clip: dict[str, float], device_scale_factor: float) -> bytes:
    """Capture Chromium pixels without Playwright's unbounded document.fonts.ready wait."""
    session = context.new_cdp_session(page)
    try:
        result = session.send(
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
        session.detach()


def _launch_browser(playwright, proxy_conf):
    """Prefer Playwright's pinned Chromium, then fall back to system browsers."""
    for channel in [None, "chrome", "msedge"]:
        try:
            kwargs = {"headless": True}
            if channel:
                kwargs["channel"] = channel
            if proxy_conf:
                kwargs["proxy"] = proxy_conf
            return playwright.chromium.launch(**kwargs)
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
