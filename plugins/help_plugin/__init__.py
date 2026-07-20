"""帮助插件 — 根据 /help 子指令返回对应帮助图片."""

from pathlib import Path

from utils.entari_native import ChainMsg, make_image as ChainImage, ArgVal

from configs.path_config import IMAGE_PATH
from utils.entari_native import cmd as _cmd, get_rest

HELP_IMAGE_DIR = Path(IMAGE_PATH) / "help"

# 子指令 → 图片文件名（不含扩展名）映射
TOPIC_MAP: dict[str, str] = {
    "main":      "main",
    "home":      "main",
    "steam":     "steam",
    "mc":        "minecraft",
    "minecraft": "minecraft",
    "bili":      "bili",
    "bilibili":  "bili",
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
    return sorted({
        topic
        for topic, image_name in TOPIC_MAP.items()
        if (HELP_IMAGE_DIR / f"{image_name}.png").exists()
    })


help_cmd = _cmd("help", aliases={"Help", "h", "帮助"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help_command(rest: ArgVal[str]):
    command_args = get_rest(rest)

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
