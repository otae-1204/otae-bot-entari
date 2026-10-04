"""素材通道（图片/图标/头像）的策略：超时、重试、按主机熔断、代理与主机改写、整卡截止时间。

素材与 API 请求分走两条通道（见 client.py）：一个图床不可达时，素材请求只占自己的
并发槽位，签到、绑定等 API 请求不受影响。这里只放策略与状态，不发网络请求。

配置全部来自环境变量（.env 由 otae_bot.config.settings 载入 os.environ），首次使用时
读取；默认值保持原有安全行为：不走代理、trust_env=False、不改写主机。配置了代理时
默认「先直连、连不上再走代理」（PROXY_MODE=fallback）。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx
from loguru import logger


ENV_PREFIX = "OTAE_HTTP_ASSET_"
_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
PROXY_MODES = ("always", "fallback")
# 内容地址固定（路径带哈希）的图床：磁盘缓存按这里的秒数算新鲜期，不再每 10 分钟校验。
DEFAULT_HOST_MAX_AGE = (("hycdn.cn", 7 * 86400.0),)


@dataclass(frozen=True, slots=True)
class AssetFetchSettings:
    connect_timeout: float = 4.0
    read_timeout: float = 10.0
    retries: int = 1
    concurrency: int = 8
    breaker_threshold: int = 3
    breaker_cooldown: float = 300.0
    render_budget: float = 25.0
    proxy: str = ""
    proxy_hosts: tuple[str, ...] = ()
    # always：代理主机一律走代理；fallback：先直连，连接失败 / 连接超时才改走代理。
    proxy_mode: str = "fallback"
    # fallback 模式下某主机直连失败后，这么多秒内直接走代理，过后放一个直连探测。
    direct_cooldown: float = 300.0
    trust_env: bool = False
    host_rewrites: tuple[tuple[str, str], ...] = ()
    host_max_age: tuple[tuple[str, float], ...] = DEFAULT_HOST_MAX_AGE

    @property
    def attempts(self) -> int:
        return self.retries + 1


def _number(environ: Mapping[str, str], name: str, default, cast, minimum, maximum=None):
    raw = str(environ.get(ENV_PREFIX + name, "") or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning(f"[http-assets] {ENV_PREFIX}{name} is not a number; using {default}")
        return default
    value = max(minimum, value)
    return value if maximum is None else min(maximum, value)


def _flag(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(environ.get(ENV_PREFIX + name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _hosts(raw: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip().lower().rstrip(".")
            for item in raw.replace(";", ",").split(",")
            if item.strip()
        )
    )


def _rewrites(raw: str) -> tuple[tuple[str, str], ...]:
    """``a.example=b.example,c=d`` 或 JSON 对象；只改写主机名，不改路径。"""
    raw = raw.strip()
    if not raw:
        return ()
    pairs: list[tuple[str, str]] = []
    if raw.startswith("{"):
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[http-assets] {ENV_PREFIX}HOST_REWRITE is not valid JSON; ignored")
            return ()
        if isinstance(mapping, dict):
            pairs = [(str(key), str(value)) for key, value in mapping.items()]
    else:
        for item in raw.replace(";", ",").split(","):
            source, sep, target = item.partition("=")
            if sep:
                pairs.append((source, target))
    cleaned = {
        source.strip().lower(): target.strip().lower()
        for source, target in pairs
        if source.strip() and target.strip() and source.strip().lower() != target.strip().lower()
    }
    return tuple(cleaned.items())


def _proxy(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"  # 与 httpx 处理 HTTP(S)_PROXY 的方式一致，如 127.0.0.1:7897
    if urlsplit(raw).scheme.lower() not in _PROXY_SCHEMES:
        # 不把取值写进日志：代理地址可能带口令。
        logger.warning(f"[http-assets] {ENV_PREFIX}PROXY has an unsupported scheme; ignored")
        return ""
    return raw


def _proxy_mode(raw: str, default: str) -> str:
    raw = raw.strip().lower()
    if not raw:
        return default
    if raw not in PROXY_MODES:
        logger.warning(
            f"[http-assets] {ENV_PREFIX}PROXY_MODE must be one of {'/'.join(PROXY_MODES)}; using {default}"
        )
        return default
    return raw


def _host_max_age(raw: str, default) -> tuple[tuple[str, float], ...]:
    """``hycdn.cn=604800,example.com=86400``；按主机后缀匹配，``off`` 表示全部按默认 TTL。"""
    raw = raw.strip()
    if not raw:
        return default
    if raw.lower() in {"off", "none", "0"}:
        return ()
    pairs: dict[str, float] = {}
    for item in raw.replace(";", ",").split(","):
        suffix, sep, seconds = item.partition("=")
        suffix = suffix.strip().lower().lstrip("*.").rstrip(".")
        try:
            value = float(seconds) if sep and suffix else None
        except ValueError:
            value = None
        if value is None:
            logger.warning(f"[http-assets] {ENV_PREFIX}HOST_MAX_AGE entry ignored: {item.strip()[:64]}")
            continue
        if value > 0:
            pairs[suffix] = min(value, 365 * 86400.0)
    return tuple(pairs.items())


def load_asset_settings(environ: Mapping[str, str] | None = None) -> AssetFetchSettings:
    env = os.environ if environ is None else environ
    defaults = AssetFetchSettings()
    return AssetFetchSettings(
        connect_timeout=_number(env, "CONNECT_TIMEOUT", defaults.connect_timeout, float, 0.5, 60.0),
        read_timeout=_number(env, "READ_TIMEOUT", defaults.read_timeout, float, 1.0, 120.0),
        retries=_number(env, "RETRIES", defaults.retries, int, 0, 5),
        concurrency=_number(env, "CONCURRENCY", defaults.concurrency, int, 1, 64),
        breaker_threshold=_number(env, "BREAKER_THRESHOLD", defaults.breaker_threshold, int, 0),
        breaker_cooldown=_number(env, "BREAKER_COOLDOWN", defaults.breaker_cooldown, float, 1.0),
        render_budget=_number(env, "RENDER_BUDGET", defaults.render_budget, float, 0.0),
        proxy=_proxy(str(env.get(ENV_PREFIX + "PROXY", "") or "")),
        proxy_hosts=_hosts(str(env.get(ENV_PREFIX + "PROXY_HOSTS", "") or "")),
        proxy_mode=_proxy_mode(str(env.get(ENV_PREFIX + "PROXY_MODE", "") or ""), defaults.proxy_mode),
        direct_cooldown=_number(env, "DIRECT_COOLDOWN", defaults.direct_cooldown, float, 1.0, 86400.0),
        trust_env=_flag(env, "TRUST_ENV", defaults.trust_env),
        host_rewrites=_rewrites(str(env.get(ENV_PREFIX + "HOST_REWRITE", "") or "")),
        host_max_age=_host_max_age(
            str(env.get(ENV_PREFIX + "HOST_MAX_AGE", "") or ""), defaults.host_max_age
        ),
    )


_settings: AssetFetchSettings | None = None
_settings_lock = RLock()


def asset_settings() -> AssetFetchSettings:
    global _settings
    with _settings_lock:
        if _settings is None:
            _settings = load_asset_settings()
        return _settings


def configure_asset_settings(settings: AssetFetchSettings | None) -> None:
    """Replace the cached settings (None re-reads the environment on next use)."""
    global _settings, _breaker, _direct_routes
    with _settings_lock:
        _settings = settings
        _breaker = None
        _direct_routes = None


def rewrite_asset_url(url: str, settings: AssetFetchSettings | None = None) -> str:
    rewrites = (settings or asset_settings()).host_rewrites
    if not rewrites:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    target = dict(rewrites).get(host)
    if not target:
        return url
    netloc = target if parts.port is None else f"{target}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def host_max_age(host: str, settings: AssetFetchSettings | None = None) -> float | None:
    """Configured disk freshness for a content-addressed host (longest suffix wins)."""
    host = (host or "").lower().rstrip(".")
    best: tuple[str, float] | None = None
    for suffix, seconds in (settings or asset_settings()).host_max_age:
        if (host == suffix or host.endswith("." + suffix)) and (
            best is None or len(suffix) > len(best[0])
        ):
            best = (suffix, seconds)
    return None if best is None else best[1]


def proxied_host(host: str, settings: AssetFetchSettings | None = None) -> bool:
    """Whether the asset proxy applies to ``host``; same patterns as httpx mounts."""
    settings = settings or asset_settings()
    if not settings.proxy:
        return False
    if not settings.proxy_hosts:
        return True
    host = (host or "").lower()
    for pattern in settings.proxy_hosts:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif pattern.startswith("*"):
            if host == pattern[1:] or host.endswith("." + pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


# ---------------------------------------------------------------- 按主机熔断


class HostCircuitOpen(httpx.TransportError):
    """The asset host is in its cool-down window; no request was sent."""


CONNECT_FAILURES = (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError)
# Raised before the host was reached, or by us: says nothing about the host.
_NEUTRAL = (httpx.PoolTimeout, HostCircuitOpen)


@dataclass(slots=True)
class _HostState:
    failures: int = 0
    opened_at: float | None = None
    probing: bool = False


class HostCircuitBreaker:
    """连续 N 次连接失败后打开；冷却期内直接拒绝；冷却期过后只放一个探测请求（半开）。

    HTTP 状态（含 404）、读超时等说明主机可达，会清零计数并关闭熔断；
    连接超时 / 连接失败 / 代理失败才计数。日志只在打开和关闭时各记一次。
    """

    def __init__(
        self,
        *,
        threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = int(threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._hosts: dict[str, _HostState] = {}
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    def blocked(self, host: str) -> bool:
        """True while the host is open and still cooling down (read-only check)."""
        if not self.enabled:
            return False
        with self._lock:
            state = self._hosts.get(host)
            if state is None or state.opened_at is None:
                return False
            if state.probing:
                return True
            return self._clock() - state.opened_at < self.cooldown_seconds

    def acquire(self, host: str) -> bool:
        """Call right before sending. Grants at most one half-open probe per host."""
        if not self.enabled:
            return True
        with self._lock:
            state = self._hosts.get(host)
            if state is None or state.opened_at is None:
                return True
            if state.probing or self._clock() - state.opened_at < self.cooldown_seconds:
                return False
            state.probing = True
            return True

    def record(self, host: str, error: BaseException | None) -> None:
        if not self.enabled:
            return
        with self._lock:
            state = self._hosts.get(host)
            if error is not None and isinstance(error, CONNECT_FAILURES):
                self._failure(host, state or self._hosts.setdefault(host, _HostState()), error)
            elif error is not None and (
                isinstance(error, _NEUTRAL) or not isinstance(error, httpx.HTTPError)
            ):
                # Cancelled / local errors: free the probe slot, keep the verdict.
                if state is not None:
                    state.probing = False
            elif state is not None:
                if state.opened_at is not None:
                    logger.info(
                        f"[http-assets] host recovered, circuit closed host={host}"
                    )
                self._hosts.pop(host, None)

    def _failure(self, host: str, state: _HostState, error: BaseException) -> None:
        now = self._clock()
        if state.opened_at is not None:
            # A failed half-open probe re-arms the cool-down silently.
            if state.probing:
                state.probing = False
                state.opened_at = now
                logger.debug(f"[http-assets] probe failed, circuit stays open host={host}")
            return
        state.failures += 1
        if state.failures >= self.threshold:
            state.opened_at = now
            logger.warning(
                f"[http-assets] circuit opened host={host} "
                f"after {state.failures} connect failures ({type(error).__name__}); "
                f"skipping for {self.cooldown_seconds:.0f}s"
            )

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                host: {
                    "failures": state.failures,
                    "open": state.opened_at is not None,
                    "probing": state.probing,
                }
                for host, state in self._hosts.items()
            }


_breaker: HostCircuitBreaker | None = None


def asset_breaker() -> HostCircuitBreaker:
    global _breaker
    with _settings_lock:
        if _breaker is None:
            settings = asset_settings()
            _breaker = HostCircuitBreaker(
                threshold=settings.breaker_threshold,
                cooldown_seconds=settings.breaker_cooldown,
            )
        return _breaker


def reset_asset_breaker() -> None:
    global _breaker
    with _settings_lock:
        _breaker = None


# ---------------------------------------------------------------- 先直连、失败再走代理

# 只有这两类说明「直连连不上」；读超时、HTTP 状态说明已经连上了，不改走代理。
DIRECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)


class DirectRoutes:
    """fallback 模式下按主机记住「直连不通」。

    直连失败后冷却期内该主机直接走代理；冷却期过后只放一个请求去探测直连，
    其余请求继续走代理，探测成功才恢复直连。日志只在切换时各记一次。
    """

    def __init__(self, *, cooldown_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._failed_at: dict[str, float] = {}
        self._probing: set[str] = set()
        self._lock = RLock()

    def try_direct(self, host: str) -> bool:
        """Call right before sending; False means go straight to the proxy."""
        with self._lock:
            failed_at = self._failed_at.get(host)
            if failed_at is None:
                return True
            if host in self._probing or self._clock() - failed_at < self.cooldown_seconds:
                return False
            self._probing.add(host)
            return True

    def record(self, host: str, reachable: bool | None) -> None:
        """``reachable=None``: cancelled or local error, says nothing about the route."""
        with self._lock:
            self._probing.discard(host)
            if reachable is None:
                return
            if reachable:
                if self._failed_at.pop(host, None) is not None:
                    logger.info(f"[http-assets] direct connection works again host={host}")
                return
            if host not in self._failed_at:
                logger.warning(
                    f"[http-assets] direct connection failed host={host}; "
                    f"using the asset proxy for {self.cooldown_seconds:.0f}s"
                )
            self._failed_at[host] = self._clock()

    def snapshot(self) -> dict[str, bool]:
        with self._lock:
            return {host: host in self._probing for host in self._failed_at}


_direct_routes: DirectRoutes | None = None


def asset_direct_routes() -> DirectRoutes:
    global _direct_routes
    with _settings_lock:
        if _direct_routes is None:
            _direct_routes = DirectRoutes(cooldown_seconds=asset_settings().direct_cooldown)
        return _direct_routes


# ---------------------------------------------------------------- 整卡截止时间

_render_deadline: ContextVar[float | None] = ContextVar(
    "asset_render_deadline", default=None
)


@contextmanager
def asset_render_budget(seconds: float | None = None) -> Iterator[None]:
    """Share one wall-clock budget across every asset batch fetched in this block.

    嵌套时只会收紧，不会延长外层的截止时间；预算 <= 0 表示不限时。
    """
    budget = asset_settings().render_budget if seconds is None else float(seconds)
    current = _render_deadline.get()
    deadline = current
    if budget > 0:
        deadline = time.monotonic() + budget
        if current is not None:
            deadline = min(deadline, current)
    token = _render_deadline.set(deadline)
    try:
        yield
    finally:
        _render_deadline.reset(token)


def batch_deadline(budget_seconds: float | None = None) -> float | None:
    """Absolute ``time.monotonic()`` deadline for one batch: card budget or a fresh one."""
    current = _render_deadline.get()
    budget = asset_settings().render_budget if budget_seconds is None else budget_seconds
    own = time.monotonic() + budget if budget > 0 else None
    if current is None:
        return own
    if own is None or budget_seconds is None:
        return current
    return min(current, own)
