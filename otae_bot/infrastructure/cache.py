"""Small async-friendly TTL/LRU cache with request coalescing."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Awaitable, Callable, Generic, Hashable, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class CacheStats:
    entries: int
    bytes: int
    hits: int
    misses: int
    coalesced: int
    evictions: int
    inflight: int
    expirations: int = 0
    capacity_evictions: int = 0
    oversized: int = 0
    failures: int = 0
    fill_seconds: float = 0.0
    wait_seconds: float = 0.0

    @property
    def direct_hits(self) -> int:
        # Keep ``hits`` compatible with callers that count shared requests as hits.
        return self.hits - self.coalesced


@dataclass(slots=True)
class _CacheEntry(Generic[V]):
    value: V
    expires_at: float
    size: int


@dataclass(slots=True)
class _Flight(Generic[V]):
    task: asyncio.Task[V] | None = None


class AsyncTTLCache(Generic[K, V]):
    """Bounded in-memory cache that coalesces concurrent misses per key."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_bytes: int,
        max_entries: int = 512,
        sizeof: Callable[[V], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[K, str, float], None] | None = None,
        ttl_for_value: Callable[[V], float] | None = None,
    ):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(1, int(max_entries))
        self._sizeof = sizeof or (lambda _value: 1)
        self._clock = clock
        self._entries: OrderedDict[K, _CacheEntry[V]] = OrderedDict()
        self._inflight: dict[K, _Flight[V]] = {}
        self._tasks: set[asyncio.Task[V]] = set()
        self._lock = RLock()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._coalesced = 0
        self._evictions = 0
        self._expirations = 0
        self._capacity_evictions = 0
        self._oversized = 0
        self._failures = 0
        self._fill_seconds = 0.0
        self._wait_seconds = 0.0
        self._on_event = on_event
        self._ttl_for_value = ttl_for_value

    def _event(self, key: K, event: str, value: float = 1) -> None:
        if self._on_event is not None:
            self._on_event(key, event, value)

    async def get_or_create(
        self,
        key: K,
        factory: Callable[[], Awaitable[V]],
        *,
        ttl_seconds: float | None = None,
    ) -> V:
        value, _hit = await self.get_or_create_with_status(
            key, factory, ttl_seconds=ttl_seconds
        )
        return value

    async def get_or_create_with_status(
        self,
        key: K,
        factory: Callable[[], Awaitable[V]],
        *,
        ttl_seconds: float | None = None,
    ) -> tuple[V, bool]:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._entries[key] = entry
                self._hits += 1
                self._event(key, "direct_hits")
                return entry.value, True

            flight = self._inflight.get(key)
            if flight is not None:
                self._hits += 1
                self._coalesced += 1
                shared = True
                self._event(key, "coalesced")
            else:
                self._misses += 1
                shared = False
                self._event(key, "misses")
                flight = _Flight()
                self._inflight[key] = flight
                effective_ttl = (
                    self.ttl_seconds
                    if ttl_seconds is None
                    else max(0.0, float(ttl_seconds))
                )
                flight.task = asyncio.create_task(
                    self._fill(key, flight, factory, effective_ttl)
                )
                self._tasks.add(flight.task)
                flight.task.add_done_callback(self._task_done)

        assert flight.task is not None
        started = time.perf_counter()
        try:
            return await asyncio.shield(flight.task), shared
        finally:
            if shared:
                elapsed = time.perf_counter() - started
                with self._lock:
                    self._wait_seconds += elapsed
                    self._event(key, "wait_seconds", elapsed)

    async def _fill(
        self,
        key: K,
        flight: _Flight[V],
        factory: Callable[[], Awaitable[V]],
        ttl: float,
    ) -> V:
        """The shared task owns cache publication, even if every caller cancels."""
        started = time.perf_counter()
        try:
            value = await factory()
            if self._ttl_for_value is not None:
                ttl = min(ttl, self._ttl_for_value(value))
            with self._lock:
                if self._inflight.get(key) is not flight or ttl <= 0:
                    return value
                size = max(0, int(self._sizeof(value)))
                if self.max_bytes and size > self.max_bytes:
                    self._oversized += 1
                    self._event(key, "oversized")
                    return value
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._bytes -= previous.size
                self._entries[key] = _CacheEntry(value, self._clock() + ttl, size)
                self._bytes += size
                self._evict_to_limits()
            return value
        except BaseException:
            with self._lock:
                self._failures += 1
                self._event(key, "failures")
            raise
        finally:
            with self._lock:
                elapsed = time.perf_counter() - started
                self._fill_seconds += elapsed
                self._event(key, "fill_seconds", elapsed)
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)

    def _task_done(self, task: asyncio.Task[V]) -> None:
        with self._lock:
            self._tasks.discard(task)
        # Retrieve failures even when all waiters cancelled. Awaiters still
        # receive the original exception; this only prevents orphan warnings.
        if not task.cancelled():
            task.exception()

    async def clear(self, predicate: Callable[[K], bool] | None = None) -> int:
        with self._lock:
            keys = [key for key in self._entries if predicate is None or predicate(key)]
            for key in keys:
                entry = self._entries.pop(key)
                self._bytes -= entry.size
            # Detach only matching flights. Existing waiters may finish, but
            # post-clear readers must start new work and old work cannot publish.
            for key in list(self._inflight):
                if predicate is None or predicate(key):
                    self._inflight.pop(key)
            return len(keys)

    async def close(self) -> None:
        """Invalidate values and drain owned tasks before closing their resources."""
        await self.clear()
        loop = asyncio.get_running_loop()
        with self._lock:
            tasks = [task for task in self._tasks if task.get_loop() is loop]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stats(self, predicate: Callable[[K], bool] | None = None) -> CacheStats:
        with self._lock:
            self._purge_expired(self._clock())
            entries = [
                entry
                for key, entry in self._entries.items()
                if predicate is None or predicate(key)
            ]
            return CacheStats(
                entries=len(entries),
                bytes=sum(entry.size for entry in entries),
                hits=self._hits,
                misses=self._misses,
                coalesced=self._coalesced,
                evictions=self._evictions,
                inflight=sum(
                    1 for key in self._inflight if predicate is None or predicate(key)
                ),
                expirations=self._expirations,
                capacity_evictions=self._capacity_evictions,
                oversized=self._oversized,
                failures=self._failures,
                fill_seconds=self._fill_seconds,
                wait_seconds=self._wait_seconds,
            )

    def _purge_expired(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            entry = self._entries.pop(key)
            self._bytes -= entry.size
            self._evictions += 1
            self._expirations += 1
            self._event(key, "expirations")

    def _evict_to_limits(self) -> None:
        while self._entries and (
            len(self._entries) > self.max_entries
            or (self.max_bytes and self._bytes > self.max_bytes)
        ):
            key, entry = self._entries.popitem(last=False)
            self._bytes -= entry.size
            self._evictions += 1
            self._capacity_evictions += 1
            self._event(key, "capacity_evictions")
