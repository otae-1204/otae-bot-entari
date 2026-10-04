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

HELP = """Grok Bot 问答服务
/grok <问题> —— 发送文本或附带图片提问；支持引用消息后提问（包含引用正文与图片，至多 3 张，每张限 5 MB）
Grok Bot 生成的图片与文件将自动转送至本会话（单文件上限 20 MB，单次至多 10 个且合计限 50 MB）。
别名：/grokbot。同群成员共享该群会话，不同群聊与私聊各自独立，默认采用花园多惠人设。
支持在生成任务进行期间追加输入；各会话按序排队接收，不同会话彼此并行。
若未追加输入则直接引用原提问回复；追加后改为在会话中顺序发送。
功能默认关闭：群内由 SuperUser 使用 /功能 开启 grok（管理员与群主可关闭）；私聊由 SuperUser 发送 /grok 开启 或 /grok 关闭。
如遇会话创建异常：SuperUser 确认云端无残留 Bot 后，可在当前会话执行 /grok 修复会话 重置状态。
若提示等待人工确认，请管理员登录 Grok Bot 客户端处理。"""


queue = (keeping("grok_conversation_receiver", obj_factory=RequestQueue, dispose=RequestQueue.close)
         if current_plugin.get(None) else RequestQueue())


def plain_text(elements) -> str:
    return "".join(item.text if isinstance(item, Text) else item for item in elements if isinstance(item, (Text, str))).strip()


def image_sources(elements) -> tuple[str, ...]:
    sources = []
    for item in elements:
        if isinstance(item, Image):
            if not isinstance(item.src, str) or not item.src:
                raise GrokError("无法解析图片链接，请重新发送该图片。")
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
        text = text[:20000] + "\n\n文本内容已超出最大长度限制，未展示部分请前往 Grok Bot 客户端查阅。"
    if text:
        await send_text(session, text, reply_to=reply_to)
    for attachment in reply.attachments:
        try:
            if not attachment.image and not attachment.error and (reply_to() if callable(reply_to) else reply_to):
                await send_text(session, f"推送文件：{attachment.name}", reply_to=reply_to)
            await send_attachment(session, attachment, reply_to=reply_to)
        except GrokError as error:
            await send_text(session, f"附件「{attachment.name}」发送未成功：{error}", reply_to=reply_to)


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
                await send_text(session, "格式：群聊请发送 /功能 开启 grok 或 /功能 关闭 grok 进行开关配置。")
            elif not is_superuser(session.event):
                await send_text(session, "权限受限：仅 SuperUser 拥有修改私聊 Grok Bot 开关状态的权限。")
            else:
                feature_store.set_enabled(scope.feature_scope, "grok_bot", text == "开启")
                await send_text(session, f"已为当前私聊{text} Grok Bot，状态更新完成。")
            return
        if not feature_store.is_enabled(scope.feature_scope, "grok_bot"):
            await send_text(session, "当前会话尚未启用 Grok Bot，需由 SuperUser 开启（群聊发送 /功能 开启 grok，私聊发送 /grok 开启）。")
            return
        repair_only = text == "修复会话" and not images and not session.reply
        if repair_only and not is_superuser(session.event):
            await send_text(session, "权限受限：仅 SuperUser 可对当前会话执行修复重置。")
            return
        if session.reply and session.reply.origin:
            quoted_elements = MessageChain(session.reply.origin.message)
            quoted = plain_text(quoted_elements)
            images += image_sources(quoted_elements)
            if quoted:
                text = f"{text or '请分析并解答以下引用的内容：'}\n\n[引用消息]\n{quoted}"
        if len(images) > MAX_INPUT_IMAGES:
            raise GrokError(f"输入与引用的图片总数不可超过 {MAX_INPUT_IMAGES} 张。")
        if images and not text:
            text = "请针对提供的图片进行识别与分析说明。"
        if not text:
            raise GrokError("未检测到有效输入，请附带文字、图片或引用目标消息后再试。")
        if len(text) > 6000:
            raise GrokError("提问文字与引用文本总长度不得超出 6000 字。")
        config = GrokConfig.from_env()

        async def progress(message: str):
            await send_text(session, message)

        if repair_only:
            await send_text(session, await queue.run(config, "", progress, scope, repair_only=True))
            return
        # Group members share a conversation but must remain distinguishable.
        user_id = str(getattr(getattr(session.event, "user", None), "id", "") or "")
        text = f"[发言者 ID：{user_id}]\n{text}"

        async def deliver(reply: Reply, *, reply_to=True):
            await send_reply(session, reply, reply_to=reply_to)

        answer = await queue.run(config, text, progress, scope, on_reply=deliver,
                                 **({"images": images, "account": session.account} if images else {}))
        await send_reply(session, answer)
    except GrokError as error:
        await send_text(session, str(error))
    except (OSError, ValueError) as error:
        logger.warning("[grok_bot] config or storage failed: {}", type(error).__name__)
        await send_text(session, "本地配置或存储数据读取失败，请联系管理员核对文件读写权限。")
    except Exception as error:  # noqa: BLE001 - Never expose gateway bodies or credentials.
        logger.warning("[grok_bot] request failed: {}", type(error).__name__)
        await send_text(session, "处理请求时发生内部错误，请稍后重新发起提问。")


grok_command = Alconna(["grok", "grokbot"], Args["content;?", AllParam])
command.on(grok_command)(handle_grok)
