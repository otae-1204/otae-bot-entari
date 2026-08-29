"""Small OneBot action adapter for native API fallbacks."""

from __future__ import annotations

from typing import Any

import httpx

from configs.config import _env


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
