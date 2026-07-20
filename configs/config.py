from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List


def _load_dotenv() -> dict:
    """简易 .env 加载器，避免引入 python-dotenv 依赖."""
    env_vars: dict = {}
    env_file = Path(".env")
    if not env_file.exists():
        return env_vars
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if "[" in value and "]" in value:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        env_vars[key] = value
    return env_vars


_dotenv = _load_dotenv()
for _k, _v in _dotenv.items():
    if isinstance(_v, str):
        os.environ.setdefault(_k, _v)
    elif isinstance(_v, (list, dict)):
        # json 写入 os.environ 供 pydantic Config 读取（如 SATORI_CLIENTS）
        os.environ.setdefault(_k, json.dumps(_v))


def _env(key: str, default: Any = None) -> Any:
    raw = os.getenv(key)
    if raw is not None:
        # os.environ 中的 JSON 字符串（如 SATORI_CLIENTS）需要解析，
        # 普通字符串（如 HOST=127.0.0.1）json.loads 会失败，返回原值
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return _dotenv.get(key, default)


# ── Bot 基础配置 ──
_nick = _env("NICKNAME", "otae")
NICKNAME: str = str(_nick[0] if isinstance(_nick, list) and _nick else _nick)


class Config:
    SUPERUSERS: List[str] = _env("SUPERUSERS", ["2461673400"])
    COMMAND_START: List[str] = _env("COMMAND_START", ["/"])


SATORI_CLIENTS: List[dict] = _env("SATORI_CLIENTS", [])


# ── 全局代理 ──
SYSTEM_PROXY: dict = {
    "http": str(_env("HTTP_PROXY", "") or "").strip() or None,
    "https": str(_env("HTTPS_PROXY", "") or "").strip() or None,
}


# ── MCSManager 面板 ──
MCSM_PANEL_URL: str = _env("MCSM_PANEL_URL", "http://127.0.0.1:23333")
MCSM_API_KEY: str = _env("MCSM_API_KEY", "")
MCSM_GROUP_WHITELIST: List[str] = [str(g) for g in _env("MCSM_GROUP_WHITELIST", [])]

# ── Minecraft 插件 ──
MC_BROADCAST_INTERVAL: int = int(_env("MC_BROADCAST_INTERVAL", "300"))

# ── Steam 插件 ──
STEAM_API_KEYS: List[str] = (
    _env("STEAM_API_KEYS", "").split(",")
    if _env("STEAM_API_KEYS", "")
    else []
)
STEAM_REQUEST_INTERVAL: int = int(_env("STEAM_REQUEST_INTERVAL", "60"))


# ── JSON 文件工具 ──
def openJson(json_file: Path) -> Dict[str, Any]:
    if not json_file.parent.exists():
        json_file.parent.mkdir(parents=True, exist_ok=True)
    if not json_file.exists():
        json_file.write_text("{}", encoding="utf-8")
    return json.loads(json_file.read_text(encoding="utf-8"))


def saveJson(json_file: Path, data: dict) -> None:
    json_file.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


# ── 向后兼容：保留原来的 Plugin_Config 类 ──
class Plugin_Config:
    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.plugin_config_path = Path("configs") / f"{plugin_name}/config.json"
        self.plugin_content = openJson(self.plugin_config_path)

    def update(self):
        saveJson(self.plugin_config_path, self.plugin_content)
