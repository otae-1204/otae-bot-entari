from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import pytest
from loguru import logger

from tests.test_core_logic import _load_bili_new_module


CLOCK_START = 1_800_000_000
HOUR = 3600

OUTBOX_METHODS = (
    "due_outbox",
    "outbox_done",
    "outbox_retry",
    "outbox_drop",
    "outbox_expire",
    "outbox_count",
)


@dataclass(slots=True)
class OutboxRow:
    """Mirror of the row shape agreed with the store (refactor plan 5.3)."""

    id: int
    event_key: str
    kind: str
    uid: str
    card_type: str
    subscriber_type: str
    subscriber_id: str
    card_json: str
    created_at: int
    attempts: int
    next_attempt_at: int
    last_error: str = ""


class FakeClock:
    """Injected clock; tests advance it instead of patching time.time."""

    def __init__(self, start: int = CLOCK_START) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStore:
    """In-memory stand-in for the outbox half of BiliStore.

    Only the six async methods the notifier is allowed to call are provided.
    Rows are kept in the order the poller would have written them, and
    due_outbox keeps the documented rule: for every recipient only the lowest
    id is a candidate, and a recipient whose head record is still waiting for
    its retry does not hand out any later record.
    """

    def __init__(
        self, rows: dict[int, dict[str, Any]] | None = None, next_id: int = 1
    ) -> None:
        self.rows: dict[int, dict[str, Any]] = dict(rows or {})
        self._next_id = next_id

    # -- test helpers ------------------------------------------------------

    def add(
        self,
        card: Any,
        *,
        event_key: str | None = None,
        subscriber_type: str = "group",
        subscriber_id: str = "900",
        kind: str = "live",
        uid: str = "123",
        created_at: int | None = None,
        attempts: int = 0,
        next_attempt_at: int | None = None,
        last_error: str = "",
    ) -> int:
        row_id = self._next_id
        self._next_id += 1
        created = CLOCK_START if created_at is None else int(created_at)
        self.rows[row_id] = {
            "id": row_id,
            "event_key": event_key or f"{kind}:{uid}:{card.card_type}",
            "kind": kind,
            "uid": uid,
            "card_type": card.card_type,
            "subscriber_type": subscriber_type,
            "subscriber_id": subscriber_id,
            "card_json": json.dumps(asdict(card), ensure_ascii=False),
            "created_at": created,
            "attempts": int(attempts),
            "next_attempt_at": created
            if next_attempt_at is None
            else int(next_attempt_at),
            "last_error": last_error,
        }
        return row_id

    def snapshot(self, row_id: int) -> dict[str, Any] | None:
        row = self.rows.get(row_id)
        return dict(row) if row is not None else None

    def dump(self) -> tuple[dict[int, dict[str, Any]], int]:
        return ({key: dict(value) for key, value in self.rows.items()}, self._next_id)

    @classmethod
    def reload(cls, dumped: tuple[dict[int, dict[str, Any]], int]) -> FakeStore:
        """Simulates closing the database and opening it again."""
        rows, next_id = dumped
        return cls(rows, next_id)

    # -- BiliStore outbox API ---------------------------------------------

    async def due_outbox(self, now: int, limit: int) -> list[OutboxRow]:
        """Same rule as the real store: at most one record per recipient.

        The head record is the lowest id of that recipient, whether or not it
        is due yet; a recipient whose head is still backing off yields nothing.
        """
        heads: dict[tuple[str, str], dict[str, Any]] = {}
        for row in sorted(self.rows.values(), key=lambda item: item["id"]):
            heads.setdefault((row["subscriber_type"], row["subscriber_id"]), row)
        due = [row for row in heads.values() if row["next_attempt_at"] <= now]
        due.sort(key=lambda item: item["id"])
        return [OutboxRow(**row) for row in due[:limit]]

    async def outbox_done(self, row_id: int) -> None:
        self.rows.pop(row_id, None)

    async def outbox_retry(
        self, row_id: int, *, attempts: int, next_attempt_at: int, error: str
    ) -> None:
        row = self.rows[row_id]
        row["attempts"] = int(attempts)
        row["next_attempt_at"] = int(next_attempt_at)
        row["last_error"] = error

    async def outbox_drop(self, row_id: int) -> None:
        self.rows.pop(row_id, None)

    async def outbox_expire(self, now: int, max_age: dict[str, int]) -> int:
        expired = [
            row
            for row in self.rows.values()
            if now - row["created_at"] >= int(max_age.get(row["card_type"], 24 * HOUR))
        ]
        for row in expired:
            del self.rows[row["id"]]
        return len(expired)

    async def outbox_count(self) -> int:
        return len(self.rows)


def _models():
    return _load_bili_new_module("models")


def _notifier_module():
    return _load_bili_new_module("notifier")


def _card(card_type: str = "live_on", **kwargs: Any):
    models = _models()
    payload = {"title": card_type, "uid": "123", "url": "https://live.bilibili.com/1"}
    payload.update(kwargs)
    return models.BiliCard(card_type, **payload)


class Recorder:
    """Injectable send/render pair that records what the notifier did."""

    def __init__(
        self,
        *,
        fail: Callable[[Any], bool] | None = None,
        delay: float = 0.0,
        render_delay: float = 0.0,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.renders: list[str] = []
        self.attempts: list[tuple[int, str]] = []
        self.inflight = 0
        self.max_inflight = 0
        self._fail = fail
        self._delay = delay
        self._render_delay = render_delay

    async def render(self, card: Any) -> bytes:
        self.renders.append(card.card_type)
        if self._render_delay:
            await asyncio.sleep(self._render_delay)
        return f"png:{card.card_type}".encode()

    async def send(self, row: Any, png: bytes) -> None:
        self.attempts.append((row.id, row.subscriber_id))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._fail is not None and self._fail(row):
                raise RuntimeError(f"send failed for {row.subscriber_id}")
            self.sent.append((row.id, row.subscriber_id))
        finally:
            self.inflight -= 1


def _build(store: FakeStore, recorder: Recorder, clock: FakeClock, **kwargs: Any):
    module = _notifier_module()
    return module.Notifier(
        store, render=recorder.render, send=recorder.send, clock=clock, **kwargs
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met in time")


class LogCapture:
    """Collects loguru messages so retry/drop logging can be asserted."""

    def __init__(self, level: str = "DEBUG") -> None:
        self.records: list[dict[str, Any]] = []

        def sink(message: Any) -> None:
            record = message.record
            self.records.append(
                {"level": record["level"].name, "message": record["message"]}
            )

        self._sink_id = logger.add(sink, level=level, format="{message}")

    def close(self) -> None:
        logger.remove(self._sink_id)

    def messages(self, level: str | None = None) -> list[str]:
        return [
            item["message"]
            for item in self.records
            if level is None or item["level"] == level
        ]


@pytest.fixture
def logs():
    capture = LogCapture()
    try:
        yield capture
    finally:
        capture.close()


# ---------------------------------------------------------------------------
# retry / give up
# ---------------------------------------------------------------------------


def test_retry_backoff_grows_to_the_cap_then_drops_the_record(logs):
    async def scenario() -> None:
        clock = FakeClock()
        store = FakeStore()
        row_id = store.add(_card("live_on"), subscriber_id="900")
        recorder = Recorder(fail=lambda row: True)
        notifier = _build(store, recorder, clock, concurrency=1)
        await notifier.start()
        try:
            await _wait_until(
                lambda: (store.snapshot(row_id) or {}).get("attempts") == 1
            )
            delays = []
            for expected_attempts in range(2, 8):
                row = store.snapshot(row_id)
                assert row is not None
                delays.append(row["next_attempt_at"] - int(clock()))
                assert row["last_error"].startswith("RuntimeError")
                clock.advance(delays[-1] + 1)
                notifier.wake()
                await _wait_until(
                    lambda: (store.snapshot(row_id) or {}).get("attempts")
                    == expected_attempts
                )
            row = store.snapshot(row_id)
            assert row is not None
            delays.append(row["next_attempt_at"] - int(clock()))
            clock.advance(delays[-1] + 1)
            notifier.wake()
            await _wait_until(lambda: store.snapshot(row_id) is None)
        finally:
            await notifier.stop()

        # 30, 60, 120, 240, 480 and then the 600 s cap.
        assert delays == [30, 60, 120, 240, 480, 600, 600]
        assert store.rows == {}
        assert len(recorder.attempts) == 8
        assert recorder.sent == []
        dropped = [
            text for text in logs.messages("ERROR") if "dropping notification" in text
        ]
        assert len(dropped) == 1
        assert "group:900" in dropped[0]
        assert "live:123:live_on" in dropped[0]

    asyncio.run(scenario())


def test_bot_unavailable_does_not_consume_a_retry(logs):
    async def scenario() -> None:
        module = _notifier_module()
        clock = FakeClock()
        store = FakeStore()
        row_id = store.add(_card("live_on"), subscriber_id="900")
        attempts_seen: list[int] = []

        async def send(row: Any, _png: bytes) -> None:
            attempts_seen.append(row.attempts)
            raise module.SenderUnavailable("bot is offline")

        notifier = module.Notifier(
            store, render=Recorder().render, send=send, clock=clock, concurrency=1
        )
        await notifier.start()
        try:
            # Three rounds in a row: the record is still there, still at
            # attempts = 0, and only postponed by the fixed 10 s delay.
            for round_index in range(3):
                await _wait_until(lambda: len(attempts_seen) >= round_index + 1)
                row = store.snapshot(row_id)
                assert row is not None
                assert row["attempts"] == 0
                assert row["next_attempt_at"] == int(clock()) + 10
                if round_index < 2:
                    clock.advance(11)
                    notifier.wake()
                    await asyncio.sleep(0.05)
        finally:
            await notifier.stop()

        assert attempts_seen == [0, 0, 0]
        row = store.snapshot(row_id)
        assert row is not None
        assert row["attempts"] == 0
        assert logs.messages("DEBUG"), (
            "expected a debug log while the bot is unavailable"
        )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ordering inside one recipient
# ---------------------------------------------------------------------------


def test_same_recipient_keeps_order_while_the_head_record_retries():
    async def scenario() -> None:
        clock = FakeClock()
        store = FakeStore()
        live_on = store.add(
            _card("live_on"), event_key="live:123:live_on:1", subscriber_id="900"
        )
        live_off = store.add(
            _card("live_off"), event_key="live:123:live_off:2", subscriber_id="900"
        )
        failures = {"left": 2}

        def should_fail(row: Any) -> bool:
            if row.id != live_on or failures["left"] == 0:
                return False
            failures["left"] -= 1
            return True

        recorder = Recorder(fail=should_fail)
        notifier = _build(store, recorder, clock, concurrency=2)
        await notifier.start()
        try:
            await _wait_until(lambda: len(recorder.attempts) == 1)
            clock.advance(31)
            notifier.wake()
            await _wait_until(lambda: len(recorder.attempts) == 2)
            # The head record is still retrying: the later record must wait.
            assert [row_id for row_id, _ in recorder.attempts] == [live_on, live_on]
            assert recorder.sent == []
            clock.advance(61)
            notifier.wake()
            await _wait_until(lambda: len(recorder.sent) == 2)
        finally:
            await notifier.stop()

        assert recorder.sent == [(live_on, "900"), (live_off, "900")]
        assert recorder.attempts == [
            (live_on, "900"),
            (live_on, "900"),
            (live_on, "900"),
            (live_off, "900"),
        ]
        assert store.rows == {}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------


def test_expired_live_notification_is_dropped_but_a_fresh_video_is_sent(logs):
    async def scenario() -> None:
        clock = FakeClock()
        store = FakeStore()
        now = int(clock())
        stale = store.add(
            _card("live_on"),
            event_key="live:123:live_on:stale",
            subscriber_id="900",
            created_at=now - 2 * HOUR - 1,
        )
        video = store.add(
            _card("video", url="https://www.bilibili.com/video/BV1extE6LEKB"),
            event_key="video:123:BV1extE6LEKB",
            subscriber_id="901",
            kind="video",
            created_at=now - 23 * HOUR,
        )
        recorder = Recorder()
        notifier = _build(store, recorder, clock)
        await notifier.start()
        try:
            await _wait_until(lambda: store.snapshot(video) is None)
        finally:
            await notifier.stop()

        assert store.snapshot(stale) is None
        assert store.rows == {}
        assert [row_id for row_id, _ in recorder.sent] == [video]
        expired_logs = [text for text in logs.messages("WARNING") if "expired" in text]
        assert expired_logs, "expected a warning about the expired record"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# rendering cache and concurrency
# ---------------------------------------------------------------------------


def test_one_event_rendered_once_for_three_recipients():
    async def scenario() -> None:
        clock = FakeClock()
        store = FakeStore()
        event_key = "live:123:live_on:1"
        for recipient in ("900", "901", "902"):
            store.add(_card("live_on"), event_key=event_key, subscriber_id=recipient)
        # Rendering is slow and all three records are due at once: the event
        # must still be rendered exactly once.
        recorder = Recorder(render_delay=0.02)
        notifier = _build(store, recorder, clock, concurrency=3)
        await notifier.start()
        try:
            await _wait_until(lambda: store.rows == {})
        finally:
            await notifier.stop()

        assert sorted(recipient for _, recipient in recorder.sent) == [
            "900",
            "901",
            "902",
        ]
        assert recorder.renders == ["live_on"]

    asyncio.run(scenario())


def test_sends_to_different_recipients_are_bounded_by_concurrency():
    async def scenario() -> None:
        clock = FakeClock()
        store = FakeStore()
        for recipient in ("900", "901", "902", "903"):
            store.add(
                _card("live_on"),
                event_key=f"live:123:live_on:{recipient}",
                subscriber_id=recipient,
            )
        recorder = Recorder(delay=0.02)
        notifier = _build(store, recorder, clock, concurrency=2)
        await notifier.start()
        try:
            await _wait_until(lambda: store.rows == {})
        finally:
            await notifier.stop()

        assert len(recorder.sent) == 4
        # Two at a time: the bound is respected and actually used.
        assert recorder.max_inflight == 2

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# backlog
# ---------------------------------------------------------------------------


def test_backlog_wait_blocks_until_the_outbox_drops_below_the_limit(logs):
    async def scenario() -> None:
        module = _notifier_module()
        clock = FakeClock()
        store = FakeStore()
        for recipient in ("900", "901", "902"):
            store.add(
                _card("live_on"),
                event_key=f"live:123:live_on:{recipient}",
                subscriber_id=recipient,
            )

        async def send(_row: Any, _png: bytes) -> None:
            raise module.SenderUnavailable("bot is offline")

        notifier = module.Notifier(
            store, render=Recorder().render, send=send, clock=clock, high_watermark=3
        )
        await notifier.start()
        waiter = asyncio.create_task(notifier.wait_backlog_below())
        try:
            await asyncio.sleep(0.05)
            assert not waiter.done()
            assert await store.outbox_count() == 3
            # A record expires or is dropped elsewhere; the notifier notices
            # after its next batch and releases the waiting poller.
            del store.rows[min(store.rows)]
            notifier.wake()
            await asyncio.wait_for(waiter, timeout=10)
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            await notifier.stop()

        assert await store.outbox_count() == 2
        messages = logs.messages("INFO")
        assert any("pausing the poll" in text for text in messages)
        assert any("resuming the poll" in text for text in messages)

    asyncio.run(scenario())


def test_backlog_wait_returns_immediately_when_the_outbox_is_small():
    async def scenario() -> None:
        store = FakeStore()
        store.add(_card("live_on"), subscriber_id="900")
        recorder = Recorder()
        notifier = _build(store, recorder, FakeClock(), high_watermark=5)
        await asyncio.wait_for(notifier.wait_backlog_below(), timeout=1)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------


def test_records_left_behind_are_resent_once_per_recipient_after_a_restart():
    async def scenario() -> None:
        clock = FakeClock()
        first = FakeStore()
        first.add(_card("live_on"), event_key="live:123:live_on:1", subscriber_id="900")
        first.add(_card("live_on"), event_key="live:123:live_on:1", subscriber_id="901")
        first.add(
            _card("live_off"), event_key="live:123:live_off:2", subscriber_id="900"
        )
        # The process dies before the notifier ever starts.
        reopened = FakeStore.reload(first.dump())

        recorder = Recorder()
        notifier = _build(reopened, recorder, clock)
        await notifier.start()
        try:
            await _wait_until(lambda: reopened.rows == {})
        finally:
            await notifier.stop()

        assert recorder.sent == [(1, "900"), (2, "901"), (3, "900")]
        assert recorder.renders == ["live_on", "live_off"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# integration with the real store (once the store side of phase 4 has landed)
# ---------------------------------------------------------------------------


def _load_real_store():
    """The real BiliStore, or None while its outbox API is still landing."""
    module = _load_bili_new_module("store")
    if not all(hasattr(module.BiliStore, name) for name in OUTBOX_METHODS):
        return None
    return module


async def _maybe_await(value: Any) -> Any:
    """Phase 4 keeps the store synchronous, phase 5 makes it async."""
    return await value if inspect.isawaitable(value) else value


async def _commit_event(
    store: Any, event: Any, created_at: int, recipients: tuple[str, ...]
) -> int:
    """One poller transaction: the event becomes one outbox row per recipient."""
    return await store.apply_poll_result(
        [],
        [],
        [event],
        created_at,
        expand=lambda _event: [("group", recipient) for recipient in recipients],
        event_key=lambda item: f"{item.kind}:{item.uid}:{item.card.card_type}",
    )


def test_outbox_left_in_a_real_database_is_delivered_after_a_restart(tmp_path: Path):
    module = _load_real_store()
    if module is None:  # pragma: no cover - the store side lands in parallel
        pytest.skip("BiliStore has no outbox API yet")

    db_path = tmp_path / "bilibili.db"
    legacy_path = tmp_path / "legacy.db"
    models = _models()
    event = models.BiliEvent(
        kind="live",
        uid="123",
        card=models.BiliCard(
            "live_on", "已开播", uid="123", url="https://live.bilibili.com/1"
        ),
    )

    async def scenario() -> None:
        # The poller commits the event, then the process dies before sending it.
        first = module.BiliStore(db_path, legacy_path)
        await first.open()
        try:
            assert await _commit_event(first, event, CLOCK_START, ("900", "901")) == 2
            assert await first.outbox_count() == 2
        finally:
            await first.close()

        reopened = module.BiliStore(db_path, legacy_path)
        await reopened.open()
        recorder = Recorder()
        notifier = _notifier_module().Notifier(
            reopened, render=recorder.render, send=recorder.send, clock=FakeClock()
        )
        await notifier.start()
        try:
            await _wait_until(lambda: len(recorder.sent) == 2)
            assert await reopened.outbox_count() == 0
        finally:
            await notifier.stop()
            await reopened.close()
        # Exactly one delivery per recipient, and one render for the event.
        assert sorted(recipient for _, recipient in recorder.sent) == ["900", "901"]
        assert recorder.renders == ["live_on"]

    asyncio.run(scenario())


def test_real_store_expiry_drops_a_stale_live_notification(tmp_path: Path):
    module = _load_real_store()
    if module is None:  # pragma: no cover - the store side lands in parallel
        pytest.skip("BiliStore has no outbox API yet")

    db_path = tmp_path / "bilibili.db"
    legacy_path = tmp_path / "legacy.db"
    models = _models()
    clock = FakeClock()
    stale = models.BiliEvent(
        kind="live", uid="123", card=models.BiliCard("live_on", "已开播", uid="123")
    )
    video = models.BiliEvent(
        kind="video", uid="123", card=models.BiliCard("video", "新视频", uid="123")
    )

    async def scenario() -> None:
        store = module.BiliStore(db_path, legacy_path)
        await store.open()
        try:
            await _commit_event(store, stale, int(clock()) - 3 * HOUR, ("900",))
            await _commit_event(store, video, int(clock()) - 23 * HOUR, ("901",))
            recorder = Recorder()
            notifier = _notifier_module().Notifier(
                store, render=recorder.render, send=recorder.send, clock=clock
            )
            await notifier.start()
            try:
                await _wait_until(lambda: len(recorder.sent) == 1)
            finally:
                await notifier.stop()
            assert [recipient for _, recipient in recorder.sent] == ["901"]
            assert await store.outbox_count() == 0
        finally:
            await store.close()

    asyncio.run(scenario())
