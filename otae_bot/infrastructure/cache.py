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


@dataclass(slots=True)
class _CacheEntry(Generic[V]):
    value: V
    expires_at: float
    size: int


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
    ):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(1, int(max_entries))
        self._sizeof = sizeof or (lambda _value: 1)
        self._clock = clock
        self._entries: OrderedDict[K, _CacheEntry[V]] = OrderedDict()
        self._inflight: dict[K, asyncio.Task[V]] = {}
        self._lock = RLock()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._coalesced = 0
        self._evictions = 0
        self._generation = 0

    async def get_or_create(
        self,
        key: K,
        factory: Callable[[], Awaitable[V]],
        *,
        ttl_seconds: float | None = None,
    ) -> V:
        value, _hit = await self.get_or_create_with_status(key, factory, ttl_seconds=ttl_seconds)
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
                return entry.value, True

            task = self._inflight.get(key)
            if task is not None:
                self._hits += 1
                self._coalesced += 1
                owner = False
                generation = self._generation
            else:
                self._misses += 1
                task = asyncio.create_task(factory())
                self._inflight[key] = task
                owner = True
                generation = self._generation

        try:
            value = await asyncio.shield(task)
        except BaseException:
            if owner:
                with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
            raise

        if not owner:
            return value, True

        with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            effective_ttl = self.ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
            if generation != self._generation or effective_ttl <= 0:
                return value, False

            size = max(0, int(self._sizeof(value)))
            if self.max_bytes and size > self.max_bytes:
                return value, False
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous.size
            self._entries[key] = _CacheEntry(value, self._clock() + effective_ttl, size)
            self._bytes += size
            self._evict_to_limits()
        return value, False

    async def clear(self, predicate: Callable[[K], bool] | None = None) -> int:
        with self._lock:
            self._generation += 1
            keys = [key for key in self._entries if predicate is None or predicate(key)]
            for key in keys:
                entry = self._entries.pop(key)
                self._bytes -= entry.size
            return len(keys)

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
                inflight=len(self._inflight),
            )

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            self._bytes -= entry.size
            self._evictions += 1

    def _evict_to_limits(self) -> None:
        while self._entries and (
            len(self._entries) > self.max_entries
            or (self.max_bytes and self._bytes > self.max_bytes)
        ):
            _key, entry = self._entries.popitem(last=False)
            self._bytes -= entry.size
            self._evictions += 1
