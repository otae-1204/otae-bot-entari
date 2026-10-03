"""Resolve output capacity or an explicitly configured deployment output budget."""
from __future__ import annotations

import time

from .errors import FrontierError

# https://api-docs.deepseek.com/quick_start/pricing/ (384K = 384,000 tokens).
# Keep verified aliases explicit: a future model must not inherit an old limit by prefix.
_DEEPSEEK_LIMITS = dict.fromkeys((
    "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp", "deepseek-v4.1-flash-expires-on-0910",
), 384_000)
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash
_GOOGLE_LIMITS = {"gemini-3.8-flash": 65_536}
_CATALOG_URL = "https://models.dev/api.json"
_catalog: tuple[float, dict] = (0, {})


def known_output_limit(provider: str, model: str) -> int | None:
    """Offline limits for verified model IDs; does not change the requested model."""
    if provider == "deepseek":
        return _DEEPSEEK_LIMITS.get(model)
    if provider == "google":
        return _GOOGLE_LIMITS.get(model.removeprefix("models/"))
    return None


def _metadata_limit(data: dict) -> int | None:
    for key in ("max_output_tokens", "max_completion_tokens", "output_token_limit"):
        value = data.get(key)
        if type(value) is int and value > 0:
            return value
    return None


async def resolve_output_limit(connection, client, model: str) -> int:
    """Prefer native metadata; use the provider-scoped public catalog where APIs lack it.

    Catalog requests carry no credentials, prompts or requested model ID. Cache only public
    metadata, not SDK clients/tasks, so independent event loops can safely reuse the result.
    """
    if connection.max_output_tokens is not None:
        # Explicit deployment budget takes precedence; never infer it from context size.
        return connection.max_output_tokens
    limit = known_output_limit(connection.provider, model)
    if limit is not None:
        return limit
    if connection.api == "google":
        info = await client.aio.models.get(model=model)
        limit = _metadata_limit(info.model_dump())
    elif connection.provider in ("anthropic", "openai-compatible"):
        try:
            info = await client.models.retrieve(model)
        except Exception as error:
            # Some compatible APIs only implement generation, not model metadata.
            if getattr(error, "status_code", None) not in (404, 405, 501):
                raise
        else:
            limit = _metadata_limit(info.model_dump())
    if limit is not None:
        return limit
    # A compatible endpoint can serve a differently capped deployment of the same ID.
    # Do not borrow the original vendor's limit merely because the model name matches.
    if connection.provider != "openai-compatible":
        global _catalog
        import httpx
        expires, catalog = _catalog
        if time.monotonic() >= expires:
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
                    response = await http.get(_CATALOG_URL)
                    response.raise_for_status()
                    catalog = response.json()
                if not isinstance(catalog, dict):
                    raise ValueError("Invalid model catalog")
            except (httpx.HTTPError, ValueError):
                # A previously fetched, provider-scoped limit is preferable to inventing one.
                catalog = _catalog[1]
            else:
                _catalog = (time.monotonic() + 3600, catalog)
        entry = catalog.get(connection.provider, {}).get("models", {}).get(model.removeprefix("models/"), {})
        limit = entry.get("limit", {}).get("output")
        if type(limit) is int and limit > 0:
            return limit
    raise FrontierError("无法自动确定该模型的最大输出额度，未启动生成；请稍后重试或使用已有容量信息的模型。",
                        diagnostics={"code": "model_output_limit_unknown", "retryable": True})
