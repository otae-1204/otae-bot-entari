"""Forward completed text while a bounded worker downloads outgoing files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .config import GrokError
from .gateway import DownloadBudget, Gateway
from .media import MAX_REPLY_FILES, Attachment, Reply

# File delivery can upload and send, each with its own 60s bound.
EMIT_TIMEOUT = 130


class ReplyRelay:
    def __init__(self, gateway: Gateway, deliver: Callable[[Reply], Awaitable[None]], *,
                 hold: Callable[[float], None] | None = None):
        self.gateway, self.deliver = gateway, deliver
        self.text = ""
        self.text_limit_reported = False
        self.file_limit_reported = False
        self.seen: set[tuple[str, str]] = set()
        self.files: asyncio.Queue[Attachment | None] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        # `hold` is told each attachment download deadline.
        self.budget = DownloadBudget(hold=hold)
        self.delivered = False
        self.pending_files = 0

    async def emit(self, reply: Reply) -> None:
        try:
            await asyncio.wait_for(self.deliver(reply), EMIT_TIMEOUT)
            self.delivered = True
        except asyncio.TimeoutError:
            raise GrokError("消息发送状态未确认，请检查当前会话接收情况；为避免刷屏本次未重复推送。") from None

    async def publish(self, snapshot: Reply) -> None:
        if self.worker is not None and self.worker.done():
            await self.worker
        # Completed outgoing messages append to the transcript. Refuse a
        # rewritten prefix rather than resend text or invent a matching answer.
        if not snapshot.text.startswith(self.text):
            raise GrokError("云端已推送的回复内容出现变更，为避免错乱后续请在 Grok Bot 客户端查阅；已停止重复转发。")
        delta = snapshot.text[len(self.text):20000].strip()
        self.text = snapshot.text
        if len(self.text) > 20000 and not self.text_limit_reported:
            self.text_limit_reported = True
            delta += "\n\n回复篇幅已超限，剩余内容请前往 Grok Bot 客户端查阅。"
        if delta:
            await self.emit(Reply(delta))
        for item in snapshot.attachments:
            key = (item.source, item.name)
            if key in self.seen:
                continue
            self.seen.add(key)
            if len(self.seen) > MAX_REPLY_FILES:
                if not self.file_limit_reported:
                    self.file_limit_reported = True
                    await self.emit(Reply(f"本次生成附件数量已超 {MAX_REPLY_FILES} 个上限，剩余附件请在 Grok Bot 客户端查阅。"))
                continue
            self.files.put_nowait(item)
            self.pending_files += 1
            if self.worker is None:
                self.worker = asyncio.create_task(self.forward_files())

    async def forward_files(self) -> None:
        while (item := await self.files.get()) is not None:
            try:
                completed = await self.gateway.collect_attachment(item, self.budget)
                # Text polling runs separately during download/upload.
                await self.emit(Reply(attachments=(completed,)))
            finally:
                self.pending_files -= 1

    async def finish(self) -> None:
        if self.worker is not None:
            self.files.put_nowait(None)
            await self.worker

    async def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
