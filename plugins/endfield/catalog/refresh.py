"""Refresh public collection data on demand, with a bot-wide persisted cooldown."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from otae_bot.infrastructure.storage.json_store import JsonStore

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
from .snapshot_store import persist_snapshot

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
RETRY_INTERVAL_SECONDS = 30 * 60
_DEFAULT_STATE_PATH = Path("data/endfield/collection_refresh.json")

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
        "schema": 2,
        "latest": latest_version_entry(manifest),
        "previous": pick_previous_game_version(manifest),
        "sharedRevision": manifest.get("sharedRevision"),
        "updatedAt": manifest.get("updatedAt"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class CollectionSnapshotRefresher:
    def __init__(
        self,
        service: EndfieldService,
        medal_store: MedalSnapshotStore,
        archive_store: ArchiveSnapshotStore,
        *,
        state_path: str | Path = _DEFAULT_STATE_PATH,
        check_interval_seconds: int = CHECK_INTERVAL_SECONDS,
        retry_interval_seconds: int = RETRY_INTERVAL_SECONDS,
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
        self._state_store = JsonStore(state_path)
        self._state_lock = asyncio.Lock()
        self._check = self._state_store.get("check", {})
        self.check_interval_seconds = check_interval_seconds
        self.retry_interval_seconds = retry_interval_seconds

    def _complete(self, revision: str) -> bool:
        return bool(revision) and all(
            (current := target.store.load_current_view()) is not None
            and current.total_count > 0 and current.source_revision == revision
            for target in self._targets.values()
        )

    def _cooling_down(self) -> bool:
        check = self._check
        if not isinstance(check, dict) or check.get("schema") != 1:
            return False
        checked_at = check.get("checked_at")
        if not isinstance(checked_at, (int, float)):
            return False
        complete = check.get("complete") is True
        interval = self.check_interval_seconds if complete else self.retry_interval_seconds
        if not 0 <= time.time() - checked_at < interval:
            return False
        # Removed/legacy snapshots must still initialize even if check metadata survived.
        return not complete or self._complete(check.get("revision", ""))

    async def ensure_fresh(self) -> dict[str, bool]:
        """Queries share one check, including after restart; no timer or user-keyed cache."""
        async with self._lock:
            results = dict.fromkeys(self._targets, False)
            # Check inside the lock: queued users reuse the first caller's result/failure.
            if self._cooling_down():
                return results
            revision = ""
            try:
                manifest = await fetch_akedata_manifest()
                revision = source_revision(manifest)
                token = uuid4().hex
                for kind in self._targets:
                    try:
                        results[kind] = await self._refresh(kind, manifest, revision, token, force=False)
                    except Exception as exc:  # noqa: BLE001 - isolate each collection
                        logger.warning(f"[endfield] {kind} query refresh failed; keeping snapshot: {exc}")
            except Exception as exc:  # noqa: BLE001 - queries can use existing snapshots offline
                logger.warning(f"[endfield] collection manifest check failed; keeping snapshots: {exc}")
            self._check = {
                "schema": 1,
                "checked_at": int(time.time()),
                "revision": revision,
                "complete": self._complete(revision),
            }
            try:
                await persist_snapshot(self._state_store, self._state_lock, check=self._check)
            except Exception as exc:  # noqa: BLE001 - retain the in-memory cooldown on disk failure
                logger.warning(f"[endfield] collection check state save failed: {exc}")
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
