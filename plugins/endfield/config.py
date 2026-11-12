"""终末地插件配置：entari.yml 的 ``plugins: plugins.endfield:`` 段。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class EndfieldSettings(Settings):
    """明日方舟：终末地（/ef）"""

    credential_key: str = field(
        default="",
        description="账号凭据加密密钥（密钥）：Base64 编码的 32 字节，填 \"${{ env.ENDFIELD_CREDENTIAL_KEY :- }}\"；"
        "留空时禁用账号绑定与凭据读取",
    )
    gacha_layout: str = field(default="v3", description="抽卡分析版式：v3（三栏 + 按高度分页）或 v1（旧版两栏，回滚用）")
    gacha_forward_above: int = field(default=3, description="抽卡分析图超过几张改为合并转发（0 = 关闭合并转发）")
    gacha_show_standard: bool = field(default=True, description="是否展示基础寻访卡片（隐藏时仍计入总数）")
    gacha_rerun_side: str = field(default="right", description="重构侧栏放在哪一边：right 或 left")


def endfield_settings() -> EndfieldSettings:
    return plugin_settings("endfield", EndfieldSettings)
