"""Minecraft 插件配置：entari.yml 的 ``plugins: plugins.minecraft_plugin:`` 段。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class MinecraftSettings(Settings):
    """Minecraft 服务器状态与播报"""

    broadcast_interval: int = field(default=300, description="全局默认播报间隔秒数（各群可用命令单独设置）")


def minecraft_settings() -> MinecraftSettings:
    return plugin_settings("minecraft_plugin", MinecraftSettings)
