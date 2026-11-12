"""AI 智商雷达插件的配置层。

配置来自 entari.yml 的 ``plugins: plugins.radar:`` 段（:class:`RadarSettings`）；默认值即
「不配置也能跑」的一套只读参数。:meth:`RadarConfig.from_settings` 再做下限钳制。

**注意**：本模块的 dataclass 都是 ``slots=True``，所以类属性取到的是 slot 描述符而不是
默认值（``RadarConfig.base_url`` 不是字符串）。默认值因此放在模块级常量里，供
``from_settings`` 显式引用。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from otae_bot.config.model import Settings, field
from otae_bot.config.settings import plugin_settings

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


class RadarSettings(Settings):
    """AI 智商雷达（/radar、/智商雷达）：只读消费 api.codexradar.com，全部可留空"""

    base_url: str = field(default=DEFAULT_BASE_URL, description="上游 API 基址；一般无需修改")
    default_benchmark: str = field(
        default=DEFAULT_BENCHMARK,
        description="默认频道：deep-swe（代码修复）或 pompeii-adjacency（视觉恢复）；两者分数不可比",
    )
    timeout: float = field(default=DEFAULT_TIMEOUT, description="单次请求超时秒数（下限 1.0）")
    table_timeout: float = field(default=DEFAULT_TABLE_TIMEOUT, description="/table 类命令的超时秒数（下限 1.0）")
    concurrency: int = field(default=DEFAULT_CONCURRENCY, description="同时执行的雷达命令数上限（下限 1）")
    max_bytes: int = field(default=DEFAULT_MAX_BYTES, description="单次响应字节上限（下限 1024）")
    max_table_bytes: int = field(default=DEFAULT_TABLE_MAX_BYTES, description="/table 单次响应字节上限（下限 1024）")
    user_agent: str = field(default="", description="出网 User-Agent；留空使用内置的 otae-bot-radar/<版本> (+仓库地址)")
    proxy: str = field(default="", description="预留：共享 HTTP 层不挂代理，设置后不生效（只记一条警告，不记录取值）")


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
    #: 仅用于记录部署意图（见 ``from_settings`` 的告警）。
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
    def from_settings(cls, settings: RadarSettings | None = None) -> RadarConfig:
        settings = settings or plugin_settings("radar", RadarSettings)
        proxy = settings.proxy.strip()
        if proxy and proxy.lower() != "direct" and "proxy" not in _warned:
            # 只记录「已忽略」这一事实，不记录被忽略的值（代理 URL 可能含口令）。
            _warned.add("proxy")
            logger.warning(
                "[radar] plugins.radar.proxy is set but the shared HTTP layer pins trust_env=False "
                "and mounts no proxy; the value is ignored"
            )
        return cls(
            base_url=(settings.base_url.strip() or DEFAULT_BASE_URL).rstrip("/"),
            proxy=proxy or None,
            timeout=max(1.0, settings.timeout),
            table_timeout=max(1.0, settings.table_timeout),
            default_benchmark=settings.default_benchmark.strip() or DEFAULT_BENCHMARK,
            concurrency=max(1, settings.concurrency),
            max_bytes=max(1024, settings.max_bytes),
            table_max_bytes=max(1024, settings.max_table_bytes),
            user_agent=settings.user_agent.strip() or DEFAULT_USER_AGENT,
        )
