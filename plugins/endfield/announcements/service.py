"""Refresh, plan and deliver without depending on a bot or a running scheduler."""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from .source import POLL_SECONDS
from .store import AnnouncementStore


class DeliveryDeferred(Exception):
    """The bound account is offline or this group's Endfield switch is off."""


class AnnouncementService:
    def __init__(self, store: AnnouncementStore, source, sender, *, clock=time.time):
        self.store = store
        self.source = source
        self.sender = sender
        self.clock = clock
        self.lock = asyncio.Lock()
        self.next_refresh = 0

    async def tick(self, *, read_only: bool = False):
        if self.lock.locked():
            return
        async with self.lock:
            now = int(self.clock())
            if not read_only and not await asyncio.to_thread(self.store.subscriptions):
                await asyncio.to_thread(self.store.plan, now)
                return
            if now >= self.next_refresh:
                self.next_refresh = now + POLL_SECONDS
                try:
                    watch = await asyncio.to_thread(self.store.watch_ids, now)
                    articles = await asyncio.wait_for(
                        self.source.fetch(watch, now=now), 120
                    )
                    await asyncio.to_thread(
                        self.store.apply_snapshot, articles, int(self.clock())
                    )
                except Exception as exc:  # noqa: BLE001 - keep the persisted snapshot on upstream failure
                    await asyncio.to_thread(self.store.set_error, type(exc).__name__)
                    logger.warning(
                        "[endfield-announcements] refresh failed error_type={}",
                        type(exc).__name__,
                    )
            now = int(self.clock())
            await asyncio.to_thread(self.store.plan, now)
            health = await asyncio.to_thread(self.store.metadata)
            # Do not announce a possibly cancelled/rescheduled activity from a
            # stale snapshot after a long upstream outage.
            if now - int(health.get("last_success", 0)) > 3 * POLL_SECONDS:
                return
            jobs = await asyncio.to_thread(self.store.due, now, None)
            semaphore = asyncio.Semaphore(4)

            async def deliver_group(group):
                async with semaphore:
                    batches = {}
                    for job in group:
                        # Catch-up may contain several distinct opening times.
                        # Keep each one separate from news and advance reminders.
                        key = (0, job.due_at) if job.phase == "started" else (1, 0)
                        batches.setdefault(key, []).append(job)
                    for key in sorted(batches):
                        await deliver_batch(batches[key])

            async def deliver_batch(group):
                current = int(self.clock())
                sub = await asyncio.to_thread(
                    self.store.subscription, group[0].subscription_key
                )
                pending = await asyncio.to_thread(
                    self.store.pending_jobs, group, current
                )
                if sub is None or not pending:
                    return
                try:
                    # Receipts for an entire image commit after its ack.
                    await asyncio.wait_for(self.sender(sub.destination, pending), 120)
                except DeliveryDeferred:
                    await asyncio.to_thread(
                        self.store.defer_many, pending, int(self.clock())
                    )
                except Exception as exc:  # noqa: BLE001 - isolate retry state per destination
                    await asyncio.to_thread(
                        self.store.failed_many, pending, int(self.clock())
                    )
                    logger.warning(
                        "[endfield-announcements] delivery failed error_type={}",
                        type(exc).__name__,
                    )
                else:
                    await asyncio.to_thread(self.store.sent_many, pending)

            groups: dict[str, list] = {}
            for job in jobs:
                groups.setdefault(job.subscription_key, []).append(job)
            await asyncio.gather(*(deliver_group(group) for group in groups.values()))
