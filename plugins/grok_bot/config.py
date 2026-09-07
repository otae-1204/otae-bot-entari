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

    def __init__(self, message: str, *, not_submitted: bool = False):
        super().__init__(message)
        self.not_submitted = not_submitted


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
            raise GrokError("Grok Bot 尚未配置。请管理员填写 GROKBOT_GATEWAY_URL 和 GROKBOT_GATEWAY_TOKEN。")
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
            agent_id = str(UUID(agent_id)) if agent_id else ""
        except ValueError:
            raise GrokError("GROKBOT_AGENT_ID 应为目标 Bot 的 UUID。") from None
        if not token.isascii() or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token):
            raise GrokError("GROKBOT_GATEWAY_TOKEN 格式无效，请填写网关 Token 原文。")
        try:
            timeout = int(str(_env("GROKBOT_TIMEOUT", 300)))
            pending = int(str(_env("GROKBOT_MAX_PENDING", 8)))
            concurrent = int(str(_env("GROKBOT_MAX_CONCURRENT", 4)))
            if not 30 <= timeout <= 1800 or not 1 <= pending <= 32 or not 1 <= concurrent <= 16:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise GrokError("GROKBOT_TIMEOUT 应为 30～1800 秒，GROKBOT_MAX_PENDING 应为 1～32，GROKBOT_MAX_CONCURRENT 应为 1～16。") from None
        persona = str(_env("GROKBOT_PERSONA_FILE", "") or "").strip()
        return cls(base_url=base, token=token, agent_id=agent_id, timeout=timeout, max_pending=pending,
                   max_concurrent=concurrent, persona_file=Path(persona) if persona else cls.persona_file)

    def persona(self) -> str:
        try:
            with self.persona_file.open(encoding="utf-8-sig") as stream:
                text = stream.read(20001).strip()
        except (OSError, UnicodeError):
            raise GrokError("Grok Bot 人设模板读取失败，请管理员检查 GROKBOT_PERSONA_FILE 和文件编码。") from None
        if not text or len(text) > 20000:
            raise GrokError("Grok Bot 人设模板须为 1～20000 字的 UTF-8 文本。")
        return text
