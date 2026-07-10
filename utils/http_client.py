"""Shared HTTP GET client and bounded response cache for public resources."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .async_cache import AsyncTTLCache, CacheStats


DEFAULT_CACHE_TTL_SECONDS = 600.0
DEFAULT_CONCURRENCY = 8
DEFAULT_MAX_RESOURCE_BYTES = 10 * 1024 * 1024
HTTP_CACHE_MAX_BYTES = 32 * 1024 * 1024

RequestKey = tuple[str, str, tuple[tuple[str, str], ...], str, str]
_suppress_request_log: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shared_http_suppress_request_log",
    default=False,
)


class _SharedRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _suppress_request_log.get()


_shared_request_log_filter = _SharedRequestLogFilter()
logging.getLogger("httpx._client").addFilter(_shared_request_log_filter)


def _install_request_log_filter() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(_shared_request_log_filter)


@dataclass(frozen=True, slots=True)
class HttpResource:
    content: bytes
    content_type: str
    status_code: int
    url: str


_response_cache: AsyncTTLCache[RequestKey, HttpResource] = AsyncTTLCache(
    ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
    max_bytes=HTTP_CACHE_MAX_BYTES,
    max_entries=512,
    sizeof=lambda resource: len(resource.content),
)
_client: httpx.AsyncClient | None = None
_client_lock = RLock()
_stats_lock = RLock()
_namespace_hits: dict[str, int] = {}
_namespace_misses: dict[str, int] = {}
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_request_semaphore: asyncio.Semaphore | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=DEFAULT_CONCURRENCY,
                    max_keepalive_connections=DEFAULT_CONCURRENCY,
                ),
            )
        return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore_loop, _request_semaphore
    loop = asyncio.get_running_loop()
    if _request_semaphore is None or _semaphore_loop is not loop:
        _semaphore_loop = loop
        _request_semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY)
    return _request_semaphore


def _normalized_params(params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> tuple[tuple[str, str], ...]:
    if params is None:
        return ()
    items = params.items() if isinstance(params, Mapping) else params
    return tuple(sorted((str(key), str(value)) for key, value in items))


def _header_fingerprint(headers: Mapping[str, str] | None) -> str:
    if not headers:
        return ""
    normalized = "\n".join(f"{key.lower()}:{value}" for key, value in sorted(headers.items()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _request_resource(
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
    timeout_seconds: float,
    max_bytes: int,
) -> HttpResource:
    async with _get_semaphore():
        _install_request_log_filter()
        log_token = _suppress_request_log.set(True)
        try:
            response = await _get_client().get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_seconds,
            )
        finally:
            _suppress_request_log.reset(log_token)
        response.raise_for_status()
        content = response.content
        if not content:
            raise ValueError(f"HTTP resource is empty: {url}")
        if len(content) > max_bytes:
            raise ValueError(f"HTTP resource exceeds {max_bytes} bytes: {url}")
        return HttpResource(
            content=content,
            content_type=response.headers.get("content-type", "").split(";", 1)[0],
            status_code=response.status_code,
            url=str(response.url),
        )


async def _fetch_resource(
    url: str,
    *,
    namespace: str,
    response_kind: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
    timeout_seconds: float,
    ttl_seconds: float,
    max_bytes: int,
    validator: Callable[[HttpResource], object] | None = None,
) -> HttpResource:
    key: RequestKey = (
        namespace,
        str(url),
        _normalized_params(params),
        _header_fingerprint(headers),
        response_kind,
    )

    async def request() -> HttpResource:
        resource = await _request_resource(
            url,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
        if validator is not None:
            validator(resource)
        return resource

    if ttl_seconds <= 0:
        with _stats_lock:
            _namespace_misses[namespace] = _namespace_misses.get(namespace, 0) + 1
        return await request()

    resource, hit = await _response_cache.get_or_create_with_status(
        key,
        request,
        ttl_seconds=ttl_seconds,
    )
    with _stats_lock:
        counters = _namespace_hits if hit else _namespace_misses
        counters[namespace] = counters.get(namespace, 0) + 1
    return resource


async def fetch_bytes(
    url: str,
    *,
    namespace: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> HttpResource:
    return await _fetch_resource(
        url,
        namespace=namespace,
        response_kind="bytes",
        params=params,
        headers=headers,
        timeout_seconds=timeout_seconds,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
    )


async def fetch_many(
    urls: Iterable[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> dict[str, HttpResource | None]:
    unique_urls = tuple(dict.fromkeys(str(url) for url in urls if url))
    results = await asyncio.gather(
        *(
            fetch_bytes(
                url,
                namespace=namespace,
                headers=headers,
                timeout_seconds=timeout_seconds,
                ttl_seconds=ttl_seconds,
                max_bytes=max_bytes,
            )
            for url in unique_urls
        ),
        return_exceptions=True,
    )
    return {
        url: None if isinstance(result, BaseException) else result
        for url, result in zip(unique_urls, results)
    }


async def fetch_json(
    url: str,
    *,
    namespace: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 12.0,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> Any:
    resource = await _fetch_resource(
        url,
        namespace=namespace,
        response_kind="json",
        params=params,
        headers=headers,
        timeout_seconds=timeout_seconds,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
        validator=lambda response: json.loads(response.content),
    )
    return json.loads(resource.content)


async def clear_http_cache(namespace_prefix: str | None = None) -> int:
    with _stats_lock:
        if namespace_prefix is None:
            _namespace_hits.clear()
            _namespace_misses.clear()
        else:
            for counters in (_namespace_hits, _namespace_misses):
                for namespace in list(counters):
                    if namespace.startswith(namespace_prefix):
                        counters.pop(namespace, None)
    if namespace_prefix is None:
        return await _response_cache.clear()
    return await _response_cache.clear(lambda key: key[0].startswith(namespace_prefix))


async def get_http_cache_stats(namespace_prefix: str | None = None) -> CacheStats:
    stats = await _response_cache.stats(
        None if namespace_prefix is None else lambda key: key[0].startswith(namespace_prefix)
    )
    with _stats_lock:
        hits = sum(
            count
            for namespace, count in _namespace_hits.items()
            if namespace_prefix is None or namespace.startswith(namespace_prefix)
        )
        misses = sum(
            count
            for namespace, count in _namespace_misses.items()
            if namespace_prefix is None or namespace.startswith(namespace_prefix)
        )
    return CacheStats(
        entries=stats.entries,
        bytes=stats.bytes,
        hits=hits,
        misses=misses,
        coalesced=stats.coalesced,
        evictions=stats.evictions,
        inflight=stats.inflight,
    )


async def close_http_client() -> None:
    global _client, _request_semaphore, _semaphore_loop
    with _client_lock:
        client = _client
        _client = None
    if client is not None:
        await client.aclose()
    _request_semaphore = None
    _semaphore_loop = None
    await clear_http_cache()
