"""Outbox consumer for bilibilibot notifications.

The poller writes one outbox row per recipient inside the same transaction as
the state change (refactor plan, section 5.3). This module only drains that
table: it renders each event once, sends it per recipient and retries with
backoff. Delivery is "at least once": unsent rows stay in the database and are
picked up again after a restart.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from .models import BiliCard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .store import BiliStore


# Live notifications are worthless once the stream is long over; feed items
# (videos and dynamics) can wait much longer.
LIVE_NOTIFICATION_MAX_AGE = 2 * 60 * 60
FEED_NOTIFICATION_MAX_AGE = 24 * 60 * 60
OUTBOX_MAX_AGE: dict[str, int] = {
    "live_on": LIVE_NOTIFICATION_MAX_AGE,
    "live_off": LIVE_NOTIFICATION_MAX_AGE,
    "live_idle": LIVE_NOTIFICATION_MAX_AGE,
    "video": FEED_NOTIFICATION_MAX_AGE,
    "dynamic": FEED_NOTIFICATION_MAX_AGE,
}

MAX_ATTEMPTS = 8
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 600
SENDER_UNAVAILABLE_DELAY_SECONDS = 10
DISPATCH_BATCH_LIMIT = 50
IDLE_INTERVAL_SECONDS = 5.0
BACKLOG_RECHECK_SECONDS = 5.0
RENDER_CACHE_SIZE = 32
MAX_ERROR_CHARS = 500


class SenderUnavailable(Exception):
    """The bot cannot send anything right now (not connected).

    Such a failure does not consume a retry attempt: the whole bot is offline,
    so every queued record would otherwise burn its attempts at once.
    """


class _RenderCache:
    """Small LRU cache keyed by the outbox event_key.

    One event usually fans out to several recipients; the card image only has
    to be rendered once. The cache is lost on restart, which is fine.
    """

    def __init__(self, capacity: int = RENDER_CACHE_SIZE) -> None:
        self.capacity = max(1, int(capacity))
        self._items: OrderedDict[str, bytes] = OrderedDict()

    def get(self, key: str) -> bytes | None:
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: str, value: bytes) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


async def _resolve(value: Any) -> Any:
    """Accept both store generations.

    Phase 4 adds the outbox methods to the still synchronous store, phase 5
    turns them into coroutine functions. Awaiting whatever comes back keeps
    the notifier unchanged across that switch.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def _card_from_row(row: Any) -> BiliCard:
    """Recover the card of an outbox row.

    The store normally deserializes card_json for us; the raw column is
    accepted as a fallback so the notifier does not depend on that detail.
    """
    data = getattr(row, "card_json", None)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if isinstance(data, str) and data:
        return BiliCard(**json.loads(data))
    card = getattr(row, "card", None)
    if callable(card):  # OutboxRow exposes a card() helper.
        card = card()
    if isinstance(card, BiliCard):
        return card
    raise ValueError(f"outbox row {getattr(row, 'id', '?')} carries no card payload")


def _head_rows(rows: list[Any]) -> list[Any]:
    """Keep only the lowest id per recipient.

    A chat must keep the order in which its events were produced (a live_on
    always precedes its live_off), and while the head record is waiting for a
    retry every later record of that chat has to wait too.
    """
    heads: dict[tuple[str, str], Any] = {}
    for row in sorted(rows, key=lambda item: item.id):
        heads.setdefault((row.subscriber_type, row.subscriber_id), row)
    return list(heads.values())


class Notifier:
    """Drains the outbox table: renders once per event, sends per recipient."""

    def __init__(
        self,
        store: BiliStore,
        *,
        render: Callable[[BiliCard], Awaitable[bytes]],
        send: Callable[[Any, bytes], Awaitable[None]],
        concurrency: int = 2,
        high_watermark: int = 500,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.render = render
        self.send = send
        # Rendering goes through a global 2 thread pool, more is pointless.
        self.concurrency = max(1, int(concurrency))
        self.high_watermark = int(high_watermark)
        self.clock = clock
        self._render_cache = _RenderCache()
        # One lock per event key so a burst of recipients for the same event
        # still renders the card only once. Bounded like the cache itself.
        self._render_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._dispatcher: asyncio.Task[None] | None = None
        self._wakeup: asyncio.Event | None = None
        self._stopping = False
        self._backlog_waiters: list[asyncio.Event] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._dispatcher is not None and not self._dispatcher.done()

    def wake(self) -> None:
        """Called by the poller once a batch of outbox rows is committed."""
        event = self._wakeup
        if event is not None:
            event.set()

    async def start(self) -> None:
        """Start the dispatcher; records left over by the last run go first."""
        if self.running:
            return
        self._stopping = False
        self._wakeup = asyncio.Event()
        self._dispatcher = asyncio.create_task(self._run(), name="bilibilibot-notifier")

    async def stop(self, drain_timeout: float = 10) -> None:
        """Finish the batch in flight; unsent records stay in the outbox."""
        task = self._dispatcher
        self._stopping = True
        self._release_backlog_waiters()
        if task is None:
            return
        event = self._wakeup
        if event is not None:
            event.set()
        try:
            await asyncio.wait_for(task, timeout=drain_timeout)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            logger.warning(
                "[bilibilibot] notifier drain timed out; unsent records stay in the outbox"
            )
        finally:
            self._dispatcher = None
            self._wakeup = None

    # -- backlog -----------------------------------------------------------

    async def wait_backlog_below(self, limit: int | None = None) -> None:
        """Block while the outbox holds at least the given number of records.

        Called by the poller before it fetches new data, so a long bot outage
        cannot pile up unbounded notifications.
        """
        threshold = self.high_watermark if limit is None else int(limit)
        if threshold <= 0:
            return
        count = await _resolve(self.store.outbox_count())
        if count < threshold:
            return
        logger.info(
            f"[bilibilibot] notification backlog {count} >= {threshold}, pausing the poll"
        )
        waiter = asyncio.Event()
        self._backlog_waiters.append(waiter)
        try:
            while True:
                if not self.running:
                    # Nothing drains the outbox, waiting here would deadlock.
                    logger.warning(
                        f"[bilibilibot] notifier is not running, backlog wait skipped at {count} records"
                    )
                    return
                try:
                    await asyncio.wait_for(
                        waiter.wait(), timeout=BACKLOG_RECHECK_SECONDS
                    )
                except TimeoutError:
                    pass
                waiter.clear()
                count = await _resolve(self.store.outbox_count())
                if count < threshold:
                    break
        finally:
            if waiter in self._backlog_waiters:
                self._backlog_waiters.remove(waiter)
        logger.info(
            f"[bilibilibot] notification backlog {count} < {threshold}, resuming the poll"
        )

    def _release_backlog_waiters(self) -> None:
        """Let waiting pollers re-check the outbox after a batch (or on stop)."""
        for waiter in self._backlog_waiters:
            waiter.set()

    # -- dispatcher --------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                handled = await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"[bilibilibot] notifier dispatch failed: {exc}")
                handled = 0
            self._release_backlog_waiters()
            if self._stopping:
                break
            if handled:
                # A full batch: keep draining, but let other tasks run.
                await asyncio.sleep(0)
                continue
            await self._wait_for_wakeup(IDLE_INTERVAL_SECONDS)

    async def _wait_for_wakeup(self, timeout: float) -> None:
        event = self._wakeup
        if event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return
        # The dispatcher always re-reads the outbox, so a wake that arrives
        # while a batch runs is safely coalesced into the next one.
        event.clear()

    async def _dispatch_once(self) -> int:
        """Send one batch; returns the number of records handled."""
        now = int(self.clock())
        expired = await _resolve(self.store.outbox_expire(now, dict(OUTBOX_MAX_AGE)))
        if expired:
            logger.warning(
                f"[bilibilibot] dropped {expired} expired notification(s) from the outbox"
            )
        rows = await _resolve(self.store.due_outbox(now, DISPATCH_BATCH_LIMIT))
        if not rows:
            return 0
        heads = _head_rows(rows)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run(row: Any) -> None:
            async with semaphore:
                await self._handle_row(row)

        results = await asyncio.gather(
            *(run(row) for row in heads), return_exceptions=True
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error(
                    f"[bilibilibot] notifier failed to handle a record: {result}"
                )
        return len(heads)

    async def _handle_row(self, row: Any) -> None:
        recipient = f"{row.subscriber_type}:{row.subscriber_id}"
        try:
            png = await self._render(row)
            await self.send(row, png)
        except asyncio.CancelledError:
            raise
        except SenderUnavailable as exc:
            now = int(self.clock())
            await _resolve(
                self.store.outbox_retry(
                    row.id,
                    attempts=int(row.attempts or 0),
                    next_attempt_at=now + SENDER_UNAVAILABLE_DELAY_SECONDS,
                    error=_error_text(exc),
                )
            )
            logger.debug(
                f"[bilibilibot] notifier paused, bot unavailable for {recipient}: {exc}"
            )
        except Exception as exc:
            await self._record_failure(row, recipient, exc)
        else:
            await _resolve(self.store.outbox_done(row.id))

    async def _record_failure(self, row: Any, recipient: str, exc: Exception) -> None:
        attempts = int(row.attempts or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            await _resolve(self.store.outbox_drop(row.id))
            logger.error(
                f"[bilibilibot] dropping notification for {recipient} after {attempts} attempts "
                f"(event {row.event_key}): {exc}"
            )
            return
        delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
        await _resolve(
            self.store.outbox_retry(
                row.id,
                attempts=attempts,
                next_attempt_at=int(self.clock()) + delay,
                error=_error_text(exc),
            )
        )
        logger.warning(
            f"[bilibilibot] notification for {recipient} failed ({attempts}/{MAX_ATTEMPTS}), "
            f"retry in {delay}s: {exc}"
        )

    def _render_lock(self, event_key: str) -> asyncio.Lock:
        lock = self._render_locks.get(event_key)
        if lock is None:
            lock = asyncio.Lock()
            self._render_locks[event_key] = lock
            while len(self._render_locks) > RENDER_CACHE_SIZE:
                self._render_locks.popitem(last=False)
        else:
            self._render_locks.move_to_end(event_key)
        return lock

    async def _render(self, row: Any) -> bytes:
        cached = self._render_cache.get(row.event_key)
        if cached is not None:
            return cached
        async with self._render_lock(row.event_key):
            cached = self._render_cache.get(row.event_key)
            if cached is not None:
                return cached
            png = await self.render(_card_from_row(row))
            self._render_cache.put(row.event_key, png)
            return png
