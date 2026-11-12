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
from tests.support.dispatch import run_scenarios

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

    async def test_guild_and_member_requests_wait_for_the_superuser(self):
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
                    # 审批与回执走真实派发，见 RequestDispatchTests

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


DISPATCH_SCENARIOS = r'''
import json
from xml.sax.saxutils import quoteattr

handlers = plugin('plugins.request_handler.handlers')
handlers.superuser = '42'
account = default_account
for api in ('friend.approve', 'guild.approve', 'guild.member.approve'):
    account.protocol.answers[api] = {}

ARK = json.dumps({"meta": {"news": {
    "title": "测试群",
    "jumpUrl": "https://qun.qq.com/qunpro/robot/qunshare?groupcode=100&senderuin=7&msgseq=9&邀请你加入群聊",
}}}, ensure_ascii=False)


def calls(name):
    return [params for action, params in account.protocol.calls if action == name]


async def request(kind, flag, **fields):
    origin = Event(kind, datetime.now(timezone.utc), Login(user=User('test-bot'), platform='qq'),
                   message=MessageObject(flag, ''), **fields)
    await app.handle_event(account, origin)


async def main():
    # 好友申请：自动同意，用事件里的请求 ID
    await request('friend-request', 'flag-1', user=User('7', '张三'))
    assert calls('friend.approve') == [{'message_id': 'flag-1', 'approve': True, 'comment': ''}], calls('friend.approve')

    # 群邀请：私聊通知超级用户；超级用户私聊回「同意」才审批，并在私聊里回执
    await request('guild-request', 'flag-2', user=User('7', '张三'), guild=Guild('300', '测试群'))
    notice = account.protocol.sent[-1]
    assert notice[0] == 'private:42' and '群号：300' in notice[1] and '邀请人：张三' in notice[1], notice
    assert 'test-bot:flag-2' in handlers._pending

    # 群里说「同意」、别人私聊说「同意」、超级用户私聊说别的：都不算
    assert await say('同意', user='42') == []
    assert await say('同意', user='8', private=True) == []
    assert await say('你好', user='42', private=True) == []
    assert calls('guild.approve') == [] and 'test-bot:flag-2' in handlers._pending

    assert await say('同意', user='42', private=True) == ['已同意加入 测试群。']
    assert calls('guild.approve') == [{'message_id': 'flag-2', 'approve': True, 'comment': ''}], calls('guild.approve')
    assert handlers._pending == {}

    # 入群申请：拒绝走 guild.member.approve
    await request('guild-member-request', 'flag-3', user=User('8', '李四'), guild=Guild('300', '测试群'))
    assert await say('拒绝', user='42', private=True) == ['已拒绝加入 测试群。']
    assert calls('guild.member.approve') == [{'message_id': 'flag-3', 'approve': False, 'comment': ''}]
    assert handlers._pending == {}

    # 没有待审批时超级用户的「同意」不回应
    assert await say('同意', user='42', private=True) == []

    # Ark 群邀请卡片：只私聊通知超级用户，不在原处回复，也不产生待审批
    before = len(account.protocol.sent)
    assert await say(f'<onebot:json data={quoteattr(ARK)}/>', user='7') == ['收到群邀请卡片消息：\n群名：测试群\n群号：100\n邀请人：7\n\n'
        '该卡片不是 QQ 系统的入群请求，无法自动通过；请在 QQ 里使用“邀请 bot 加入群聊”的系统入口邀请。']
    assert account.protocol.sent[-1][0] == 'private:42' and len(account.protocol.sent) == before + 1
    assert handlers._pending == {}
    print('ALL-OK')

run(main())
'''


class RequestDispatchTests(unittest.TestCase):
    def test_requests_and_replies_through_the_real_event_path(self):
        result = run_scenarios(DISPATCH_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
