"""Poller integration tests: fake api, real temporary database, recording send.

These cover the parts the pure detector tests cannot: the single write
transaction, outbox fan-out, crash replay and backlog back-pressure.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import threading
from time import perf_counter
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger

from otae_bot.infrastructure import loop_watchdog

from tests.test_core_logic import (
    _load_bili_new_module,
    _load_bili_subpackage,
    _load_module,
)


def _load_in_package(package: str, name: str):
    """Load one more module inside the synthetic package the loader created."""
    key = f"{package}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    return _load_module(key, f"plugins/bilibilibot/{name}.py")


def asyncio_test(fn):
    """Run one coroutine test on a fresh loop (this repo has no asyncio plugin)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture
def bili():
    poller = _load_bili_new_module("poller")
    package = poller.__package__
    return SimpleNamespace(
        poller=poller,
        notifier=_load_in_package(package, "notifier"),
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
        # The real transport, for the loop-stall regression test.
        api=_load_bili_subpackage(package, "api"),
    )


class FakeApi:
    """Records calls and replays canned observations."""

    def __init__(self, observations=None, cards=None, items=None, error=None):
        self.observations = observations or {}
        self.cards = cards or {}
        self.items = items or {}
        self.error = error
        self.batch_calls = []
        self.room_calls = []
        self.video_calls = []
        self.dynamic_calls = []

    async def batch_live_status(self, uids):
        self.batch_calls.append(list(uids))
        if self.error is not None:
            raise self.error
        return {uid: self.observations[uid] for uid in uids if uid in self.observations}

    async def live_observation(self, target):
        self.room_calls.append(target.uid)
        return self.observations.get(target.uid)

    async def latest_video(self, uid, *, deadline=None):
        self.video_calls.append(uid)
        return self.cards[uid]

    async def dynamic_items(self, uid, *, deadline=None):
        self.dynamic_calls.append(uid)
        return self.items.get(uid, [])


async def open_store(bili, tmp_path):
    store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    return store


def subscription(bili, kind, uid, subscriber_type="group", subscriber_id="900"):
    return bili.models.Subscription(kind, uid, subscriber_type, subscriber_id)


# --- live batch -------------------------------------------------------------


@asyncio_test
async def test_one_batch_request_covers_every_target(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        for uid in ("1", "2", "3"):
            await store.upsert_target(bili.models.TargetInfo("live", uid))
            await store.add_subscription("live", uid, "group", "900")
        api = FakeApi(
            observations={
                uid: bili.models.LiveObservation(uid, room_id="100", is_live=True)
                for uid in ("1", "2", "3")
            }
        )
        poller = bili.poller.Poller(api, store)
        await poller.tick_live()
        assert api.batch_calls == [["1", "2", "3"]]
        assert api.room_calls == []
        # One live_on per target, fanned out to the single subscriber.
        assert await store.outbox_count() == 3
    finally:
        await store.close()


@asyncio_test
async def test_missing_uid_is_not_treated_as_going_offline(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(
            bili.models.TargetInfo("live", "1", is_live=True, live_started_at=500, live_last_seen_at=900)
        )
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={})
        poller = bili.poller.Poller(api, store, clock=lambda: 1000)
        await poller.tick_live()
        assert await store.outbox_count() == 0
        target = await store.get_target("live", "1")
        # State untouched: no end notification, no last_seen advance.
        assert target.is_live
        assert (target.live_started_at, target.live_last_seen_at) == (500, 900)
    finally:
        await store.close()


@asyncio_test
async def test_batch_failure_falls_back_to_per_room_queries(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        for uid in ("1", "2"):
            await store.upsert_target(bili.models.TargetInfo("live", uid))
            await store.add_subscription("live", uid, "group", "900")
        api = FakeApi(
            observations={uid: bili.models.LiveObservation(uid, is_live=True) for uid in ("1", "2")},
            error=RuntimeError("batch down"),
        )
        poller = bili.poller.Poller(api, store)
        await poller.tick_live()
        assert sorted(api.room_calls) == ["1", "2"]
        assert await store.outbox_count() == 2
    finally:
        await store.close()


# --- outbox fan-out and ordering --------------------------------------------


@asyncio_test
async def test_event_is_written_once_per_recipient(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        await store.add_subscription("live", "1", "group", "901")
        await store.add_subscription("live", "1", "user", "7")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})
        poller = bili.poller.Poller(api, store)
        await poller.tick_live()
        rows = await store.outbox_rows()
        assert {(row.subscriber_type, row.subscriber_id) for row in rows} == {
            ("group", "900"),
            ("group", "901"),
            ("user", "7"),
        }
        # Same event_key for every recipient: the render cache depends on it.
        assert len({row.event_key for row in rows}) == 1
        assert all(row.card_type == "live_on" for row in rows)
    finally:
        await store.close()


@asyncio_test
async def test_repeat_ticks_do_not_duplicate_the_same_event(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})
        # A frozen clock makes the event_key identical across both rounds.
        poller = bili.poller.Poller(api, store, clock=lambda: 1000)
        await poller.tick_live()
        await poller.tick_live()
        assert await store.outbox_count() == 1
    finally:
        await store.close()


@asyncio_test
async def test_live_on_then_off_keeps_per_recipient_order(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})
        clock = [1000]
        poller = bili.poller.Poller(api, store, clock=lambda: clock[0])
        await poller.tick_live()
        clock[0] = 1100
        api.observations = {"1": bili.models.LiveObservation("1", is_live=False)}
        await poller.tick_live()
        due = await store.due_outbox(1100)
        # Only the head row of the recipient is due; live_on precedes live_off.
        assert [row.card_type for row in due] == ["live_on"]
        await store.outbox_done(due[0].id)
        assert [row.card_type for row in await store.due_outbox(1100)] == ["live_off"]
    finally:
        await store.close()


# --- crash replay -----------------------------------------------------------


@asyncio_test
async def test_crash_before_delivery_replays_every_recipient(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        await store.add_subscription("live", "1", "user", "7")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})
        await bili.poller.Poller(api, store).tick_live()
    finally:
        await store.close()

    # Reopen as a fresh process would: the pending notifications are still there.
    store = await open_store(bili, tmp_path)
    try:
        sent = []

        async def send(row, png):
            sent.append((row.subscriber_type, row.subscriber_id))

        notifier = bili.notifier.Notifier(
            store, render=lambda card: asyncio.sleep(0, result=b"png"), send=send
        )
        await notifier.start()
        await asyncio.sleep(0.1)
        await notifier.stop()
        assert sorted(sent) == [("group", "900"), ("user", "7")]
        assert await store.outbox_count() == 0
    finally:
        await store.close()


@asyncio_test
async def test_failed_transaction_writes_nothing(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        target = bili.models.TargetInfo("live", "1", is_live=True)
        card = bili.models.BiliCard("live_on", "标题", uid="1")
        event = bili.models.BiliEvent("live", "1", card)

        def explode(_event):
            raise RuntimeError("outbox unavailable")

        with pytest.raises(RuntimeError, match="outbox unavailable"):
            await store.apply_poll_result(
                [target],
                [bili.models.SeenItem("live", "1", "x", 1)],
                [event],
                1000,
                expand=lambda _event: [("group", "900")],
                event_key=explode,
            )
        assert await store.get_target("live", "1") is None
        assert await store.seen_ids("live", "1", ["x"]) == set()
        assert await store.outbox_count() == 0
    finally:
        await store.close()


# --- backlog back-pressure --------------------------------------------------


@asyncio_test
async def test_backlog_above_the_watermark_stops_the_poll(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})

        class BlockingNotifier:
            def __init__(self):
                self.woken = 0
                self.waits = 0

            def wake(self):
                self.woken += 1

            async def wait_backlog_below(self, limit=None):
                self.waits += 1
                raise AssertionError("poll must wait here")

        notifier = BlockingNotifier()
        poller = bili.poller.Poller(api, store, notifier)
        with pytest.raises(AssertionError, match="poll must wait here"):
            await poller.tick_live()
        # The wait happens before any request is issued.
        assert api.batch_calls == []
        assert notifier.waits == 1
    finally:
        await store.close()


@asyncio_test
async def test_notifier_is_woken_after_a_committed_event(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=True)})

        class RecordingNotifier:
            def __init__(self):
                self.woken = 0

            def wake(self):
                self.woken += 1

            async def wait_backlog_below(self, limit=None):
                return None

        notifier = RecordingNotifier()
        await bili.poller.Poller(api, store, notifier).tick_live()
        assert notifier.woken == 1
    finally:
        await store.close()


@asyncio_test
async def test_no_wake_when_nothing_changed(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        api = FakeApi(observations={"1": bili.models.LiveObservation("1", is_live=False)})

        class RecordingNotifier:
            def __init__(self):
                self.woken = 0

            def wake(self):
                self.woken += 1

            async def wait_backlog_below(self, limit=None):
                return None

        notifier = RecordingNotifier()
        await bili.poller.Poller(api, store, notifier).tick_live()
        assert notifier.woken == 0
    finally:
        await store.close()


# --- overlap and failure isolation -----------------------------------------


@asyncio_test
async def test_same_kind_tick_is_singleflighted(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "1"))
        await store.add_subscription("live", "1", "group", "900")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class SlowApi(FakeApi):
            async def batch_live_status(self, uids):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return {}

        api = SlowApi()
        poller = bili.poller.Poller(api, store)
        first = asyncio.create_task(poller.tick_live())
        await started.wait()
        await poller.tick_live()
        assert calls == 1
        release.set()
        await first
    finally:
        await store.close()


@asyncio_test
async def test_one_failing_target_does_not_block_the_others(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        for uid in ("1", "2", "3"):
            await store.upsert_target(bili.models.TargetInfo("video", uid))
            await store.add_subscription("video", uid, "group", "900")
        cards = {
            uid: bili.models.BiliCard("video", "标题", uid=uid, item_id=f"BV{uid}", published_at=1000)
            for uid in ("1", "2", "3")
        }

        class FlakyApi(FakeApi):
            async def latest_video(self, uid, *, deadline=None):
                if uid == "2":
                    raise RuntimeError("boom")
                return cards[uid]

        api = FlakyApi()
        poller = bili.poller.Poller(api, store, intervals={"video": 0})
        await poller.tick_video()
        # Two targets still produced notifications.
        assert await store.outbox_count() == 2
        assert {row.uid for row in await store.outbox_rows()} == {"1", "3"}
    finally:
        await store.close()


@asyncio_test
async def test_target_timeout_is_enforced(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("video", "1"))
        await store.add_subscription("video", "1", "group", "900")

        class HangingApi(FakeApi):
            async def latest_video(self, uid, *, deadline=None):
                await asyncio.sleep(5)
                raise AssertionError("should have been cancelled")

        poller = bili.poller.Poller(
            HangingApi(), store, target_timeout=0.05, intervals={"video": 0}
        )
        await asyncio.wait_for(poller.tick_video(), timeout=2)
        assert await store.outbox_count() == 0
    finally:
        await store.close()


# --- scheduling -------------------------------------------------------------


@asyncio_test
async def test_video_and_dynamic_targets_are_staggered_and_backed_off(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("video", "1"))
        await store.add_subscription("video", "1", "group", "900")
        api = FakeApi(cards={"1": bili.models.BiliCard("video", "标题", uid="1", item_id="BV1", published_at=1)})
        poller = bili.poller.Poller(api, store, intervals={"video": 60}, clock=lambda: 1000)
        # The first round is staggered by hash(uid) % interval, so a tick may be
        # a no-op; driving the clock forward must eventually run the target.
        for now in range(1000, 1000 + 61):
            poller.clock = lambda now=now: now
            await poller.tick_video()
        assert api.video_calls == ["1"]
    finally:
        await store.close()


@asyncio_test
async def test_failures_grow_the_interval_and_success_resets_it(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("video", "1"))
        await store.add_subscription("video", "1", "group", "900")

        class FailingApi(FakeApi):
            async def latest_video(self, uid, *, deadline=None):
                raise RuntimeError("boom")

        poller = bili.poller.Poller(FailingApi(), store, intervals={"video": 60})
        poller._is_due("video", "1", 0)
        poller._fail("video", "1", 0)
        assert poller._next_due[("video", "1")] == min(60 * 2, 30 * 60)
        poller._fail("video", "1", 0)
        assert poller._next_due[("video", "1")] == min(60 * 4, 30 * 60)
        poller._succeed("video", "1", 0)
        assert poller._next_due[("video", "1")] == 60
        assert poller._failures[("video", "1")] == 0
    finally:
        await store.close()


@asyncio_test
async def test_backoff_is_capped(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        poller = bili.poller.Poller(FakeApi(), store, intervals={"video": 60})
        for _ in range(20):
            poller._fail("video", "1", 0)
        assert poller._next_due[("video", "1")] == bili.poller.MAX_BACKOFF_SECONDS
    finally:
        await store.close()


# --- event-loop stall regression (plan section 7) ---------------------------


def _stall_api(bili, targets, calls, clients):
    """A real BiliApi whose only transport is a slow MockTransport.

    Every request sleeps inside the transport, which is exactly what a real
    socket read does: it yields to the loop. A regression that blocks the loop
    (a synchronous database call) then shows up as a watchdog warning.

    `clients` records which `BiliSession` client issued each request: the
    plan's other named regression, "a new HTTP client per request", costs almost
    nothing under MockTransport, so it needs its own assertion rather than the
    stall budget.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # Hold a strong reference, so a rebuilt client cannot hide behind memory reuse.
        clients.append(api.session._http_client)
        await asyncio.sleep(0.01)
        if request.url.path.endswith("/x/web-interface/nav"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                        }
                    },
                },
            )
        if request.url.path.endswith("/get_status_info_by_uids"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        target.uid: {
                            "uid": int(target.uid),
                            "room_id": 100 + index,
                            "uname": target.name,
                            "face": "face.png",
                            "title": "标题",
                            "live_status": 1,
                            "live_time": 1000,
                        }
                        for index, target in enumerate(targets)
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    api = bili.api.BiliApi(transport=httpx.MockTransport(handler), min_interval=0)
    # Pre-seed the WBI keys so the round does not spend a request on /nav.
    api.session.img_key = "a" * 32
    api.session.sub_key = "b" * 32
    api.session._wbi_updated_at = 9999999999
    return api


WATCHDOG_BUDGET_SECONDS = 0.05


def _record_sync_threads(store, loop_thread: int, off_loop: list[bool]) -> None:
    """Note, per synchronous store body, whether it ran off the event-loop thread.

    The store funnels every statement through `_run`, which hands the `_sync`
    body to a one-thread executor. Wrapping the bodies (rather than `_run`)
    keeps this sensitive to a regression that swaps the executor for a direct
    call, because the mutation replaces `_run` and would bypass a wrapper there.
    """
    for name in [item for item in dir(type(store)) if item.endswith("_sync")]:
        original = getattr(store, name)

        def wrapper(*args, __original=original, **kwargs):
            off_loop.append(threading.get_ident() != loop_thread)
            return __original(*args, **kwargs)

        setattr(store, name, wrapper)


def _capture_watchdog(records: list[str]) -> int:
    """Route loguru output into a list so the watchdog's own verdict is readable."""

    def sink(message):
        records.append(message.record["message"])

    return logger.add(sink, level="DEBUG", format="{message}")


async def _run_tick_under_watchdog(*, tick, budget: float):
    """Run one tick while the shared watchdog coroutine watches the loop.

    Returns the watchdog's own log lines. The plan's acceptance rule is exactly
    "one tick must not stall the loop for more than 50 ms, judged by the
    watchdog log", so the assertion reads the watchdog's verdict rather than a
    second hand-rolled timer.
    """
    interval = 0.005
    records: list[str] = []
    sink_id = _capture_watchdog(records)
    task = asyncio.create_task(
        loop_watchdog.watch_loop(
            interval_seconds=interval, warn_seconds=budget, error_seconds=1.0
        )
    )
    try:
        await asyncio.sleep(0)  # let the watchdog take its first sample
        await asyncio.wait_for(tick(), timeout=60)
        # Give the watchdog a few more samples: a stall that ends together with
        # the tick is only reported on the sample after it, and cancelling here
        # would silently drop it.
        await asyncio.sleep(interval * 4)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.remove(sink_id)
    return [line for line in records if "[watchdog]" in line]


@asyncio_test
async def test_one_live_tick_never_stalls_the_event_loop(bili, tmp_path):
    """100 targets over a real (slow) transport must not block the loop.

    This is the regression guard from the plan's test strategy: every request
    sleeps 10 ms inside the transport, the watchdog coroutine runs alongside the
    tick, and no watchdog warning may be emitted. It fires as soon as anyone
    builds an HTTP client per request or touches sqlite from the loop thread
    instead of the store's worker thread.
    """
    store = await open_store(bili, tmp_path)
    calls: list[str] = []
    clients: list[object] = []
    try:
        targets = []
        for index in range(100):
            target = bili.models.TargetInfo("live", str(1000 + index), name=f"UP {index}")
            await store.upsert_target(target)
            await store.add_subscription("live", target.uid, "group", "900")
            targets.append(target)

        api = _stall_api(bili, targets, calls, clients)
        poller = bili.poller.Poller(api, store, concurrency=8, clock=lambda: 5000)

        # Watch where the synchronous store bodies actually run. At this scale a
        # direct sqlite call is only a few milliseconds, well inside the 50 ms
        # budget, so the budget alone cannot prove the database stayed off the
        # loop thread; this does.
        off_loop: list[bool] = []
        _record_sync_threads(store, threading.get_ident(), off_loop)

        started = perf_counter()
        warnings = await _run_tick_under_watchdog(
            tick=poller.tick_live, budget=WATCHDOG_BUDGET_SECONDS
        )
        elapsed = perf_counter() - started

        # The round really did go out over the transport: one batch call per
        # chunk of 50 uids (plan section 5.3), not one call per target.
        batch_urls = [url for url in calls if "get_status_info_by_uids" in url]
        assert len(batch_urls) == 2
        assert not [url for url in calls if "/Room/get_info" in url]
        assert await store.outbox_count() == 100
        # The transport alone costs 2 x 10 ms; the round is far from serial.
        assert elapsed < 1.0, f"tick took {elapsed:.2f}s"
        # Plan section 7: no stall over the budget during one tick.
        assert warnings == [], warnings
        # Plan section 7's other named regressions, asserted directly because at
        # this scale neither is expensive enough to trip the 50 ms budget.
        assert len({id(client) for client in clients}) == 1, (
            f"{len({id(client) for client in clients})} HTTP clients for one round"
        )
        assert off_loop and all(off_loop), (
            f"{off_loop.count(False)} of {len(off_loop)} store calls ran on the loop thread"
        )
    finally:
        await store.close()


@asyncio_test
async def test_the_stall_watchdog_notices_a_blocking_call(bili, tmp_path):
    """Sanity check: the watchdog really does report a blocked loop.

    Without this, a watchdog that silently never warns would make the regression
    test above pass no matter what the poller did.
    """
    async def blocked_tick() -> None:
        # A synchronous call on the loop thread, as a sync sqlite access is.
        deadline = perf_counter() + 0.15
        while perf_counter() < deadline:
            pass

    warnings = await _run_tick_under_watchdog(
        tick=blocked_tick, budget=WATCHDOG_BUDGET_SECONDS
    )
    assert warnings, "watchdog failed to report a 150 ms block"


# --- dynamic path -----------------------------------------------------------


@asyncio_test
async def test_dynamic_tick_writes_seen_marks_and_events(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(bili.models.TargetInfo("dynamic", "1"))
        await store.add_subscription("dynamic", "1", "group", "900")
        items = [
            {
                "id_str": "100",
                "modules": {
                    "module_author": {"name": "作者", "face": "f.png", "pub_ts": 1000},
                    "module_dynamic": {"desc": {"text": "第一条"}},
                },
            }
        ]
        api = FakeApi(items={"1": items})
        poller = bili.poller.Poller(api, store, intervals={"dynamic": 0})
        await poller.tick_dynamic()
        assert await store.outbox_count() == 1
        assert await store.seen_ids("dynamic", "1", ["100"]) == {"100"}
        # The second round sees the same item as already handled.
        await poller.tick_dynamic()
        assert await store.outbox_count() == 1
    finally:
        await store.close()
