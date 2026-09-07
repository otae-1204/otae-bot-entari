from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from satori import ChannelType, File, Image, Text

from otae_bot.group_features import GroupFeatureStore
from plugins.grok_bot import conversations, handlers, stream
from plugins.grok_bot.config import GrokConfig, GrokError
from plugins.grok_bot.conversations import ConversationScope, SessionStore
from plugins.grok_bot.media import Attachment, Reply

SCOPE = ConversationScope("qq", "bot", "group", "100", "100")
CONFIG = GrokConfig(base_url="http://grok.test:1340", token="secret-token", poll_interval=.001)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 2)


class Host:
    def __init__(self):
        self.agents = {}
        self.entries = {}
        self.busy = set()
        self.nonces = {}
        self.calls = []
        self.hooks = {}
        self.files = {"/out/old.png": (b"image bytes", "image/png"), "/out/file.txt": (b"file bytes", "text/plain")}

    async def respond(self, request):
        command = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((command, body))
        if command in self.hooks:
            value = await self.hooks[command](body)
            if value is not None:
                return value
        if command == "listAgents":
            result = [{**agent, "isRunning": aid in self.busy} for aid, agent in self.agents.items()]
        elif command == "createAgent":
            aid = str(uuid4())
            self.agents[aid] = {"id": aid, "isGroup": False, "isRunning": False, "isComposingMessage": False,
                                "name": body["name"], "description": body["description"]}
            self.entries[aid] = []
            result = {"agent": self.agents[aid]}
        elif command == "sendPrompt":
            aid, nonce = body["agentId"], body["clientNonce"]
            self.nonces[nonce] = aid
            self.entries[aid].append({"kind": "message", "role": "user", "content": body["prompt"]})
            self.busy.add(aid)
            result = {"accepted": True}
        elif command == "promptAcceptanceStatus":
            nonce = body["clientNonce"]
            result = {"outcome": "found", "record": {"clientNonce": nonce, "agentId": self.nonces[nonce], "status": "accepted"}}
        elif command in {"getAsyncTasks", "getSubagents"}:
            result = []
        elif command == "getAgentTranscriptTail":
            result = {"entries": self.entries[body["id"]]}
        elif command == "uploadAttachment":
            result = {"path": "/input/" + body["filename"]}
        elif command == "readAttachmentChunk":
            data, mime = self.files[body["path"]]
            result = {"totalSize": len(data), "mime": mime,
                      "bytesBase64": base64.b64encode(data[body["offset"]:body["offset"] + body["length"]]).decode()}
        else:
            raise AssertionError(command)
        return httpx.Response(200, json=result)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.respond), trust_env=False)

    def publish(self, aid, text="", path=""):
        message = {"type": "attachment", "url": path} if path else {"type": "text", "content": text}
        self.entries[aid].append({"kind": "send-message", "message": message})

    def prompts(self):
        return [body for command, body in self.calls if command == "sendPrompt"]


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.switches = GroupFeatureStore(root / "switches.json")
        self.switches.set_enabled(SCOPE.feature_scope, "grok_bot", True)
        self.host = Host()
        self.hub = stream.RequestQueue()
        self.enterContext(patch.object(stream, "feature_store", self.switches))
        self.enterContext(patch.object(handlers, "feature_store", self.switches))
        self.enterContext(patch.object(conversations, "session_store", SessionStore(root / "sessions.json")))
        self.enterContext(patch.object(conversations, "make_client", side_effect=self.host.client))
        self.addAsyncCleanup(self.hub.close)
        self.received = []

    def deliver(self, who):
        async def send(reply, *, reply_to=True):
            self.received.append((who, reply, reply_to() if callable(reply_to) else reply_to))
        return send

    async def submit(self, text="first", who="A", scope=SCOPE, **kwargs):
        return await self.hub.run(CONFIG, text, AsyncMock(), scope, on_reply=self.deliver(who), **kwargs)

    async def test_uninterrupted_text_and_files_reply_to_initial_input_and_next_round_resets(self):
        self.assertEqual(await self.submit(), Reply())
        live = self.hub.slots[SCOPE.key]
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, "正文")
        self.host.publish(aid, path="/out/old.png")
        self.host.publish(aid, path="/out/file.txt")
        self.host.busy.clear()
        await asyncio.wait_for(live.task, 2)
        self.assertEqual([reply.text for _, reply, _ in self.received if reply.text], ["正文"])
        self.assertEqual([item.data for _, reply, _ in self.received for item in reply.attachments], [b"image bytes", b"file bytes"])
        self.assertTrue(all(who == "A" and quote for who, _, quote in self.received))
        self.assertEqual(self.hub.active, 0)
        await self.submit("next round", "C")
        self.host.publish(aid, "新一轮")
        await until(lambda: any(reply.text == "新一轮" for _, reply, _ in self.received))
        self.assertEqual(self.received[-1], ("C", Reply("新一轮"), True))
        self.assertEqual(len(self.host.agents), 1)

    async def test_followup_is_submitted_while_busy_and_one_receiver_delivers_without_quote(self):
        await self.submit()
        live = self.hub.slots[SCOPE.key]
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, "先发一条")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0], ("A", Reply("先发一条"), True))
        await self.submit("改成蓝色", "B")
        self.assertIs(self.hub.slots[SCOPE.key], live)
        self.assertEqual(len(self.host.prompts()), 2)
        self.assertIn(aid, self.host.busy)
        self.assertEqual(self.hub.active, 1)
        self.host.publish(aid, "根据补充调整")
        self.host.publish(aid, path="/out/old.png")
        self.host.busy.clear()
        await asyncio.wait_for(live.task, 2)
        self.assertEqual([reply.text for _, reply, _ in self.received if reply.text], ["先发一条", "根据补充调整"])
        self.assertTrue(all(who == "A" and not quote for who, _, quote in self.received[1:]))
        self.assertEqual(len(self.host.agents), 1)

    async def test_same_user_followup_also_interrupts_but_another_group_does_not(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        other = replace(SCOPE, peer_id="101", channel_id="101")
        self.switches.set_enabled(other.feature_scope, "grok_bot", True)
        await self.submit("another group", "B", other)
        self.host.publish(aid, "本群正文")
        await until(lambda: bool(self.received))
        self.assertTrue(self.received[0][2])
        await self.submit("same user again")
        self.host.publish(aid, "追加后正文")
        await until(lambda: any(reply.text == "追加后正文" for _, reply, _ in self.received))
        self.assertFalse(self.received[-1][2])
        self.assertEqual(len(self.host.agents), 2)

    async def test_external_cloud_input_disables_quote_without_breaking_receiver(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        self.host.entries[aid].append({"role": "user", "content": "来自应用的补充"})
        self.host.publish(aid, "统一回复")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0], ("A", Reply("统一回复"), False))

    async def test_failed_image_preparation_or_rejected_send_does_not_interrupt_initial_quote(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        with patch.object(stream, "input_images", AsyncMock(side_effect=GrokError("图片无效"))), self.assertRaisesRegex(GrokError, "图片无效"):
            await self.submit("bad image", "B", images=("bad",))

        async def reject(_):
            return httpx.Response(400)

        self.host.hooks["sendPrompt"] = reject
        with self.assertRaisesRegex(GrokError, "HTTP 400"):
            await self.submit("rejected", "B")
        self.host.publish(aid, "原问题正文")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0], ("A", Reply("原问题正文"), True))

    async def test_lost_followup_response_is_reconciled_without_resending(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]

        async def lost(body):
            self.host.nonces[body["clientNonce"]] = aid
            self.host.entries[aid].append({"role": "user", "content": body["prompt"]})
            raise httpx.RemoteProtocolError("secret-token")

        self.host.hooks["sendPrompt"] = lost
        with self.assertRaisesRegex(GrokError, "RemoteProtocolError"):
            await self.submit("followup", "B")
        self.host.publish(aid, "追加后的结果")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0], ("A", Reply("追加后的结果"), False))
        self.assertEqual(len(self.host.prompts()), 2)

    async def test_followup_image_is_uploaded_to_same_bot_without_waiting_for_idle(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        with patch.object(stream, "input_images", AsyncMock(return_value=(Attachment("input.jpg", data=b"jpg"),))):
            await self.submit("解释图片", "B", images=("input-image",))
        uploaded = [body for command, body in self.host.calls if command == "uploadAttachment"]
        self.assertEqual(uploaded[0]["agentId"], aid)
        prompt = self.host.prompts()[-1]
        self.assertEqual(prompt["agentId"], aid)
        self.assertEqual(prompt["attachmentNames"], ["input.jpg"])
        self.assertTrue(prompt["attachmentPaths"][0].startswith("/input/"))

    async def test_capacity_wait_does_not_block_followups_to_running_group_and_cancelled_input_is_not_sent(self):
        config = replace(CONFIG, max_concurrent=1)
        await self.hub.run(config, "first", AsyncMock(), SCOPE, on_reply=self.deliver("A"))
        other = replace(SCOPE, peer_id="101", channel_id="101")
        self.switches.set_enabled(other.feature_scope, "grok_bot", True)
        waiting = asyncio.create_task(self.hub.run(config, "other", AsyncMock(), other, on_reply=self.deliver("B")))
        await until(lambda: self.hub.pending == 1)
        await self.hub.run(config, "followup", AsyncMock(), SCOPE, on_reply=self.deliver("A"))
        self.assertEqual(len(self.host.prompts()), 2)
        self.assertEqual(len(self.host.agents), 1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, "finished")
        self.host.busy.clear()
        await until(lambda: not self.hub.slots)
        self.assertEqual(len(self.host.prompts()), 2)
        self.assertEqual(self.hub.pending, 0)
        self.assertEqual(self.hub.active, 0)

    async def test_shutdown_cancels_file_reader_and_receivers(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def stuck(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.host.hooks["readAttachmentChunk"] = stuck
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, path="/out/old.png")
        await asyncio.wait_for(started.wait(), 2)
        await self.hub.close()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.hub.slots, {})
        self.assertEqual(self.hub.active, 0)
        self.assertEqual(self.hub.pending, 0)
        self.assertEqual(self.received, [])

    async def test_invalid_first_image_does_not_create_a_bot(self):
        with patch.object(stream, "input_images", AsyncMock(side_effect=GrokError("图片无效"))), self.assertRaisesRegex(GrokError, "图片无效"):
            await self.submit(images=("bad",))
        await until(lambda: not self.hub.slots)
        self.assertEqual(self.host.agents, {})
        self.assertEqual(self.hub.pending, 0)

    async def test_disabling_while_followup_uploads_prevents_its_prompt(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def upload(_):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"path": "/input/image.jpg"})

        self.host.hooks["uploadAttachment"] = upload
        await self.submit()
        with patch.object(stream, "input_images", AsyncMock(return_value=(Attachment("image.jpg", data=b"jpg"),))):
            followup = asyncio.create_task(self.submit("picture", "B", images=("source",)))
            await asyncio.wait_for(started.wait(), 2)
            self.switches.set_enabled(SCOPE.feature_scope, "grok_bot", False)
            release.set()
            with self.assertRaises(GrokError):
                await followup
        await until(lambda: not self.hub.slots)
        self.assertEqual(len(self.host.prompts()), 1)
        self.assertEqual(self.hub.pending, 0)

    async def test_cancelling_an_unsent_upload_does_not_submit_it_or_stop_receiver(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def prepare(*_):
            started.set()
            await release.wait()
            return (Attachment("image.jpg", data=b"jpg"),)

        await self.submit()
        with patch.object(stream, "input_images", prepare):
            followup = asyncio.create_task(self.submit("picture", "B", images=("source",)))
            await asyncio.wait_for(started.wait(), 2)
            followup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await followup
            release.set()
            await until(lambda: self.hub.pending == 0)
        self.assertEqual(len(self.host.prompts()), 1)
        self.assertTrue(self.hub.slots[SCOPE.key].can_quote())

    async def test_pending_input_limit_and_wait_timeout_do_not_send_late(self):
        config = replace(CONFIG, max_concurrent=1)
        await self.hub.run(config, "first", AsyncMock(), SCOPE, on_reply=self.deliver("A"))
        other = replace(SCOPE, peer_id="101", channel_id="101")
        self.switches.set_enabled(other.feature_scope, "grok_bot", True)
        waiting = asyncio.create_task(self.hub.run(replace(config, timeout=.05), "wait", AsyncMock(), other, on_reply=self.deliver("B")))
        await until(lambda: self.hub.pending == 1)
        with self.assertRaisesRegex(GrokError, "队列已满"):
            await self.hub.run(replace(config, max_pending=1), "not accepted", AsyncMock(), SCOPE, on_reply=self.deliver("C"))
        with self.assertRaises(GrokError):
            await waiting
        self.assertEqual(len(self.host.prompts()), 1)
        self.assertEqual(len(self.host.agents), 1)

    async def test_repair_refuses_active_receiver_and_new_input_waits_for_repair(self):
        await self.submit()
        with patch.object(conversations, "repair", AsyncMock()) as repair, self.assertRaisesRegex(GrokError, "正在接收"):
            await self.hub.run(CONFIG, "", AsyncMock(), SCOPE, repair_only=True)
        repair.assert_not_awaited()
        other = replace(SCOPE, peer_id="101", channel_id="101")
        self.switches.set_enabled(other.feature_scope, "grok_bot", True)
        started, release = asyncio.Event(), asyncio.Event()

        async def repair_call(*_):
            started.set()
            await release.wait()
            return "repaired"

        with patch.object(conversations, "repair", repair_call):
            repair_task = asyncio.create_task(self.hub.run(CONFIG, "", AsyncMock(), other, repair_only=True))
            await asyncio.wait_for(started.wait(), 2)
            question = asyncio.create_task(self.submit("after repair", "B", other))
            await asyncio.sleep(.01)
            self.assertEqual(len(self.host.prompts()), 1)
            release.set()
            self.assertEqual(await repair_task, "repaired")
            await asyncio.wait_for(question, 2)
        self.assertEqual(len(self.host.prompts()), 2)

    async def test_new_input_during_idle_check_keeps_same_receiver_open(self):
        await self.submit()
        live = self.hub.slots[SCOPE.key]
        aid = self.host.prompts()[0]["agentId"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def state_read(_):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=[])

        self.host.hooks["getAsyncTasks"] = state_read
        self.host.publish(aid, "初始答案")
        self.host.busy.clear()
        await asyncio.wait_for(entered.wait(), 2)
        await self.submit("followup", "B")
        release.set()
        self.assertIs(self.hub.slots[SCOPE.key], live)
        self.host.publish(aid, "追加答案")
        self.host.busy.clear()
        await asyncio.wait_for(live.task, 2)
        self.assertEqual([reply.text for _, reply, _ in self.received if reply.text], ["初始答案", "追加答案"])
        self.assertFalse(self.received[-1][2])

    async def test_old_answer_cannot_finish_newly_accepted_but_not_started_followup(self):
        await self.submit()
        live = self.hub.slots[SCOPE.key]
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, "旧答案")
        await until(lambda: bool(self.received))
        await self.submit("new question", "B")
        self.host.busy.clear()  # Transient idle before the cloud runner starts.
        await asyncio.sleep(.025)
        self.assertIs(self.hub.slots.get(SCOPE.key), live)
        self.host.publish(aid, "新答案")
        await asyncio.wait_for(live.task, 2)
        self.assertEqual(self.received[-1], ("A", Reply("新答案"), False))

    async def test_receiver_cancelled_before_first_step_releases_queued_input(self):
        item = stream.Input("unsent", (), None, self.deliver("A"), asyncio.get_running_loop().create_future(),
                            asyncio.get_running_loop().time() + 10)
        live = stream.LiveConversation(self.hub, CONFIG, SCOPE, item)
        self.hub.slots[SCOPE.key] = live
        self.hub.pending = 1
        live.enqueue(item)
        live.task = asyncio.create_task(live.run())
        await self.hub.close()
        with self.assertRaisesRegex(GrokError, "尚未提交"):
            await item.done
        self.assertEqual((self.hub.active, self.hub.pending, self.hub.slots), (0, 0, {}))
        self.assertEqual(self.host.calls, [])

    async def test_async_rejection_of_followup_keeps_initial_reply_owner(self):
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        await until(lambda: self.hub.slots[SCOPE.key].submissions[0].status == "accepted")

        async def reject(body):
            return httpx.Response(200, json={"outcome": "found", "record": {
                "agentId": aid, "clientNonce": body["clientNonce"], "status": "rejected"}})

        async def enqueue_only(body):
            self.host.nonces[body["clientNonce"]] = aid
            return httpx.Response(200, json={"accepted": True})

        self.host.hooks["sendPrompt"] = enqueue_only
        self.host.hooks["promptAcceptanceStatus"] = reject
        await self.submit("rejected followup", "B")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0][0], "B")
        self.assertIn("拒绝", self.received[0][1].text)
        self.host.publish(aid, "初始问题仍然回复 A")
        await until(lambda: len(self.received) == 2)
        self.assertEqual(self.received[-1], ("A", Reply("初始问题仍然回复 A"), True))

    async def test_unknown_durability_still_requires_initial_prompt_anchor(self):
        async def unknown(_):
            return httpx.Response(200, json={"outcome": "unknown-durability"})

        self.host.hooks["promptAcceptanceStatus"] = unknown
        await self.submit()
        aid = self.host.prompts()[0]["agentId"]
        prompt = self.host.entries[aid].pop()
        self.host.publish(aid, "历史内容")
        await asyncio.sleep(.02)
        self.assertEqual(self.received, [])
        self.host.entries[aid].append(prompt)
        self.host.publish(aid, "本轮内容")
        await until(lambda: bool(self.received))
        self.assertEqual(self.received[0], ("A", Reply("本轮内容"), True))

    async def test_total_receiver_timeout_reports_once_without_resubmitting(self):
        await self.hub.run(replace(CONFIG, timeout=.08), "first", AsyncMock(), SCOPE, on_reply=self.deliver("A"))
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, "正文先到")
        await until(lambda: not self.hub.slots)
        self.assertEqual(self.received[0], ("A", Reply("正文先到"), True))
        self.assertEqual(len(self.received), 2)
        self.assertIn("接收等待超过", self.received[-1][1].text)
        self.assertEqual(len(self.host.prompts()), 1)

    async def test_qq_upload_checks_interruption_before_image_send(self):
        uploading, release = asyncio.Event(), asyncio.Event()

        async def upload(*_):
            uploading.set()
            await release.wait()
            return ["internal:image"]

        session = SimpleNamespace(send=AsyncMock(return_value=["receipt"]),
                                  account=SimpleNamespace(adapter="test", protocol=SimpleNamespace(upload_create=AsyncMock(side_effect=upload))))

        async def deliver(reply, *, reply_to=True):
            await handlers.send_reply(session, reply, reply_to=reply_to)

        await self.hub.run(CONFIG, "first", AsyncMock(), SCOPE, on_reply=deliver)
        aid = self.host.prompts()[0]["agentId"]
        self.host.publish(aid, path="/out/old.png")
        await asyncio.wait_for(uploading.wait(), 2)
        await self.submit("change picture", "B")
        release.set()
        await until(lambda: session.send.await_count > 0)
        self.assertIsInstance(session.send.await_args.args[0][0], Image)
        self.assertFalse(session.send.await_args.kwargs["reply_to"])
        self.assertFalse(self.received)  # Initial session remains the sole delivery channel.

    async def test_qq_file_has_initial_quote_notice_only_when_uninterrupted(self):
        for quote in (True, False):
            session = SimpleNamespace(send=AsyncMock(return_value=["receipt"]), account=SimpleNamespace(
                adapter="test", protocol=SimpleNamespace(upload_create=AsyncMock(return_value=["internal:file"]))),
                event=SimpleNamespace(channel=SimpleNamespace(type=ChannelType.TEXT)))
            await handlers.send_reply(session, Reply(attachments=(Attachment("result.txt", data=b"result"),)), reply_to=lambda value=quote: value)
            calls = session.send.await_args_list
            if quote:
                self.assertIsInstance(calls[0].args[0][0], Text)
                self.assertTrue(calls[0].kwargs["reply_to"])
            self.assertEqual(len(calls), 2 if quote else 1)
            self.assertIsInstance(calls[-1].args[0][0], File)
            self.assertFalse(calls[-1].kwargs["reply_to"])
