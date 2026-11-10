from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from otae_bot.config.settings import SYSTEM_PROXY, _env


def _env_int(name: str, default: int) -> int:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_positive(name: str, default: float) -> float:
    """Positive, finite seconds; anything else falls back to the default."""
    value = _env_float(name, default)
    return value if math.isfinite(value) and value > 0 else default


def _env_at_least(name: str, default: int, minimum: int) -> int:
    """Integers below ``minimum`` are treated as invalid, not clamped."""
    value = _env_int(name, default)
    return value if value >= minimum else default


def _proxy(name: str, fallback: str = "") -> str:
    value = str(_env(name, "") or "").strip()
    if value.lower() == "direct":
        return ""
    if value:
        return value
    return fallback


@dataclass(frozen=True)
class HywConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "gemini-3.8-flash"
    proxy: str = field(default="", repr=False)
    search_proxy: str = field(default="", repr=False)
    render: bool = True
    timeout: float = 300
    request_timeout: float = 90
    reasoning_mode: str = "auto"
    max_rounds: int = 10
    render_timeout: float = 45
    render_max_images: int = 16
    render_max_edge: int = 1024
    max_concurrent: int = 2
    max_tool_images: int = 600
    max_reader_images: int = 30
    search_provider: str = "ddgs"
    search_mode: str = "turbo"
    reader_engine: str = "browser"
    auth_mode: str = "api_key"
    provider: str = "openai-compatible"
    api: str = "chat"
    credentials_file: str = ""
    vertex_location: str = "global"
    vertex_base_url: str = ""
    auth_error: str = ""
    home: str = "data/hyw-frontier"

    @property
    def configured(self) -> bool:
        if self.auth_mode == "service_account":
            return bool(self.credentials_file) and not self.auth_error
        if self.auth_mode == "api_key":
            return bool(self.api_key)
        return False

    @classmethod
    def from_env(cls) -> "HywConfig":
        source = str(_env("HYW_CONFIG_SOURCE", "hyw")).strip().lower()
        prefixes = {"hyw": "HYW", "llm": "LLM", "steam": "STEAM_LLM"}
        if source not in prefixes:
            raise ValueError("HYW_CONFIG_SOURCE 应为 hyw、llm 或 steam")
        prefix = prefixes[source]
        default_base = cls.base_url if source == "hyw" else "https://api.deepseek.com"
        default_model = cls.model if source == "hyw" else "deepseek-v4-flash"
        proxy = _proxy("HYW_PROXY", str(SYSTEM_PROXY.get("https") or SYSTEM_PROXY.get("http") or "").strip())
        search_proxy = _proxy("HYW_SEARCH_PROXY", proxy)
        base_url = str(_env(f"{prefix}_BASE_URL", "") or default_base).strip().rstrip("/")
        model = str(_env(f"{prefix}_MODEL", "") or default_model).strip()
        credentials_file = str(_env("HYW_CREDENTIALS_FILE", "") or _env("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
        vertex_location = str(_env("HYW_VERTEX_LOCATION", "") or "").strip() or "global"
        vertex_base_url = str(_env("HYW_VERTEX_BASE_URL", "") or "").strip().rstrip("/")
        api_key = str(_env(f"{prefix}_API_KEY", "") or "").strip()
        auth_mode, auth_error, provider, api = "api_key", "", "openai-compatible", "chat"
        if "api.deepseek.com" in base_url and not credentials_file:
            provider, api = "deepseek", "responses"
        if credentials_file:
            auth_mode, auth_error = "service_account", ""
            provider, api = "google", "google"
            if not Path(credentials_file).expanduser().is_file():
                auth_mode, auth_error = "none", "找不到服务账号 JSON。"
            model = model.split("/", 1)[-1]
        elif not api_key:
            auth_mode = "none"
        home = str(_env("HYW_HOME", "") or cls.home).strip() or cls.home
        search_provider = str(_env("HYW_SEARCH_PROVIDER", "") or cls.search_provider).strip().lower()
        if search_provider not in {"ddgs", "jina", "parallel"}:
            search_provider = cls.search_provider
        reader_engine = str(_env("HYW_READER_ENGINE", "") or cls.reader_engine).strip().lower()
        if reader_engine not in {"default", "browser"}:
            reader_engine = cls.reader_engine
        reasoning_mode = str(_env("HYW_REASONING_MODE", "") or cls.reasoning_mode).strip().lower()
        if reasoning_mode not in {"auto", "high", "medium", "low"}:
            reasoning_mode = cls.reasoning_mode
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            proxy=proxy,
            search_proxy=search_proxy,
            render=str(_env("HYW_RENDER", True)).lower() not in {"false", "0", "no"},
            timeout=max(1.0, _env_float("HYW_TIMEOUT", cls.timeout)),
            request_timeout=max(1.0, _env_float("HYW_REQUEST_TIMEOUT", cls.request_timeout)),
            reasoning_mode=reasoning_mode,
            max_rounds=_env_at_least("HYW_MAX_ROUNDS", cls.max_rounds, 1),
            render_timeout=_env_positive("HYW_RENDER_TIMEOUT", cls.render_timeout),
            render_max_images=_env_at_least("HYW_RENDER_MAX_IMAGES", cls.render_max_images, 0),
            # The render worker accepts at most 1280px previews; tiny edges would be useless.
            render_max_edge=min(1280, _env_at_least("HYW_RENDER_MAX_EDGE", cls.render_max_edge, 64)),
            max_concurrent=max(1, _env_int("HYW_MAX_CONCURRENT", cls.max_concurrent)),
            max_tool_images=max(0, _env_int("HYW_MAX_TOOL_IMAGES", cls.max_tool_images)),
            max_reader_images=max(0, _env_int("HYW_MAX_READER_IMAGES", cls.max_reader_images)),
            search_provider=search_provider,
            search_mode=str(_env("HYW_SEARCH_MODE", "") or cls.search_mode).strip() or cls.search_mode,
            reader_engine=reader_engine,
            auth_mode=auth_mode,
            provider=provider,
            api=api,
            credentials_file=credentials_file,
            vertex_location=vertex_location,
            vertex_base_url=vertex_base_url,
            auth_error=auth_error,
            home=home,
        )


class HywError(Exception):
    pass
