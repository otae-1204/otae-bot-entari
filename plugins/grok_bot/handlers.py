from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from arclet.alconna import Alconna, AllParam, Args, Arparma
from arclet.entari import MessageChain, Session, command
from loguru import logger
from satori import Image, Text

from otae_bot.group_features import feature_store
from otae_bot.permissions import is_superuser

from .config import GrokConfig, GrokError
from .conversations import ConversationScope, ask, scope_from_session

HELP = """Grok Bot 问答
/grok 问题：发送文字问题；引用消息后 /grok 问题可附上引用正文。
别名：/grokbot。同群共用本群对话，不同群与私聊分别独立，默认花园多惠人设。
支持同一用户连续提交；同会话排队，不同会话可并行处理。
默认关闭：SuperUser 在目标群执行 /功能 开启 grok；管理员和群主可关闭。
私聊需 SuperUser 用 /grok 开启 或 /grok 关闭 管理自己的私聊开关。
需要人工确认时，请管理员在 Grok Bot 应用中处理。"""


@dataclass
class SessionSlot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class RequestQueue:
    """Serialize each conversation before acquiring shared execution capacity."""

    def __init__(self):
        self.slots: dict[str, SessionSlot] = {}
        self.capacity = asyncio.Condition()
        self.active = 0
        self.pending = 0

    async def run(self, config: GrokConfig, prompt: str, progress: Callable[[str], Awaitable[None]], scope: ConversationScope) -> str:
        if self.pending >= config.max_pending:
            raise GrokError("Grok Bot 等待队列已满，请稍后重试。")
        slot = self.slots.setdefault(scope.key, SessionSlot())
        slot.users += 1
        self.pending += 1
        acquired = running = False

        async def acquire():
            nonlocal acquired, running
            await slot.lock.acquire()
            acquired = True
            async with self.capacity:
                await self.capacity.wait_for(lambda: self.active < config.max_concurrent)
                self.active += 1
                running = True

        try:
            if slot.users > 1 or self.active >= config.max_concurrent:
                await progress("Grok Bot 正在处理其他问题，本次问题已排队。")
            try:
                await asyncio.wait_for(acquire(), timeout=config.timeout)
            except asyncio.TimeoutError:
                raise GrokError("Grok Bot 排队等待超时，本次问题尚未发送，请稍后重试。") from None
            # An administrator may have disabled this conversation while queued.
            if not feature_store.is_enabled(scope.feature_scope, "grok_bot"):
                raise GrokError("当前会话的 Grok Bot 已关闭，本次问题尚未发送。")
            return await ask(config, prompt, scope)
        finally:
            if running:
                async with self.capacity:
                    self.active -= 1
                    self.capacity.notify_all()
            if acquired:
                slot.lock.release()
            slot.users -= 1
            if not slot.users:
                self.slots.pop(scope.key, None)
            self.pending -= 1


queue = RequestQueue()


def plain_text(elements) -> str:
    elements = list(elements)
    if any(isinstance(item, Image) for item in elements):
        raise GrokError("Grok Bot 当前支持文字提问和引用正文，图片暂未接入。")
    return "".join(item.text if isinstance(item, Text) else item for item in elements if isinstance(item, (Text, str))).strip()


async def send_text(session: Session, text: str):
    for start in range(0, len(text), 2000):
        # Satori Text prevents model output from injecting mentions or media tags.
        await session.send(MessageChain([Text(text[start:start + 2000])]), reply_to=True)


async def handle_grok(session: Session, result: Arparma):
    try:
        text = plain_text(result.all_matched_args.get("content", []) or [])
        if text.lower() in {"帮助", "help", "--help"} or (not text and not session.reply):
            await send_text(session, HELP)
            return
        scope = scope_from_session(session)
        if text in {"开启", "关闭"} and not session.reply:
            if scope.kind != "private":
                await send_text(session, "请用 /功能 开启 grok 或 /功能 关闭 grok 管理本群开关。")
            elif not is_superuser(session.event):
                await send_text(session, "仅 SuperUser 可管理自己的 Grok Bot 私聊开关。")
            else:
                feature_store.set_enabled(scope.feature_scope, "grok_bot", text == "开启")
                await send_text(session, f"本次私聊已{text} Grok Bot，配置已保存。")
            return
        if not feature_store.is_enabled(scope.feature_scope, "grok_bot"):
            await send_text(session, "当前会话的 Grok Bot 默认关闭，需 SuperUser 手动开启。群内用 /功能 开启 grok，自己的私聊用 /grok 开启。")
            return
        if session.reply and session.reply.origin:
            quoted = plain_text(MessageChain(session.reply.origin.message))
            if quoted:
                text = f"{text or '请回答或解释以下引用内容。'}\n\n[引用消息]\n{quoted}"
        if not text:
            raise GrokError("请输入文字问题，或引用一条包含正文的消息。")
        if len(text) > 6000:
            raise GrokError("问题和引用内容合计不能超过 6000 字。")
        config = GrokConfig.from_env()

        async def progress(message: str):
            await send_text(session, message)

        # Group members share a conversation but must remain distinguishable.
        user_id = str(getattr(getattr(session.event, "user", None), "id", "") or "")
        text = f"[本次发言者 ID：{user_id}]\n{text}"
        answer = await queue.run(config, text, progress, scope)
        if len(answer) > 20000:
            answer = answer[:20000] + "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
        await send_text(session, answer)
    except GrokError as error:
        await send_text(session, str(error))
    except (OSError, ValueError) as error:
        logger.warning("[grok_bot] config or storage failed: {}", type(error).__name__)
        await send_text(session, "Grok Bot 配置或开关读写失败，请管理员检查本地数据文件及目录权限。")
    except Exception as error:  # noqa: BLE001 - Never expose gateway bodies or credentials.
        logger.warning("[grok_bot] request failed: {}", type(error).__name__)
        await send_text(session, "Grok Bot 处理失败，请稍后重试。")


grok_command = Alconna(["grok", "grokbot"], Args["content;?", AllParam])
command.on(grok_command)(handle_grok)
