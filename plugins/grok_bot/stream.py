"""One receiver per QQ conversation, with independently submitted follow-ups."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from loguru import logger

from otae_bot.group_features import feature_store

from . import conversations
from .config import GatewayError, GrokConfig, GrokError
from .conversations import ConversationScope
from .gateway import Gateway, reply_from
from .media import Reply, input_images
from .relay import EMIT_TIMEOUT, ReplyRelay


@dataclass
class Input:
    prompt: str
    images: tuple[str, ...]
    account: object
    deliver: Callable[..., Awaitable[None]]
    done: asyncio.Future
    deadline: float


@dataclass
class Submission:
    item: Input
    nonce: str
    status: str = "submitting"

    @property
    def marker(self) -> str:
        return f"[QQ请求编号:{self.nonce}]"


class LiveConversation:
    def __init__(self, hub: RequestQueue, config: GrokConfig, scope: ConversationScope, first: Input):
        self.hub, self.config, self.scope, self.first = hub, config, scope, first
        self.inputs: asyncio.Queue[Input] = asyncio.Queue()
        self.submissions: list[Submission] = []
        self.preparing = False
        self.external_input = False
        self.revision = 0
        self.closing = False
        self.task: asyncio.Task | None = None
        self.relay: ReplyRelay | None = None
        self.gateway: Gateway | None = None
        self.deadline: asyncio.Timeout | None = None

    def enqueue(self, item: Input) -> None:
        self.inputs.put_nowait(item)
        self.revision += 1

    def can_quote(self) -> bool:
        return len(self.submissions) == 1 and not self.external_input

    async def emit(self, reply: Reply) -> None:
        owner = self.submissions[0].item if self.submissions else self.first
        # Evaluate the policy at actual QQ send time, including after an upload.
        await owner.deliver(reply, reply_to=self.can_quote)

    def hold(self, until: float) -> None:
        """A started attachment download gets until its own deadline, plus delivery, past GROKBOT_TIMEOUT."""
        until += EMIT_TIMEOUT
        if self.deadline is not None and not self.deadline.expired() and until > self.deadline.when():
            self.deadline.reschedule(until)

    async def run(self) -> None:
        acquired = False
        try:
            async def acquire():
                nonlocal acquired
                async with self.hub.capacity:
                    await self.hub.capacity.wait_for(lambda: self.hub.active < self.config.max_concurrent)
                    self.hub.active += 1
                    acquired = True

            await asyncio.wait_for(acquire(), self.config.timeout)
            async with asyncio.timeout(self.config.timeout) as self.deadline:
                await self.work()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Background errors must be delivered and redacted.
            self.closing = True
            if isinstance(error, asyncio.TimeoutError):
                message = (f"本轮响应等待已达 {self.config.timeout:g} 秒上限，云端生成可能仍在继续，后续内容请在 Grok Bot 客户端查阅。"
                           if acquired else "排队等待会话名额超时，本次内容尚未提交。")
            elif isinstance(error, GrokError):
                message = str(error)
            else:
                logger.warning("[grok_bot] conversation receiver failed: {}", type(error).__name__)
                message = "会话消息接收中断，请管理员查看运行日志；已提交的任务可能仍在云端处理中。"
            if self.submissions:
                try:
                    await asyncio.wait_for(self.emit(Reply(message)), EMIT_TIMEOUT)
                except Exception as delivery_error:  # noqa: BLE001 - Do not retry an ambiguous QQ send.
                    logger.warning("[grok_bot] receiver diagnostic delivery failed: {}", type(delivery_error).__name__)
            else:
                self.fail_waiting(message)
        finally:
            self.closing = True
            self.deadline = None
            self.fail_waiting("当前接收流程已终止，本次提问尚未提交成功，请重新发送。")
            if acquired:
                async with self.hub.capacity:
                    self.hub.active -= 1
                    self.hub.capacity.notify_all()
            if self.hub.slots.get(self.scope.key) is self:
                del self.hub.slots[self.scope.key]

    def fail_waiting(self, message: str) -> None:
        while not self.inputs.empty():
            item = self.inputs.get_nowait()
            self.hub.pending -= 1
            if not item.done.done():
                item.done.set_exception(GrokError(message))

    async def send_inputs(self, client) -> None:
        while True:
            item = await self.inputs.get()
            self.preparing = True
            try:
                if item.done.cancelled():
                    continue
                remaining = item.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GrokError("请求排队等待超时，本次提问尚未发出。")
                await asyncio.wait_for(self.send_one(client, item), remaining)
                if not item.done.done():
                    item.done.set_result(Reply())
            except asyncio.CancelledError:
                if not item.done.done():
                    item.done.set_exception(GrokError("提问提交过程已终止，请在 Grok Bot 客户端确认接收状态；本次未重复推送。"))
                raise
            except Exception as error:  # noqa: BLE001 - Preserve the receiver after one failed input.
                if isinstance(error, asyncio.TimeoutError):
                    error = GrokError("请求提交等待超时，请在 Grok Bot 客户端核实是否已接收；本次未重复推送。")
                elif not isinstance(error, GrokError):
                    logger.warning("[grok_bot] input submission failed: {}", type(error).__name__)
                    error = GrokError("本次输入处理遇到异常，请管理员查阅运行日志。")
                if not item.done.done():
                    # Do not share a live worker's traceback with the caller.
                    item.done.set_exception(GrokError(str(error)))
            finally:
                self.hub.pending -= 1
                self.preparing = False
                self.revision += 1

    async def send_one(self, client, item: Input) -> None:
        self.require_enabled()
        prepared = await input_images(item.images, item.account) if item.images else ()
        if item.done.cancelled():
            return
        if self.gateway is None:
            connect = conversations.make_client
            agent_id = await conversations.resolve_agent(Gateway(self.config, client, connect=connect), self.scope,
                                                         conversations.session_store)
            gateway = Gateway(replace(self.config, agent_id=agent_id), client, connect=connect)
            _, busy = await gateway.state()
            self.external_input = busy  # Pre-existing cloud work has no reliable QQ owner.
            self.gateway = gateway
            self.relay = ReplyRelay(gateway, self.emit, hold=self.hold)
        gateway = self.gateway
        if item.done.cancelled():
            return
        uploaded = await gateway.upload_images(prepared) if prepared else ()
        self.require_enabled()
        if item.done.cancelled():
            return
        submission = Submission(item, str(uuid4()))
        self.submissions.append(submission)
        try:
            await gateway.submit(item.prompt, submission.nonce, uploaded)
        except GatewayError as error:
            if error.not_submitted:
                self.submissions.remove(submission)
            raise
        finally:
            # A lost send response is not proof of rejection. Keep its nonce for
            # read-only reconciliation and never resubmit the prompt.
            submission.status = "pending"

    def require_enabled(self) -> None:
        if not feature_store.is_enabled(self.scope.feature_scope, "grok_bot"):
            raise GrokError("当前会话的 Grok Bot 服务已停用，提问尚未发送。")

    async def work(self) -> None:
        self.require_enabled()
        async with conversations.make_client() as client:
            sender = asyncio.create_task(self.send_inputs(client))
            try:
                while self.gateway is None:
                    if not self.preparing and self.inputs.empty():
                        self.closing = True
                        return
                    await asyncio.sleep(self.config.poll_interval)
                await self.receive(self.gateway)
                if self.relay is not None:
                    await self.relay.finish()
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                if self.relay is not None:
                    await self.relay.cancel()

    async def receive(self, gateway: Gateway) -> None:
        previous = None
        while True:
            if not feature_store.is_enabled(self.scope.feature_scope, "grok_bot"):
                raise GrokError("当前会话的 Grok Bot 服务已停用，接收流程已中止；已提交的任务可能仍在云端处理。")
            revision = self.revision
            for submission in list(self.submissions):
                if submission.status in {"submitting", "accepted"}:
                    continue
                submission.status = await gateway.acceptance(submission.nonce)
                if submission.status == "rejected":
                    self.submissions.remove(submission)
                    await submission.item.deliver(Reply("Grok Bot 未接受本次输入请求，详情请在客户端查阅。"), reply_to=True)
            if not self.submissions:
                if not self.preparing and self.inputs.empty():
                    self.closing = True
                    return
            elif self.submissions[0].status in {"accepted", "unknown-durability"}:
                anchor = self.submissions[0].marker
                entries = await gateway.transcript_entries(anchor)
                anchor_index = next((i for i, row in enumerate(entries) if row.get("role") == "user" and anchor in str(row.get("content", ""))), None)
                if anchor_index is not None:
                    users = [row for row in entries[anchor_index:] if row.get("role") == "user"]
                    if any(not any(sub.marker in str(row.get("content", "")) for sub in self.submissions) for row in users):
                        self.external_input = True
                    outgoing = reply_from(entries, anchor, include_assistant=False, limit_attachments=False, allow_intervening=True)
                    if outgoing:
                        await self.relay.publish(outgoing)
                    _, busy = await gateway.state()
                    pending = any(sub.status not in {"accepted", "unknown-durability"} for sub in self.submissions)
                    # Every submitted input must be present before closing the receiver.
                    anchored = all(any(sub.marker in str(row.get("content", "")) for row in users) for sub in self.submissions)
                    reply = reply_from(entries, anchor, include_assistant=self.can_quote(), limit_attachments=False, allow_intervening=True)
                    last_user = max(i for i, row in enumerate(entries) if row.get("role") == "user")
                    tail = entries[last_user:]
                    latest_reply = reply_from(tail, str(tail[0].get("content", "")), include_assistant=self.can_quote(),
                                              limit_attachments=False, allow_intervening=True)
                    snapshot = (revision, len(users), reply)
                    if not busy and not pending and anchored and not self.preparing and self.inputs.empty() and revision == self.revision:
                        # An old answer cannot complete a newly accepted follow-up,
                        # even when the host briefly reports idle before starting it.
                        if reply and latest_reply and snapshot == previous:
                            await self.relay.publish(reply)
                            if not self.relay.pending_files and revision == self.revision and self.inputs.empty() and not self.preparing:
                                self.closing = True
                                return
                        previous = snapshot
                    else:
                        previous = None
            await asyncio.sleep(self.config.poll_interval)


class RequestQueue:
    """Bound concurrent receivers; serialize uploads/sends, not cloud tasks."""

    def __init__(self):
        self.slots: dict[str, LiveConversation] = {}
        self.repairs: dict[str, asyncio.Task] = {}
        self.capacity = asyncio.Condition()
        self.active = 0
        self.pending = 0
        self.closed = False

    async def run(self, config: GrokConfig, prompt: str, progress: Callable[[str], Awaitable[None]], scope: ConversationScope,
                  *, repair_only: bool = False, images: tuple[str, ...] = (), account=None,
                  on_reply: Callable[..., Awaitable[None]] | None = None) -> Reply | str:
        if self.closed:
            raise GrokError("Grok Bot 模块正在停止服务，请稍候重试。")
        if repair_only:
            if scope.key in self.slots or scope.key in self.repairs:
                raise GrokError("当前会话正处于回复接收或修复处理中，请等待完成后再试。")
            task = self.repairs[scope.key] = asyncio.create_task(conversations.repair(config, scope))
            try:
                return await task
            finally:
                self.repairs.pop(scope.key, None)
        if on_reply is None:
            raise GrokError("缺少可用应答通道，无法完成回复推送。")
        if self.pending >= config.max_pending:
            raise GrokError("当前并发请求队列已达上限，请稍候再试。")
        self.pending += 1
        enqueued = False
        item = Input(prompt, images, account, on_reply, asyncio.get_running_loop().create_future(),
                     asyncio.get_running_loop().time() + config.timeout)
        try:
            if scope.key in self.repairs:
                await asyncio.wait_for(asyncio.shield(self.repairs[scope.key]), config.timeout)
            while (live := self.slots.get(scope.key)) is not None and live.closing:
                await asyncio.wait_for(asyncio.shield(live.task), config.timeout)
            if self.closed:
                raise GrokError("Grok Bot 模块正在停止服务，请稍候重试。")
            if live is None:
                live = self.slots[scope.key] = LiveConversation(self, config, scope, item)
                live.enqueue(item)
                live.task = asyncio.create_task(live.run())
            else:
                live.enqueue(item)
            enqueued = True
            if self.active >= config.max_concurrent and live.relay is None:
                await progress("当前正忙于处理其他会话请求，本次输入已进入等待队列。")
            return await asyncio.wait_for(asyncio.shield(item.done), max(.001, item.deadline - asyncio.get_running_loop().time()))
        except asyncio.TimeoutError:
            raise GrokError("请求提交等待超时，请在 Grok Bot 客户端核对接收情况；本次未重复推送。") from None
        finally:
            if not item.done.done():
                item.done.cancel()
            elif not item.done.cancelled():
                item.done.exception()  # Also consume a raced failure if the caller was cancelled.
            if not enqueued:
                self.pending -= 1

    async def close(self) -> None:
        self.closed = True
        tasks = [live.task for live in self.slots.values()] + list(self.repairs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step never enters run()'s finally.
        for live in self.slots.values():
            live.fail_waiting("Grok Bot 服务已终止，本次提问未提交。")
        self.slots.clear()
