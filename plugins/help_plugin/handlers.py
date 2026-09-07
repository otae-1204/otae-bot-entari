"""帮助插件 — 根据 /help 子指令返回对应帮助图片."""

from pathlib import Path

from otae_bot.adapters.entari import ChainMsg, make_image as ChainImage, ArgVal

from otae_bot.config.paths import IMAGE_PATH
from otae_bot.adapters.entari import cmd as _cmd, get_rest

HELP_IMAGE_DIR = Path(IMAGE_PATH) / "help"
HYW_HELP = "HYW 搜索问答：/q 问题，可附带图片；引用自己的回答后 /q 追问。\n/q 帮助 查看详细用法，/q 清空 删除当前会话历史。\n别名：/hyw、/何意味。管理员需先配置 HYW_* 模型参数。"
TEXT_TOPICS = {name: HYW_HELP for name in ("hyw", "q", "何意味")}
GROK_HELP = "Grok Bot 问答：/grok 问题；引用消息后提问可附上引用正文。\n别名：/grokbot，/grok 帮助 查看说明。每群与每个私聊独立，同会话排队，不同会话可并行，默认花园多惠人设。\n默认关闭，需 SuperUser 在目标群执行 /功能 开启 grok；管理员和群主可关闭。\n私聊默认关闭，SuperUser 可用 /grok 开启 管理自己的私聊；管理员需先配置 GROKBOT_*。"
TEXT_TOPICS.update({name: GROK_HELP for name in ("grok", "grokbot", "grok_bot")})
GROUP_FEATURE_HELP = "群内插件开关：/功能 列表、/功能 关闭 hyw、/功能 开启 hyw。\n支持插件名和 ef、steam、bili、mc、tibo 等别名。\n仅 SuperUser、本群管理员或群主可执行，只影响当前群，重启后保留。\nGrok Bot 默认关闭，仅 SuperUser 可开启，管理员和群主可关闭。"
TEXT_TOPICS.update({name: GROUP_FEATURE_HELP for name in ("功能", "插件", "plugin")})

# 子指令 → 图片文件名（不含扩展名）映射
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
    "mcping":    "mcping",
    "ping":      "mcping",
    "p":         "mcping",
    "mcsm":      "mcsm",
    "online":    "online",
    "ol":        "online",
    "broadcast": "broadcast",
    "bc":        "broadcast",
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


help_cmd = _cmd("help", aliases={"Help", "h", "帮助"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help_command(rest: ArgVal[str]):
    command_args = get_rest(rest)

    if command_args.lower() in TEXT_TOPICS:
        await help_cmd.finish(TEXT_TOPICS[command_args.lower()])
        return

    if command_args.lower() in ("list", "列表"):
        available = _available_topics()
        if available:
            await help_cmd.finish("可用的帮助主题:\n" + "\n".join(f"  /help {t}" for t in available))
        await help_cmd.finish("暂无帮助图片，请将图片放入 assets/image/help/")
        return

    if command_args:
        img_path = _resolve_image(command_args)
    else:
        img_path = HELP_IMAGE_DIR / "main.png"

    if img_path and img_path.exists():
        await help_cmd.finish(ChainMsg([ChainImage(path=str(img_path))]))
        return

    # 无匹配图片 → 提示
    available = _available_topics()
    tip = "可用的帮助主题:\n" + "\n".join(f"  /help {t}" for t in available) if available else "暂无帮助图片，请将图片放入 assets/image/help/"
    await help_cmd.finish(tip)
