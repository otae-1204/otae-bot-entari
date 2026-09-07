"""Actionable transport diagnostics without logging credentials or raw exceptions."""

from __future__ import annotations

import errno
import socket
import ssl
from dataclasses import dataclass

import httpx
from loguru import logger

from .config import HywConfig


@dataclass(frozen=True)
class NetworkFailure:
    code: str
    message: str


def classify_error(error: httpx.HTTPError) -> NetworkFailure:
    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(causes) < 12 and all(current is not item for item in causes):
        causes.append(current)
        current = current.__cause__ or current.__context__
    # Some HTTP transports retain only the SSL exception's text while wrapping it.
    if any(isinstance(item, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(item) for item in causes):
        return NetworkFailure("tls_certificate", "TLS 证书校验失败，请管理员检查接口证书链、系统时间和 Python CA 证书。")
    if any(isinstance(item, socket.gaierror) for item in causes):
        return NetworkFailure("dns", "域名解析失败，请管理员检查机器人运行机器的 DNS 和代理地址。")
    if any(isinstance(item, ssl.SSLError) for item in causes):
        return NetworkFailure("tls_handshake", "TLS 握手失败，请管理员检查接口及代理的协议配置。")
    if isinstance(error, httpx.ProxyError):
        return NetworkFailure("proxy", "代理连接失败，请管理员检查代理地址、端口和认证信息。")
    if isinstance(error, httpx.LocalProtocolError):
        return NetworkFailure("request_protocol", "请求格式无效，请管理员检查 API 密钥是否包含换行或异常字符。")
    if isinstance(error, httpx.UnsupportedProtocol):
        return NetworkFailure("url_protocol", "接口地址缺少或使用了不支持的协议，请填写完整的 http:// 或 https:// 地址。")
    if isinstance(error, httpx.TimeoutException):
        return NetworkFailure("timeout", "模型请求超时，请稍后重试；管理员可检查接口及代理是否可达。")
    refused = {errno.ECONNREFUSED, 10061}
    if any(isinstance(item, ConnectionRefusedError) or getattr(item, "errno", None) in refused or getattr(item, "winerror", None) in refused for item in causes):
        return NetworkFailure("connection_refused", "连接被拒绝，请管理员检查模型服务或代理是否运行、端口是否正确。")
    if isinstance(error, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return NetworkFailure("connection_interrupted", "模型请求的连接中断或响应协议异常，请检查接口网关及代理后重试。")
    if isinstance(error, httpx.DecodingError):
        return NetworkFailure("response_encoding", "模型响应解码失败，请管理员检查接口网关返回的压缩格式。")
    return NetworkFailure("connection", "无法建立模型连接，请管理员检查运行机器到接口或代理的网络。")


def report_error(error: httpx.HTTPError, config: HywConfig) -> str:
    failure = classify_error(error)
    route = "proxy" if config.proxy else "direct"
    # Exception strings can contain Authorization headers or proxy passwords.
    # Keep only a fixed classification, exception type and route in the log.
    logger.warning("[hyw] model request failed: code={} error={} route={}", failure.code, type(error).__name__, route)
    hint = "当前使用代理；如需仅让 HYW 直连，可设置 HYW_PROXY=direct 后重启。" if config.proxy else "当前使用直连。"
    return f"{failure.message}（{type(error).__name__} / {failure.code}）\n{hint}"
