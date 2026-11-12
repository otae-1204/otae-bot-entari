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

from .core import CoreSettings, LlmSettings
from .model import Settings, validate

__all__ = [
    "CONFIG_FILE",
    "PROCESS_ENV",
    "Config",
    "ConfigFileMissing",
    "core_settings",
    "environment",
    "llm_profile",
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


def llm_profile(name: str) -> LlmSettings:
    """A named LLM credential group: ``llm`` is ``otae.llm``; ``steam`` is the
    ``llm_*`` keys of ``plugins.steamInfo`` (shared with the plugins that may borrow it)."""
    if name == "steam":
        key = "plugins.steamInfo"
        raw = _source.plugins.get(key) or {}
        raw = raw if isinstance(raw, Mapping) else {}
        group = {field: raw[f"llm_{field}"] for field in ("api_key", "base_url", "model") if f"llm_{field}" in raw}
        return _section(LlmSettings, f"{key}#llm", group, key)
    if name == "llm":
        return core_settings().llm
    raise ValueError(f"unknown LLM profile: {name}")


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

