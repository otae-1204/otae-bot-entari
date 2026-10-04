from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from otae_bot.config.settings import _env

DEFAULT_PERSONA = Path(__file__).with_name("persona.md")


class GrokError(Exception):
    """A diagnostic safe to show in QQ; never include upstream bodies."""


class GatewayError(GrokError):
    """Transport metadata, without upstream response bodies or credentials."""

    def __init__(self, message: str, *, not_submitted: bool = False, retryable: bool = False):
        super().__init__(message)
        self.not_submitted = not_submitted
        self.retryable = retryable


@dataclass(frozen=True)
class GrokConfig:
    base_url: str = ""
    token: str = field(default="", repr=False)
    agent_id: str = ""
    timeout: float = 300
    max_pending: int = 8
    max_concurrent: int = 4
    persona_file: Path = DEFAULT_PERSONA
    poll_interval: float = 1

    @classmethod
    def from_env(cls) -> GrokConfig:
        base = str(_env("GROKBOT_GATEWAY_URL", "") or "").strip().rstrip("/")
        token = str(_env("GROKBOT_GATEWAY_TOKEN", "") or "").strip()
        agent_id = str(_env("GROKBOT_AGENT_ID", "") or "").strip()
        if not all((base, token)):
            raise GrokError("Grok Bot 尚未完成配置，请管理员补充填写 GROKBOT_GATEWAY_URL 与 GROKBOT_GATEWAY_TOKEN。")
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
            raise GrokError("GROKBOT_GATEWAY_URL 须配置为网关基础地址（如 http://100.x.x.x:1340），请勿包含 /v1、/api 或设为 0.0.0.0。")
        try:
            agent_id = str(UUID(agent_id)) if agent_id else ""
        except ValueError:
            raise GrokError("GROKBOT_AGENT_ID 格式不符合规范，须为目标 Bot 的 UUID。") from None
        if not token.isascii() or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token):
            raise GrokError("GROKBOT_GATEWAY_TOKEN 无效，请填写正确的网关 Token 原始内容。")
        try:
            timeout = int(str(_env("GROKBOT_TIMEOUT", 300)))
            pending = int(str(_env("GROKBOT_MAX_PENDING", 8)))
            concurrent = int(str(_env("GROKBOT_MAX_CONCURRENT", 4)))
            if not 30 <= timeout <= 1800 or not 1 <= pending <= 32 or not 1 <= concurrent <= 16:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise GrokError("配置参数超出有效区间：GROKBOT_TIMEOUT 需在 30～1800 秒内，GROKBOT_MAX_PENDING 需在 1～32 内，GROKBOT_MAX_CONCURRENT 需在 1～16 内。") from None
        persona = str(_env("GROKBOT_PERSONA_FILE", "") or "").strip()
        return cls(base_url=base, token=token, agent_id=agent_id, timeout=timeout, max_pending=pending,
                   max_concurrent=concurrent, persona_file=Path(persona) if persona else cls.persona_file)

    def persona(self) -> str:
        try:
            with self.persona_file.open(encoding="utf-8-sig") as stream:
                text = stream.read(20001).strip()
        except (OSError, UnicodeError):
            raise GrokError("Grok Bot 预设人设加载失败，请管理员核查 GROKBOT_PERSONA_FILE 路径与编码格式。") from None
        if not text or len(text) > 20000:
            raise GrokError("Grok Bot 预设人设内容须为 1～20000 字的有效 UTF-8 文本。")
        return text
