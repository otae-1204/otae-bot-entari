from __future__ import annotations

from dataclasses import dataclass, field

from otae_bot.config.settings import SYSTEM_PROXY, _env


@dataclass(frozen=True)
class HywConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "gpt-4o"
    proxy: str = field(default="", repr=False)
    render: bool = True
    timeout: float = 120
    max_turns: int = 10
    max_tools: int = 8

    @classmethod
    def from_env(cls) -> HywConfig:
        # Read the complete credential/endpoint/model tuple from one source.
        source = str(_env("HYW_CONFIG_SOURCE", "hyw")).lower()
        prefixes = {"hyw": "HYW", "llm": "LLM", "steam": "STEAM_LLM"}
        if source not in prefixes:
            raise ValueError("HYW_CONFIG_SOURCE 应为 hyw、llm 或 steam")
        prefix = prefixes[source]
        default_base = cls.base_url if source == "hyw" else "https://api.deepseek.com"
        default_model = cls.model if source == "hyw" else "deepseek-v4-flash"
        return cls(
            api_key=str(_env(f"{prefix}_API_KEY", "") or "").strip(),
            base_url=str(_env(f"{prefix}_BASE_URL", "") or default_base).rstrip("/"),
            model=str(_env(f"{prefix}_MODEL", "") or default_model),
            proxy=str(_env("HYW_PROXY", "") or SYSTEM_PROXY.get("https") or SYSTEM_PROXY.get("http") or ""),
            render=str(_env("HYW_RENDER", True)).lower() not in {"false", "0", "no"},
        )


class HywError(Exception):
    """An error whose message is safe to send to the chat."""
