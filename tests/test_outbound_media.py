"""出站媒体：data:/file: 资源按账号 features 上传（upload.create）或内联，绝不带本地路径出去。

Real ``EntariProtocol`` accounts (SendRequest posted by Entari itself); only the
HTTP call is faked. The hook is registered in a test scope through the module's
own publisher, exactly as ``install_outbound_media`` does in the root scope.
"""

from __future__ import annotations

import asyncio
import base64
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from arclet.entari import Audio, File, Image, MessageChain, Quote, Session, Text, Video
from arclet.entari.session import EntariProtocol
from arclet.letoderea import Scope, use
from arclet.letoderea.utils import _EventSystem
from satori import Api, Channel, ChannelType, Event, Guild, Login, MessageObject, User
from satori.client.account import Account, ApiInfo
from satori.exception import ServerException
from satori.parser import parse

from otae_bot.delivery import Target, deliver
from otae_bot.messaging.forward import make_forward
from otae_bot.pipeline import outbound_media

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
PNG_B64 = base64.b64encode(PNG).decode()
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(30))


class FakeProtocol(EntariProtocol):
    """A native Satori endpoint: records calls, answers upload.create like a server would."""

    def __init__(self, account):
        super().__init__(account)
        self.calls: list[tuple[str, dict]] = []
        self.upload: object = None  # None: answer every field; an exception: raise it; a dict: return it
        self.upload_gate: asyncio.Event | None = None

    async def call_api(self, action, params=None, multipart=False, method="POST"):
        name = str(getattr(action, "value", action))
        self.calls.append((name, dict(params or {})))
        if name == Api.UPLOAD_CREATE.value:
            assert multipart, "upload.create must be multipart"
            if self.upload_gate is not None:
                await self.upload_gate.wait()
            if isinstance(self.upload, BaseException):
                raise self.upload
            if isinstance(self.upload, dict):
                return self.upload
            return {key: f"internal:ir_web/bot/upload/{key}" for key, value in params.items() if isinstance(value, dict)}
        if name == Api.MESSAGE_CREATE.value:
            return [{"id": f"sent-{len(self.calls)}", "content": params["content"]}]
        if name == Api.USER_CHANNEL_CREATE.value:
            return {"id": "private:" + params["user_id"], "type": ChannelType.DIRECT.value}
        raise AssertionError(f"unexpected API call {name}")

    # Properties, not methods: EntariProtocol wraps every method as an API call.
    @property
    def sent(self) -> list[str]:
        return [params["content"] for action, params in self.calls if action == Api.MESSAGE_CREATE.value]

    @property
    def uploads(self) -> list[dict]:
        return [params for action, params in self.calls if action == Api.UPLOAD_CREATE.value]


def media_srcs(content: str) -> list[tuple[str, str]]:
    found = []

    def walk(elements):
        for element in elements:
            if element.type in ("img", "audio", "video", "file"):
                found.append((element.type, element.attrs.get("src")))
            walk(element.children)

    walk(parse(content))
    return found


class OutboundMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Entari pins letoderea's tasks to its own loop at import; this test runs on another.
        loop_patch = mock.patch.object(_EventSystem, "loop", asyncio.get_running_loop())
        loop_patch.start()
        self.addCleanup(loop_patch.stop)
        self.scope = Scope.of("test-outbound-media")
        self.addAsyncCleanup(self.scope.cleanup)
        with self.scope.context():
            use(outbound_media.publisher, outbound_media.rewrite_outbound_media, priority=0)
        patcher = mock.patch.object(outbound_media, "logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def account(self, *features: str) -> Account:
        login = Login(user=User("bot"), platform="ir_web", features=list(features))
        account = Account(login, ApiInfo(port=5500, token="t"), [], FakeProtocol)
        self.addAsyncCleanup(account.protocol.session.close)
        return account

    def uploader(self) -> Account:
        return self.account("message.create", "upload.create")

    # ── without upload.create ──

    async def test_without_upload_feature_data_urls_stay_inline(self):
        account = self.account("message.create")
        await account.protocol.send_message("g1", MessageChain([Text("看图 "), Image.of(raw=PNG)]))
        self.assertEqual(account.protocol.uploads, [])
        (content,) = account.protocol.sent
        self.assertEqual(content, f'看图 <img src="data:image/png;base64,{PNG_B64}"/>')
        self.log.warning.assert_not_called()

    async def test_without_upload_feature_inline_data_is_not_even_decoded(self):
        message = MessageChain([Image.of(raw=PNG)])
        with mock.patch.object(outbound_media, "_decode", side_effect=AssertionError("decoded")):
            self.assertIsNone(await outbound_media.prepare(self.account(), "g1", message))

    async def test_file_urls_become_data_urls_without_upload(self):
        account = self.account()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "card.png"
            path.write_bytes(PNG)
            await account.protocol.send_message("g1", MessageChain([Image(src=path.as_uri(), width=320)]))
        (content,) = account.protocol.sent
        self.assertEqual(content, f'<img src="data:image/png;base64,{PNG_B64}" width="320"/>')
        self.assertNotIn("file:", content)

    # ── with upload.create ──

    async def test_upload_replaces_src_and_passes_the_channel(self):
        account = self.uploader()
        await account.protocol.send_message("g1", MessageChain([Text("前"), Image.of(raw=PNG), Text("后")]))
        (upload,) = account.protocol.uploads
        self.assertEqual(upload["channel_id"], "g1")
        self.assertEqual(upload["file0"], {"value": PNG, "filename": "img-0.png", "content_type": "image/png"})
        self.assertEqual(set(upload), {"channel_id", "file0"})
        (content,) = account.protocol.sent
        self.assertEqual(content, '前<img src="internal:ir_web/bot/upload/file0"/>后')
        # upload first, then the one send
        self.assertEqual([name for name, _ in account.protocol.calls], ["upload.create", "message.create"])

    async def test_nested_forward_and_quote_are_uploaded_and_deduplicated(self):
        account = self.uploader()
        forward = make_forward([Image.of(raw=PNG), [Text("第二页"), Image.of(raw=JPEG, mime="image/jpeg")],
                                Image.of(raw=PNG)], name="Bot", uin="bot")
        quote = Quote("q1")(Image.of(raw=JPEG, mime="image/jpeg"))
        await account.protocol.send_message("g1", MessageChain([quote, forward]))
        (upload,) = account.protocol.uploads
        self.assertEqual(sorted(k for k in upload if k != "channel_id"), ["file0", "file1"])
        payloads = {upload[k]["value"] for k in ("file0", "file1")}
        self.assertEqual(payloads, {PNG, JPEG})
        (content,) = account.protocol.sent
        srcs = [src for _, src in media_srcs(content)]
        self.assertEqual(len(srcs), 4)
        self.assertTrue(all(src.startswith("internal:ir_web/bot/upload/file") for src in srcs), srcs)
        jpeg_key = "file0" if upload["file0"]["value"] == JPEG else "file1"
        png_key = "file1" if jpeg_key == "file0" else "file0"
        self.assertEqual(srcs, [f"internal:ir_web/bot/upload/{k}" for k in (jpeg_key, png_key, jpeg_key, png_key)])
        self.assertNotIn("data:", content)
        self.assertIn("第二页", content)
        self.assertTrue(content.startswith('<quote id="q1">'), content)

    async def test_attributes_are_preserved(self):
        account = self.uploader()
        message = MessageChain([
            Image.of(raw=PNG, name="radar.png", width=640, height=480),
            File.of(raw=b"hello", mime="text/plain", name="notes.txt"),
            Audio.of(raw=b"ID3audio", mime="audio/mpeg", duration=3.5),
            Video.of(raw=b"\x00\x00\x00\x18ftypmp42", mime="video/mp4", width=10),
        ])
        await account.protocol.send_message("g1", message)
        (upload,) = account.protocol.uploads
        self.assertEqual(upload["file1"]["filename"], "notes.txt")
        self.assertEqual(upload["file1"]["content_type"], "text/plain")
        (content,) = account.protocol.sent
        elements = parse(content)
        self.assertEqual([e.type for e in elements], ["img", "file", "audio", "video"])
        self.assertEqual(elements[0].attrs, {"src": "internal:ir_web/bot/upload/file0", "title": "radar.png",
                                             "width": "640", "height": "480"})
        self.assertEqual(elements[1].attrs, {"src": "internal:ir_web/bot/upload/file1", "title": "notes.txt"})
        self.assertEqual(elements[2].attrs["duration"], "3.5")
        self.assertEqual(elements[3].attrs["width"], "10")

    async def test_http_and_internal_sources_are_left_alone(self):
        account = self.uploader()
        content = '<img src="https://example.com/a.png"/><img src="internal:ir_web/bot/upload/x"/>'
        await account.protocol.send_message("g1", MessageChain([Image(src="https://example.com/a.png"),
                                                                Image(src="internal:ir_web/bot/upload/x")]))
        self.assertEqual(account.protocol.uploads, [])
        self.assertEqual(account.protocol.sent, [content])

    async def test_file_urls_are_uploaded_with_their_name(self):
        account = self.uploader()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "help main.png"
            path.write_bytes(PNG)
            await account.protocol.send_message("g1", MessageChain([Image(src=path.as_uri())]))
        (upload,) = account.protocol.uploads
        self.assertEqual(upload["file0"], {"value": PNG, "filename": "help main.png", "content_type": "image/png"})
        self.assertNotIn("file:", account.protocol.sent[0])

    async def test_caller_elements_are_not_mutated(self):
        account = self.uploader()
        image = Image.of(raw=PNG)
        forward = make_forward([image])
        message = MessageChain([forward])
        before = str(message)
        await account.protocol.send_message("g1", message)
        self.assertEqual(str(message), before)
        self.assertTrue(image.src.startswith("data:"))

    # ── failures ──

    async def test_upload_failure_sends_inline_once_with_one_warning(self):
        account = self.uploader()
        account.protocol.upload = ServerException("boom " + "A" * 500)
        await account.protocol.send_message("g1", MessageChain([Image.of(raw=PNG), Image.of(raw=PNG)]))
        self.assertEqual(len(account.protocol.uploads), 1)
        (content,) = account.protocol.sent
        self.assertEqual(content, f'<img src="data:image/png;base64,{PNG_B64}"/>' * 2)
        self.log.warning.assert_called_once()
        self.assertNotIn("AAAA", self.log.warning.call_args.args[0])

    async def test_failed_upload_of_a_file_url_still_leaves_no_path(self):
        account = self.uploader()
        account.protocol.upload = ServerException("down")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.png"
            path.write_bytes(PNG)
            await account.protocol.send_message("g1", MessageChain([Image(src=path.as_uri())]))
        self.assertEqual(account.protocol.sent, [f'<img src="data:image/png;base64,{PNG_B64}"/>'])

    async def test_partial_upload_answer_inlines_the_rest(self):
        account = self.uploader()
        account.protocol.upload = {"file0": "internal:ir_web/bot/upload/one"}
        await account.protocol.send_message("g1", MessageChain([Image.of(raw=PNG), Image.of(raw=JPEG, mime="image/jpeg")]))
        (content,) = account.protocol.sent
        self.assertEqual(media_srcs(content), [
            ("img", "internal:ir_web/bot/upload/one"),
            ("img", "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()),
        ])
        self.log.warning.assert_called_once()

    async def test_cancellation_propagates_and_nothing_is_sent(self):
        account = self.uploader()
        account.protocol.upload_gate = asyncio.Event()
        task = asyncio.create_task(account.protocol.send_message("g1", MessageChain([Image.of(raw=PNG)])))
        for _ in range(50):
            await asyncio.sleep(0)
            if account.protocol.uploads:
                break
        self.assertEqual(len(account.protocol.uploads), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(account.protocol.sent, [])

    # ── every send path ──

    async def test_private_message_uploads_for_the_private_channel(self):
        account = self.uploader()
        await account.protocol.send_private_message("u1", MessageChain([Image.of(raw=PNG)]))
        (upload,) = account.protocol.uploads
        self.assertEqual(upload["channel_id"], "private:u1")
        self.assertEqual(account.protocol.sent, ['<img src="internal:ir_web/bot/upload/file0"/>'])

    async def test_session_send_is_covered(self):
        account = self.uploader()
        origin = Event("message-created", datetime.now(timezone.utc), Login(user=User("bot"), platform="ir_web"),
                       channel=Channel("g9", ChannelType.TEXT), guild=Guild("g9"), user=User("u"),
                       message=MessageObject("m1", "hi"))
        await Session(account, origin).send(MessageChain([Image.of(raw=PNG)]))
        (upload,) = account.protocol.uploads
        self.assertEqual(upload["channel_id"], "g9")
        self.assertEqual(account.protocol.sent, ['<img src="internal:ir_web/bot/upload/file0"/>'])

    async def test_deliver_is_covered_for_groups_and_private_chats(self):
        account = self.uploader()
        group = await deliver(Target.group("g2"), MessageChain([Image.of(raw=PNG)]), account=account, plugin="")
        private = await deliver(Target.private("u2"), MessageChain([Image.of(raw=PNG)]), account=account)
        self.assertTrue(group.sent and private.sent, (group, private))
        self.assertEqual([u["channel_id"] for u in account.protocol.uploads], ["g2", "private:u2"])
        self.assertEqual(account.protocol.sent, ['<img src="internal:ir_web/bot/upload/file0"/>'] * 2)
        # no channel.get or other lookup per proactive send
        self.assertEqual({name for name, _ in account.protocol.calls},
                         {"upload.create", "message.create", "user.channel.create"})


class InstallTests(unittest.TestCase):
    def test_install_is_idempotent(self):
        with mock.patch("arclet.letoderea.use") as use_, mock.patch.object(outbound_media, "_installed", False):
            outbound_media.install_outbound_media()
            outbound_media.install_outbound_media()
        use_.assert_called_once_with(outbound_media.publisher, outbound_media.rewrite_outbound_media, priority=0)


if __name__ == "__main__":
    unittest.main()


IR_SCENARIOS = r"""
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arclet.entari import Image, MessageChain
from satori.parser import parse

from otae_bot.delivery import Target, deliver

PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(40))
help_plugin = plugin('plugins.help_plugin.handlers')
card = Path('help-card.png').resolve()
card.write_bytes(PNG)

# An Ir-like native Satori endpoint: not QQ, not on this machine, declares upload.create.
ir = make_account('ir-bot', platform='ir_web', features=['message.create', 'upload.create'])
stored = {}

def upload(params):
    urls = {}
    for key, value in params.items():
        if isinstance(value, dict):
            url = 'internal:ir_web/ir-bot/upload/' + str(len(stored))
            stored[url] = value['value']
            urls[key] = url
    return urls

ir.protocol.answers['upload.create'] = upload


def media(content):
    found = []
    def walk(elements):
        for element in elements:
            if element.type in ('img', 'audio', 'video', 'file'):
                found.append(element.attrs['src'])
            walk(element.children)
    walk(parse(content))
    return found


async def main():
    # a reply in a command handler (session.send)
    with patch.object(help_plugin, 'cached_help_image', AsyncMock(return_value=card)):
        sent = await say('/help', account=ir, group='ir-channel')
    assert len(sent) == 1, sent
    (src,) = media(sent[0])
    assert src.startswith('internal:ir_web/ir-bot/upload/') and stored[src] == PNG, (src, stored)
    assert 'data:' not in sent[0] and 'file:' not in sent[0], sent[0]
    names = [name for name, _ in ir.protocol.calls]
    assert names == ['upload.create', 'message.create'], names
    (upload_params,) = [p for n, p in ir.protocol.calls if n == 'upload.create']
    assert upload_params['channel_id'] == 'ir-channel', upload_params
    print('SESSION-OK')

    # a proactive message (deliver)
    before = len(ir.protocol.calls)
    result = await deliver(Target.group('ir-channel'), MessageChain([Image.of(raw=PNG)]), account=ir, plugin='')
    assert result.sent, result
    calls = ir.protocol.calls[before:]
    assert [n for n, _ in calls] == ['upload.create', 'message.create'], calls
    content = calls[-1][1]['content']
    (src,) = media(content)
    assert stored[src] == PNG and 'data:' not in content and 'file:' not in content, content
    print('DELIVER-OK')

    # the QQ-side default account (no upload.create) still gets the image inline
    with patch.object(help_plugin, 'cached_help_image', AsyncMock(return_value=card)):
        sent = await say('/help')
    assert len(sent) == 1 and media(sent[0])[0].startswith('data:image/png;base64,'), sent
    assert not [n for n, _ in default_account.protocol.calls if n == 'upload.create']
    print('ALL-OK')

run(main())
"""


class IrLikeEndpointTests(unittest.TestCase):
    """The whole app (create_app, every plugin) against an in-test native Satori endpoint."""

    def test_media_reach_a_remote_satori_endpoint_as_uploads(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(IR_SCENARIOS)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-6000:])
        self.assertIn("SESSION-OK", result.stdout)
        self.assertIn("DELIVER-OK", result.stdout)
        self.assertIn("ALL-OK", result.stdout)
