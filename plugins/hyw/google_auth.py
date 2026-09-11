"""Google 服务账号凭据：自签 RS256 assertion 并换取短期访问令牌。

只使用标准库与 pycryptodome（项目已有依赖），不引入 google-auth。
私钥只保存在内存中的 ServiceAccount 里，不进入 HywConfig、日志或聊天回复。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from loguru import logger

from .config import HywConfig, HywError
from .network_errors import classify_auth_error

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
ASSERTION_LIFETIME = 3600
CLOCK_SKEW = 60
REFRESH_MARGIN = 300
TOKEN_TIMEOUT = 20
MAX_ERROR_BYTES = 4096


class _MaskedHeaders(dict):
    """Authorization 头字典；repr 打码，因为它会被 traceback 渲染。"""

    def __repr__(self) -> str:
        return "{'Authorization': 'Bearer <hidden>'}"


class _MaskedForm(dict):
    """令牌交换的表单体；repr 打码，避免 assertion 出现在 traceback 里。"""

    def __repr__(self) -> str:
        return "{'grant_type': '...', 'assertion': '<hidden>'}"


class Secret:
    """敏感字符串的持有者，repr 固定打码。

    entari 以 ``diagnose=True`` 安装 loguru 处理器，traceback 会渲染每个帧当前
    执行行上的变量值。因此访问令牌/assertion 不能以裸字符串局部变量的形式出现
    在调用行上（否则任何一次请求失败都会把它们打进日志）。取值只经由 reveal()。
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def bearer_headers(self) -> dict[str, str]:
        # 逐行赋值：本方法内没有任何一行直接引用原始值。
        headers = _MaskedHeaders()
        headers["Authorization"] = "Bearer " + self.reveal()
        return headers

    def form_data(self) -> dict[str, str]:
        data = _MaskedForm()
        data["grant_type"] = GRANT_TYPE
        data["assertion"] = self.reveal()
        return data

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "<hidden>"


@dataclass(frozen=True)
class ServiceAccount:
    """已校验的服务账号凭据。

    client_email / private_key_id / private_key 都是身份或密钥材料，一律排除出
    repr：entari 的 loguru 处理器带 diagnose=True，对象一旦出现在被记录的帧里，
    repr 就会被写进日志。project_id 与 token_uri 保留，便于定位项目与端点。
    """

    project_id: str
    client_email: str = field(repr=False)
    private_key_id: str = field(repr=False)
    private_key: str = field(repr=False)
    token_uri: str
    source: str = field(repr=False)


def _now() -> float:
    return time.time()


def _invalid(reason: str) -> HywError:
    # Fixed text only: these messages reach the chat, so no path or key material.
    return HywError(f"HYW 服务账号凭据不可用：{reason}")


def resolve_credentials_path(path: str) -> Path:
    """展开 ~，相对路径按进程工作目录（机器人从仓库根启动）解析。"""
    expanded = Path(str(path).strip()).expanduser()
    return expanded if expanded.is_absolute() else (Path.cwd() / expanded)


def _parse(path: Path) -> ServiceAccount:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise _invalid("无法读取凭据文件") from None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise _invalid("凭据文件不是有效的 JSON") from None
    if not isinstance(data, dict):
        raise _invalid("凭据文件不是有效的 JSON 对象")
    if str(data.get("type", "")).strip() != "service_account":
        raise _invalid("凭据文件的 type 不是 service_account")
    for name in ("project_id", "client_email", "private_key", "token_uri"):
        if not str(data.get(name, "") or "").strip():
            raise _invalid(f"凭据文件缺少字段：{name}")
    private_key = str(data["private_key"])
    if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
        raise _invalid("凭据文件的 private_key 不是 PEM 私钥")
    try:
        RSA.import_key(private_key)
    except (ValueError, IndexError, TypeError):
        raise _invalid("凭据文件的 private_key 无法解析") from None
    return ServiceAccount(
        project_id=str(data["project_id"]).strip(),
        client_email=str(data["client_email"]).strip(),
        private_key_id=str(data.get("private_key_id", "") or "").strip(),
        private_key=private_key,
        token_uri=str(data["token_uri"]).strip(),
        source=str(path),
    )


_accounts: dict[tuple[str, int, int], ServiceAccount] = {}


def load_service_account(path: str) -> ServiceAccount:
    """读取并校验凭据文件；同一文件未变化时复用已解析的结果（含 RSA 导入）。"""
    resolved = resolve_credentials_path(path)
    try:
        stat = resolved.stat()
    except OSError:
        logger.warning("[hyw] service account file not found: {}", resolved)
        raise _invalid("找不到凭据文件") from None
    if not resolved.is_file():
        logger.warning("[hyw] service account path is not a file: {}", resolved)
        raise _invalid("凭据文件路径不是文件") from None
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    account = _accounts.get(key)
    if account is None:
        account = _parse(resolved)
        for stale in [item for item in _accounts if item[0] == key[0]]:
            _accounts.pop(stale, None)
        _accounts[key] = account
    return account


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign(account: ServiceAccount, signing_input: str) -> bytes | None:
    """签名，失败返回 None。

    本函数吞掉全部异常，因此它的帧永远不会进入 traceback。这一点是必需的：
    pycryptodome 的 ``RsaKey.__repr__`` 会打印 n/e/d/p/q（含私钥指数），而 entari
    以 ``diagnose=True`` 安装 loguru 处理器，会把 traceback 中每个帧当前执行行上
    引用的局部变量值渲染出来——只要私钥对象出现在某个被记录的帧里就会泄漏。
    调用方因此在**不引用私钥**的行上抛错（见 build_assertion）。
    """
    try:
        key = RSA.import_key(account.private_key)
        return pkcs1_15.new(key).sign(SHA256.new(signing_input.encode("ascii")))
    except Exception:
        return None


def build_assertion(account: ServiceAccount, *, now: float | None = None, skew: float = CLOCK_SKEW) -> str:
    """自签 RS256 JWT：iss/aud/scope/iat/exp，有效期恰为 3600 秒。"""
    issued = int(_now() if now is None else now) - int(skew)
    header: dict[str, object] = {"alg": "RS256", "typ": "JWT"}
    if account.private_key_id:
        header["kid"] = account.private_key_id
    claims = {
        "iss": account.client_email,
        "scope": CLOUD_PLATFORM_SCOPE,
        "aud": account.token_uri,
        "iat": issued,
        "exp": issued + ASSERTION_LIFETIME,
    }
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode("utf-8")) for part in (header, claims)
    )
    # 下面两行只引用 account（其 repr 已排除 private_key/source）与 signing_input，
    # 两者都不是敏感值，因此即便抛错也不会把私钥写进日志。
    signature = _sign(account, signing_input)
    if signature is None:
        raise _invalid("凭据文件的 private_key 无法用于签名")
    return f"{signing_input}.{_b64(signature)}"


class TokenProvider:
    """缓存访问令牌，并保证并发请求只交换一次。"""

    def __init__(self, account: ServiceAccount, *, refresh_margin: float = REFRESH_MARGIN, skew: float = CLOCK_SKEW) -> None:
        self._account = account
        self._refresh_margin = refresh_margin
        self._skew = skew
        self._lock = asyncio.Lock()
        self._token: Secret | None = None
        self._expires_at = 0.0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self._account.source, self._account.client_email, self._account.private_key_id)

    def _valid(self) -> bool:
        return self._token is not None and _now() < self._expires_at - self._refresh_margin

    async def bearer(self, client: httpx.AsyncClient, *, force: bool = False) -> Secret:
        if not force and self._valid():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if not force and self._valid():
                return self._token  # type: ignore[return-value]
            token, expires_at = await self._exchange(client)
            self._token = token
            self._expires_at = expires_at
            return token

    async def _exchange(self, client: httpx.AsyncClient) -> tuple[Secret, float]:
        # 先包成 Secret 再入局部变量：assertion 是可直接换令牌的凭据，
        # 而 traceback 会渲染当前执行行上出现的变量值。
        form = Secret(build_assertion(self._account, skew=self._skew)).form_data()
        # Must be form encoded; a JSON body makes Google answer 400 Invalid JSON payload.
        response = await client.post(
            self._account.token_uri,
            data=form,
            timeout=TOKEN_TIMEOUT,
            follow_redirects=False,
        )
        if response.status_code != 200:
            failure = classify_auth_error(response.status_code, response.text[:MAX_ERROR_BYTES])
            logger.warning("[hyw] token exchange failed: status={} code={}", response.status_code, failure.code)
            raise HywError(f"{failure.message}（HTTP {response.status_code} / {failure.code}）")
        return _parse_token(response)


def _parse_token(response: httpx.Response) -> tuple[Secret, float]:
    """从交换响应取出令牌；令牌一取出即包进 Secret，不留裸字符串局部变量。

    本函数里唯一引用原始值的语句都不抛异常（dict 取值包在 try 内、赋值不抛），
    因此 traceback 不会渲染出令牌。
    """
    try:
        expires_in = float(response.json().get("expires_in") or 0)
        issued = Secret(str(response.json()["access_token"]).strip())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HywError("凭据交换未返回访问令牌，请管理员检查服务账号凭据。") from None
    if not issued:
        raise HywError("凭据交换未返回访问令牌，请管理员检查服务账号凭据。")
    # Missing or implausible lifetimes fall back to Google's documented 3600 seconds.
    lifetime = expires_in if 60 <= expires_in <= 7200 else float(ASSERTION_LIFETIME)
    return issued, _now() + lifetime


_providers: dict[tuple[str, str, str], TokenProvider] = {}


async def bearer_for(client: httpx.AsyncClient, config: HywConfig, *, force: bool = False) -> Secret:
    """返回 Authorization 使用的令牌；api_key 模式原样返回配置的密钥。"""
    if config.auth_mode != "service_account":
        return Secret(config.api_key)
    account = load_service_account(config.credentials_file)
    provider = _providers.get((account.source, account.client_email, account.private_key_id))
    if provider is None:
        provider = TokenProvider(account)
        _providers[(account.source, account.client_email, account.private_key_id)] = provider
    return await provider.bearer(client, force=force)
