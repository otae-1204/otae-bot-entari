"""MCSM 插件配置."""

from typing import List

from configs.config import _env


class Config:
    """MCSM 插件配置，读取自 .env 全局配置."""
    mcsm_panel_url: str = _env("MCSM_PANEL_URL", "http://127.0.0.1:23333")
    mcsm_api_key: str = _env("MCSM_API_KEY", "")
    mcsm_group_whitelist: List[str] = [str(g) for g in _env("MCSM_GROUP_WHITELIST", [])]

