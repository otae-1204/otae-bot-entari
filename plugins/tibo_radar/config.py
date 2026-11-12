"""Tibo 雷达的配置：entari.yml 的 ``plugins: plugins.tibo_radar:`` 段。"""

from __future__ import annotations

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings


class TiboRadarSettings(Settings):
    """Tibo 雷达（Codex 重置 / Tibo 动态订阅）；全部可留空"""

    refresh_minutes: int = field(default=10, description="采集周期分钟数（2–1440）")
    timeout_seconds: int = field(default=15, description="单次请求超时秒数（3–60）")
    codexradar_enabled: bool = field(default=True, description="是否采集 codexradar.com")
    subscription_batch: int = field(default=20, description="群订阅每次最多推送条数（1–100）")
    subscription_max_pages: int = field(default=1, description="群订阅每次最多推送页数（1–10）")
    codexradar_url: str = field(default="https://codexradar.com/", description="codexradar 页面地址")
    feed_url: str = field(default="https://codex-reset.com/api/feed?locale=zh", description="动态 feed 接口")
    timeline_url: str = field(
        default="https://codex-reset.com/api/timeline?locale=zh", description="重置时间线接口"
    )


def tibo_radar_settings() -> TiboRadarSettings:
    return plugin_settings("tibo_radar", TiboRadarSettings)
