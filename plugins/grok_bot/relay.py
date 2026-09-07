"""Forward completed text while a bounded worker downloads outgoing files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .config import GrokError
from .gateway import DownloadBudget, Gateway
from .media import MAX_REPLY_FILES, Attachment, Reply


class ReplyRelay:
    def __init__(self, gateway: Gateway, deliver: Callable[[Reply], Awaitable[None]]):
        self.gateway, self.deliver = gateway, deliver
        self.text = ""
        self.text_limit_reported = False
        self.file_limit_reported = False
        self.seen: set[tuple[str, str]] = set()
        self.files: asyncio.Queue[Attachment | None] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.budget = DownloadBudget()
        self.delivered = False
        self.pending_files = 0

    async def emit(self, reply: Reply) -> None:
        try:
            # File delivery can upload and send, each with its own 60s bound.
            await asyncio.wait_for(self.deliver(reply), 130)
            self.delivered = True
        except asyncio.TimeoutError:
            raise GrokError("QQ 回复发送未确认，请检查当前会话；本次未重复发送。") from None

    async def publish(self, snapshot: Reply) -> None:
        if self.worker is not None and self.worker.done():
            await self.worker
        # Completed outgoing messages append to the transcript. Refuse a
        # rewritten prefix rather than resend text or invent a matching answer.
        if not snapshot.text.startswith(self.text):
            raise GrokError("云端已发送的回复发生变化，后续结果请在 Grok Bot 应用中查看；未重复转发。")
        delta = snapshot.text[len(self.text):20000].strip()
        self.text = snapshot.text
        if len(self.text) > 20000 and not self.text_limit_reported:
            self.text_limit_reported = True
            delta += "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
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
                    await self.emit(Reply(f"本次附件超过 {MAX_REPLY_FILES} 个，其余附件请在 Grok Bot 应用中查看。"))
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
