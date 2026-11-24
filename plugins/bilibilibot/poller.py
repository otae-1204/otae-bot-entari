"""Scheduling layer: the only place that ties the transport, the detectors and
the store together.

Each tick selects the due targets for one kind, fetches observations, runs the
pure detectors and writes new state, seen marks and outbox rows in a single
transaction before waking the notifier (refactor plan, section 5.3).
"""

from __future__ import annotations

import asyncio
import time
from time import perf_counter
from typing import Callable

from loguru import logger

from .api.mapping import dynamic_item_to_card
from .api.session import DynamicEnrichDeferred
from .config import bili_settings
from .detect import detect_dynamic, detect_live, detect_video, seen_items_of
from .lottery_filter import applies_to
from .models import BiliEvent, KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, SeenItem, TargetInfo
from .official import ENDFIELD_OFFICIAL_UID
from .store import BiliStore

# Video and dynamic run on their own intervals; live is covered by one batch call.
KIND_INTERVALS = {KIND_VIDEO: 120, KIND_DYNAMIC: 60}
MAX_BACKOFF_SECONDS = 30 * 60
DYNAMIC_CARD_LIMIT = 5
# A window that never fits (detail keeps failing) must not retry at debug
# level forever; after this many consecutive *no-progress* deferrals it joins
# the failure schedule and logs one warning. Deferrals that resolved something
# are converging and never count towards this.
DYNAMIC_DEFERRAL_BACKOFF_ROUNDS = 3
# A failing UP logs once per half hour; repeats are demoted to debug.
VIDEO_FAILURE_LOG_INTERVAL = 1800
def live_batch_enabled() -> bool:
    """Escape hatch from plan section 6/phase 3: ``plugins.bilibilibot.live_batch: false``
    skips the batch endpoint and goes straight back to per-room queries."""
    return bili_settings().live_batch


def event_key(event: BiliEvent, now: int) -> str:
    """Stable identity of an event, used for outbox dedup and render caching."""
    if event.kind == KIND_LIVE:
        return f"live:{event.uid}:{event.card.card_type}:{now}"
    return f"{event.kind}:{event.uid}:{event.card.item_id}"


class _PollResult:
    """One round's outcome, accumulated before the single write transaction."""

    __slots__ = ("updates", "events", "seen", "successes", "failures", "deferred")

    def __init__(self) -> None:
        self.updates: list[TargetInfo] = []
        self.events: list[BiliEvent] = []
        self.seen: list[SeenItem] = []
        self.successes = 0
        self.failures = 0
        # Soft-deferred rounds: no write, no schedule change, retried next tick.
        self.deferred = 0


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
        # Consecutive soft deferrals per target, so a window that can never be
        # filled does not retry at debug level forever.
        self._deferrals: dict[tuple[str, str], int] = {}
        # Per-item transient failures and the ids already resolved in the
        # current window. Item counts decide which shells may be released after
        # repeated failures; the resolved-id set keeps a cache eviction from
        # looking like new progress.
        self._detail_failures: dict[tuple[str, str], dict[str, int]] = {}
        self._resolved_windows: dict[tuple[str, str], set[str]] = {}
        # Shells already released as placeholders in the current window: they
        # are never queried again until a successful round clears the window.
        self._released_windows: dict[tuple[str, str], set[str]] = {}
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
            f"success={result.successes} failed={result.failures} "
            f"deferred={result.deferred} events={len(result.events)} "
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
            logger.info("[bilibilibot] live batch disabled by plugins.bilibilibot.live_batch, using per-room queries")
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
            except DynamicEnrichDeferred as exc:
                # Soft deferral: the subscription baseline and cursor stay put,
                # so the same window is retried on the next tick. The first
                # rounds are free: no backoff, no state or outbox writes.
                result.deferred += 1
                key = (kind, target.uid)
                if exc.item_id:
                    failures = self._detail_failures.setdefault(key, {})
                    failures[exc.item_id] = failures.get(exc.item_id, 0) + 1
                # Failures are consecutive per item: a shell resolved in this
                # round (including one released this round) starts over.
                failures = self._detail_failures.get(key)
                if failures:
                    for item_id in exc.resolved_ids:
                        failures.pop(item_id, None)
                    if not failures:
                        self._detail_failures.pop(key, None)
                if exc.released_ids:
                    self._released_windows.setdefault(key, set()).update(
                        exc.released_ids
                    )
                known = self._resolved_windows.setdefault(key, set())
                newly_resolved = exc.resolved_ids - known
                known.update(exc.resolved_ids)
                if newly_resolved:
                    # This round resolved items before running out of budget,
                    # so the window is converging: the cached successes make
                    # the next attempt cheaper. A slow window must keep its
                    # normal cadence, otherwise the 120/240/480 s backoff would
                    # outlive the 300 s detail cache and it would never finish.
                    self._deferrals.pop(key, None)
                    logger.debug(
                        f"[bilibilibot] {kind} enrichment resolved "
                        f"{len(newly_resolved)} item(s) for {target.uid} before "
                        f"deferring, retrying next tick: {exc}"
                    )
                    return
                rounds = self._deferrals.get(key, 0) + 1
                self._deferrals[key] = rounds
                if rounds < DYNAMIC_DEFERRAL_BACKOFF_ROUNDS:
                    logger.debug(
                        f"[bilibilibot] {kind} enrichment deferred for {target.uid}: {exc}"
                    )
                    return
                # A window that keeps deferring without progress would never
                # escape otherwise: join the failure schedule and warn once
                # when crossing over.
                if rounds == DYNAMIC_DEFERRAL_BACKOFF_ROUNDS:
                    logger.warning(
                        f"[bilibilibot] {kind} enrichment deferred {rounds} rounds in a row "
                        f"without progress for {target.uid}, applying failure backoff: {exc}"
                    )
                else:
                    logger.debug(
                        f"[bilibilibot] {kind} enrichment still deferred for {target.uid}: {exc}"
                    )
                self._fail(kind, target.uid, now)
            except Exception as exc:
                result.failures += 1
                self._deferrals.pop((kind, target.uid), None)
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
        # One absolute deadline covers the feed page and the detail lookups it
        # triggers, so the whole target stays inside the per-target budget.
        deadline = self._deadline()
        items = await self.api.dynamic_items(target.uid, deadline=deadline)
        # 参与过滤的账号与官号都看整页：抽奖动态不该把正常动态挤出前五条，
        # 官号经 RSS 回查得到的详情也不该被截掉。
        whole_page = applies_to(target.uid) or target.uid == ENDFIELD_OFFICIAL_UID
        selected = items if whole_page else items[:DYNAMIC_CARD_LIMIT]
        # 只为基线之后的新动态回查空壳详情：更早的条目要么已经通知过，要么
        # 属于首次订阅的存量，不该消耗详情请求预算。回查失败 / 超预算时整轮
        # 延后（DynamicEnrichDeferred），不推进游标也不推送半成品。
        # 官号走独立的采集与详情校验路径，绝不调用普通动态的 detail。
        if target.uid != ENDFIELD_OFFICIAL_UID:
            # 条数上限必须覆盖即将检测的整个窗口（整页账号最多 10 条），否则
            # 超过 5 条空壳的窗口会每轮延后、永不收敛；10 秒预算才是真正的
            # 请求时间上限。
            # 同一条目连续多轮补不出来后才放行：否则一个永远补不出的条目会把
            # 该 UP 之后的所有新动态一起挡在游标后面，退避多久都没用。只放行
            # 已经失败够多次的 id，新到且首次失败的空壳仍会得到自己的重试轮。
            key = (kind, target.uid)
            release_ids = {
                item_id
                for item_id, attempts in self._detail_failures.get(key, {}).items()
                if attempts >= DYNAMIC_DEFERRAL_BACKOFF_ROUNDS
            }
            window_ids = {
                str(item.get("id_str") or "")
                for item in selected
                if isinstance(item, dict)
            }
            selected = await self.api.enrich_empty_dynamic_items(
                selected,
                deadline=deadline,
                after_ts=target.latest_ts,
                limit=max(DYNAMIC_CARD_LIMIT, len(selected)),
                release_ids=release_ids,
                # Only the current window matters; a released item that has
                # already fallen out of the feed needs no skip entry.
                skip_ids=self._released_windows.get(key, set()) & window_ids,
            )
        cards = [dynamic_item_to_card(item, target.uid) for item in selected]
        seen_ids = await self.store.seen_ids(
            KIND_DYNAMIC, target.uid, [card.item_id for card in cards if card.item_id]
        )
        live_subscribed = await self._live_subscribed(target.uid)
        live_room_id = ""
        if live_subscribed:
            live_target = await self.store.get_target(KIND_LIVE, target.uid)
            live_room_id = live_target.room_id if live_target else ""
        updated, events, marked = detect_dynamic(
            target,
            cards,
            seen_ids=seen_ids,
            video_subscribed=await self._video_subscribed(target.uid),
            live_subscribed=live_subscribed,
            live_room_id=live_room_id,
        )
        # Video/live dynamics are marked as seen without being pushed.
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

    async def _live_subscribed(self, uid: str) -> bool:
        """A UP watched for both live and dynamics must not get a double push."""
        return bool(await self.store.subscriptions_for_target(KIND_LIVE, uid))

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
        self._deferrals.pop(key, None)
        self._detail_failures.pop(key, None)
        self._resolved_windows.pop(key, None)
        self._released_windows.pop(key, None)
        self._next_due[key] = now + self._interval(kind)

    def _fail(self, kind: str, uid: str, now: int) -> None:
        key = (kind, uid)
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        interval = self._interval(kind)
        self._next_due[key] = now + min(interval * 2**failures, MAX_BACKOFF_SECONDS)
