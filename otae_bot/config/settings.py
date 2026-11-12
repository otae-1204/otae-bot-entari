"""The bot's configuration: ``entari.yml`` for settings, ``.env`` for secrets.

``load_config()`` is called once by ``bot.py`` before anything else is imported, so
modules that read settings at import time see the configured values. Until it runs
(tests, scripts, ``python -c``) every accessor returns the model defaults.

* ``basic:`` is Entari's own section (connections, prefix, superusers, log).
* ``otae:`` holds the core settings (:class:`otae_bot.config.core.CoreSettings`).
* ``plugins: plugins.<目录名>:`` holds each plugin's settings, declared as a
  :class:`~otae_bot.config.model.Settings` model next to the plugin and read with
  :func:`plugin_settings`.
* ``plugins: server:`` declares the in-process OneBot boundary (entari-plugin-server).

Secrets stay in ``.env`` (or the real environment, which wins) and are referenced from
``entari.yml`` as ``${{ env.NAME :- }}``. Entari rewrites the configuration file it
loaded when the bot exits and drops placeholders whose value is empty, so the bot
loads a private copy under ``.runtime/`` and the operator's ``entari.yml`` is never
written. A few variables are read straight from the process environment by
third-party code (proxies, TLS bundles, vendored hyw-frontier); those listed in
:data:`PROCESS_ENV` are copied from ``.env`` into ``os.environ`` when missing there.

Only this package may read the environment directly (guard rule R8).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from arclet.entari.config import EntariConfig

from .core import CoreSettings
from .model import Settings, validate

__all__ = [
    "CONFIG_FILE",
    "PROCESS_ENV",
    "Config",
    "ConfigFileMissing",
    "core_settings",
    "environment",
    "load_config",
    "loaded_config",
    "network_endpoints",
    "override",
    "plugin_settings",
    "server_declaration",
    "system_proxy",
]

S = TypeVar("S", bound=Settings)

CONFIG_FILE = Path("entari.yml")
RUNTIME_DIR = Path(".runtime")
SERVER_KEY = "server"
CORE_KEY = "otae"

#: Read from the process environment by third-party or vendored code, not by us
#: through entari.yml. ``load_config`` copies them from ``.env`` when unset.
PROCESS_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "JINA_API_KEY",
    "PARALLEL_API_KEY",
    "HYW_FRONTIER_HOME",
    "HYW_OPENCODE_SESSION",
    "HYW_RETRY_ATTEMPTS",
    "HYW_RETRY_BASE_DELAY",
    "HYW_RETRY_MAX_DELAY",
    "HYW_RETRY_BUDGET",
)


class ConfigFileMissing(FileNotFoundError):
    """``entari.yml`` does not exist (copy ``entari.example.yml`` or run the converter)."""


class Config:
    """Bot operators, platform-independent: every id under ``basic.superusers``.

    Entari keys superusers by platform; the bot has always treated an operator id as
    valid on every account, so this is the union of all lists.
    """

    SUPERUSERS: list[str] = []


class _Source:
    def __init__(self, data: Mapping[str, Any] | None = None, plugins: Mapping[str, Any] | None = None,
                 config: EntariConfig | None = None):
        self.data: Mapping[str, Any] = data or {}
        self.plugins: Mapping[str, Any] = plugins if plugins is not None else (self.data.get("plugins") or {})
        self.config = config
        self.cache: dict[tuple[str, type], Settings] = {}


_source = _Source()


def _install(source: _Source) -> None:
    global _source
    _source = source
    basic = source.data.get("basic") or {}
    superusers = basic.get("superusers") or {}
    ids: dict[str, None] = {}
    if isinstance(superusers, Mapping):
        for values in superusers.values():
            for value in values if isinstance(values, (list, tuple)) else [values]:
                if str(value).strip():
                    ids[str(value).strip()] = None
    Config.SUPERUSERS = list(ids)


def load_config(path: Path = CONFIG_FILE, runtime_dir: Path = RUNTIME_DIR) -> EntariConfig:
    """Load ``entari.yml`` (through a private copy) and make it the bot's configuration."""
    if not path.is_file():
        raise ConfigFileMissing(str(path))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / path.name
    staging = runtime_dir / f".{path.name}.{os.getpid()}.tmp"
    staging.write_bytes(path.read_bytes())
    os.replace(staging, target)
    config = EntariConfig.load(target)
    for name in PROCESS_ENV:
        value = config.env_vars.get(name)
        if value and name not in os.environ:
            os.environ[name] = value
    _install(_Source(config.data, config.plugin, config))
    return config


def loaded_config() -> EntariConfig | None:
    """The loaded Entari configuration, or None before :func:`load_config`."""
    return _source.config


@contextmanager
def override(data: Mapping[str, Any]) -> Iterator[None]:
    """Use ``data`` (shaped like entari.yml: basic/otae/plugins) as the configuration; for tests."""
    previous = _source
    _install(_Source(copy.deepcopy(dict(data))))
    try:
        yield
    finally:
        _install(previous)


def _section(model: type[S], key: str, raw: Any, where: str) -> S:
    cached = _source.cache.get((key, model))
    if cached is None:
        cached = validate(model, raw if isinstance(raw, Mapping) else {}, where)
        _source.cache[(key, model)] = cached
    return cached  # type: ignore[return-value]


def plugin_settings(name: str, model: type[S]) -> S:
    """Settings of plugin ``plugins/<name>/`` from ``plugins: plugins.<name>:``."""
    key = f"plugins.{name}"
    return _section(model, key, _source.plugins.get(key), key)


def core_settings() -> CoreSettings:
    return _section(CoreSettings, CORE_KEY, _source.data.get(CORE_KEY), CORE_KEY)


def server_declaration() -> dict | None:
    """A copy of ``plugins: server:`` (the OneBot boundary), or None when not declared."""
    raw = _source.plugins.get(SERVER_KEY)
    if raw is None:
        return None
    return copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}


def network_endpoints() -> list[str]:
    """``host:port`` of each Satori connection under ``basic.network``."""
    basic = _source.data.get("basic") or {}
    endpoints = []
    for entry in basic.get("network") or []:
        if isinstance(entry, Mapping):
            endpoints.append(f"{entry.get('host', 'localhost')}:{entry.get('port', '')}")
    return endpoints


def environment(name: str) -> str:
    """A process environment variable (see :data:`PROCESS_ENV`), stripped; '' when unset."""
    return str(os.environ.get(name, "") or "").strip()


def system_proxy() -> dict[str, str | None]:
    """The process-wide HTTP(S) proxy (``HTTP_PROXY`` / ``HTTPS_PROXY``)."""
    return {
        "http": environment("HTTP_PROXY") or None,
        "https": environment("HTTPS_PROXY") or None,
    }


# ── Transitional: the legacy .env parser, until every plugin reads typed settings ──
# Removed once the last plugin is migrated; nothing new may use it.
import json  # noqa: E402
from typing import Dict, List  # noqa: E402


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
        os.environ.setdefault(_k, json.dumps(_v))


def _env(key: str, default: Any = None) -> Any:
    raw = os.getenv(key)
    if raw is not None:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return _dotenv.get(key, default)


SYSTEM_PROXY: dict = {
    "http": str(_env("HTTP_PROXY", "") or "").strip() or None,
    "https": str(_env("HTTPS_PROXY", "") or "").strip() or None,
}
MCSM_PANEL_URL: str = _env("MCSM_PANEL_URL", "http://127.0.0.1:23333")
MCSM_API_KEY: str = _env("MCSM_API_KEY", "")
MCSM_GROUP_WHITELIST: List[str] = [str(g) for g in _env("MCSM_GROUP_WHITELIST", [])]
MC_BROADCAST_INTERVAL: int = int(_env("MC_BROADCAST_INTERVAL", "300"))
STEAM_API_KEYS: List[str] = (
    _env("STEAM_API_KEYS", "").split(",")
    if _env("STEAM_API_KEYS", "")
    else []
)
STEAM_REQUEST_INTERVAL: int = int(_env("STEAM_REQUEST_INTERVAL", "60"))


def openJson(json_file: Path) -> Dict[str, Any]:
    if not json_file.parent.exists():
        json_file.parent.mkdir(parents=True, exist_ok=True)
    if not json_file.exists():
        json_file.write_text("{}", encoding="utf-8")
    return json.loads(json_file.read_text(encoding="utf-8"))


def saveJson(json_file: Path, data: dict) -> None:
    json_file.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


class Plugin_Config:
    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.plugin_config_path = Path("configs") / f"{plugin_name}/config.json"
        self.plugin_content = openJson(self.plugin_config_path)

    def update(self):
        saveJson(self.plugin_config_path, self.plugin_content)
