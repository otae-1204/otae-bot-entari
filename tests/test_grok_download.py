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
        self.held: list[float] = []

    def host(self, size: int) -> ChunkHost:
        host = ChunkHost(payload(size))
        self.addAsyncCleanup(host.pooled.aclose)
        return host

    async def read(self, host: ChunkHost) -> tuple[bytes, str]:
        return await host.gateway().read_attachment(Attachment("立绘.png", PATH), media.MAX_FILE_BYTES, hold=self.held.append)

    def test_budget_follows_the_size_up_to_a_cap(self):
        self.assertEqual(gateway.download_seconds(0), 30)
        self.assertEqual(gateway.download_seconds(1), 55)
        self.assertEqual(gateway.download_seconds(CHUNK), 55)
        self.assertEqual(gateway.download_seconds(CHUNK + 1), 80)
        self.assertEqual(gateway.download_seconds(4 * CHUNK), 130)
        self.assertEqual(gateway.download_seconds(11 * CHUNK), 300)
        self.assertEqual(gateway.download_seconds(media.MAX_FILE_BYTES), gateway.ATTACHMENT_MAX_SECONDS)
        # Each chunk's share of the budget is under the point where a read counts as stalled.
        self.assertLess(gateway.ATTACHMENT_CHUNK_SECONDS, gateway.CHUNK_TIMEOUT)

    async def test_deadline_starts_at_the_base_and_moves_to_the_size_budget(self):
        host = self.host(4 * CHUNK)
        start = self.clock.now
        self.assertEqual(await self.read(host), (host.data, "image/png"))
        self.assertEqual(self.held, [start + 30, start + 130])

    async def test_file_is_read_in_small_chunks_and_each_is_logged(self):
        self.assertTrue(256 * 1024 <= CHUNK <= 512 * 1024)
        host = self.host(5 * CHUNK // 2)

        async def relay(index, offset, length):
            await self.clock.advance(15 * length / CHUNK)  # ~15 s per chunk at 23 KB/s.

        host.behave = relay
        with logs() as sink:
            data, _ = await self.read(host)
        self.assertEqual(data, host.data)
        self.assertEqual([call[1:3] for call in host.calls], [(0, 0), (0, CHUNK), (CHUNK, CHUNK), (2 * CHUNK, CHUNK // 2)])
        # A full chunk's JSON response stays inside the response limit.
        full = json.dumps({"totalSize": len(data), "mime": "image/png", "bytesBase64": base64.b64encode(data[:CHUNK]).decode()})
        self.assertLess(len(full), 2 * CHUNK)
        lines = [line for line in sink.getvalue().splitlines() if line.startswith("INFO [grok_bot] attachment")]
        ids = [call[3] for call in host.calls]
        self.assertEqual(lines, [
            f"INFO [grok_bot] attachment path={PATH} size={len(data)}B mime=image/png chunks=3 budget=105s",
            f"INFO [grok_bot] attachment path={PATH} chunk=1/3 offset=0 bytes={CHUNK} elapsed=15.00s request_id={ids[1]}",
            f"INFO [grok_bot] attachment path={PATH} chunk=2/3 offset={CHUNK} bytes={CHUNK} elapsed=15.00s request_id={ids[2]}",
            f"INFO [grok_bot] attachment path={PATH} chunk=3/3 offset={2 * CHUNK} bytes={CHUNK // 2} elapsed=7.50s request_id={ids[3]}",
            f"INFO [grok_bot] attachment path={PATH} done size={len(data)}B chunks=3 elapsed=37.50s backoff=0s",
        ])

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
        # The stalled 46 s count against the budget; the 1 s backoff does not.
        self.assertIn(f"chunk=2/2 offset={CHUNK} bytes={CHUNK} elapsed=47.00s", log)
        self.assertIn(f"done size={len(data)}B chunks=2 elapsed=47.00s backoff=1s", log)
        self.assertEqual(self.held[-1] - self.held[0], 80 - 30 + 1)

    async def test_exhausted_budget_logs_progress_and_tells_the_user(self):
        host = self.host(3_000_000)  # 12 chunks: the 300 s cap.

        async def slow(index, offset, length):
            if length:
                await self.clock.advance(40)  # Under CHUNK_TIMEOUT: slow, not stalled.

        host.behave = slow
        start = self.clock.now
        with logs() as sink:
            result = await host.gateway().collect_attachment(
                Attachment("立绘.png", PATH, image=True), gateway.DownloadBudget(hold=self.held.append))
        self.assertIsNone(result.data)
        self.assertEqual(result.error, "附件拉取超时（文件约 3.0 MB，传输速度过慢），请在 Grok Bot 客户端查阅。")
        self.assertEqual(self.held, [start + 30, start + 300])
        self.assertEqual(len(host.calls), 1 + 8)  # The size, seven chunks, and the eighth cut off at 300 s.
        warnings = [line for line in sink.getvalue().splitlines() if line.startswith("WARNING [grok_bot]")]
        self.assertEqual(warnings, [(
            f"WARNING [grok_bot] attachment path={PATH} budget exhausted received={7 * CHUNK}/3000000B chunks=7/12 "
            f"elapsed=320.00s budget=300s backoff=0s request_id={host.calls[-1][3]}"
        )])

    async def test_budget_runs_out_before_the_size_is_known(self):
        host = self.host(CHUNK)

        async def slow_head(index, offset, length):
            await self.clock.advance(gateway.ATTACHMENT_BASE_SECONDS + 5)

        host.behave = slow_head
        with logs() as sink:
            result = await host.gateway().collect_attachment(Attachment("立绘.png", PATH), gateway.DownloadBudget())
        self.assertEqual(result.error, gateway.DOWNLOAD_TIMEOUT)
        self.assertIn(f"WARNING [grok_bot] attachment path={PATH} budget exhausted received=0/?B chunks=0/? "
                      f"elapsed=35.00s budget=30s backoff=0s request_id={host.calls[0][3]}", sink.getvalue())

    async def test_retry_backoff_does_not_use_up_the_budget(self):
        host = self.host(1000)  # One chunk: a 55 s budget.

        async def flaky(index, offset, length):
            if length and index <= 1 + len(gateway.READ_RETRY_DELAYS):
                raise httpx.ConnectError("tailnet down")  # Five refused reads, 31 s of backoff.
            if length:
                await self.clock.advance(40)

        host.behave = flaky
        start = self.clock.now
        with logs() as sink:
            data, _ = await self.read(host)
        self.assertEqual(data, host.data)
        self.assertEqual(self.clock.slept, list(gateway.READ_RETRY_DELAYS))
        # 31 s of backoff and a 40 s read pass 55 s, but backoff stops the budget clock.
        self.assertEqual(self.held[-1], start + 55 + 31)
        self.assertIn("done size=1000B chunks=1 elapsed=71.00s backoff=31s", sink.getvalue())

    async def test_logs_never_contain_the_token(self):
        host = self.host(2 * CHUNK)

        async def broken(index, offset, length):
            if length:
                raise httpx.ReadError(f"{CONFIG.token} upstream-body")

        host.behave = broken
        with logs() as sink:
            result = await host.gateway().collect_attachment(Attachment("立绘.png", PATH), gateway.DownloadBudget())
        log = sink.getvalue()
        self.assertIn("readAttachmentChunk / ReadError", result.error)
        self.assertIn("cause=httpx.ReadError: <redacted> upstream-body", log)
        self.assertIn("Traceback", log)  # The final failure carries its call chain.
        self.assertIn(f"WARNING [grok_bot] attachment path={PATH} failed received=0/{2 * CHUNK}B chunks=0/2 ", log)
        self.assertEqual(len(host.calls), 1 + 1 + len(gateway.READ_RETRY_DELAYS))
        for text in (log, result.error):
            self.assertNotIn(CONFIG.token, text)
            self.assertNotIn("Authorization", text)
