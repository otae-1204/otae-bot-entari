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
    # Async TLS reads normally pass through SSLWantReadError. It can remain in
    # a timeout's exception context even after the handshake has succeeded.
    if isinstance(error, httpx.ReadTimeout):
        return NetworkFailure("timeout", "等待模型响应超时，可能是模型生成较慢、接口排队或网络延迟，请稍后重试。")
    if isinstance(error, httpx.TimeoutException):
        return NetworkFailure("timeout", "模型请求超时，请稍后重试；管理员可检查接口及代理是否可达。")
    if any(isinstance(item, ssl.SSLError) and not isinstance(item, (ssl.SSLWantReadError, ssl.SSLWantWriteError)) for item in causes):
        return NetworkFailure("tls_handshake", "TLS 握手失败，请管理员检查接口及代理的协议配置。")
    if isinstance(error, httpx.ProxyError):
        return NetworkFailure("proxy", "代理连接失败，请管理员检查代理地址、端口和认证信息。")
    if isinstance(error, httpx.LocalProtocolError):
        return NetworkFailure("request_protocol", "请求格式无效，请管理员检查 API 密钥是否包含换行或异常字符。")
    if isinstance(error, httpx.UnsupportedProtocol):
        return NetworkFailure("url_protocol", "接口地址缺少或使用了不支持的协议，请填写完整的 http:// 或 https:// 地址。")
    refused = {errno.ECONNREFUSED, 10061}
    if any(isinstance(item, ConnectionRefusedError) or getattr(item, "errno", None) in refused or getattr(item, "winerror", None) in refused for item in causes):
        return NetworkFailure("connection_refused", "连接被拒绝，请管理员检查模型服务或代理是否运行、端口是否正确。")
    if isinstance(error, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return NetworkFailure("connection_interrupted", "模型请求的连接中断或响应协议异常，请检查接口网关及代理后重试。")
    if isinstance(error, httpx.DecodingError):
        return NetworkFailure("response_encoding", "模型响应解码失败，请管理员检查接口网关返回的压缩格式。")
    return NetworkFailure("connection", "无法建立模型连接，请管理员检查运行机器到接口或代理的网络。")


def classify_auth_error(status: int, payload: str) -> NetworkFailure:
    """把 Google 凭据/Vertex 的 HTTP 错误映射成可操作的中文提示。

    payload 只用于匹配固定的错误标记，从不回显，也不写入日志。
    """
    body = (payload or "").lower()
    if status == 429:
        return NetworkFailure("quota", "模型请求过于频繁或额度不足，请稍后重试。")
    if status == 401:
        return NetworkFailure("sa_token", "模型访问令牌被拒绝，已尝试自动刷新，请重试或检查服务账号状态。")
    if status == 403:
        if "consumer_invalid" in body or "consumer invalid" in body:
            return NetworkFailure("sa_project", "该服务账号无权访问此项目（凭据中的项目与实际请求的项目不一致），请管理员核对。")
        return NetworkFailure("sa_project", "该服务账号无权访问此项目，或项目未启用 Vertex AI，请管理员检查 IAM 权限与 API 启用状态。")
    if status == 404:
        return NetworkFailure("sa_model", "模型或区域不可用，请管理员检查模型名称与 HYW_VERTEX_LOCATION 是否匹配。")
    if status == 400:
        if "invalid jwt signature" in body or "jwt signature" in body:
            return NetworkFailure("sa_signature", "服务账号私钥与凭据不匹配，请重新下载凭据 JSON。")
        if "account not found" in body:
            return NetworkFailure("sa_account", "服务账号不存在或已被删除，请管理员重新创建服务账号。")
        if "short-lived token" in body or "short lived token" in body:
            return NetworkFailure("sa_clock", "运行机器的系统时间偏差过大，请校准系统时间后重试。")
        if "malformed publisher model" in body:
            return NetworkFailure("sa_model_prefix", "模型名缺少发布者前缀，请填写形如 google/gemini-3.8-flash 的名称。")
        if "provided image is not valid" in body or "image is not valid" in body:
            return NetworkFailure("sa_image", "图片无法被模型接受，请改用 JPEG 或 PNG 重新发送。")
        if "invalid_grant" in body or "invalid_request" in body or "invalid json payload" in body:
            return NetworkFailure("sa_assertion", "凭据文件内容不完整或格式不正确，请重新下载凭据 JSON。")
    return NetworkFailure("sa_unknown", "模型服务拒绝了该凭据，请管理员检查服务账号凭据与项目配置。")


def report_error(error: httpx.HTTPError, config: HywConfig) -> str:
    failure = classify_error(error)
    route = "proxy" if config.proxy else "direct"
    # Exception strings can contain Authorization headers or proxy passwords.
    # Keep only a fixed classification, exception type and route in the log.
    logger.warning("[hyw] model request failed: code={} error={} route={}", failure.code, type(error).__name__, route)
    hint = "当前使用代理；如需仅让 HYW 直连，可设置 HYW_PROXY=direct 后重启。" if config.proxy else "当前使用直连。"
    return f"{failure.message}（{type(error).__name__} / {failure.code}）\n{hint}"
