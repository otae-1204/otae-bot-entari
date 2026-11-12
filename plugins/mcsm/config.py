"""MCSM 插件配置：entari.yml 的 ``plugins: plugins.mcsm:`` 段。

面板地址与 API Key 是各群用命令绑定的运行时数据（data/，见 store.py），不在这里。
"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class McsmSettings(Settings):
    """MCSManager 面板管理"""

    deploy_large_package_mb: int = field(
        default=200, description="部署时超过多少 MB 的压缩包改走大包策略（远程直链安装）"
    )


def mcsm_settings() -> McsmSettings:
    return plugin_settings("mcsm", McsmSettings)
