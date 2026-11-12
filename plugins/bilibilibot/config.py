"""bilibilibot 的配置：entari.yml 的 ``plugins: plugins.bilibilibot:`` 段。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class BiliSettings(Settings):
    """B 站订阅推送；Cookie 类字段是密钥，经 ${{ env.X :- }} 引用 .env"""

    sessdata: str = field(default="", description="Cookie SESSDATA（密钥）")
    buvid3: str = field(default="", description="Cookie buvid3（密钥）")
    dm_img_list: str = field(default="", description="风控参数 dm_img_list（密钥）")
    dm_img_str: str = field(default="", description="风控参数 dm_img_str（密钥）")
    dm_cover_img_str: str = field(default="", description="风控参数 dm_cover_img_str（密钥）")
    rsshub_base_urls: list[str] = field(
        default_factory=lambda: ["https://rsshub.app", "https://rss.materium.io"],
        description="RSSHub 镜像列表，按顺序尝试",
    )
    lottery_filter_uids: str = field(
        default="*", description="动态订阅跳过社交抽奖的账号：* = 所有账号；逗号分隔的 UID 收窄；留空关闭过滤"
    )
    live_batch: bool = field(default=True, description="直播状态走批量接口；false 回到逐个房间查询（回滚开关）")


def bili_settings() -> BiliSettings:
    return plugin_settings("bilibilibot", BiliSettings)
