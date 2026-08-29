from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import plugins.endfield as endfield_plugin
from satori.model import IterablePageResult, PageResult
from plugins.endfield.account_client import EndfieldAPIError
from plugins.endfield.account_crypto import CredentialCipher
from plugins.endfield.account_store import (
    EndfieldStore,
    OperatorCatalogEntry,
    OperatorSnapshotMember,
    RoleCandidate,
)
from plugins.endfield.commands import parse_command
from plugins.endfield.ownership_stats import (
    GroupMemberListError,
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
            "name-m": "男管理员",
            "name-f": "女管理员",
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
        self.assertEqual(female.operator_key, hashlib.md5(b"chr_9000_endmin").hexdigest())
        self.assertEqual({item.name for item in catalog}, {"男管理员", "女管理员"})
        self.assertEqual({item.profession for item in catalog}, {"先锋"})

        canonical_female_key = hashlib.md5(b"chr_0003_endminf").hexdigest()
        for raw_id in (female.operator_key, canonical_female_key, "chr_9000_endmin"):
            members, _ = parse_operator_snapshot(
                {
                    "base": {"charNum": 1},
                    "chars": [{"charData": {"id": raw_id}, "potentialLevel": 0}],
                },
                catalog,
            )
            self.assertEqual(members[0].operator_key, female.operator_key)

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
        self.assertTrue(result.stopped_early)
        self.assertIn("405", result.stop_reason)
        self.assertEqual(len(self.store.list_operator_snapshots()), 3)

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
            "plugins.endfield.ownership_stats.call_onebot_action",
            mock.AsyncMock(side_effect=RuntimeError("unsupported")),
        ):
            with self.assertRaises(GroupMemberListError):
                asyncio.run(collect_group_member_ids(BrokenBot(), "group"))

        fallback = mock.AsyncMock(
            return_value={"status": "ok", "data": [{"user_id": 300}, {"user_id": "400"}]}
        )
        with mock.patch("plugins.endfield.ownership_stats.call_onebot_action", fallback):
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
        self.assertIn("尝试 2，成功 1，失败 1，跳过 0", result)
        self.assertIn("目录无变化", result)
        self.assertIn("耗时 11 秒", result)
        self.assertIn("/ef 绑定", result)
        refresh_roles.assert_awaited_once_with(roles, self.cipher, force=True)
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
