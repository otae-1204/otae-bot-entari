"""Boundaries and runtime contracts that can regress when modules are moved."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from otae_bot import application, lifecycle
from otae_bot.plugin_registry import discover_plugins


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_shared_code_does_not_import_feature_plugins(self):
        for path in (ROOT / "otae_bot").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name == "plugins" or name.startswith("plugins."), (path, name))

    def test_proactive_messages_only_go_through_delivery(self):
        """Plugins send via otae_bot.delivery.deliver, not the Satori send calls or SendDest."""
        paths = list((ROOT / "plugins").rglob("*.py"))
        paths.extend(
            path for path in (ROOT / "otae_bot").rglob("*.py")
            if not path.is_relative_to(ROOT / "otae_bot/delivery")
            and not path.is_relative_to(ROOT / "otae_bot/boundary")
        )
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"send_message", "send_private_message"}, path)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("SendDest", [alias.name for alias in node.names], path)

    def test_business_models_and_view_builders_do_not_register_events(self):
        paths = list((ROOT / "plugins/endfield").rglob("models.py"))
        paths.extend((ROOT / "plugins/endfield/catalog/views").glob("*.py"))
        paths.append(ROOT / "plugins/endfield/account/challenge/parsing.py")
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(
                        module.startswith(("arclet.entari", "otae_bot.compat", "otae_bot.accounts", "otae_bot.pipeline"))
                        or "rendering" in module.split("."),
                        (path, module),
                    )

    def test_plugin_entrypoints_only_import_handlers(self):
        for name in discover_plugins(ROOT / "plugins"):
            path = ROOT / name.replace(".", "/") / "__init__.py"
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                self.assertIsInstance(node, (ast.Expr, ast.ImportFrom), path)
                if isinstance(node, ast.ImportFrom):
                    self.assertEqual((node.level, node.module), (1, "handlers"), path)
                else:
                    self.assertIsInstance(node.value, ast.Constant, path)

    def test_resource_paths_are_independent_of_feature_depth(self):
        from plugins.endfield import paths
        from plugins.endfield.catalog import aliases
        from plugins.endfield.calendar import akedata
        from plugins.endfield.account import draw
        from plugins.endfield.account.challenge import draw as challenge
        from plugins.endfield.rendering import cards

        plugin_root = ROOT / "plugins/endfield"
        self.assertEqual(paths.PROJECT_ROOT, ROOT)
        self.assertEqual(aliases.ALIAS_DATA_PATH, plugin_root / "alias_data.json")
        self.assertEqual(akedata.CALENDAR_DIR, plugin_root / "assets/calendar")
        self.assertEqual(draw.UI_ASSET_ROOT, plugin_root / "assets/ui")
        self.assertEqual(challenge.POTENTIAL_ICON_DIR, plugin_root / "assets/ui")
        self.assertEqual(cards.ASSET_DIR, ROOT / "assets/image/endfield")

    def test_discovery_ignores_resources_and_nested_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("zeta", "alpha", "resource_only", "alpha/nested"):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ("zeta", "alpha", "alpha/nested"):
                (root / name / "__init__.py").touch()
            self.assertEqual(discover_plugins(root), ("plugins.alpha", "plugins.zeta"))

    def test_run_lock_blocks_a_second_process_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = lifecycle.acquire_run_lock(Path(directory))
            self.assertIsNotNone(lock)
            self.addCleanup(lock.close)
            child = (
                "from pathlib import Path; import sys; "
                "from otae_bot.lifecycle import acquire_run_lock; "
                "lock = acquire_run_lock(Path(sys.argv[1])); "
                "raise SystemExit(2 if lock is None else 0)"
            )
            result = subprocess.run([sys.executable, "-c", child, directory], cwd=ROOT, timeout=20)
            self.assertEqual(result.returncode, 2)
            lock.close()
            result = subprocess.run([sys.executable, "-c", child, directory], cwd=ROOT, timeout=20)
            self.assertEqual(result.returncode, 0)

    def test_main_releases_lock_when_application_creation_fails(self):
        lock = Mock()
        with patch.object(application, "acquire_run_lock", return_value=lock):
            with patch.object(application, "create_app", side_effect=RuntimeError("startup failed")):
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    application.main()
        lock.close.assert_called_once_with()

    def test_network_configuration_preserves_defaults_and_multiple_clients(self):
        with patch.object(application, "WS", side_effect=lambda **values: values):
            self.assertEqual(application.build_networks([]), [
                {"host": "localhost", "port": 5500, "path": "", "token": None},
            ])
            networks = application.build_networks([
                {"host": "one", "port": "5501", "token": "test-token"},
                {"host": "two", "port": 5502, "path": "/satori", "token": ""},
            ])
        self.assertEqual(networks[0], {"host": "one", "port": 5501, "path": "", "token": "test-token"})
        self.assertEqual(networks[1], {"host": "two", "port": 5502, "path": "/satori", "token": None})

    def test_every_way_the_bot_stops_a_handler_is_an_entari_exit_state(self):
        # Handlers re-raise ``except ExitState`` before ``except Exception``; every
        # stop/finish helper must raise something that clause recognises.
        import asyncio
        from arclet.alconna import Alconna
        from arclet.entari import Session
        from arclet.letoderea import ExitState
        from otae_bot.compat import entari

        session = object.__new__(Session)
        stops = {
            "Session.stop": lambda: session.stop(),
            "stop_session": entari.stop_session,
            "finish": lambda: asyncio.run(entari.finish(session)),
            "Matcher.finish": lambda: asyncio.run(entari.Matcher(Alconna("x")).finish()),
            "on_event.finish": lambda: asyncio.run(entari._EventHook().finish()),
        }
        token = entari._CURRENT_SESSION.set(session)
        try:
            for name, stop in stops.items():
                with self.subTest(name), self.assertRaises(ExitState):
                    stop()
        finally:
            entari._CURRENT_SESSION.reset(token)

    def test_scheduler_uses_the_scope_registering_imported_service_methods(self):
        from otae_bot.compat import entari

        plugin = SimpleNamespace(module=SimpleNamespace(__name__="plugins.demo.handlers"))
        token = entari.current_plugin.set(plugin)
        try:
            self.assertEqual(entari._plugin_module("plugins.demo.service"), "plugins.demo.handlers")
        finally:
            entari.current_plugin.reset(token)

    def test_all_plugins_load_in_real_entari_without_connecting(self):
        script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from otae_bot.application import create_app
from otae_bot.plugin_registry import discover_plugins
from otae_bot.compat.entari import timer
from arclet.entari.plugin.service import plugin_service
from arclet.alconna import command_manager

app = create_app()
expected = set(discover_plugins())
missing = sorted(expected - plugin_service.plugins.keys())
assert not missing, missing
jobs = sorted(timer._jobs)
for name in ('bili_live_check', 'bili_video_check', 'bili_dynamic_check',
             'endfield_ownership_refresh', 'endfield_ownership_catalog_refresh',
             'endfield_announcement_reminders',
             'tibo_radar_refresh'):
    assert name in jobs, (name, jobs)
for job in timer._jobs.values():
    assert job.subscriber is not None
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from arclet.entari import MessageChain, Session, command
from arclet.entari.command.provider import _remove_config_prefix
from arclet.entari.config import EntariConfig
EntariConfig.instance.basic.prefix = ['/']

async def check_hyw_dispatch():
    session = object.__new__(Session)
    session.account = SimpleNamespace(platform='qq', self_id='test-bot')
    session.event = SimpleNamespace(gather=AsyncMock(return_value=None), message=None, user=SimpleNamespace(id='test-user'), guild=None,
                                    channel=SimpleNamespace(id='test-channel'))
    session.reply = None
    session.send = AsyncMock(return_value=[])
    for name in ('q', 'hyw', '何意味'):
        message = _remove_config_prefix(MessageChain('/' + name + ' 帮助'))
        await command.execute(message, session)
    assert session.send.await_count == 3, session.send.await_count

asyncio.get_event_loop().run_until_complete(check_hyw_dispatch())

async def check_grok_dispatch():
    session = object.__new__(Session)
    session.account = SimpleNamespace(platform='qq', self_id='test-bot')
    session.event = SimpleNamespace(gather=AsyncMock(return_value=None), message=None, user=SimpleNamespace(id='test-user'), guild=None,
                                    channel=SimpleNamespace(id='test-channel'))
    session.reply = None
    session.send = AsyncMock(return_value=[])
    for name in ('grok', 'grokbot'):
        await command.execute(_remove_config_prefix(MessageChain('/' + name + ' 帮助')), session)
    assert session.send.await_count == 2, session.send.await_count

asyncio.get_event_loop().run_until_complete(check_grok_dispatch())

from satori import ChannelType, Role
from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.letoderea import EVENT, STOP, post
from arclet.entari.event.command import CommandExecute
from otae_bot.config.settings import Config
from otae_bot.groups.features import feature_store, scope_from_event
from otae_bot.pipeline.feature_gate import GroupFeatureGate
from otae_bot.command_usage.recorder import CommandUsageRecorder
from arclet.entari.plugin import get_plugin_subscribers
from unittest.mock import patch

def assert_attached(prefix, *kinds):
    subscribers = [sub for plg in plugin_service.plugins.values() if plg.path.startswith(prefix)
                   for sub in get_plugin_subscribers(plg)]
    assert subscribers, prefix
    for sub in subscribers:
        for kind in kinds:
            sub.get_propagator(kind)  # raises ValueError when missing
Config.SUPERUSERS = ['test-root']

def group_session(group='100', user='test-root', bot='test-bot', private=False):
    session = object.__new__(Session)
    session.account = SimpleNamespace(
        platform='qq', self_id=bot,
        guild_member_get=AsyncMock(return_value=SimpleNamespace(roles=[])),
        internal=AsyncMock(return_value={}),
    )
    session.event = SimpleNamespace(gather=AsyncMock(return_value=None), message=None, 
        user=SimpleNamespace(id=user), member=SimpleNamespace(roles=[]),
        guild=None if private else SimpleNamespace(id=group),
        channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT),
        content='https://www.bilibili.com/video/BVtest',
    )
    session.reply = None
    session.send = AsyncMock(return_value=[])
    return session

async def run_command(session, text):
    session.send.reset_mock()
    await post(CommandExecute(_remove_config_prefix(MessageChain(text)), session),
               inherit_ctx={ITEM_ACCOUNT: session.account})

async def check_random_wiki_dispatch():
    endfield = plugin_service.plugins['plugins.endfield.handlers'].module
    current = group_session(user='unbound-user')
    scope = scope_from_event(current.account, current.event)
    commands = ('/zmd 随机', '/ef random', '/终末地 随机百科', '/endfield RAND')
    with patch.object(endfield, '_handle_random_wiki', AsyncMock()) as random_wiki:
        for text in commands:
            random_wiki.reset_mock()
            await run_command(current, text)
            random_wiki.assert_awaited_once()
        feature_store.set_enabled(scope, 'endfield', False)
        try:
            for text in commands:
                random_wiki.reset_mock()
                await run_command(current, text)
                random_wiki.assert_not_awaited()
                current.send.assert_not_awaited()
            # Disabling this group does not disable another group or private chat.
            for other in (group_session(group='101'), group_session(private=True)):
                random_wiki.reset_mock()
                await run_command(other, '/zmd 随机')
                random_wiki.assert_awaited_once()
        finally:
            feature_store.set_enabled(scope, 'endfield', True)

asyncio.get_event_loop().run_until_complete(check_random_wiki_dispatch())

async def check_group_switches():
    current = group_session()
    scope = scope_from_event(current.account, current.event)
    # Every subscriber, including handlers submodules, is guarded before dispatch.
    assert_attached('plugins.hyw', GroupFeatureGate)
    await run_command(current, '/功能 关闭 hyw')
    assert current.send.await_count == 1, current.send.await_args_list
    assert '关闭' in str(current.send.await_args.args[0]), current.send.await_args_list
    assert not feature_store.is_enabled(scope, 'hyw')
    for alias in ('q', 'hyw', '何意味'):
        await run_command(current, '/' + alias + ' 帮助')
        current.send.assert_not_awaited()
    # Command parser's generated help is also covered by the gate.
    await run_command(current, '/q --help')
    current.send.assert_not_awaited()
    for other in (group_session(group='101'), group_session(bot='other-bot'), group_session(private=True)):
        await run_command(other, '/q 帮助')
        other.send.assert_awaited_once()
    await run_command(current, '/plugin on q')
    assert feature_store.is_enabled(scope, 'hyw')
    await run_command(current, '/q 帮助')
    current.send.assert_awaited_once()

    await run_command(current, '/功能 关闭 grok')
    assert not feature_store.is_enabled(scope, 'grok_bot')
    for name in ('grok', 'grokbot'):
        await run_command(current, '/' + name + ' 帮助')
        current.send.assert_not_awaited()
    other = group_session(group='101')
    await run_command(other, '/grok 帮助')
    other.send.assert_not_awaited()
    admin = group_session(user='group-admin')
    admin.event.member.roles = [Role('admin')]
    await run_command(admin, '/功能 开启 grok')
    assert '仅 SuperUser' in str(admin.send.await_args.args[0])
    assert not feature_store.is_enabled(scope, 'grok_bot')
    await run_command(current, '/功能 开启 grokbot')
    assert feature_store.is_enabled(scope, 'grok_bot')
    await run_command(current, '/grok 帮助')
    current.send.assert_awaited_once()
    grok = plugin_service.plugins['plugins.grok_bot'].module
    with patch.object(grok.GrokConfig, 'from_env', return_value=SimpleNamespace()), \\
         patch.object(grok.queue, 'run', AsyncMock(return_value='已修复')) as repair_run:
        await run_command(admin, '/grok 修复会话')
        repair_run.assert_not_awaited()
        assert '仅 SuperUser' in str(admin.send.await_args.args[0])
        await run_command(current, '/grok 修复会话')
        repair_run.assert_awaited_once()
        assert repair_run.await_args.kwargs['repair_only'] is True
        assert repair_run.await_args.args[3].peer_id == '100'

    # Legacy commands are filtered too, without disabling the manager.
    await run_command(current, '/插件 关闭 help')
    assert not feature_store.is_enabled(scope, 'help_plugin')
    await run_command(current, '/help hyw')
    current.send.assert_not_awaited()
    await run_command(current, '/功能 开启 help')
    await run_command(current, '/help hyw')
    current.send.assert_awaited_once()

    for rejected in (group_session(user='member'), group_session(private=True)):
        await run_command(rejected, '/功能 关闭 hyw')
        rejected.send.assert_awaited_once()
        assert feature_store.is_enabled(scope, 'hyw')
    admin = group_session(user='group-admin')
    admin.event.member.roles = [Role('admin')]
    await run_command(admin, '/功能 关闭 hyw')
    assert not feature_store.is_enabled(scope, 'hyw')
    await run_command(current, '/功能 开启 hyw')

    # Actual passive listener, using the loaded module and its registered scope.
    bili = plugin_service.plugins['plugins.bilibilibot'].module
    listeners = [sub for plg in plugin_service.plugins.values()
                 if plg.path.startswith('plugins.bilibilibot')
                 for sub in (slot.subscriber for slot in plg._scope.subscribers)
                 if sub.callable_target.__qualname__.startswith('_EventHook.')]
    assert len(listeners) == 1, listeners
    with patch.object(bili.client, 'parse_link', AsyncMock(return_value=None)) as parse_link:
        await run_command(current, '/功能 关闭 bili')
        ctx = {ITEM_SESSION: current, ITEM_ACCOUNT: current.account, EVENT: current.event}
        assert await listeners[0].handle(ctx.copy()) is STOP
        parse_link.assert_not_awaited()
        await run_command(current, '/功能 开启 bili')
        await listeners[0].handle(ctx.copy())
        parse_link.assert_awaited_once()

asyncio.get_event_loop().run_until_complete(check_group_switches())

async def check_usage_recording():
    from otae_bot.command_usage import recorder as usage
    current = group_session()
    scope = scope_from_event(current.account, current.event)
    with patch.object(usage.usage_recorder, 'add') as add:
        await run_command(current, '/q 帮助')
        await run_command(current, '/没有这个指令')
        feature_store.set_enabled(scope, 'hyw', False)
        try:
            await run_command(current, '/q 帮助')
        finally:
            feature_store.set_enabled(scope, 'hyw', True)
    records = [call.args[0] for call in add.call_args_list]
    assert [(r.plugin, r.ok, r.group_id, r.user_id) for r in records] == [('hyw', True, '100', 'test-root')], records

asyncio.get_event_loop().run_until_complete(check_usage_recording())

async def check_attendance_dispatch():
    # There is no standalone /签到 command; each game's own 签到 also signs the
    # other game, and needs the injected account for that game's group switch.
    assert 'plugins.signin' not in plugin_service.plugins
    session = group_session(user='test-root')
    await run_command(session, '/签到')
    session.send.assert_not_awaited()
    arknights = sys.modules['plugins.arknights.handlers']
    endfield = sys.modules['plugins.endfield.handlers']
    recorded = AsyncMock()
    session = group_session(user='test-root')
    with patch.object(arknights, '_handle_attendance', recorded):
        await run_command(session, '/ak 签到 1234')
    recorded.assert_awaited_once()
    assert recorded.await_args.kwargs['bot'] is session.account, recorded.await_args
    assert recorded.await_args.args[3] == '1234', recorded.await_args
    recorded = AsyncMock()
    session = group_session(user='test-root')
    with (patch.object(endfield, '_handle_attendance', recorded),
          patch.object(endfield.CredentialCipher, 'from_env', return_value=object())):
        await run_command(session, '/ef 签到 1234')
    recorded.assert_awaited_once()
    assert recorded.await_args.kwargs['bot'] is session.account, recorded.await_args
    assert recorded.await_args.args[3].account_selector == '1234', recorded.await_args
    # Both games must have registered their attendance capability while loading.
    from otae_bot.hypergryph.attendance_registry import registry
    assert registry.games() == ('endfield', 'arknights'), registry.games()

asyncio.get_event_loop().run_until_complete(check_attendance_dispatch())

# A direct call to the cleanup function misses Entari's parameter injection.
async def check_attendance_real_unload_reload():
    from arclet.entari import Entari, load_plugin
    from arclet.entari.plugin import unload_plugin_async
    from otae_bot.hypergryph.attendance_registry import registry
    old = registry.get('arknights')
    with patch.object(Entari, 'current', return_value=app):
        assert await unload_plugin_async('plugins.arknights')
        await asyncio.sleep(.05)
    assert registry.get('arknights') is None, registry.games()
    assert registry.get('endfield') is not None
    assert load_plugin('plugins.arknights')
    await asyncio.sleep(.05)
    assert registry.get('arknights').module is not old.module
    # The reloaded plugin is gated and counted without reinstalling anything.
    assert_attached('plugins.arknights', GroupFeatureGate, CommandUsageRecorder)
    assert len(registry) == 2

asyncio.get_event_loop().run_until_complete(check_attendance_real_unload_reload())

async def check_announcement_dispatch():
    current = group_session()
    endfield = plugin_service.plugins['plugins.endfield'].module
    runtime = endfield.announcement_runtime
    await run_command(current, '/ef 公告 帮助')
    current.send.assert_awaited_once()
    assert '国服公告提醒' in str(current.send.await_args.args[0])
    await run_command(current, '/ef 公告 订阅 活动 签到')
    assert '已订阅' in str(current.send.await_args.args[0]), current.send.await_args_list
    saved = runtime.store.subscriptions()
    assert len(saved) == 1
    assert saved[0].destination.account_id == 'test-bot'
    assert set(saved[0].kinds) == {'activity', 'signin'}
    await run_command(current, '/ef 公告 提前 开始 30')
    assert runtime.store.subscriptions()[0].start_minutes == 30
    assert runtime.store.subscriptions()[0].notify_started
    await run_command(current, '/ef 公告 开始推送 关闭')
    assert not runtime.store.subscriptions()[0].notify_started
    await run_command(current, '/ef 公告 开始推送 开启')
    assert runtime.store.subscriptions()[0].notify_started
    member = group_session(user='ordinary-member')
    await run_command(member, '/ef 公告 开始推送 关闭')
    assert '仅群主' in str(member.send.await_args.args[0])
    assert runtime.store.subscriptions()[0].notify_started
    await run_command(member, '/ef 公告 取消订阅')
    assert '仅群主' in str(member.send.await_args.args[0])
    scope = scope_from_event(current.account, current.event)
    feature_store.set_enabled(scope, 'endfield', False)
    await run_command(current, '/ef 公告 取消订阅')
    current.send.assert_not_awaited()
    assert len(runtime.store.subscriptions()) == 1
    feature_store.set_enabled(scope, 'endfield', True)
    await run_command(current, '/ef 公告 取消订阅')
    assert not runtime.store.subscriptions()

asyncio.get_event_loop().run_until_complete(check_announcement_dispatch())

# Exercise the actual incoming-event path, including quote lookup and the
# prefix filter. CommandExecute alone skips the prefix filter that caused this bug.
from datetime import datetime, timezone
from itertools import count
from arclet.entari import MessageCreatedEvent, load_plugin
from arclet.letoderea import publish
from otae_bot.groups.features import GroupScope
from satori import At, Author, Channel, Event, Guild, Image, Login, MessageObject, Quote, Text, User
# Entari 0.19: built-in niche plugins only run their body when the app starts.
plugin_service.plugins['$.commands'].exec_apply()
ids = count()
hyw = plugin_service.plugins['plugins.hyw'].module

def quote_event(elements, *, group='100', author='quoted-user', inline=False):
    quote_body = [Text('被引用的正文'), Image(src='https://example.com/quoted.png')]
    quoted = MessageObject.from_elements('quoted-id', quote_body, user=User(author))
    account = SimpleNamespace(platform='qq', self_id='test-bot',
                              protocol=SimpleNamespace(message_get=AsyncMock(return_value=quoted)))
    if inline:
        authors = [] if inline == 'without-author' else [Author(author)]
        elements = [Quote('quoted-id', content=[*authors, *quote_body]), *elements[1:]]
    origin = Event('message-created', datetime.now(timezone.utc),
                   Login(user=User('test-bot'), platform='qq'), channel=Channel(group, ChannelType.TEXT),
                   guild=Guild(group), user=User('sender'),
                   message=MessageObject.from_elements('incoming-' + str(next(ids)), elements))
    return MessageCreatedEvent(account, origin)

async def check_quoted_command_dispatch():
    request = AsyncMock()
    with patch.object(hyw.HywConfig, 'from_env', return_value=hyw.HywConfig(api_key='test', timeout=1)), \
         patch.dict(hyw.handle_hyw.__globals__, run_request=request):
        for alias in ('q', 'hyw', '何意味'):
            for inline in (False, True, 'without-author'):
                request.reset_mock()
                event = quote_event([Quote('quoted-id'), Text(' '), At('quoted-user'),
                                     Text(' /' + alias + ' 解释 '), Image(src='https://example.com/current.png')],
                                    inline=inline)
                await publish(event, scope='.commands')
                request.assert_awaited_once()
                assert '被引用的正文' in request.await_args.args[3], request.await_args
                assert request.await_args.args[4] == ['https://example.com/current.png',
                                                     'https://example.com/quoted.png'], request.await_args
                if inline == 'without-author':
                    event.account.protocol.message_get.assert_awaited_once_with('100', 'quoted-id')
                elif inline:
                    event.account.protocol.message_get.assert_not_awaited()
                # Quote normalization is local to the command dispatcher.
                assert event.content.has(At)
        for elements, author in (
            ([Quote('quoted-id'), At('quoted-user'), Text(' /q')], 'quoted-user'),
            ([Quote('quoted-id'), Text('/q 解释')], 'quoted-user'),
            ([Quote('quoted-id'), At('test-bot'), Text(' /q 解释')], 'test-bot'),
        ):
            request.reset_mock()
            await publish(quote_event(elements, author=author), scope='.commands')
            request.assert_awaited_once()
            assert '被引用的正文' in request.await_args.args[3]
        for elements in ([At('quoted-user'), Text(' /q 解释')],
                         [Quote('quoted-id'), At('another-user'), Text(' /q 解释')],
                         [Quote('quoted-id'), At('quoted-user'), Text(' 普通回复')]):
            request.reset_mock()
            await publish(quote_event(elements), scope='.commands')
            request.assert_not_awaited()
        for text, mention in (('/q 解释', 'another-user'), ('普通回复', 'quoted-user')):
            request.reset_mock()
            event = quote_event([Quote('quoted-id'), At(mention), Text(' ' + text)], inline='without-author')
            await publish(event, scope='.commands')
            request.assert_not_awaited()
            if text == '普通回复':
                event.account.protocol.message_get.assert_not_awaited()
        request.reset_mock()
        with patch.object(command._commands.judge, 'need_notice_me', True):
            for inline in (False, 'without-author'):
                await publish(quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /q 解释')], inline=inline), scope='.commands')
                request.assert_not_awaited()
        scope = GroupScope('qq', 'test-bot', '100')
        feature_store.set_enabled(scope, 'hyw', False)
        await publish(quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /q 解释')], inline='without-author'), scope='.commands')
        request.assert_not_awaited()
        await publish(quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /q 解释')], group='101'), scope='.commands')
        request.assert_awaited_once()
        feature_store.set_enabled(scope, 'hyw', True)

asyncio.get_event_loop().run_until_complete(check_quoted_command_dispatch())

async def check_grok_quoted_dispatch():
    grok = plugin_service.plugins['plugins.grok_bot'].module
    run = AsyncMock(return_value='Grok 回答')
    with patch.object(grok.GrokConfig, 'from_env', return_value=SimpleNamespace()), \\
         patch.object(grok.queue, 'run', run), patch.object(Session, 'send', AsyncMock(return_value=[])) as send:
        for alias in ('grok', 'grokbot'):
            run.reset_mock()
            event = quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /' + alias + ' 解释'), Image(src='https://cdn.example/current.png')])
            event.account.protocol.message_get.return_value = MessageObject('quoted-id', '引用正文<img src="https://cdn.example/quoted.png"/>', user=User('quoted-user'))
            await publish(event, scope='.commands')
            run.assert_awaited_once()
            assert '引用正文' in run.await_args.args[1], run.await_args
            assert run.await_args.kwargs['images'] == ('https://cdn.example/current.png', 'https://cdn.example/quoted.png'), run.await_args
            assert run.await_args.kwargs['account'] is event.account
            assert send.await_args.kwargs['reply_to'] is True
            run.reset_mock()
            await publish(quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /' + alias + ' 解释')], inline='without-author'), scope='.commands')
            run.assert_awaited_once()
            assert '被引用的正文' in run.await_args.args[1]
            assert run.await_args.kwargs['images'] == ('https://example.com/quoted.png',)
        run.reset_mock()
        event = quote_event([Text(' /grok '), Image(src='https://cdn.example/only.png')])
        await publish(event, scope='.commands')
        run.assert_awaited_once()
        assert run.await_args.kwargs['images'] == ('https://cdn.example/only.png',)
        assert '图片进行识别' in run.await_args.args[1], run.await_args
        run.reset_mock()
        event = quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /grok')])
        event.account.protocol.message_get.return_value = MessageObject('quoted-id', '<img src="https://cdn.example/quoted-only.png"/>', user=User('quoted-user'))
        await publish(event, scope='.commands')
        run.assert_awaited_once()
        assert run.await_args.kwargs['images'] == ('https://cdn.example/quoted-only.png',)
        assert '图片进行识别' in run.await_args.args[1], run.await_args

    async def interrupted_run(*args, on_reply, **kwargs):
        await on_reply(grok.Reply('插入后的回答'), reply_to=False)
        return grok.Reply()

    with patch.object(grok.GrokConfig, 'from_env', return_value=SimpleNamespace()), \\
         patch.object(grok.queue, 'run', interrupted_run), patch.object(Session, 'send', AsyncMock(return_value=[])) as send:
        await publish(quote_event([Quote('quoted-id'), At('quoted-user'), Text(' /grok 追加')]), scope='.commands')
        send.assert_awaited_once()
        assert send.await_args.kwargs['reply_to'] is False

asyncio.get_event_loop().run_until_complete(check_grok_quoted_dispatch())

async def check_command_space_dispatch():
    # Alconna only splits on ASCII spaces: NBSP / U+3000 used to leave
    # "/zmd\\u00a0回响" as one head that no command matched.
    endfield = sys.modules['plugins.endfield.handlers']
    recorded = AsyncMock()
    with patch.object(endfield, '_handle_command', recorded):
        await publish(quote_event([Text('/zmd 回响 错视赛季')]), scope='.commands')
        recorded.assert_awaited_once()
        expected = recorded.await_args.args[2]
        assert (expected.action, expected.challenge_kind, expected.challenge_terms) == ('challenge', 'war_echo', ('错视赛季',)), expected
        for elements in ([Text('/zmd\\u00a0回响\\u00a0错视赛季')], [Text('/ef\\u3000回响\\u3000错视赛季')],
                         [Text('\\u00a0/zmd 回响\\u3000\\u00a0错视赛季')],
                         [Quote('quoted-id'), At('quoted-user'), Text('\\u00a0/ef\\u00a0回响\\u00a0错视赛季')]):
            recorded.reset_mock()
            event = quote_event(elements)
            await publish(event, scope='.commands')
            recorded.assert_awaited_once()
            assert recorded.await_args.args[2] == expected, (elements, recorded.await_args)
            # Only the command dispatcher sees the rewritten spaces.
            assert '\\u00a0' in str(event.content) or '\\u3000' in str(event.content)

asyncio.get_event_loop().run_until_complete(check_command_space_dispatch())

async def check_minecraft_rejoin_clears_the_kick_mark():
    from arclet.entari.event.base import GuildMemberAddedEvent
    broadcast = sys.modules['plugins.minecraft_plugin.broadcast']
    account = SimpleNamespace(platform='qq', self_id='test-bot')

    def joined(user):
        origin = Event('guild-member-added', datetime.now(timezone.utc), Login(user=User('test-bot'), platform='qq'),
                       guild=Guild('100'), user=User(user))
        return GuildMemberAddedEvent(account, origin)

    broadcast._kicked_groups.add('100')
    with patch.object(broadcast, 'get_bot', return_value=account):
        await publish(joined('someone'))
        assert '100' in broadcast._kicked_groups
        await publish(joined('test-bot'))
        assert '100' not in broadcast._kicked_groups

asyncio.get_event_loop().run_until_complete(check_minecraft_rejoin_clears_the_kick_mark())
print('CONTRACT ' + json.dumps({'plugins': sorted(expected), 'jobs': jobs}, ensure_ascii=False))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "assets", root / "assets")
            # Discover top-level names in the sandbox; imports resolve to source.
            for name in discover_plugins(ROOT / "plugins"):
                package = root / name.replace(".", "/")
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
            result = subprocess.run(
                [sys.executable, "-c", script, str(ROOT)], cwd=root,
                capture_output=True, text=True, timeout=45,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        contract = next(line.removeprefix("CONTRACT ") for line in result.stdout.splitlines() if line.startswith("CONTRACT "))
        loaded = json.loads(contract)["plugins"]
        self.assertEqual(loaded, sorted(discover_plugins(ROOT / "plugins")))
        self.assertIn("plugins.hyw", loaded)
        self.assertIn("plugins.grok_bot", loaded)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_stop_before_shared_resources_close(self):
        order = []

        async def stop_jobs():
            order.append("jobs")

        async def close_resource():
            self.assertEqual(order[0], "jobs")
            order.append("resource")

        with (
            patch.object(lifecycle, "close_scheduled_jobs", AsyncMock(side_effect=stop_jobs)),
            patch.object(lifecycle, "close_http_client", AsyncMock(side_effect=close_resource)),
            patch.object(lifecycle, "close_image_executor", AsyncMock(side_effect=close_resource)),
            patch.object(lifecycle, "close_browser", AsyncMock(side_effect=close_resource)),
        ):
            await lifecycle.close_shared_resources()
        self.assertEqual(order, ["jobs", "resource", "resource", "resource"])
