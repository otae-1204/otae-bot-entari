"""素材通道（图片/图标/头像）的策略：超时、重试、按主机熔断、整卡截止时间。

素材与 API 请求分走两条通道（见 client.py）：一个图床不可达时，素材请求只占自己的
并发槽位，签到、绑定等 API 请求不受影响。这里只放策略与状态，不发网络请求。

配置全部来自环境变量（.env 由 otae_bot.config.settings 载入 os.environ），首次使用时读取。
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterator, Mapping

import httpx
from loguru import logger


ENV_PREFIX = "OTAE_HTTP_ASSET_"


@dataclass(frozen=True, slots=True)
class AssetFetchSettings:
    connect_timeout: float = 4.0
    read_timeout: float = 10.0
    retries: int = 1
    concurrency: int = 8
    breaker_threshold: int = 3
    breaker_cooldown: float = 300.0
    render_budget: float = 25.0

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
    global _settings, _breaker
    with _settings_lock:
        _settings = settings
        _breaker = None


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
