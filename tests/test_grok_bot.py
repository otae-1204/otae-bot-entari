from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from arclet.entari import MessageChain
from satori import ChannelType, Image, MessageObject, Text

from otae_bot.group_features import GroupFeatureStore
from plugins.grok_bot import gateway, handlers
from plugins.grok_bot.config import GrokConfig, GrokError
from plugins.grok_bot.conversations import ConversationScope
from plugins.grok_bot.media import Reply

AGENT = "00000000-0000-4000-8000-000000000001"
CONFIG = GrokConfig(base_url="http://grok.test:1340", token="private-token", agent_id=AGENT, poll_interval=0)


class Host:
    """Contract fixture: asynchronous acceptance, busy tasks, transcript paging."""

    def __init__(self):
        self.calls = []
        self.prompt = ""
        self.nonce = ""
        self.polls = 0
        self.override = {}
        self.answer = "本次问题的完整回答。"

    def agent(self):
        return {"id": AGENT, "name": "QQBOT", "isGroup": False, "isRunning": bool(self.prompt) and self.polls < 2,
                "isComposingMessage": False, "awaitingUserResponse": None, "lastMessagePreview": "陈旧的截断预览"}

    def response(self, request):
        name = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((name, body, request))
        if name in self.override:
            value = self.override[name](body)
            return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
        if name == "health":
            value = {"ok": True}
        elif name == "listAgents":
            value = [self.agent()]
        elif name == "getAsyncTasks":
            value = [{"id": "background"}] if self.prompt and self.polls < 3 else []
        elif name == "getSubagents":
            value = [{"status": "running" if self.prompt and self.polls < 4 else "completed"}]
        elif name == "sendPrompt":
            self.prompt, self.nonce = body["prompt"], body["clientNonce"]
            value = {"accepted": True}
        elif name == "promptAcceptanceStatus":
            self.polls += 1
            value = {"outcome": "found", "record": {"agentId": AGENT, "clientNonce": self.nonce,
                     "status": "pending" if self.polls < 2 else "accepted"}}
        elif name == "getAgentTranscriptTail":
            value = {"entries": [
                {"kind": "message", "role": "assistant", "content": "历史答案"},
                {"kind": "message", "role": "user", "content": self.prompt},
                {"kind": "tool-call", "content": "内部工具信息"},
                {"kind": "message", "role": "assistant", "content": "中间过程"},
                {"kind": "send-message", "message": {"type": "text", "content": self.answer}},
                {"kind": "message", "role": "assistant", "content": "流式预览", "streaming": True},
            ]}
        else:
            raise AssertionError(name)
        return httpx.Response(200, json=value)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.response), trust_env=False)


class ConfigTests(unittest.TestCase):
    def load(self, **extra):
        values = {"GROKBOT_GATEWAY_URL": CONFIG.base_url, "GROKBOT_GATEWAY_TOKEN": CONFIG.token,
                  "GROKBOT_AGENT_ID": AGENT, **extra}
        with patch("plugins.grok_bot.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            return GrokConfig.from_env()

    def test_private_gateway_config_and_redacted_repr(self):
        config = self.load(GROKBOT_GATEWAY_URL=" http://grok.tailnet.ts.net:1340/ ")
        self.assertEqual(config.base_url, "http://grok.tailnet.ts.net:1340")
        self.assertEqual(config.agent_id, AGENT)
        self.assertNotIn(CONFIG.token, repr(config))

    def test_config_rejects_wrong_endpoint_credentials_and_bounds(self):
        for key, values in {
            "GROKBOT_GATEWAY_URL": ["", "0.0.0.0:1340", "http://0.0.0.0:1340", "http://host/v1", "https://user:secret@host",
                                    "http://host?token=secret", "http://host:70000", "ftp://host", "http://[invalid"],
            "GROKBOT_GATEWAY_TOKEN": ["", "Bearer secret", "secret\nvalue", "中文"],
            "GROKBOT_AGENT_ID": ["QQBOT", "not-a-uuid"],
            "GROKBOT_TIMEOUT": [0, 1801, "bad", "NaN"],
            "GROKBOT_MAX_PENDING": [0, 33, "bad", True, 1.5],
            "GROKBOT_MAX_CONCURRENT": [0, 17, "bad", True, 1.5],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(GrokError) as caught:
                    self.load(**{key: value})
                self.assertNotIn("secret", str(caught.exception))

    def test_gateway_does_not_inherit_any_proxy(self):
        with patch("plugins.grok_bot.gateway.httpx.AsyncClient") as client:
            gateway.make_client()
        self.assertIs(client.call_args.kwargs["trust_env"], False)
        self.assertIs(client.call_args.kwargs["follow_redirects"], False)

    def test_reference_agent_is_optional_and_default_persona_is_loadable(self):
        config = self.load(GROKBOT_AGENT_ID="")
        self.assertEqual(config.agent_id, "")
        self.assertEqual(config.max_concurrent, 4)
        self.assertIn("花园多惠", config.persona())


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_outgoing_text_is_published_before_background_tasks_finish(self):
        host = Host()
        published = []

        async def publish(reply):
            published.append((host.polls, reply))

        async with host.client() as client:
            result = await gateway.Gateway(CONFIG, client).ask("问题", on_reply=publish)
        self.assertEqual(published[0][0], 2)  # Accepted, but async/subagent tasks still run.
        self.assertEqual(published[0][1].text, host.answer)
        self.assertGreaterEqual(host.polls, 5)  # Conversation remains occupied until idle.
        self.assertEqual(result.text, host.answer)
        self.assertTrue(all(reply.text == host.answer for _, reply in published))

    async def test_early_publish_excludes_streaming_and_assistant_fallback(self):
        host = Host()
        host.override["getAgentTranscriptTail"] = lambda _: {"entries": [
            {"role": "user", "content": host.prompt},
            {"role": "assistant", "content": "内部中间记录"},
            {"kind": "send-message", "streaming": True, "message": {"type": "text", "content": "未完成"}},
        ]}
        publish = AsyncMock()
        async with host.client() as client:
            reply = await gateway.Gateway(CONFIG, client).ask("问题", on_reply=publish)
        publish.assert_not_awaited()
        self.assertGreaterEqual(host.polls, 5)
        self.assertEqual(reply.text, "内部中间记录")  # Legacy fallback only after stable idle.

    async def test_early_publish_rejects_intervening_input_and_missing_anchor(self):
        for intervening in (False, True):
            host = Host()
            host.override["getAgentTranscriptTail"] = lambda _, fixture=host, other=intervening: {"entries": [
                {"role": "user", "content": fixture.prompt if other else "old question"},
                *([{"role": "user", "content": "another question"}] if other else []),
                {"kind": "send-message", "message": {"type": "text", "content": "不可转发"}},
            ]}
            publish = AsyncMock()
            async with host.client() as client:
                with self.subTest(intervening=intervening), self.assertRaises(GrokError if intervening else asyncio.TimeoutError):
                    await asyncio.wait_for(gateway.Gateway(CONFIG, client).ask("问题", on_reply=publish), .05)
            publish.assert_not_awaited()

    async def test_complete_request_waits_for_acceptance_tasks_and_full_answer(self):
        host = Host()
        async with host.client() as client:
            result = await gateway.Gateway(CONFIG, client).ask("问题")
        self.assertEqual(result.text, host.answer)
        self.assertGreaterEqual(host.polls, 5)
        self.assertEqual(sum(name == "sendPrompt" for name, _, _ in host.calls), 1)
        for name, body, request in host.calls:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["Authorization"], "Bearer " + CONFIG.token)
            if name == "promptAcceptanceStatus":
                self.assertEqual(body, {"accountSlot": "host", "clientNonce": host.nonce})
        self.assertIn("问题", host.prompt)

    async def test_stale_reply_does_not_complete_even_when_bot_is_idle(self):
        host = Host()
        host.override["getAgentTranscriptTail"] = lambda _: {"entries": [{"role": "assistant", "content": "历史答案"}]}
        async with host.client() as client:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(gateway.Gateway(CONFIG, client).ask("问题"), .03)

    async def test_paginated_transcript_anchors_reply_to_request(self):
        host = Host()

        def tail(body):
            if body.get("beforeSeq") == 20:
                return {"entries": [{"entry": {"role": "user", "content": host.prompt}}]}
            return {"entries": [{"kind": "send-message", "message": {"type": "text", "content": "答案"}}], "nextBeforeSeq": 20}

        host.override["getAgentTranscriptTail"] = tail
        async with host.client() as client:
            self.assertEqual((await gateway.Gateway(CONFIG, client).ask("问题")).text, "答案")

    async def test_intervening_app_input_does_not_become_qq_reply(self):
        host = Host()
        host.override["getAgentTranscriptTail"] = lambda _: {"entries": [
            {"role": "user", "content": host.prompt}, {"role": "user", "content": "另一个用户的输入"},
            {"role": "assistant", "content": "另一个用户的答案"}]}
        async with host.client() as client:
            with self.assertRaisesRegex(GrokError, "另一条输入"):
                await gateway.Gateway(CONFIG, client).ask("问题")

    async def test_auth_errors_redirects_invalid_json_and_transport_never_expose_secrets(self):
        for status in (401, 403, 404, 429, 500, 302):
            host = Host()
            host.override["listAgents"] = lambda _, code=status: httpx.Response(code, text="private-token upstream-body", headers={"Location": "http://untrusted.test"})
            async with host.client() as client:
                with patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)), self.subTest(status=status), self.assertRaises(GrokError) as caught:
                    await gateway.Gateway(CONFIG, client).agent()
            self.assertNotIn("private-token", str(caught.exception))
            self.assertNotIn("upstream-body", str(caught.exception))
            self.assertEqual(len(host.calls), 3 if status in {429, 500} else 1)
        for payload in (httpx.Response(200, text="not json private-token"), httpx.Response(200, json={"error": "private-token"})):
            host.override["listAgents"] = lambda _, value=payload: value
            async with host.client() as client:
                with self.assertRaises(GrokError) as caught:
                    await gateway.Gateway(CONFIG, client).agent()
                self.assertNotIn("private-token", str(caught.exception))
        for kind in (httpx.ConnectError, httpx.ReadTimeout):
            client = SimpleNamespace(request=AsyncMock(side_effect=kind("private-token")))
            with self.assertRaises(GrokError) as caught:
                await gateway.Gateway(CONFIG, client).request("sendPrompt")
            self.assertNotIn("private-token", str(caught.exception))
            client.request.assert_awaited_once()

    async def test_rejection_and_mismatched_acceptance_do_not_read_answers(self):
        for record in ({"agentId": AGENT, "status": "rejected"}, {"agentId": "someone-else", "status": "accepted"}):
            host = Host()
            host.override["promptAcceptanceStatus"] = lambda _, value=record, fixture=host: {"outcome": "found", "record": {**value, "clientNonce": fixture.nonce}}
            async with host.client() as client:
                with self.assertRaises(GrokError):
                    await gateway.Gateway(CONFIG, client).ask("问题")
            self.assertFalse(any(name == "getAgentTranscriptTail" for name, _, _ in host.calls))

    async def test_unknown_durability_requires_current_prompt_and_reply(self):
        host = Host()

        def lookup(_):
            host.polls += 1
            return {"outcome": "unknown-durability"}

        host.override["promptAcceptanceStatus"] = lookup
        async with host.client() as client:
            self.assertEqual((await gateway.Gateway(CONFIG, client).ask("问题")).text, host.answer)

    async def test_waiting_for_user_missing_agent_and_unknown_state_are_not_idle(self):
        for row in (None, {**Host().agent(), "awaitingUserResponse": {"prompt": "private"}}, {"id": AGENT}):
            host = Host()
            host.override["listAgents"] = lambda _, value=row: [] if value is None else [value]
            async with host.client() as client:
                with self.assertRaises(GrokError):
                    await gateway.Gateway(CONFIG, client).ask("问题")
            self.assertFalse(any(name == "sendPrompt" for name, _, _ in host.calls))

    async def test_existing_cloud_work_finishes_before_new_send(self):
        host = Host()
        count = 0

        def roster(_):
            nonlocal count
            count += 1
            return [{**host.agent(), "isRunning": count < 3}]

        host.override["listAgents"] = roster
        async with host.client() as client:
            await gateway.Gateway(CONFIG, client).ask("问题")
        index = next(i for i, (name, _, _) in enumerate(host.calls) if name == "sendPrompt")
        self.assertEqual(sum(name == "listAgents" for name, _, _ in host.calls[:index]), 3)

    async def test_readonly_diagnostic_authenticates_without_sending_prompt(self):
        host = Host()
        with patch.object(gateway, "make_client", side_effect=host.client):
            self.assertIn("检查通过", await gateway.check(CONFIG))
        self.assertFalse(any(name == "sendPrompt" for name, _, _ in host.calls))
        health = host.calls[0][2]
        self.assertEqual(health.method, "GET")
        self.assertNotIn("Authorization", health.headers)

    async def test_diagnostic_without_reference_agent_only_reads_health_and_roster(self):
        host = Host()
        with patch.object(gateway, "make_client", side_effect=host.client):
            self.assertIn("未创建 Bot", await gateway.check(replace(CONFIG, agent_id="")))
        self.assertEqual([name for name, _, _ in host.calls], ["health", "listAgents"])

    async def test_timeout_is_bounded_and_does_not_interrupt_shared_bot(self):
        host = Host()
        host.override["listAgents"] = lambda _: [{**host.agent(), "isRunning": True}]
        with patch.object(gateway, "make_client", side_effect=host.client), self.assertRaisesRegex(GrokError, "仍在云端运行"):
            await gateway.ask(replace(CONFIG, timeout=.01), "问题")
        self.assertFalse(any(name in {"sendPrompt", "interruptAgentRun", "deleteAgent"} for name, _, _ in host.calls))

    async def test_transient_read_failures_recover_with_redacted_diagnostics(self):
        for failure in (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            client = SimpleNamespace(request=AsyncMock(side_effect=[
                failure("private-token upstream-body"), httpx.Response(200, json={"entries": []}),
            ]))
            with self.subTest(failure=failure), patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)), \
                 patch.object(gateway.logger, "warning") as warning:
                result = await gateway.Gateway(CONFIG, client).request("getAgentTranscriptTail", {"id": AGENT})
            self.assertEqual(result, {"entries": []})
            self.assertEqual(client.request.await_count, 2)
            self.assertIn(failure.__name__, str(warning.call_args))
            self.assertNotIn(CONFIG.token, str(warning.call_args))
            self.assertNotIn("upstream-body", str(warning.call_args))

    async def test_read_retries_are_bounded_and_do_not_retry_local_protocol_errors(self):
        for failure, attempts in ((httpx.RemoteProtocolError, 3), (httpx.ReadTimeout, 3), (httpx.LocalProtocolError, 1)):
            client = SimpleNamespace(request=AsyncMock(side_effect=failure("private-token")))
            with self.subTest(failure=failure), patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)), \
                 self.assertRaises(GrokError) as caught:
                await gateway.Gateway(CONFIG, client).request("getAgentTranscriptTail")
            self.assertEqual(client.request.await_count, attempts)
            self.assertIn(failure.__name__, str(caught.exception))
            self.assertNotIn(CONFIG.token, str(caught.exception))

    async def test_transient_server_failure_recovers_but_mutations_never_retry(self):
        for status in (408, 429, 500, 502, 503, 504):
            client = SimpleNamespace(request=AsyncMock(side_effect=[httpx.Response(status), httpx.Response(200, json=[])]))
            with self.subTest(status=status), patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)):
                self.assertEqual(await gateway.Gateway(CONFIG, client).request("getAsyncTasks"), [])
            self.assertEqual(client.request.await_count, 2)
        for command in ("sendPrompt", "createAgent", "updateAgent", "uploadAttachment", "interruptAgentRun"):
            for failure in (httpx.RemoteProtocolError("private-token"), httpx.ReadTimeout("private-token"), httpx.Response(503)):
                client = SimpleNamespace(request=AsyncMock(side_effect=[failure]))
                with self.subTest(command=command, failure=failure), self.assertRaises(GrokError):
                    await gateway.Gateway(CONFIG, client).request(command, {"prompt": "private content"})
                client.request.assert_awaited_once()

    async def test_transcript_read_timeout_is_separate_from_state_rpc_timeout(self):
        host = Host()
        async with httpx.AsyncClient(transport=httpx.MockTransport(host.response), timeout=httpx.Timeout(20, connect=10)) as client:
            api = gateway.Gateway(CONFIG, client)
            await api.request("getAgentTranscriptTail")
            await api.request("listAgents")
        self.assertEqual(host.calls[0][2].extensions["timeout"], {"connect": 10, "read": 60, "write": 20, "pool": 20})
        self.assertEqual(host.calls[1][2].extensions["timeout"]["read"], 20)

    async def test_disconnected_older_transcript_page_recovers_without_resubmitting_prompt(self):
        host = Host()
        older_reads = 0

        def tail(body):
            nonlocal older_reads
            if body.get("beforeSeq") == 20:
                older_reads += 1
                if older_reads == 1:
                    raise httpx.RemoteProtocolError("private-token")
                return {"entries": [{"entry": {"role": "user", "content": host.prompt}}]}
            return {"entries": [{"kind": "send-message", "message": {"type": "text", "content": "答案"}}], "nextBeforeSeq": 20}

        host.override["getAgentTranscriptTail"] = tail
        async with host.client() as client:
            with patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)):
                reply = await gateway.Gateway(CONFIG, client).ask("问题")
        self.assertEqual(reply.text, "答案")
        self.assertEqual(sum(name == "sendPrompt" for name, _, _ in host.calls), 1)
        pages = [body for name, body, _ in host.calls if name == "getAgentTranscriptTail"]
        self.assertEqual(pages[1], pages[2])
        self.assertEqual(pages[1]["beforeSeq"], 20)

    async def test_total_task_timeout_cancels_read_backoff_without_new_prompt(self):
        host = Host()

        def broken_tail(_):
            raise httpx.ReadError("private-token")

        host.override["getAgentTranscriptTail"] = broken_tail
        with patch.object(gateway, "make_client", side_effect=host.client), patch.object(gateway, "READ_RETRY_DELAYS", (1, 1)), \
             self.assertRaisesRegex(GrokError, "仍在云端运行"):
            await gateway.ask(replace(CONFIG, timeout=.05), "问题")
        self.assertEqual(sum(name == "getAgentTranscriptTail" for name, _, _ in host.calls), 1)
        self.assertEqual(sum(name == "sendPrompt" for name, _, _ in host.calls), 1)
        self.assertFalse(any(name == "interruptAgentRun" for name, _, _ in host.calls))

    async def test_permanent_state_error_joins_other_inflight_state_reads(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def respond(request):
            if request.url.path.endswith("/listAgents"):
                await started.wait()
                return httpx.Response(401)
            if request.url.path.endswith("/getSubagents"):
                return httpx.Response(200, json=[])
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(GrokError, "认证失败"):
                await asyncio.wait_for(gateway.Gateway(CONFIG, client).state(), 1)
            self.assertTrue(stopped.is_set())


SCOPE = ConversationScope("qq", "bot", "group", "100", "100")


def session(group="100", user="200", private=False):
    return SimpleNamespace(
        reply=None, send=AsyncMock(return_value=[]),
        account=SimpleNamespace(platform="qq", self_id="bot"),
        event=SimpleNamespace(user=SimpleNamespace(id=user),
                              guild=None if private else SimpleNamespace(id=group),
                              channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT)),
    )


def result(text):
    return SimpleNamespace(all_matched_args={"content": [Text(text)]})


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = GroupFeatureStore(Path(directory.name) / "switches.json")
        self.store.set_enabled(SCOPE.feature_scope, "grok_bot", True)
        self.enterContext(patch.object(handlers, "feature_store", self.store))

    async def test_handler_forwards_early_reply_and_does_not_repeat_at_completion(self):
        target = session()

        async def run(*args, on_reply, **kwargs):
            await on_reply(Reply("已经完成的正文"))
            self.assertEqual(str(target.send.await_args.args[0]), "已经完成的正文")
            return Reply()

        with patch.object(GrokConfig, "from_env", return_value=CONFIG), patch.object(handlers.queue, "run", side_effect=run):
            await handlers.handle_grok(target, result("问题"))
        target.send.assert_awaited_once()

    async def test_delivery_policy_is_checked_for_each_qq_text_chunk(self):
        target = session()
        interrupted = False

        async def send(*args, **kwargs):
            nonlocal interrupted
            interrupted = True

        target.send.side_effect = send
        await handlers.send_reply(target, Reply("x" * 2001), reply_to=lambda: not interrupted)
        self.assertEqual([call.kwargs["reply_to"] for call in target.send.await_args_list], [True, False])

    async def test_help_quotes_images_and_length_validation(self):
        for alias in ("grok", "grokbot"):
            self.assertTrue(handlers.grok_command.parse(MessageChain(alias + " 帮助")).matched)
        target = session()
        with patch.object(GrokConfig, "from_env", side_effect=AssertionError("help needs no config")):
            await handlers.handle_grok(target, result("帮助"))
        self.assertIn("独立", str(target.send.await_args.args[0]))
        target.reply = SimpleNamespace(origin=MessageObject("quoted", "引用正文"))
        with patch.object(GrokConfig, "from_env", return_value=CONFIG), patch.object(handlers.queue, "run", AsyncMock(return_value="回答")) as run:
            await handlers.handle_grok(target, result("解释一下"))
            self.assertIn("引用正文", run.await_args.args[1])
            await handlers.handle_grok(target, result("x" * 6001))
            self.assertIn("6000", str(target.send.await_args.args[0]))
            await handlers.handle_grok(target, SimpleNamespace(all_matched_args={"content": [Image(src="https://example.test/a.png")]}))
            self.assertEqual(run.await_args.kwargs["images"], ("https://example.test/a.png",))
            self.assertEqual(run.await_count, 2)

    async def test_error_logging_is_redacted_and_output_is_plain_text(self):
        target = session()
        with patch.object(GrokConfig, "from_env", return_value=CONFIG), patch.object(handlers.queue, "run", AsyncMock(side_effect=ValueError("private-token"))), patch.object(handlers.logger, "warning") as warning:
            await handlers.handle_grok(target, result("question"))
        self.assertNotIn("private-token", str(target.send.await_args))
        self.assertNotIn("private-token", str(warning.call_args))
        await handlers.send_text(target, '<at id="all"/>')
        self.assertEqual(target.send.await_args.args[0][0].text, '<at id="all"/>')
        self.assertNotIn('<at id="all"/>', str(target.send.await_args.args[0]))
