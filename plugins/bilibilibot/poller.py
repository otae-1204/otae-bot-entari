"""Scheduling layer: the only place that ties the transport, the detectors and
the store together.

Each tick selects the due targets for one kind, fetches observations, runs the
pure detectors and writes new state, seen marks and outbox rows in a single
transaction before waking the notifier (refactor plan, section 5.3).
"""

from __future__ import annotations

import asyncio
import os
import time
from time import perf_counter
from typing import Callable

from loguru import logger

from .api.mapping import dynamic_item_to_card
from .detect import detect_dynamic, detect_live, detect_video, seen_items_of
from .models import BiliEvent, KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, SeenItem, TargetInfo
from .store import BiliStore

# Video and dynamic run on their own intervals; live is covered by one batch call.
KIND_INTERVALS = {KIND_VIDEO: 120, KIND_DYNAMIC: 60}
MAX_BACKOFF_SECONDS = 30 * 60
DYNAMIC_CARD_LIMIT = 5
# A failing UP logs once per half hour; repeats are demoted to debug.
VIDEO_FAILURE_LOG_INTERVAL = 1800
# Escape hatch from plan section 6/phase 3: set to 0 to skip the batch endpoint
# entirely and go straight back to per-room queries.
LIVE_BATCH_ENV = "BILI_LIVE_BATCH"


def live_batch_enabled() -> bool:
    """Read the batch-live kill switch; anything but 0/false/no/off keeps it on."""
    raw = os.getenv(LIVE_BATCH_ENV, "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def event_key(event: BiliEvent, now: int) -> str:
    """Stable identity of an event, used for outbox dedup and render caching."""
    if event.kind == KIND_LIVE:
        return f"live:{event.uid}:{event.card.card_type}:{now}"
    return f"{event.kind}:{event.uid}:{event.card.item_id}"


class _PollResult:
    """One round's outcome, accumulated before the single write transaction."""

    __slots__ = ("updates", "events", "seen", "successes", "failures")

    def __init__(self) -> None:
        self.updates: list[TargetInfo] = []
        self.events: list[BiliEvent] = []
        self.seen: list[SeenItem] = []
        self.successes = 0
        self.failures = 0


class Poller:
    """Runs one tick per kind; commands and delivery never go through here."""

    def __init__(
        self,
        api,
        store: BiliStore,
        notifier=None,
        *,
        concurrency: int = 4,
        target_timeout: float = 30,
        clock: Callable[[], float] = time.time,
        intervals: dict[str, int] | None = None,
        live_batch: bool | None = None,
    ):
        self.api = api
        self.store = store
        self.notifier = notifier
        # None means "follow the environment"; an explicit bool wins for tests.
        self.live_batch = live_batch_enabled() if live_batch is None else bool(live_batch)
        self.concurrency = max(1, int(concurrency))
        self.target_timeout = float(target_timeout)
        self.clock = clock
        self.intervals = dict(KIND_INTERVALS if intervals is None else intervals)
        self._locks = {
            KIND_LIVE: asyncio.Lock(),
            KIND_VIDEO: asyncio.Lock(),
            KIND_DYNAMIC: asyncio.Lock(),
        }
        # Schedule state is deliberately in memory: a restart just re-staggers.
        self._next_due: dict[tuple[str, str], float] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._video_failure_log_cache: dict[str, tuple[str, int]] = {}

    # --- ticks ------------------------------------------------------------

    async def tick_live(self) -> None:
        await self._tick(KIND_LIVE)

    async def tick_video(self) -> None:
        await self._tick(KIND_VIDEO)

    async def tick_dynamic(self) -> None:
        await self._tick(KIND_DYNAMIC)

    async def _tick(self, kind: str) -> None:
        lock = self._locks[kind]
        if lock.locked():
            logger.info(f"[bilibilibot] poll kind={kind} skipped=overlap")
            return
        async with lock:
            await self._run_tick(kind)

    async def _run_tick(self, kind: str) -> None:
        started = perf_counter()
        now = int(self.clock())
        backlog_wait = 0.0
        if self.notifier is not None:
            # Wait before reading state, so the round works on the freshest data.
            wait_started = perf_counter()
            await self.notifier.wait_backlog_below()
            backlog_wait = perf_counter() - wait_started
        targets = list(await self.store.list_active_targets(kind))
        if kind == KIND_LIVE:
            due = targets
            result = await self._poll_live(targets, now)
        else:
            due = [target for target in targets if self._is_due(kind, target.uid, now)]
            result = await self._poll_due(kind, due, now)
        rows = 0
        if result.updates or result.seen or result.events:
            # Recipients are resolved here, so the write itself stays one
            # synchronous transaction on the store's worker thread.
            recipients = await self._recipients_for(result.events)
            rows = await self.store.apply_poll_result(
                result.updates,
                result.seen,
                result.events,
                now,
                expand=lambda event: recipients.get((event.kind, event.uid), []),
                event_key=lambda event: event_key(event, now),
            )
        if result.events and self.notifier is not None:
            self.notifier.wake()
        logger.info(
            f"[bilibilibot] poll kind={kind} targets={len(targets)} due={len(due)} "
            f"success={result.successes} failed={result.failures} events={len(result.events)} "
            f"outbox={rows} backlog_wait={backlog_wait:.3f}s elapsed={perf_counter() - started:.3f}s"
        )

    async def _poll_live(self, targets: list[TargetInfo], now: int) -> _PollResult:
        """One batch request covers every target; per-room queries are the fallback."""
        result = _PollResult()
        if not targets:
            return result
        observations = None
        if self.live_batch:
            try:
                observations = await self.api.batch_live_status([target.uid for target in targets])
            except Exception as exc:
                logger.warning(f"[bilibilibot] live batch failed, falling back to per-room: {exc}")
        else:
            logger.info(f"[bilibilibot] live batch disabled by {LIVE_BATCH_ENV}, using per-room queries")
        if observations is not None:
            for target in targets:
                observation = observations.get(target.uid)
                if observation is None:
                    # Not observed this round: keep the state and stay silent.
                    logger.debug(f"[bilibilibot] live target {target.uid} not observed this round")
                    continue
                self._apply_live(result, target, observation, now)
            return result
        # The fallback is per-room, so it obeys the same concurrency cap and
        # per-target deadline as the video/dynamic ticks (plan section 5.3).
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_target(target: TargetInfo) -> None:
            try:
                async with semaphore:
                    async with asyncio.timeout(self.target_timeout):
                        observation = await self.api.live_observation(target)
            except Exception as exc:
                result.failures += 1
                logger.warning(f"[bilibilibot] live check failed for {target.uid}: {exc}")
                return
            if observation is None:
                # Room missing from the response: same as "not observed".
                return
            self._apply_live(result, target, observation, now)

        await asyncio.gather(*(run_target(target) for target in targets))
        return result

    def _apply_live(
        self, result: _PollResult, target: TargetInfo, observation, now: int
    ) -> None:
        updated, events = detect_live(target, observation, now)
        result.updates.append(updated)
        result.events.extend(events)
        result.successes += 1

    async def _poll_due(self, kind: str, due: list[TargetInfo], now: int) -> _PollResult:
        result = _PollResult()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_target(target: TargetInfo) -> None:
            try:
                async with semaphore:
                    # One target may walk a whole fallback chain; without a hard
                    # budget it can hold a concurrency slot indefinitely.
                    async with asyncio.timeout(self.target_timeout):
                        updated, events, marked = await self._check_target(kind, target)
                result.updates.append(updated)
                result.events.extend(events)
                result.seen.extend(marked)
                result.successes += 1
                self._succeed(kind, target.uid, now)
            except Exception as exc:
                result.failures += 1
                self._fail(kind, target.uid, now)
                if kind == KIND_VIDEO:
                    # A persistently broken UP must not flood the log.
                    self.log_video_check_failure(target.uid, exc)
                else:
                    logger.warning(f"[bilibilibot] {kind} check failed for {target.uid}: {exc}")

        await asyncio.gather(*(run_target(target) for target in due))
        return result

    async def _check_target(
        self, kind: str, target: TargetInfo
    ) -> tuple[TargetInfo, list[BiliEvent], list[SeenItem]]:
        if kind == KIND_VIDEO:
            card = await self.api.latest_video(target.uid, deadline=self._deadline())
            updated, events = detect_video(
                target,
                card,
                already_seen=await self.store.has_seen(KIND_VIDEO, target.uid, card.item_id or ""),
            )
            return updated, events, seen_items_of(events)
        items = await self.api.dynamic_items(target.uid, deadline=self._deadline())
        cards = [dynamic_item_to_card(item, target.uid) for item in items[:DYNAMIC_CARD_LIMIT]]
        seen_ids = await self.store.seen_ids(
            KIND_DYNAMIC, target.uid, [card.item_id for card in cards if card.item_id]
        )
        updated, events, marked = detect_dynamic(
            target,
            cards,
            seen_ids=seen_ids,
            video_subscribed=await self._video_subscribed(target.uid),
        )
        # Video dynamics are marked as seen without being pushed.
        return updated, events, seen_items_of(events) + marked

    def log_video_check_failure(self, uid: str, exc: Exception) -> None:
        """One warning per (uid, message) per 30 minutes; repeats drop to debug."""
        message = str(exc)
        now = int(self.clock())
        cached = self._video_failure_log_cache.get(uid)
        self._video_failure_log_cache[uid] = (message, now)
        if cached and cached[0] == message and now - cached[1] < VIDEO_FAILURE_LOG_INTERVAL:
            logger.debug(f"[bilibilibot] video check failed for {uid}: {message}")
            return
        logger.warning(f"[bilibilibot] video check failed for {uid}: {message}")

    async def _video_subscribed(self, uid: str) -> bool:
        """A UP watched for both videos and dynamics must not get a double push."""
        return bool(await self.store.subscriptions_for_target(KIND_VIDEO, uid))

    async def _recipients_for(self, events: list[BiliEvent]) -> dict[tuple[str, str], list[tuple[str, str]]]:
        """Expand each event into its current subscribers (one query per target)."""
        recipients: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for event in events:
            key = (event.kind, event.uid)
            if key in recipients:
                continue
            subs = await self.store.subscriptions_for_target(event.kind, event.uid)
            recipients[key] = [(sub.subscriber_type, sub.subscriber_id) for sub in subs]
        return recipients

    def _deadline(self) -> float:
        return asyncio.get_running_loop().time() + self.target_timeout

    # --- schedule ---------------------------------------------------------

    def _interval(self, kind: str) -> int:
        # A non-positive interval means "every round"; never divide by it.
        return max(0, int(self.intervals.get(kind, 60)))

    def _is_due(self, kind: str, uid: str, now: int) -> bool:
        key = (kind, uid)
        next_due = self._next_due.get(key)
        if next_due is None:
            interval = self._interval(kind)
            # Stagger the first round across the interval so requests never bunch up.
            next_due = now if interval <= 0 else now + (hash(uid) % interval)
            self._next_due[key] = next_due
        return next_due <= now

    def _succeed(self, kind: str, uid: str, now: int) -> None:
        key = (kind, uid)
        self._failures[key] = 0
        self._next_due[key] = now + self._interval(kind)

    def _fail(self, kind: str, uid: str, now: int) -> None:
        key = (kind, uid)
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        interval = self._interval(kind)
        self._next_due[key] = now + min(interval * 2**failures, MAX_BACKOFF_SECONDS)
