"""eft_helper 配置：entari.yml 的 ``plugins: plugins.eft_helper:`` 段（原 configs/eft_helper/config.json）。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class EftSettings(Settings):
    """逃离塔科夫弹药查询"""

    use_proxy: bool = field(default=False, description="请求 api.tarkov.dev 时是否走 HTTP_PROXY")


def eft_settings() -> EftSettings:
    return plugin_settings("eft_helper", EftSettings)
