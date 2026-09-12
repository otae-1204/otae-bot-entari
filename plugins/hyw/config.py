from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from otae_bot.config.settings import SYSTEM_PROXY, _env

VERTEX_ENDPOINT = "https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/endpoints/openapi"
_warned: set[str] = set()


def _env_int(name: str, default: int) -> int:
    """Read an integer setting; missing or malformed values fall back to the default.

    ``_env`` runs values through ``json.loads``, so a legitimate ``0`` arrives as the
    falsy int ``0``: compare against None rather than testing truthiness, or ``0``
    would be indistinguishable from "unset".
    """
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


@dataclass(frozen=True)
class HywConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "gpt-4o"
    proxy: str = field(default="", repr=False)
    search_proxy: str | None = field(default=None, repr=False)
    render: bool = True
    timeout: float = 120
    max_turns: int = 10
    max_tools: int = 8
    # 凭据模式：api_key=静态密钥（默认，行为与旧版一致）；service_account=Google 服务账号。
    auth_mode: str = "api_key"
    credentials_file: str = ""
    vertex_location: str = "global"
    vertex_base_url: str = ""
    auth_error: str = ""
    # 瞬时失败（429/5xx/超时/连接中断）的退避重试；attempts<=1 或 budget<=0 表示关闭。
    retry_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    retry_budget: float = 20.0

    @property
    def configured(self) -> bool:
        """当前凭据模式是否可用；不可用时由调用点给出聊天提示。"""
        if self.auth_mode == "service_account":
            return bool(self.credentials_file)
        if self.auth_mode == "api_key":
            return bool(self.api_key)
        return False

    @classmethod
    def from_env(cls) -> HywConfig:
        # Read the complete credential/endpoint/model tuple from one source.
        source = str(_env("HYW_CONFIG_SOURCE", "hyw")).strip().lower()
        prefixes = {"hyw": "HYW", "llm": "LLM", "steam": "STEAM_LLM"}
        if source not in prefixes:
            raise ValueError("HYW_CONFIG_SOURCE 应为 hyw、llm 或 steam")
        prefix = prefixes[source]
        default_base = cls.base_url if source == "hyw" else "https://api.deepseek.com"
        default_model = cls.model if source == "hyw" else "deepseek-v4-flash"
        proxy = str(_env("HYW_PROXY", "") or "").strip()
        if proxy.lower() == "direct":
            proxy = ""
        elif not proxy:
            proxy = str(SYSTEM_PROXY.get("https") or SYSTEM_PROXY.get("http") or "").strip()
        search_proxy = str(_env("HYW_SEARCH_PROXY", "") or "").strip()
        if search_proxy.lower() == "direct":
            search_proxy = ""
        elif not search_proxy:
            search_proxy = proxy
        explicit_base = str(_env(f"{prefix}_BASE_URL", "") or "").strip()
        base_url = (explicit_base or default_base).rstrip("/")
        model = str(_env(f"{prefix}_MODEL", "") or default_model).strip()
        credentials_file = str(_env("HYW_CREDENTIALS_FILE", "") or _env("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
        vertex_location = str(_env("HYW_VERTEX_LOCATION", "") or "").strip() or "global"
        vertex_base_url = str(_env("HYW_VERTEX_BASE_URL", "") or "").strip().rstrip("/")
        auth_mode, auth_error = ("api_key" if str(_env(f"{prefix}_API_KEY", "") or "").strip() else "none"), ""
        if credentials_file:
            auth_mode, auth_error = cls._apply_service_account(credentials_file, vertex_location, vertex_base_url)
            if auth_mode == "service_account":
                # Vertex 的地址形态无法由「根地址 + /chat/completions」拼出，必须整体推导；
                # 显式设置的中转地址在这里会被忽略，避免把服务账号令牌发往 OpenAI 中转。
                base_url = cls._vertex_base_url(credentials_file, vertex_location, vertex_base_url, explicit_base)
                model = model if "/" in model else f"google/{model}"
        return cls(
            api_key=str(_env(f"{prefix}_API_KEY", "") or "").strip(),
            base_url=base_url,
            model=model,
            proxy=proxy,
            search_proxy=search_proxy,
            render=str(_env("HYW_RENDER", True)).lower() not in {"false", "0", "no"},
            auth_mode=auth_mode,
            credentials_file=credentials_file,
            vertex_location=vertex_location,
            vertex_base_url=vertex_base_url,
            auth_error=auth_error,
            retry_attempts=max(1, _env_int("HYW_RETRY_ATTEMPTS", cls.retry_attempts)),
            retry_base_delay=max(0.0, _env_float("HYW_RETRY_BASE_DELAY", cls.retry_base_delay)),
            retry_max_delay=max(0.0, _env_float("HYW_RETRY_MAX_DELAY", cls.retry_max_delay)),
            retry_budget=max(0.0, _env_float("HYW_RETRY_BUDGET", cls.retry_budget)),
        )

    @property
    def retry(self) -> RetryPolicy:
        """本次配置对应的退避重试策略。"""
        from .retry import RetryPolicy

        return RetryPolicy(
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            budget=self.retry_budget,
        )

    @staticmethod
    def _apply_service_account(credentials_file: str, vertex_location: str, vertex_base_url: str) -> tuple[str, str]:
        """校验凭据文件；不可用时降级为 none 并保留可展示的原因，不抛异常。"""
        from .google_auth import load_service_account

        try:
            load_service_account(credentials_file)
        except HywError as error:
            return "none", str(error)
        return "service_account", ""

    @staticmethod
    def _vertex_base_url(credentials_file: str, vertex_location: str, vertex_base_url: str, explicit_base: str) -> str:
        from .google_auth import load_service_account

        if vertex_base_url:
            return vertex_base_url
        project = load_service_account(credentials_file).project_id
        derived = VERTEX_ENDPOINT.format(project=project, location=vertex_location)
        if explicit_base and "aiplatform.googleapis.com" not in explicit_base and "HYW_BASE_URL" not in _warned:
            # 只记录「已忽略」这一事实，不记录被忽略的值。
            _warned.add("HYW_BASE_URL")
            logger.warning("[hyw] service account credentials in use: HYW_BASE_URL is ignored, endpoint derived from the credentials file")
        return derived


class HywError(Exception):
    """An error whose message is safe to send to the chat."""
