"""请求处理：好友申请自动同意，群邀请 / 入群申请转超级用户审批，Ark 群卡片只通知。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import quoteattr

from arclet.entari.event.base import FriendRequestEvent, GuildMemberRequestEvent, GuildRequestEvent
from satori import ChannelType, Event, Guild, Login, Member, MessageObject, User

from plugins.request_handler import handlers
from plugins.request_handler.ark import parse_ark_invite_segment

ARK = json.dumps({"meta": {"news": {
    "title": "测试群",
    "jumpUrl": "https://qun.qq.com/qunpro/robot/qunshare?groupcode=100&senderuin=7&msgseq=9&邀请你加入群聊",
}}}, ensure_ascii=False)


def _elements(content: str):
    return MessageObject("m", content).message


def _account():
    return SimpleNamespace(
        self_id="bot", platform="qq-client", adapter="onebot",
        friend_approve=AsyncMock(), guild_approve=AsyncMock(), guild_member_approve=AsyncMock(),
        protocol=SimpleNamespace(send_private_message=AsyncMock(return_value=[SimpleNamespace(id="m")])),
    )


def _event(cls, account, **fields):
    login = Login(user=User("bot"), platform="qq-client")
    return cls(account, Event(cls.type.value, datetime.now(timezone.utc), login, **fields))


class ArkTests(unittest.TestCase):
    def test_card_is_read_from_onebot_json_and_llonebot_ark(self):
        for tag in ("onebot:json", "llonebot:ark"):
            with self.subTest(tag=tag):
                info = parse_ark_invite_segment(_elements(f"<{tag} data={quoteattr(ARK)}/>")[0])
                self.assertEqual((info["group_code"], info["group_name"], info["inviter_uin"]), ("100", "测试群", "7"))

    def test_other_elements_and_other_cards_are_ignored(self):
        normal = json.dumps({"meta": {"news": {"title": "normal card"}}})
        for content in ("普通消息", f"<onebot:xml data={quoteattr(ARK)}/>", f"<onebot:json data={quoteattr(normal)}/>"):
            with self.subTest(content=content):
                self.assertIsNone(parse_ark_invite_segment(_elements(content)[0]))

    async def _notify(self, content):
        account = _account()
        sent = account.protocol.send_private_message
        event = SimpleNamespace(message=SimpleNamespace(message=_elements(content)), content=None)
        with patch.object(handlers, "superuser", "42"), patch.object(handlers.ark_invite, "finish", AsyncMock()):
            self.assertTrue(await handlers._is_ark_group_invite(event))
            await handlers.handle_ark_group_invite(event, account)
        return account, sent

    def test_ark_card_only_notifies_the_superuser(self):
        import asyncio

        account, sent = asyncio.run(self._notify(f"<onebot:json data={quoteattr(ARK)}/>"))
        sent.assert_awaited_once()
        self.assertEqual(sent.await_args.args[0], "42")
        self.assertIn("收到群邀请卡片消息", str(sent.await_args.args[1]))
        self.assertEqual(handlers._pending, {})
        account.guild_approve.assert_not_awaited()


class RequestEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handlers._pending.clear()
        self.addCleanup(handlers._pending.clear)
        patcher = patch.object(handlers, "superuser", "42")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_friend_requests_are_approved_with_the_event_request_id(self):
        account = _account()
        event = _event(FriendRequestEvent, account, user=User("7", "张三"), message=MessageObject("flag-1", "你好"))
        session = SimpleNamespace(event=event, stop=lambda: None)
        await handlers.handle_friend_request(session, account)
        account.friend_approve.assert_awaited_once_with(request_id="flag-1", approve=True, comment="")

    async def test_guild_and_member_requests_wait_for_the_superuser_and_use_standard_approve(self):
        cases = [
            (GuildRequestEvent, dict(user=User("7", "张三"), guild=Guild("100", "测试群")), "guild_approve"),
            (GuildMemberRequestEvent, dict(user=User("8", "李四"), guild=Guild("100", "测试群"), member=Member(User("8"))),
             "guild_member_approve"),
        ]
        for cls, fields, api in cases:
            for decision in (True, False):
                with self.subTest(event=cls.__name__, decision=decision):
                    handlers._pending.clear()
                    account = _account()
                    event = _event(cls, account, message=MessageObject("flag-2", ""), **fields)
                    notify = AsyncMock()
                    with patch.object(handlers, "_notify_superuser", notify):
                        await handlers.handle_guild_request(SimpleNamespace(event=event, stop=lambda: None), account)
                    notify.assert_awaited_once_with(account, "测试群", fields["user"].name, "bot:flag-2", "100")
                    reply = SimpleNamespace(
                        user=User("42"), guild=None, channel=SimpleNamespace(id="private:42", type=ChannelType.DIRECT),
                        message=SimpleNamespace(message=_elements("同意" if decision else "拒绝")), content=None,
                    )
                    self.assertTrue(handlers._is_superuser_reply(account, reply))
                    send = AsyncMock()
                    with patch.object(handlers.approve_handler, "send", send), \
                            patch.object(handlers.approve_handler, "finish", AsyncMock()):
                        await handlers.handle_approve_reply(reply, account)
                    getattr(account, api).assert_awaited_once_with(request_id="flag-2", approve=decision, comment="")
                    self.assertEqual(send.await_args.args[0], f"已{'同意' if decision else '拒绝'}加入 测试群。")
                    self.assertEqual(handlers._pending, {})

    async def test_superuser_is_notified_privately_and_a_failed_notice_drops_the_request(self):
        account = _account()
        handlers._pending["bot:flag-3"] = {"guild_name": "测试群"}
        await handlers._notify_superuser(account, "测试群", "张三", "bot:flag-3", "100")
        user, message = account.protocol.send_private_message.await_args.args
        self.assertEqual(user, "42")
        self.assertIn("群号：100", str(message))
        self.assertIn("bot:flag-3", handlers._pending)
        account.protocol.send_private_message.side_effect = RuntimeError("not a friend")
        await handlers._notify_superuser(account, "测试群", "张三", "bot:flag-3", "100")
        self.assertNotIn("bot:flag-3", handlers._pending)

    def test_listeners_are_bound_to_the_entari_event_classes(self):
        from pathlib import Path

        source = Path(handlers.__file__).read_text(encoding="utf-8")
        self.assertIn("listen(FriendRequestEvent, priority=5)(handle_friend_request)", source)
        self.assertIn("listen(GuildRequestEvent, priority=5)(handle_guild_request)", source)
        self.assertIn("listen(GuildMemberRequestEvent, priority=5)(handle_guild_request)", source)
        self.assertNotIn("__class__.__name__", source)


if __name__ == "__main__":
    unittest.main()
