"""AI 智商雷达插件的配置层。

配置全部来自 ``RADAR_*`` 环境变量；默认值即「不配置也能跑」的一套只读参数。

数值型配置一律走 :func:`_env_int` / :func:`_env_float`：本仓库的 ``.env`` 加载器会把
``"0"`` 这类值经 ``json.loads`` 变成 int ``0``，用真值判断会把合法的 0 当成未设置
（``plugins/hyw/config.py`` 的同名函数就是这个坑的修复写法）。

**注意**：本模块的 dataclass 都是 ``slots=True``，所以类属性取到的是 slot 描述符而不是
默认值（``RadarConfig.base_url`` 不是字符串）。默认值因此放在模块级常量里，供
``from_env`` 显式引用。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from otae_bot.config.settings import _env

PLUGIN_VERSION = "1.0.0"
PROJECT_URL = "https://github.com/otae-1204/otae-bot-entari"
# 自有标识 UA：便于上游识别与联系。**不伪造** X-DRadar-Client-Version /
# X-DRadar-Capabilities / Authorization —— 公开读端点免鉴权，伪装既不诚实也无必要。
DEFAULT_USER_AGENT = f"otae-bot-radar/{PLUGIN_VERSION} (+{PROJECT_URL})"
DEFAULT_BASE_URL = "https://api.codexradar.com"
DEFAULT_BENCHMARK = "deep-swe"
DEFAULT_TIMEOUT = 20.0
DEFAULT_TABLE_TIMEOUT = 60.0
DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_MODELS_LISTED = 15
DEFAULT_MAX_TASKS_LISTED = 10
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_TABLE_MAX_BYTES = 24 * 1024 * 1024

#: 推理档位由低到高。``(model, effort)`` 才是可比的键，比较类接口必须带档位。
EFFORT_TIERS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")

_warned: set[str] = set()


def _env_int(name: str, default: int) -> int:
    """读整数配置；缺失或不可解析时回默认值。

    ``_env`` 会把值经 ``json.loads``，所以合法的 ``0`` 到达时是 falsy 的 int ``0``：
    必须与 ``None`` 比较，否则 ``0`` 与「未设置」无法区分。
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


def _env_str(name: str, default: str = "") -> str:
    raw = _env(name, None)
    if raw is None:
        return default
    return str(raw).strip()


@dataclass(frozen=True, slots=True)
class RadarRetry:
    """退避参数**对齐官方客户端**（``codex-radar/dradar`` v0.5.200 ``api_client.py``）。

    不自行发明退避参数：

    - 429：最多 5 次重试（共 6 次尝试）
    - ``Retry-After``：解析失败用 ``default_retry_after``；取值 clamp 到
      ``[min_wait, max_wait]`` = ``[1.0, 60.0]`` 秒
    - 等待 = ``clamp(retry_after) + uniform(0, min(1.0, retry_after * jitter_ratio))``
      —— 抖动防羊群
    - 503 + 维护栅栏（``deployment_maintenance``）：``Retry-After`` **不 clamp**，
      由 ``total_budget`` 裁决

    只读 GET 是幂等的，故重试安全。**不用**连接级重试（``HTTPTransport(retries=2)``）：
    本机常年开代理，代理层重连会放大故障（官方客户端也在检测到代理时禁用该重试）。
    """

    rate_limit_retries: int = 5
    min_wait: float = 1.0
    max_wait: float = 60.0
    default_retry_after: float = 5.0
    jitter_ratio: float = 0.1
    total_budget: float = 360.0


@dataclass(frozen=True, slots=True)
class RadarCacheTTL:
    """插件侧 TTL。

    **TTL 下限规则**：插件 TTL 不得短于上游 ``Cache-Control`` 的 ``max-age`` /
    ``s-maxage``（下表已按实测值取齐或更长），否则等于用我们的请求量替上游刷缓存。
    """

    benchmarks: float = 3600.0
    leaderboard: float = 60.0
    table: float = 300.0
    iq_history: float = 120.0
    radar_insights: float = 600.0
    intelligence_efficiency: float = 60.0
    model_metrics: float = 60.0
    events: float = 30.0
    quota: float = 3600.0
    suggest: float = 60.0


@dataclass(frozen=True, slots=True)
class RadarConfig:
    base_url: str = DEFAULT_BASE_URL
    #: 保留字段：共享 HTTP 层固定 ``trust_env=False`` 且不挂代理，故本值当前不生效，
    #: 仅用于记录部署意图（见 ``from_env`` 的告警）。
    proxy: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    table_timeout: float = DEFAULT_TABLE_TIMEOUT
    default_benchmark: str = DEFAULT_BENCHMARK
    max_models_listed: int = DEFAULT_MAX_MODELS_LISTED
    max_tasks_listed: int = DEFAULT_MAX_TASKS_LISTED
    concurrency: int = DEFAULT_CONCURRENCY
    max_bytes: int = DEFAULT_MAX_BYTES
    table_max_bytes: int = DEFAULT_TABLE_MAX_BYTES
    cache_ttl: RadarCacheTTL = RadarCacheTTL()
    user_agent: str = DEFAULT_USER_AGENT
    retry: RadarRetry = RadarRetry()

    @classmethod
    def from_env(cls) -> RadarConfig:
        proxy = _env_str("RADAR_PROXY")
        if proxy and proxy.lower() != "direct" and "RADAR_PROXY" not in _warned:
            # 只记录「已忽略」这一事实，不记录被忽略的值（代理 URL 可能含口令）。
            _warned.add("RADAR_PROXY")
            logger.warning(
                "[radar] RADAR_PROXY is set but the shared HTTP layer pins trust_env=False "
                "and mounts no proxy; the value is ignored"
            )
        return cls(
            base_url=(_env_str("RADAR_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            proxy=proxy or None,
            timeout=max(1.0, _env_float("RADAR_TIMEOUT", DEFAULT_TIMEOUT)),
            table_timeout=max(1.0, _env_float("RADAR_TABLE_TIMEOUT", DEFAULT_TABLE_TIMEOUT)),
            default_benchmark=_env_str("RADAR_DEFAULT_BENCHMARK") or DEFAULT_BENCHMARK,
            concurrency=max(1, _env_int("RADAR_CONCURRENCY", DEFAULT_CONCURRENCY)),
            max_bytes=max(1024, _env_int("RADAR_MAX_BYTES", DEFAULT_MAX_BYTES)),
            table_max_bytes=max(1024, _env_int("RADAR_MAX_TABLE_BYTES", DEFAULT_TABLE_MAX_BYTES)),
            user_agent=_env_str("RADAR_USER_AGENT") or DEFAULT_USER_AGENT,
        )
