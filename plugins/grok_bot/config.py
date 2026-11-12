from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from otae_bot.config.model import Settings, field as setting
from otae_bot.config.settings import plugin_settings

DEFAULT_PERSONA = Path(__file__).with_name("persona.md")


class GrokError(Exception):
    """A diagnostic safe to show in QQ; never include upstream bodies."""


class GatewayError(GrokError):
    """Transport metadata, without upstream response bodies or credentials."""

    def __init__(self, message: str, *, not_submitted: bool = False, retryable: bool = False,
                 responded: bool | None = None):
        super().__init__(message)
        self.not_submitted = not_submitted
        self.retryable = retryable
        # Whether response headers arrived before the failure; None when unknown.
        self.responded = responded


class GrokSettings(Settings):
    """Cursor Grok Bot 应用（/grok、/grokbot），经 Tailscale/SSH 私网网关直连；默认关闭，
    SuperUser 在目标群发送 /功能 开启 grok 后才可使用"""

    gateway_url: str = setting(default="", description="网关基址，不带 /v1 或 /api；固定绕过全局代理")
    gateway_token: str = setting(default="", description="gateway.json 中的 token 原文（不含 Bearer 前缀；密钥）")
    agent_id: str = setting(default="", description="可选：原 QQBOT UUID，仅供只读诊断；问答自动使用各会话独立 Bot")
    timeout: int = setting(default=300, description="输入提交和每轮接收的等待上限秒数（30–1800，分别计时）")
    max_pending: int = setting(default=8, description="等待提交或正在上传/提交的输入上限（1–32）")
    max_concurrent: int = setting(default=4, description="同时接收回复的会话数（1–16）")
    persona_file: str = setting(default="", description="留空使用 plugins/grok_bot/persona.md（花园多惠）；也可填 UTF-8 文件路径")


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
    def from_settings(cls, settings: GrokSettings | None = None) -> GrokConfig:
        settings = settings or plugin_settings("grok_bot", GrokSettings)
        base = settings.gateway_url.strip().rstrip("/")
        token = settings.gateway_token.strip()
        agent_id = settings.agent_id.strip()
        if not all((base, token)):
            raise GrokError("Grok Bot 尚未完成配置，请管理员在 entari.yml 的 plugins.grok_bot 下补充 gateway_url 与 gateway_token。")
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
            raise GrokError("plugins.grok_bot.gateway_url 须配置为网关基础地址（如 http://100.x.x.x:1340），请勿包含 /v1、/api 或设为 0.0.0.0。")
        try:
            agent_id = str(UUID(agent_id)) if agent_id else ""
        except ValueError:
            raise GrokError("plugins.grok_bot.agent_id 格式不符合规范，须为目标 Bot 的 UUID。") from None
        if not token.isascii() or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in token):
            raise GrokError("plugins.grok_bot.gateway_token 无效，请填写正确的网关 Token 原始内容。")
        timeout, pending, concurrent = settings.timeout, settings.max_pending, settings.max_concurrent
        if not 30 <= timeout <= 1800 or not 1 <= pending <= 32 or not 1 <= concurrent <= 16:
            raise GrokError("配置参数超出有效区间：plugins.grok_bot 的 timeout 需在 30～1800 秒内，max_pending 需在 1～32 内，max_concurrent 需在 1～16 内。")
        persona = settings.persona_file.strip()
        return cls(base_url=base, token=token, agent_id=agent_id, timeout=timeout, max_pending=pending,
                   max_concurrent=concurrent, persona_file=Path(persona) if persona else cls.persona_file)

    def persona(self) -> str:
        try:
            with self.persona_file.open(encoding="utf-8-sig") as stream:
                text = stream.read(20001).strip()
        except (OSError, UnicodeError):
            raise GrokError("Grok Bot 预设人设加载失败，请管理员核查 plugins.grok_bot.persona_file 路径与编码格式。") from None
        if not text or len(text) > 20000:
            raise GrokError("Grok Bot 预设人设内容须为 1～20000 字的有效 UTF-8 文本。")
        return text
