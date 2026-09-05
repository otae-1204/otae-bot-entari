"""Shared HTTP GET client and bounded response cache for public resources."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from loguru import logger

from otae_bot.infrastructure.cache import AsyncTTLCache, CacheStats
from .json_values import freeze_json, freeze_json_object, json_memory_size, mutable_json
from .disk import DiskImage, public_image_request, public_images


DEFAULT_CACHE_TTL_SECONDS = 600.0
DEFAULT_CONCURRENCY = 8
DEFAULT_MAX_RESOURCE_BYTES = 10 * 1024 * 1024


def _cache_budget(name: str, default_mib: int) -> int:
    return max(1, int(os.environ.get(name, default_mib))) * 1024 * 1024


API_CACHE_MAX_BYTES = _cache_budget("OTAE_HTTP_API_CACHE_MIB", 8)
TABLE_CACHE_MAX_BYTES = _cache_budget("OTAE_HTTP_TABLE_CACHE_MIB", 64)
ASSET_CACHE_MAX_BYTES = _cache_budget("OTAE_HTTP_ASSET_CACHE_MIB", 24)
HTTP_CACHE_MAX_BYTES = (
    API_CACHE_MAX_BYTES + TABLE_CACHE_MAX_BYTES + ASSET_CACHE_MAX_BYTES
)

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
    decoded: Any = field(default=None, repr=False, compare=False)
    decoded_bytes: int = 0
    etag: str = ""
    modified: str = ""
    cache_control: str = ""
    private_response: bool = False
    expires_at: float | None = None


_client: httpx.AsyncClient | None = None
_client_lifetime = None
_client_lock = RLock()
_stats_lock = RLock()
_namespace_metrics: dict[str, dict[str, float]] = {}
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_request_semaphore: asyncio.Semaphore | None = None


def _cache_event(key: RequestKey, event: str, value: float) -> None:
    with _stats_lock:
        counters = _namespace_metrics.setdefault(key[0], {})
        counters[event] = counters.get(event, 0) + value


def _new_pool(budget: int) -> AsyncTTLCache[RequestKey, HttpResource]:
    return AsyncTTLCache(
        ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        max_bytes=budget,
        max_entries=512,
        sizeof=lambda resource: len(resource.content) + resource.decoded_bytes,
        on_event=_cache_event,
        ttl_for_value=lambda resource: (
            float("inf")
            if resource.expires_at is None
            else max(0, resource.expires_at - time.time())
        ),
    )


_response_cache = _new_pool(API_CACHE_MAX_BYTES)
_table_cache = _new_pool(TABLE_CACHE_MAX_BYTES)
_asset_cache = _new_pool(ASSET_CACHE_MAX_BYTES)
_cache_pools = (_response_cache, _table_cache, _asset_cache)


def _pool_for(url: str, response_kind: str) -> AsyncTTLCache[RequestKey, HttpResource]:
    if response_kind == "bytes":
        return _asset_cache
    parts = urlsplit(url)
    if parts.hostname == "data.akedata.wiki" and not parts.path.endswith(
        "/manifest.json"
    ):
        return _table_cache
    return _response_cache


def _get_client() -> httpx.AsyncClient:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=DEFAULT_CONCURRENCY,
                    max_keepalive_connections=DEFAULT_CONCURRENCY,
                ),
            )
        return _client


async def _own_client(client):
    # asyncio.run/Runner closes registered async generators before closing the
    # loop. This also drains sockets when an isolated caller omits explicit
    # application shutdown; never reuse transports owned by a closed loop.
    try:
        yield client
    finally:
        await client.aclose()


async def _get_owned_client():
    global _client_lifetime
    client = _get_client()
    if _client_lifetime is None or _client_lifetime[0] is not client:
        lifetime = _own_client(client)
        await anext(lifetime)
        _client_lifetime = (client, lifetime)
    return client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore_loop, _request_semaphore
    loop = asyncio.get_running_loop()
    if _request_semaphore is None or _semaphore_loop is not loop:
        _semaphore_loop = loop
        _request_semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY)
    return _request_semaphore


def _normalized_params(
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
) -> tuple[tuple[str, str], ...]:
    if params is None:
        return ()
    items = params.items() if isinstance(params, Mapping) else params
    return tuple(sorted((str(key), str(value)) for key, value in items))


def _header_fingerprint(headers: Mapping[str, str] | None) -> str:
    if not headers:
        return ""
    normalized = "\n".join(
        f"{key}:{value}"
        for key, value in sorted((key.lower(), value) for key, value in headers.items())
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _request_resource(
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
    timeout_seconds: float,
    max_bytes: int,
    cached_image: DiskImage | None = None,
) -> HttpResource:
    async with _get_semaphore():
        _install_request_log_filter()
        log_token = _suppress_request_log.set(True)
        try:
            client = await _get_owned_client()
            response = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_seconds,
            )
        finally:
            _suppress_request_log.reset(log_token)
        if response.status_code == 304 and cached_image is not None:
            content = cached_image.content
        else:
            response.raise_for_status()
            content = response.content
        if not content:
            raise ValueError(f"HTTP resource is empty: {url}")
        if len(content) > max_bytes:
            raise ValueError(f"HTTP resource exceeds {max_bytes} bytes: {url}")
        return HttpResource(
            content=content,
            content_type=response.headers.get(
                "content-type", cached_image.content_type if cached_image else ""
            ).split(";", 1)[0],
            status_code=response.status_code,
            url=str(response.url),
            etag=response.headers.get(
                "etag", cached_image.etag if cached_image else ""
            ),
            modified=response.headers.get(
                "last-modified", cached_image.modified if cached_image else ""
            ),
            cache_control=response.headers.get("cache-control", ""),
            private_response=(
                "set-cookie" in response.headers
                or any(
                    name in response.request.headers
                    for name in ("cookie", "authorization", "cred", "sign")
                )
            ),
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
    client = await _get_owned_client()
    # Client-level Auth flows may add credentials only during send. Do not
    # publish or reuse those responses using a pre-authentication cache key.
    if client.auth is not None:
        ttl_seconds = 0
    effective_headers = client.build_request(
        "GET", url, params=params, headers=headers
    ).headers
    key: RequestKey = (
        namespace,
        str(url),
        _normalized_params(params),
        _header_fingerprint(effective_headers),
        response_kind,
    )

    disk_key = hashlib.sha256(repr(key).encode()).hexdigest()
    eligible = (
        ttl_seconds > 0
        and public_image_request(url, namespace, headers, params, response_kind)
        and not any(
            name in effective_headers
            for name in (
                "cookie",
                "authorization",
                "proxy-authorization",
                "cred",
                "sign",
            )
        )
    )
    # Capture before the factory is scheduled: a clear must invalidate queued
    # work too, not only requests that already reached the network.
    generation = public_images.register(namespace) if eligible else None

    async def request() -> HttpResource:
        cached = None
        if eligible:
            try:
                cached = await asyncio.to_thread(public_images.get, disk_key, max_bytes)
            except (OSError, sqlite3.Error):
                _cache_event(key, "disk_errors", 1)
        if cached is not None:
            deadline = cached.validated_at + min(ttl_seconds, cached.max_age)
            if time.time() < deadline:
                _cache_event(key, "disk_hits", 1)
                return HttpResource(
                    cached.content, cached.content_type, 200, url, expires_at=deadline
                )
        request_headers = dict(headers or {})
        if cached is not None:
            if cached.etag:
                request_headers["If-None-Match"] = cached.etag
            elif cached.modified:
                request_headers["If-Modified-Since"] = cached.modified
        resource = await _request_resource(
            url,
            params=params,
            headers=request_headers,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            cached_image=cached,
        )
        if eligible:
            private = resource.private_response or any(
                flag in resource.cache_control.lower()
                for flag in ("private", "no-store")
            )
            private = private or not public_image_request(
                resource.url, namespace, headers, None, response_kind
            )
            match = re.search(
                r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)", resource.cache_control, re.I
            )
            max_age = min(ttl_seconds, int(match[1])) if match else ttl_seconds
            if (
                resource.status_code == 304
                and not resource.cache_control
                and cached is not None
            ):
                max_age = min(ttl_seconds, cached.max_age)
            if "no-cache" in resource.cache_control.lower():
                max_age = 0
            if private:
                resource = replace(resource, expires_at=time.time())
            elif resource.content_type.startswith("image/"):
                value = DiskImage(
                    resource.content,
                    resource.content_type,
                    resource.etag,
                    resource.modified,
                    time.time(),
                    max_age,
                )
                try:
                    await asyncio.to_thread(
                        public_images.put, disk_key, namespace, value, generation
                    )
                except (OSError, sqlite3.Error):
                    _cache_event(key, "disk_errors", 1)
                resource = replace(resource, expires_at=value.validated_at + max_age)
            if resource.status_code == 304:
                _cache_event(key, "not_modified", 1)
                resource = replace(resource, status_code=200)
        if validator is not None:
            started = time.perf_counter()
            decoded = freeze_json(validator(resource))
            resource = replace(
                resource, decoded=decoded, decoded_bytes=json_memory_size(decoded)
            )
            _cache_event(key, "decode_seconds", time.perf_counter() - started)
            _cache_event(key, "decodes", 1)
        return resource

    if ttl_seconds <= 0:
        _cache_event(key, "misses", 1)
        return await request()

    resource = await _pool_for(url, response_kind).get_or_create(
        key,
        request,
        ttl_seconds=ttl_seconds,
    )
    # A permissive request may have populated the same cache key previously.
    # Reapply each caller's resource limit even on a cache hit.
    if len(resource.content) > max_bytes:
        raise ValueError(f"HTTP resource exceeds {max_bytes} bytes")
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


def classify_failure(exc: BaseException) -> tuple[bool, str]:
    """Return (give_up, human_reason) for an asset fetch exception.

    图床（bbs.hycdn.cn）实测：同一批头像 URL 一组 15 次全 404、另一组 12 个全 200
    ——404 是**间歇**的（边缘节点对象不一致），不能当永久失败，否则会把原有容错削掉。
    只有确定性的失败才放弃：体积超过上限，重取也不会变小。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return False, f"http {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return False, type(exc).__name__.lower()
    if isinstance(exc, httpx.TransportError):
        return False, type(exc).__name__.lower()
    if isinstance(exc, ValueError):
        exceeds = "exceeds" in str(exc)
        return exceeds, "too_large" if exceeds else "empty_body"
    return False, type(exc).__name__.lower()


async def fetch_many_resilient(
    urls: Iterable[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    attempts: int = 3,
    base_delay_seconds: float = 0.25,
    log_prefix: str = "[assets]",
) -> tuple[dict[str, HttpResource | None], dict[str, str]]:
    """并发取多个资源，对间歇性失败退避重试，并返回失败原因。

    与 fetch_many 的区别只有两点，都是为图床的抖动服务：
    1. 失败会重试（默认 3 轮，0.25s / 0.5s 退避）——hycdn 的 404 与超时都是间歇的；
    2. 放弃的原因会写进日志，调用方不再只看到「少了几张图」。
    第一项覆盖**每一个**请求过的 url（失败为 None），第二项只保留最终失败原因，
    调用方既不会悄悄少图，也能决定是否把失败详情带入诊断信息。
    """
    unique_urls = tuple(dict.fromkeys(str(url) for url in urls if url))
    resolved: dict[str, HttpResource] = {}
    failures: dict[str, str] = {}
    pending = list(unique_urls)
    for attempt in range(max(1, attempts)):
        if not pending:
            break
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
                for url in pending
            ),
            return_exceptions=True,
        )
        retry: list[str] = []
        for url, outcome in zip(pending, results):
            if isinstance(outcome, BaseException):
                give_up, reason = classify_failure(outcome)
                failures[url] = reason
                if not give_up and attempt + 1 < attempts:
                    retry.append(url)
                continue
            resolved[url] = outcome
            failures.pop(url, None)
        pending = retry
        if pending and attempt + 1 < attempts:
            await asyncio.sleep(base_delay_seconds * (2**attempt))
    if failures:
        summary = ", ".join(
            f"{url.rsplit('/', 1)[-1][:12]}={failures[url]}"
            for url in list(failures)[:6]
        )
        logger.warning(
            f"{log_prefix} fetch incomplete "
            f"namespace={namespace} requested={len(unique_urls)} resolved={len(resolved)} "
            f"failed={len(failures)} detail={summary}"
        )
    return {url: resolved.get(url) for url in unique_urls}, failures


async def fetch_json(
    url: str,
    *,
    namespace: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 12.0,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    read_only: bool = False,
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
        validator=lambda response: json.loads(
            response.content, object_hook=freeze_json_object
        ),
    )
    return resource.decoded if read_only else mutable_json(resource.decoded)


async def clear_http_cache(
    namespace_prefix: str | None = None, *, include_disk: bool = True
) -> int:
    disk_removed = 0
    if include_disk:
        try:
            disk_removed = await asyncio.to_thread(
                public_images.clear, namespace_prefix
            )
        except (OSError, sqlite3.Error):
            pass  # Disposable cache unavailable; memory invalidation must still run.
    with _stats_lock:
        if namespace_prefix is None:
            _namespace_metrics.clear()
        else:
            for namespace in list(_namespace_metrics):
                if namespace.startswith(namespace_prefix):
                    _namespace_metrics.pop(namespace)
    return disk_removed + sum(
        await asyncio.gather(
            *(
                pool.clear(
                    None
                    if namespace_prefix is None
                    else lambda key: key[0].startswith(namespace_prefix)
                )
                for pool in _cache_pools
            )
        )
    )


async def get_http_cache_stats(namespace_prefix: str | None = None) -> CacheStats:
    stats = await asyncio.gather(
        *(
            pool.stats(
                None
                if namespace_prefix is None
                else lambda key: key[0].startswith(namespace_prefix)
            )
            for pool in _cache_pools
        )
    )
    with _stats_lock:
        metrics: dict[str, float] = {}
        for namespace, counters in _namespace_metrics.items():
            if namespace_prefix is None or namespace.startswith(namespace_prefix):
                for key, value in counters.items():
                    metrics[key] = metrics.get(key, 0) + value
    return CacheStats(
        entries=sum(item.entries for item in stats),
        bytes=sum(item.bytes for item in stats),
        hits=int(metrics.get("direct_hits", 0) + metrics.get("coalesced", 0)),
        misses=int(metrics.get("misses", 0)),
        coalesced=int(metrics.get("coalesced", 0)),
        evictions=int(
            metrics.get("expirations", 0) + metrics.get("capacity_evictions", 0)
        ),
        inflight=sum(item.inflight for item in stats),
        expirations=int(metrics.get("expirations", 0)),
        capacity_evictions=int(metrics.get("capacity_evictions", 0)),
        oversized=int(metrics.get("oversized", 0)),
        failures=int(metrics.get("failures", 0)),
        fill_seconds=metrics.get("fill_seconds", 0),
        wait_seconds=metrics.get("wait_seconds", 0),
    )


async def get_http_cache_diagnostics() -> dict[str, Any]:
    """Anonymous aggregate metrics only: never expose URLs or request headers."""
    stats = await asyncio.gather(*(pool.stats() for pool in _cache_pools))
    with _stats_lock:
        metrics = {name: dict(values) for name, values in _namespace_metrics.items()}
    return {
        "pools": {
            name: {
                "budget_bytes": pool.max_bytes,
                "bytes": stat.bytes,
                "entries": stat.entries,
                "capacity_evictions": stat.capacity_evictions,
            }
            for name, pool, stat in zip(
                ("api", "tables", "assets"), _cache_pools, stats
            )
        },
        "namespaces": metrics,
    }


async def close_http_client() -> None:
    global _client, _client_lifetime, _request_semaphore, _semaphore_loop
    await asyncio.gather(*(pool.close() for pool in _cache_pools))
    with _client_lock:
        client = _client
        _client = None
        lifetime = _client_lifetime
        _client_lifetime = None
    if lifetime is not None:
        await lifetime[1].aclose()
    if client is not None:
        await client.aclose()
    _request_semaphore = None
    _semaphore_loop = None
    await clear_http_cache(include_disk=False)
    await asyncio.to_thread(public_images.close)
