"""明日方舟插件配置：entari.yml 的 ``plugins: plugins.arknights:`` 段。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class ArknightsSettings(Settings):
    """明日方舟（森空岛签到，/ak）"""

    credential_key: str = field(
        default="",
        description="森空岛凭据加密密钥（密钥）：Base64 编码的 32 字节，填 \"${{ env.ARKNIGHTS_CREDENTIAL_KEY :- }}\"；"
        "留空时 /ak 绑定与签到禁用，不会回退到终末地的密钥",
    )


def arknights_settings() -> ArknightsSettings:
    return plugin_settings("arknights", ArknightsSettings)
