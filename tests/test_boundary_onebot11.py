"""OneBot 11 boundary adapter, driven with recorded-shape LLBot payloads; no network."""

from __future__ import annotations

import asyncio
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
from otae_bot.config import settings
from tests.support.config import loaded_config_file
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


LOGIN = Login(sn=0, status=LoginStatus.ONLINE, adapter="onebot", platform="qq-client", user=User(SELF))


def request_event(sub_type: str | None, **extra) -> dict:
    """LLBot 8.3.2 request payloads (OB11GroupRequest*Event / OB11FriendRequestEvent)."""
    raw = {"time": 1762900000, "self_id": int(SELF), "post_type": "request", "user_id": 10001,
           "comment": "问题：暗号\n答案：<b>&</b>", "flag": "183643844|1762900000123|1|0"}
    if sub_type is None:
        raw |= {"request_type": "friend", "flag": "u_abc|1762900000", "via": "QQ号查找"}
    else:
        raw |= {"request_type": "group", "sub_type": sub_type, "group_id": int(GROUP)}
    return raw | extra


class RequestEventTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, **handlers):
        return make_adapter(get_group_info={"group_id": int(GROUP), "group_name": "测试群"},
                            get_stranger_info={"user_id": 10001, "nickname": "alice"}, **handlers)

    async def test_join_request_is_a_guild_member_request_carrying_the_flag(self):
        adapter, _ = self._adapter()
        event = await events["request.group.add"](LOGIN, adapter, request_event("add", invitor_id=0))
        self.assertEqual(event.type, "guild-member-request")
        self.assertEqual((event.guild.id, event.guild.name), (GROUP, "测试群"))
        self.assertEqual((event.user.id, event.user.name), ("10001", "alice"))
        self.assertEqual(event.member.user.id, "10001")
        self.assertEqual(event.message.id, "183643844|1762900000123|1|0")
        self.assertEqual(event.message.content, "问题：暗号\n答案：&lt;b&gt;&amp;&lt;/b&gt;")
        self.assertEqual([e.text for e in event.message.message], ["问题：暗号\n答案：<b>&</b>"])

    async def test_join_request_survives_missing_names_and_comment(self):
        adapter, _ = make_adapter()  # get_group_info / get_stranger_info both fail
        event = await events["request.group.add"](LOGIN, adapter, request_event("add", comment=""))
        self.assertEqual(event.type, "guild-member-request")
        self.assertEqual((event.guild.id, event.guild.name, event.user.id), (GROUP, None, "10001"))
        self.assertEqual((event.message.id, event.message.content), ("183643844|1762900000123|1|0", ""))

    async def test_invite_and_friend_requests_keep_stock_mapping(self):
        adapter, _ = self._adapter()
        invite = await events["request.group.invite"](LOGIN, adapter, request_event("invite", source_group_id=0))
        self.assertEqual(invite.type, "guild-request")
        self.assertEqual((invite.guild.id, invite.guild.name, invite.user.id), (GROUP, "测试群", "10001"))
        self.assertEqual(invite.message.id, "183643844|1762900000123|1|0")
        friend = await events["request.friend"](LOGIN, adapter, request_event(None))
        self.assertEqual(friend.type, "friend-request")
        self.assertEqual((friend.user.id, friend.message.id), ("10001", "u_abc|1762900000"))

    async def test_other_adapters_keep_the_stock_join_mapping(self):
        fake = FakeOneBot(get_group_info={"group_name": "g"}, get_stranger_info={"nickname": "n"})
        net = Mock(call_api=fake)
        event = await events["request.group.add"](LOGIN, net, request_event("add"))
        self.assertEqual(event.type, "guild-member-added")

    async def test_approve_routes_pass_the_flag_back(self):
        adapter, fake = make_adapter(set_group_add_request=lambda _: None, set_friend_add_request=lambda _: None)
        flag = "183643844|1762900000123|1|0"
        await adapter.routes[Api.GUILD_MEMBER_APPROVE.value](
            request(Api.GUILD_MEMBER_APPROVE, {"message_id": flag, "approve": True, "comment": ""}))
        await adapter.routes[Api.GUILD_APPROVE.value](
            request(Api.GUILD_APPROVE, {"message_id": flag, "approve": False, "comment": "no"}))
        await adapter.routes[Api.FRIEND_APPROVE.value](
            request(Api.FRIEND_APPROVE, {"message_id": "u_abc|1762900000", "approve": True, "comment": ""}))
        self.assertEqual(fake.calls, [
            ("set_group_add_request", {"flag": flag, "sub_type": "add", "approve": True, "reason": ""}),
            ("set_group_add_request", {"flag": flag, "sub_type": "invite", "approve": False, "reason": "no"}),
            ("set_friend_add_request", {"flag": "u_abc|1762900000", "approve": True}),
        ])

    async def test_request_handler_reads_the_flag_from_the_boundary_event(self):
        from arclet.entari.event.base import GuildMemberRequestEvent

        from plugins.request_handler import handlers

        adapter, _ = self._adapter()
        raw_event = await events["request.group.add"](LOGIN, adapter, request_event("add"))
        event = GuildMemberRequestEvent(Mock(), raw_event)
        self.assertEqual(handlers._request_id(event), "183643844|1762900000123|1|0")


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    """upload.create + message.create through the server's request handler, as Entari calls it."""

    def setUp(self):
        from satori.server import Server

        self.server = Server()
        self.adapter, self.fake = make_adapter(send_group_msg={"message_id": 7}, upload_group_file={"file_id": "f"})
        self.server.apply(self.adapter)
        self.adapter.logins[SELF] = Login(sn=0, status=LoginStatus.ONLINE, adapter="onebot", platform="qq-client",
                                          user=User(SELF))

    def tearDown(self):
        self.server._tempdir.cleanup()

    async def _call(self, action: Api, *, json: dict | None = None, form=None):
        from starlette.requests import Request as StarletteRequest

        headers = [(b"satori-platform", b"qq-client"), (b"satori-user-id", SELF.encode())]
        req = StarletteRequest({"type": "http", "method": "POST", "path_params": {"action": action.value},
                                "headers": headers})
        if form is not None:
            req._form = form
        else:
            req._json = json
        return await self.server.http_server_handler(req)

    async def _upload(self, data: bytes, mime: str, filename: str) -> str:
        import io
        import json

        from starlette.datastructures import FormData, Headers, UploadFile

        upload = UploadFile(io.BytesIO(data), filename=filename, headers=Headers({"content-type": mime}))
        # The headers entari-plugin-server's direct mode builds: no Content-Disposition.
        with patch.object(asyncio.get_running_loop(), "call_later") as later:
            resp = await self._call(Api.UPLOAD_CREATE, form=FormData([("a", upload)]))
        self.assertEqual(resp.status_code, 200, resp.body)
        url = json.loads(resp.body)["a"]
        self.assertTrue(url.startswith(f"internal:qq-client/{SELF}/_tmp/"), url)
        self.assertTrue(url.endswith(f"-{filename}"), url)
        delay, unlink, *_ = later.call_args.args
        self.assertEqual((delay, unlink.__self__), (600, Path(self.server._tempdir.name) / url.rsplit("/", 1)[1]))
        return url

    async def _send(self, content: str, channel: str = GROUP):
        return await self._call(Api.MESSAGE_CREATE, json={"channel_id": channel, "content": content})

    def _sent(self, action: str = "send_group_msg") -> list:
        return [params for name, params in self.fake.calls if name == action]

    def test_upload_create_is_routed_and_declared(self):
        self.assertIn("upload.create", self.adapter.routes)
        self.assertIn("upload.create", self.adapter.features)

    async def test_uploaded_image_audio_video_are_sent_inline(self):
        import base64

        url = await self._upload(PNG, "image/png", "radar.png")
        resp = await self._send(f'[测试] <img src="{url}"/>')
        self.assertEqual(resp.status_code, 200, resp.body)
        (message,) = self._sent()
        self.assertEqual(message["message"][0], {"type": "text", "data": {"text": "[测试] "}})
        image = message["message"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["data"]["file"], "base64://" + base64.b64encode(PNG).decode())
        for tag, kind in (("audio", "record"), ("video", "video")):
            with self.subTest(tag=tag):
                media = await self._upload(b"media", "application/octet-stream", f"x.{tag}")
                await self._send(f'<{tag} src="{media}"/>')
                sent = self._sent()[-1]["message"][0]
                self.assertEqual((sent["type"], sent["data"]["file"]), (kind, "base64://bWVkaWE="))

    async def test_proxied_upload_url_and_nested_elements_resolve(self):
        url = await self._upload(PNG, "image/png", "a.png")
        proxied = f"http://127.0.0.1:5140/v1/proxy/{url}"
        await self._send(f'<quote id="1"/><img src="{proxied}"/>')
        self.assertTrue(self._sent()[0]["message"][-1]["data"]["file"].startswith("base64://iVBORw0KGgo"))

    async def test_uploaded_file_keeps_its_name(self):
        url = await self._upload(b"hello", "text/plain", "notes.txt")
        resp = await self._send(f'<file src="{url}"/>')
        self.assertEqual(resp.status_code, 200, resp.body)
        (params,) = self._sent("upload_group_file")
        self.assertEqual(params, {"group_id": int(GROUP), "file": "base64://aGVsbG8=", "name": "notes.txt"})

    async def test_unknown_expired_or_foreign_internal_urls_fail_without_sending(self):
        url = await self._upload(PNG, "image/png", "gone.png")
        (Path(self.server._tempdir.name) / url.rsplit("/", 1)[1]).unlink()
        cases = {
            url: 404,
            f"internal:qq-client/{SELF}/_tmp/nope.png": 404,
            f"internal:qq-client/{SELF}/_tmp/../escape.png": 404,
            f"internal:qq-client/{SELF}/_api/get_msg": 400,
        }
        for src, status in cases.items():
            with self.subTest(src=src):
                resp = await self._send(f'text <img src="{src}"/>')
                self.assertEqual(resp.status_code, status, resp.body)
                self.assertIn(b"upload" if status == 404 else b"only upload.create", resp.body)
        self.assertEqual(self._sent(), [])

    async def test_ordinary_urls_are_left_to_the_stock_encoder(self):
        await self._send('<img src="https://example.com/a.png"/><at id="1"/>')
        self.assertEqual([seg["type"] for seg in self._sent()[0]["message"]], ["image", "at"])
        self.assertEqual(self._sent()[0]["message"][0]["data"]["file"], "https://example.com/a.png")


class WiringTests(unittest.TestCase):
    def _create_app(self):
        names = ("install_message_log_redaction", "install_recent_alerts", "install_quote_fetch_fallback",
                 "install_command_input_normalizers", "install_group_feature_gates",
                 "install_command_usage_recorders", "listen")
        patches = {name: Mock() for name in names}
        with (
            patch.multiple(application, **patches),
            patch.object(application, "Entari") as entari,
            patch.object(application, "discover_plugins", return_value=("plugins.demo",)),
            patch.object(application, "load_plugin") as load_plugin,
        ):
            application.create_app()
        return entari, load_plugin

    def test_connections_come_from_entari_yml(self):
        yml = (
            "basic:\n  network:\n"
            "    - {type: ws, host: one, port: 5501, token: \"${{ env.T1 :- }}\"}\n"
            "    - {type: ws, host: two, port: 5502, path: /satori}\n"
        )
        with loaded_config_file(yml, "T1=test-token\n") as (config, _):
            entari, load_plugin = self._create_app()
        entari.from_config.assert_called_once_with(config)
        self.assertEqual([c.args for c in load_plugin.call_args_list], [("plugins.demo",)])
        self.assertEqual([n.dump()["token"] for n in config.basic.network], ["test-token", None])

    def test_without_a_loaded_configuration_the_app_has_no_connection(self):
        with settings.override({}):
            entari, load_plugin = self._create_app()
        entari.assert_called_once_with()
        self.assertEqual([c.args for c in load_plugin.call_args_list], [("plugins.demo",)])

    def test_server_section_loads_the_boundary_before_plugins(self):
        yml = (
            "basic:\n  network: []\n"
            "plugins:\n  server:\n    host: 0.0.0.0\n    port: 5140\n    adapters:\n"
            "      - endpoint: ws://127.0.0.1:3001\n        access_token: ${{ env.ONEBOT_TOKEN_1 :- }}\n"
        )
        with loaded_config_file(yml, "ONEBOT_TOKEN_1=test-token\n") as (_, root):
            entari, load_plugin = self._create_app()
            declared = settings.server_declaration()
        entari.from_config.assert_called_once()
        name, config = load_plugin.call_args_list[0].args
        self.assertEqual(name, SERVER_PLUGIN)
        self.assertTrue(config["direct_adapter"])
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["adapters"], [{"$path": ONEBOT11_ADAPTER, "endpoint": "ws://127.0.0.1:3001",
                                                "access_token": "test-token"}])
        self.assertEqual(load_plugin.call_args_list[1].args, ("plugins.demo",))
        # The forced values and the fresh token never reach the loaded file's data.
        self.assertNotIn("token", declared)
        self.assertEqual(declared["host"], "0.0.0.0")

    def test_server_listens_on_a_private_unix_socket(self):
        import os
        import tempfile

        if os.name == "nt":
            self.skipTest("unix sockets only")
        with tempfile.TemporaryDirectory() as directory:
            config = server_plugin_config({"adapters": [{"endpoint": "ws://h:1"}]}, Path(directory))
            socket_dir = Path(directory) / "satori-server"
            self.assertEqual(config["options"]["uds"], str(socket_dir / "satori.sock"))
            self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(config["adapters"][0]["access_token"])
        self.assertEqual(config["adapters"][0]["$path"], ONEBOT11_ADAPTER)
        self.assertTrue(config["token"])
        self.assertFalse(config["options"]["access_log"])


if __name__ == "__main__":
    unittest.main()
