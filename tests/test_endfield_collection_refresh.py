from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.endfield import cold_start, handlers
from plugins.endfield.archives.store import ArchiveSnapshotStore
from plugins.endfield.catalog.commands import parse_command
from plugins.endfield.catalog.models import (
    ArchiveBaselineView,
    ArchiveItemView,
    ArchiveSnapshotView,
    MedalBaselineView,
    MedalItemView,
    MedalSnapshotView,
)
from plugins.endfield.catalog.refresh import (
    CHECK_INTERVAL_SECONDS,
    RETRY_INTERVAL_SECONDS,
    CollectionSnapshotRefresher,
    SnapshotPersistenceError,
    source_revision,
)
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.medals.store import MedalSnapshotStore
from plugins.endfield.providers import akedata

NOW = 1_790_000_000
REFRESH_MODULE = "plugins.endfield.catalog.refresh"


def manifest(latest="1.5.3@10506507-7", previous="1.4.4@8764515-7"):
    ids = [latest, "1.5.3@10024360-6", previous]
    return {
        "latest": latest,
        "sharedRevision": "shared-1",
        "versions": [
            {
                "id": version,
                "tableCfgPath": f"public/{version.replace('@', '/')}/TableCfg",
                "publishedAt": "2026-09-24",
            }
            for version in dict.fromkeys(ids) if version
        ],
    }


class CollectionRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.medals = MedalSnapshotStore(str(self.root / "medals.json"))
        self.archives = ArchiveSnapshotStore(str(self.root / "archives.json"))
        self.manifest = manifest()
        self.fetch_manifest = AsyncMock(side_effect=lambda: copy.deepcopy(self.manifest))
        self.source = SimpleNamespace(
            fetch_medal_snapshot_akedata=AsyncMock(side_effect=self.medal_snapshot),
            fetch_archive_snapshot_akedata=AsyncMock(side_effect=self.archive_snapshot),
            fetch_akedata_baseline=AsyncMock(side_effect=self.medal_baseline),
            fetch_archive_baseline=AsyncMock(side_effect=self.archive_baseline),
        )
        self.refresher = self.new_refresher()
        p = patch(REFRESH_MODULE + ".fetch_akedata_manifest", self.fetch_manifest)
        p.start()
        self.addCleanup(p.stop)
        p = patch(REFRESH_MODULE + ".time.time", return_value=NOW)
        self.clock = p.start()
        self.addCleanup(p.stop)

    def new_refresher(self, *, check_interval_seconds=0, retry_interval_seconds=0):
        # Most regression cases exercise refresh mechanics without waiting for the cooldown.
        return CollectionSnapshotRefresher(
            self.source, self.medals, self.archives,
            state_path=self.root / "check.json",
            check_interval_seconds=check_interval_seconds,
            retry_interval_seconds=retry_interval_seconds,
        )

    def use_cooldown(self):
        self.refresher = self.new_refresher(
            check_interval_seconds=CHECK_INTERVAL_SECONDS,
            retry_interval_seconds=RETRY_INTERVAL_SECONDS,
        )

    @staticmethod
    async def medal_snapshot(*, manifest, fetched_at, cache_token):
        ids = ["achv_old", "achv_new_" + manifest["latest"]]
        return MedalSnapshotView(
            medals=[MedalItemView(medal_id=i, name=i) for i in ids],
            version=akedata.game_version_label(manifest["latest"]),
            fetched_at=fetched_at, source="akedata", total_count=len(ids),
        )

    @staticmethod
    async def archive_snapshot(*, manifest, fetched_at, cache_token):
        ids = ["nar_old", "nar_new_" + manifest["latest"]]
        return ArchiveSnapshotView(
            items=[ArchiveItemView(item_id=i, name=i) for i in ids],
            version=akedata.game_version_label(manifest["latest"]),
            fetched_at=fetched_at, total_count=len(ids),
        )

    @staticmethod
    async def medal_baseline(*, manifest, fetched_at, cache_token):
        previous = akedata.pick_previous_game_version(manifest)
        if previous is None:
            return None
        return MedalBaselineView(
            version=akedata.game_version_label(previous["id"]), version_id=previous["id"],
            ids=["achv_old"], fetched_at=fetched_at,
        )

    @staticmethod
    async def archive_baseline(*, manifest, fetched_at, cache_token):
        previous = akedata.pick_previous_game_version(manifest)
        if previous is None:
            return None
        return ArchiveBaselineView(
            version=akedata.game_version_label(previous["id"]), version_id=previous["id"],
            ids=["nar_old"], fetched_at=fetched_at,
        )

    async def test_bootstrap_and_restart_skip_unchanged_tables(self):
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": True})
        self.assertEqual(self.medals.load_current_view().source_revision, source_revision(self.manifest))
        self.assertEqual(self.archives.load_baseline_view().version, "1.4")
        self.medals = MedalSnapshotStore(str(self.root / "medals.json"))
        self.archives = ArchiveSnapshotStore(str(self.root / "archives.json"))
        self.assertEqual(await self.new_refresher().ensure_fresh(), {"medal": False, "archive": False})
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()
        self.source.fetch_archive_snapshot_akedata.assert_awaited_once()
        self.source.fetch_akedata_baseline.assert_awaited_once()
        self.assertEqual(self.fetch_manifest.await_count, 2)
        # Shared language table URL can coalesce within this refresh batch.
        self.assertEqual(
            self.source.fetch_medal_snapshot_akedata.call_args.kwargs["cache_token"],
            self.source.fetch_archive_snapshot_akedata.call_args.kwargs["cache_token"],
        )

    async def test_legacy_snapshots_are_refreshed_without_manual_migration(self):
        await self.medals.replace_current(MedalSnapshotView(version="1.5", total_count=1, fetched_at=NOW))
        await self.archives.replace_current(ArchiveSnapshotView(version="1.5", total_count=1, fetched_at=NOW))
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": True})

    async def test_hotfix_refreshes_totals_but_keeps_previous_game_version(self):
        await self.refresher.ensure_fresh()
        self.manifest = manifest("1.5.3@10506508-8")
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": True})
        self.assertEqual(self.medals.load_current_view().version, "1.5")
        service = EndfieldService.__new__(EndfieldService)
        diff = service.build_medal_diff(self.medals.load_current_view(), self.medals.load_baseline_view())
        self.assertEqual(diff.previous_version, "1.4")
        self.assertEqual([m.medal_id for m in diff.new_medals], ["achv_new_1.5.3@10506508-8"])
        archive_diff = service.build_archive_diff(
            self.archives.load_current_view(), self.archives.load_baseline_view(),
        )
        self.assertEqual(archive_diff.previous_version, "1.4")
        self.assertEqual([a.item_id for a in archive_diff.new_items], ["nar_new_1.5.3@10506508-8"])

    async def test_major_version_rotates_the_baseline(self):
        await self.refresher.ensure_fresh()
        self.manifest = manifest("1.6.1@12000000-1", "1.5.3@10506507-7")
        # AKEData's versions are newest first.
        self.manifest["versions"][1:] = list(reversed(self.manifest["versions"][1:]))
        await self.refresher.ensure_fresh()
        for store in (self.medals, self.archives):
            self.assertEqual(store.load_current_view().version, "1.6")
            self.assertEqual(store.load_baseline_view().version_id, "1.5.3@10506507-7")

    async def test_publication_or_shared_revision_also_invalidates(self):
        await self.refresher.ensure_fresh()
        self.manifest["versions"][0]["publishedAt"] = "2026-09-25"
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))
        self.manifest["sharedRevision"] = "shared-2"
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))
        self.manifest["updatedAt"] = "2026-09-26"
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))

    async def test_unchanged_manifest_never_downloads_tables_on_age_alone(self):
        await self.refresher.ensure_fresh()
        first_token = self.source.fetch_medal_snapshot_akedata.call_args.kwargs["cache_token"]
        self.clock.return_value = NOW + 6 * 3600
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.clock.return_value = NOW + 90 * 24 * 3600
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()
        self.assertEqual(first_token, self.source.fetch_medal_snapshot_akedata.call_args.kwargs["cache_token"])

    async def test_one_failed_collection_preserves_old_data_and_retries_independently(self):
        await self.refresher.ensure_fresh()
        old = (self.root / "medals.json").read_bytes()
        self.manifest = manifest("1.5.3@10506508-8")
        self.source.fetch_medal_snapshot_akedata.side_effect = ValueError("incomplete tables")
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": False, "archive": True})
        self.assertEqual((self.root / "medals.json").read_bytes(), old)
        self.source.fetch_medal_snapshot_akedata.side_effect = self.medal_snapshot
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": False})

    async def test_baseline_failure_keeps_compatible_baseline_and_retries_after_restart(self):
        await self.refresher.ensure_fresh()
        self.manifest = manifest("1.5.3@10506508-8")
        self.source.fetch_akedata_baseline.side_effect = RuntimeError("history unavailable")
        await self.refresher.ensure_fresh()
        self.assertEqual(self.medals.load_current_view().source_revision, "")
        self.assertEqual(self.medals.load_baseline_view().version, "1.4")
        self.medals = MedalSnapshotStore(str(self.root / "medals.json"))
        self.source.fetch_akedata_baseline.side_effect = self.medal_baseline
        self.assertEqual(await self.new_refresher().ensure_fresh(), {"medal": True, "archive": False})
        self.assertTrue(self.medals.load_current_view().source_revision)

    async def test_new_major_with_failed_baseline_does_not_report_wrong_version_diff(self):
        await self.refresher.ensure_fresh()
        self.manifest = manifest("1.6.1@12000000-1")
        self.source.fetch_archive_baseline.side_effect = RuntimeError("history unavailable")
        await self.refresher.ensure_fresh()
        self.assertEqual(self.archives.load_current_view().version, "1.6")
        self.assertIsNone(self.archives.load_baseline_view())
        self.assertEqual(self.archives.load_current_view().source_revision, "")

    async def test_no_previous_version_is_success_and_not_an_endless_retry(self):
        self.manifest = manifest(previous="")
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))
        self.assertIsNone(self.medals.load_baseline_view())
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))

    async def test_invalid_or_unavailable_manifest_does_not_touch_snapshots(self):
        await self.refresher.ensure_fresh()
        old = self.medals.load_current_view()
        self.manifest["versions"][0].pop("tableCfgPath")
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.fetch_manifest.side_effect = TimeoutError("manifest offline")
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.assertEqual(self.medals.load_current_view(), old)
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()

    async def test_failed_save_keeps_memory_and_disk_and_can_retry(self):
        await self.refresher.ensure_fresh()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        old_medals, old_archives = self.medals.load_current_view(), self.archives.load_current_view()
        self.manifest = manifest("1.5.3@10506508-8")
        with patch("plugins.endfield.catalog.snapshot_store.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(await self.refresher.ensure_fresh(), {"medal": False, "archive": False})
            with self.assertRaises(SnapshotPersistenceError):
                await self.refresher.refresh("medal")
        self.assertEqual(self.medals.load_current_view(), old_medals)
        self.assertEqual(self.archives.load_current_view(), old_archives)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))

    async def test_manual_refresh_forces_unchanged_data(self):
        await self.refresher.ensure_fresh()
        token = self.source.fetch_archive_snapshot_akedata.call_args.kwargs["cache_token"]
        self.assertTrue(await self.refresher.refresh("archive"))
        self.assertEqual(self.source.fetch_archive_snapshot_akedata.await_count, 2)
        self.assertNotEqual(token, self.source.fetch_archive_snapshot_akedata.call_args.kwargs["cache_token"])
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()

    async def test_cancelling_a_write_does_not_publish_half_a_pair_or_allow_rollback(self):
        await self.refresher.ensure_fresh()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        replace_file = os.replace

        def delayed_replace(source, target):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release writer")
            replace_file(source, target)

        with patch("plugins.endfield.catalog.snapshot_store.os.replace", side_effect=delayed_replace):
            first = asyncio.create_task(self.medals.replace_current_and_baseline(
                MedalSnapshotView(version="1.6", total_count=3), MedalBaselineView(version="1.5"),
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(self.medals.load_current_view().version, "1.5")
                self.assertEqual(self.medals.load_baseline_view().version, "1.4")
                first.cancel()
                second = asyncio.create_task(self.medals.replace_current_and_baseline(
                    MedalSnapshotView(version="1.7", total_count=4), MedalBaselineView(version="1.6"),
                ))
                await asyncio.sleep(0)
                self.assertFalse(first.done())
                self.assertFalse(second.done())
            finally:
                release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIsNone(results[1])
        reopened = MedalSnapshotStore(str(self.root / "medals.json"))
        self.assertEqual(reopened.load_current_view().version, "1.7")
        self.assertEqual(reopened.load_baseline_view().version, "1.6")

    async def test_overlapping_automatic_checks_only_build_once(self):
        results = await asyncio.gather(self.refresher.ensure_fresh(), self.refresher.ensure_fresh())
        self.assertEqual(results, [{"medal": True, "archive": True}, {"medal": False, "archive": False}])
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()

    async def test_many_queries_share_manifest_tables_and_daily_cooldown(self):
        self.use_cooldown()
        self.fetch_manifest.assert_not_awaited()  # Constructing the refresher never checks upstream.
        results = await asyncio.gather(*(self.refresher.ensure_fresh() for _ in range(20)))
        self.assertEqual(sum(result["medal"] for result in results), 1)
        self.fetch_manifest.assert_awaited_once()
        self.clock.return_value = NOW + CHECK_INTERVAL_SECONDS - 1
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.fetch_manifest.assert_awaited_once()
        self.clock.return_value += 1
        self.assertFalse(any((await self.refresher.ensure_fresh()).values()))
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()
        self.source.fetch_archive_snapshot_akedata.assert_awaited_once()

    async def test_restart_reuses_last_manifest_check_even_when_tables_are_old(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.clock.return_value += 7 * CHECK_INTERVAL_SECONDS
        await self.refresher.ensure_fresh()  # Only the manifest timestamp advances.
        self.medals = MedalSnapshotStore(str(self.root / "medals.json"))
        self.archives = ArchiveSnapshotStore(str(self.root / "archives.json"))
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()
        self.assertEqual(self.medals.load_current_view().fetched_at, NOW)

    async def test_hotfix_waits_for_daily_check_then_all_users_see_new_snapshot(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.manifest = manifest("1.5.3@10506508-8")
        self.clock.return_value += CHECK_INTERVAL_SECONDS - 1
        await self.refresher.ensure_fresh()
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()
        self.clock.return_value += 1
        await asyncio.gather(*(self.refresher.ensure_fresh() for _ in range(10)))
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.assertEqual(self.source.fetch_medal_snapshot_akedata.await_count, 2)
        self.assertEqual(self.medals.load_current_view().source_revision, source_revision(self.manifest))

    async def test_manifest_failure_is_throttled_across_users_and_restarts(self):
        self.use_cooldown()
        self.fetch_manifest.side_effect = TimeoutError("offline")
        await asyncio.gather(*(self.refresher.ensure_fresh() for _ in range(10)))
        self.fetch_manifest.assert_awaited_once()
        self.use_cooldown()
        self.clock.return_value += RETRY_INTERVAL_SECONDS - 1
        await self.refresher.ensure_fresh()
        self.fetch_manifest.assert_awaited_once()
        self.fetch_manifest.side_effect = lambda: copy.deepcopy(self.manifest)
        self.clock.return_value += 1
        self.assertTrue(all((await self.refresher.ensure_fresh()).values()))
        self.assertEqual(self.fetch_manifest.await_count, 2)

    async def test_partial_failure_retries_only_failed_target_after_cooldown(self):
        self.use_cooldown()
        self.source.fetch_akedata_baseline.side_effect = RuntimeError("incomplete history")
        await self.refresher.ensure_fresh()
        self.assertEqual(self.medals.load_current_view().source_revision, "")
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.fetch_manifest.assert_awaited_once()
        self.clock.return_value += RETRY_INTERVAL_SECONDS
        self.source.fetch_akedata_baseline.side_effect = self.medal_baseline
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": False})
        self.source.fetch_archive_snapshot_akedata.assert_awaited_once()
        await self.refresher.ensure_fresh()
        self.assertEqual(self.fetch_manifest.await_count, 2)

    async def test_failed_update_preserves_snapshot_for_queries_until_retry(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        old = self.medals.load_current_view()
        self.clock.return_value += CHECK_INTERVAL_SECONDS
        self.manifest = manifest("1.5.3@10506508-8")
        self.source.fetch_medal_snapshot_akedata.side_effect = ValueError("incomplete data")
        await asyncio.gather(*(self.refresher.ensure_fresh() for _ in range(10)))
        self.assertEqual(self.medals.load_current_view(), old)
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.assertEqual(self.source.fetch_medal_snapshot_akedata.await_count, 2)

    async def test_manual_refresh_bypasses_success_and_failure_cooldowns(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        await self.refresher.refresh("medal")
        self.assertEqual(self.source.fetch_medal_snapshot_akedata.await_count, 2)
        self.clock.return_value += CHECK_INTERVAL_SECONDS
        self.fetch_manifest.side_effect = TimeoutError("offline")
        await self.refresher.ensure_fresh()
        self.fetch_manifest.side_effect = lambda: copy.deepcopy(self.manifest)
        await self.refresher.refresh("medal")
        self.assertEqual(self.source.fetch_medal_snapshot_akedata.await_count, 3)

    async def test_removed_snapshot_does_not_hide_behind_success_cooldown(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        (self.root / "medals.json").unlink()
        self.medals = MedalSnapshotStore(str(self.root / "medals.json"))
        self.use_cooldown()
        self.assertEqual(await self.refresher.ensure_fresh(), {"medal": True, "archive": False})

    async def test_check_state_save_failure_keeps_in_memory_cooldown(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.clock.return_value += CHECK_INTERVAL_SECONDS
        with patch("plugins.endfield.catalog.snapshot_store.os.replace", side_effect=OSError("disk full")):
            await self.refresher.ensure_fresh()
            await self.refresher.ensure_fresh()
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()

    async def test_clock_rollback_does_not_leave_cooldown_stuck(self):
        self.use_cooldown()
        await self.refresher.ensure_fresh()
        self.clock.return_value -= 3600
        await self.refresher.ensure_fresh()
        self.assertEqual(self.fetch_manifest.await_count, 2)
        self.source.fetch_medal_snapshot_akedata.assert_awaited_once()

    async def test_manual_waits_for_query_and_resolves_manifest_after_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return await self.medal_snapshot(**kwargs)

        self.source.fetch_medal_snapshot_akedata.side_effect = delayed
        background = asyncio.create_task(self.refresher.ensure_fresh())
        await asyncio.wait_for(entered.wait(), 2)
        self.manifest = manifest("1.5.3@10506508-8")
        manual = asyncio.create_task(self.refresher.refresh("medal"))
        await asyncio.sleep(0)
        self.fetch_manifest.assert_awaited_once()
        release.set()
        await asyncio.wait_for(asyncio.gather(background, manual), 2)
        self.assertEqual(self.medals.load_current_view().source_revision, source_revision(self.manifest))
        # Both collections in the background batch used its original pinned manifest.
        self.assertEqual(
            self.source.fetch_archive_snapshot_akedata.call_args.kwargs["manifest"]["latest"],
            "1.5.3@10506507-7",
        )


class PinnedProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_providers_use_pinned_paths_and_shared_cache_token(self):
        pinned = manifest()
        with (
            patch.object(akedata, "fetch_akedata_manifest", AsyncMock()) as fetch_manifest,
            patch.object(akedata, "_get", AsyncMock(return_value={"ok": {}})) as get,
        ):
            medals = await akedata.fetch_akedata_medal_tables(manifest=pinned, cache_token="refresh-1")
            archives = await akedata.fetch_akedata_archive_tables(manifest=pinned, cache_token="refresh-1")
        fetch_manifest.assert_not_awaited()
        paths = [call.args[0] for call in get.await_args_list]
        self.assertTrue(all(p.startswith("/public/1.5.3/10506507-7/TableCfg/") for p in paths))
        self.assertTrue(all(p.endswith("?v=refresh-1") for p in paths))
        self.assertEqual(len([p for p in paths if "I18nTextTable_CN" in p]), 2)
        self.assertEqual(medals[-1], pinned["latest"])
        self.assertEqual(archives[-1], pinned["latest"])

    async def test_refresh_token_keeps_i18n_warm_for_cold_start_checks(self):
        """A ``?v=`` refresh read must still count as a warm I18n read."""
        pinned = manifest()
        akedata.clear_i18n_process_warm()
        self.addCleanup(akedata.clear_i18n_process_warm)
        with patch.object(akedata, "fetch_json", AsyncMock(return_value={"ok": {}})) as fetch:
            await akedata.fetch_akedata_medal_tables(manifest=pinned, cache_token="refresh-1")
        requested = [
            call.args[0] for call in fetch.await_args_list if "I18nTextTable_CN" in call.args[0]
        ]
        self.assertEqual(len(requested), 1)
        # Cache identity drops the token, so it still matches the manifest path.
        self.assertEqual(
            akedata.i18n_loaded_path(),
            "/public/1.5.3/10506507-7/TableCfg/I18nTextTable_CN.json",
        )
        self.assertTrue(akedata.i18n_process_warm())
        self.assertTrue(akedata.i18n_loaded_request_path().endswith("?v=refresh-1"))
        with (
            patch.object(cold_start, "fetch_akedata_manifest", AsyncMock(return_value=pinned)),
            patch.object(
                cold_start, "cached_public_resource", AsyncMock(return_value=True)
            ) as cached,
        ):
            self.assertFalse(await cold_start.ake_public_tables_cold())
        # The probe must use the path the refresh really wrote, not the token-less one.
        self.assertEqual(cached.await_args.args[0], requested[0])
        with (
            patch.object(cold_start, "fetch_akedata_manifest", AsyncMock(return_value=pinned)),
            patch.object(cold_start, "cached_public_resource", AsyncMock(return_value=False)),
        ):
            self.assertTrue(await cold_start.ake_public_tables_cold())

    async def test_baselines_use_same_manifest_and_reject_missing_historical_path(self):
        service = EndfieldService.__new__(EndfieldService)
        pinned = manifest()
        with (
            patch("plugins.endfield.catalog.service.fetch_akedata_manifest", AsyncMock()) as fetch_manifest,
            patch("plugins.endfield.catalog.service.fetch_akedata_achievement_table",
                  AsyncMock(return_value={"achv_old": {}})) as medal_table,
            patch("plugins.endfield.catalog.service.fetch_akedata_prts_all_item",
                  AsyncMock(return_value={"nar_old": {}})) as archive_table,
        ):
            medal = await service.fetch_akedata_baseline(manifest=pinned, cache_token="refresh-1")
            archive = await service.fetch_archive_baseline(manifest=pinned, cache_token="refresh-1")
            self.assertEqual(medal.version_id, "1.4.4@8764515-7")
            self.assertEqual(archive.version_id, medal.version_id)
            fetch_manifest.assert_not_awaited()
            for table in (medal_table, archive_table):
                table.assert_awaited_once_with("public/1.4.4/8764515-7/TableCfg", cache_token="refresh-1")
            pinned["versions"][-1].pop("tableCfgPath")
            for fetch in (service.fetch_akedata_baseline, service.fetch_archive_baseline):
                with self.assertRaisesRegex(ValueError, "缺少 tableCfgPath"):
                    await fetch(manifest=pinned)


class CollectionLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_queries_read_snapshot_after_ensuring_freshness(self):
        for kind, text, store in (
            ("medal", "奖章", handlers.medal_store),
            ("archive", "档案", handlers.archive_store),
        ):
            state = SimpleNamespace(current=None)
            updated = SimpleNamespace(version="new")

            async def refresh(state=state, updated=updated):
                state.current = updated

            with (
                self.subTest(kind=kind),
                patch.object(handlers.collection_refresher, "ensure_fresh", AsyncMock(side_effect=refresh)) as ensure,
                patch.object(store, "load_current_view", side_effect=lambda state=state: state.current),
                patch.object(store, "load_baseline_view", return_value=None),
                patch.object(handlers.service, f"build_{kind}_diff") as build,
                patch.object(handlers, f"draw_{kind}_stats_card", AsyncMock(return_value=(b"png",))),
                patch.object(handlers, "_finish_pngs", AsyncMock()) as finish,
            ):
                matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
                await getattr(handlers, f"_handle_{kind}")(matcher, parse_command(text))
                ensure.assert_awaited_once()
                build.assert_called_once_with(updated, None)
                finish.assert_awaited_once()

    async def test_personal_queries_refresh_before_comparing_player_progress(self):
        @asynccontextmanager
        async def claim(role):
            yield

        role = SimpleNamespace(nickname="test", masked_uid="123", server_name="官服", server_id="1")
        for action, text, store in (
            ("medal_missing", "奖章 缺章", handlers.medal_store),
            ("archive_progress", "档案 收集", handlers.archive_store),
        ):
            state = SimpleNamespace(current=None)
            updated = SimpleNamespace(version="new")

            async def refresh(state=state, updated=updated):
                state.current = updated

            with (
                self.subTest(action=action),
                patch.object(handlers.collection_refresher, "ensure_fresh", AsyncMock(side_effect=refresh)) as ensure,
                patch.object(store, "load_current_view", side_effect=lambda state=state: state.current),
                patch.object(handlers.account_store, "resolve_role", return_value=role),
                patch.object(handlers.account_store, "decrypt_token", return_value="token"),
                patch.object(handlers.ROLE_TASKS, "claim", side_effect=claim),
                patch.object(handlers.official_client, "endfield_card_detail", AsyncMock(return_value={})) as detail,
                patch.object(handlers.service, f"build_{action}_view") as build,
                patch.object(handlers, f"draw_{action}_card", AsyncMock(return_value=(b"png",))),
                patch.object(handlers, "_finish_pngs", AsyncMock()),
            ):
                matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
                await getattr(handlers, f"_handle_{action}")(
                    matcher, "user", parse_command(text), None, group=False,
                )
                ensure.assert_awaited_once()
                detail.assert_awaited_once()
                self.assertIs(build.call_args.args[1], updated)

    async def test_unbound_personal_queries_do_not_trigger_public_refresh(self):
        for action, text in (("medal_missing", "奖章 缺章"), ("archive_progress", "档案 收集")):
            with (
                self.subTest(action=action),
                patch.object(handlers.account_store, "resolve_role", return_value=None),
                patch.object(handlers.collection_refresher, "ensure_fresh", AsyncMock()) as ensure,
            ):
                matcher = SimpleNamespace(finish=AsyncMock())
                await getattr(handlers, f"_handle_{action}")(
                    matcher, "unbound", parse_command(text), None, group=False,
                )
                ensure.assert_not_awaited()

    def test_collections_have_no_timer_or_startup_hook(self):
        self.assertNotIn("endfield_collection_snapshot_refresh", handlers.timer._jobs)
        self.assertFalse(hasattr(handlers, "_warmup_collection_snapshots"))

    async def test_cleanup_cancels_startup_before_closing_resources(self):
        entered = asyncio.Event()

        async def startup():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(startup())
        await entered.wait()

        async def assert_stopped():
            self.assertTrue(task.cancelled())

        with ExitStack() as stack:
            stack.enter_context(patch.object(handlers, "_ownership_startup_task", task))
            stack.enter_context(patch.object(handlers, "close_challenge_locale", AsyncMock(side_effect=assert_stopped)))
            for resource in (
                handlers._CARD_CACHE, handlers._LOADOUT_CACHE, handlers._CALENDAR_CACHE,
                handlers._CHALLENGE_DATA_CACHE, handlers._CHALLENGE_RENDER_CACHE,
                handlers.service._weapon_relations, handlers.service._ake_views,
                handlers._ACCOUNT_PAGE_CACHE, handlers.official_client,
            ):
                stack.enter_context(patch.object(resource, "close", AsyncMock(side_effect=assert_stopped)))
            await handlers._close_ownership_startup_task()

    async def test_manual_commands_use_shared_refresher(self):
        for kind, text, store in (
            ("medal", "奖章 刷新", handlers.medal_store),
            ("archive", "档案 刷新", handlers.archive_store),
        ):
            with (
                self.subTest(kind=kind),
                patch.object(handlers.collection_refresher, "refresh", AsyncMock()) as refresh,
                patch.object(handlers.collection_refresher, "ensure_fresh", AsyncMock()) as ensure,
                patch.object(store, "load_current_view", return_value=None),
            ):
                matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
                await getattr(handlers, f"_handle_{kind}")(matcher, parse_command(text))
                refresh.assert_awaited_once_with(kind)
                ensure.assert_not_awaited()
                self.assertIn("稍后查询", matcher.finish.call_args.args[0])
