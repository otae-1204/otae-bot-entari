"""Project-local model credentials and endpoints; never load Pi/Codex credentials."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import getpass
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from .errors import FrontierError

PROVIDERS = ("deepseek", "openai", "anthropic", "google", "openai-compatible")
_DEFAULTS = {
    "deepseek": ("responses", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openai": ("responses", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "google": ("google", None, "GOOGLE_API_KEY"),
    "openai-compatible": ("chat", None, "OPENAI_COMPATIBLE_API_KEY"),
}


@dataclass(frozen=True)
class ModelConnection:
    provider: str
    api: str
    base_url: str | None
    api_key: str = field(repr=False)
    max_output_tokens: int | None = None
    service_account: dict | None = field(default=None, repr=False)
    location: str = "global"


class CredentialStore:
    def __init__(self, home: Path, timeout=300, cancel_event=None):
        self.home = home
        self.timeout = timeout
        self.cancel_event = cancel_event

    def _load(self, filename: str) -> dict:
        try:
            data = json.loads((self.home / filename).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            raise FrontierError(f"{filename} 配置无效；未覆盖原文件。") from None

    @contextmanager
    def locked(self):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home.chmod(0o700)
        fd = os.open(self.home / "auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
            else:
                import fcntl
            deadline = time.monotonic() + self.timeout
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise FrontierError("请求已取消。")
                try:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FrontierError("凭据正被另一进程使用，请稍后重试。") from None
                    time.sleep(.05)
            yield fd
        finally:
            os.close(fd)

    def _save(self, data: dict):
        fd, name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=self.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.home / "auth.json")
        finally:
            Path(name).unlink(missing_ok=True)

    def status(self):
        return [{"providerId": name, "type": entry.get("type"),
                 "supported": name in PROVIDERS and (entry.get("type") == "api_key"
                               or (name == "google" and entry.get("type") == "service_account"))}
                for name, entry in self._load("auth.json").items() if isinstance(entry, dict)]

    def login(self, provider: str, method: str):
        if provider not in PROVIDERS or method != "api_key":
            raise FrontierError("纯 Python 接入仅支持 API Key；旧 OAuth 凭据保留但不使用。")
        key = getpass.getpass(f"{provider} API Key（隐藏输入）: ").strip()
        if not key:
            raise FrontierError("API Key 不能为空。")
        with self.locked():
            data = self._load("auth.json")
            data[provider] = {"type": "api_key", "key": key}
            self._save(data)
        print("API Key 已保存到 Hyw-Frontier 独立目录（未联网验证权限）。")

    def logout(self, provider: str):
        with self.locked():
            data = self._load("auth.json")
            data.pop(provider, None)
            self._save(data)
        return {"provider": provider, "loggedOut": True}

    def model_config(self, provider: str) -> dict:
        config = self._load("models.json").get(provider, {})
        allowed = {"api", "base_url", "api_key_env", "model", "label", "max_output_tokens"}
        if provider == "google":
            allowed.add("location")
        if not isinstance(config, dict) or set(config) - allowed:
            raise FrontierError("models.json 提供商设置仅支持 api、base_url、api_key_env、model、label、max_output_tokens；google 另支持 location。")
        if "location" in config:
            location = config["location"]
            if (not isinstance(location, str) or not 1 <= len(location) <= 63
                    or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in location)):
                raise FrontierError("Google location 必须是有效区域名称，例如 global、us 或 us-central1。")
        for name, maximum in (("model", 150), ("label", 100)):
            value = config.get(name)
            if name in config and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
                raise FrontierError(f"models.json 的 {name} 必须是1–{maximum}字符的字符串。")
        limit = config.get("max_output_tokens")
        if "max_output_tokens" in config and (type(limit) is not int or limit < 1):
            raise FrontierError("max_output_tokens 必须是正整数（部署的输出预算，不是上下文长度）。")
        return config

    def provider_presets(self) -> dict:
        # Only expose presentation fields, never endpoint credentials or environment names.
        return {provider: {key: value for key, value in self.model_config(provider).items()
                           if key in ("model", "label")} for provider in PROVIDERS}

    def connection(self, provider: str, *, api=None, base_url=None, api_key=None) -> ModelConnection:
        if provider not in PROVIDERS:
            raise FrontierError("不支持该提供商；旧 openai-codex OAuth 不再用于模型请求，请配置 API Key。")
        default_api, default_url, key_env = _DEFAULTS[provider]
        config = self.model_config(provider)
        api = api if api is not None else config.get("api", default_api)
        base_url = base_url if base_url is not None else config.get("base_url", default_url)
        if api not in ("responses", "chat", "anthropic", "google"):
            raise FrontierError("不支持的模型 API 协议。")
        if provider == "deepseek" and api != "responses":
            raise FrontierError("DeepSeek 固定使用 Responses API，不回退 Chat Completions。")
        if ((provider in ("anthropic", "google") and api != default_api)
                or (provider in ("openai", "openai-compatible") and api not in ("responses", "chat"))):
            raise FrontierError("提供商与 API 协议不匹配。")
        if base_url is not None:
            try:
                parts = urlsplit(base_url)
                _ = parts.port
                if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                        or parts.password or parts.query or parts.fragment or any(c.isspace() for c in base_url)):
                    raise ValueError
            except (ValueError, TypeError):
                raise FrontierError("base_url 必须是无凭据、查询参数或片段的 HTTP(S) 地址。") from None
        elif provider == "openai-compatible":
            raise FrontierError("openai-compatible 需要显式 base_url 或 models.json 配置。")
        key_env = config.get("api_key_env", key_env)
        if not isinstance(key_env, str) or not key_env:
            raise FrontierError("api_key_env 必须是环境变量名称。")
        key = api_key if api_key is not None else os.environ.get(key_env, "").strip()
        if not key and provider == "google" and key_env == "GOOGLE_API_KEY":
            key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            entry = self._load("auth.json").get(provider, {})
            if not isinstance(entry, dict):
                raise FrontierError("模型凭据格式无效。")
            if provider == "google" and entry.get("type") == "service_account":
                required = ("project_id", "client_email", "private_key", "token_uri")
                if any(not isinstance(entry.get(name), str) or not entry[name].strip() for name in required):
                    raise FrontierError("Google 服务账号凭据不完整。")
                if entry["token_uri"] != "https://oauth2.googleapis.com/token" or base_url is not None:
                    raise FrontierError("Google 服务账号仅允许官方 OAuth 和 Vertex AI 端点。")
                return ModelConnection(provider, api, None, "", config.get("max_output_tokens"),
                                       service_account=entry, location=config.get("location", "global"))
            if entry and entry.get("type") != "api_key":
                raise FrontierError("已保存的凭据类型不受支持；请配置 API Key 或 Google 服务账号，原凭据未删除。")
            key = entry.get("key", "")
        if not isinstance(key, str) or not key.strip():
            raise FrontierError(f"未配置 {provider} API Key 凭据；请设置 {key_env} 或运行 login。",
                                diagnostics={"code": "credentials_missing", "retryable": False})
        return ModelConnection(provider, api, base_url, key.strip(), config.get("max_output_tokens"))
