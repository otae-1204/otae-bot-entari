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

from arclet.entari import MessageChain, MessageCreatedEvent, Session
from arclet.entari.const import ITEM_MESSAGE_REPLY, ITEM_SESSION
from arclet.entari.event.api import APIRequest
from arclet.entari.event.base import MessageEvent
from arclet.entari.message import Reply
from arclet.entari.session import EntariProtocol
from arclet.letoderea import Scope, add_shared_supplier, on, publish
from arclet.letoderea.utils import _EventSystem
from satori import Channel, ChannelType, Event, Guild, Login, MessageObject, Quote, Text, User
from satori.client.account import Account, ApiInfo
from satori.exception import ServerException

from otae_bot.pipeline import quote_fallback
from otae_bot.plugin_registry import discover_plugins

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SATORI-SECRET-TOKEN"


class FakeProtocol(EntariProtocol):
    """EntariProtocol whose HTTP call is replaced; message_get etc. stay Entari-wrapped."""

    def __init__(self, account):
        super().__init__(account)
        self.calls = []
        self.result: object = ServerException("消息为空")

    async def call_api(self, action, params=None, multipart=False, method="POST"):
        self.calls.append((str(getattr(action, "value", action)), dict(params or {})))
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


class QuoteFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Both hooks registered in a test scope; events go through the real dispatch."""

    async def asyncSetUp(self):
        # Entari pins letoderea's tasks to its own loop at import; this test runs on another.
        loop_patch = mock.patch.object(_EventSystem, "loop", asyncio.get_running_loop())
        loop_patch.start()
        self.addCleanup(loop_patch.stop)
        self.scope = Scope.of("test-quote-fallback")
        self.addAsyncCleanup(self.scope.cleanup)
        self.seen = []

        async def plugin(session: Session, event: MessageCreatedEvent, reply: Reply | None = None,
                         is_reply_me: bool = False, content: MessageChain | None = None):
            self.seen.append(SimpleNamespace(session=session, event=event, reply=reply,
                                             is_reply_me=is_reply_me, content=str(content)))

        with self.scope.context():
            on(APIRequest, quote_fallback.guard_message_get, priority=0)
            add_shared_supplier(quote_fallback.drop_unavailable_reply)
            on(MessageCreatedEvent, plugin)
        patcher = mock.patch.object(quote_fallback, "logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        self.accounts = []

    def account(self, result=None):
        account = _account()
        if result is not None:
            account.protocol.result = result
        self.accounts.append(account)
        self.addAsyncCleanup(account.protocol.session.close)
        return account

    async def dispatch(self, event):
        await publish(event, scope=self.scope)
        self.assertEqual(len(self.seen), 1, "the event must reach the plugin")
        return self.seen.pop()

    async def test_without_the_hook_entari_loses_the_event(self):
        # 复现：Entari 0.19.4 的 gather 把 message_get 的异常原样抛出，事件到不了任何订阅者。
        await self.scope.cleanup()
        account = self.account()
        with self.assertRaises(ServerException):
            await account.protocol.message_get("100", "fwd-msg-1")

    async def test_failed_fetch_dispatches_without_reply_and_keeps_the_quote(self):
        account = self.account()
        event = _event([Quote("fwd-msg-1"), Text("/q 总结一下")], account)
        got = await self.dispatch(event)
        self.assertIsNone(got.reply)
        self.assertIsNone(got.session.reply)
        self.assertIs(got.is_reply_me, False)
        self.assertEqual(got.content, "/q 总结一下")
        # 原始 quote 元素（含 id）留在事件上交给插件
        self.assertIsInstance(got.event.quote, Quote)
        self.assertEqual(got.event.quote.id, "fwd-msg-1")
        # 只请求一次：钩子自己调用，没有再触发 APIRequest，也没有回落到原方法
        self.assertEqual(account.protocol.calls, [("message.get", {"channel_id": "100", "message_id": "fwd-msg-1"})])

    async def test_failure_is_logged_with_channel_quote_and_error_but_no_token(self):
        account = self.account(ServerException(f"消息为空 Authorization: Bearer {TOKEN}"))
        await self.dispatch(_event([Quote("fwd-msg-1"), Text("/q")], account))
        self.log.warning.assert_called_once()
        template, *args = self.log.warning.call_args.args
        self.assertIn("quoted message fetch failed", template)
        self.assertEqual(args[:3], ["100", "fwd-msg-1", "ServerException"])
        self.assertIn("消息为空", args[3])
        self.assertNotIn(TOKEN, " ".join(map(str, args)))

    async def test_any_exception_from_message_get_is_tolerated(self):
        for error in (RuntimeError("boom"), asyncio.TimeoutError(), KeyError("id")):
            got = await self.dispatch(_event([Quote("q-1"), Text("/q 问")], self.account(error)))
            self.assertIsNone(got.reply)
            self.assertEqual(got.content, "/q 问")

    async def test_successful_fetch_keeps_entari_behaviour(self):
        account = self.account({"id": "q-1", "content": "原文", "user": {"id": "test-bot"}})
        event = _event([Quote("q-1"), Text(" /q 问")], account)
        got = await self.dispatch(event)
        self.assertIsInstance(got.reply, Reply)
        self.assertIs(got.session.reply, got.reply)
        self.assertNotIsInstance(got.reply.origin, quote_fallback.UnavailableMessage)
        self.assertEqual(got.reply.origin.content, "原文")
        self.assertIs(got.reply.quote, event.quote)
        self.assertIs(got.is_reply_me, True)
        # is_reply_me 时 Entari 去掉开头空白，这一步仍由原 gather 完成
        self.assertEqual(got.content, "/q 问")
        self.assertEqual(len(account.protocol.calls), 1)
        self.log.warning.assert_not_called()

    async def test_inline_quote_and_plain_messages_do_not_fetch(self):
        account = self.account()
        got = await self.dispatch(_event([Quote("q-1", content=[Text("内联")]), Text("/q")], account))
        self.assertEqual(got.reply.origin.content, "内联")
        got = await self.dispatch(_event([Text("/q 没有引用")], account))
        self.assertIsNone(got.reply)
        self.assertEqual(account.protocol.calls, [])

    async def test_other_api_calls_are_left_to_entari(self):
        account = self.account(ServerException("boom"))
        with self.assertRaises(ServerException):
            await account.protocol.message_delete("100", "m-1")
        self.assertEqual([action for action, _ in account.protocol.calls], ["message.delete"])
        self.log.warning.assert_not_called()

    async def test_cancellation_is_not_swallowed(self):
        account = self.account(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await account.protocol.message_get("100", "q-1")
        self.log.warning.assert_not_called()

    async def test_only_placeholder_replies_are_dropped(self):
        reply = Reply(Quote("q-1"), MessageObject.from_elements("q-1", [Text("原文")]))
        session = SimpleNamespace(reply=reply)
        context = {ITEM_MESSAGE_REPLY: reply, ITEM_SESSION: session}
        await quote_fallback.drop_unavailable_reply(context)
        self.assertIs(context[ITEM_MESSAGE_REPLY], reply)
        self.assertIs(session.reply, reply)
        await quote_fallback.drop_unavailable_reply({})


class InstallTests(unittest.TestCase):
    def test_entari_classes_are_not_patched(self):
        # 不再替换 MessageEvent.gather 或 letoderea 的 publisher supplier
        self.assertEqual(MessageEvent.__dict__["gather"].__module__, "arclet.entari.event.base")
        self.assertFalse(hasattr(MessageEvent.__dict__["gather"], "__wrapped__"))


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
from arclet.entari.session import EntariProtocol
from satori import Api, Channel, ChannelType, Event, Guild, Login, MessageObject, User
from satori.client.account import Account, ApiInfo
from satori.exception import NotFoundException, ServerException
from unittest.mock import AsyncMock, patch

# Entari 0.19 的内置插件是 niche，主体到应用启动的 blocking 阶段才执行；这里不启动应用，手动执行。
for builtin in ('$.commands', '$.record_message'):
    plugin_service.plugins[builtin].exec_apply()
EntariConfig.instance.basic.prefix = ['/']
hyw = plugin_service.plugins['plugins.hyw'].module

# 不再给 Entari 打补丁：gather 还是 Entari 自己的，保护靠 create_app 注册的公开钩子
assert MessageEvent.__dict__['gather'].__module__ == 'arclet.entari.event.base'
assert not hasattr(MessageEvent.__dict__['gather'], '__wrapped__')

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


class Protocol(EntariProtocol):
    """Real EntariProtocol (APIRequest hooks included); only the HTTP call is faked."""

    def __init__(self, account):
        super().__init__(account)
        self.gets = []
        self.internal_calls = []

    async def call_api(self, action, params=None, multipart=False, method='POST'):
        if action == Api.MESSAGE_GET:
            self.gets.append(params['message_id'])
            raise ServerException('消息为空')
        raise AssertionError(f'unexpected API call {action}')

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
    with patch.object(hyw.HywConfig, 'from_settings', return_value=hyw.HywConfig(api_key='test', timeout=5)), \
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
            env.update(PYTHONIOENCODING="utf-8")
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
