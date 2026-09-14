"""AI 智商雷达插件的错误与降级契约。

两条硬规则：

1. **上游响应体原文不得进入** ``message``（可能含内部标识）。只回结构化错误码 + 固定话术，
   原始片段只进 debug 日志（脱敏、截断）。
2. 错误码是**稳定契约**：formatter / handler 只按 ``code`` 分支，不解析文案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

# ── 错误码 ──
CODE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
CODE_UNKNOWN_BENCHMARK = "unknown_benchmark"
CODE_UNKNOWN_MODEL = "unknown_model"
CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_PAYLOAD_TOO_LARGE = "payload_too_large"
CODE_SCHEMA_DRIFT = "schema_drift"
CODE_RATE_LIMITED = "rate_limited"
CODE_PRIVATE_PATH_REFUSED = "private_path_refused"

#: 面向用户的固定话术。``{detail}`` 只允许由本模块拼装的**本地**信息（可用 id、候选列表、
#: 合法取值），绝不来自上游响应体。
_MESSAGES: dict[str, str] = {
    CODE_UPSTREAM_UNAVAILABLE: "雷达数据源暂时不可用，请稍后重试。",
    CODE_UNKNOWN_BENCHMARK: "没有这个评测频道。",
    CODE_UNKNOWN_MODEL: "没有该模型档位的实测数据。",
    CODE_INVALID_ARGUMENT: "参数不合法。",
    CODE_PAYLOAD_TOO_LARGE: "该查询的数据量过大，请改用轻量命令。",
    CODE_SCHEMA_DRIFT: "数据源结构发生变化，请反馈给维护者。",
    CODE_RATE_LIMITED: "数据源正在限流，请稍后再试。",
    CODE_PRIVATE_PATH_REFUSED: "内部错误：插件不应访问私有接口。",
}

#: 脱敏用的常见敏感片段（上游 401/403 体里可能出现 token 形态的字符串）。
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer|token|authorization)\b\s*[:=]?\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)
_LOG_LIMIT = 200


def redact(text: str, *, limit: int = _LOG_LIMIT) -> str:
    """把任意上游片段压成可安全写日志的形式。

    只保留结构（截断 + 打码），不保留可能含凭据的长串。返回的字符串**不得**用于
    面向用户的 ``message``。
    """
    cleaned = " ".join(str(text or "").split())
    for pattern in _REDACT_PATTERNS:
        cleaned = pattern.sub("<redacted>", cleaned)
    return cleaned[:limit]


class RadarError(Exception):
    """插件错误基类：``code`` 是稳定契约，``message`` 可直接回给用户。

    ``message`` 是**实例属性**（contract: ``message: str``），由固定话术表或调用方传入的
    本地信息拼成；上游响应体原文一律不得进入这里。
    """

    code = CODE_UPSTREAM_UNAVAILABLE

    def __init__(self, message: str | None = None, *, detail: str = "") -> None:
        self.detail = detail
        self.message = message or _MESSAGES.get(self.code, "雷达查询失败。")
        super().__init__(self.message)


class RadarSourceError(RadarError):
    """上游 / 网络侧问题（不可归因于用户输入）。"""


class RadarQueryError(RadarError):
    """参数或语义问题（本地即可判定）。"""


class UpstreamUnavailable(RadarSourceError):
    code = CODE_UPSTREAM_UNAVAILABLE


class UnknownBenchmark(RadarQueryError):
    code = CODE_UNKNOWN_BENCHMARK


class UnknownModel(RadarQueryError):
    code = CODE_UNKNOWN_MODEL


class InvalidArgument(RadarQueryError):
    code = CODE_INVALID_ARGUMENT


class PayloadTooLarge(RadarSourceError):
    code = CODE_PAYLOAD_TOO_LARGE


class SchemaDrift(RadarSourceError):
    code = CODE_SCHEMA_DRIFT


class RateLimited(RadarSourceError):
    code = CODE_RATE_LIMITED


class PrivatePathRefused(RadarSourceError):
    """代码误触 ``/api/private/*``：属实现缺陷，正常路径不应出现。"""

    code = CODE_PRIVATE_PATH_REFUSED


def schema_drift(endpoint: str, field: str, *, got: object = None) -> SchemaDrift:
    """构造 ``schema_drift``，并把实际键名写进 debug 日志（不含值）。"""
    logger.debug(
        "[radar] schema drift at {}: missing/invalid field {!r} (got {})",
        endpoint,
        field,
        type(got).__name__,
    )
    return SchemaDrift(detail=f"{endpoint}:{field}")


def upstream_failure(endpoint: str, exc: BaseException) -> RadarSourceError:
    """把 httpx/解析异常归一到插件错误码，并只记录脱敏摘要。"""
    import httpx

    logger.debug("[radar] {} failed: {} ({})", endpoint, type(exc).__name__, redact(str(exc)))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return RateLimited()
        if status == 404:
            return UnknownBenchmark()
        if status == 422:
            return InvalidArgument()
        return UpstreamUnavailable()
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamUnavailable(detail="timeout")
    if isinstance(exc, ValueError):
        return PayloadTooLarge() if "exceeds" in str(exc) else UpstreamUnavailable(detail="payload")
    if isinstance(exc, httpx.HTTPError):
        return UpstreamUnavailable(detail=type(exc).__name__)
    return UpstreamUnavailable(detail=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """一次退避决策：``wait`` 秒后重试；``give_up`` 为真时不再重试。"""

    wait: float
    give_up: bool


__all__ = [
    "CODE_INVALID_ARGUMENT",
    "CODE_PAYLOAD_TOO_LARGE",
    "CODE_PRIVATE_PATH_REFUSED",
    "CODE_RATE_LIMITED",
    "CODE_SCHEMA_DRIFT",
    "CODE_UNKNOWN_BENCHMARK",
    "CODE_UNKNOWN_MODEL",
    "CODE_UPSTREAM_UNAVAILABLE",
    "InvalidArgument",
    "PayloadTooLarge",
    "PrivatePathRefused",
    "RadarError",
    "RadarQueryError",
    "RadarSourceError",
    "RateLimited",
    "RetryDecision",
    "SchemaDrift",
    "UnknownBenchmark",
    "UnknownModel",
    "UpstreamUnavailable",
    "redact",
    "schema_drift",
    "upstream_failure",
]
