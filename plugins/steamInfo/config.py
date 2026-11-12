"""steamInfo 插件配置：entari.yml 的 ``plugins: plugins.steamInfo:`` 段（SteamSettings），
运行时整理成 ``Config``。"""

from typing import List
from pydantic import BaseModel

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings, system_proxy

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"


class SteamSettings(Settings):
    """Steam 好友动态播报；API Key 是密钥，经 ${{ env.X :- }} 引用 .env"""

    api_keys: str = field(default="", description="Steam Web API Key，多个用逗号分隔轮换（密钥）")
    api_key: str = field(default="", description="单个 Steam Web API Key；api_keys 为空时使用（密钥）")
    request_interval: int = field(default=120, description="轮询间隔秒数")
    disable_broadcast_on_startup: bool = field(default=False, description="启动时不开启播报轮询")
    llm_api_key: str = field(default="", description="Steam 评价用 LLM 的 API Key（密钥）")
    llm_base_url: str = field(default=DEFAULT_LLM_BASE_URL, description="Steam 评价用 LLM 的接口基址")
    llm_model: str = field(default=DEFAULT_LLM_MODEL, description="Steam 评价用 LLM 的模型名")


class Config(BaseModel):
    steam_api_key: str | List[str] = ""
    steam_api_keys: str | List[str] = ""
    proxy: str | None = None
    steam_request_interval: int = 120
    steam_broadcast_type: str = "part"
    steam_disable_broadcast_on_startup: bool = False
    steam_llm_api_key: str = ""
    steam_llm_base_url: str = "https://api.deepseek.com"
    steam_llm_model: str = "deepseek-v4-flash"


def load_config() -> Config:
    """The plugin's runtime config; the proxy is the process-wide HTTP(S)_PROXY."""
    settings = plugin_settings("steamInfo", SteamSettings)
    proxy = system_proxy()
    return Config(
        steam_api_key=settings.api_key,
        steam_api_keys=settings.api_keys,
        proxy=proxy["http"],
        steam_request_interval=settings.request_interval,
        steam_disable_broadcast_on_startup=settings.disable_broadcast_on_startup,
        steam_llm_api_key=settings.llm_api_key,
        steam_llm_base_url=settings.llm_base_url or DEFAULT_LLM_BASE_URL,
        steam_llm_model=settings.llm_model or DEFAULT_LLM_MODEL,
    )
