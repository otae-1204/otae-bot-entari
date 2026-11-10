"""HYW 卡片渲染失败回退、渲染期限分段与送渲染配图上限。"""
from __future__ import annotations

import base64
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("hyw_frontier")
pytest.importorskip("md2png")

from PIL import Image

from hyw_frontier import api, rendering
from hyw_frontier.media import fit_edge
from hyw_frontier.media_refs import display_url
from hyw_frontier.render_worker import DEFAULT_DRAW_TIMEOUT, MAX_DRAW_TIMEOUT, draw_timeout
from hyw_frontier.rendering import CardRenderer, RenderError, worker_timeout

from plugins.hyw.config import HywConfig

ANSWER = ("<final_response><summary>一句话摘要</summary>\n# 标题\n\n"
          "完整答案正文，引用 [来源](https://example.com/a)。\n</final_response>")


def _jpeg(width: int, height: int, shade: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (shade, 120, 160)).save(buffer, "JPEG", quality=70)
    return buffer.getvalue()


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, "PNG")
    return buffer.getvalue()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class Stdin:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes):
        self.data += data

    def flush(self):
        pass


class StubbedRenderer:
    """CardRenderer with a fake clock: queue, prewarm and drawing take scripted seconds."""

    def __init__(self, case: unittest.TestCase, *, timeout=45.0, queue=0.0, prepare=0.0, draw=0.0):
        self.clock = Clock()
        patcher = patch.object(rendering, "time", self.clock)
        patcher.start()
        case.addCleanup(patcher.stop)
        self.renderer = CardRenderer(timeout=timeout)
        case.addCleanup(self.renderer.close)
        self.seconds = {"queue": queue, "prepare": prepare, "draw": draw}
        self.calls: dict = {}
        self.renderer._acquire = self._acquire
        self.renderer._ensure_worker = self._ensure_worker
        self.renderer._receive = self._receive

    def _acquire(self, cancel, deadline):
        self.calls["queue_budget"] = deadline - self.clock.now
        self.clock.advance(self.seconds["queue"])
        self.renderer._check(cancel, deadline)
        self.renderer.slots.acquire()

    def _ensure_worker(self, cancel, deadline, font_set=None):
        self.calls["prepare_budget"] = deadline - self.clock.now
        self.clock.advance(self.seconds["prepare"])
        self.renderer._check(cancel, deadline)
        self.calls["ready_at"] = self.clock.now
        return SimpleNamespace(stdin=Stdin(), poll=lambda: None)

    def _receive(self, process, cancel, deadline):
        self.calls["draw_deadline"] = deadline
        work = Path(json.loads(process.stdin.data.decode())["work"])
        self.calls["payload"] = json.loads((work / "input.json").read_text(encoding="utf-8"))
        self.calls["sent_at"] = self.clock.now
        self.clock.advance(self.seconds["draw"])
        self.renderer._check(cancel, deadline)
        (work / "card.png").write_bytes(_png())
        return {"ok": True, "timings": {"worker_ms": 5.0}, "diagnostics": [], "links": []}

    def render(self, text: str = ANSWER, **kwargs):
        return self.renderer.render(text, metadata={}, cancel=Event(), **kwargs)


class RenderDeadlineTests(unittest.TestCase):
    def test_queue_and_prewarm_do_not_eat_the_drawing_budget(self):
        stub = StubbedRenderer(self, timeout=45, queue=100, prepare=30, draw=40)
        card = stub.render()
        # 排队 100s、预热 30s 都超过 45s 的旧总期限，但绘制期限从 worker 就绪后才起算。
        self.assertEqual(stub.calls["draw_deadline"], stub.calls["ready_at"] + 45)
        self.assertEqual(stub.calls["queue_budget"], math.inf)
        self.assertEqual(stub.calls["prepare_budget"], 45)
        self.assertEqual(card.timings["queue_ms"], 100_000)
        self.assertEqual(card.timings["prepare_ms"], 30_000)
        self.assertEqual(card.timings["draw_ms"], 40_000)
        self.assertEqual(card.timings["worker_ms"], 5.0)
        self.assertEqual(card.timings["total_ms"], 170_000)

    def test_worker_budget_never_exceeds_the_parent_deadline(self):
        stub = StubbedRenderer(self, timeout=45)
        stub.render()
        remaining = stub.calls["draw_deadline"] - stub.calls["sent_at"]
        self.assertEqual(stub.calls["payload"]["timeout"], 40)
        self.assertLessEqual(stub.calls["payload"]["timeout"], remaining)
        for seconds in (0.05, 0.5, 3, 10, 45, 90, 700):
            with self.subTest(seconds=seconds):
                budget = worker_timeout(seconds)
                self.assertGreater(budget, 0)
                self.assertLessEqual(budget, seconds)
                self.assertEqual(draw_timeout(budget), min(budget, MAX_DRAW_TIMEOUT))

    def test_render_timeout_setting_reaches_the_worker(self):
        stub = StubbedRenderer(self, timeout=20)
        stub.render()
        self.assertEqual(stub.calls["draw_deadline"], stub.calls["ready_at"] + 20)
        self.assertEqual(stub.calls["payload"]["timeout"], 15)

    def test_slow_drawing_still_times_out_with_phase_timings(self):
        stub = StubbedRenderer(self, timeout=45, queue=2, prepare=3, draw=50)
        with self.assertRaises(RenderError) as raised:
            stub.render()
        error = raised.exception
        self.assertEqual((error.code, error.stage), ("render_timeout", "draw"))
        self.assertEqual(error.timings["queue_ms"], 2000)
        self.assertEqual(error.timings["prepare_ms"], 3000)
        self.assertEqual(error.timings["draw_ms"], 50_000)
        self.assertEqual(error.timings["total_ms"], 55_000)
        self.assertEqual(error.selection["selected_images"], 0)

    def test_a_stuck_prewarm_has_its_own_bound(self):
        stub = StubbedRenderer(self, timeout=45, prepare=60)
        with self.assertRaises(RenderError) as raised:
            stub.render()
        self.assertEqual((raised.exception.code, raised.exception.stage), ("render_timeout", "prepare"))
        self.assertEqual(raised.exception.timings["prepare_ms"], 60_000)

    def test_worker_draw_timeout_parsing(self):
        self.assertEqual(draw_timeout(12.5), 12.5)
        for bad in (None, "30", 0, -1, math.nan, math.inf, True):
            with self.subTest(bad=bad):
                self.assertEqual(draw_timeout(bad), DEFAULT_DRAW_TIMEOUT)
        self.assertEqual(draw_timeout(10_000), MAX_DRAW_TIMEOUT)

    def test_invalid_renderer_timeout_is_rejected(self):
        for bad in (0, -5, math.inf, math.nan, "45", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CardRenderer(timeout=bad)


class RenderImageTests(unittest.TestCase):
    def _assets(self, count: int, *, wide=()):
        assets = {}
        for index in range(count):
            raw = _jpeg(1280 if index in wide else 120, 80, index * 7 % 255)
            assets[display_url(f"https://example.com/{index}.jpg", raw)] = raw
        return assets

    def _answer(self, urls) -> str:
        pictures = "\n\n".join(f"![图{index}]({url})" for index, url in enumerate(urls))
        return f"<final_response><summary>摘要</summary>\n# 标题\n\n{pictures}\n</final_response>"

    def test_only_the_first_images_in_reading_order_are_sent(self):
        assets = self._assets(20)
        ordered = list(assets)
        reversed_assets = dict(reversed(list(assets.items())))  # 资产字典顺序 ≠ 正文引用顺序
        stub = StubbedRenderer(self)
        card = stub.render(self._answer(ordered), image_assets=reversed_assets, max_images=16)
        self.assertEqual(list(stub.calls["payload"]["image_assets"]), ordered[:16])
        selection = card.diagnostics[0]
        self.assertEqual(selection["referenced_images"], 20)
        self.assertEqual(selection["selected_images"], 16)
        self.assertEqual(selection["dropped_images"], 4)

    def test_without_a_cap_every_referenced_image_is_sent(self):
        assets = self._assets(5)
        stub = StubbedRenderer(self)
        stub.render(self._answer(list(assets)), image_assets=assets)
        self.assertEqual(len(stub.calls["payload"]["image_assets"]), 5)

    def test_wide_images_are_shrunk_and_small_ones_pass_through(self):
        assets = self._assets(3, wide={1})
        urls = list(assets)
        stub = StubbedRenderer(self)
        card = stub.render(self._answer(urls), image_assets=assets, max_image_edge=1024)
        sent = {url: base64.b64decode(data) for url, data in stub.calls["payload"]["image_assets"].items()}
        self.assertEqual(sent[urls[0]], assets[urls[0]])
        self.assertEqual(sent[urls[2]], assets[urls[2]])
        with Image.open(io.BytesIO(sent[urls[1]])) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 1024)
        self.assertEqual(card.diagnostics[0]["resized_images"], 1)

    def test_fit_edge_reuses_compress_and_never_upscales(self):
        small = _jpeg(800, 600)
        self.assertEqual(fit_edge(small, 1024), (small, False))
        shrunk, resized = fit_edge(_jpeg(1280, 640), 1024)
        self.assertTrue(resized)
        with Image.open(io.BytesIO(shrunk)) as image:
            self.assertEqual(image.size, (1024, 512))
        with self.assertRaises(Exception):
            fit_edge(b"not an image", 1024)


class FormulaPrecheckTests(unittest.TestCase):
    def test_too_many_formulas_skip_drawing(self):
        stub = StubbedRenderer(self)
        formulas = " ".join(f"$x_{{{index}}}$" for index in range(65))
        with self.assertRaises(RenderError) as raised:
            stub.render(f"# 标题\n\n{formulas}")
        self.assertEqual((raised.exception.code, raised.exception.stage), ("render_formula_limit", "select"))
        self.assertNotIn("queue_budget", stub.calls)  # 没排队、没起 worker

    def test_an_overlong_formula_skips_drawing(self):
        stub = StubbedRenderer(self)
        with self.assertRaises(RenderError) as raised:
            stub.render("# 标题\n\n$$" + "x+" * 1001 + "y$$")
        self.assertEqual(raised.exception.code, "render_formula_limit")
        self.assertNotIn("queue_budget", stub.calls)

    def test_ordinary_formulas_are_drawn(self):
        stub = StubbedRenderer(self)
        formulas = " ".join(f"$x_{{{index}}}$" for index in range(64))
        stub.render(f"# 标题\n\n{formulas}")
        self.assertIn("payload", stub.calls)


class FakeJina:
    def __init__(self, home, reader_engine=None):
        pass

    def close(self):
        pass


class FakeTools:
    def __init__(self, jina, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cost_items(self):
        return []


class FakeAgent:
    seen: dict = {}

    def __init__(self, bridge, tools, **kwargs):
        FakeAgent.seen = kwargs
        self.image_assets, self.favicon_assets, self.context, self.on_event = {}, {}, None, None

    def run(self, provider, model, context):
        self.context = {"messages": list(context["messages"])}
        return {"content": [{"type": "text", "text": ANSWER}], "stopReason": "stop"}

    def release(self):
        pass


def _failing_renderer(code: str):
    class FailingRenderer:
        made: list = []

        def __init__(self, timeout=45):
            FailingRenderer.made.append({"timeout": timeout})

        def render(self, text, **kwargs):
            FailingRenderer.made[-1]["kwargs"] = kwargs
            error = RenderError(code)
            error.stage = "draw"
            error.timings = {"queue_ms": 1.0, "prepare_ms": 2.0, "draw_ms": 3.0, "total_ms": 6.5}
            error.selection = {"selected_images": 0, "selected_icons": 1}
            raise error

        def close(self):
            pass

    return FailingRenderer


class AnswerFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _patch(self, renderer):
        home = tempfile.mkdtemp(prefix="hyw-test-")
        for patcher in (
            patch.object(api, "JinaClient", FakeJina),
            patch.object(api, "ToolRuntime", FakeTools),
            patch.object(api, "SearchAgent", FakeAgent),
            patch.object(api, "CardRenderer", renderer),
            patch.object(api, "Bridge", lambda *args, **kwargs: SimpleNamespace(home=Path(home))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_render_failures_return_the_finished_text(self):
        for code in ("render_timeout", "render_formula_limit", "render_failed"):
            with self.subTest(code=code):
                renderer = _failing_renderer(code)
                self._patch(renderer)
                events = []
                result = await api.answer("问题", on_event=events.append, provider="openai-compatible",
                                          model="m", api="chat", base_url="https://x.test/v1", api_key="k",
                                          render_timeout=12, render_max_images=16, render_max_edge=1024,
                                          max_rounds=7)
                self.assertEqual(result.kind, "text")
                self.assertEqual(result.render_error, code)
                self.assertIsNone(result.png)
                self.assertIn("完整答案正文", result.display_text)
                self.assertEqual(result.links, ("https://example.com/a",))
                self.assertEqual(result.diagnostics[0]["render_code"], code)
                self.assertEqual(result.diagnostics[0]["timings"]["draw_ms"], 3.0)
                self.assertEqual(renderer.made[-1]["timeout"], 12)
                self.assertEqual(renderer.made[-1]["kwargs"]["max_images"], 16)
                self.assertEqual(renderer.made[-1]["kwargs"]["max_image_edge"], 1024)
                self.assertEqual(FakeAgent.seen["max_rounds"], 7)
                error_event = next(event for event in events if event["type"] == "render_error")
                self.assertEqual((error_event["code"], error_event["stage"]), (code, "draw"))
                self.assertEqual(error_event["timings"]["queue_ms"], 1.0)
                self.assertTrue(any(event["type"] == "render_fallback" for event in events))

    async def test_cancellation_still_raises(self):
        self._patch(_failing_renderer("cancelled"))
        with self.assertRaises(RenderError):
            await api.answer("问题", provider="openai-compatible", model="m", api="chat",
                             base_url="https://x.test/v1", api_key="k")

    async def test_render_settings_are_validated(self):
        for kwargs in ({"render_timeout": 0}, {"render_timeout": math.inf}, {"render_max_images": -1},
                       {"render_max_edge": 0}, {"render_max_images": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await api.answer("问题", **kwargs)

    async def test_the_chat_gets_text_with_a_card_failure_note(self):
        from plugins.hyw import handlers

        self._patch(_failing_renderer("render_timeout"))
        session = AsyncMock()
        session.send = AsyncMock(return_value=[SimpleNamespace(id="m1")])
        config = HywConfig(api_key="cfg-SECRET-API-KEY", home=tempfile.mkdtemp(prefix="hyw-home-"))
        scope = ("qq", "b", "g", "c", "u")
        self.addCleanup(handlers.history_store.clear, scope)
        self.addCleanup(handlers.sources.clear_sender, handlers.channel_for(scope), scope[4])
        with patch.object(handlers, "logger") as log:
            await handlers.run_request(session, config, scope, "问题", [], [])
        sent = str(session.send.await_args_list[-1].args[0])
        self.assertIn("卡片生成失败", sent)
        self.assertIn("完整答案正文", sent)
        self.assertNotIn("请稍后重试", sent)
        logged = " ".join(str(call) for call in log.info.call_args_list + log.warning.call_args_list)
        self.assertIn("queue_ms=1.0", logged)
        self.assertIn("draw_ms=3.0", logged)
        self.assertIn("selected_icons=1", logged)
        self.assertNotIn("完整答案正文", logged)
        self.assertNotIn("cfg-SECRET-API-KEY", logged)


if __name__ == "__main__":
    sys.exit(unittest.main())
