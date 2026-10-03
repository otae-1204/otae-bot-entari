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
        # 复现：Entari 0.17.4 的 gather 把 message_get 的异常原样抛出，事件到不了任何订阅者。
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
import asyncio, json, sys
from datetime import datetime, timezone

import httpx
sys.path.insert(0, sys.argv[1])

from otae_bot.application import create_app
app = create_app()

from arclet.entari import load_plugin
from arclet.entari.config import EntariConfig
from arclet.entari.event.base import MessageEvent
from arclet.entari.plugin.service import plugin_service
from arclet.letoderea.publisher import _publishers
from satori import Channel, ChannelType, Event, Guild, Login, MessageObject, Quote, Text, User
from satori.client.account import Account, ApiInfo
from satori.exception import NotFoundException, ServerException
from unittest.mock import AsyncMock, patch

load_plugin('.commands')
load_plugin('.record_message')
EntariConfig.instance.basic.prefix = ['/']
hyw = plugin_service.plugins['plugins.hyw'].module
import otae_bot.adapters.onebot as onebot

# create_app 装好的补丁对真实 publisher 生效
gather = MessageEvent.__dict__['gather']
users = sorted(k for k, p in _publishers.items() if p.supplier is gather)
assert users == ['message-created', 'message-deleted', 'message-updated', 'reaction-added', 'reaction-removed'], users

QUOTE_ID = '2|100|3969610'
FORWARD = {'status': 'ok', 'retcode': 0, 'data': {'messages': [
    {'sender': {'nickname': '甲', 'user_id': '1'}, 'time': 0,
     'content': [{'type': 'text', 'data': {'text': '转发里的第一句'}}]},
    {'sender': {'nickname': '乙', 'user_id': '2'}, 'time': 1,
     'content': [{'type': 'image', 'data': {'url': 'https://example.com/in-forward.png'}}]},
]}}

class Protocol:
    def __init__(self, account):
        self.account = account
        self.internal_calls = []

    async def message_get(self, channel_id, message_id):
        raise ServerException('消息为空')

    async def internal(self, action, method='POST', **params):
        self.internal_calls.append((action, params))
        raise NotFoundException('not found')

    async def send_message(self, channel, message, at_sender=None, reply_to=None, referrer=None):
        return []

# LLBot 8.2.1 的 Satori 透传 /v1/internal/onebot11/<action>
seen = []
llbot = {'in_group': True}

def answer(request):
    seen.append(request)
    body = json.loads(request.content)
    if request.url.path == '/v1/internal/onebot11/get_group_msg_history':
        if not llbot['in_group']:
            return httpx.Response(500, json={'message': "Cannot read properties of undefined (reading 'start')"})
        seq = body['message_seq']
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': {'messages': [
            {'message_id': 1, 'message_seq': seq - 1, 'message': [{'type': 'text', 'data': {'text': '前一条'}}]},
            {'message_id': 537779999, 'message_seq': seq, 'message': [{'type': 'forward', 'data': {'id': 'resid-1'}}]},
        ]}})
    if request.url.path == '/v1/internal/onebot11/get_forward_msg' and body == {'id': 'resid-1'}:
        return httpx.Response(200, json=FORWARD)
    return httpx.Response(404)

real_client = httpx.AsyncClient

def client(**kwargs):
    return real_client(transport=httpx.MockTransport(answer), timeout=kwargs.get('timeout'))

def incoming(n):
    account = Account(Login(user=User('test-bot'), platform='qq'), ApiInfo(port=5500, token='SECRET-TOKEN'), [], Protocol)
    origin = Event('message-created', datetime.now(timezone.utc), Login(user=User('test-bot'), platform='qq'),
                   channel=Channel('100', ChannelType.TEXT), guild=Guild('100'), user=User('otae'),
                   message=MessageObject.from_elements('incoming-' + str(n), [Quote(QUOTE_ID), Text('/q 总结一下')]))
    return account, origin

async def main():
    request = AsyncMock()
    with patch.object(hyw.HywConfig, 'from_env', return_value=hyw.HywConfig(api_key='test', timeout=5)), \
         patch.dict(hyw.handle_hyw.__globals__, run_request=request), \
         patch.object(onebot.httpx, 'AsyncClient', client), \
         patch.object(onebot, 'ashared_ssl_context', AsyncMock(return_value=None)):
        # 1) 合并转发：Satori message.get 500，事件照常分发；按 seq 找到消息，用 resid 展开
        account, origin = incoming(1)
        await app.handle_event(account, origin)
        request.assert_awaited_once()
        session, _, _, text, images, prior = request.await_args.args
        assert session.event.quote.id == QUOTE_ID, session.event.quote
        assert session.reply is None
        assert '总结一下' in text and '[引用消息]' in text, text
        assert '转发里的第一句' in text and '甲' in text and '前一条' not in text, text
        assert images == ['https://example.com/in-forward.png'], images
        assert request.await_args.kwargs['rich'] is True
        assert account.protocol.internal_calls == [], account.protocol.internal_calls
        assert [str(sent.url) for sent in seen] == [
            'http://localhost:5500/v1/internal/onebot11/get_group_msg_history',
            'http://localhost:5500/v1/internal/onebot11/get_forward_msg',
        ], seen
        assert json.loads(seen[0].content) == {'group_id': 100, 'message_seq': 3969610, 'count': 10}, seen[0].content
        for sent in seen:
            assert sent.headers['authorization'] == 'Bearer SECRET-TOKEN', sent.headers
            assert sent.headers['satori-user-id'] == 'test-bot', sent.headers
            assert sent.headers['satori-platform'] == 'qq', sent.headers
            assert 'x-self-id' not in sent.headers, sent.headers
        print('FORWARD-OK')

        # 2) 收到事件的号不在群：history 报错，/q 照常执行，只是没有引用上下文
        request.reset_mock()
        seen.clear()
        llbot['in_group'] = False
        account, origin = incoming(2)
        await app.handle_event(account, origin)
        request.assert_awaited_once()
        text = request.await_args.args[3]
        assert text == '总结一下', text
        assert len(seen) == 1, seen
        print('UNAVAILABLE-OK')

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
            env.update(PYTHONIOENCODING="utf-8", ONEBOT_HTTP_URL="", SATORI_CLIENTS="[]")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", DISPATCH_SCRIPT, str(ROOT)], cwd=root,
                capture_output=True, text=True, timeout=120, env=env,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("FORWARD-OK", result.stdout)
        self.assertIn("UNAVAILABLE-OK", result.stdout)
        # record_message 记下了这条消息：事件确实分发了
        self.assertIn("(otae) -> ", result.stdout)
        self.assertIn("quoted message fetch failed", output)
        self.assertIn("quoted message unavailable", output)
        self.assertIn("可能不在群 100", output)
        self.assertNotIn("SECRET-TOKEN", output)


if __name__ == "__main__":
    unittest.main()
