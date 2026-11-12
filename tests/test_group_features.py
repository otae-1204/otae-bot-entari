from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arclet.entari import MessageChain, Text
from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.letoderea import EVENT, STOP, Scope, Subscriber
from satori import ChannelType, Role

from otae_bot.delivery import Target, deliver
from otae_bot.groups.features import (
    GroupFeatureStore,
    GroupScope,
    resolve_plugin,
    scope_from_event,
)
from otae_bot.pipeline import feature_gate, framework_hooks
from plugins.group_manager import permissions
from tests.support.dispatch import run_scenarios


def session(group="100", user="200", bot="300", roles=(), private=False):
    return SimpleNamespace(
        account=SimpleNamespace(
            platform="qq", self_id=bot,
            guild_member_get=AsyncMock(return_value=SimpleNamespace(roles=[])),
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

    def test_v1_migration_preserves_switches(self):
        self.path.write_text(json.dumps({"version": 1, "disabled": {self.scope.key: ["hyw"]}}))
        self.assertFalse(self.store.is_enabled(self.scope, "hyw"))
        self.assertTrue(self.store.is_enabled(self.scope, "radar"))
        self.assertTrue(self.store.set_enabled(self.scope, "radar", False))
        restarted = GroupFeatureStore(self.path)
        self.assertFalse(restarted.is_enabled(self.scope, "radar"))
        self.assertFalse(restarted.is_enabled(self.scope, "hyw"))
        self.assertTrue(restarted.is_enabled(GroupScope("qq", "300", "101"), "radar"))
        self.assertTrue(restarted.is_enabled(GroupScope("qq", "300", "100", private=True), "radar"))
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)
        self.assertTrue(restarted.set_enabled(self.scope, "radar", True))
        self.assertTrue(GroupFeatureStore(self.path).is_enabled(self.scope, "radar"))

    def test_failed_write_keeps_memory_and_disk_unchanged(self):
        self.store.set_enabled(self.scope, "hyw", False)
        before = self.path.read_bytes()
        with (
            patch("otae_bot.groups.features.os.replace", side_effect=OSError("disk full")),
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

    async def test_member_roles_from_the_lookup_decide_alone(self):
        current = session()
        scope = scope_from_event(current.account, current.event)
        for roles, allowed in (([Role("admin", "管理员")], True), ([Role("owner", "群主")], True),
                               ([Role("member", "群成员")], False), ([], False)):
            with self.subTest(roles=roles):
                current.account.guild_member_get.return_value = SimpleNamespace(roles=roles)
                self.assertEqual(await permissions.can_manage(current.account, current.event, scope), allowed)

    async def test_failed_permission_checks_deny_access(self):
        current = session()
        current.account.guild_member_get.side_effect = TimeoutError()
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


GROUP_MANAGER_SCENARIOS = r"""
from unittest.mock import patch
from satori import Member, Role, User
from otae_bot.groups.features import GroupScope

handlers = plugin('plugins.group_manager.handlers')
store = handlers.feature_store
default_account.protocol.answers['guild.member.get'] = {'user': {'id': 'member'}, 'roles': []}
admin = Member(User('admin'), roles=[Role('admin')])
owner = Member(User('owner'), roles=[Role('owner')])
SCOPE = GroupScope('qq', 'test-bot', '100')


def one(sent):
    assert len(sent) == 1, sent
    return sent[0]


async def main():
    with patch('otae_bot.config.settings.Config.SUPERUSERS', ['root']):
        # 私聊、普通成员都不能管理；三个指令名都能用
        assert '群聊内' in one(await say('/功能 关闭 hyw', user='root', private=True))
        for text in ('/功能 关闭 hyw', '/插件 开启 hyw', '/plugin 列表'):
            assert '权限不足' in one(await say(text, user='member')), text
        assert not store.path.exists()
        # 无参数、帮助、看不懂的动作都回帮助
        for text in ('/功能', '/功能 帮助', '/功能 help', '/功能 关闭', '/功能 删除 hyw'):
            assert '本群插件管理' in one(await say(text, member=admin, user='admin')), text
        # 管理员关闭只影响当前账号与当前群
        reply = one(await say('/功能 关闭 HYW', member=admin, user='admin'))
        assert '已成功关闭' in reply and '配置已持久化保存' in reply, reply
        assert not store.is_enabled(SCOPE, 'hyw')
        for other in (GroupScope('qq', 'test-bot', '101'), GroupScope('qq', 'other-bot', '100')):
            assert store.is_enabled(other, 'hyw')
        assert '开关状态未发生改变' in one(await say('/功能 关闭 hyw', user='root'))
        assert '[关闭] hyw' in one(await say('/功能 列表', member=admin, user='admin'))
        assert '已成功开启' in one(await say('/功能 开启 q', member=owner, user='owner'))
        assert store.is_enabled(SCOPE, 'hyw')
        # 跨群、路径、受保护插件都拒绝
        before = store.path.read_bytes()
        assert '本群插件管理' in one(await say('/功能 关闭 hyw 101', user='root'))
        assert '未找到' in one(await say('/功能 关闭 ../../hyw', user='root'))
        for name in ('group_manager', 'request_handler'):
            assert '保护插件' in one(await say('/功能 关闭 ' + name, user='root'))
        assert store.path.read_bytes() == before
        # 保存失败只报失败，不改状态
        with patch('otae_bot.groups.features.os.replace', side_effect=OSError('disk full')):
            reply = one(await say('/功能 关闭 hyw', user='root'))
        assert '保存失败' in reply and '已成功' not in reply, reply
        assert store.is_enabled(SCOPE, 'hyw')
    print('ALL-OK')

run(main())
"""


class GroupManagerDispatchTests(unittest.TestCase):
    def test_feature_command_through_the_real_event_path(self):
        result = run_scenarios(GROUP_MANAGER_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


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

    async def test_install_covers_existing_future_and_reloaded_subscribers(self):
        calls = AsyncMock()

        def handler(module):
            async def callback():
                await calls()

            callback.__module__ = module
            return callback

        scope = Scope("test-group-gate")
        self.addCleanup(scope.dispose)
        existing = scope.register(handler("plugins.hyw.handlers"))
        with patch.object(framework_hooks, "get_all_subscribers", return_value=[existing]):
            self.addCleanup(feature_gate.install_group_feature_gates())
            self.addCleanup(feature_gate.install_group_feature_gates())
        # Attached once, even when installed twice.
        self.assertEqual(sum(sub.callable_target.__name__ == "check" for sub in existing._propagates), 1)
        # Registered later, e.g. by Entari's hot reload into a new plugin scope.
        reloaded = Scope("test-group-gate-reload")
        self.addCleanup(reloaded.dispose)
        future = reloaded.register(handler("plugins.hyw"))
        for subscriber in (existing, future):
            self.assertIs(await subscriber.handle({ITEM_SESSION: self.current}), STOP)
        calls.assert_not_awaited()
        # Protected plugins and the bot's own listeners are never gated.
        for module in ("plugins.group_manager.handlers", "otae_bot.application"):
            subscriber = reloaded.register(handler(module))
            with self.assertRaises(ValueError):
                subscriber.get_propagator(feature_gate.GroupFeatureGate)

    async def test_background_delivery_filters_each_destination_and_keeps_private_messages(self):
        results = []

        async def notify():
            for target in (Target.group("100"), Target.group("101"), Target.private("100")):
                # A worker inherits its plugin's subscriber context.
                result = await asyncio.create_task(deliver(target, MessageChain([Text("test")]), account=self.current.account))
                results.append(result.messages)

        notify.__module__ = "plugins.hyw.handlers"
        await Subscriber(notify).handle({})
        self.assertEqual(results, [[], ["receipt"], ["private-receipt"]])
        self.current.account.protocol.send_message.assert_awaited_once_with("101", MessageChain([Text("test")]))

    async def test_other_plugin_delivery_is_unaffected(self):
        async def notify():
            result = await deliver(Target.group("100"), MessageChain([Text("test")]), account=self.current.account)
            return result.messages

        notify.__module__ = "plugins.endfield.handlers"
        self.assertEqual(await Subscriber(notify).handle({}), ["receipt"])
