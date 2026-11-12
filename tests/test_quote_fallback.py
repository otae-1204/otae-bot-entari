"""引用的消息取不到（LLBot message.get 500 消息为空）时，事件照常分发。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arclet.entari import MessageCreatedEvent
from arclet.entari.const import ITEM_ACCOUNT, ITEM_MESSAGE_CONTENT, ITEM_MESSAGE_REPLY
from arclet.entari.event.base import MessageEvent
from arclet.entari.message import Reply
from satori import Channel, ChannelType, Event, Guild, Login, MessageObject, Quote, Text, User
from satori.client.account import Account, ApiInfo
from satori.exception import ServerException

from otae_bot.adapters import quote_fallback
from otae_bot.plugin_registry import discover_plugins

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SATORI-SECRET-TOKEN"


def _original_gather():
    gather = MessageEvent.__dict__["gather"]
    return gather.__wrapped__ if getattr(gather, quote_fallback._MARK, False) else gather


class FakeProtocol:
    def __init__(self, account):
        self.account = account
        self.calls = []
        self.result: object = ServerException("消息为空")

    async def message_get(self, channel_id, message_id):
        self.calls.append((channel_id, message_id))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _account() -> Account:
    login = Login(user=User("test-bot"), platform="qq")
    return Account(login, ApiInfo(port=5500, token=TOKEN), [], FakeProtocol)


def _event(elements, account=None) -> MessageCreatedEvent:
    origin = Event(
        "message-created", datetime.now(timezone.utc), Login(user=User("test-bot"), platform="qq"),
        channel=Channel("100", ChannelType.TEXT), guild=Guild("100"), user=User("sender"),
        message=MessageObject.from_elements("incoming-1", elements),
    )
    return MessageCreatedEvent(account or _account(), origin)


class GatherFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gather = quote_fallback._wrap(_original_gather(), ITEM_ACCOUNT, ITEM_MESSAGE_REPLY)
        patcher = mock.patch.object(quote_fallback, "logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_unpatched_gather_drops_the_event(self):
        # 复现：Entari 0.19.4 的 gather（0.17.4 相同） 把 message_get 的异常原样抛出，事件到不了任何订阅者。
        event = _event([Quote("fwd-msg-1"), Text("/q 总结一下")])
        with self.assertRaises(ServerException):
            await _original_gather()(event, {})

    async def test_failed_fetch_dispatches_without_reply_and_keeps_the_quote(self):
        account = _account()
        quote = Quote("fwd-msg-1")
        event = _event([quote, Text("/q 总结一下")], account)
        context = {}
        await self.gather(event, context)
        self.assertNotIn(ITEM_MESSAGE_REPLY, context)
        self.assertIs(context["is_reply_me"], False)
        self.assertEqual(str(context[ITEM_MESSAGE_CONTENT]), "/q 总结一下")
        self.assertIs(context[ITEM_ACCOUNT], account)
        self.assertIs(event.account, account)
        # 原始 quote 元素（含 id）留在事件上交给插件
        self.assertIsInstance(event.quote, Quote)
        self.assertEqual(event.quote.id, "fwd-msg-1")
        self.assertEqual(account.protocol.calls, [("100", "fwd-msg-1")])

    async def test_failure_is_logged_with_channel_quote_and_error_but_no_token(self):
        account = _account()
        account.protocol.result = ServerException(f"消息为空 Authorization: Bearer {TOKEN}")
        await self.gather(_event([Quote("fwd-msg-1"), Text("/q")], account), {})
        self.log.warning.assert_called_once()
        template, *args = self.log.warning.call_args.args
        self.assertIn("quoted message fetch failed", template)
        self.assertEqual(args[:3], ["100", "fwd-msg-1", "ServerException"])
        self.assertIn("消息为空", args[3])
        self.assertNotIn(TOKEN, " ".join(map(str, args)))

    async def test_any_exception_from_message_get_is_tolerated(self):
        for error in (RuntimeError("boom"), asyncio.TimeoutError(), KeyError("id")):
            account = _account()
            account.protocol.result = error
            context = {}
            await self.gather(_event([Quote("q-1"), Text("/q 问")], account), context)
            self.assertNotIn(ITEM_MESSAGE_REPLY, context)
            self.assertEqual(str(context[ITEM_MESSAGE_CONTENT]), "/q 问")

    async def test_successful_fetch_keeps_entari_behaviour(self):
        account = _account()
        quoted = MessageObject.from_elements("q-1", [Text("原文")], user=User("test-bot"))
        account.protocol.result = quoted
        event = _event([Quote("q-1"), Text(" /q 问")], account)
        context = {}
        await self.gather(event, context)
        reply = context[ITEM_MESSAGE_REPLY]
        self.assertIsInstance(reply, Reply)
        self.assertIs(reply.origin, quoted)
        self.assertIs(reply.quote, event.quote)
        self.assertIs(context["is_reply_me"], True)
        # is_reply_me 时 Entari 去掉开头空白，这一步仍由原 gather 完成
        self.assertEqual(str(context[ITEM_MESSAGE_CONTENT]), "/q 问")
        self.assertIs(context[ITEM_ACCOUNT], account)
        self.assertEqual(account.protocol.calls, [("100", "q-1")])
        self.log.warning.assert_not_called()

    async def test_inline_quote_and_plain_messages_do_not_fetch(self):
        account = _account()
        context = {}
        await self.gather(_event([Quote("q-1", content=[Text("内联")]), Text("/q")], account), context)
        self.assertEqual(context[ITEM_MESSAGE_REPLY].origin.content, "内联")
        await self.gather(_event([Text("/q 没有引用")], account), context)
        self.assertEqual(account.protocol.calls, [])

    async def test_cancellation_is_not_swallowed(self):
        account = _account()
        account.protocol.result = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.gather(_event([Quote("q-1"), Text("/q")], account), {})
        self.log.warning.assert_not_called()


class InstallTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(quote_fallback, "logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_entari_gather_has_the_expected_structure(self):
        self.assertIsNone(quote_fallback._unexpected_structure(_original_gather()))

    def test_install_patches_the_class_and_repoints_publishers_once(self):
        original = _original_gather()
        saved = MessageEvent.__dict__["gather"]
        self.addCleanup(setattr, MessageEvent, "gather", saved)
        other = SimpleNamespace(supplier=object())
        publishers = {"message-created": SimpleNamespace(supplier=original), "other": other}

        MessageEvent.gather = original
        self.assertTrue(quote_fallback.install_quote_fetch_fallback(MessageEvent, publishers))
        wrapper = MessageEvent.__dict__["gather"]
        self.assertTrue(getattr(wrapper, quote_fallback._MARK))
        self.assertIs(wrapper.__wrapped__, original)
        self.assertIs(publishers["message-created"].supplier, wrapper)
        self.assertIsNot(other.supplier, wrapper)
        # 再装一次：不套第二层，后来出现的旧引用也会改指过来
        publishers["late"] = SimpleNamespace(supplier=original)
        self.assertTrue(quote_fallback.install_quote_fetch_fallback(MessageEvent, publishers))
        self.assertIs(MessageEvent.__dict__["gather"], wrapper)
        self.assertIs(publishers["late"].supplier, wrapper)
        self.log.info.assert_called_once()
        self.log.warning.assert_not_called()

    def test_unexpected_structure_is_left_alone_with_a_warning(self):
        async def moved(self, context):
            return await self.account.fetch_reply(context)

        async def renamed(self, ctx, extra=None):
            return self.account.protocol.message_get(self.quote.children, Reply, ITEM_MESSAGE_REPLY)

        def sync(self, context):
            return None

        for gather in (moved, renamed, sync, None):
            self.log.reset_mock()
            namespace = {} if gather is None else {"gather": gather}
            event_cls = type("FutureMessageEvent", (), namespace)
            publishers = {"message-created": SimpleNamespace(supplier=gather)}
            self.assertFalse(quote_fallback.install_quote_fetch_fallback(event_cls, publishers))
            self.assertIs(event_cls.__dict__.get("gather"), gather)
            self.assertIs(publishers["message-created"].supplier, gather)
            self.log.warning.assert_called_once()
            self.assertIn("not installed", self.log.warning.call_args.args[0])

    def test_missing_publisher_registry_is_skipped(self):
        event_cls = type("E", (), {"gather": _original_gather()})
        self.assertFalse(quote_fallback.install_quote_fetch_fallback(event_cls, object()))
        self.assertIs(event_cls.__dict__["gather"], _original_gather())
        self.log.warning.assert_called_once()


DISPATCH_SCRIPT = r'''
import asyncio, sys
from datetime import datetime, timezone

import httpx
sys.path.insert(0, sys.argv[1])

from otae_bot.application import create_app
app = create_app()

from arclet.entari.config import EntariConfig
from arclet.entari.event.base import MessageEvent
from arclet.entari.plugin.service import plugin_service
from arclet.letoderea.publisher import _publishers
from satori import Channel, ChannelType, Event, Guild, Login, MessageObject, User
from satori.client.account import Account, ApiInfo
from satori.exception import NotFoundException, ServerException
from unittest.mock import AsyncMock, patch

# Entari 0.19 的内置插件是 niche，主体到应用启动的 blocking 阶段才执行；这里不启动应用，手动执行。
for builtin in ('$.commands', '$.record_message'):
    plugin_service.plugins[builtin].exec_apply()
EntariConfig.instance.basic.prefix = ['/']
hyw = plugin_service.plugins['plugins.hyw'].module

# create_app 装好的补丁对真实 publisher 生效
gather = MessageEvent.__dict__['gather']
users = sorted(k for k, p in _publishers.items() if p.supplier is gather)
assert users == ['message-created', 'message-deleted', 'message-updated', 'reaction-added', 'reaction-removed'], users

QUOTE_ID = '2|100|3969610'
# 边界适配器送来的引用：作者 + 原消息内容；被引用的是合并转发时，转发的各条消息就在其中
FORWARD_QUOTE = (
    '<quote id="' + QUOTE_ID + '"><author id="9" name="转发人"/>'
    '<message id="resid-1" forward="true">'
    '<message><author id="1" name="甲"/>转发里的第一句</message>'
    '<message><author id="2" name="乙"/><img src="https://example.com/in-forward.png"/></message>'
    '</message></quote>'
)
UNEXPANDED_QUOTE = '<quote id="' + QUOTE_ID + '"><author id="9" name="转发人"/><message id="resid-1" forward="true"/></quote>'
BARE_QUOTE = '<quote id="' + QUOTE_ID + '"/>'


class Protocol:
    def __init__(self, account):
        self.account = account
        self.gets = []
        self.internal_calls = []

    async def message_get(self, channel_id, message_id):
        self.gets.append(message_id)
        raise ServerException('消息为空')

    async def internal(self, action, method='POST', **params):
        self.internal_calls.append((action, params))
        raise NotFoundException('not found')

    async def send_message(self, channel, message, at_sender=None, reply_to=None, referrer=None):
        return []


seen = []
real_client = httpx.AsyncClient

def client(*args, **kwargs):
    def answer(request):
        seen.append(request)
        return httpx.Response(404)
    return real_client(transport=httpx.MockTransport(answer))

def incoming(n, quote):
    account = Account(Login(user=User('test-bot'), platform='qq'), ApiInfo(port=5500, token='SECRET-TOKEN'), [], Protocol)
    origin = Event('message-created', datetime.now(timezone.utc), Login(user=User('test-bot'), platform='qq'),
                   channel=Channel('100', ChannelType.TEXT), guild=Guild('100'), user=User('otae'),
                   message=MessageObject('incoming-' + str(n), quote + '/q 总结一下'))
    return account, origin

async def main():
    request = AsyncMock()
    with patch.object(hyw.HywConfig, 'from_env', return_value=hyw.HywConfig(api_key='test', timeout=5)), \
         patch.dict(hyw.handle_hyw.__globals__, run_request=request), \
         patch.object(httpx, 'AsyncClient', client):
        # 1) 引用合并转发：内容随事件送达，不查询 message.get，也不发任何 HTTP
        account, origin = incoming(1, FORWARD_QUOTE)
        await app.handle_event(account, origin)
        request.assert_awaited_once()
        session, _, _, text, images, prior = request.await_args.args
        assert session.event.quote.id == QUOTE_ID, session.event.quote
        assert session.reply is not None
        assert text.startswith('总结一下\n\n[引用消息]\n'), text
        assert '转发里的第一句' in text and '甲（1）' in text and '乙（2）' in text, text
        assert images == ['https://example.com/in-forward.png'], images
        assert request.await_args.kwargs['rich'] is True
        assert account.protocol.gets == [], account.protocol.gets
        assert account.protocol.internal_calls == [], account.protocol.internal_calls
        print('FORWARD-OK')

        # 2) 合并转发没有展开：/q 照常执行，只是没有引用上下文
        request.reset_mock()
        account, origin = incoming(2, UNEXPANDED_QUOTE)
        await app.handle_event(account, origin)
        request.assert_awaited_once()
        assert request.await_args.args[3] == '总结一下', request.await_args.args[3]
        assert account.protocol.gets == [] and account.protocol.internal_calls == []
        print('UNEXPANDED-OK')

        # 3) 空引用：Entari 的 message.get 失败，事件照常分发；/q 照常执行，没有引用上下文
        request.reset_mock()
        account, origin = incoming(3, BARE_QUOTE)
        await app.handle_event(account, origin)
        request.assert_awaited_once()
        session = request.await_args.args[0]
        assert session.reply is None and session.event.quote.id == QUOTE_ID
        assert request.await_args.args[3] == '总结一下', request.await_args.args[3]
        assert account.protocol.gets == [QUOTE_ID], account.protocol.gets
        assert account.protocol.internal_calls == [], account.protocol.internal_calls
        print('UNAVAILABLE-OK')
    assert seen == [], [str(sent.url) for sent in seen]

asyncio.get_event_loop().run_until_complete(main())
'''


class DispatchEndToEndTests(unittest.TestCase):
    def test_quoted_forward_reaches_hyw_through_the_real_event_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "assets", root / "assets")
            for name in discover_plugins(ROOT / "plugins"):
                package = root / name.replace(".", "/")
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
            env = dict(os.environ)
            env.update(PYTHONIOENCODING="utf-8", SATORI_CLIENTS="[]", ONEBOT_CLIENTS="[]")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", DISPATCH_SCRIPT, str(ROOT)], cwd=root,
                capture_output=True, text=True, timeout=120, env=env,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("FORWARD-OK", result.stdout)
        self.assertIn("UNEXPANDED-OK", result.stdout)
        self.assertIn("UNAVAILABLE-OK", result.stdout)
        # record_message 记下了这条消息：事件确实分发了
        self.assertIn("(otae) -> ", result.stdout)
        self.assertIn("quoted message fetch failed", output)
        self.assertIn("quoted message unavailable", output)
        self.assertIn("引用的合并转发没有随事件展开", output)
        self.assertIn("引用的消息没有随事件送达", output)
        self.assertNotIn("SECRET-TOKEN", output)


if __name__ == "__main__":
    unittest.main()
