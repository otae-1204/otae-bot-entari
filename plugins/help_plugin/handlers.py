"""帮助插件 — 根据 /help 子指令返回对应帮助图片."""

from pathlib import Path

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Image, MessageChain, Session, command
from arclet.entari.command import Match
from nepattern import AnyString

from otae_bot.config.paths import IMAGE_PATH
from otae_bot.messaging.inbound import get_rest
from otae_bot.infrastructure.rendering.help_runtime import cached_help_image

HELP_IMAGE_DIR = Path(IMAGE_PATH) / "help"
HYW_HELP = "HYW 搜索问答：\n/q <问题> —— 搜索并回答，支持附带图片\n/q <追问> —— 引用自己之前的回答即可继续追问\n/q 帮助 —— 查看详细说明\n/q 清空 —— 清空当前会话历史\n别名：/hyw、/何意味；管理员需先配置 HYW_* 模型参数。"
TEXT_TOPICS = {name: HYW_HELP for name in ("hyw", "q", "何意味")}
GROK_HELP = "Grok Bot 问答：\n/grok <问题> —— 发送文本或附带图片提问，支持引用消息提问\n/grok 帮助 —— 查看详细使用说明\n别名：/grokbot；各群与各私聊会话相互独立，默认采用花园多惠人设。\n功能默认关闭，群内需 SuperUser 执行 /功能 开启 grok（管理员与群主可关闭）；私聊需 SuperUser 使用 /grok 开启；管理员需先配置 GROKBOT_* 参数。"
TEXT_TOPICS.update({name: GROK_HELP for name in ("grok", "grokbot", "grok_bot")})
GROUP_FEATURE_HELP = "本群插件开关：\n/功能 列表 —— 查看本群插件启用状态\n/功能 开启 <插件名> —— 在本群启用指定插件\n/功能 关闭 <插件名> —— 在本群禁用指定插件\n支持插件原名及别名（如 ef、steam、bili、mc、tibo 等）。\n仅 SuperUser、本群管理员或群主可操作，设置仅对当前群生效且持久保存。Grok Bot 默认关闭，仅限 SuperUser 开启，管理员与群主可关闭。"
TEXT_TOPICS.update({name: GROUP_FEATURE_HELP for name in ("功能", "插件", "plugin")})
CHANGELOG_HELP = "更新日志查询：\n/更新日志 —— 查看最新版本更新说明\n/更新日志 列表 —— 查看版本目录列表（每页 12 项）\n/更新日志 <版本号/序号/关键词> —— 查询指定版本的更新条目（如 /更新日志 v1.14.0、/更新日志 2）\n/更新日志 统计 —— 查看版本与提交汇总数据\n别名：/更新、/changelog、/版本；版本按阶段划分，每项条目均对应仓库真实提交。"
TEXT_TOPICS.update({name: CHANGELOG_HELP for name in ("更新", "更新日志", "changelog", "版本")})
SIGNIN_HELP = "签到：/ef 签到（终末地）或 /ak 签到（明日方舟），选择器只作用于该命令的游戏。\n若同一 QQ 还绑定了另一款游戏，会顺带签到它的全部已绑定角色，两张结果卡上下合成一张图；未绑定另一款时只返回当前游戏。\n本群关闭了另一款游戏时不会顺带签到；单个角色失败只影响它自己。"
TEXT_TOPICS.update({name: SIGNIN_HELP for name in ("签到", "checkin")})

# 子指令 → 图片文件名（不含扩展名）映射
# 每个目标都必须有对应的 assets/image/help/<name>.png，否则该主题无法解析；
# tests/test_help_plugin.py 会校验这张表与磁盘一致，避免登记了却没有图。
# assets/image/help/<name>.png 由 scripts/render/render_help_cards.py 生成，作为无浏览器时的静态回退；
# 实际发送的插画按页面底板比例从 gallery.json 里随机挑一张，运行时渲染并缓存（见 help_runtime）。
TOPIC_MAP: dict[str, str] = {
    "main":      "main",
    "home":      "main",
    "steam":     "steam",
    "mc":        "minecraft",
    "minecraft": "minecraft",
    "bili":      "bili",
    "bilibili":  "bili",
    "tibo":      "tibo",
    "雷达":       "tibo",
    "endfield":  "endfield",
    "ef":        "endfield",
    "zmd":       "endfield",
    "终末地":    "endfield",
    "ak":        "ak",
    "明日方舟":  "ak",
    "arknights": "ak",
    "森空岛":    "ak",
    "mcsm":      "mcsm",
}


def _resolve_image(topic: str) -> Path | None:
    """根据子指令查找对应图片路径，无匹配时返回 None."""
    name = TOPIC_MAP.get(topic.lower())
    if name:
        p = HELP_IMAGE_DIR / f"{name}.png"
        if p.exists():
            return p
    return None


def _available_topics() -> list[str]:
    return sorted(set(TEXT_TOPICS) | {
        topic
        for topic, image_name in TOPIC_MAP.items()
        if (HELP_IMAGE_DIR / f"{image_name}.png").exists()
    })


help_cmd = Alconna(["help", "Help", "h", "帮助"], Args["rest;?", MultiVar(AnyString)])


@command.on(help_cmd)
async def handle_help_command(session: Session, rest: Match[tuple[str, ...]]):
    command_args = get_rest(rest)

    if command_args.lower() in TEXT_TOPICS:
        await session.send(TEXT_TOPICS[command_args.lower()])
        return

    if command_args.lower() in ("list", "列表"):
        available = _available_topics()
        if available:
            await session.send("可用帮助主题：\n" + "\n".join(f"  /help {t}" for t in available))
            return
        await session.send("暂无可用的帮助图，请联系管理员检查 assets/image/help/ 目录。")
        return

    # 未给出主题时默认发主帮助图；给了未知主题则不匹配，落到下面的提示。
    topic = TOPIC_MAP.get(command_args.lower()) if command_args else "main"
    chosen = await cached_help_image(topic) if topic else None
    if chosen:
        await session.send(MessageChain([Image.of(path=Path(chosen))]))
        return

    # 无匹配图片 → 提示
    available = _available_topics()
    tip = "可用帮助主题：\n" + "\n".join(f"  /help {t}" for t in available) if available else "暂无可用的帮助图，请联系管理员检查 assets/image/help/ 目录。"
    await session.send(tip)
