"""Small OneBot action adapter used by features that need native forwards.

Satori does define a merged-forward element (``<message forward>`` nesting child
``<message>`` elements) and LLOneBot's Satori encoder maps it to a native QQ
merged forward, so prefer ``utils.entari_native.send_forward`` first.  This
module stays as the fallback for implementations that ignore ``forward``, and as
the only way to give each node a distinct sender: LLOneBot's Satori encoder keeps
one ``<author>`` state per forward, whereas ``send_*_forward_msg`` takes a
per-node name/uin.  Callers can try the account's internal action first and fall
back to the configured OneBot HTTP endpoint without importing the
request-handler plugin (which would register that plugin as a side effect).

Reads that only work for one account (message history, per-account message ids) go
through ``call_account_action``: that account's OneBot HTTP server, then LLBot's Satori
passthrough ``/v1/internal/onebot11/{action}`` on the same connection.
"""

from __future__ import annotations

import base64
from typing import Any, Sequence

import httpx

from otae_bot.config.settings import _env
from otae_bot.adapters.entari import event_user_id, get_group_id
from otae_bot.infrastructure.http.tls import ashared_ssl_context


def _base_urls() -> list[str]:
    result: list[str] = []
    configured = str(_env("ONEBOT_HTTP_URL", "") or _env("LLONEBOT_HTTP_URL", "") or "").rstrip("/")
    if configured:
        result.append(configured)
    clients = _env("SATORI_CLIENTS", [])
    if isinstance(clients, list):
        for item in clients:
            if not isinstance(item, dict):
                continue
            host = item.get("host") or item.get("hostname")
            port = item.get("port")
            if host and port:
                url = f"http://{host}:{port}".rstrip("/")
                if url not in result:
                    result.append(url)
    return result


def _access_token() -> str:
    direct = str(_env("ONEBOT_ACCESS_TOKEN", "") or "")
    if direct:
        return direct
    clients = _env("SATORI_CLIENTS", [])
    if isinstance(clients, list) and clients and isinstance(clients[0], dict):
        return str(clients[0].get("token", "") or "")
    return ""


def _own_client(bot: Any) -> dict | None:
    """The ``SATORI_CLIENTS`` entry of the connection this account came in on."""
    config = getattr(bot, "config", None)
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    clients = _env("SATORI_CLIENTS", [])
    if not host or port is None or not isinstance(clients, list):
        return None
    for item in clients:
        if not isinstance(item, dict):
            continue
        if str(item.get("host") or item.get("hostname")) == str(host) and str(item.get("port")) == str(port):
            return item
    return None


def _client_onebot(client: dict | None) -> tuple[str, str] | None:
    """(``onebot_url``, token) of one ``SATORI_CLIENTS`` entry, if it names its own server."""
    own = str((client or {}).get("onebot_url") or "").rstrip("/")
    if not client or not own:
        return None
    token = client.get("onebot_token")
    if token is None:
        token = _env("ONEBOT_ACCESS_TOKEN", "") or client.get("token")
    return own, str(token or "")


def _endpoints(bot: Any) -> list[tuple[str, str]]:
    """(base URL, token) pairs to try.

    With several LLBot instances (one per QQ account) a single ``ONEBOT_HTTP_URL`` would
    answer for the wrong account, so a client may name its own ``onebot_url`` (and
    ``onebot_token``); that account then uses only its own endpoint.
    """
    own = _client_onebot(_own_client(bot))
    if own:
        return [own]
    token = _access_token()
    return [(url, token) for url in _base_urls()]


class OneBotUnavailable(RuntimeError):
    """No channel of the account could run the action.

    ``answered`` is True when OneBot itself rejected it (``status`` failed, a non-zero
    ``retcode`` or HTTP 5xx), False when no channel was configured or reachable.
    """

    def __init__(self, message: str, *, answered: bool = False):
        super().__init__(message)
        self.answered = answered


class _Rejected(RuntimeError):
    pass


def _account_onebot(bot: Any) -> tuple[str, str] | None:
    """This account's OneBot HTTP server: its own ``onebot_url``, or ``ONEBOT_HTTP_URL``
    when only one Satori connection is configured (so it cannot belong to another account)."""
    own = _client_onebot(_own_client(bot))
    if own:
        return own
    configured = str(_env("ONEBOT_HTTP_URL", "") or _env("LLONEBOT_HTTP_URL", "") or "").rstrip("/")
    clients = _env("SATORI_CLIENTS", [])
    if configured and (not isinstance(clients, list) or len(clients) <= 1):
        return configured, _access_token()
    return None


def _satori_passthrough(bot: Any) -> tuple[str, dict[str, str]] | None:
    """LLBot's Satori route to its OneBot 11 actions, as this account.

    ``POST {api_base}/internal/onebot11/{action}`` on the connection the account came in
    on, with that connection's Satori token; ``Satori-User-ID`` / ``Satori-Platform`` pick
    the account. LLBot answered 403 when probed with ``X-Self-ID``, so it is not sent.
    """
    config = getattr(bot, "config", None)
    self_id = str(getattr(bot, "self_id", "") or "")
    base = str(getattr(config, "api_base", "") or "")
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    if not base and host and port:
        path = str(getattr(config, "path", "") or "").strip("/")
        base = f"http://{host}:{port}" + (f"/{path}" if path else "") + "/v1"
    if not base or not self_id:
        return None
    headers = {
        "Content-Type": "application/json",
        "Satori-User-ID": self_id,
        "Satori-Platform": str(getattr(bot, "platform", "") or ""),
    }
    token = getattr(config, "token", None) or (_own_client(bot) or {}).get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return f"{base.rstrip('/')}/internal/onebot11", headers


def _answer(response: httpx.Response) -> Any:
    """OneBot payload of a response; failures OneBot reported itself raise ``_Rejected``."""
    if response.status_code >= 500:
        detail = response.text
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("wording") or body.get("msg") or detail)
        except ValueError:
            pass
        raise _Rejected(f"HTTP {response.status_code}: {' '.join(detail.split())[:200]}")
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("响应不是 JSON") from None
    if isinstance(data, dict) and (data.get("status") == "failed" or data.get("retcode") not in (None, 0)):
        message = data.get("wording") or data.get("message") or data.get("msg") or "OneBot action failed"
        raise _Rejected(f"retcode={data.get('retcode')}: {' '.join(str(message).split())[:200]}")
    return data


async def call_account_action(bot: Any, action: str, *, timeout: float = 8, **params: Any) -> Any:
    """Call a OneBot 11 action as the account ``bot`` and no other.

    Tries the account's own OneBot HTTP server, then LLBot's Satori passthrough on the
    account's connection. Unlike ``call_onebot_action`` it never tries another account's
    endpoint: ``get_group_msg_history`` fails for an account that is not in the group, and
    OneBot message ids are generated per account.
    """
    channels: list[tuple[str, list[str], dict[str, str]]] = []
    if own := _account_onebot(bot):
        base, token = own
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        channels.append(("onebot", [f"{base}/{action}", f"{base}/api/{action}"], headers))
    if passthrough := _satori_passthrough(bot):
        base, headers = passthrough
        channels.append(("satori", [f"{base}/{action}"], headers))
    if not channels:
        raise OneBotUnavailable("没有可用通道：账号没配 onebot_url，Satori 连接也没有地址")
    tokens = [headers["Authorization"].removeprefix("Bearer ") for _, _, headers in channels if "Authorization" in headers]
    errors: list[str] = []
    answered = False
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, verify=await ashared_ssl_context(trust_env=False),
    ) as client:
        for name, urls, headers in channels:
            for url in urls:
                try:
                    response = await client.post(url, json=params, headers=headers)
                    if response.status_code == 404 and url != urls[-1]:
                        errors.append(f"{name} {url}: HTTP 404")
                        continue
                    return _answer(response)
                except _Rejected as exc:
                    answered = True
                    errors.append(f"{name} {url}: {exc}")
                except Exception as exc:  # noqa: BLE001 - try the next channel
                    errors.append(f"{name} {url}: {type(exc).__name__}" + (f": {exc}" if isinstance(exc, RuntimeError) else ""))
                break
    message = "; ".join(errors)
    for token in tokens:
        if token:
            message = message.replace(token, "<redacted>")
    raise OneBotUnavailable(message, answered=answered)


async def call_onebot_action(bot: Any, action: str, *, http_timeout: float = 10, **params: Any) -> Any:
    """Call an action through Satori internal API, then configured HTTP.

    ``http_timeout`` only applies to the HTTP fallback; large merged forwards
    pass a longer value, every other caller keeps the 10 s default.
    """
    internal_error: Exception | None = None
    if bot is not None and hasattr(bot, "internal"):
        try:
            result = await bot.internal(action=action, **params)
            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError(result.get("wording") or result.get("message") or "OneBot action failed")
            return result
        except Exception as exc:  # pragma: no cover - adapter-specific
            internal_error = exc

    endpoints = _endpoints(bot)
    if not endpoints:
        raise RuntimeError("未配置 OneBot HTTP 地址") from internal_error
    errors: list[str] = []
    async with httpx.AsyncClient(
        timeout=http_timeout, trust_env=False, verify=await ashared_ssl_context(trust_env=False),
    ) as client:
        for base, token in endpoints:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            for suffix in (f"/{action}", f"/api/{action}"):
                try:
                    response = await client.post(f"{base}{suffix}", json=params, headers=headers)
                    if response.status_code == 404:
                        errors.append(f"{suffix}:404")
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if isinstance(data, dict) and data.get("status") == "failed":
                        raise RuntimeError(data.get("wording") or data.get("message") or "OneBot action failed")
                    return data
                except Exception as exc:  # pragma: no cover - adapter-specific
                    errors.append(f"{suffix}:{type(exc).__name__}")
    raise RuntimeError("; ".join(errors[-4:]) or "OneBot action failed") from internal_error


def _forward_node(png: bytes, *, name: str, uin: str, file_uri: str = "") -> dict[str, Any]:
    """One image node; a local ``file://`` URI is referenced directly instead of inlining base64."""
    source = file_uri if file_uri.startswith("file://") else f"base64://{base64.b64encode(png).decode('ascii')}"
    return {
        "type": "node",
        "data": {
            "name": name,
            "uin": str(uin or "0"),
            "content": [{"type": "image", "data": {"file": source}}],
        },
    }


async def send_forward_images(
    bot: Any,
    event: Any,
    pages: Sequence[bytes],
    *,
    name: str = "Endfield",
    file_uris: Sequence[str] = (),
    timeout: float = 10,
) -> None:
    """Send PNG pages as one private/group merged-forward message.

    ``file_uris`` (aligned with ``pages``) lets LLOneBot read already-written
    temp files, keeping a many-page forward small; pages without a URI fall back
    to base64.  ``timeout`` bounds the HTTP fallback of the action call.
    """
    group = get_group_id(event)
    if group:
        action = "send_group_forward_msg"
        target = {"group_id": group}
    else:
        action = "send_private_forward_msg"
        target = {"user_id": event_user_id(event)}
    self_id = str(getattr(bot, "self_id", "") or getattr(bot, "id", "") or event_user_id(event) or "0")
    uris = list(file_uris)[:len(pages)]
    uris += [""] * (len(pages) - len(uris))
    messages = [_forward_node(page, name=name, uin=self_id, file_uri=uri) for page, uri in zip(pages, uris)]
    await call_onebot_action(bot, action, **target, messages=messages, http_timeout=timeout)
