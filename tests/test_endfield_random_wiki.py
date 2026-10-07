from __future__ import annotations

import asyncio
import json
import random
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import plugins.endfield.handlers as endfield
from plugins.endfield.catalog import random_wiki
from plugins.endfield.catalog.commands import EndfieldCandidate, parse_command
from plugins.endfield.catalog.models import ArchiveItemView, ArchiveSnapshotView
from plugins.endfield.catalog.random_wiki import (
    RandomWikiService,
    is_public_wiki_candidate,
)
from plugins.endfield.catalog.service import EndfieldService
from plugins.endfield.encyclopedia import archives, index
from plugins.endfield.encyclopedia.models import EncyclopediaIndex, IndexEntry
from plugins.endfield.providers import repository
from plugins.endfield.providers.repository import AkeSnapshot
from plugins.endfield.providers.warfarin import WarfarinClient
from plugins.endfield.stages.models import (
    StageCatalogGroup,
    StageCatalogItem,
    StageCatalogView,
)


def candidate(kind="operator", key="char_1"):
    return EndfieldCandidate(kind, key, "公开资料", 100, source="akedata")


class RandomWikiCommandTests(unittest.TestCase):
    def test_aliases_whitespace_and_case(self):
        for text in (
            "随机",
            "随机wiki",
            "随机百科",
            "random",
            "rand",
            "RANDOM",
            "随机Wiki",
            "  随机  ",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, "random_wiki")

    def test_extra_arguments_are_rejected_without_routing_to_account_commands(self):
        for text in (
            "随机 账号",
            "random 流水",
            "随机 -s fz",
            "随机 --all",
            "随机 养成统计",
        ):
            with self.subTest(text=text):
                parsed = parse_command(text)
                self.assertEqual(parsed.action, "invalid")
                self.assertIn("/zmd 随机", parsed.error)

    def test_existing_commands_keep_their_routes(self):
        for text, action in (
            ("账号", "accounts"),
            ("养成统计", "account_investment"),
            ("资源流水", "currency_log"),
            ("干员 陈千语", "query"),
            ("敌人 随机", "query"),
            ("随机化", "query"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).action, action)


class RandomWikiCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = AkeSnapshot(
            "1.5.3@9885010-4", "public/1.5.3/9885010-4/TableCfg", "random-test"
        )
        self.data._tables = json.loads(
            (
                Path(__file__).parent / "fixtures/endfield_akedata_1_5_3.json"
            ).read_bytes()
        )
        for patcher in (
            patch.object(repository, "snapshot", AsyncMock(return_value=self.data)),
            patch.object(
                repository,
                "_get",
                side_effect=AssertionError("Unexpected network request"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(index.clear_index_caches)
        self.addCleanup(archives.clear_caches)
        self.catalog = EndfieldService(Mock(spec=WarfarinClient))
        self.stages = Mock()
        self.archive_snapshot = Mock(return_value=None)
        self.service = RandomWikiService(
            self.catalog, self.stages, self.archive_snapshot
        )

    async def test_all_seven_ake_catalogs_produce_public_entries_with_revision(self):
        for kind in (
            "operator",
            "weapon",
            "equipment",
            "item",
            "prop",
            "enemy",
            "term",
        ):
            with self.subTest(kind=kind):
                entries = await self.service.load_candidates(kind)
                self.assertTrue(entries)
                self.assertTrue(
                    all(is_public_wiki_candidate(entry) for entry in entries)
                )
                self.assertTrue(
                    all(
                        entry.kind == kind and entry.revision == self.data.revision
                        for entry in entries
                    )
                )
        self.archive_snapshot.assert_not_called()
        self.assertFalse(self.stages.mock_calls)

    async def test_equipment_includes_every_rarity_without_loading_full_details(self):
        with patch.object(
            self.catalog,
            "get_equipment_catalog_view",
            wraps=self.catalog.get_equipment_catalog_view,
        ) as get:
            entries = await self.service.load_candidates("equipment")
        get.assert_awaited_once_with(
            rarity_filter="all", include_details=False, source="akedata"
        )
        self.assertTrue(entries)

    async def test_hidden_and_mismatched_encyclopedia_entries_are_excluded(self):
        source = EncyclopediaIndex(
            "enemy",
            "test",
            entries=(
                IndexEntry("enemy", "public", "已收录"),
                IndexEntry("enemy", "hidden", "隐藏变体", listed=False),
                IndexEntry("account", "private", "账号"),
            ),
        )
        with patch.object(random_wiki, "get_index", AsyncMock(return_value=source)):
            entries = await self.service.load_candidates("enemy")
        self.assertEqual([entry.key for entry in entries], ["public"])

    async def test_stages_use_queryable_public_catalog_entries_and_default_detail(self):
        item = StageCatalogItem(
            "公开关卡",
            "测试关卡",
            "war",
            "战争回响",
            "revision",
            "today",
            entry_key="room",
        )
        self.stages.get_catalog_view = AsyncMock(
            return_value=StageCatalogView(
                (
                    StageCatalogGroup(
                        "war", "战争回响", (item, replace(item, queryable=False))
                    ),
                ),
                "akedata",
                "revision",
                "today",
            )
        )
        entries = await self.service.load_candidates("stage")
        self.stages.get_catalog_view.assert_awaited_once_with("akedata")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].key, item.stage_key)
        self.assertEqual((entries[0].mode, entries[0].variant), ("detail", ""))
        self.archive_snapshot.assert_not_called()

    async def test_missing_archive_snapshot_is_skipped(self):
        self.assertEqual(await self.service.load_candidates("archive_entry"), [])

    async def test_archive_entries_use_the_shared_public_snapshot(self):
        self.archive_snapshot.return_value = ArchiveSnapshotView(
            items=[ArchiveItemView(item_id="public_report", name="公开报告")],
            version="random-test",
            fetched_at=123,
        )
        entries = await self.service.load_candidates("archive_entry")
        self.assertEqual([entry.key for entry in entries], ["public_report"])
        self.assertTrue(all(is_public_wiki_candidate(entry) for entry in entries))

    async def test_personal_kinds_rejected_before_any_data_access(self):
        for kind in (
            "account",
            "accounts",
            "account_base",
            "account_investment",
            "currency_log",
            "challenge",
            "gacha",
            "daily",
            "archive_progress",
            "medal_missing",
            "ownership_stats",
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                await self.service.load_candidates(kind)
        self.archive_snapshot.assert_not_called()
        self.assertFalse(self.stages.mock_calls)

    async def test_archive_refresh_does_not_reuse_an_older_card(self):
        await endfield._CARD_CACHE.clear()
        self.addAsyncCleanup(endfield._CARD_CACHE.clear)
        view = ArchiveSnapshotView(
            items=[ArchiveItemView(item_id="report", name="旧报告")],
            version="archive-cache-test",
            fetched_at=1,
        )
        self.archive_snapshot.return_value = view
        with (
            patch.object(
                endfield.archive_store, "load_current_view", self.archive_snapshot
            ),
            patch.object(
                endfield.encyclopedia_draw,
                "draw_archive_entry_card",
                AsyncMock(side_effect=lambda entry: entry.name.encode()),
            ) as draw,
        ):
            first = (await self.service.load_candidates("archive_entry"))[0]
            self.assertEqual(
                await endfield._render_candidate(first), ("旧报告".encode(),)
            )
            # The same game version can be refreshed with revised public metadata.
            self.archive_snapshot.return_value = replace(
                view,
                fetched_at=2,
                items=[ArchiveItemView(item_id="report", name="修订报告")],
            )
            second = (await self.service.load_candidates("archive_entry"))[0]
            self.assertEqual(
                await endfield._render_candidate(second), ("修订报告".encode(),)
            )
            self.assertNotEqual(first.revision, second.revision)
            self.assertEqual(draw.await_count, 2)


class RandomWikiSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rng = Mock()
        self.rng.choice.side_effect = lambda entries: entries[-1]
        self.service = RandomWikiService(Mock(), Mock(), Mock(), rng=self.rng)

    async def test_loads_only_selected_category_and_selects_an_individual_entry(self):
        entries = [candidate(key="a"), candidate(key="b")]
        with patch.object(
            self.service, "load_candidates", AsyncMock(return_value=entries)
        ) as load:
            iterator = self.service.candidates()
            selected = await anext(iterator)
            await iterator.aclose()
        self.assertEqual(selected.key, "b")
        load.assert_awaited_once_with("operator")
        self.rng.shuffle.assert_called_once()
        self.rng.choice.assert_called_once_with(tuple(entries))

    async def test_skips_errors_empty_catalogs_and_private_candidates(self):
        with patch.object(
            self.service,
            "load_candidates",
            AsyncMock(
                side_effect=[
                    ValueError("Unavailable"),
                    [],
                    [candidate("account")],
                    [candidate("enemy")],
                ]
            ),
        ) as load:
            iterator = self.service.candidates()
            self.assertEqual((await anext(iterator)).kind, "enemy")
            await iterator.aclose()
        self.assertEqual(load.await_count, 4)

    async def test_rejects_invalid_entries_and_deduplicates_ids_before_sampling(self):
        valid = candidate()
        with patch.object(
            self.service,
            "load_candidates",
            AsyncMock(
                return_value=[
                    valid,
                    valid,
                    replace(valid, key=" "),
                    replace(valid, display_name=""),
                    replace(valid, kind="account"),
                    replace(valid, source="official"),
                ]
            ),
        ):
            iterator = self.service.candidates()
            self.assertEqual(await anext(iterator), valid)
            await iterator.aclose()
        self.rng.choice.assert_called_once_with((valid,))

    async def test_render_attempts_are_bounded(self):
        with patch.object(
            self.service,
            "load_candidates",
            AsyncMock(side_effect=lambda kind: [candidate(kind)]),
        ) as load:
            results = [entry async for entry in self.service.candidates()]
        self.assertEqual(len(results), 3)
        self.assertEqual(load.await_count, 3)

    async def test_expired_selection_budget_stops_loading(self):
        with (
            patch.object(random_wiki, "monotonic", side_effect=[0, 31]),
            patch.object(self.service, "load_candidates", AsyncMock()) as load,
        ):
            self.assertEqual([entry async for entry in self.service.candidates()], [])
        load.assert_not_awaited()

    async def test_catalog_timeout_falls_through_to_next_category(self):
        async def load(kind):
            if kind == "operator":
                await asyncio.Event().wait()
            return [candidate(kind)]

        with (
            patch.object(random_wiki, "CATALOG_TIMEOUT_SECONDS", 0.01),
            patch.object(self.service, "load_candidates", side_effect=load),
        ):
            iterator = self.service.candidates()
            self.assertEqual((await anext(iterator)).kind, "weapon")
            await iterator.aclose()

    async def test_cancellation_propagates_without_retry(self):
        with (
            patch.object(
                self.service,
                "load_candidates",
                AsyncMock(side_effect=asyncio.CancelledError),
            ) as load,
            self.assertRaises(asyncio.CancelledError),
        ):
            await anext(self.service.candidates())
        self.assertEqual(load.await_count, 1)

    async def test_repeated_calls_are_not_cached_and_reach_all_categories(self):
        self.service.rng = random.Random(7)
        with patch.object(
            self.service,
            "load_candidates",
            AsyncMock(side_effect=lambda kind: [candidate(kind)]),
        ):
            kinds = set()
            for _ in range(100):
                iterator = self.service.candidates()
                kinds.add((await anext(iterator)).kind)
                await iterator.aclose()
        self.assertEqual(kinds, set(random_wiki.PUBLIC_WIKI_KINDS))


class RandomWikiHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.matcher = Mock(finish=AsyncMock())
        self.patches = {}
        for name in (
            "notice_default_ake_public",
            "_render_candidate",
            "_finish_pngs",
            "_handle_personal_command",
        ):
            patcher = patch.object(endfield, name, AsyncMock())
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patches["_render_candidate"].return_value = (b"card",)
        # Random Wiki must also work without any user identity or account services.
        for name in ("event_user_id", "account_store", "official_client"):
            patcher = patch.object(endfield, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.yielded = []

    async def run_command(self, entries):
        async def candidates():
            for entry in entries:
                self.yielded.append(entry)
                yield entry

        with patch.object(endfield.random_wiki_service, "candidates", candidates):
            await endfield._handle_command(
                self.matcher, object(), parse_command("随机")
            )

    async def test_routes_without_account_access_and_sends_one_entry_only(self):
        selected = candidate()
        await self.run_command([selected, candidate("weapon")])
        self.assertEqual(self.yielded, [selected])
        self.patches["_render_candidate"].assert_awaited_once_with(selected)
        self.patches["_finish_pngs"].assert_awaited_once_with(self.matcher, (b"card",))
        self.patches["_handle_personal_command"].assert_not_awaited()

    async def test_private_or_unknown_entries_never_reach_any_renderer(self):
        await self.run_command(
            [
                candidate("account"),
                candidate("currency_log"),
                candidate("account_investment"),
                candidate("unknown"),
            ]
        )
        self.patches["_render_candidate"].assert_not_awaited()
        self.patches["_finish_pngs"].assert_not_awaited()
        self.assertIn("公开 Wiki 资料暂时不可用", self.matcher.finish.call_args.args[0])

    async def test_render_failure_and_empty_card_try_another_public_entry(self):
        self.patches["_render_candidate"].side_effect = [
            ValueError("missing"),
            None,
            (b"page1", b"page2"),
        ]
        await self.run_command([candidate(), candidate("weapon"), candidate("stage")])
        self.patches["_finish_pngs"].assert_awaited_once_with(
            self.matcher, (b"page1", b"page2")
        )

    async def test_send_failure_does_not_select_or_send_another_entry(self):
        self.patches["_finish_pngs"].side_effect = RuntimeError("send failed")
        await self.run_command([candidate(), candidate("weapon")])
        self.assertEqual(len(self.yielded), 1)
        self.patches["_finish_pngs"].assert_awaited_once()
        self.matcher.finish.assert_awaited_once_with("图片发送失败，请稍后再试。")

    async def test_finish_exit_signal_is_not_treated_as_a_failure(self):
        self.patches["_finish_pngs"].side_effect = endfield._ExitException
        with self.assertRaises(endfield._ExitException):
            await self.run_command([candidate(), candidate("weapon")])
        self.assertEqual(len(self.yielded), 1)
        self.matcher.finish.assert_not_awaited()

    async def test_render_cancellation_is_not_swallowed(self):
        self.patches["_render_candidate"].side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.run_command([candidate(), candidate("weapon")])
        self.assertEqual(len(self.yielded), 1)
        self.patches["_finish_pngs"].assert_not_awaited()

    async def test_empty_pool_has_actionable_reply_without_binding_prompt(self):
        await self.run_command([])
        self.matcher.finish.assert_awaited_once_with(
            "公开 Wiki 资料暂时不可用，请稍后再试。"
        )
        self.patches["_handle_personal_command"].assert_not_awaited()
