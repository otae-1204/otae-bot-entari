from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from satori import ChannelType

from otae_bot.config.settings import Config
from otae_bot.group_features import GroupFeatureStore
from plugins.grok_bot import conversations, handlers
from plugins.grok_bot.config import GatewayError, GrokConfig, GrokError
from plugins.grok_bot.conversations import (
    ConversationScope,
    SessionStore,
    repair_binding,
    resolve_agent,
    scope_from_session,
)
from plugins.grok_bot.gateway import Gateway
from plugins.grok_bot.media import Reply

SCOPE = ConversationScope("qq", "bot", "group", "100", "100")
REFERENCE = str(uuid4())


def session(group="100", user="200", bot="bot", private=False):
    return SimpleNamespace(
        account=SimpleNamespace(platform="qq", self_id=bot), reply=None, send=AsyncMock(),
        event=SimpleNamespace(
            channel=SimpleNamespace(id=group, type=ChannelType.DIRECT if private else ChannelType.TEXT),
            guild=None if private else SimpleNamespace(id=group), user=SimpleNamespace(id=user),
        ),
    )


def message(text):
    return SimpleNamespace(all_matched_args={"content": [text]})


class Host:
    def __init__(self):
        self.agents = [{"id": REFERENCE, "name": "QQBOT", "description": "旧会话", "isGroup": False}]
        self.calls = []
        self.lose_create_response = False

    def respond(self, request):
        command = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((command, body))
        if command == "listAgents":
            data = self.agents
        elif command == "createAgent":
            agent = {"id": str(uuid4()), "isGroup": False, "isRunning": False, "isComposingMessage": False,
                     **{key: body[key] for key in ("name", "description")}}
            self.agents.append(agent)
            if self.lose_create_response:
                raise httpx.ReadTimeout("secret upstream error", request=request)
            data = {"agent": agent}
        elif command == "updateAgent":
            agent = next(row for row in self.agents if row["id"] == body["id"])
            agent.update(body["profile"])
            data = agent
        else:
            raise AssertionError(command)
        return httpx.Response(200, json=data)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "sessions.json"
        self.persona = Path(directory.name) / "persona.md"
        self.persona.write_text("多惠的人设", encoding="utf-8-sig")
        self.config = GrokConfig(base_url="http://grok.test:1340", token="private-token", agent_id=REFERENCE,
                                 persona_file=self.persona)
        self.store = SessionStore(self.path)
        self.host = Host()

    async def resolve(self, scope=SCOPE, store=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond)) as client:
            return await resolve_agent(Gateway(self.config, client), scope, store or self.store)

    async def test_fresh_bots_for_distinct_scopes_and_restart_reuses_each_binding(self):
        scopes = [SCOPE, replace(SCOPE, peer_id="101", channel_id="101"),
                  replace(SCOPE, self_id="other-bot"), replace(SCOPE, platform="other"),
                  ConversationScope("qq", "bot", "private", "200"), ConversationScope("qq", "bot", "private", "201")]
        ids = await asyncio.gather(*(self.resolve(scope) for scope in scopes))
        self.assertEqual(len(set(ids)), len(scopes))
        self.assertNotIn(REFERENCE, ids)
        for scope, agent_id in zip(scopes, ids):
            self.assertEqual(await self.resolve(scope, SessionStore(self.path)), agent_id)
        creates = [body for name, body in self.host.calls if name == "createAgent"]
        self.assertEqual(len(creates), len(scopes))
        for body in creates:
            self.assertIn("多惠的人设", body["description"])
            self.assertEqual(set(body), {"name", "description", "isIntroductionSuppressed"})
            self.assertTrue(body["isIntroductionSuppressed"])
        self.assertEqual(self.host.agents[0]["description"], "旧会话")
        self.assertNotIn(self.config.token, self.path.read_text())
        self.assertNotIn("duplicateAgent", [name for name, _ in self.host.calls])

    async def test_changed_persona_updates_only_owned_bot_preserving_other_profile_fields(self):
        agent_id = await self.resolve()
        agent = self.host.agents[-1]
        agent.update(name="我的多惠", title="标题", avatarShape="circle", avatarColor="blue")
        self.persona.write_text("新的多惠人设", encoding="utf-8")
        self.assertEqual(await self.resolve(), agent_id)
        self.assertIn("新的多惠人设", agent["description"])
        self.assertEqual(agent["name"], "我的多惠")
        self.assertEqual(agent["avatarColor"], "blue")
        self.assertEqual(self.host.agents[0]["description"], "旧会话")
        await self.resolve()
        self.assertEqual(sum(name == "updateAgent" for name, _ in self.host.calls), 1)

    async def test_lost_create_response_recovers_by_owner_marker_after_restart(self):
        self.host.lose_create_response = True
        with self.assertRaises(GrokError):
            await self.resolve()
        self.assertIsNone(self.store.snapshot()["bindings"][SCOPE.key]["agent_id"])
        agent_id = await self.resolve(store=SessionStore(self.path))
        self.assertEqual(agent_id, self.host.agents[-1]["id"])
        self.assertEqual(sum(name == "createAgent" for name, _ in self.host.calls), 1)

    async def test_pending_creation_without_remote_result_never_retries_blindly(self):
        self.store.put(SCOPE.key, None, str(uuid4()))
        with self.assertRaisesRegex(GrokError, "尚未确认"):
            await self.resolve()
        self.assertEqual([name for name, _ in self.host.calls], ["listAgents"])

    async def test_rejected_create_does_not_leave_a_permanent_pending_binding(self):
        for status in (400, 401, 403, 404, 422, 429):
            with self.subTest(status=status):
                scope = replace(SCOPE, peer_id=str(status))
                failure = httpx.Response(status, json={"error": "secret-token private upstream body"})

                def respond(request, failure=failure):
                    if request.url.path.endswith("/createAgent"):
                        return failure
                    return self.host.respond(request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(GatewayError) as caught:
                        await resolve_agent(Gateway(self.config, client), scope, self.store)
                self.assertTrue(caught.exception.not_submitted)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertNotIn(scope.key, SessionStore(self.path).snapshot()["bindings"])
                self.assertEqual(await self.resolve(scope), self.host.agents[-1]["id"])

    async def test_connect_failure_can_retry_but_read_write_and_server_failures_stay_pending(self):
        for index, (error, retryable) in enumerate((
            (httpx.ConnectError("secret"), True), (httpx.ConnectTimeout("secret"), True),
            (httpx.PoolTimeout("secret"), True), (httpx.ReadTimeout("secret"), False),
            (httpx.WriteError("secret"), False), (None, False),
        )):
            scope = replace(SCOPE, peer_id=f"network-{index}")

            def respond(request, error=error):
                if request.url.path.endswith("/createAgent"):
                    if error is not None:
                        raise error
                    return httpx.Response(500, text="secret")
                return self.host.respond(request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with self.subTest(error=error), self.assertRaises(GatewayError) as caught:
                    await resolve_agent(Gateway(self.config, client), scope, self.store)
            self.assertEqual(caught.exception.not_submitted, retryable)
            self.assertEqual(scope.key not in self.store.snapshot()["bindings"], retryable)

    async def test_legacy_purpose_binding_migrates_without_creating_another_bot(self):
        marker = conversations.conversation_marker(self.store.snapshot(), SCOPE)
        agent_id = str(uuid4())
        self.store.put(SCOPE.key, agent_id, str(uuid4()))
        self.host.agents.append({"id": agent_id, "isGroup": False, "name": "旧多惠", "description": "旧人设", "purpose": marker})
        self.assertEqual(await self.resolve(), agent_id)
        self.assertIn(f"[{marker}]", self.host.agents[-1]["description"].splitlines())
        self.assertFalse(any(name == "createAgent" for name, _ in self.host.calls))

    async def test_repair_clears_only_current_pending_record_and_does_not_create_or_delete_bots(self):
        nonce = str(uuid4())
        self.store.put(SCOPE.key, None, nonce)
        self.store.put("other-pending", None, str(uuid4()))
        self.store.put("other-bound", str(uuid4()), str(uuid4()))
        before = self.store.snapshot()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond)) as client:
            reply = await repair_binding(Gateway(self.config, client), SCOPE, self.store)
        self.assertIn("已清除", reply)
        after = SessionStore(self.path).snapshot()
        del before["bindings"][SCOPE.key]
        self.assertEqual(before, after)
        self.assertEqual([name for name, _ in self.host.calls], ["listAgents"])
        self.assertEqual(await self.resolve(), self.host.agents[-1]["id"])

    async def test_repair_recovers_a_created_bot_and_preserves_active_binding(self):
        self.host.lose_create_response = True
        with self.assertRaises(GrokError):
            await self.resolve()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond)) as client:
            gw = Gateway(self.config, client)
            self.assertIn("已确认", await repair_binding(gw, SCOPE, self.store))
            self.assertIn("已确认", await repair_binding(gw, SCOPE, self.store))
        self.assertEqual(self.store.snapshot()["bindings"][SCOPE.key]["agent_id"], self.host.agents[-1]["id"])
        self.assertEqual(sum(name == "createAgent" for name, _ in self.host.calls), 1)

    async def test_repair_refuses_unmarked_same_name_or_failed_roster_check(self):
        self.store.put(SCOPE.key, None, str(uuid4()))
        before = self.path.read_bytes()
        self.host.agents.append({"id": str(uuid4()), "name": conversations.conversation_name(SCOPE), "isGroup": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond)) as client:
            with self.assertRaisesRegex(GrokError, "同名"):
                await repair_binding(Gateway(self.config, client), SCOPE, self.store)
        self.assertEqual(self.path.read_bytes(), before)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with self.assertRaises(GrokError):
                await repair_binding(Gateway(self.config, client), SCOPE, self.store)
        self.assertEqual(self.path.read_bytes(), before)

    def test_clear_pending_checks_nonce_bound_state_and_atomic_writes(self):
        nonce = str(uuid4())
        self.store.put(SCOPE.key, None, nonce)
        self.assertFalse(self.store.clear_pending(SCOPE.key, str(uuid4())))
        before = self.path.read_bytes()
        with patch.object(conversations.os, "replace", side_effect=OSError("disk full")), self.assertRaises(GrokError):
            self.store.clear_pending(SCOPE.key, nonce)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn(SCOPE.key, self.store.snapshot()["bindings"])
        self.store.put(SCOPE.key, str(uuid4()), nonce)
        self.assertFalse(self.store.clear_pending(SCOPE.key, nonce))

    async def test_bad_or_ambiguous_remote_binding_never_uses_shared_or_different_chat(self):
        await self.resolve()
        original = self.host.agents[-1].copy()
        for replacement in (None, {**original, "description": "different-conversation"},
                            {**original, "id": str(uuid4())}, {**original, "isGroup": True}):
            self.host.agents = self.host.agents[:1] + ([replacement] if replacement else [])
            with self.subTest(replacement=replacement), self.assertRaises(GrokError):
                await self.resolve()
        self.host.agents = [self.host.agents[0], original, {**original, "id": str(uuid4())}]
        with self.assertRaisesRegex(GrokError, "重复"):
            await self.resolve()
        self.assertEqual(sum(name == "createAgent" for name, _ in self.host.calls), 1)

    async def test_atomic_binding_failure_recovers_remote_bot_without_duplicate(self):
        actual = self.store.put
        calls = 0

        def fail_binding(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                with patch.object(conversations.os, "replace", side_effect=OSError("disk full")):
                    return actual(*args)
            return actual(*args)

        with patch.object(self.store, "put", side_effect=fail_binding), self.assertRaisesRegex(GrokError, "保存失败"):
            await self.resolve()
        self.assertIsNone(self.store.snapshot()["bindings"][SCOPE.key]["agent_id"])
        self.assertEqual(await self.resolve(store=SessionStore(self.path)), self.host.agents[-1]["id"])
        self.assertEqual(sum(name == "createAgent" for name, _ in self.host.calls), 1)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {"sessions.json", "persona.md"})

    async def test_corrupt_bindings_and_invalid_persona_fail_before_remote_calls(self):
        for raw in ("{", "[]", '{"version":1}', '{"version":1,"installation":"bad","bindings":{}}'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(GrokError):
                await self.resolve(store=SessionStore(self.path))
            self.assertEqual(self.path.read_text(), raw)
        self.assertFalse(self.host.calls)
        self.path.unlink()
        for content in ("", "x" * 20001):
            self.persona.write_text(content)
            with self.assertRaises(GrokError):
                await self.resolve()
        self.persona.unlink()
        with self.assertRaises(GrokError):
            await self.resolve()
        self.assertFalse(self.host.calls)

    async def test_duplicate_local_ids_are_rejected(self):
        agent_id = await self.resolve()
        with self.assertRaisesRegex(GrokError, "冲突"):
            self.store.put("different scope", agent_id, str(uuid4()))
        payload = self.store.snapshot()
        payload["bindings"]["bad"] = payload["bindings"][SCOPE.key]
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(GrokError):
            SessionStore(self.path).snapshot()

    async def test_prompt_is_routed_to_resolved_bot(self):
        with patch.object(conversations, "make_client", side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond))), \
             patch.object(conversations, "session_store", self.store), patch.object(Gateway, "ask", autospec=True, return_value=Reply("回答")) as ask:
            self.assertEqual(await conversations.ask(self.config, "问题", SCOPE), Reply("回答"))
            self.assertEqual(ask.await_args.args[0].config.agent_id, self.host.agents[-1]["id"])
            self.assertNotEqual(ask.await_args.args[0].config.agent_id, REFERENCE)

    async def test_timeout_cancels_local_wait_and_preserves_created_binding(self):
        async def wait_for_reply(*_):
            await asyncio.Event().wait()

        with patch.object(conversations, "make_client", side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.host.respond))), \
             patch.object(conversations, "session_store", self.store), patch.object(Gateway, "ask", side_effect=wait_for_reply), \
             self.assertRaisesRegex(GrokError, "仍在云端运行"):
            await conversations.ask(replace(self.config, timeout=.03), "问题", SCOPE)
        self.assertEqual(self.store.snapshot()["bindings"][SCOPE.key]["agent_id"], self.host.agents[-1]["id"])
        self.assertFalse(any(name in {"deleteAgent", "interruptAgentRun"} for name, _ in self.host.calls))

    def test_group_members_share_but_private_users_accounts_and_channels_do_not(self):
        self.assertEqual(scope_from_session(session()), scope_from_session(session(user="201")))
        scopes = {scope_from_session(item).key for item in (
            session(), session(group="101"), session(bot="other"), session(private=True), session(private=True, user="201"))}
        self.assertEqual(len(scopes), 5)
        self.assertEqual(scope_from_session(session(private=True)), scope_from_session(session(private=True, group="dm-other")))
        current = session()
        current.event.channel.id = "subchannel"
        self.assertNotEqual(scope_from_session(current), SCOPE)
        for current in (session(bot=""), session(group=""), session(private=True, user="")):
            with self.assertRaises(GrokError):
                scope_from_session(current)


class ConcurrentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = GroupFeatureStore(Path(directory.name) / "switches.json")
        self.other = replace(SCOPE, peer_id="101", channel_id="101")
        for scope in (SCOPE, self.other):
            self.store.set_enabled(scope.feature_scope, "grok_bot", True)
        self.enterContext(patch.object(handlers, "feature_store", self.store))
        self.config = GrokConfig(max_concurrent=2)

    async def test_same_group_backlog_does_not_block_parallel_group(self):
        queue = handlers.RequestQueue()
        first_started, other_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def ask(_, text, scope):
            order.append(text)
            if text == "first":
                first_started.set()
                await release.wait()
            if scope == self.other:
                other_started.set()
            return text

        with patch.object(handlers, "ask", side_effect=ask):
            first = asyncio.create_task(queue.run(self.config, "first", AsyncMock(), SCOPE))
            await asyncio.wait_for(first_started.wait(), 1)
            second = asyncio.create_task(queue.run(self.config, "second", AsyncMock(), SCOPE))
            third = asyncio.create_task(queue.run(self.config, "other", AsyncMock(), self.other))
            await asyncio.wait_for(other_started.wait(), 1)
            self.assertEqual(order, ["first", "other"])
            release.set()
            self.assertEqual(await asyncio.gather(first, second, third), ["first", "second", "other"])
        self.assertEqual((queue.active, queue.pending, queue.slots), (0, 0, {}))

    async def test_global_limit_and_cancellation_of_capacity_waiter(self):
        queue = handlers.RequestQueue()
        entered, release = asyncio.Event(), asyncio.Event()
        config = replace(self.config, max_concurrent=1)

        async def ask(*_):
            entered.set()
            await release.wait()
            return "done"

        with patch.object(handlers, "ask", side_effect=ask) as call:
            first = asyncio.create_task(queue.run(config, "first", AsyncMock(), SCOPE))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(queue.run(config, "other", AsyncMock(), self.other))
            await asyncio.sleep(.01)
            self.assertEqual(call.await_count, 1)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertEqual((queue.active, queue.pending), (1, 1))
            self.assertNotIn(self.other.key, queue.slots)
            release.set()
            await first
        self.assertEqual((queue.active, queue.pending, queue.slots), (0, 0, {}))

    async def test_disabling_while_queued_prevents_submission(self):
        queue = handlers.RequestQueue()
        entered, release = asyncio.Event(), asyncio.Event()

        async def ask(*_):
            entered.set()
            await release.wait()
            return "done"

        with patch.object(handlers, "ask", side_effect=ask) as call:
            first = asyncio.create_task(queue.run(self.config, "first", AsyncMock(), SCOPE))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(queue.run(self.config, "second", AsyncMock(), SCOPE))
            await asyncio.sleep(0)
            self.store.set_enabled(SCOPE.feature_scope, "grok_bot", False)
            release.set()
            await first
            with self.assertRaisesRegex(GrokError, "已关闭"):
                await second
            self.assertEqual(call.await_count, 1)
        self.assertEqual(queue.slots, {})

    async def test_private_default_off_and_only_superuser_can_opt_in_own_chat(self):
        private = session(private=True, user="root")
        other = session(private=True, user="member")
        with patch.object(Config, "SUPERUSERS", ["root"]), patch.object(handlers.queue, "run", return_value="回答") as run, \
             patch.object(GrokConfig, "from_env", return_value=self.config):
            await handlers.handle_grok(private, message("你好"))
            await handlers.handle_grok(other, message("开启"))
            run.assert_not_awaited()
            self.assertIn("仅 SuperUser", str(other.send.await_args))
            await handlers.handle_grok(private, message("开启"))
            self.assertTrue(self.store.is_enabled(scope_from_session(private).feature_scope, "grok_bot"))
            self.assertFalse(self.store.is_enabled(scope_from_session(other).feature_scope, "grok_bot"))
            await handlers.handle_grok(private, message("你好"))
            run.assert_awaited_once()
            await handlers.handle_grok(private, message("关闭"))
            await handlers.handle_grok(private, message("你好"))
            run.assert_awaited_once()

    async def test_only_superuser_can_request_repair_in_the_current_enabled_scope(self):
        with patch.object(Config, "SUPERUSERS", ["root"]), patch.object(handlers.queue, "run", return_value="已修复") as run, \
             patch.object(GrokConfig, "from_env", return_value=self.config):
            for roles in ([], ["admin"], ["owner"]):
                current = session()
                current.event.member = SimpleNamespace(roles=roles)
                await handlers.handle_grok(current, message("修复会话"))
                self.assertIn("仅 SuperUser", str(current.send.await_args))
            run.assert_not_awaited()
            current = session(user="root")
            await handlers.handle_grok(current, message("修复会话"))
            self.assertIs(run.await_args.kwargs["repair_only"], True)
            self.assertEqual(run.await_args.args[3], SCOPE)
            await handlers.handle_grok(current, message("修复会话 101"))
            self.assertNotIn("repair_only", run.await_args.kwargs)

    async def test_repair_waits_for_same_scope_request_and_never_sends_a_prompt(self):
        queue = handlers.RequestQueue()
        entered, release = asyncio.Event(), asyncio.Event()

        async def ask(*_):
            entered.set()
            await release.wait()
            return "answer"

        with patch.object(handlers, "ask", side_effect=ask) as ask_call, patch.object(handlers, "repair", return_value="repaired") as repair:
            first = asyncio.create_task(queue.run(self.config, "question", AsyncMock(), SCOPE))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(queue.run(self.config, "", AsyncMock(), SCOPE, repair_only=True))
            await asyncio.sleep(.01)
            repair.assert_not_awaited()
            release.set()
            self.assertEqual(await asyncio.gather(first, second), ["answer", "repaired"])
            ask_call.assert_awaited_once()
            repair.assert_awaited_once_with(self.config, SCOPE)
        self.assertEqual((queue.active, queue.pending, queue.slots), (0, 0, {}))
