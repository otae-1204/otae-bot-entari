"""Refresh version-dependent public collection data, independent of chat commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from ..archives.store import ArchiveSnapshotStore
from ..medals.store import MedalSnapshotStore
from ..providers.akedata import (
    fetch_akedata_manifest,
    latest_version_entry,
    pick_previous_game_version,
)
from .models import (
    ArchiveBaselineView,
    ArchiveSnapshotView,
    MedalBaselineView,
    MedalSnapshotView,
)
from .service import EndfieldService

CHECK_INTERVAL_SECONDS = 10 * 60
REVALIDATE_INTERVAL_SECONDS = 6 * 60 * 60

Snapshot = ArchiveSnapshotView | MedalSnapshotView
Baseline = ArchiveBaselineView | MedalBaselineView | None


class SnapshotPersistenceError(RuntimeError):
    """Fetching succeeded but the replacement could not be saved."""


@dataclass
class _RefreshTarget:
    store: ArchiveSnapshotStore | MedalSnapshotStore
    fetch_current: Callable[..., Awaitable[Snapshot]]
    fetch_baseline: Callable[..., Awaitable[Baseline]]


def source_revision(manifest: dict[str, Any]) -> str:
    """Include hotfix/build and publication metadata, never just major.minor."""
    payload = {
        "schema": 1,
        "latest": latest_version_entry(manifest),
        "previous": pick_previous_game_version(manifest),
        "sharedRevision": manifest.get("sharedRevision"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class CollectionSnapshotRefresher:
    def __init__(
        self,
        service: EndfieldService,
        medal_store: MedalSnapshotStore,
        archive_store: ArchiveSnapshotStore,
        *,
        revalidate_seconds: int = REVALIDATE_INTERVAL_SECONDS,
    ) -> None:
        self._targets = {
            "medal": _RefreshTarget(
                medal_store, service.fetch_medal_snapshot_akedata, service.fetch_akedata_baseline,
            ),
            "archive": _RefreshTarget(
                archive_store, service.fetch_archive_snapshot_akedata, service.fetch_archive_baseline,
            ),
        }
        self._lock = asyncio.Lock()
        self.revalidate_seconds = revalidate_seconds

    async def refresh_all(self) -> dict[str, bool]:
        """One failed collection must not prevent the other from updating."""
        async with self._lock:
            manifest = await fetch_akedata_manifest()
            revision = source_revision(manifest)
            token = uuid4().hex
            results = {}
            for kind in self._targets:
                try:
                    results[kind] = await self._refresh(kind, manifest, revision, token, force=False)
                except Exception as exc:  # noqa: BLE001 - isolate each scheduled collection
                    logger.warning(f"[endfield] {kind} auto refresh failed; will retry: {exc}")
                    results[kind] = False
            return results

    async def refresh(self, kind: str, *, force: bool = True) -> bool:
        """Manual and automatic refreshes share the entire fetch/save critical section."""
        async with self._lock:
            # Resolve inside the lock so an older queued refresh cannot overwrite a newer build.
            manifest = await fetch_akedata_manifest()
            return await self._refresh(kind, manifest, source_revision(manifest), uuid4().hex, force=force)

    async def _refresh(
        self, kind: str, manifest: dict[str, Any], revision: str, token: str, *, force: bool,
    ) -> bool:
        target = self._targets[kind]
        current = target.store.load_current_view()
        now = int(time.time())
        if (
            not force and current is not None and current.total_count > 0
            and current.source_revision == revision
            and 0 <= now - current.fetched_at < self.revalidate_seconds
        ):
            return False

        # Pin current tables and baseline selection to the same manifest for this entire batch.
        snapshot = await target.fetch_current(manifest=manifest, cache_token=token, fetched_at=now)
        try:
            baseline = await target.fetch_baseline(manifest=manifest, cache_token=token, fetched_at=now)
        except Exception as exc:  # noqa: BLE001 - retain usable totals on baseline failure
            baseline = target.store.load_baseline_view()
            previous = pick_previous_game_version(manifest)
            if baseline is not None and (not previous or baseline.version_id != previous.get("id")):
                baseline = None
            # Current totals can advance; an obsolete baseline must not claim a wrong version diff.
            # Empty revision persists the need to retry, including after a process restart.
            snapshot.source_revision = ""
            logger.warning(f"[endfield] {kind} baseline unavailable; will retry: {exc}")
        else:
            snapshot.source_revision = revision
        try:
            await target.store.replace_current_and_baseline(snapshot, baseline)
        except Exception as exc:
            raise SnapshotPersistenceError(f"{kind} snapshot save failed") from exc
        logger.info(
            f"[endfield] {kind} snapshot refreshed build={manifest['latest']} "
            f"count={snapshot.total_count} baseline={baseline.version if baseline else 'none'} "
            f"complete={bool(snapshot.source_revision)}"
        )
        return True
