"""steamInfo 插件配置 — 从 .env 读取."""

from typing import List
from pydantic import BaseModel


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
