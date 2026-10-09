"""Unified ``/签到`` entry point: registry, orchestration and the game hooks.

Everything here runs against synthetic data — temporary SQLite files, fake
clients and a fake session — so no real account, credential or network call is
involved.  The Endfield service is loaded through a private package name (like
``tests/test_endfield_daily.py``) so the loop can be exercised without pulling
in the Entari plugin scope.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from satori import ChannelType

from otae_bot import attendance_registry as registry_module
from otae_bot.adapters.entari import ArgVal

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "endfield_attendance_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[PACKAGE] = package

endfield_crypto = _load(f"{PACKAGE}.account.crypto", "plugins/endfield/account/crypto.py")
endfield_client = _load(f"{PACKAGE}.account.client", "plugins/endfield/account/client.py")
endfield_gacha = _load(f"{PACKAGE}.gacha.service", "plugins/endfield/gacha/service.py")
endfield_models = _load(f"{PACKAGE}.catalog.models", "plugins/endfield/catalog/models.py")
endfield_attendance = _load(f"{PACKAGE}.attendance", "plugins/endfield/attendance.py")


# ------------------------------------------------------------------- helpers


def capability(game, *, roles=(), sign=None, owner=None, label="", module=None):
    """Build a capability whose bound roles are fixed by the test."""

    async def _sign(user_id, *, group):
        return registry_module.AttendanceResult(text=f"{game} 结果")

    return registry_module.AttendanceCapability(
        game=game,
        owner=owner or f"plugins.{game}.handlers",
        module=module if module is not None else sys.modules[__name__],
        roles=lambda user_id: list(roles),
        sign=sign or _sign,
        label=label,
    )


def outcome(game, status, text="", png=None):
    return registry_module.AttendanceOutcome(
        game, registry_module.game_label(game), status, text, png
    )


# ------------------------------------------------------------------ registry


class AttendanceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = registry_module.AttendanceRegistry()

    def test_registration_is_keyed_by_game_id_and_replaces(self):
        first = capability("endfield")
        self.registry.register(first)
        self.assertIs(self.registry.get("endfield"), first)
        second = capability("endfield")
        self.registry.register(second)
        self.assertIs(self.registry.get("endfield"), second)
        self.assertEqual(len(self.registry), 1)
        self.assertIn("endfield", self.registry)
        self.assertTrue(self.registry.unregister("endfield"))
        self.assertFalse(self.registry.unregister("endfield"))
        self.assertEqual(len(self.registry), 0)

    def test_a_capability_without_a_game_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(capability(""))

    def test_games_keep_the_documented_execution_order(self):
        self.registry.register(capability("arknights"))
        self.registry.register(capability("endfield"))
        self.assertEqual(self.registry.games(), ("endfield", "arknights"))
        # An unknown game id is still run, after the known ones.
        self.registry.register(capability("zeta"))
        self.assertEqual(self.registry.games(), ("endfield", "arknights", "zeta"))

    def test_unregister_owner_drops_stale_modules_but_keeps_hot_reloads(self):
        live = sys.modules[__name__]
        stale = types.ModuleType("plugins.demo.handlers")
        owner = "plugins.demo.handlers"

        # A plugin that was unloaded: its module is no longer the live one.
        self.registry.register(capability("endfield", owner=owner, module=stale))
        with mock.patch.dict(sys.modules, {owner: None}):
            self.assertEqual(self.registry.unregister_owner(owner), 1)
        self.assertIsNone(self.registry.get("endfield"))

        # A hot reload already registered its fresh capability, so the late
        # PluginUnloaded event of the old module must not remove it.
        self.registry.register(capability("endfield", owner=owner, module=live))
        with mock.patch.dict(sys.modules, {owner: live}):
            self.assertEqual(self.registry.unregister_owner(owner), 0)
        self.assertIsNotNone(self.registry.get("endfield"))
        self.assertEqual(self.registry.unregister_owner(""), 0)


class PluginUnloadedHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = registry_module.AttendanceRegistry()
        self.enterContext(mock.patch.object(registry_module, "registry", self.registry))

    async def test_the_unloaded_hook_releases_that_plugin(self):
        owner = "plugins.demo.handlers"
        self.registry.register(
            capability("endfield", owner=owner, module=types.ModuleType(owner))
        )
        with mock.patch.dict(sys.modules, {owner: None}):
            await registry_module.on_plugin_unloaded(SimpleNamespace(plugin_id=owner))
        self.assertEqual(self.registry.games(), ())

    async def test_an_unrelated_or_empty_plugin_id_is_ignored(self):
        self.registry.register(capability("arknights"))
        await registry_module.on_plugin_unloaded(SimpleNamespace(plugin_id="plugins.other"))
        await registry_module.on_plugin_unloaded(SimpleNamespace())
        self.assertEqual(self.registry.games(), ("arknights",))


# --------------------------------------------------------------- scheduling


class CollectOutcomesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = registry_module.AttendanceRegistry()
        self.enterContext(mock.patch.object(registry_module, "registry", self.registry))
        self.enabled_calls: list[str] = []

    def enabled(self, *disabled):
        disabled = set(disabled)

        def check(game):
            self.enabled_calls.append(game)
            return game not in disabled

        return check

    async def test_both_games_run_in_order_and_return_their_own_card(self):
        calls: list[tuple[str, str, bool]] = []

        async def endfield_sign(user_id, *, group):
            calls.append(("endfield", user_id, group))
            return registry_module.AttendanceResult(png=b"ef-card", text="终末地文本")

        async def arknights_sign(user_id, *, group):
            calls.append(("arknights", user_id, group))
            return registry_module.AttendanceResult(png=b"ak-card", text="明日方舟文本")

        self.registry.register(capability("endfield", roles=[object()], sign=endfield_sign))
        self.registry.register(capability("arknights", roles=[object()], sign=arknights_sign))
        outcomes = await registry_module.collect_outcomes(
            "7", group=True, enabled=self.enabled()
        )

        self.assertEqual(calls, [("endfield", "7", True), ("arknights", "7", True)])
        self.assertEqual([item.status for item in outcomes], ["signed", "signed"])
        self.assertEqual([item.png for item in outcomes], [b"ef-card", b"ak-card"])
        self.assertEqual(registry_module.build_notice(outcomes), "")

    async def test_only_the_bound_game_runs_and_the_other_stays_silent(self):
        self.registry.register(capability("endfield", roles=[]))
        self.registry.register(capability("arknights", roles=[object()]))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )

        self.assertEqual([item.status for item in outcomes], ["unbound", "signed"])
        # A single-game user must not be nagged about the other game.
        self.assertEqual(registry_module.build_notice(outcomes), "")

    async def test_no_binding_at_all_prompts_both_bind_commands(self):
        self.registry.register(capability("endfield", roles=[]))
        self.registry.register(capability("arknights", roles=[]))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )
        notice = registry_module.build_notice(outcomes)
        self.assertIn("/zmd 绑定", notice)
        self.assertIn("/ak 绑定", notice)
        self.assertIn("尚未绑定", notice)

    async def test_the_group_switch_uses_the_capability_feature_key(self):
        self.registry.register(
            registry_module.AttendanceCapability(
                game="endfield",
                owner="plugins.endfield.handlers",
                module=sys.modules[__name__],
                roles=lambda user_id: [object()],
                sign=capability("endfield").sign,
                feature_key="ef_preview",
            )
        )
        await registry_module.collect_outcomes("7", group=True, enabled=self.enabled())
        self.assertEqual(self.enabled_calls, ["ef_preview"])

    async def test_a_disabled_game_is_skipped_without_touching_its_interface(self):
        signed: list[str] = []

        async def arknights_sign(user_id, *, group):
            signed.append("arknights")
            return registry_module.AttendanceResult(png=b"ak-card", text="明日方舟文本")

        def endfield_roles(user_id):
            raise AssertionError("a disabled game must not be queried")

        self.registry.register(
            capability("endfield", roles=[object()], sign=endfield_roles)
        )
        self.registry.register(capability("arknights", roles=[object()], sign=arknights_sign))
        outcomes = await registry_module.collect_outcomes(
            "7", group=True, enabled=self.enabled("endfield")
        )

        self.assertEqual([item.status for item in outcomes], ["disabled", "signed"])
        self.assertEqual(signed, ["arknights"])
        self.assertIn("本群未开启", registry_module.build_notice(outcomes))

    async def test_an_unloaded_plugin_is_reported_as_unavailable_not_unbound(self):
        self.registry.register(capability("arknights", roles=[]))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )

        self.assertEqual([item.status for item in outcomes], ["unavailable", "unbound"])
        notice = registry_module.build_notice(outcomes)
        self.assertIn("插件未加载", notice)
        self.assertIn("明日方舟", notice)
        # The group switch is meaningless for a game that never registered.
        self.assertEqual(self.enabled_calls, ["arknights"])

    async def test_one_game_failing_never_stops_the_other(self):
        async def boom(user_id, *, group):
            raise RuntimeError("endfield db is locked")

        async def arknights_sign(user_id, *, group):
            return registry_module.AttendanceResult(png=b"ak-card", text="明日方舟文本")

        self.registry.register(capability("endfield", roles=[object()], sign=boom))
        self.registry.register(capability("arknights", roles=[object()], sign=arknights_sign))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )

        self.assertEqual([item.status for item in outcomes], ["failed", "signed"])
        notice = registry_module.build_notice(outcomes)
        self.assertIn("终末地", notice)
        self.assertIn(registry_module.FAILURE_RETRY_MESSAGE, notice)
        self.assertNotIn("db is locked", notice)

    async def test_a_binding_read_failure_is_contained_and_never_leaks_details(self):
        def broken_roles(user_id):
            raise OSError("no such table: roles")

        async def sign(user_id, *, group):
            return registry_module.AttendanceResult(text="终末地 结果")

        self.registry.register(
            registry_module.AttendanceCapability(
                game="endfield",
                owner="plugins.endfield.handlers",
                module=sys.modules[__name__],
                roles=broken_roles,
                sign=sign,
            )
        )
        self.registry.register(capability("arknights", roles=[object()]))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )
        self.assertEqual([item.status for item in outcomes], ["failed", "signed"])
        self.assertNotIn("no such table", registry_module.build_notice(outcomes))

    async def test_a_game_level_failure_keeps_its_own_message(self):
        async def refused(user_id, *, group):
            return registry_module.AttendanceResult(
                ok=False, text="未配置 ARKNIGHTS_CREDENTIAL_KEY"
            )

        self.registry.register(capability("arknights", roles=[object()], sign=refused))
        outcomes = await registry_module.collect_outcomes(
            "7", group=False, enabled=self.enabled()
        )
        arknights = next(item for item in outcomes if item.game == "arknights")
        self.assertEqual(arknights.status, "failed")
        self.assertIn("ARKNIGHTS_CREDENTIAL_KEY", registry_module.build_notice(outcomes))

    async def test_cancellation_and_entari_exit_still_propagate(self):
        from arclet.letoderea.exceptions import _ExitException

        async def cancelled(user_id, *, group):
            raise asyncio.CancelledError()

        async def stopping(user_id, *, group):
            raise _ExitException("stop")

        for sign, expected in ((cancelled, asyncio.CancelledError), (stopping, _ExitException)):
            with self.subTest(sign=sign):
                self.registry.clear()
                self.registry.register(capability("endfield", roles=[object()], sign=sign))
                with self.assertRaises(expected):
                    await registry_module.collect_outcomes(
                        "7", group=False, enabled=self.enabled()
                    )


class BuildNoticeTests(unittest.TestCase):
    def test_notes_for_problems_are_kept_alongside_a_successful_card(self):
        outcomes = [
            outcome("endfield", registry_module.SIGNED, "文本", None),
            outcome("arknights", registry_module.DISABLED, "明日方舟：本群未开启该功能，已跳过。"),
        ]
        notice = registry_module.build_notice(outcomes)
        self.assertEqual(notice, "明日方舟：本群未开启该功能，已跳过。")

    def test_unbound_is_named_when_nothing_ran(self):
        outcomes = [
            outcome("endfield", registry_module.UNBOUND, "终末地：尚未绑定。"),
            outcome("arknights", registry_module.DISABLED, "明日方舟：本群未开启该功能，已跳过。"),
        ]
        notice = registry_module.build_notice(outcomes)
        self.assertIn("终末地：尚未绑定。", notice)
        self.assertIn("本群未开启", notice)

    def test_nothing_to_say_when_every_card_is_sent(self):
        outcomes = [outcome("endfield", registry_module.SIGNED, "x", b"png")]
        self.assertEqual(registry_module.build_notice(outcomes), "")


# ------------------------------------------- endfield attendance service


def endfield_role(role_id: int, server_id: str = "1", nickname: str = "管理员"):
    return SimpleNamespace(
        id=role_id,
        role_id=str(role_id),
        server_id=server_id,
        nickname=nickname,
        masked_uid=f"****{role_id:04d}",
        server_name="官方服务器",
    )


class _FakeEndfieldStore:
    def __init__(self, tokens: dict[str, str], broken: tuple[str, ...] = ()):
        self.tokens = dict(tokens)
        self.broken = set(broken)

    def decrypt_token(self, role, _cipher):
        if role.role_id in self.broken:
            raise RuntimeError("database is locked")
        if role.role_id not in self.tokens:
            raise endfield_crypto.CredentialKeyError("终末地账号凭据解密失败")
        return self.tokens[role.role_id]


class _FakeEndfieldClient:
    def __init__(self, results):
        self.results = results
        self.calls: list[str] = []

    async def attendance(self, token, role):
        self.calls.append(role.role_id)
        result = self.results[token]
        if isinstance(result, Exception):
            raise result
        return result


class EndfieldAttendanceServiceTests(unittest.IsolatedAsyncioTestCase):
    def _roles(self):
        return [endfield_role(1, nickname="甲"), endfield_role(2, "2", nickname="乙")]

    async def test_calendar_survives_service_mapping_and_reaches_the_card(self):
        from lxml import html
        draw = importlib.import_module(f"{PACKAGE}.rendering.cards")
        result = endfield_client.AttendanceResult(
            "already", "今日已签到", monthly_count=1, calendar_days=3,
            milestones=(endfield_client.AttendanceMilestone(1, endfield_client.AttendanceReward("嵌晶玉", 80)),
                        endfield_client.AttendanceMilestone(3, endfield_client.AttendanceReward("嵌晶玉", 120))),
        )
        view = await endfield_attendance.sign_roles(
            _FakeEndfieldStore({"1": "t"}), _FakeEndfieldClient({"t": result}), None, self._roles()[:1],
        )
        capture = mock.AsyncMock(return_value=b"png")
        with mock.patch.object(draw, "_draw_neutral_card", capture):
            await draw.draw_attendance_card(view)
        document = html.fromstring(capture.await_args.args[1])
        self.assertEqual(len(document.find_class("milestone-cell")), 3)
        self.assertIn("done", document.find_class("milestone-cell")[0].classes)
        self.assertIn("80/ 200", document.find_class("milestone-summary")[0].text_content())
        self.assertIn("还需签到 2 天", document.text_content())

    async def test_success_already_and_failure_are_independent(self):
        store = _FakeEndfieldStore({"1": "token-a", "2": "token-b"})
        client = _FakeEndfieldClient(
            {
                "token-a": endfield_client.AttendanceResult("success", "签到成功", (), 3),
                "token-b": endfield_client.EndfieldAPIError("网络请求失败", "attendance", "500"),
            }
        )
        view = await endfield_attendance.sign_roles(
            store, client, None, self._roles(), generated_at="2026-01-01 00:00"
        )
        self.assertEqual([item.status for item in view.roles], ["success", "failed"])
        self.assertEqual(view.roles[0].monthly_count, 3)
        self.assertEqual([item.uid for item in view.roles], ["****0001", "****0002"])
        self.assertEqual(client.calls, ["1", "2"])

    async def test_expired_credential_does_not_stop_other_roles(self):
        store = _FakeEndfieldStore({"2": "token-b"})
        client = _FakeEndfieldClient(
            {"token-b": endfield_client.AttendanceResult("already", "今日已签到")}
        )
        view = await endfield_attendance.sign_roles(store, client, None, self._roles())
        self.assertEqual([item.status for item in view.roles], ["failed", "already"])
        self.assertIn("解密失败", view.roles[0].message)
        self.assertEqual(client.calls, ["2"])

    async def test_a_storage_fault_is_contained(self):
        store = _FakeEndfieldStore({"2": "token-b"}, broken=("1",))
        client = _FakeEndfieldClient(
            {"token-b": endfield_client.AttendanceResult("success", "签到成功")}
        )
        view = await endfield_attendance.sign_roles(store, client, None, self._roles())
        self.assertEqual([item.status for item in view.roles], ["failed", "success"])
        self.assertEqual(
            view.roles[0].message, endfield_attendance.FAILURE_RETRY_MESSAGE
        )

    async def test_the_shared_role_lock_blocks_a_duplicate_request(self):
        active = endfield_gacha.RoleTaskRegistry()
        store = _FakeEndfieldStore({"1": "token-a"})
        client = _FakeEndfieldClient({})
        with mock.patch.object(endfield_gacha, "ROLE_TASKS", active):
            async with active.claim(self._roles()[0]):
                view = await endfield_attendance.sign_one(
                    store, client, None, self._roles()[0], registry=active
                )
        self.assertEqual(view.status, "failed")
        self.assertIn("当前角色已有任务正在执行中", view.message)
        self.assertEqual(client.calls, [])

    def test_text_report_covers_every_status(self):
        view = endfield_models.AttendanceCardView(
            roles=[
                endfield_models.AttendanceRoleView(
                    "甲", "****0001", "官方服务器", "success", "签到成功",
                    rewards=[endfield_models.AttendanceRewardView("合成玉", 500)],
                    monthly_count=4,
                ),
                endfield_models.AttendanceRoleView(
                    "乙", "****0002", "B服", "already", "今日已签到"
                ),
                endfield_models.AttendanceRoleView(
                    "丙", "****0003", "官方服务器", "failed", "凭据已失效"
                ),
            ],
            generated_at="2026-01-01 00:00",
        )
        text = endfield_attendance.format_attendance_report(view)
        self.assertIn("终末地森空岛签到结果（3 个角色）", text)
        self.assertIn("签到成功：合成玉 × 500", text)
        self.assertIn("本月累签 4 天", text)
        self.assertIn("今日已签到，无需重复签到", text)
        self.assertIn("签到失败：凭据已失效", text)
        self.assertIn("生成时间：2026-01-01 00:00", text)


# -------------------------------------------------------- /签到 handler


class _FakeFeatureStore:
    def __init__(self, disabled=()):
        self.disabled = set(disabled)

    def is_enabled(self, scope, plugin):
        if scope is None:
            return True
        return plugin not in self.disabled


class _FakeSession:
    """Just enough Session for the handler, including ``stop()`` raising STOP."""

    def __init__(self, *, private: bool = True, group_id: str = "100", user_id: str = "7"):
        self.account = SimpleNamespace(platform="qq", self_id="test-bot")
        self.event = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild=None if private else SimpleNamespace(id=group_id),
            channel=SimpleNamespace(
                id=user_id if private else group_id,
                type=ChannelType.DIRECT if private else ChannelType.TEXT,
            ),
        )
        self.sent: list = []
        self.fail_sends = 0
        self.stopped = False

    async def send(self, message, *args, **kwargs):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("message connection interrupted")
        self.sent.append(message)
        return []

    def stop(self):
        self.stopped = True
        from arclet.letoderea.exceptions import _ExitException

        raise _ExitException("stop")


class SigninHandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from plugins.signin import handlers as signin_handlers

        cls.handlers = signin_handlers

    def setUp(self):
        self.registry = registry_module.AttendanceRegistry()
        self.enterContext(mock.patch.object(registry_module, "registry", self.registry))

    async def _run(self, session, rest=""):
        from arclet.letoderea.exceptions import _ExitException

        with self.assertRaises(_ExitException):
            await self.handlers.handle_signin(session, ArgVal(rest or None, bool(rest)))

    def test_the_command_declares_its_names(self):
        alconna = self.handlers.signin_cmd.alconna
        for name in ("签到", "checkin", "qiandao"):
            with self.subTest(name=name):
                self.assertTrue(alconna.parse(name).matched)

    async def test_two_games_produce_one_combined_card_and_no_notice(self):
        self.registry.register(capability("endfield", roles=[object()], sign=_png_sign("ef")))
        self.registry.register(capability("arknights", roles=[object()], sign=_png_sign("ak")))
        session = _FakeSession()
        self.enterContext(
            mock.patch.object(self.handlers, "feature_store", _FakeFeatureStore())
        )
        await self._run(session)

        self.assertEqual(len(session.sent), 1)
        self.assertTrue(session.stopped)
        self.assertTrue(all(not isinstance(item, str) for item in session.sent))

    async def test_a_group_switch_skips_one_game_and_explains_it(self):
        self.registry.register(capability("endfield", roles=[object()], sign=_png_sign("ef")))
        self.registry.register(capability("arknights", roles=[object()], sign=_png_sign("ak")))
        session = _FakeSession(private=False)
        self.enterContext(
            mock.patch.object(
                self.handlers, "feature_store", _FakeFeatureStore(disabled={"endfield"})
            )
        )
        await self._run(session)

        self.assertEqual(len(session.sent), 1)
        self.assertIn("本群未开启", str(session.sent[0]))

    async def test_a_private_chat_ignores_group_switches(self):
        self.registry.register(capability("endfield", roles=[object()], sign=_png_sign("ef")))
        self.registry.register(capability("arknights", roles=[object()], sign=_png_sign("ak")))
        session = _FakeSession(private=True)
        self.enterContext(
            mock.patch.object(
                self.handlers, "feature_store", _FakeFeatureStore(disabled={"endfield"})
            )
        )
        await self._run(session)

        # Private chats have no group scope, so both games run and no skip note
        # is produced.
        self.assertEqual(len(session.sent), 1)
        self.assertTrue(all(not isinstance(item, str) for item in session.sent))

    async def test_an_unbound_user_gets_one_bind_prompt(self):
        self.registry.register(capability("endfield", roles=[]))
        self.registry.register(capability("arknights", roles=[]))
        session = _FakeSession()
        self.enterContext(
            mock.patch.object(self.handlers, "feature_store", _FakeFeatureStore())
        )
        await self._run(session)

        self.assertEqual(len(session.sent), 1)
        self.assertIn("/zmd 绑定", session.sent[0])
        self.assertIn("/ak 绑定", session.sent[0])

    async def test_extra_arguments_return_the_usage_note(self):
        session = _FakeSession()
        await self._run(session, "全部")

        self.assertEqual(session.sent, [self.handlers.USAGE])
        self.assertEqual(self.registry.games(), ())

    async def test_a_delivery_failure_does_not_submit_a_second_sign_in(self):
        calls: list[str] = []

        async def sign(user_id, *, group):
            calls.append(user_id)
            return registry_module.AttendanceResult(png=_test_png("red"), text="文本")

        self.registry.register(capability("endfield", roles=[object()], sign=sign))
        self.registry.register(capability("arknights", roles=[object()], sign=sign))
        session = _FakeSession()
        session.fail_sends = 1
        self.enterContext(
            mock.patch.object(self.handlers, "feature_store", _FakeFeatureStore())
        )
        await self._run(session)

        self.assertEqual(calls, ["7", "7"])
        # Sending the combined image failed; retry the complete text, not sign-in.
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.sent[0], "文本\n\n文本")

    async def test_a_text_only_result_is_sent_as_text(self):
        async def endfield_sign(user_id, *, group):
            return registry_module.AttendanceResult(
                png=None, text="终末地森空岛签到结果（1 个角色）"
            )

        async def arknights_sign(user_id, *, group):
            return registry_module.AttendanceResult(
                png=None, text="明日方舟森空岛签到结果（1 个角色）"
            )

        self.registry.register(capability("endfield", roles=[object()], sign=endfield_sign))
        self.registry.register(capability("arknights", roles=[object()], sign=arknights_sign))
        session = _FakeSession()
        self.enterContext(
            mock.patch.object(self.handlers, "feature_store", _FakeFeatureStore())
        )
        await self._run(session)

        self.assertEqual(
            session.sent,
            ["终末地森空岛签到结果（1 个角色）\n\n明日方舟森空岛签到结果（1 个角色）"],
        )


def _test_png(color: str) -> bytes:
    output = BytesIO()
    with Image.new("RGB", (32, 24), color) as image:
        image.save(output, "PNG")
    return output.getvalue()


def _png_sign(tag: str):
    async def sign(user_id, *, group):
        return registry_module.AttendanceResult(png=_test_png("blue" if tag == "ak" else "yellow"), text=f"{tag} 文本")

    return sign


# ------------------------------------------------ real plugin registration


class GameCapabilityWiringTests(unittest.IsolatedAsyncioTestCase):
    """The two game plugins must register themselves while they load."""

    @classmethod
    def setUpClass(cls):
        from plugins import arknights, endfield

        cls.arknights = arknights.handlers
        cls.endfield = endfield.handlers

    def test_both_games_registered_under_their_plugin_module(self):
        self.assertIn("endfield", registry_module.registry)
        self.assertIn("arknights", registry_module.registry)
        self.assertEqual(registry_module.registry.games(), ("endfield", "arknights"))
        self.assertEqual(
            registry_module.registry.get("endfield").owner,
            "plugins.endfield.handlers",
        )
        self.assertEqual(
            registry_module.registry.get("arknights").owner,
            "plugins.arknights.handlers",
        )

    async def test_arknights_capability_signs_every_bound_role(self):
        from plugins.arknights import client as arknights_client
        from plugins.arknights import crypto as arknights_crypto
        from plugins.arknights import store as arknights_store

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = arknights_store.ArknightsStore(Path(directory.name) / "ak.db")
        self.addCleanup(store.close)
        cipher = arknights_crypto.ArknightsCipher(b"k" * 32)
        store.bind_roles(
            "7",
            "token-a",
            [
                arknights_store.RoleCandidate("10001234", "1", "甲", "官服"),
                arknights_store.RoleCandidate("20005678", "2", "乙", "B服"),
            ],
            cipher,
        )

        class _Client:
            def __init__(self):
                self.calls: list[str] = []

            async def attendance(self, token, role):
                self.calls.append(role.uid)
                return arknights_client.AttendanceResult("already", "今日已签到", ())

        client = _Client()
        stub = type("_Cipher", (), {"from_env": classmethod(lambda cls: cipher)})
        with (
            mock.patch.object(self.arknights, "_store_instance", lambda: store),
            mock.patch.object(self.arknights, "_client_instance", mock.AsyncMock(return_value=client)),
            mock.patch.object(self.arknights, "ArknightsCipher", stub),
            mock.patch.object(
                self.arknights, "draw_attendance_card", mock.AsyncMock(return_value=None)
            ),
        ):
            capability_ = registry_module.registry.get("arknights")
            self.assertEqual(len(capability_.roles("7")), 2)
            result = await capability_.sign("7", group=True)

        self.assertEqual(client.calls, ["10001234", "20005678"])
        self.assertIsNone(result.png)  # the renderer was unavailable
        self.assertIn("****1234", result.text)  # group chat keeps masked UIDs
        self.assertNotIn("10001234", result.text)

    async def test_the_unified_entry_shares_the_arknights_role_lock(self):
        from plugins.arknights import client as arknights_client
        from plugins.arknights import crypto as arknights_crypto
        from plugins.arknights import store as arknights_store
        from plugins.arknights import tasks as arknights_tasks

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = arknights_store.ArknightsStore(Path(directory.name) / "ak.db")
        self.addCleanup(store.close)
        cipher = arknights_crypto.ArknightsCipher(b"k" * 32)
        bound = store.bind_roles(
            "7",
            "token-a",
            [arknights_store.RoleCandidate("10001234", "1", "甲", "官服")],
            cipher,
        )

        class _Client:
            def __init__(self):
                self.calls = 0

            async def attendance(self, token, role):
                self.calls += 1
                return arknights_client.AttendanceResult("success", "签到成功", ())

        client = _Client()
        stub = type("_Cipher", (), {"from_env": classmethod(lambda cls: cipher)})
        with (
            mock.patch.object(self.arknights, "_store_instance", lambda: store),
            mock.patch.object(self.arknights, "_client_instance", mock.AsyncMock(return_value=client)),
            mock.patch.object(self.arknights, "ArknightsCipher", stub),
            mock.patch.object(
                self.arknights, "draw_attendance_card", mock.AsyncMock(return_value=b"png")
            ),
        ):
            # ``/ak 签到`` is already signing this very role.
            async with arknights_tasks.ROLE_TASKS.claim(bound[0]):
                result = await registry_module.registry.get("arknights").sign("7", group=False)

        self.assertEqual(client.calls, 0)
        self.assertIn("正在签到", result.text)

    async def test_endfield_capability_uses_its_own_store_and_reports_masked_uids(self):
        from plugins.endfield.account.client import AttendanceResult

        role = SimpleNamespace(
            id=1,
            role_id="1",
            server_id="1",
            nickname="管理员",
            masked_uid="****0001",
            server_name="官方服务器",
        )

        class _Store:
            def list_roles(self, user_id):
                return [role]

            def decrypt_token(self, item, _cipher):
                return "token-a"

        class _Client:
            def __init__(self):
                self.calls = 0

            async def attendance(self, token, item):
                self.calls += 1
                return AttendanceResult("success", "签到成功", (), 2)

        client = _Client()
        stub = type("_Cipher", (), {"from_env": classmethod(lambda cls: object())})
        with (
            mock.patch.object(self.endfield, "account_store", _Store()),
            mock.patch.object(self.endfield, "official_client", client),
            mock.patch.object(self.endfield, "CredentialCipher", stub),
            mock.patch.object(
                self.endfield, "draw_attendance_card", mock.AsyncMock(return_value=None)
            ),
        ):
            result = await registry_module.registry.get("endfield").sign("7", group=True)

        self.assertEqual(client.calls, 1)
        self.assertIsNone(result.png)
        self.assertIn("****0001", result.text)
        self.assertIn("签到成功", result.text)

    async def test_endfield_capability_reports_a_missing_key_without_calling_the_api(self):
        class _Store:
            def list_roles(self, user_id):
                return [object()]

        class _Cipher:
            @classmethod
            def from_env(cls):
                raise self.endfield.CredentialKeyError("未配置 ENDFIELD_CREDENTIAL_KEY")

        with mock.patch.object(self.endfield, "account_store", _Store()), mock.patch.object(
            self.endfield, "CredentialCipher", _Cipher
        ):
            result = await registry_module.registry.get("endfield").sign("7", group=False)

        self.assertFalse(result.ok)
        self.assertIn("ENDFIELD_CREDENTIAL_KEY", result.text)

    async def test_an_unbound_user_never_triggers_a_key_error(self):
        class _Store:
            def list_roles(self, user_id):
                return []

        class _Cipher:
            @classmethod
            def from_env(cls):
                raise AssertionError("an unbound user must not resolve the key")

        with (
            mock.patch.object(self.endfield, "account_store", _Store()),
            mock.patch.object(self.endfield, "CredentialCipher", _Cipher),
        ):
            endfield_capability = registry_module.registry.get("endfield")
            self.assertEqual(list(endfield_capability.roles("7")), [])
            endfield_result = await endfield_capability.sign("7", group=False)
        self.assertFalse(endfield_result.ok)
        self.assertIn("/zmd 绑定", endfield_result.text)

        with (
            mock.patch.object(self.arknights, "_store_instance", lambda: _Store()),
            mock.patch.object(self.arknights, "ArknightsCipher", _Cipher),
        ):
            arknights_capability = registry_module.registry.get("arknights")
            self.assertEqual(list(arknights_capability.roles("7")), [])
            arknights_result = await arknights_capability.sign("7", group=False)
        self.assertFalse(arknights_result.ok)
        self.assertIn("/ak 绑定", arknights_result.text)


if __name__ == "__main__":
    unittest.main()
