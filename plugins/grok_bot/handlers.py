from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from arclet.alconna import Alconna, AllParam, Args, Arparma
from arclet.entari import MessageChain, Session, command
from loguru import logger
from satori import Image, Text

from .config import GrokConfig, GrokError
from .gateway import ask

HELP = """Grok Bot 问答
/grok 问题：发送文字问题；引用消息后 /grok 问题可附上引用正文。
别名：/grokbot。支持同一用户连续提交，多条请求排队处理。
当前使用管理员指定的同一个 Bot，各群与私聊共用它的上下文。
需要人工确认时，请管理员在 Grok Bot 应用中处理。"""


class RequestQueue:
    """Accept concurrent callers while protecting the one shared Bot chat."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = 0

    async def run(self, config: GrokConfig, prompt: str, progress: Callable[[str], Awaitable[None]]) -> str:
        if self.pending >= config.max_pending:
            raise GrokError("Grok Bot 等待队列已满，请稍后重试。")
        self.pending += 1
        acquired = False
        try:
            if self.pending > 1:
                await progress("Grok Bot 正在处理其他问题，本次问题已排队。")
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=config.timeout)
                acquired = True
            except asyncio.TimeoutError:
                raise GrokError("Grok Bot 排队等待超时，本次问题尚未发送，请稍后重试。") from None
            return await ask(config, prompt)
        finally:
            if acquired:
                self.lock.release()
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

        answer = await queue.run(config, text, progress)
        if len(answer) > 20000:
            answer = answer[:20000] + "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
        await send_text(session, answer)
    except GrokError as error:
        await send_text(session, str(error))
    except Exception as error:  # noqa: BLE001 - Never expose gateway bodies or credentials.
        logger.warning("[grok_bot] request failed: {}", type(error).__name__)
        await send_text(session, "Grok Bot 处理失败，请稍后重试。")


grok_command = Alconna(["grok", "grokbot"], Args["content;?", AllParam])
command.on(grok_command)(handle_grok)
