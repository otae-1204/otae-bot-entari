"""Cloud attachment downloads over a slow tailnet path, on a fake clock."""

from __future__ import annotations

import base64
import io
import json
import unittest
from contextlib import contextmanager

import httpx
from loguru import logger

from plugins.grok_bot import gateway, media
from plugins.grok_bot.config import GrokConfig
from plugins.grok_bot.media import Attachment
from tests.support.clock import FakeClock

AGENT = "00000000-0000-4000-8000-000000000001"
CONFIG = GrokConfig(base_url="http://grok.test:1340", token="private-token", agent_id=AGENT, poll_interval=0)
PATH = "/out/立绘.png"
CHUNK = gateway.CHUNK_BYTES


def payload(size: int) -> bytes:
    return (bytes(range(256)) * (size // 256 + 1))[:size]


@contextmanager
def logs():
    sink = io.StringIO()
    handler = logger.add(sink, level="DEBUG", format="{level} {message}")
    try:
        yield sink
    finally:
        logger.remove(handler)


class ChunkHost:
    """readAttachmentChunk over a pooled client and one-off clients.

    `behave(index, offset, length)` runs before each answer (1-based call index):
    it may advance the clock to model transfer time, or raise.
    """

    def __init__(self, data: bytes):
        self.data = data
        self.calls: list[tuple[str, int, int, str]] = []  # connection, offset, length, x-sand-request-id
        self.opened: list[httpx.AsyncClient] = []
        self.behave = None
        self.pooled = self.client("pooled")

    def client(self, label: str) -> httpx.AsyncClient:
        async def respond(request):
            body = json.loads(request.content)
            self.calls.append((label, body["offset"], body["length"], request.headers["x-sand-request-id"]))
            if self.behave is not None:
                await self.behave(len(self.calls), body["offset"], body["length"])
            part = self.data[body["offset"]:body["offset"] + body["length"]]
            return httpx.Response(200, json={"totalSize": len(self.data), "mime": "image/png",
                                             "bytesBase64": base64.b64encode(part).decode()})
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    def connect(self) -> httpx.AsyncClient:
        self.opened.append(self.client(f"new-{len(self.opened) + 1}"))
        return self.opened[-1]

    def gateway(self) -> gateway.Gateway:
        return gateway.Gateway(CONFIG, self.pooled, connect=self.connect)


class AttachmentDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock(self)

    def host(self, size: int) -> ChunkHost:
        host = ChunkHost(payload(size))
        self.addAsyncCleanup(host.pooled.aclose)
        return host

    async def read(self, host: ChunkHost) -> tuple[bytes, str]:
        return await host.gateway().read_attachment(Attachment("立绘.png", PATH), media.MAX_FILE_BYTES)

    async def test_file_is_read_in_small_chunks(self):
        self.assertTrue(256 * 1024 <= CHUNK <= 512 * 1024)
        host = self.host(5 * CHUNK // 2)
        data, _ = await self.read(host)
        self.assertEqual(data, host.data)
        self.assertEqual([call[1:3] for call in host.calls], [(0, 0), (0, CHUNK), (CHUNK, CHUNK), (2 * CHUNK, CHUNK // 2)])
        # A full chunk's JSON response stays inside the response limit.
        full = json.dumps({"totalSize": len(data), "mime": "image/png", "bytesBase64": base64.b64encode(data[:CHUNK]).decode()})
        self.assertLess(len(full), 2 * CHUNK)

    async def test_stalled_chunk_is_retried_on_a_new_connection(self):
        host = self.host(2 * CHUNK)

        async def stall_once(index, offset, length):
            if index == 3:  # The first read of the second chunk never finishes.
                while True:
                    await self.clock.advance(gateway.CHUNK_TIMEOUT + 1)

        host.behave = stall_once
        with logs() as sink:
            data, _ = await self.read(host)
        self.assertEqual(data, host.data)
        self.assertEqual([call[:3] for call in host.calls],
                         [("pooled", 0, 0), ("pooled", 0, CHUNK), ("pooled", CHUNK, CHUNK), ("new-1", CHUNK, CHUNK)])
        self.assertEqual(self.clock.slept, [gateway.READ_RETRY_DELAYS[0]])
        self.assertTrue(all(client.is_closed for client in host.opened))
        log = sink.getvalue()
        self.assertIn("WARNING [grok_bot] gateway command=readAttachmentChunk attempt=1/6 ", log)
        self.assertIn(f"request_id={host.calls[2][3]} connection=pooled retry=True "
                      "error=网关接口请求超时（readAttachmentChunk / ReadTimeout）", log)
        self.assertIn("cause=httpx.ReadTimeout: no complete response within 45s", log)
        self.assertIn("INFO [grok_bot] gateway command=readAttachmentChunk attempt=2/6 ", log)
        self.assertIn(f"request_id={host.calls[3][3]} connection=new ok", log)
