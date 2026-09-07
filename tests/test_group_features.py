from __future__ import annotations

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.letoderea import EVENT, STOP, Scope, Subscriber
from satori import ChannelType, Role

from otae_bot.adapters import feature_gate
from otae_bot.adapters.entari import ArgVal, ChainMsg, SendDest
from otae_bot.group_features import (
    GroupFeatureStore,
    GroupScope,
    resolve_plugin,
    scope_from_event,
)
from plugins.group_manager import handlers, permissions


def session(group="100", user="200", bot="300", roles=(), private=False):
    return SimpleNamespace(
        account=SimpleNamespace(
            platform="qq", self_id=bot,
            guild_member_get=AsyncMock(return_value=SimpleNamespace(roles=[])),
            internal=AsyncMock(return_value={}),
            protocol=SimpleNamespace(send_message=AsyncMock(return_value=["receipt"]),
                                     send_private_message=AsyncMock(return_value=["private-receipt"])),
        ),
        event=SimpleNamespace(
            guild=None if private else SimpleNamespace(id=group),
            channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT),
            user=SimpleNamespace(id=user), member=SimpleNamespace(roles=list(roles)),
        ),
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "switches.json"
        self.store = GroupFeatureStore(self.path)
        self.scope = GroupScope("qq", "300", "100")

    def test_defaults_isolation_and_restart(self):
        self.assertTrue(self.store.is_enabled(self.scope, "hyw"))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.store.set_enabled(self.scope, "hyw", False))
        self.assertFalse(GroupFeatureStore(self.path).is_enabled(self.scope, "hyw"))
        for scope in (None, GroupScope("qq", "300", "101"),
                      GroupScope("qq", "301", "100"), GroupScope("other", "300", "100")):
            with self.subTest(scope=scope):
                self.assertTrue(self.store.is_enabled(scope, "hyw"))
        self.assertTrue(self.store.is_enabled(self.scope, "endfield"))
        self.assertFalse(self.store.set_enabled(self.scope, "hyw", False))
        self.assertTrue(self.store.set_enabled(self.scope, "hyw", True))
        self.assertTrue(GroupFeatureStore(self.path).is_enabled(self.scope, "hyw"))

    def test_failed_write_keeps_memory_and_disk_unchanged(self):
        self.store.set_enabled(self.scope, "hyw", False)
        before = self.path.read_bytes()
        with (
            patch("otae_bot.group_features.os.replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.store.set_enabled(self.scope, "hyw", True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.store.is_enabled(self.scope, "hyw"))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_updates_are_not_lost(self):
        names = ["hyw", "endfield", "steamInfo", "bilibilibot"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda name: self.store.set_enabled(self.scope, name, False), names))
        restarted = GroupFeatureStore(self.path)
        for name in names:
            self.assertFalse(restarted.is_enabled(self.scope, name))

    def test_corrupt_data_is_not_overwritten_and_management_stays_available(self):
        for data in ("{", "[]", '{"version":2}', '{"version":1,"disabled":{"x":"hyw"}}'):
            with self.subTest(data=data):
                self.path.write_text(data)
                store = GroupFeatureStore(self.path)
                with self.assertRaises(ValueError):
                    store.set_enabled(self.scope, "hyw", False)
                self.assertTrue(store.is_enabled(self.scope, "group_manager"))
                self.assertEqual(self.path.read_text(), data)

    def test_protected_plugins_cannot_be_disabled(self):
        for name in ("group_manager", "request_handler"):
            with self.assertRaises(ValueError):
                self.store.set_enabled(self.scope, name, False)
        self.assertFalse(self.path.exists())

    def test_group_detection_and_aliases(self):
        current = session()
        self.assertEqual(scope_from_event(current.account, current.event), self.scope)
        current.event.guild = None
        self.assertEqual(scope_from_event(current.account, current.event), self.scope)
        current.event.channel.type = ChannelType.DIRECT.value
        self.assertIsNone(scope_from_event(current.account, current.event))
        current.event.channel.type = None
        self.assertIsNone(scope_from_event(current.account, current.event))
        for name, expected in (("HYW", "hyw"), ("plugins.steamInfo", "steamInfo"),
                               ("ef", "endfield"), ("何意味", "hyw")):
            self.assertEqual(resolve_plugin(name, ["hyw", "steamInfo", "endfield"]), expected)
        self.assertIsNone(resolve_plugin("../../hyw", ["hyw"]))


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_superuser_and_current_group_roles(self):
        for configured in (["200"], "200", 200):
            current = session()
            with patch.object(permissions.Config, "SUPERUSERS", configured):
                self.assertTrue(await permissions.can_manage(
                    current.account, current.event, scope_from_event(current.account, current.event)))
            current.account.guild_member_get.assert_not_awaited()
            current.account.internal.assert_not_awaited()
        for role in ("admin", "owner", Role("administrator"), {"id": "group_owner"}):
            current = session(roles=[role])
            with patch.object(permissions.Config, "SUPERUSERS", []):
                self.assertTrue(await permissions.can_manage(
                    current.account, current.event, scope_from_event(current.account, current.event)))
            current.account.guild_member_get.assert_not_awaited()

    async def test_query_uses_current_group_and_sender(self):
        current = session()
        current.account.guild_member_get.return_value = SimpleNamespace(roles=[Role("owner")])
        self.assertTrue(await permissions.can_manage(
            current.account, current.event, scope_from_event(current.account, current.event)))
        current.account.guild_member_get.assert_awaited_once_with(guild_id="100", user_id="200")

    async def test_role_display_names_and_substrings_do_not_grant_permissions(self):
        for role in ("not_admin", "former_owner", Role("member", "管理员"),
                     {"id": "member", "name": "admin"}, {"description": "owner"}):
            with self.subTest(role=role):
                current = session(roles=[role])
                self.assertFalse(await permissions.can_manage(
                    current.account, current.event, scope_from_event(current.account, current.event)))

    async def test_qq_internal_fallback_checks_account_group_user_and_success(self):
        current = session()
        current.account.guild_member_get.side_effect = RuntimeError("unsupported")
        scope = scope_from_event(current.account, current.event)
        good = {"user_id": 200, "group_id": 100, "role": "admin"}
        for response, allowed in (
            (good, True), ({"status": "ok", "retcode": 0, "data": good}, True),
            ({"status": "failed", "data": good}, False),
            ({"retcode": 1, "data": good}, False),
            ({**good, "group_id": 101}, False), ({**good, "user_id": 201}, False),
            ({**good, "role": "member"}, False), ({"role": "admin"}, False),
        ):
            with self.subTest(response=response):
                current.account.internal.return_value = response
                self.assertEqual(await permissions.can_manage(current.account, current.event, scope), allowed)
        current.account.internal.assert_awaited_with(
            action="get_group_member_info", group_id=100, user_id=200, no_cache=True)

    async def test_failed_permission_checks_deny_access(self):
        current = session()
        current.account.guild_member_get.side_effect = TimeoutError()
        current.account.internal.side_effect = TimeoutError()
        self.assertFalse(await permissions.can_manage(
            current.account, current.event, scope_from_event(current.account, current.event)))

    async def test_member_lookup_cannot_authorize_a_different_user(self):
        current = session()
        current.account.guild_member_get.return_value = SimpleNamespace(
            roles=[Role("owner")], user=SimpleNamespace(id="201"))
        self.assertFalse(await permissions.can_manage(
            current.account, current.event, scope_from_event(current.account, current.event)))

    async def test_current_group_admin_cannot_authorize_a_different_scope(self):
        current = session(roles=[Role("admin")])
        self.assertFalse(await permissions.can_manage(
            current.account, current.event, GroupScope("qq", "300", "101")))
        self.assertFalse(await permissions.can_manage(
            current.account, current.event, GroupScope("qq", "301", "100")))
        current.account.guild_member_get.assert_not_awaited()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = GroupFeatureStore(Path(directory.name) / "switches.json")
        self.enterContext(patch.object(handlers, "feature_store", self.store))
        self.enterContext(patch.object(handlers, "loaded_group_plugins", return_value=[
            "group_manager", "hyw", "endfield", "request_handler", "steamInfo"]))
        self.reply = self.enterContext(patch.object(handlers.feature_cmd, "finish", new_callable=AsyncMock))
        self.enterContext(patch.object(permissions.Config, "SUPERUSERS", ["root"]))

    async def run_command(self, current, text):
        self.reply.reset_mock()
        await handlers.handle_group_features(current, ArgVal(text, True))
        self.reply.assert_awaited_once()
        return self.reply.await_args.args[0]

    async def test_switch_changes_only_current_account_and_group(self):
        current = session(roles=[Role("admin")])
        reply = await self.run_command(current, "关闭 HYW")
        self.assertIn("已关闭", reply)
        scope = scope_from_event(current.account, current.event)
        self.assertFalse(self.store.is_enabled(scope, "hyw"))
        for other in (session(group="101"), session(bot="301"), session(private=True)):
            self.assertTrue(self.store.is_enabled(scope_from_event(other.account, other.event), "hyw"))
        self.assertIn("[关闭] hyw", await self.run_command(current, "列表"))
        self.assertIn("已开启", await self.run_command(current, "开启 q"))
        self.assertTrue(self.store.is_enabled(scope, "hyw"))

    async def test_private_superuser_and_ordinary_members_cannot_manage(self):
        self.assertIn("群内", await self.run_command(session(user="root", private=True), "关闭 hyw"))
        for text in ("关闭 hyw", "开启 hyw", "列表"):
            self.assertIn("管理权限", await self.run_command(session(), text))
        self.assertFalse(self.store.path.exists())

    async def test_rejects_target_group_unknown_plugin_and_disabling_management(self):
        current = session(user="root")
        for text in ("关闭 hyw 101", "关闭 ../../hyw", "关闭 group_manager", "关闭 request_handler"):
            await self.run_command(current, text)
        self.assertFalse(self.store.path.exists())

    async def test_failed_save_reports_failure_without_success_or_state_change(self):
        current = session(user="root")
        with patch("otae_bot.group_features.os.replace", side_effect=OSError("disk full")):
            reply = await self.run_command(current, "关闭 hyw")
        self.assertIn("读写失败", reply)
        self.assertTrue(self.store.is_enabled(scope_from_event(current.account, current.event), "hyw"))


class GateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = GroupFeatureStore(Path(directory.name) / "switches.json")
        self.enterContext(patch.object(feature_gate, "feature_store", self.store))
        self.current = session()
        self.store.set_enabled(scope_from_event(self.current.account, self.current.event), "hyw", False)

    async def test_gate_supports_native_command_session_and_event_listener_contexts(self):
        calls = AsyncMock()

        async def callback():
            await calls()

        scope = Scope("test-group-gate-contexts")
        self.addCleanup(scope.dispose)
        subscriber = scope.register(callback)
        subscriber.propagate(feature_gate.GroupFeatureGate("hyw"))
        contexts = [
            {ITEM_SESSION: self.current},
            {ITEM_ACCOUNT: self.current.account, EVENT: self.current.event},
        ]
        for ctx in contexts:
            self.assertIs(await subscriber.handle(ctx), STOP)
        calls.assert_not_awaited()
        for current in (session(group="101"), session(bot="301"), session(private=True)):
            await subscriber.handle({ITEM_SESSION: current})
        # Non-group lifecycle and timer events are still executed.
        await subscriber.handle({})
        self.assertEqual(calls.await_count, 4)
        self.store.set_enabled(scope_from_event(self.current.account, self.current.event), "hyw", True)
        await subscriber.handle({ITEM_SESSION: self.current})
        self.assertEqual(calls.await_count, 5)

    async def test_install_covers_existing_future_and_reloaded_scopes(self):
        scope = Scope("test-group-gate")
        self.addCleanup(scope.dispose)
        calls = AsyncMock()

        async def callback():
            await calls()

        existing = scope.register(callback)
        plugin = SimpleNamespace(path="plugins.hyw.handlers", _scope=scope)
        with patch.object(feature_gate.plugin_service, "plugins", {plugin.path: plugin}):
            feature_gate.install_group_feature_gates()
            feature_gate.install_group_feature_gates()
            self.assertEqual(len(scope.propagators), 1)
            future = scope.register(callback)
            for subscriber in (existing, future):
                self.assertIs(await subscriber.handle({ITEM_SESSION: self.current}), STOP)
            reloaded = Scope("test-group-gate-reload")
            self.addCleanup(reloaded.dispose)
            plugin._scope = reloaded
            existing = reloaded.register(callback)
            await feature_gate.on_plugin_loaded(SimpleNamespace(plugin_id=plugin.path))
            self.assertIs(await existing.handle({ITEM_SESSION: self.current}), STOP)
        calls.assert_not_awaited()

    async def test_background_delivery_filters_each_destination_and_keeps_private_messages(self):
        results = []

        async def notify():
            for dest in (SendDest("100", "100"), SendDest("101", "101"), SendDest("100", private=True)):
                # A worker inherits its plugin's subscriber context.
                results.append(await asyncio.create_task(ChainMsg.text("test").send(dest, self.current.account)))

        notify.__module__ = "plugins.hyw.handlers"
        await Subscriber(notify).handle({})
        self.assertEqual(results, [[], ["receipt"], ["private-receipt"]])
        self.current.account.protocol.send_message.assert_awaited_once_with("101", ChainMsg.text("test"))

    async def test_other_plugin_delivery_is_unaffected(self):
        async def notify():
            return await ChainMsg.text("test").send(SendDest("100", "100"), self.current.account)

        notify.__module__ = "plugins.endfield.handlers"
        self.assertEqual(await Subscriber(notify).handle({}), ["receipt"])
