from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from otae_bot.config.model import Settings
from otae_bot.config.model import field as setting
from otae_bot.config.settings import environment, llm_profile, plugin_settings, system_proxy

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"


class HywSettings(Settings):
    """HYW 搜索问答（/q、/hyw、/何意味），见 docs/features/hyw_plugin.md"""

    config_source: str = setting(
        default="hyw", description="凭据来源：hyw = 本段；llm = 整组用 otae.llm；steam = 整组用 plugins.steamInfo 的 llm_*"
    )
    api_key: str = setting(default="", description="API Key（密钥）")
    base_url: str = setting(default="https://openrouter.ai/api/v1", description="OpenAI 兼容接口基址")
    model: str = setting(default="gemini-3.8-flash", description="模型名")
    credentials_file: str = setting(
        default="",
        description="Google 服务账号 JSON 路径（Vertex AI）；留空时看环境变量 GOOGLE_APPLICATION_CREDENTIALS",
    )
    vertex_location: str = setting(default="global", description="服务账号模式下的区域段")
    vertex_base_url: str = setting(default="", description="覆盖 Vertex 基址；留空按 project_id 自动推导")
    proxy: str = setting(default="", description="模型和用户图片下载代理；留空继承 HTTP(S)_PROXY，direct = 直连")
    search_proxy: str = setting(default="", description="搜索/网页读取代理；留空沿用 proxy，direct = 直连")
    render: bool = setting(default=True, description="是否出卡片图")
    search_provider: str = setting(default="ddgs", description="搜索：ddgs、jina 或 parallel")
    search_mode: str = setting(default="turbo", description="搜索模式")
    reader_engine: str = setting(default="browser", description="网页读取：browser 或 default")
    max_tool_images: int = setting(default=600, description="工具可取用的图片总数上限")
    max_reader_images: int = setting(default=30, description="网页读取保留的图片数上限")
    max_concurrent: int = setting(default=2, description="同时进行的问答数")
    timeout: float = setting(default=300, description="一次问答总超时秒数")
    request_timeout: float = setting(
        default=90, description="单次模型调用超时秒数；超时后至多再试 1 次，重试超时取 min(30, max(15, 本值/2))"
    )
    reasoning_mode: str = setting(default="auto", description="推理强度：auto（全程 medium）、high、medium、low")
    max_rounds: int = setting(default=10, description="每次提问的模型轮次上限（含最终作答；小于 1 回退 10）")
    render_timeout: float = setting(default=45, description="卡片绘制时限秒数；失败时改发纯文字答案")
    render_max_images: int = setting(default=16, description="送进卡片渲染的配图上限，按正文引用顺序保留前 N 张")
    render_max_edge: int = setting(default=1024, description="送渲染前配图最长边像素（64–1280）")
    home: str = setting(default="data/hyw-frontier", description="hyw-frontier 的数据目录")


def _positive(value: float, default: float) -> float:
    """Positive, finite seconds; anything else falls back to the default."""
    return value if math.isfinite(value) and value > 0 else default


def _at_least(value: int, default: int, minimum: int) -> int:
    """Integers below ``minimum`` are treated as invalid, not clamped."""
    return value if value >= minimum else default


def _proxy(value: str, fallback: str = "") -> str:
    value = value.strip()
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
    def from_settings(cls, settings: HywSettings | None = None) -> "HywConfig":
        settings = settings or plugin_settings("hyw", HywSettings)
        source = settings.config_source.strip().lower()
        if source not in {"hyw", "llm", "steam"}:
            raise ValueError("plugins.hyw.config_source 应为 hyw、llm 或 steam")
        if source == "hyw":
            api_key, base_url, model = settings.api_key, settings.base_url or cls.base_url, settings.model or cls.model
        else:
            group = llm_profile(source)
            api_key, base_url, model = group.api_key, group.base_url or DEEPSEEK_BASE_URL, group.model or DEEPSEEK_MODEL
        global_proxy = system_proxy()
        proxy = _proxy(settings.proxy, str(global_proxy["https"] or global_proxy["http"] or "").strip())
        search_proxy = _proxy(settings.search_proxy, proxy)
        base_url = base_url.strip().rstrip("/")
        model = model.strip()
        credentials_file = (settings.credentials_file.strip() or environment("GOOGLE_APPLICATION_CREDENTIALS"))
        vertex_location = settings.vertex_location.strip() or "global"
        vertex_base_url = settings.vertex_base_url.strip().rstrip("/")
        api_key = api_key.strip()
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
        home = settings.home.strip() or cls.home
        search_provider = settings.search_provider.strip().lower() or cls.search_provider
        if search_provider not in {"ddgs", "jina", "parallel"}:
            search_provider = cls.search_provider
        reader_engine = settings.reader_engine.strip().lower() or cls.reader_engine
        if reader_engine not in {"default", "browser"}:
            reader_engine = cls.reader_engine
        reasoning_mode = settings.reasoning_mode.strip().lower() or cls.reasoning_mode
        if reasoning_mode not in {"auto", "high", "medium", "low"}:
            reasoning_mode = cls.reasoning_mode
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            proxy=proxy,
            search_proxy=search_proxy,
            render=settings.render,
            timeout=max(1.0, settings.timeout),
            request_timeout=max(1.0, settings.request_timeout),
            reasoning_mode=reasoning_mode,
            max_rounds=_at_least(settings.max_rounds, cls.max_rounds, 1),
            render_timeout=_positive(settings.render_timeout, cls.render_timeout),
            render_max_images=_at_least(settings.render_max_images, cls.render_max_images, 0),
            # The render worker accepts at most 1280px previews; tiny edges would be useless.
            render_max_edge=min(1280, _at_least(settings.render_max_edge, cls.render_max_edge, 64)),
            max_concurrent=max(1, settings.max_concurrent),
            max_tool_images=max(0, settings.max_tool_images),
            max_reader_images=max(0, settings.max_reader_images),
            search_provider=search_provider,
            search_mode=settings.search_mode.strip() or cls.search_mode,
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
