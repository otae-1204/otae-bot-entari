"""OneBot 11 boundary adapter, driven with recorded-shape LLBot payloads; no network."""

from __future__ import annotations

import ast
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from satori import Api, LoginStatus
from satori.adapters.onebot11 import message as stock_message
from satori.adapters.onebot11.events.base import events
from satori.adapters.onebot11.utils import ROLE_MAPPING
from satori.element import Message, Quote, Text
from satori.exception import ActionFailed
from satori.model import Login, MessageObject, User
from satori.parser import parse
from satori.server import Request

from otae_bot import application
from otae_bot.boundary import ONEBOT11_ADAPTER, SERVER_PLUGIN, server_plugin_config
from otae_bot.boundary.onebot11 import (
    FORWARD_MAX_DEPTH,
    FORWARD_MAX_MESSAGES,
    OneBot11ForwardConfig,
    OneBotBoundaryAdapter,
    decode,
)

ROOT = Path(__file__).resolve().parents[1]
GROUP = "183643844"
SELF = "3525876051"


def member(uin: int, role: str, card: str = "") -> dict:
    return {"group_id": int(GROUP), "user_id": uin, "nickname": f"n{uin}", "card": card,
            "role": role, "join_time": 1700000000, "level": "1", "title": ""}


def history_message(seq: int, text: str = "hi", *, message=None) -> dict:
    return {"self_id": int(SELF), "user_id": 10001, "time": 1762900000 + seq, "message_id": -seq,
            "message_seq": seq, "message_type": "group", "group_id": int(GROUP), "group_name": "测试群",
            "sender": {"user_id": 10001, "nickname": "alice", "card": "A", "role": "admin"},
            "message": message if message is not None else [{"type": "text", "data": {"text": text}}],
            "message_format": "array", "post_type": "message", "sub_type": "normal"}


def node(uin: int, content: list, *, key: str = "content") -> dict:
    return {"sender": {"nickname": f"n{uin}", "user_id": uin}, "time": 1762900000, key: content}


def text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def forward(resid: str) -> dict:
    return {"type": "forward", "data": {"id": resid}}


def reply(message_id: str) -> dict:
    return {"type": "reply", "data": {"id": message_id}}


class FakeOneBot:
    """call_api stand-in: action -> payload, or callable(params) -> payload."""

    def __init__(self, **handlers):
        self.handlers = {"get_version_info": {"app_name": "LLOneBot", "app_version": "8.3.2"}, **handlers}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, action: str, params: dict | None = None):
        params = params or {}
        self.calls.append((action, params))
        handler = self.handlers.get(action)
        if handler is None:
            raise ActionFailed(f"100: {action} failed", {"status": "failed", "retcode": 100})
        result = handler(params) if callable(handler) else handler
        if isinstance(result, Exception):
            raise result
        return result

    def count(self, action: str) -> int:
        return sum(1 for name, _ in self.calls if name == action)


def make_adapter(**handlers) -> tuple[OneBotBoundaryAdapter, FakeOneBot]:
    adapter = OneBotBoundaryAdapter(OneBot11ForwardConfig("ws://127.0.0.1:1"))
    fake = FakeOneBot(**handlers)
    adapter.call_api = fake  # type: ignore[method-assign]
    return adapter, fake


def request(action: Api, params: dict) -> Request:
    return Request(None, action.value, params, platform="qq-client", self_id=SELF)  # type: ignore[arg-type]


def forward_store(trees: dict[str, list]):
    """get_forward_msg backed by resid -> node list."""

    def get_forward_msg(params):
        nodes = trees.get(params.get("id"))
        if nodes is None:
            return ActionFailed("100: 消息为空", {"status": "failed", "retcode": 100})
        return {"messages": nodes}

    return get_forward_msg


class MemberRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_get_and_list_carry_onebot_roles(self):
        members = [member(1, "owner", "群主卡片"), member(2, "admin"), member(3, "member")]
        adapter, _ = make_adapter(
            get_group_member_list=members,
            get_group_member_info=lambda params: next(m for m in members if m["user_id"] == params["user_id"]),
        )
        got = await adapter.routes[Api.GUILD_MEMBER_GET.value](
            request(Api.GUILD_MEMBER_GET, {"guild_id": GROUP, "user_id": "1"}))
        self.assertEqual([(role.id, role.name) for role in got.roles], [("owner", "群主")])
        self.assertEqual((got.user.id, got.nick), ("1", "群主卡片"))
        self.assertEqual(got.joined_at, datetime.fromtimestamp(1700000000))
        page = await adapter.routes[Api.GUILD_MEMBER_LIST.value](request(Api.GUILD_MEMBER_LIST, {"guild_id": GROUP}))
        self.assertEqual([[role.id for role in m.roles] for m in page.data], [["owner"], ["admin"], ["member"]])

    async def test_roles_match_the_stock_event_roles(self):
        from satori.adapters.onebot11.utils import ROLE_MAPPING

        adapter, _ = make_adapter(get_group_member_info=member(2, "admin"))
        got = await adapter.routes[Api.GUILD_MEMBER_GET.value](
            request(Api.GUILD_MEMBER_GET, {"guild_id": GROUP, "user_id": "2"}))
        self.assertIs(got.roles[0], ROLE_MAPPING["admin"])

    async def test_unknown_role_gives_no_roles(self):
        adapter, _ = make_adapter(get_group_member_info=member(4, "stranger"))
        got = await adapter.routes[Api.GUILD_MEMBER_GET.value](
            request(Api.GUILD_MEMBER_GET, {"guild_id": GROUP, "user_id": "4"}))
        self.assertEqual(got.roles, [])


class MessageListTests(unittest.IsolatedAsyncioTestCase):
    def history(self, latest: int = 30):
        def get_group_msg_history(params):
            end = int(params.get("message_seq") or latest)
            count = int(params.get("count", 20))
            return {"messages": [history_message(seq) for seq in range(max(1, end - count + 1), end + 1)]}

        return get_group_msg_history

    async def test_next_none_reads_latest_page_from_dict_history(self):
        adapter, fake = make_adapter(get_group_msg_history=self.history())
        page = await adapter.routes[Api.MESSAGE_LIST.value](
            request(Api.MESSAGE_LIST, {"channel_id": GROUP, "next": None, "direction": "before", "limit": 5,
                                       "order": "asc"}))
        self.assertEqual([m.id for m in page.data], ["-26", "-27", "-28", "-29", "-30"])
        self.assertEqual(page.next, "25")
        self.assertEqual(fake.calls[-1], ("get_group_msg_history", {"count": 5, "group_id": int(GROUP)}))
        first = page.data[0]
        self.assertEqual(first.content, "hi")
        self.assertEqual((first.user.id, first.member.nick, [r.id for r in first.member.roles]),
                         ("10001", "A", ["admin"]))
        self.assertEqual((first.channel.id, first.guild.name), (GROUP, "测试群"))
        self.assertEqual(first.created_at, datetime.fromtimestamp(1762900026))

    async def test_paging_with_next_reaches_the_first_message(self):
        adapter, fake = make_adapter(get_group_msg_history=self.history(latest=12))
        route = adapter.routes[Api.MESSAGE_LIST.value]
        seen, token = [], None
        for _ in range(5):
            page = await route(request(Api.MESSAGE_LIST, {"channel_id": GROUP, "next": token, "limit": 5}))
            seen.extend(m.id for m in page.data)
            token = page.next
            if token is None:
                break
        self.assertEqual(seen, ["-8", "-9", "-10", "-11", "-12", "-3", "-4", "-5", "-6", "-7", "-1", "-2"])
        self.assertIsNone(token)
        self.assertEqual(fake.calls[-1][1]["message_seq"], 2)

    async def test_order_desc_and_empty_history(self):
        adapter, _ = make_adapter(get_group_msg_history=self.history())
        page = await adapter.routes[Api.MESSAGE_LIST.value](
            request(Api.MESSAGE_LIST, {"channel_id": GROUP, "limit": 3, "order": "desc"}))
        self.assertEqual([m.id for m in page.data], ["-30", "-29", "-28"])
        adapter, _ = make_adapter(get_group_msg_history={"messages": []})
        page = await adapter.routes[Api.MESSAGE_LIST.value](request(Api.MESSAGE_LIST, {"channel_id": GROUP}))
        self.assertEqual((page.data, page.next), ([], None))

    async def test_private_history_uses_friend_action(self):
        raw = history_message(4)
        raw.update(message_type="private", sender={"user_id": 10001, "nickname": "alice"})
        adapter, fake = make_adapter(get_friend_msg_history={"messages": [raw]})
        page = await adapter.routes[Api.MESSAGE_LIST.value](
            request(Api.MESSAGE_LIST, {"channel_id": "private:10001", "next": "4", "limit": 1}))
        self.assertEqual(fake.calls[-1], ("get_friend_msg_history",
                                          {"count": 1, "message_seq": 4, "user_id": 10001}))
        self.assertEqual((page.data[0].channel.id, page.data[0].member, page.next), ("private:10001", None, "3"))

    async def test_other_implementations_keep_stock_route_without_next_crash(self):
        adapter, _ = make_adapter(get_version_info={"app_name": "Unknown"})
        page = await adapter.routes[Api.MESSAGE_LIST.value](
            request(Api.MESSAGE_LIST, {"channel_id": GROUP, "next": None}))
        self.assertEqual(page.data, [])


class LoginFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_features_list_every_routed_api_plus_guild_plain(self):
        adapter, _ = make_adapter()
        features = adapter.features
        self.assertEqual(features[-1], "guild.plain")
        routed = {path for path in adapter.routes if not path.startswith("internal/")}
        self.assertEqual(set(features[:-1]), routed)
        for api in ("message.create", "message.get", "message.list", "guild.member.get", "guild.member.list",
                    "guild.list", "reaction.create", "friend.approve"):
            self.assertIn(api, features)
        self.assertNotIn("internal/*", features)

    async def test_login_posted_by_stock_code_carries_features(self):
        adapter, _ = make_adapter()
        login = Login(sn=0, status=LoginStatus.ONLINE, adapter="onebot", platform="qq-client", user=User(SELF), features=["guild.plain"])
        adapter.logins[SELF] = login  # what the stock _login_update does before posting
        self.assertEqual(login.features, adapter.features)
        self.assertEqual((await adapter.get_logins())[0].features, adapter.features)


class ForwardDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_forward_is_id_only_without_api_calls(self):
        adapter, fake = make_adapter()
        content = await decode([forward("RES-1")], adapter)
        self.assertEqual(content, '<message id="RES-1" forward="true"/>')
        self.assertEqual(fake.calls, [])
        (element,) = parse(content)
        self.assertEqual((element.type, element.attrs["id"], element.children), ("message", "RES-1", []))

    async def test_forward_elements_survive_the_client_side_parser(self):
        # satori's transform() raised on the bare ``forward`` attribute, so Entari lost
        # every event carrying a merged forward (seen live when one was quoted).
        from satori import transform

        adapter, _ = make_adapter(
            get_msg={"message_id": 77, "sender": {"user_id": 9, "nickname": "quoted"},
                     "message": [forward("OUTER")]},
            get_forward_msg=forward_store({"OUTER": [node(1, [text("第一条")])]}),
        )
        for segments in ([forward("RES-1")], [reply("77"), text(" /q 解释")]):
            elements = transform(parse(await decode(segments, adapter)))
            candidates = (elements[0], *getattr(elements[0], "children", []))
            forwards = [e for e in candidates if type(e) is Message]
            self.assertTrue(forwards and all(e.forward is True for e in forwards), elements)

    async def test_quoted_forward_expands_nested_children(self):
        trees = {
            "OUTER": [node(1, [text("第一条")]), node(2, [forward("INNER")], key="message")],
            "INNER": [node(3, [text("里层")])],
        }
        adapter, fake = make_adapter(
            get_msg={"message_id": 77, "sender": {"user_id": 9, "nickname": "quoted"},
                     "message": [forward("OUTER")]},
            get_forward_msg=forward_store(trees),
        )
        content = await decode([reply("77"), text(" /q 解释")], adapter)
        quote = parse(content)[0]
        self.assertEqual((quote.type, quote.attrs["id"]), ("quote", "77"))
        author, outer = quote.children
        self.assertEqual((author.type, author.attrs["id"]), ("author", "9"))
        self.assertEqual((outer.type, outer.attrs["id"], outer.attrs["forward"]), ("message", "OUTER", "true"))
        first, second = outer.children
        self.assertEqual([c.type for c in first.children], ["author", "text"])
        self.assertEqual((first.children[0].attrs, first.children[1].attrs["text"]),
                         ({"id": "1", "name": "n1"}, "第一条"))
        inner = second.children[1]
        self.assertEqual((inner.attrs["id"], inner.children[0].children[1].attrs["text"]), ("INNER", "里层"))
        self.assertTrue(content.endswith(" /q 解释"))
        self.assertEqual([p["id"] for a, p in fake.calls if a == "get_forward_msg"], ["OUTER", "INNER"])

    async def test_expansion_stops_at_max_depth(self):
        depth = FORWARD_MAX_DEPTH + 2
        trees = {f"L{i}": [node(i, [forward(f"L{i + 1}")])] for i in range(1, depth + 1)}
        adapter, fake = make_adapter(
            get_msg={"sender": {"user_id": 9, "nickname": "q"}, "message": [forward("L1")]},
            get_forward_msg=forward_store(trees),
        )
        quote = parse(await decode([reply("1")], adapter))[0]
        self.assertEqual(fake.count("get_forward_msg"), FORWARD_MAX_DEPTH)
        level, element = 0, quote.children[1]
        while element.children:
            level += 1
            element = element.children[0].children[1]
        self.assertEqual(level, FORWARD_MAX_DEPTH)
        self.assertEqual((element.attrs["id"], element.children), (f"L{FORWARD_MAX_DEPTH + 1}", []))

    async def test_expansion_stops_at_max_messages_in_total(self):
        trees = {
            "BIG": [node(1, [forward("NESTED")])] + [node(i, [text(str(i))]) for i in range(2, 300)],
            "NESTED": [node(i, [text("n")]) for i in range(50)],
        }
        adapter, _ = make_adapter(
            get_msg={"sender": {"user_id": 9, "nickname": "q"}, "message": [forward("BIG")]},
            get_forward_msg=forward_store(trees),
        )
        quote = parse(await decode([reply("1")], adapter))[0]
        outer = quote.children[1]
        nested = outer.children[0].children[1]
        self.assertEqual(len(nested.children), 50)
        self.assertEqual(len(outer.children) + len(nested.children), FORWARD_MAX_MESSAGES)

    async def test_failed_expansion_degrades_to_id_only(self):
        for failure in (ActionFailed("100: 消息为空", {}), {"unexpected": True}, TimeoutError("slow")):
            with self.subTest(failure=failure):
                adapter, _ = make_adapter(
                    get_msg={"sender": {"user_id": 9, "nickname": "q"}, "message": [forward("GONE"), text("尾巴")]},
                    get_forward_msg=lambda params, f=failure: f,
                )
                content = await decode([reply("5")], adapter)
                self.assertIn('<message id="GONE" forward="true"/>', content)
                self.assertIn("尾巴", content)

    async def test_nested_failure_keeps_the_outer_expansion(self):
        adapter, _ = make_adapter(
            get_msg={"sender": {"user_id": 9, "nickname": "q"}, "message": [forward("OUT")]},
            get_forward_msg=forward_store({"OUT": [node(1, [forward("MISSING")]), node(2, [text("ok")])]}),
        )
        outer = parse(await decode([reply("5")], adapter))[0].children[1]
        self.assertEqual(len(outer.children), 2)
        self.assertEqual((outer.children[0].children[1].attrs["id"], outer.children[0].children[1].children),
                         ("MISSING", []))


class ReplyDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_get_msg_gives_bare_quote_and_keeps_the_rest(self):
        adapter, _ = make_adapter()  # no get_msg handler: ActionFailed
        content = await decode([reply("123"), {"type": "at", "data": {"qq": "42"}}, text(" /q 解释")], adapter)
        self.assertEqual(content, '<quote id="123"/><at id="42"/> /q 解释')

    async def test_group_event_survives_failed_quote_lookup(self):
        adapter, _ = make_adapter(
            get_group_info={"group_id": int(GROUP), "group_name": "测试群"},
            get_group_member_info=member(10001, "member", "A"),
        )
        login = Login(sn=0, status=LoginStatus.ONLINE, adapter="onebot", platform="qq-client", user=User(SELF))
        raw = history_message(9, message=[reply("123"), text("还在")])
        raw.update(post_type="message", message_type="group", sub_type="normal")
        event = await events["message.group.normal"](login, adapter, raw)
        self.assertEqual(event.type, "message-created")
        self.assertEqual(event.message.content, '<quote id="123"/>还在')

    async def test_group_event_is_built_from_the_payload_without_api_calls(self):
        # The stock handler costs two lookups (~0.7 s on LLBot 8.3.2) per group message.
        adapter, fake = make_adapter()
        login = Login(sn=0, status=LoginStatus.ONLINE, adapter="onebot", platform="qq-client", user=User(SELF))
        raw = history_message(9, message=[text("hi")])
        raw.update(post_type="message", message_type="group", sub_type="normal", group_name="测试群")
        raw["sender"].update(role="admin", card="名片")
        event = await events["message.group.normal"](login, adapter, raw)
        self.assertEqual(fake.calls, [])
        self.assertEqual((event.guild.name, event.channel.name, event.member.nick), ("测试群", "测试群", "名片"))
        self.assertEqual([role.id for role in event.member.roles], [ROLE_MAPPING["admin"].id])

    async def test_quoted_reply_inside_quote_is_not_chased(self):
        adapter, fake = make_adapter(
            get_msg={"sender": {"user_id": 9, "nickname": "q"}, "message": [reply("1"), text("原文")]},
        )
        quote = parse(await decode([reply("2")], adapter))[0]
        self.assertEqual(fake.count("get_msg"), 1)
        self.assertEqual((quote.children[1].type, quote.children[1].attrs["id"]), ("quote", "1"))

    async def test_other_adapters_keep_stock_decoding(self):
        class Other:
            async def call_api(self, action, params=None):
                raise AssertionError("no API call expected")

        from satori.adapters.onebot11.events import message as stock_events

        make_adapter()  # installs the dispatching decode
        self.assertEqual(await stock_events.decode([forward("X")], Other()), '<onebot:forward id="X"/>')
        self.assertEqual(await stock_message.decode([forward("X")], Other()), '<onebot:forward id="X"/>')

    async def test_message_get_route_uses_boundary_decoding(self):
        adapter, _ = make_adapter(
            get_msg=lambda params: (
                {"message_id": -1, "message_type": "group", "sender": {"user_id": 1, "nickname": "a"},
                 "message": [reply("2"), forward("F")]} if params["message_id"] == -1 else ActionFailed("gone", {})
            ),
        )
        result = await adapter.routes[Api.MESSAGE_GET.value](
            request(Api.MESSAGE_GET, {"channel_id": GROUP, "message_id": "-1"}))
        self.assertIsInstance(result, MessageObject)
        self.assertEqual(result.content, '<quote id="2"/><message id="F" forward="true"/>')
        self.assertEqual(str(Quote("2", content=[Text("x")])), '<quote id="2">x</quote>')


class WiringTests(unittest.TestCase):
    def _create_app(self, satori: list, onebot: list):
        names = ("install_message_log_redaction", "install_recent_alerts", "install_quote_fetch_fallback",
                 "install_command_input_normalizers", "install_group_feature_gates",
                 "install_command_usage_recorders", "listen")
        patches = {name: Mock() for name in names}
        with (
            patch.multiple(application, **patches),
            patch.object(application, "SATORI_CLIENTS", satori),
            patch.object(application, "ONEBOT_CLIENTS", onebot),
            patch.object(application, "Entari") as entari,
            patch.object(application, "WS", side_effect=lambda **values: values),
            patch.object(application, "discover_plugins", return_value=("plugins.demo",)),
            patch.object(application, "load_plugin") as load_plugin,
        ):
            application.create_app()
        return entari, load_plugin

    def test_without_onebot_clients_app_is_built_as_before(self):
        clients = [{"host": "one", "port": 5501, "token": "test-token"}]
        entari, load_plugin = self._create_app(clients, [])
        entari.assert_called_once_with({"host": "one", "port": 5501, "path": "", "token": "test-token"})
        self.assertEqual([c.args for c in load_plugin.call_args_list], [("plugins.demo",)])
        entari, _ = self._create_app([], [])
        entari.assert_called_once_with({"host": "localhost", "port": 5500, "path": "", "token": None})

    def test_onebot_clients_load_the_server_plugin_without_default_network(self):
        onebot = [{"endpoint": "ws://127.0.0.1:3001", "token": "test-token"}]
        entari, load_plugin = self._create_app([], onebot)
        entari.assert_called_once_with()
        name, config = load_plugin.call_args_list[0].args
        self.assertEqual(name, SERVER_PLUGIN)
        self.assertTrue(config["direct_adapter"])
        self.assertEqual(config["adapters"], [{"$path": ONEBOT11_ADAPTER, "endpoint": "ws://127.0.0.1:3001",
                                                "access_token": "test-token"}])
        self.assertEqual(load_plugin.call_args_list[1].args, ("plugins.demo",))
        entari, _ = self._create_app([{"host": "one", "port": 5501}], onebot)
        entari.assert_called_once_with({"host": "one", "port": 5501, "path": "", "token": None})

    def test_server_listens_on_a_private_unix_socket(self):
        import os
        import tempfile

        if os.name == "nt":
            self.skipTest("unix sockets only")
        with tempfile.TemporaryDirectory() as directory:
            config = server_plugin_config([{"endpoint": "ws://h:1"}], Path(directory))
            socket_dir = Path(directory) / "satori-server"
            self.assertEqual(config["options"]["uds"], str(socket_dir / "satori.sock"))
            self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(config["adapters"][0]["access_token"])
        self.assertTrue(config["token"])

    def test_only_the_boundary_imports_onebot_adapters(self):
        for path in (ROOT / "otae_bot").rglob("*.py"):
            if "boundary" in path.relative_to(ROOT / "otae_bot").parts:
                continue
            for item in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(item, ast.ImportFrom):
                    names = [item.module or ""]
                elif isinstance(item, ast.Import):
                    names = [alias.name for alias in item.names]
                else:
                    continue
                for name in names:
                    self.assertFalse(name.startswith("satori.adapters"), (path, name))


if __name__ == "__main__":
    unittest.main()
