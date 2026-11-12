"""A stand-in Playwright for render queue tests: real host thread, queue and pool, no Chromium."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from otae_bot.config import settings
from otae_bot.infrastructure.rendering import browser


class FakePage:
    def __init__(self, context: FakeContext):
        self.context = context
        self.closed = False
        self.routes: list[object] = []
        self.viewports: list[dict] = []

    async def set_viewport_size(self, size):
        self.viewports.append(dict(size))

    async def route(self, pattern, handler):
        self.routes.append(pattern)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, device_scale_factor: float):
        self.device_scale_factor = device_scale_factor
        self.closed = False
        self.pages: list[FakePage] = []

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts: list[FakeContext] = []
        self.connected = True

    async def new_context(self, *, viewport, device_scale_factor):
        context = FakeContext(device_scale_factor)
        self.contexts.append(context)
        return context

    def is_connected(self):
        return self.connected

    async def close(self):
        self.connected = False


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeBrowserHarness:
    """Swap Playwright for fakes; ``await start()`` then gives the test a fresh browser host.

    ``screenshot`` / ``evaluate`` replace the page work; they run on the host loop.
    The host is closed again before the patches are undone.
    """

    def __init__(self, test, *, concurrency: int = 1, screenshot=None, evaluate=None):
        self.browsers: list[FakeBrowser] = []
        self.playwrights: list[FakePlaywright] = []
        test.enterContext(settings.override({"otae": {"browser": {"concurrency": concurrency}}}))
        test.enterContext(patch.object(browser, "_start_playwright", self._start))
        test.enterContext(patch.object(browser, "_launch_browser", self._launch))
        if screenshot is not None:
            test.enterContext(patch.object(browser, "_screenshot_on_page", screenshot))
        if evaluate is not None:
            test.enterContext(patch.object(browser, "_evaluate_on_page", evaluate))
        if isinstance(test, unittest.IsolatedAsyncioTestCase):
            test.addAsyncCleanup(browser.close_browser)
        else:
            test.addCleanup(lambda: asyncio.run(browser.close_browser()))

    async def start(self):
        await browser.close_browser()  # a host left by an earlier test may hold a real Chromium
        return self

    async def _start(self):
        playwright = FakePlaywright()
        self.playwrights.append(playwright)
        return playwright

    async def _launch(self, _playwright, _proxy_conf):
        fake = FakeBrowser()
        self.browsers.append(fake)
        return fake

    @property
    def host(self):
        return browser._host

    @property
    def contexts(self) -> list[FakeContext]:
        return [context for fake in self.browsers for context in fake.contexts]
