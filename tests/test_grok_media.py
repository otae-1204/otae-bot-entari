from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from arclet.entari import MessageChain
from PIL import Image as PILImage
from satori import ChannelType, File, Image, MessageObject, Text

from otae_bot.group_features import GroupFeatureStore
from plugins.grok_bot import conversations, gateway, handlers, media
from plugins.grok_bot.config import GrokConfig, GrokError
from plugins.grok_bot.conversations import ConversationScope, SessionStore
from plugins.grok_bot.media import Attachment, Reply
from plugins.grok_bot.relay import ReplyRelay


def png() -> bytes:
    buffer = BytesIO()
    PILImage.new("RGB", (16, 12), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def session():
    return SimpleNamespace(account=SimpleNamespace(protocol=SimpleNamespace(upload_create=AsyncMock(return_value=["internal:uploaded"]))),
                           send=AsyncMock(return_value=[SimpleNamespace(id="receipt")]))


class ReplyTests(unittest.TestCase):
    def test_desktop_attachment_url_and_file_name_are_supported(self):
        item = gateway.attachments_from({"type": "attachment", "url": "file:///output/test.pdf", "file_name": "测试.pdf"})[0]
        self.assertEqual((item.name, item.source), ("测试.pdf", "file:///output/test.pdf"))
        item = gateway.attachments_from({"type": "attachment", "url": "file:///output/test%20file.pdf"})[0]
        self.assertEqual(item.name, "test file.pdf")

    def test_text_images_and_files_are_matched_only_after_the_current_prompt(self):
        entries = [
            {"kind": "send-message", "message": {"type": "attachment", "fileName": "old.pdf", "path": "/old.pdf"}},
            {"kind": "message", "role": "user", "content": "[current] question"},
            {"kind": "user-attachment", "fileName": "input.png", "file_path": "/input.png"},
            {"kind": "tool-call", "message": {"type": "attachment", "path": "/tool-private.txt"}},
            {"kind": "message", "role": "assistant", "content": "intermediate"},
            {"kind": "send-message", "message": {"type": "text", "content": "回答", "images": [{"url": "file:///output/plot.png", "alt": "plot.png"}]}},
            {"kind": "send-message", "message": {"type": "attachment", "fileName": "report.pdf", "mime": "application/pdf", "file_path": "/output/report.pdf"}},
            {"kind": "send-message", "streaming": True, "message": {"type": "attachment", "path": "/preview.txt"}},
        ]
        reply = gateway.reply_from(entries, "[current]")
        self.assertEqual(reply.text, "回答")
        self.assertEqual([(item.name, item.source, item.image) for item in reply.attachments],
                         [("plot.png", "file:///output/plot.png", True), ("report.pdf", "/output/report.pdf", False)])
        self.assertIsNone(gateway.reply_from(entries, "[different]"))
        entries.append({"role": "user", "content": "someone else"})
        with self.assertRaisesRegex(GrokError, "另一条输入"):
            gateway.reply_from(entries, "[current]")

    def test_attachment_only_answer_is_complete_and_deduplicated(self):
        attachment = {"kind": "send-message", "message": {"type": "attachment", "attachmentPaths": ["/out/file.zip"], "attachmentNames": ["file.zip"]}}
        entries = [{"role": "user", "content": "marker"}, {"role": "assistant", "content": "internal"}, attachment, attachment]
        reply = gateway.reply_from(entries, "marker")
        self.assertEqual(reply.text, "")
        self.assertEqual(len(reply.attachments), 1)
        self.assertEqual(reply.attachments[0].name, "file.zip")

    def test_missing_attachment_content_and_over_limit_are_not_silently_dropped(self):
        entries = [{"role": "user", "content": "marker"}]
        entries.extend({"kind": "send-message", "message": {"type": "attachment", "fileName": f"file-{index}.zip"}}
                       for index in range(media.MAX_REPLY_FILES + 1))
        reply = gateway.reply_from(entries, "marker")
        self.assertEqual(len(reply.attachments), media.MAX_REPLY_FILES)
        self.assertIn("其余附件", reply.text)


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_images_are_decoded_and_normalized_in_input_order(self):
        images = await media.input_images((data_url(png()), data_url(png())))
        self.assertEqual([image.name for image in images], ["qq-image-1.jpg", "qq-image-2.jpg"])
        for image in images:
            with PILImage.open(BytesIO(image.data)) as decoded:
                self.assertEqual(decoded.size, (16, 12))
                self.assertEqual(decoded.format, "JPEG")
            self.assertNotIn("base64", repr(image))
        with self.assertRaisesRegex(GrokError, "3 张"):
            await media.input_images((data_url(png()),) * 4)

    async def test_corrupt_inline_images_and_oversized_input_are_rejected(self):
        for source in ("data:image/png;base64,!!", "data:image/png,hello", data_url(b"not an image")):
            with self.subTest(source=source), self.assertRaises(GrokError):
                await media.input_images((source,))
        with patch.object(media, "MAX_IMAGE_BYTES", 2), self.assertRaises(GrokError):
            await media.input_images((data_url(png()),))

    async def test_public_download_pins_dns_and_never_attaches_gateway_credentials(self):
        calls = []

        def response(request):
            calls.append(request)
            return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

        real_client = httpx.AsyncClient
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))])), \
             patch.object(media.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs)):
            payload, mime = await media.download_url("https://cdn.example/photo.png?signature=private", limit=10000)
        self.assertEqual(payload, png())
        self.assertEqual(mime, "image/png")
        self.assertEqual(calls[0].url.host, "1.1.1.1")
        self.assertEqual(calls[0].headers["host"], "cdn.example")
        self.assertEqual(calls[0].extensions["sni_hostname"], "cdn.example")
        self.assertNotIn("authorization", calls[0].headers)

    async def test_private_hosts_local_files_and_redirect_to_lan_are_blocked(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])):
            for url in ("file:///etc/passwd", "http://127.0.0.1", "http://lan.example", "http://user:secret@example.com", "http://example.com:1340"):
                with self.subTest(url=url), self.assertRaises(GrokError) as caught:
                    await media.public_request(url)
                self.assertNotIn("secret", str(caught.exception))
        real_client = httpx.AsyncClient
        addresses = [[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))],
                     [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]]
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=addresses)), \
             patch.object(media.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})), **kwargs)), \
             self.assertRaises(GrokError):
            await media.download_url("https://cdn.example/photo.png", limit=10000)

    async def test_internal_image_uses_only_the_current_bridge_and_is_bounded(self):
        account = SimpleNamespace(ensure_url=lambda url: "http://127.0.0.1:5500/v1/proxy/" + url)
        real_client = httpx.AsyncClient
        calls = []

        def response(request):
            calls.append(request)
            return httpx.Response(200, content=png())

        with patch.object(media.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs)):
            data, _ = await media.download_url("internal:image", limit=10000, account=account)
            self.assertEqual(data, png())
            with self.assertRaisesRegex(GrokError, "大小"):
                await media.download_url("internal:image", limit=1, account=account)
        self.assertEqual(calls[0].url.path, "/v1/proxy/internal:image")
        self.assertNotIn("authorization", calls[0].headers)
        with self.assertRaises(GrokError):
            await media.download_url("internal:image", limit=10000)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_llonebot_files_use_same_account_upload_api_with_actual_bytes(self):
        for private in (False, True):
            with self.subTest(private=private):
                target = session()
                target.account.adapter = "llonebot"
                target.account.protocol.internal = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"file_id": "sent"}})
                target.event = SimpleNamespace(channel=SimpleNamespace(type=ChannelType.DIRECT if private else ChannelType.TEXT, id="100"),
                                               user=SimpleNamespace(id="200"), guild=None if private else SimpleNamespace(id="100"))
                await media.send_attachment(target, Attachment("report.txt", data=b"report"))
                call = target.account.protocol.internal.await_args
                self.assertEqual(call.args[0], "onebot11/upload_private_file" if private else "onebot11/upload_group_file")
                self.assertEqual(call.kwargs["user_id" if private else "group_id"], 200 if private else 100)
                self.assertEqual(base64.b64decode(call.kwargs["file"].removeprefix("base64://")), b"report")
                self.assertEqual(call.kwargs["name"], "report.txt")
                target.send.assert_not_awaited()
                target.account.protocol.upload_create.assert_not_awaited()
                target.account.protocol.internal.side_effect = asyncio.TimeoutError("secret-token")
                with self.assertRaises(GrokError) as caught:
                    await media.send_attachment(target, Attachment("report.txt", data=b"report"))
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(target.account.protocol.internal.await_count, 2)
                target.send.assert_not_awaited()

    async def test_real_image_and_file_bytes_are_uploaded_to_the_current_qq_bridge(self):
        current = session()
        for is_image in (True, False):
            item = Attachment("../report.pdf", mime="application/pdf", image=is_image, data=b"file bytes")
            await media.send_attachment(current, item)
            upload = current.account.protocol.upload_create.await_args.args[0]
            self.assertEqual(upload.file, b"file bytes")
            self.assertEqual(upload.name, "report.pdf")
            element = current.send.await_args.args[0][0]
            self.assertIsInstance(element, Image if is_image else File)
            self.assertEqual(element.src, "internal:uploaded")
            self.assertEqual(current.send.await_args.kwargs["reply_to"], is_image)

    async def test_bridge_without_upload_create_gets_inline_media_not_a_windows_local_path(self):
        current = session()
        current.account.protocol.upload_create.side_effect = NotImplementedError
        await media.send_attachment(current, Attachment("out.txt", mime="text/plain", data=b"hello"))
        element = current.send.await_args.args[0][0]
        self.assertIsInstance(element, File)
        self.assertTrue(element.src.startswith("data:text/plain;base64,"))
        self.assertEqual(base64.b64decode(element.src.split(",", 1)[1]), b"hello")

    async def test_reply_keeps_text_and_other_attachments_when_one_fails(self):
        current = session()
        reply = Reply("正文", (Attachment("bad.txt", error="下载失败"), Attachment("good.txt", data=b"good")))
        await handlers.send_reply(current, reply)
        sent = [call.args[0] for call in current.send.await_args_list]
        self.assertEqual(str(sent[0]), "正文")
        self.assertIn("bad.txt", str(sent[1]))
        self.assertIn("good.txt", str(sent[2]))
        self.assertIsInstance(sent[3][0], File)
        self.assertEqual(current.account.protocol.upload_create.await_count, 1)

    async def test_send_failure_is_redacted_and_does_not_resend_ambiguously(self):
        current = session()
        current.send.side_effect = RuntimeError("secret-token base64-payload")
        with self.assertRaises(GrokError) as caught:
            await media.send_attachment(current, Attachment("out.txt", data=b"secret-file"))
        self.assertNotIn("secret", str(caught.exception))
        current.send.assert_awaited_once()

    def test_current_and_quoted_image_elements_are_extractable_without_at_injection(self):
        chain = MessageChain([Text("问题"), Image(src="https://cdn.example/current.png")])
        quoted = MessageChain(MessageObject("quoted", '<img src="https://cdn.example/quoted.png"/>').message)
        self.assertEqual(handlers.plain_text(chain), "问题")
        self.assertEqual(handlers.image_sources(chain) + handlers.image_sources(quoted),
                         ("https://cdn.example/current.png", "https://cdn.example/quoted.png"))


AGENT = "00000000-0000-4000-8000-000000000001"
SCOPE = ConversationScope("qq", "bot", "group", "100", "100")
CONFIG = GrokConfig(base_url="http://grok.test:1340", token="private-token", poll_interval=0)


class MediaHost:
    """Attachment wire keys verified in the official desktop 0.30.0 bundle."""

    def __init__(self):
        self.calls = []
        self.agents = []
        self.prompt = self.nonce = ""
        self.files = {"/out/photo.png": png(), "/out/report.txt": b"report bytes"}

    def respond(self, request):
        command = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((command, body))
        if command == "listAgents":
            result = self.agents
        elif command == "createAgent":
            self.agents.append({"id": AGENT, "name": body["name"], "description": body["description"], "isGroup": False,
                                "isRunning": False, "isComposingMessage": False})
            result = {"agent": self.agents[0]}
        elif command in {"getAsyncTasks", "getSubagents"}:
            result = []
        elif command == "uploadAttachment":
            assert set(body) == {"agentId", "filename", "bytesBase64"}
            assert body["agentId"] == AGENT
            path = "/attachments/" + body["filename"]
            self.files[path] = base64.b64decode(body["bytesBase64"], validate=True)
            result = {"path": path}
        elif command == "sendPrompt":
            assert body["agentId"] == AGENT
            self.prompt, self.nonce = body["prompt"], body["clientNonce"]
            result = {"accepted": True}
        elif command == "promptAcceptanceStatus":
            result = {"outcome": "found", "record": {"agentId": AGENT, "clientNonce": self.nonce, "status": "accepted"}}
        elif command == "getAgentTranscriptTail":
            result = {"entries": [
                {"role": "user", "content": "old question"},
                {"kind": "send-message", "message": {"type": "attachment", "url": "/other/private.txt"}},
                {"role": "user", "content": self.prompt},
                {"kind": "send-message", "message": {"type": "text", "content": "", "images": [{"url": "file:///out/photo.png"}]}},
                {"kind": "send-message", "message": {"type": "attachment", "url": "file:///out/report.txt", "file_name": "report.txt"}},
            ]}
        elif command == "readAttachmentChunk":
            assert set(body) == {"agentId", "path", "offset", "length"}
            assert body["agentId"] == AGENT
            data = self.files[body["path"]]
            result = {"totalSize": len(data), "bytesBase64": base64.b64encode(data[body["offset"]:body["offset"] + body["length"]]).decode(),
                      "mime": "image/png" if body["path"].endswith(".png") else "text/plain"}
        else:
            raise AssertionError(command)
        return httpx.Response(200, json=result)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.respond), trust_env=False)


class GatewayMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcript_and_partial_attachment_disconnects_preserve_early_reply_without_duplicates(self):
        host = MediaHost()
        original = host.respond
        tail_reads = file_reads = 0
        delivered = []

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"bytesBase64":"private-token'
                raise httpx.ReadError("private-token upstream-body")

        def respond(request):
            nonlocal tail_reads, file_reads
            response = original(request)
            command = request.url.path.rsplit("/", 1)[-1]
            if command == "getAgentTranscriptTail":
                tail_reads += 1
                if tail_reads == 2:
                    raise httpx.RemoteProtocolError("private-token")
                payload = response.json()
                payload["entries"][3]["message"]["content"] = "正文先到"
                if tail_reads > 2:
                    payload["entries"].append({"kind": "send-message", "message": {"type": "text", "content": "恢复后的正文"}})
                return httpx.Response(200, json=payload)
            if command == "readAttachmentChunk" and json.loads(request.content)["length"] > 0:
                file_reads += 1
                if file_reads == 1:
                    return httpx.Response(200, stream=BrokenStream())
            return response

        async def deliver(reply):
            delivered.append(reply)

        host.respond = respond
        with tempfile.TemporaryDirectory() as directory, patch.object(conversations, "make_client", side_effect=host.client), \
             patch.object(conversations, "session_store", SessionStore(Path(directory) / "sessions.json")), \
             patch.object(gateway, "READ_RETRY_DELAYS", (0, 0)):
            result = await conversations.ask(CONFIG, "问题", SCOPE, on_reply=deliver)
        self.assertEqual(result, Reply())
        self.assertEqual([reply.text for reply in delivered if reply.text], ["正文先到", "恢复后的正文"])
        self.assertEqual([item.data for reply in delivered for item in reply.attachments], [png(), b"report bytes"])
        self.assertEqual(sum(command == "sendPrompt" for command, _ in host.calls), 1)
        self.assertEqual(sum(command == "createAgent" for command, _ in host.calls), 1)

    async def test_streaming_pipeline_delivers_text_and_each_file_before_idle_without_duplicates(self):
        host = MediaHost()
        original = host.respond
        idle, text_sent, first_file_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release_second, second_started = asyncio.Event(), asyncio.Event()
        delivered = []

        def respond(request):
            command = request.url.path.rsplit("/", 1)[-1]
            response = original(request)
            if command == "getAsyncTasks" and host.prompt and not idle.is_set():
                return httpx.Response(200, json=[{"id": "still-running"}])
            if command == "getAgentTranscriptTail":
                payload = response.json()
                payload["entries"][3]["message"]["content"] = "先发正文"
                if second_started.is_set():
                    payload["entries"].append({"kind": "send-message", "message": {"type": "text", "content": "后续正文"}})
                return httpx.Response(200, json=payload)
            return response

        host.respond = respond
        original_read = gateway.Gateway.read_attachment

        async def read(api, item, limit):
            self.assertTrue(text_sent.is_set())
            if item.name == "report.txt":
                second_started.set()
                await release_second.wait()
            return await original_read(api, item, limit)

        async def deliver(reply):
            delivered.append(reply)
            if reply.text == "先发正文":
                text_sent.set()
            if reply.attachments:
                first_file_sent.set()

        with tempfile.TemporaryDirectory() as directory:
            switches = GroupFeatureStore(Path(directory) / "switches.json")
            switches.set_enabled(SCOPE.feature_scope, "grok_bot", True)
            with patch.object(conversations, "make_client", side_effect=host.client), \
                 patch.object(conversations, "session_store", SessionStore(Path(directory) / "sessions.json")), \
                 patch.object(handlers, "feature_store", switches), patch.object(gateway.Gateway, "read_attachment", read):
                task = asyncio.create_task(conversations.ask(CONFIG, "问题", SCOPE, on_reply=deliver))
                try:
                    await asyncio.wait_for(first_file_sent.wait(), 1)
                    await asyncio.wait_for(second_started.wait(), 1)
                    # More text must get through while the second file is blocked.
                    for _ in range(100):
                        if any(reply.text == "后续正文" for reply in delivered):
                            break
                        await asyncio.sleep(.001)
                    self.assertTrue(any(reply.text == "后续正文" for reply in delivered))
                    self.assertFalse(task.done())
                    self.assertEqual(sum(bool(reply.attachments) for reply in delivered), 1)
                    release_second.set()
                    idle.set()
                    result = await asyncio.wait_for(task, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(result, Reply())
        self.assertEqual([reply.text for reply in delivered if reply.text], ["先发正文", "后续正文"])
        self.assertEqual([item.data for reply in delivered for item in reply.attachments], [png(), b"report bytes"])
        self.assertEqual(sum(command == "sendPrompt" for command, _ in host.calls), 1)

    async def test_relay_limits_and_repeated_snapshots_apply_to_whole_question(self):
        api = gateway.Gateway(CONFIG, None)
        deliver = AsyncMock()
        relay = ReplyRelay(api, deliver)
        attachments = tuple(Attachment(f"{i}.txt", f"/out/{i}.txt") for i in range(media.MAX_REPLY_FILES + 2))
        with patch.object(api, "read_attachment", AsyncMock(return_value=(b"12", "text/plain"))) as read:
            relay.budget.remaining = 2
            await relay.publish(Reply("x" * 20001, attachments))
            await relay.publish(Reply("x" * 20001 + "追加正文", attachments))
            await relay.finish()
        texts = [call.args[0].text for call in deliver.await_args_list if call.args[0].text]
        self.assertEqual(sum("回答过长" in text for text in texts), 1)
        self.assertEqual(sum("附件超过" in text for text in texts), 1)
        self.assertNotIn("追加正文", "".join(texts))
        self.assertEqual(read.await_count, 1)
        self.assertEqual(len([call for call in deliver.await_args_list if call.args[0].attachments]), media.MAX_REPLY_FILES)

    async def test_relay_does_not_resend_when_attachment_metadata_changes(self):
        api = gateway.Gateway(CONFIG, None)
        deliver = AsyncMock()
        relay = ReplyRelay(api, deliver)
        item = Attachment("photo.png", "/out/photo.png")
        with patch.object(api, "read_attachment", AsyncMock(return_value=(png(), "image/png"))) as read:
            await relay.publish(Reply("正文", (item,)))
            await relay.publish(Reply("正文", (replace(item, mime="image/png", image=True),)))
            await relay.finish()
        read.assert_awaited_once()
        self.assertEqual(deliver.await_count, 2)  # One text and one picture.
        with self.assertRaisesRegex(GrokError, "发生变化"):
            await relay.publish(Reply("被改写的旧正文"))
        self.assertEqual(deliver.await_count, 2)

    async def test_attachment_send_failure_is_not_retried_at_finish(self):
        api = gateway.Gateway(CONFIG, None)
        deliver = AsyncMock(side_effect=GrokError("发送结果未知"))
        relay = ReplyRelay(api, deliver)
        with patch.object(api, "read_attachment", AsyncMock(return_value=(b"file", "text/plain"))):
            try:
                await relay.publish(Reply(attachments=(Attachment("file.txt", "/out/file.txt"),)))
                with self.assertRaisesRegex(GrokError, "发送结果未知"):
                    await relay.finish()
            finally:
                await relay.cancel()
        deliver.assert_awaited_once()

    async def test_cancelling_conversation_stops_attachment_worker_and_never_resends(self):
        host = MediaHost()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def read(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        deliver = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.object(conversations, "make_client", side_effect=host.client), \
             patch.object(conversations, "session_store", SessionStore(Path(directory) / "sessions.json")), \
             patch.object(gateway.Gateway, "read_attachment", read):
            task = asyncio.create_task(conversations.ask(CONFIG, "问题", SCOPE, on_reply=deliver))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        deliver.assert_not_awaited()
        self.assertEqual(sum(command == "sendPrompt" for command, _ in host.calls), 1)

    async def test_timeout_after_early_reply_does_not_repeat_text(self):
        host = MediaHost()
        deliver = AsyncMock()

        async def ask(api, prompt, attachments=(), *, on_reply):
            await on_reply(Reply("已完成正文"))
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory, patch.object(conversations, "make_client", side_effect=host.client), \
             patch.object(conversations, "session_store", SessionStore(Path(directory) / "sessions.json")), \
             patch.object(gateway.Gateway, "ask", ask), self.assertRaisesRegex(GrokError, "已转发当前回复"):
            await conversations.ask(replace(CONFIG, timeout=.03), "问题", SCOPE, on_reply=deliver)
        deliver.assert_awaited_once_with(Reply("已完成正文"))

    async def test_images_reach_bound_bot_and_attachment_only_reply_is_downloaded_and_sent(self):
        host = MediaHost()
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.json")
            switches = GroupFeatureStore(Path(directory) / "switches.json")
            switches.set_enabled(SCOPE.feature_scope, "grok_bot", True)
            with patch.object(conversations, "make_client", side_effect=host.client), \
                 patch.object(conversations, "session_store", store), patch.object(handlers, "feature_store", switches):
                reply = await conversations.ask(CONFIG, "解释两张图", SCOPE, images=(data_url(png()), data_url(png())))
        self.assertEqual(reply.text, "")
        self.assertEqual([item.data for item in reply.attachments], [png(), b"report bytes"])
        sent = next(body for command, body in host.calls if command == "sendPrompt")
        self.assertEqual(sent["attachmentNames"], ["qq-image-1.jpg", "qq-image-2.jpg"])
        self.assertEqual(len(set(sent["attachmentPaths"])), 2)
        for path in sent["attachmentPaths"]:
            self.assertTrue(host.files[path].startswith(b"\xff\xd8"))
        self.assertEqual(sum(command == "getAgentTranscriptTail" for command, _ in host.calls), 2)
        self.assertEqual({body["path"] for command, body in host.calls if command == "readAttachmentChunk"}, set(host.files) - set(sent["attachmentPaths"]))
        target = session()
        await handlers.send_reply(target, reply)
        self.assertIsInstance(target.send.await_args_list[0].args[0][0], Image)
        self.assertIn("report.txt", str(target.send.await_args_list[1].args[0]))
        self.assertIsInstance(target.send.await_args_list[2].args[0][0], File)

    async def test_invalid_image_does_not_create_bot_and_failed_upload_never_sends_partial_prompt(self):
        host = MediaHost()
        with tempfile.TemporaryDirectory() as directory, patch.object(conversations, "make_client", side_effect=host.client), \
             patch.object(conversations, "session_store", SessionStore(Path(directory) / "sessions.json")):
            with self.assertRaises(GrokError):
                await conversations.ask(CONFIG, "解释", SCOPE, images=(data_url(b"invalid"),))
            self.assertEqual(host.calls, [])
            original = host.respond

            def fail_upload(request):
                if request.url.path.endswith("uploadAttachment"):
                    return httpx.Response(400, text="secret-token error-body")
                return original(request)

            host.respond = fail_upload
            with self.assertRaises(GrokError) as caught:
                await conversations.ask(CONFIG, "解释", SCOPE, images=(data_url(png()),))
            self.assertNotIn("secret-token", str(caught.exception))
            self.assertFalse(any(command == "sendPrompt" for command, _ in host.calls))

    async def test_chunk_offsets_and_size_limits_prevent_partial_or_oversized_files(self):
        host = MediaHost()
        async with host.client() as client:
            api = gateway.Gateway(replace(CONFIG, agent_id=AGENT), client)
            with patch.object(gateway, "CHUNK_BYTES", 5):
                # Keep the real response limit large enough for JSON metadata.
                real_request = api.request

                async def request(command, body, **_):
                    return await real_request(command, body, response_limit=1024)

                with patch.object(api, "request", side_effect=request):
                    data, _ = await api.read_attachment(Attachment("out.txt", "/out/report.txt"), 100)
            self.assertEqual(data, b"report bytes")
            self.assertEqual([(body["offset"], body["length"]) for command, body in host.calls], [(0, 0), (0, 5), (5, 5), (10, 2)])
            host.calls.clear()
            with self.assertRaisesRegex(GrokError, "限制"):
                await api.read_attachment(Attachment("out.txt", "/out/report.txt"), 2)
            self.assertEqual(len(host.calls), 1)

    async def test_bad_chunks_do_not_escape_as_files(self):
        head = {"totalSize": 4, "bytesBase64": "", "mime": "text/plain"}
        for part in (None, {"totalSize": 5, "bytesBase64": "YWJjZA=="}, {"totalSize": 4, "bytesBase64": ""},
                     {"totalSize": 4, "bytesBase64": "!!!!"}, {"totalSize": 4, "bytesBase64": "YWJjZGU="}):
            with self.subTest(part=part):
                api = gateway.Gateway(replace(CONFIG, agent_id=AGENT), None)
                with patch.object(api, "request", AsyncMock(side_effect=[head, part])), self.assertRaises(GrokError):
                    await api.read_attachment(Attachment("out.txt", "/out/report.txt"), 100)

    async def test_partial_failure_total_budget_and_timeout_keep_completed_reply(self):
        api = gateway.Gateway(replace(CONFIG, agent_id=AGENT), None)
        reply = Reply("已完成正文", tuple(Attachment(f"{name}.txt", "/out/" + name) for name in ("bad", "good", "last")))
        with patch.object(api, "read_attachment", AsyncMock(side_effect=[GrokError("下载失败"), (b"123", "text/plain")])) as read, \
             patch.object(gateway, "MAX_REPLY_BYTES", 3):
            completed = await api.collect_reply(reply)
        self.assertEqual(completed.text, "已完成正文")
        self.assertEqual(completed.attachments[1].data, b"123")
        self.assertIn("50 MB", completed.attachments[2].error)
        self.assertEqual(read.await_count, 2)
        with patch.object(api, "read_attachment", AsyncMock(side_effect=asyncio.TimeoutError)):
            completed = await api.collect_reply(reply)
        self.assertEqual(completed.text, "已完成正文")
        self.assertTrue(all(item.data is None and "超时" in item.error for item in completed.attachments))

    async def test_svg_is_preserved_as_file_and_cancellation_propagates(self):
        api = gateway.Gateway(replace(CONFIG, agent_id=AGENT), None)
        reply = Reply(attachments=(Attachment("plot.svg", "/out/plot.svg", image=True),))
        with patch.object(api, "read_attachment", AsyncMock(return_value=(b"<svg/>", "image/svg+xml"))):
            result = await api.collect_reply(reply)
        self.assertEqual(result.attachments[0].data, b"<svg/>")
        self.assertFalse(result.attachments[0].image)
        with patch.object(api, "read_attachment", AsyncMock(side_effect=asyncio.CancelledError)), self.assertRaises(asyncio.CancelledError):
            await api.collect_reply(reply)

    async def test_cloud_paths_never_open_windows_local_files_and_gateway_responses_are_bounded(self):
        self.assertEqual(media.remote_path("file:///out/a%20b.txt"), "/out/a b.txt")
        for path in ("C:\\secret.txt", "file://server/share/secret.txt", "file:///C:/secret.txt?token=secret", "relative.txt", "/out/%00"):
            if path == "/out/%00":
                path = "file://" + path
            with self.subTest(path=path), self.assertRaises(GrokError):
                media.remote_path(path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1000))) as client:
            api = gateway.Gateway(CONFIG, client)
            with self.assertRaisesRegex(GrokError, "响应过大"):
                await api.request("readAttachmentChunk", {}, response_limit=100)
