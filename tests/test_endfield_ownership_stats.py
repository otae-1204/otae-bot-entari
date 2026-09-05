from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import plugins.endfield.handlers as endfield_plugin
from loguru import logger
from satori.model import IterablePageResult, PageResult
from plugins.endfield.account.client import EndfieldAPIError
from plugins.endfield.account.crypto import CredentialCipher
from plugins.endfield.account.store import (
    EndfieldStore,
    OperatorCatalogEntry,
    OperatorSnapshotMember,
    RoleCandidate,
)
from plugins.endfield.catalog.commands import parse_command
from plugins.endfield.ownership.service import (
    CATALOG_MAPPING_REVISION,
    GroupMemberListError,
    OwnershipRefreshIssue,
    OwnershipRefreshResult,
    OwnershipStatsService,
    build_operator_catalog,
    collect_group_member_ids,
    member_has_group_admin_role,
    parse_operator_snapshot,
    register_ownership_stats_renderer,
    render_ownership_stats,
)


NOW = 2_000_000


def _catalog() -> tuple[OperatorCatalogEntry, ...]:
    return (
        OperatorCatalogEntry("op-a", "chr_a", "甲", 6, "先锋", 1, version="v1"),
        OperatorCatalogEntry("op-b", "chr_b", "乙", 6, "先锋", 2, version="v1"),
        OperatorCatalogEntry("op-c", "chr_c", "丙", 5, "术师", 3, version="v1"),
    )


class OwnershipCommandParserTests(unittest.TestCase):
    def test_aliases_defaults_and_swappable_refresh_scope(self):
        for alias in ("持有率", "干员占比", "干员统计"):
            parsed = parse_command(alias)
            self.assertEqual((parsed.action, parsed.scope), ("ownership_stats", "auto"))

        first = parse_command("持有率 刷新 群内")
        second = parse_command("持有率 全局 刷新")
        self.assertEqual((first.action, first.scope), ("ownership_refresh", "group"))
        self.assertEqual((second.action, second.scope), ("ownership_refresh", "global"))
        self.assertTrue(parse_command("持有率 群内 全局").error)
        invalid = parse_command("持有率 未知范围")
        self.assertIn("/ef 持有率", invalid.error)
        self.assertNotIn("/zmd", invalid.error)


class OwnershipCatalogAndSnapshotTests(unittest.TestCase):
    def test_akedata_catalog_uses_md5_and_keeps_endministrator_variants(self):
        characters = {
            "male": {
                "charId": "chr_0002_endminm",
                "name": {"id": "name-m"},
                "rarity": 6,
                "profession": 0,
                "sortOrder": 1,
            },
            "female": {
                "charId": "chr_0003_endminf",
                "name": {"id": "name-f"},
                "rarity": 6,
                "profession": 0,
                "sortOrder": 2,
            },
            "account-alias": {
                "charId": "chr_9000_endmin",
                "name": {"id": "name-f"},
                "rarity": 6,
                "profession": 0,
                "sortOrder": 0,
            },
        }
        professions = {"0": {"profession": 0, "name": {"id": "profession-0"}}}
        i18n = {
            "name-m": "管理员",
            "name-f": "管理员",
            "profession-0": "先锋",
        }

        catalog = build_operator_catalog(characters, professions, i18n, version="v1")

        self.assertEqual(len(catalog), 2)
        self.assertNotEqual(catalog[0].operator_key, catalog[1].operator_key)
        male = next(item for item in catalog if item.source_id == "chr_0002_endminm")
        female = next(item for item in catalog if item.source_id == "chr_0003_endminf")
        self.assertEqual(
            male.operator_key,
            hashlib.md5(male.source_id.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            female.operator_key,
            hashlib.md5(female.source_id.encode("utf-8")).hexdigest(),
        )
        self.assertEqual({item.name for item in catalog}, {"管理员·男", "管理员·女"})
        self.assertEqual({item.profession for item in catalog}, {"先锋"})

        for raw_id in (female.operator_key, female.source_id):
            members, _ = parse_operator_snapshot(
                {
                    "base": {"charNum": 1},
                    "chars": [{"charData": {"id": raw_id}, "potentialLevel": 0}],
                },
                catalog,
            )
            self.assertEqual(members[0].operator_key, female.operator_key)

    def test_shared_endministrator_id_uses_entry_gender_then_base_gender(self):
        catalog = (
            OperatorCatalogEntry(
                hashlib.md5(b"chr_0002_endminm").hexdigest(),
                "chr_0002_endminm",
                "管理员·男",
                6,
                "先锋",
                1,
            ),
            OperatorCatalogEntry(
                hashlib.md5(b"chr_0003_endminf").hexdigest(),
                "chr_0003_endminf",
                "管理员·女",
                6,
                "先锋",
                2,
            ),
        )
        shared_key = hashlib.md5(b"chr_9000_endmin").hexdigest()
        expected = {
            "CHAR_GENDER_MALE": hashlib.md5(b"chr_0002_endminm").hexdigest(),
            "CHAR_GENDER_FEMALE": hashlib.md5(b"chr_0003_endminf").hexdigest(),
        }
        for gender, operator_key in expected.items():
            members, _ = parse_operator_snapshot(
                {
                    "base": {
                        "charNum": 1,
                        "gender": 2 if gender == "CHAR_GENDER_MALE" else 1,
                    },
                    "chars": [
                        {
                            "charData": {"id": shared_key},
                            "gender": gender,
                            "potentialLevel": 2,
                        }
                    ],
                },
                catalog,
            )
            self.assertEqual(members[0].operator_key, operator_key)

        members, _ = parse_operator_snapshot(
            {
                "base": {"charNum": 1, "gender": 1},
                "chars": [{"charData": {"id": shared_key}, "potentialLevel": 2}],
            },
            catalog,
        )
        self.assertEqual(members[0].operator_key, expected["CHAR_GENDER_MALE"])
        legacy_catalog = (
            catalog[0],
            OperatorCatalogEntry(
                shared_key,
                "chr_0003_endminf",
                "管理员",
                6,
                "先锋",
                2,
            ),
        )
        members, _ = parse_operator_snapshot(
            {
                "base": {"charNum": 1, "gender": 2},
                "chars": [
                    {
                        "charData": {"id": shared_key},
                        "gender": "CHAR_GENDER_FEMALE",
                        "potentialLevel": 2,
                    }
                ],
            },
            legacy_catalog,
        )
        self.assertEqual(members[0].operator_key, expected["CHAR_GENDER_FEMALE"])
        self.assertEqual(members[0].name, "管理员·女")
        with self.assertRaisesRegex(ValueError, "缺少管理员性别"):
            parse_operator_snapshot(
                {
                    "base": {"charNum": 1},
                    "chars": [{"charData": {"id": shared_key}, "potentialLevel": 2}],
                },
                catalog,
            )

    def test_snapshot_requires_complete_nonempty_roster_and_maps_exact_id(self):
        key = hashlib.md5(b"chr_a").hexdigest()
        catalog = (OperatorCatalogEntry(key, "chr_a", "甲", 6, "先锋", 1),)
        detail = {
            "base": {"charNum": 1, "saveTime": "123"},
            "chars": [{"charData": {"id": key}, "potentialLevel": 4}],
        }

        members, saved_at = parse_operator_snapshot(detail, catalog)

        self.assertEqual((members[0].operator_key, members[0].potential_level), (key, 4))
        self.assertEqual(saved_at, 123)
        with self.assertRaisesRegex(ValueError, "不完整"):
            parse_operator_snapshot({**detail, "base": {"charNum": 2}}, catalog)
        with self.assertRaisesRegex(ValueError, "有效干员"):
            parse_operator_snapshot({"base": {"charNum": 0}, "chars": []}, catalog)


class OwnershipStoreAndReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "endfield.db"
        self.store = EndfieldStore(self.path)
        self.cipher = CredentialCipher(b"k" * 32)
        self.store.replace_operator_catalog(_catalog(), "v1", updated_at=NOW)
        self.service = OwnershipStatsService(self.store, object())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _bind(self, qq: str, role_id: str, server_id: str, server_name: str = "国服"):
        return self.store.bind_roles(
            qq,
            f"token-{qq}",
            [RoleCandidate(f"binding-{qq}", role_id, server_id, qq, server_name)],
            self.cipher,
        )[0]

    def test_failed_or_invalid_update_preserves_last_success_and_restart_is_idempotent(self):
        role = self._bind("qq", "role", "1")
        self.store.replace_operator_snapshot(
            role,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=100,
        )
        self.store.record_operator_snapshot_failure(role, "cn", "auth failed", attempted_at=200)
        with self.assertRaisesRegex(ValueError, "不能为空"):
            self.store.replace_operator_snapshot(role, "cn", [], fetched_at=300)

        snapshot = self.store.list_operator_snapshots()[0]
        self.assertEqual((snapshot.fetched_at, snapshot.last_attempt_at), (100, 200))
        self.assertEqual([item.operator_key for item in snapshot.members], ["op-a"])
        self.assertIn("auth failed", snapshot.last_error)

        self.store.close()
        self.store = EndfieldStore(self.path)
        self.assertEqual(self.store.list_operator_snapshots()[0].fetched_at, 100)
        role = self.store.list_roles("qq")[0]
        self.store.replace_operator_snapshot(
            role,
            "cn",
            [OperatorSnapshotMember("op-b", 3)],
            fetched_at=400,
        )
        replaced = self.store.list_operator_snapshots()[0]
        self.assertEqual(replaced.fetched_at, 400)
        self.assertEqual([item.operator_key for item in replaced.members], ["op-b"])

    def test_snapshot_failure_backoff_columns_migrate_and_success_resets_them(self):
        role = self._bind("qq", "role", "1")
        self.store.record_operator_snapshot_failure(
            role,
            "cn",
            "temporary",
            attempted_at=100,
            retry_after_seconds=900,
        )
        failed = self.store.list_operator_snapshots()[0]
        self.assertEqual((failed.failure_count, failed.next_attempt_at), (1, 1000))

        self.store.close()
        self.store = EndfieldStore(self.path)
        migrated = self.store.list_operator_snapshots()[0]
        self.assertEqual((migrated.failure_count, migrated.next_attempt_at), (1, 1000))

        role = self.store.list_roles("qq")[0]
        self.store.replace_operator_snapshot(
            role,
            "cn",
            [OperatorSnapshotMember("op-a", 1)],
            fetched_at=1100,
        )
        succeeded = self.store.list_operator_snapshots()[0]
        self.assertEqual((succeeded.failure_count, succeeded.next_attempt_at), (0, 0))

    def test_existing_snapshot_table_migrates_refresh_control_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE operator_roster_snapshots (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT 'cn',
                    fetched_at INTEGER NOT NULL DEFAULT 0,
                    game_saved_at INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    operator_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(role_id, server_id)
                )
                """
            )
            connection.commit()
            connection.close()

            migrated = EndfieldStore(path)
            columns = {
                row["name"]
                for row in migrated.conn.execute(
                    "PRAGMA table_info(operator_roster_snapshots)"
                ).fetchall()
            }
            migrated.close()

        self.assertTrue(
            {"failure_count", "next_attempt_at", "roster_fingerprint"}.issubset(columns)
        )

    def test_unchanged_roster_only_updates_snapshot_header(self):
        role = self._bind("qq", "role", "1")
        members = [OperatorSnapshotMember("op-a", 1)]
        self.store.replace_operator_snapshot(role, "cn", members, fetched_at=100)
        before = self.store.conn.total_changes

        self.store.replace_operator_snapshot(role, "cn", members, fetched_at=200)

        self.assertEqual(self.store.conn.total_changes - before, 1)
        snapshot = self.store.list_operator_snapshots()[0]
        self.assertEqual(snapshot.fetched_at, 200)
        self.assertTrue(snapshot.roster_fingerprint)

    def test_duplicate_bindings_are_one_sample_and_snapshot_lives_until_last_unbind(self):
        first = self._bind("qq-1", "same-role", "1")
        self._bind("qq-2", "same-role", "1")
        self.store.replace_operator_snapshot(
            first,
            "cn",
            [OperatorSnapshotMember("op-a", 1)],
            fetched_at=NOW,
        )

        report = self.service.build_report("global", self.store.list_all_roles(), now=NOW)
        self.assertEqual(report.segment("all").eligible_sample_count, 1)
        self.assertEqual(report.segment("all").valid_sample_count, 1)

        self.store.unbind("qq-1", "1")
        self.assertEqual(len(self.store.list_operator_snapshots()), 1)
        self.store.unbind("qq-2", "1")
        self.assertEqual(self.store.list_operator_snapshots(), [])

    def test_regions_ttl_sorting_potential_and_collection_formulas(self):
        cn = self._bind("cn", "cn-role", "1")
        asia = self._bind("asia", "asia-role", "asia", "Asia")
        stale = self._bind("stale", "stale-role", "2")
        self._bind("duplicate", "cn-role", "1")
        self.store.replace_operator_snapshot(
            cn,
            "cn",
            [OperatorSnapshotMember("op-a", 0), OperatorSnapshotMember("op-b", None)],
            fetched_at=NOW - 47 * 60 * 60,
        )
        self.store.replace_operator_snapshot(
            asia,
            "asia",
            [OperatorSnapshotMember("op-b", 5)],
            fetched_at=NOW,
        )
        self.store.replace_operator_snapshot(
            stale,
            "cn",
            [OperatorSnapshotMember("op-c", 2)],
            fetched_at=NOW - 48 * 60 * 60 - 1,
        )

        report = self.service.build_report("global", self.store.list_all_roles(), now=NOW)
        total = report.segment("all")
        cn_segment = report.segment("cn")
        asia_segment = report.segment("asia")

        self.assertEqual(
            (total.eligible_sample_count, total.valid_sample_count, total.excluded_sample_count),
            (3, 2, 1),
        )
        self.assertEqual((cn_segment.eligible_sample_count, cn_segment.valid_sample_count), (2, 1))
        self.assertEqual((asia_segment.eligible_sample_count, asia_segment.valid_sample_count), (1, 1))
        self.assertEqual([item.operator_key for item in total.operators], ["op-b", "op-a", "op-c"])

        op_b = total.operators[0]
        self.assertEqual((op_b.owned_count, op_b.sample_count, op_b.ownership_rate), (2, 2, 1.0))
        buckets = {item.key: item.count for item in op_b.potential_buckets}
        self.assertEqual(buckets["potential_5"], 1)
        self.assertEqual(buckets["unknown"], 1)
        self.assertEqual(sum(buckets.values()), total.valid_sample_count)
        self.assertEqual(
            [item.label for item in op_b.potential_buckets],
            ["未持有", "潜能 0", "潜能 1", "潜能 2", "潜能 3", "潜能 4", "潜能 5", "未知"],
        )

        profession = next(item for item in total.professions if item.label == "先锋")
        self.assertEqual((profession.owned_slots, profession.possible_slots), (3, 4))
        self.assertEqual(profession.collection_rate, 0.75)
        rarity = next(item for item in total.rarities if item.label == "6")
        self.assertEqual(rarity.collection_rate, 0.75)

    def test_empty_samples_produce_not_calculable_rates(self):
        report = self.service.build_report("global", [], now=NOW)
        total = report.segment("all")
        self.assertEqual(total.valid_sample_count, 0)
        self.assertTrue(all(item.ownership_rate is None for item in total.operators))
        self.assertTrue(all(item.collection_rate is None for item in total.professions))

    def test_report_separates_manager_sexes_and_excludes_legacy_shared_id(self):
        male_key = hashlib.md5(b"chr_0002_endminm").hexdigest()
        female_key = hashlib.md5(b"chr_0003_endminf").hexdigest()
        shared_key = hashlib.md5(b"chr_9000_endmin").hexdigest()
        self.store.replace_operator_catalog(
            (
                OperatorCatalogEntry(male_key, "chr_0002_endminm", "管理员·男", 6, "先锋", 1),
                OperatorCatalogEntry(female_key, "chr_0003_endminf", "管理员·女", 6, "先锋", 2),
            ),
            "v2",
            updated_at=NOW,
        )
        male = self._bind("male", "male-role", "1")
        female = self._bind("female", "female-role", "1")
        legacy = self._bind("legacy", "legacy-role", "1")
        self.store.replace_operator_snapshot(
            male,
            "cn",
            [OperatorSnapshotMember(male_key, 2)],
            fetched_at=NOW,
        )
        self.store.replace_operator_snapshot(
            female,
            "cn",
            [OperatorSnapshotMember(female_key, 3)],
            fetched_at=NOW,
        )
        self.store.replace_operator_snapshot(
            legacy,
            "cn",
            [OperatorSnapshotMember(shared_key, 2)],
            fetched_at=NOW,
        )

        report = self.service.build_report("global", self.store.list_all_roles(), now=NOW)
        total = report.segment("all")
        managers = {item.source_id: item for item in total.operators}

        self.assertEqual(
            (total.eligible_sample_count, total.valid_sample_count, total.excluded_sample_count),
            (3, 2, 1),
        )
        self.assertEqual(managers["chr_0002_endminm"].ownership_rate, 0.5)
        self.assertEqual(managers["chr_0003_endminf"].ownership_rate, 0.5)
        self.assertNotIn(shared_key, {item.operator_key for item in total.operators})


class OwnershipRefreshAndGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EndfieldStore(Path(self.temp.name) / "endfield.db")
        self.cipher = CredentialCipher(b"z" * 32)
        self.store.replace_operator_catalog(_catalog(), "v1", updated_at=NOW)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_refresh_tries_next_binding_credential_after_auth_failure(self):
        self.store.bind_roles(
            "good",
            "good-token",
            [RoleCandidate("good", "same", "1", "good")],
            self.cipher,
        )
        self.store.bind_roles(
            "bad",
            "bad-token",
            [RoleCandidate("bad", "same", "1", "bad")],
            self.cipher,
        )

        class Client:
            def __init__(self):
                self.tokens = []

            async def card_detail(self, token, _role):
                self.tokens.append(token)
                if token == "bad-token":
                    raise EndfieldAPIError("账号授权", "401")
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 2}],
                }

        client = Client()
        service = OwnershipStatsService(self.store, client)
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=True)
            )

        self.assertEqual((result.attempted, result.succeeded, result.failed), (1, 1, 0))
        self.assertEqual(client.tokens, ["bad-token", "good-token"])
        self.assertEqual(self.store.list_operator_snapshots()[0].members[0].potential_level, 2)

    def test_systemic_405_does_not_try_duplicate_binding_credentials(self):
        self.store.bind_roles(
            "first",
            "first-token",
            [RoleCandidate("first", "same", "1", "first")],
            self.cipher,
        )
        self.store.bind_roles(
            "second",
            "second-token",
            [RoleCandidate("second", "same", "1", "second")],
            self.cipher,
        )

        class Client:
            def __init__(self):
                self.tokens = []

            async def card_detail(self, token, _role):
                self.tokens.append(token)
                raise EndfieldAPIError("获取社区凭据", "405", "官方服务暂时不可用")

        client = Client()
        service = OwnershipStatsService(
            self.store,
            client,
            concurrency=1,
            systemic_failure_threshold=3,
            systemic_backoff_base_seconds=0,
        )
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=True)
            )

        self.assertEqual((result.attempted, result.failed), (1, 1))
        self.assertEqual(len(client.tokens), 1)

    def test_due_refresh_only_fetches_missing_or_older_than_twenty_four_hours(self):
        fresh = self.store.bind_roles(
            "fresh",
            "fresh-token",
            [RoleCandidate("fresh", "fresh-role", "1", "fresh")],
            self.cipher,
        )[0]
        due = self.store.bind_roles(
            "due",
            "due-token",
            [RoleCandidate("due", "due-role", "1", "due")],
            self.cipher,
        )[0]
        self.store.replace_operator_snapshot(
            fresh,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=NOW - 23 * 60 * 60,
        )
        self.store.replace_operator_snapshot(
            due,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=NOW - 25 * 60 * 60,
        )

        class Client:
            def __init__(self):
                self.roles = []

            async def card_detail(self, _token, role):
                self.roles.append(role.role_id)
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        client = Client()
        service = OwnershipStatsService(self.store, client)
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=False)
            )

        self.assertEqual((result.attempted, result.succeeded), (1, 1))
        self.assertEqual(client.roles, ["due-role"])

    def test_due_refresh_retries_a_failed_attempt_even_when_old_success_is_fresh(self):
        role = self.store.bind_roles(
            "qq",
            "token",
            [RoleCandidate("binding", "role", "1", "role")],
            self.cipher,
        )[0]
        self.store.replace_operator_snapshot(
            role,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=NOW - 60,
        )
        self.store.record_operator_snapshot_failure(
            role,
            "cn",
            "temporary",
            attempted_at=NOW - 30,
            retry_after_seconds=10,
        )

        class Client:
            async def card_detail(self, _token, _role):
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        service = OwnershipStatsService(self.store, Client())
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(service.refresh_due(self.cipher, now=NOW))

        self.assertEqual((result.attempted, result.succeeded), (1, 1))
        snapshot = self.store.list_operator_snapshots()[0]
        self.assertEqual((snapshot.failure_count, snapshot.next_attempt_at), (0, 0))

    def test_due_refresh_rebuilds_legacy_shared_manager_snapshot(self):
        role = self.store.bind_roles(
            "legacy",
            "legacy-token",
            [RoleCandidate("legacy", "legacy-role", "1", "legacy")],
            self.cipher,
        )[0]
        shared_key = hashlib.md5(b"chr_9000_endmin").hexdigest()
        male_key = hashlib.md5(b"chr_0002_endminm").hexdigest()
        self.store.replace_operator_snapshot(
            role,
            "cn",
            [OperatorSnapshotMember(shared_key, 2)],
            fetched_at=NOW,
        )

        class Client:
            async def card_detail(self, _token, _role):
                return {
                    "base": {"charNum": 1, "saveTime": NOW, "gender": 1},
                    "chars": [
                        {
                            "charData": {"id": shared_key},
                            "gender": "CHAR_GENDER_MALE",
                            "potentialLevel": 2,
                        }
                    ],
                }

        service = OwnershipStatsService(self.store, Client())
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=False)
            )

        self.assertEqual((result.attempted, result.succeeded), (1, 1))
        self.assertEqual(
            [item.operator_key for item in self.store.list_operator_snapshots()[0].members],
            [male_key],
        )

    def test_catalog_refresh_rebuilds_same_version_when_identity_mapping_changes(self):
        incoming = (
            OperatorCatalogEntry(
                hashlib.md5(b"chr_a").hexdigest(),
                "chr_a",
                "甲",
                6,
                "先锋",
                1,
                source="akedata",
                version="v1",
            ),
        )
        service = OwnershipStatsService(self.store, object())
        manifest = {"latest": "v1", "versions": [{"id": "v1", "tableCfgPath": "tables"}]}
        with (
            mock.patch(
                "plugins.endfield.ownership.service.fetch_akedata_manifest",
                mock.AsyncMock(return_value=manifest),
            ),
            mock.patch(
                "plugins.endfield.ownership.service.fetch_operator_catalog",
                mock.AsyncMock(return_value=("v1", incoming)),
            ),
        ):
            updated = asyncio.run(service.refresh_catalog())

        self.assertTrue(updated)
        self.assertEqual(self.store.list_operator_catalog()[0].operator_key, incoming[0].operator_key)

    def test_catalog_refresh_uses_manifest_and_skips_unchanged_large_tables(self):
        self.store.replace_operator_catalog(
            _catalog(),
            "v1",
            updated_at=NOW,
            revision=CATALOG_MAPPING_REVISION,
        )
        service = OwnershipStatsService(self.store, object())
        manifest = {"latest": "v1", "versions": [{"id": "v1", "tableCfgPath": "tables"}]}
        with (
            mock.patch(
                "plugins.endfield.ownership.service.fetch_akedata_manifest",
                mock.AsyncMock(return_value=manifest),
            ) as fetch_manifest,
            mock.patch(
                "plugins.endfield.ownership.service.fetch_operator_catalog",
                mock.AsyncMock(),
            ) as fetch_tables,
        ):
            first = asyncio.run(service.refresh_catalog())
            second = asyncio.run(service.refresh_catalog())

        self.assertFalse(first)
        self.assertFalse(second)
        fetch_manifest.assert_awaited_once()
        fetch_tables.assert_not_awaited()

    def test_due_refresh_limits_oldest_ready_roles_and_defers_backoff(self):
        oldest = self.store.bind_roles(
            "oldest",
            "token-oldest",
            [RoleCandidate("oldest", "oldest", "1", "oldest")],
            self.cipher,
        )[0]
        waiting = self.store.bind_roles(
            "waiting",
            "token-waiting",
            [RoleCandidate("waiting", "waiting", "1", "waiting")],
            self.cipher,
        )[0]
        missing = self.store.bind_roles(
            "missing",
            "token-missing",
            [RoleCandidate("missing", "missing", "1", "missing")],
            self.cipher,
        )[0]
        self.store.replace_operator_snapshot(
            oldest,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=NOW - 30 * 60 * 60,
        )
        self.store.replace_operator_snapshot(
            waiting,
            "cn",
            [OperatorSnapshotMember("op-a", 0)],
            fetched_at=NOW - 26 * 60 * 60,
        )
        self.store.record_operator_snapshot_failure(
            waiting,
            "cn",
            "temporary",
            attempted_at=NOW - 10,
            retry_after_seconds=900,
        )

        class Client:
            def __init__(self):
                self.roles = []

            async def card_detail(self, _token, role):
                self.roles.append(role.role_id)
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        client = Client()
        service = OwnershipStatsService(self.store, client, batch_size=2)
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(service.refresh_due(self.cipher, now=NOW))

        self.assertEqual(client.roles, [missing.role_id, oldest.role_id])
        self.assertEqual((result.eligible, result.attempted, result.deferred), (3, 2, 1))
        self.assertEqual((result.requested, result.succeeded), (2, 2))

    def test_overlapping_due_batches_recheck_after_lock_and_do_not_refetch(self):
        self.store.bind_roles(
            "qq",
            "token",
            [RoleCandidate("binding", "role", "1", "role")],
            self.cipher,
        )

        class Client:
            def __init__(self):
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def card_detail(self, _token, _role):
                self.calls += 1
                self.started.set()
                await self.release.wait()
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        async def scenario():
            client = Client()
            service = OwnershipStatsService(self.store, client)
            with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
                first = asyncio.create_task(
                    service.refresh_roles(
                        self.store.list_all_roles(),
                        self.cipher,
                        now=NOW,
                        force=False,
                    )
                )
                await client.started.wait()
                second = asyncio.create_task(
                    service.refresh_roles(
                        self.store.list_all_roles(),
                        self.cipher,
                        now=NOW,
                        force=False,
                    )
                )
                client.release.set()
                return client, await first, await second

        client, first, second = asyncio.run(scenario())
        self.assertEqual(client.calls, 1)
        self.assertEqual(first.succeeded, 1)
        self.assertEqual(second.attempted, 0)

    def test_refresh_stops_after_repeated_systemic_community_failures(self):
        for index in range(8):
            self.store.bind_roles(
                f"qq-{index}",
                f"token-{index}",
                [RoleCandidate(f"binding-{index}", f"role-{index}", "1", f"role-{index}")],
                self.cipher,
            )

        class Client:
            def __init__(self):
                self.calls = 0

            async def card_detail(self, _token, _role):
                self.calls += 1
                raise EndfieldAPIError("获取社区凭据", "405", "官方服务暂时不可用")

        client = Client()
        service = OwnershipStatsService(
            self.store,
            client,
            concurrency=1,
            systemic_failure_threshold=3,
            systemic_backoff_base_seconds=0,
        )
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=True)
            )

        self.assertEqual((result.attempted, result.failed, result.skipped), (8, 3, 5))
        self.assertEqual(client.calls, 3)
        self.assertEqual(result.requested, 3)
        self.assertIn("获取社区凭据（405）", {item.label for item in result.issues})
        self.assertTrue(result.stopped_early)
        self.assertIn("405", result.stop_reason)
        self.assertEqual(len(self.store.list_operator_snapshots()), 3)

    def test_systemic_failure_window_is_not_reset_by_interleaved_success(self):
        for index in range(8):
            self.store.bind_roles(
                f"qq-{index}",
                f"token-{index}",
                [RoleCandidate(f"binding-{index}", f"role-{index}", "1", f"role-{index}")],
                self.cipher,
            )

        class Client:
            def __init__(self):
                self.calls = 0

            async def card_detail(self, _token, _role):
                self.calls += 1
                if self.calls in {1, 3, 5}:
                    raise EndfieldAPIError("获取社区凭据", "405", "unavailable")
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        client = Client()
        service = OwnershipStatsService(
            self.store,
            client,
            concurrency=1,
            systemic_failure_threshold=3,
            systemic_backoff_base_seconds=0,
        )
        with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, now=NOW, force=True)
            )

        self.assertEqual(client.calls, 5)
        self.assertEqual((result.failed, result.succeeded, result.skipped), (3, 2, 3))
        self.assertTrue(result.stopped_early)

    def test_successful_roles_use_their_own_completion_timestamps(self):
        for index in range(2):
            self.store.bind_roles(
                f"qq-{index}",
                f"token-{index}",
                [RoleCandidate(f"binding-{index}", f"role-{index}", "1", f"role-{index}")],
                self.cipher,
            )

        class Client:
            async def card_detail(self, _token, _role):
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        service = OwnershipStatsService(self.store, Client(), concurrency=1)
        with (
            mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)),
            mock.patch(
                "plugins.endfield.ownership.service.time.time",
                side_effect=[100, 101, 102, 103],
            ),
        ):
            result = asyncio.run(
                service.refresh_roles(self.store.list_all_roles(), self.cipher, force=True)
            )

        self.assertEqual(result.started_at, 100)
        self.assertEqual(
            {item.fetched_at for item in self.store.list_operator_snapshots()},
            {101, 102},
        )

    def test_refresh_result_reports_anonymous_batch_and_community_metrics(self):
        self.store.bind_roles(
            "qq",
            "token",
            [RoleCandidate("binding", "role", "1", "role")],
            self.cipher,
        )

        class Client:
            def __init__(self):
                self.metrics = {
                    "cache_hits": 0,
                    "singleflight_reuses": 0,
                    "exchange_attempts": 0,
                    "exchange_succeeded": 0,
                    "exchange_failed": 0,
                    "circuit_rejections": 0,
                }

            def community_context_metrics(self):
                return dict(self.metrics)

            async def card_detail(self, _token, _role):
                self.metrics["cache_hits"] += 1
                self.metrics["exchange_attempts"] += 1
                self.metrics["exchange_succeeded"] += 1
                return {
                    "base": {"charNum": 1, "saveTime": NOW},
                    "chars": [{"charData": {"id": "op-a"}, "potentialLevel": 1}],
                }

        client = Client()
        service = OwnershipStatsService(self.store, client)
        messages: list[str] = []
        sink = logger.add(messages.append, format="{message}", level="INFO")
        try:
            with mock.patch.object(service, "refresh_catalog", mock.AsyncMock(return_value=False)):
                result = asyncio.run(
                    service.refresh_roles(
                        self.store.list_all_roles(),
                        self.cipher,
                        now=NOW,
                        force=True,
                        trigger="manual-global",
                    )
                )
        finally:
            logger.remove(sink)

        self.assertTrue(result.batch_id)
        self.assertEqual(result.trigger, "manual-global")
        self.assertEqual(
            (
                result.cache_hits,
                result.exchange_attempts,
                result.exchange_succeeded,
                result.exchange_failed,
                result.circuit_rejections,
            ),
            (1, 1, 1, 0, 0),
        )
        batch_log = next(
            message for message in messages if "refresh batch complete" in message
        )
        self.assertIn(f"batch_id={result.batch_id}", batch_log)
        self.assertIn("trigger=manual-global", batch_log)
        self.assertIn("cache_hits=1", batch_log)
        self.assertIn("exchange_attempts=1", batch_log)
        self.assertIn("issues=none", batch_log)

    def test_group_member_pagination_failure_and_admin_detection(self):
        class Bot:
            def guild_member_list(self, guild_id):
                self.guild_id = guild_id

                async def page(next_token):
                    if next_token is None:
                        return PageResult(
                            [SimpleNamespace(user=SimpleNamespace(id="100"))],
                            "next-page",
                        )
                    return PageResult([{"user": {"id": "200"}}])

                return IterablePageResult(page)

        bot = Bot()
        self.assertEqual(asyncio.run(collect_group_member_ids(bot, "group")), {"100", "200"})
        self.assertEqual(bot.guild_id, "group")
        self.assertTrue(member_has_group_admin_role(SimpleNamespace(is_owner=True, roles=[])))
        self.assertTrue(
            member_has_group_admin_role(
                SimpleNamespace(roles=[SimpleNamespace(id="admin", name="群管理员")])
            )
        )
        self.assertFalse(member_has_group_admin_role(SimpleNamespace(roles=[])))

        class BrokenBot:
            async def guild_member_list(self, guild_id):
                raise RuntimeError(guild_id)

        with mock.patch(
            "plugins.endfield.ownership.service.call_onebot_action",
            mock.AsyncMock(side_effect=RuntimeError("unsupported")),
        ):
            with self.assertRaises(GroupMemberListError):
                asyncio.run(collect_group_member_ids(BrokenBot(), "group"))

        fallback = mock.AsyncMock(
            return_value={"status": "ok", "data": [{"user_id": 300}, {"user_id": "400"}]}
        )
        with mock.patch("plugins.endfield.ownership.service.call_onebot_action", fallback):
            self.assertEqual(
                asyncio.run(collect_group_member_ids(object(), "123")),
                {"300", "400"},
            )
        fallback.assert_awaited_once_with(
            mock.ANY,
            "get_group_member_list",
            group_id=123,
            no_cache=True,
        )

    def test_renderer_is_a_backend_registration_hook(self):
        report = SimpleNamespace(scope="global")
        register_ownership_stats_renderer(lambda value: ("rendered", value))
        try:
            result = asyncio.run(render_ownership_stats(report))
        finally:
            register_ownership_stats_renderer(None)
        self.assertEqual(result, ("rendered", report))

    def test_command_permission_boundaries_and_private_group_error(self):
        class Matcher:
            async def finish(self, message=None):
                return message

        matcher = Matcher()
        private_event = SimpleNamespace(user=SimpleNamespace(id="user"))
        message = asyncio.run(
            endfield_plugin._handle_ownership_stats(
                matcher,
                private_event,
                parse_command("持有率 群内"),
                bot=object(),
            )
        )
        self.assertIn("私聊", message)

        global_refresh = parse_command("持有率 刷新 全局")
        with mock.patch.object(endfield_plugin.Config, "SUPERUSERS", []):
            denied = asyncio.run(
                endfield_plugin._handle_ownership_stats(
                    matcher,
                    private_event,
                    global_refresh,
                    bot=object(),
                )
            )
        self.assertIn("SUPERUSER", denied)

        group_event = SimpleNamespace(
            user=SimpleNamespace(id="member"),
            guild=SimpleNamespace(id="group"),
            member=SimpleNamespace(roles=[]),
        )
        with mock.patch.object(endfield_plugin.Config, "SUPERUSERS", []):
            denied = asyncio.run(
                endfield_plugin._handle_ownership_stats(
                    matcher,
                    group_event,
                    parse_command("持有率 刷新 群内"),
                    bot=object(),
                )
            )
        self.assertIn("群管理员", denied)

    def test_refresh_returns_text_without_building_or_rendering_report(self):
        class Matcher:
            async def finish(self, message=None):
                return message

        event = SimpleNamespace(user=SimpleNamespace(id="superuser"))
        roles = [SimpleNamespace(role_id="role", server_id="1")]
        refresh = OwnershipRefreshResult(
            attempted=2,
            succeeded=1,
            failed=1,
            skipped=0,
            catalog_updated=False,
            started_at=100,
            finished_at=111,
            eligible=2,
            requested=2,
            catalog_checked=True,
            issues=(OwnershipRefreshIssue("api:账号授权:401", "账号授权（401）", 1),),
        )
        with (
            mock.patch.object(endfield_plugin.Config, "SUPERUSERS", ["superuser"]),
            mock.patch.object(
                endfield_plugin.account_store,
                "list_all_roles",
                return_value=roles,
            ),
            mock.patch.object(endfield_plugin.CredentialCipher, "from_env", return_value=self.cipher),
            mock.patch.object(
                endfield_plugin.ownership_stats_service,
                "refresh_roles",
                mock.AsyncMock(return_value=refresh),
            ) as refresh_roles,
            mock.patch.object(
                endfield_plugin.ownership_stats_service,
                "build_report",
            ) as build_report,
            mock.patch.object(
                endfield_plugin,
                "render_ownership_stats",
                mock.AsyncMock(),
            ) as renderer,
        ):
            result = asyncio.run(
                endfield_plugin._handle_ownership_stats(
                    Matcher(),
                    event,
                    parse_command("持有率 刷新 全局"),
                    bot=object(),
                )
            )

        self.assertIn("全局持有率刷新完成", result)
        self.assertIn("候选 2，入队 2，角色请求 2", result)
        self.assertIn("成功 1，失败 1，跳过 0，延后 0", result)
        self.assertIn("目录无变化", result)
        self.assertIn("账号授权（401） × 1", result)
        self.assertIn("耗时 11 秒", result)
        self.assertIn("/ef 绑定", result)
        refresh_roles.assert_awaited_once_with(
            roles,
            self.cipher,
            force=True,
            trigger="manual-global",
        )
        build_report.assert_not_called()
        renderer.assert_not_awaited()

    def test_refresh_text_reports_protective_stop_and_old_snapshot_policy(self):
        refresh = OwnershipRefreshResult(
            attempted=210,
            succeeded=5,
            failed=3,
            skipped=202,
            catalog_updated=False,
            started_at=100,
            finished_at=111,
            stopped_early=True,
            stop_reason="官方社区接口连续返回 405，已保护性停止剩余刷新",
        )

        result = endfield_plugin._format_ownership_refresh_result("global", refresh)

        self.assertIn("失败 3，跳过 202", result)
        self.assertIn("保护性停止", result)
        self.assertIn("旧快照仍按 48 小时有效期参与统计", result)
        self.assertNotIn("/ef 绑定更新凭证", result)

    def test_refresh_text_distinguishes_catalog_failure_from_unchanged(self):
        refresh = OwnershipRefreshResult(
            attempted=0,
            succeeded=0,
            failed=0,
            skipped=0,
            catalog_updated=False,
            started_at=100,
            finished_at=100,
            catalog_checked=True,
            catalog_error="RuntimeError: 目录检查失败",
        )

        result = endfield_plugin._format_ownership_refresh_result("global", refresh)

        self.assertIn("目录检查失败（RuntimeError: 目录检查失败）", result)
        self.assertNotIn("目录无变化", result)

    def test_group_view_uses_live_member_filter_and_never_global_roles(self):
        class Matcher:
            async def finish(self, message=None):
                return message

        class Bot:
            def guild_member_list(self, guild_id):
                async def members():
                    yield SimpleNamespace(user=SimpleNamespace(id="member-1"))

                return members()

        event = SimpleNamespace(
            user=SimpleNamespace(id="viewer"),
            guild=SimpleNamespace(id="group"),
            member=SimpleNamespace(roles=[]),
        )
        selected_roles = [SimpleNamespace(role_id="role", server_id="1")]
        report = SimpleNamespace(scope="group")
        with (
            mock.patch.object(
                endfield_plugin.account_store,
                "list_all_roles",
                return_value=selected_roles,
            ) as list_roles,
            mock.patch.object(
                endfield_plugin.ownership_stats_service,
                "build_report",
                return_value=report,
            ) as build_report,
            mock.patch.object(
                endfield_plugin,
                "render_ownership_stats",
                mock.AsyncMock(return_value="rendered"),
            ) as renderer,
        ):
            result = asyncio.run(
                endfield_plugin._handle_ownership_stats(
                    Matcher(),
                    event,
                    parse_command("持有率"),
                    bot=Bot(),
                )
            )

        self.assertEqual(result, "rendered")
        list_roles.assert_called_once_with({"member-1"})
        build_report.assert_called_once_with("group", selected_roles, refresh=None)
        renderer.assert_awaited_once_with(report)


if __name__ == "__main__":
    unittest.main()
