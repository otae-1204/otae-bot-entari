from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from otae_bot.config.settings import _env


class GrokError(Exception):
    """A diagnostic safe to show in QQ; never include upstream bodies."""


@dataclass(frozen=True)
class GrokConfig:
    base_url: str = ""
    token: str = field(default="", repr=False)
    agent_id: str = ""
    timeout: float = 300
    max_pending: int = 8
    poll_interval: float = 1

    @classmethod
    def from_env(cls) -> GrokConfig:
        base = str(_env("GROKBOT_GATEWAY_URL", "") or "").strip().rstrip("/")
        token = str(_env("GROKBOT_GATEWAY_TOKEN", "") or "").strip()
        agent_id = str(_env("GROKBOT_AGENT_ID", "") or "").strip()
        if not all((base, token, agent_id)):
            raise GrokError("Grok Bot 尚未配置。请管理员填写 GROKBOT_GATEWAY_URL、GROKBOT_GATEWAY_TOKEN 和 GROKBOT_AGENT_ID。")
        try:
            url = urlsplit(base)
            valid_url = (
                url.scheme in {"http", "https"} and url.hostname
                and url.hostname not in {"0.0.0.0", "::"}
                and not (url.username or url.password or url.query or url.fragment or url.path)
                and (url.port is None or 0 < url.port < 65536)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise GrokError("GROKBOT_GATEWAY_URL 应为网关基址，例如 http://100.x.x.x:1340；不要填写 /v1、/api 或 0.0.0.0。")
        try:
            agent_id = str(UUID(agent_id))
        except ValueError:
            raise GrokError("GROKBOT_AGENT_ID 应为目标 Bot 的 UUID。") from None
        if not token.isascii() or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token):
            raise GrokError("GROKBOT_GATEWAY_TOKEN 格式无效，请填写网关 Token 原文。")
        try:
            timeout = int(str(_env("GROKBOT_TIMEOUT", 300)))
            pending = int(str(_env("GROKBOT_MAX_PENDING", 8)))
            if not 30 <= timeout <= 1800 or not 1 <= pending <= 32:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise GrokError("GROKBOT_TIMEOUT 应为 30～1800 秒，GROKBOT_MAX_PENDING 应为 1～32。") from None
        return cls(base_url=base, token=token, agent_id=agent_id, timeout=timeout, max_pending=pending)
