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
"""

from __future__ import annotations

import base64
from typing import Any, Sequence

import httpx

from otae_bot.config.settings import _env
from otae_bot.adapters.entari import event_user_id, get_group_id


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


async def call_onebot_action(bot: Any, action: str, **params: Any) -> Any:
    """Call an action through Satori internal API, then configured HTTP."""
    internal_error: Exception | None = None
    if bot is not None and hasattr(bot, "internal"):
        try:
            result = await bot.internal(action=action, **params)
            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError(result.get("wording") or result.get("message") or "OneBot action failed")
            return result
        except Exception as exc:  # pragma: no cover - adapter-specific
            internal_error = exc

    urls = _base_urls()
    if not urls:
        raise RuntimeError("未配置 OneBot HTTP 地址") from internal_error
    headers = {"Content-Type": "application/json"}
    token = _access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for base in urls:
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


def _forward_node(png: bytes, *, name: str, uin: str) -> dict[str, Any]:
    encoded = base64.b64encode(png).decode("ascii")
    return {
        "type": "node",
        "data": {
            "name": name,
            "uin": str(uin or "0"),
            "content": [{"type": "image", "data": {"file": f"base64://{encoded}"}}],
        },
    }


async def send_forward_images(bot: Any, event: Any, pages: Sequence[bytes]) -> None:
    """Send PNG pages as one private/group merged-forward message."""
    group = get_group_id(event)
    if group:
        action = "send_group_forward_msg"
        target = {"group_id": group}
    else:
        action = "send_private_forward_msg"
        target = {"user_id": event_user_id(event)}
    self_id = str(getattr(bot, "self_id", "") or getattr(bot, "id", "") or event_user_id(event) or "0")
    messages = [_forward_node(page, name="Endfield", uin=self_id) for page in pages]
    await call_onebot_action(bot, action, **target, messages=messages)
