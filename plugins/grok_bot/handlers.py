from __future__ import annotations

from collections.abc import Callable

from arclet.alconna import Alconna, AllParam, Args, Arparma
from arclet.entari import MessageChain, Session, command
from arclet.entari.plugin import current_plugin, keeping
from loguru import logger
from satori import Image, Text

from otae_bot.group_features import feature_store
from otae_bot.permissions import is_superuser

from .config import GrokConfig, GrokError
from .conversations import scope_from_session
from .media import MAX_INPUT_IMAGES, Reply, send_attachment
from .stream import RequestQueue

HELP = """Grok Bot 问答
/grok 问题：文字或图片提问；引用消息后提问可附上引用正文和图片（合计最多 3 张，每张 5 MB）。
Grok Bot 回复的图片和文件会转发到当前会话（单个最多 20 MB，每次最多 10 个、合计 50 MB）。
别名：/grokbot。同群共用本群对话，不同群与私聊分别独立，默认花园多惠人设。
支持任务进行中追加输入；每个会话统一接收回复，不同会话可并行处理。
本轮没有追加输入时引用初始问题回复；追加后直接发到当前会话，下一轮重新判断。
默认关闭：SuperUser 在目标群执行 /功能 开启 grok；管理员和群主可关闭。
私聊需 SuperUser 用 /grok 开启 或 /grok 关闭 管理自己的私聊开关。
创建失败后卡在待确认状态：SuperUser 确认云端无对应 Bot 后，用 /grok 修复会话 处理当前会话。
需要人工确认时，请管理员在 Grok Bot 应用中处理。"""


queue = (keeping("grok_conversation_receiver", obj_factory=RequestQueue, dispose=RequestQueue.close)
         if current_plugin.get(None) else RequestQueue())


def plain_text(elements) -> str:
    return "".join(item.text if isinstance(item, Text) else item for item in elements if isinstance(item, (Text, str))).strip()


def image_sources(elements) -> tuple[str, ...]:
    sources = []
    for item in elements:
        if isinstance(item, Image):
            if not isinstance(item.src, str) or not item.src:
                raise GrokError("图片地址缺失，请重新发送图片。")
            sources.append(item.src)
    return tuple(sources)


async def send_text(session: Session, text: str, *, reply_to: bool | Callable[[], bool] = True):
    for start in range(0, len(text), 2000):
        # Satori Text prevents model output from injecting mentions or media tags.
        await session.send(MessageChain([Text(text[start:start + 2000])]), reply_to=reply_to() if callable(reply_to) else reply_to)


async def send_reply(session: Session, reply: Reply | str, *, reply_to: bool | Callable[[], bool] = True):
    if isinstance(reply, str):
        reply = Reply(reply)
    text = reply.text
    if len(text) > 20000:
        text = text[:20000] + "\n\n回答过长，剩余内容请在 Grok Bot 应用中查看。"
    if text:
        await send_text(session, text, reply_to=reply_to)
    for attachment in reply.attachments:
        try:
            if not attachment.image and not attachment.error and (reply_to() if callable(reply_to) else reply_to):
                await send_text(session, f"文件：{attachment.name}", reply_to=reply_to)
            await send_attachment(session, attachment, reply_to=reply_to)
        except GrokError as error:
            await send_text(session, f"附件「{attachment.name}」转发失败：{error}", reply_to=reply_to)


async def handle_grok(session: Session, result: Arparma):
    try:
        elements = list(result.all_matched_args.get("content", []) or [])
        text, images = plain_text(elements), image_sources(elements)
        if not images and (text.lower() in {"帮助", "help", "--help"} or (not text and not session.reply)):
            await send_text(session, HELP)
            return
        scope = scope_from_session(session)
        if text in {"开启", "关闭"} and not images and not session.reply:
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
        repair_only = text == "修复会话" and not images and not session.reply
        if repair_only and not is_superuser(session.event):
            await send_text(session, "仅 SuperUser 可修复当前 Grok Bot 会话。")
            return
        if session.reply and session.reply.origin:
            quoted_elements = MessageChain(session.reply.origin.message)
            quoted = plain_text(quoted_elements)
            images += image_sources(quoted_elements)
            if quoted:
                text = f"{text or '请回答或解释以下引用内容。'}\n\n[引用消息]\n{quoted}"
        if len(images) > MAX_INPUT_IMAGES:
            raise GrokError(f"发送和引用的图片合计不能超过 {MAX_INPUT_IMAGES} 张。")
        if images and not text:
            text = "请描述并分析这些图片。"
        if not text:
            raise GrokError("请输入文字问题、附带图片，或引用一条包含正文或图片的消息。")
        if len(text) > 6000:
            raise GrokError("问题和引用内容合计不能超过 6000 字。")
        config = GrokConfig.from_env()

        async def progress(message: str):
            await send_text(session, message)

        if repair_only:
            await send_text(session, await queue.run(config, "", progress, scope, repair_only=True))
            return
        # Group members share a conversation but must remain distinguishable.
        user_id = str(getattr(getattr(session.event, "user", None), "id", "") or "")
        text = f"[本次发言者 ID：{user_id}]\n{text}"

        async def deliver(reply: Reply, *, reply_to=True):
            await send_reply(session, reply, reply_to=reply_to)

        answer = await queue.run(config, text, progress, scope, on_reply=deliver,
                                 **({"images": images, "account": session.account} if images else {}))
        await send_reply(session, answer)
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
