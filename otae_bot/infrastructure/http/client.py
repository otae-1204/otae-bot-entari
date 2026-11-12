"""Shared HTTP GET client and bounded response cache for public resources."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from loguru import logger

from otae_bot.config.settings import core_settings
from otae_bot.infrastructure.cache import AsyncTTLCache, CacheStats
from .asset_policy import (
    DIRECT_FAILURES,
    AssetFetchSettings,
    HostCircuitOpen,
    asset_breaker,
    asset_direct_routes,
    asset_settings,
    batch_deadline,
    configure_asset_settings,
    host_max_age,
    proxied_host,
    rewrite_asset_url,
)
from . import assets
from .json_values import freeze_json, freeze_json_object, json_memory_size, mutable_json
from .tls import shared_ssl_context
from .disk import (
    DiskImage,
    DiskImageMeta,
    public_image_request,
    public_images,
    public_table_request,
    public_tables,
)


DEFAULT_CACHE_TTL_SECONDS = 600.0
DEFAULT_CONCURRENCY = 8
DEFAULT_MAX_RESOURCE_BYTES = 10 * 1024 * 1024
# 重新校验失败、改用旧磁盘副本时，内存里只留这么久，之后再试一次网络。
STALE_RETRY_SECONDS = 60.0


def _mib(value: int) -> int:
    return max(1, int(value)) * 1024 * 1024


_cache_settings = core_settings().http_cache
API_CACHE_MAX_BYTES = _mib(_cache_settings.api_mib)
TABLE_CACHE_MAX_BYTES = _mib(_cache_settings.table_mib)
# 当前版本的 I18nTextTable_CN（解析后计量约 45 MiB）单独一个常驻槽，不和其它表在 LRU 里互挤。
I18N_CACHE_MAX_BYTES = _mib(_cache_settings.i18n_mib)
# 小图池：要装得下一张账号卡的两百多张图标。
ASSET_CACHE_MAX_BYTES = _mib(_cache_settings.asset_mib)
# 超过这个体积的图（立绘、横幅）只要磁盘上有副本就不进内存池，命中时直接读磁盘。
ASSET_LARGE_ENTRY_BYTES = max(1, _cache_settings.asset_large_entry_kib) * 1024
HTTP_CACHE_MAX_BYTES = (
    API_CACHE_MAX_BYTES
    + TABLE_CACHE_MAX_BYTES
    + I18N_CACHE_MAX_BYTES
    + ASSET_CACHE_MAX_BYTES
)
_I18N_RESIDENT_SUFFIX = "/I18nTextTable_CN.json"

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
    # 过期的磁盘副本：重新校验失败或整批截止时间已到时用来代替空白。
    stale: bool = False
    # 正文的 SHA-256（十六进制）；磁盘命中与本进程下载的公共图片都会带上，未知时为空。
    digest: str = field(default="", compare=False)
    # 磁盘上已有（或已排队写入）同样的正文：大图因此可以不占内存池。
    disk_backed: bool = field(default=False, compare=False)
    # 服务器回了 304：正文来自磁盘副本，这次联网只是重新校验（日志里的 net_304）。
    revalidated: bool = field(default=False, compare=False)


_client: httpx.AsyncClient | None = None
_client_lifetime = None
_client_lock = RLock()
_stats_lock = RLock()
_namespace_metrics: dict[str, dict[str, float]] = {}
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_request_semaphore: asyncio.Semaphore | None = None
# 素材（图片/图标/头像）走独立的客户端与 AssetBroker 的优先级槽位：图床不可达时只占素材
# 自己的槽位，API 请求仍按原有的 Semaphore(DEFAULT_CONCURRENCY) 排队，互不阻塞。
_asset_client: httpx.AsyncClient | None = None
_asset_client_lifetime = None
# PROXY_MODE=fallback 时 _asset_client 只直连，直连连不上才用这个走代理的客户端。
_asset_proxy_client: httpx.AsyncClient | None = None
_asset_proxy_client_lifetime = None


def _cache_event(key: RequestKey, event: str, value: float) -> None:
    with _stats_lock:
        counters = _namespace_metrics.setdefault(key[0], {})
        counters[event] = counters.get(event, 0) + value


def _expires_ttl(resource: HttpResource) -> float:
    if resource.expires_at is None:
        return float("inf")
    return max(0, resource.expires_at - time.time())


def _resident_ttl(resource: HttpResource) -> float:
    """The I18n slot keeps a fresh copy for the caller's TTL, past the server max-age.

    Table paths are version-addressed, so the bytes behind one path do not
    change; a new build is a new path (and a new key). Private, no-cache and
    stale copies still expire as usual.
    """
    remaining = _expires_ttl(resource)
    return remaining if resource.stale or remaining <= 0 else float("inf")


def _new_pool(
    budget: int,
    *,
    max_entries: int = 512,
    max_entry_bytes: int | None = None,
    ttl_for_value: Callable[[HttpResource], float] = _expires_ttl,
) -> AsyncTTLCache[RequestKey, HttpResource]:
    return AsyncTTLCache(
        ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        max_bytes=budget,
        max_entries=max_entries,
        sizeof=lambda resource: len(resource.content) + resource.decoded_bytes,
        on_event=_cache_event,
        ttl_for_value=ttl_for_value,
        max_entry_bytes=max_entry_bytes,
        # Only bodies a disk row also holds may skip memory; the rest stay cached.
        may_bypass=lambda resource: resource.disk_backed,
    )


_response_cache = _new_pool(API_CACHE_MAX_BYTES)
_table_cache = _new_pool(TABLE_CACHE_MAX_BYTES)
# One entry: a new build's table (a new path) replaces the previous one.
_i18n_cache = _new_pool(I18N_CACHE_MAX_BYTES, max_entries=1, ttl_for_value=_resident_ttl)
_asset_cache = _new_pool(ASSET_CACHE_MAX_BYTES, max_entry_bytes=ASSET_LARGE_ENTRY_BYTES)
_cache_pools = (_response_cache, _table_cache, _i18n_cache, _asset_cache)
_POOL_NAMES = ("api", "tables", "i18n", "assets")


def _pool_for(url: str, response_kind: str) -> AsyncTTLCache[RequestKey, HttpResource]:
    if response_kind == "bytes":
        return _asset_cache
    parts = urlsplit(url)
    if parts.hostname == "data.akedata.wiki":
        if parts.path.endswith(_I18N_RESIDENT_SUFFIX):
            return _i18n_cache
        if not parts.path.endswith("/manifest.json"):
            return _table_cache
    return _response_cache


def _get_client() -> httpx.AsyncClient:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                follow_redirects=True,
                trust_env=False,
                verify=shared_ssl_context(trust_env=False),
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


def _build_asset_client(settings: AssetFetchSettings) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=settings.concurrency,
        max_keepalive_connections=settings.concurrency,
    )
    verify = shared_ssl_context(trust_env=settings.trust_env)
    options = dict(
        follow_redirects=True,
        trust_env=settings.trust_env,
        verify=verify,
        limits=limits,
    )
    if not settings.proxy:
        return httpx.AsyncClient(**options)
    try:
        if settings.proxy_hosts:
            # httpx 的 mounts 按主机路由：只有列出的素材主机走代理，其余直连。
            mounts = {
                f"all://{host}": httpx.AsyncHTTPTransport(
                    proxy=settings.proxy, verify=verify, limits=limits
                )
                for host in settings.proxy_hosts
            }
            client = httpx.AsyncClient(mounts=mounts, **options)
        else:
            client = httpx.AsyncClient(proxy=settings.proxy, **options)
    except (ImportError, ValueError) as exc:
        logger.warning(
            f"[http-assets] asset proxy unusable ({type(exc).__name__}); connecting directly"
        )
        return httpx.AsyncClient(**options)
    logger.info(
        f"[http-assets] asset proxy enabled via {_proxy_label(settings.proxy)} "
        f"hosts={','.join(settings.proxy_hosts) or '*'} mode={settings.proxy_mode}"
    )
    return client


def _proxy_label(proxy: str) -> str:
    # 代理地址可能带口令：只记 scheme、主机和端口。
    parts = urlsplit(proxy)
    return f"{parts.scheme}://{parts.hostname}:{parts.port or ''}"


def _direct_first(settings: AssetFetchSettings) -> bool:
    return bool(settings.proxy) and settings.proxy_mode == "fallback"


def _get_asset_client(*, proxy: bool = False) -> httpx.AsyncClient:
    global _asset_client, _asset_proxy_client
    with _client_lock:
        settings = asset_settings()
        if proxy:
            if _asset_proxy_client is None or _asset_proxy_client.is_closed:
                _asset_proxy_client = _build_asset_client(settings)
            return _asset_proxy_client
        if _asset_client is None or _asset_client.is_closed:
            if _direct_first(settings):
                _asset_client = _build_asset_client(replace(settings, proxy=""))
                logger.info(
                    f"[http-assets] asset proxy mode=fallback: direct first, "
                    f"{_proxy_label(settings.proxy)} after a connect failure "
                    f"hosts={','.join(settings.proxy_hosts) or '*'}"
                )
            else:
                _asset_client = _build_asset_client(settings)
        return _asset_client


async def _owned(client: httpx.AsyncClient, lifetime):
    if lifetime is None or lifetime[0] is not client:
        generator = _own_client(client)
        await anext(generator)
        lifetime = (client, generator)
    return lifetime


async def _get_owned_client(*, asset: bool = False, proxy: bool = False):
    global _client_lifetime, _asset_client_lifetime, _asset_proxy_client_lifetime
    if asset and proxy:
        client = _get_asset_client(proxy=True)
        _asset_proxy_client_lifetime = await _owned(client, _asset_proxy_client_lifetime)
    elif asset:
        client = _get_asset_client()
        _asset_client_lifetime = await _owned(client, _asset_client_lifetime)
    else:
        client = _get_client()
        _client_lifetime = await _owned(client, _client_lifetime)
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
    asset: bool = False,
) -> HttpResource:
    timeout: float | httpx.Timeout = timeout_seconds
    if asset:
        breaker = asset_breaker()
        host = (urlsplit(url).hostname or "").lower()
        # 熔断中的主机连信号量都不排，直接判缺失。
        if breaker.blocked(host):
            raise HostCircuitOpen(f"asset host cooling down: {host}")
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(asset_settings().connect_timeout, timeout_seconds),
        )
    # 素材按（优先级，到达顺序）在总并发与按主机上限之内排队，见 assets.py。
    async with assets.broker.slot(host) if asset else _get_semaphore():
        # 排队期间熔断可能已经打开；半开时这里只放行一个探测请求。
        if asset and not breaker.acquire(host):
            raise HostCircuitOpen(f"asset host cooling down: {host}")
        _install_request_log_filter()
        log_token = _suppress_request_log.set(True)
        try:
            request = dict(params=params, headers=headers, timeout=timeout)
            if asset:
                response = await _asset_get(url, host, request)
            else:
                client = await _get_owned_client()
                response = await client.get(url, **request)
        except BaseException as exc:
            if asset:
                # 只记最终结果：直连失败但代理成功不算连接失败，不会打开熔断。
                breaker.record(host, exc)
            raise
        finally:
            _suppress_request_log.reset(log_token)
        if asset:
            breaker.record(host, None)
        if response.status_code == 304 and cached_image is not None:
            content = cached_image.content
        else:
            response.raise_for_status()
            content = response.content
            if asset:
                assets.record_download(len(content))
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


async def _asset_get(url: str, host: str, request: dict) -> httpx.Response:
    """PROXY_MODE=fallback：先直连，只有连接失败 / 连接超时才对这次请求改走代理。"""
    settings = asset_settings()
    client = await _get_owned_client(asset=True)
    if not (_direct_first(settings) and proxied_host(host, settings)):
        return await client.get(url, **request)
    routes = asset_direct_routes()
    if routes.try_direct(host):
        try:
            response = await client.get(url, **request)
        except DIRECT_FAILURES as exc:
            routes.record(host, False)
            logger.debug(
                f"[http-assets] direct {type(exc).__name__} host={host}; retrying via proxy"
            )
        except httpx.HTTPError:
            routes.record(host, True)  # connected: a read timeout is not a route problem
            raise
        except BaseException:
            routes.record(host, None)
            raise
        else:
            routes.record(host, True)
            return response
    proxy_client = await _get_owned_client(asset=True, proxy=True)
    return await proxy_client.get(url, **request)


async def _sha256_hex(content: bytes) -> str:
    # hashlib releases the GIL: a portrait-sized body is hashed off the event loop.
    if len(content) > ASSET_LARGE_ENTRY_BYTES:
        return await asyncio.to_thread(lambda: hashlib.sha256(content).hexdigest())
    return hashlib.sha256(content).hexdigest()


def _disk_lifetime(url: str, ttl_seconds: float) -> tuple[float, bool]:
    """Disk freshness window, and whether a content-addressed host pins it."""
    if ttl_seconds <= 0:
        return ttl_seconds, False
    pinned = host_max_age(urlsplit(url).hostname or "")
    return (ttl_seconds, False) if pinned is None else (pinned, True)


def _fresh_for(max_age: float, disk_ttl: float, pinned: bool) -> float:
    # A pinned host follows the configured window even for rows stored with the
    # old 600s; a stored 0 still means the server sent no-cache.
    if pinned:
        return disk_ttl if max_age > 0 else 0.0
    return min(float(disk_ttl), float(max_age))


def _disk_metadata_reusable(
    meta, *, ttl_seconds: float, now: float | None = None, pinned: bool = False
) -> bool:
    """True for an unexpired row or one that can be revalidated with 304."""
    if meta is None or ttl_seconds <= 0:
        return False
    current = time.time() if now is None else now
    if current < meta.validated_at + _fresh_for(meta.max_age, ttl_seconds, pinned):
        return True
    return bool(meta.etag or meta.modified)


def _effective_headers(
    client: httpx.AsyncClient,
    url: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
) -> Mapping[str, str]:
    """The headers ``client.build_request("GET", url, ...)`` would carry, without building it.

    Disk keys hash these, so they must match the builder exactly: the client's
    default headers, then ``Host``, then the caller's headers. Client cookies
    (added per domain) and header input the builder would reject or join
    still go through the real builder.
    """
    extra = dict(headers or {})
    target = httpx.URL(url)
    if (
        client.cookies
        or target.is_relative_url
        or len({str(name).lower() for name in extra}) != len(extra)
        or not all(
            isinstance(name, str) and isinstance(value, str) and name.isascii() and value.isascii()
            for name, value in extra.items()
        )
    ):
        return client.build_request("GET", url, params=params, headers=headers).headers
    merged = {name.lower(): value for name, value in client.headers.items()}
    if target.host:
        merged["host"] = target.netloc.decode("ascii")
    merged.update((name.lower(), value) for name, value in extra.items())
    return merged


async def _cache_coordinates(
    url: str,
    *,
    namespace: str,
    response_kind: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
    ttl_seconds: float,
) -> tuple[RequestKey, bool, str, float]:
    """Build the same cache identity ``_fetch_resource`` will use. Does not fetch."""
    client = await _get_owned_client()
    # Client-level Auth flows may add credentials only during send. Do not
    # publish or reuse those responses using a pre-authentication cache key.
    if client.auth is not None:
        ttl_seconds = 0
    effective_headers = _effective_headers(client, url, params, headers)
    key: RequestKey = (
        namespace,
        str(url),
        _normalized_params(params),
        _header_fingerprint(effective_headers),
        response_kind,
    )
    table_eligible = public_table_request(
        url, namespace, headers, params, response_kind
    )
    disk_policy = public_table_request if table_eligible else public_image_request
    eligible = (
        ttl_seconds > 0
        and disk_policy(url, namespace, headers, params, response_kind)
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
    disk_key = hashlib.sha256(repr(key).encode()).hexdigest()
    return key, eligible, disk_key, ttl_seconds


async def _alias_disk_keys(
    url: str,
    disk_aliases: Sequence[str],
    *,
    namespace: str,
    response_kind: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
    headers: Mapping[str, str] | None,
    ttl_seconds: float,
) -> list[tuple[str, str]]:
    """(namespace, disk key) rows other namespaces would store this public image under.

    The disk key includes the namespace, so a copy another feature already
    downloaded for the same URL is invisible without this. Images only.
    """
    keys: list[tuple[str, str]] = []
    for alias in dict.fromkeys(disk_aliases):
        if alias == namespace or not public_image_request(
            url, alias, headers, params, response_kind
        ):
            continue
        _key, eligible, disk_key, _ttl = await _cache_coordinates(
            url,
            namespace=alias,
            response_kind=response_kind,
            params=params,
            headers=headers,
            ttl_seconds=ttl_seconds,
        )
        if eligible:
            keys.append((alias, disk_key))
    return keys


async def cached_public_resource(
    url: str,
    *,
    namespace: str,
    response_kind: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    asset: bool = False,
    disk_aliases: Sequence[str] = (),
) -> bool:
    """True when memory or disk metadata can serve this request without a body read."""
    if asset:
        url = rewrite_asset_url(url)
    key, eligible, disk_key, ttl_seconds = await _cache_coordinates(
        url,
        namespace=namespace,
        response_kind=response_kind,
        params=params,
        headers=headers,
        ttl_seconds=ttl_seconds,
    )
    if _pool_for(url, response_kind).contains(key):
        return True
    if not eligible:
        return False
    table_eligible = public_table_request(
        url, namespace, headers, params, response_kind
    )
    disk = public_tables if table_eligible else public_images
    disk_ttl, pinned = _disk_lifetime(url, ttl_seconds)
    try:
        meta = await _disk_metadata(disk, disk_key)
        if _disk_metadata_reusable(meta, ttl_seconds=disk_ttl, pinned=pinned):
            return True
        if table_eligible:
            return False
        for _alias, alias_key in await _alias_disk_keys(
            url,
            disk_aliases,
            namespace=namespace,
            response_kind=response_kind,
            params=params,
            headers=headers,
            ttl_seconds=ttl_seconds,
        ):
            meta = await _disk_metadata(disk, alias_key)
            if _disk_metadata_reusable(meta, ttl_seconds=disk_ttl, pinned=pinned):
                return True
    except (OSError, sqlite3.Error):
        return False
    return False


async def _disk_metadata(disk, key: str) -> DiskImageMeta | None:
    # A loaded metadata index answers with a dict lookup: no thread hop needed.
    if getattr(disk, "index_loaded", False):
        return disk.metadata(key)
    return await asyncio.to_thread(disk.metadata, key)


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
    asset: bool = False,
    disk_aliases: Sequence[str] = (),
    revalidate: bool = False,
    memory: bool = True,
    coalesce: bool = True,
) -> HttpResource:
    """``revalidate``: drop the memory copy and ask the server again even when the
    disk row is still fresh (a conditional request when validators exist).

    ``coalesce=False`` runs the download in the calling task instead of a shared
    pool fill, so cancelling the caller cancels it (AssetBroker jobs dedupe on
    their own); ``memory=False`` also skips publishing to the memory pool
    (prefetch: disk only). Disk reads and writes are unchanged either way.
    """
    key, eligible, disk_key, ttl_seconds = await _cache_coordinates(
        url,
        namespace=namespace,
        response_kind=response_kind,
        params=params,
        headers=headers,
        ttl_seconds=ttl_seconds,
    )
    table_eligible = public_table_request(
        url, namespace, headers, params, response_kind
    )
    disk = public_tables if table_eligible else public_images
    disk_policy = public_table_request if table_eligible else public_image_request
    # Capture before the factory is scheduled: a clear must invalidate queued
    # work too, not only requests that already reached the network.
    generation = disk.register(namespace) if eligible else None

    def validate(resource):
        if validator is None:
            return resource
        started = time.perf_counter()
        decoded = freeze_json(validator(resource))
        resource = replace(
            resource, decoded=decoded, decoded_bytes=json_memory_size(decoded)
        )
        _cache_event(key, "decode_seconds", time.perf_counter() - started)
        _cache_event(key, "decodes", 1)
        return resource

    disk_ttl, pinned = _disk_lifetime(url, ttl_seconds)

    async def request() -> HttpResource:
        cached = None
        # The row a 304 refreshes: ours, or the alias row the copy came from.
        row_key, row_namespace, row_generation = disk_key, namespace, generation
        if eligible:
            try:
                cached = await asyncio.to_thread(disk.get, disk_key, max_bytes)
                if cached is None and not table_eligible:
                    for alias, alias_key in await _alias_disk_keys(
                        url,
                        disk_aliases,
                        namespace=namespace,
                        response_kind=response_kind,
                        params=params,
                        headers=headers,
                        ttl_seconds=ttl_seconds,
                    ):
                        cached = await asyncio.to_thread(disk.get, alias_key, max_bytes)
                        if cached is not None:
                            row_key, row_namespace = alias_key, alias
                            row_generation = disk.register(alias)
                            _cache_event(key, "alias_disk_hits", 1)
                            break
            except (OSError, sqlite3.Error):
                _cache_event(key, "disk_errors", 1)
        if cached is not None and not revalidate:
            deadline = cached.validated_at + _fresh_for(cached.max_age, disk_ttl, pinned)
            if time.time() < deadline:
                _cache_event(key, "disk_hits", 1)
                if response_kind == "bytes" and len(cached.content) > ASSET_LARGE_ENTRY_BYTES:
                    _cache_event(key, "large_disk_hits", 1)
                return validate(
                    HttpResource(
                        cached.content,
                        cached.content_type,
                        200,
                        url,
                        expires_at=deadline,
                        digest=cached.digest,
                        disk_backed=True,
                    )
                )
        request_headers = dict(headers or {})
        if cached is not None:
            if cached.etag:
                request_headers["If-None-Match"] = cached.etag
            elif cached.modified:
                request_headers["If-Modified-Since"] = cached.modified
        try:
            resource = await _request_resource(
                url,
                params=params,
                headers=request_headers,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                cached_image=cached,
                asset=asset,
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            if cached is None:
                raise
            # stale-if-error: an expired copy beats a blank; retried after a minute.
            _cache_event(key, "stale_served", 1)
            logger.debug(
                f"[http] revalidation failed ({classify_failure(exc)[1]}); "
                f"serving the stale disk copy namespace={namespace}"
            )
            return validate(
                HttpResource(
                    cached.content,
                    cached.content_type,
                    200,
                    url,
                    expires_at=time.time() + STALE_RETRY_SECONDS,
                    stale=True,
                    digest=cached.digest,
                    disk_backed=True,
                )
            )
        resource = validate(resource)  # Never persist malformed JSON.
        if eligible:
            private = resource.private_response or any(
                flag in resource.cache_control.lower()
                for flag in ("private", "no-store")
            )
            private = private or not disk_policy(
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
            if pinned:
                max_age = disk_ttl  # content-addressed: the path changes with the bytes
            if "no-cache" in resource.cache_control.lower():
                max_age = 0
            if private:
                resource = replace(resource, expires_at=time.time())
            elif resource.content_type.startswith("image/") or table_eligible:
                validated_at = time.time()
                # Queued for the background writer: the caller never waits for SQLite.
                if resource.status_code == 304 and cached is not None:
                    # Same bytes: refresh validators and freshness, keep the stored body.
                    digest = cached.digest
                    stored = disk.refresh_later(
                        row_key,
                        row_namespace,
                        DiskImageMeta(
                            validated_at, max_age, resource.etag, resource.modified
                        ),
                        row_generation,
                    )
                else:
                    # Images get a digest once here (browser asset URLs reuse it);
                    # the writer then stores it instead of hashing the body again.
                    digest = (
                        await _sha256_hex(resource.content) if response_kind == "bytes" else ""
                    )
                    stored = disk.put_later(
                        disk_key,
                        namespace,
                        DiskImage(
                            resource.content,
                            resource.content_type,
                            resource.etag,
                            resource.modified,
                            validated_at,
                            max_age,
                            digest,
                        ),
                        generation,
                    )
                resource = replace(
                    resource,
                    expires_at=validated_at + max_age,
                    digest=digest,
                    disk_backed=stored,
                )
            if resource.status_code == 304:
                _cache_event(key, "not_modified", 1)
                resource = replace(resource, status_code=200, revalidated=True)
        return resource

    if ttl_seconds <= 0:
        _cache_event(key, "misses", 1)
        return await request()

    pool = _pool_for(url, response_kind)
    if revalidate:
        await pool.clear(lambda candidate: candidate == key)
    if coalesce and memory:
        resource = await pool.get_or_create(
            key,
            request,
            ttl_seconds=ttl_seconds,
        )
    else:
        _cache_event(key, "misses", 1)
        resource = await request()
        if memory:
            pool.put(key, resource, ttl_seconds=ttl_seconds)
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
    timeout_seconds: float | None = None,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    asset: bool = False,
    disk_aliases: Sequence[str] = (),
    memory: bool = True,
    coalesce: bool = True,
) -> HttpResource:
    """``asset=True`` 走素材通道：优先级排队、短连接超时、按主机熔断、可选代理与主机改写。

    ``disk_aliases``：同一公共图片 URL 也可能由这些命名空间存过磁盘副本；
    自己的副本缺失时借用它们（字节相同，谁下载的都一样）。
    ``memory`` / ``coalesce`` 供 AssetBroker 使用，见 ``_fetch_resource``。
    """
    if asset:
        url = rewrite_asset_url(url)
        if timeout_seconds is None:
            timeout_seconds = asset_settings().read_timeout
    elif timeout_seconds is None:
        timeout_seconds = 10.0
    return await _fetch_resource(
        url,
        namespace=namespace,
        response_kind="bytes",
        params=params,
        headers=headers,
        timeout_seconds=timeout_seconds,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
        asset=asset,
        disk_aliases=disk_aliases,
        memory=memory,
        coalesce=coalesce,
    )


async def fetch_many(
    urls: Iterable[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    asset: bool = False,
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
                asset=asset,
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
    只有确定性的失败才放弃：体积超过上限，重取也不会变小；主机熔断中，冷却期内重试也不会发请求。
    """
    if isinstance(exc, HostCircuitOpen):
        return True, "circuit_open"
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


# 截止时间已过时，仍给内存 / 磁盘缓存命中留的一点时间；网络请求赶不上就判缺失。
DEADLINE_GRACE_SECONDS = 0.5


async def fetch_many_resilient(
    urls: Iterable[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    attempts: int | None = None,
    base_delay_seconds: float = 0.25,
    log_prefix: str = "[assets]",
    budget_seconds: float | None = None,
    disk_aliases: Sequence[str] = (),
) -> tuple[dict[str, HttpResource | None], dict[str, str]]:
    """并发取多个素材，对间歇性失败退避重试，并返回失败原因。

    与 fetch_many 的区别，都是为图床的抖动与不可达服务：
    1. 走素材通道（优先级排队、按主机限流、短连接超时、按主机熔断），不占 API 请求的并发；
    2. 失败会重试（默认重试 1 次，0.25s 退避）——hycdn 的 404 与超时都是间歇的；
    3. 整批有软截止（默认取 OTAE_HTTP_ASSET_RENDER_BUDGET，或外层 asset_render_budget
       设定的整卡截止时间），到点不再等待，未取到的按缺失（``deadline``）返回；
       没下完的下载不取消，转入后台续传，完成后照常写缓存，下次查询就是热的；
    4. 放弃的原因会写进日志，调用方不再只看到「少了几张图」。
    5. 重新校验失败或到点没取到时，有旧磁盘副本就用旧图（stale=True），不算失败。
    6. ``disk_aliases`` 里的命名空间若已存过同一 URL 的磁盘副本，自己缺副本时借用（见 fetch_bytes）。
    7. 先一次分流：内存命中直接用；磁盘上新鲜的整批一次读出（一次线程切换）；
       只有剩下的才进网络队列，同一 URL 只下载一次。每批写一条 ``fetch batch`` 日志。
    第一项覆盖**每一个**请求过的 url（失败为 None），第二项只保留最终失败原因，
    调用方既不会悄悄少图，也能决定是否把失败详情带入诊断信息。返回值另带 ``.stats``
    （``assets.BatchStats``：mem / disk / net / net_304 / 续传数），形状仍是二元组。
    """
    return await assets.broker.fetch_batch(
        urls,
        namespace=namespace,
        headers=headers,
        timeout_seconds=timeout_seconds,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
        attempts=asset_settings().attempts if attempts is None else attempts,
        base_delay_seconds=base_delay_seconds,
        deadline=batch_deadline(budget_seconds),
        disk_aliases=disk_aliases,
        log_prefix=log_prefix,
    )


@dataclass(frozen=True, slots=True)
class _DiskPlan:
    url: str
    target: str
    key: RequestKey
    ttl_seconds: float
    # (namespace, disk key): ours first, then the aliases, like _fetch_resource.
    rows: tuple[tuple[str, str], ...]
    disk_ttl: float
    pinned: bool


async def _cached_batch(
    urls: Sequence[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None,
    ttl_seconds: float,
    max_bytes: int,
    disk_aliases: Sequence[str] = (),
) -> tuple[dict[str, HttpResource], int, int]:
    """Serve asset URLs that memory or a fresh disk row already hold: ``(found, mem, disk)``.

    Memory is peeked. Disk rows are judged by the metadata index and read with
    one ``get_many`` in one thread hop, then published to the pool (large
    images bypass it). Expired rows, misses and ineligible URLs are left to
    ``fetch_bytes``, which also handles revalidation.
    """
    found: dict[str, HttpResource] = {}
    memory = 0
    plans: list[_DiskPlan] = []
    for url in urls:
        target = rewrite_asset_url(url)
        key, eligible, disk_key, ttl = await _cache_coordinates(
            target,
            namespace=namespace,
            response_kind="bytes",
            params=None,
            headers=headers,
            ttl_seconds=ttl_seconds,
        )
        if ttl <= 0:
            continue
        resource = _pool_for(target, "bytes").peek(key)
        if resource is not None:
            if len(resource.content) <= max_bytes:
                found[url] = resource
                memory += 1
            continue  # over this caller's limit: fetch_bytes reports it
        if not eligible:
            continue
        aliases = await _alias_disk_keys(
            target,
            disk_aliases,
            namespace=namespace,
            response_kind="bytes",
            params=None,
            headers=headers,
            ttl_seconds=ttl,
        )
        disk_ttl, pinned = _disk_lifetime(target, ttl)
        plans.append(
            _DiskPlan(url, target, key, ttl, ((namespace, disk_key), *aliases), disk_ttl, pinned)
        )
    if not plans:
        return found, memory, 0
    disk = public_images

    def read() -> tuple[dict[str, tuple[_DiskPlan, str, str]], dict[str, DiskImage]]:
        now = time.time()
        chosen: dict[str, tuple[_DiskPlan, str, str]] = {}
        for plan in plans:
            for row_namespace, row_key in plan.rows:
                meta = disk.metadata(row_key)
                if meta is None:
                    continue
                # The first row that exists decides; an expired one needs the network.
                if now < meta.validated_at + _fresh_for(meta.max_age, plan.disk_ttl, plan.pinned):
                    chosen[plan.url] = (plan, row_namespace, row_key)
                break
        rows = [row_key for _plan, _namespace, row_key in chosen.values()]
        return chosen, disk.get_many(rows, max_bytes) if rows else {}

    try:
        chosen, images = await asyncio.to_thread(read)
    except (OSError, sqlite3.Error):
        return found, memory, 0
    hits = 0
    now = time.time()
    for url, (plan, row_namespace, row_key) in chosen.items():
        image = images.get(row_key)
        if image is None:
            continue
        expires_at = image.validated_at + _fresh_for(image.max_age, plan.disk_ttl, plan.pinned)
        if now >= expires_at:
            continue
        resource = HttpResource(
            image.content,
            image.content_type,
            200,
            plan.target,
            expires_at=expires_at,
            digest=image.digest,
            disk_backed=True,
        )
        _cache_event(plan.key, "misses", 1)
        _cache_event(plan.key, "disk_hits", 1)
        if row_namespace != namespace:
            _cache_event(plan.key, "alias_disk_hits", 1)
        if len(image.content) > ASSET_LARGE_ENTRY_BYTES:
            _cache_event(plan.key, "large_disk_hits", 1)
        _pool_for(plan.target, "bytes").put(plan.key, resource, ttl_seconds=plan.ttl_seconds)
        found[url] = resource
        hits += 1
    return found, memory, hits


async def _asset_disk_fresh(
    url: str,
    *,
    namespace: str,
    ttl_seconds: float,
    disk_aliases: Sequence[str] = (),
) -> bool | None:
    """For prefetch: True when memory or a fresh disk row already serves the asset,
    False when it needs the network, None when the disk cache would not keep it.

    Like ``_cached_batch``, the first row that exists (ours, then aliases) decides.
    """
    target = rewrite_asset_url(url)
    key, eligible, disk_key, ttl = await _cache_coordinates(
        target,
        namespace=namespace,
        response_kind="bytes",
        params=None,
        headers=None,
        ttl_seconds=ttl_seconds,
    )
    if not eligible or public_images.budget <= 0:
        return None
    if _pool_for(target, "bytes").contains(key):
        return True
    aliases = await _alias_disk_keys(
        target,
        disk_aliases,
        namespace=namespace,
        response_kind="bytes",
        params=None,
        headers=None,
        ttl_seconds=ttl,
    )
    disk_ttl, pinned = _disk_lifetime(target, ttl)
    for row_key in (disk_key, *(alias_key for _alias, alias_key in aliases)):
        meta = await _disk_metadata(public_images, row_key)
        if meta is not None:
            return time.time() < meta.validated_at + _fresh_for(meta.max_age, disk_ttl, pinned)
    return False


async def _stale_copies(
    urls: Sequence[str],
    *,
    namespace: str,
    headers: Mapping[str, str] | None,
    ttl_seconds: float,
    max_bytes: int,
    disk_aliases: Sequence[str] = (),
) -> dict[str, HttpResource]:
    """Disk copies of any age for asset URLs a batch could not fetch in time.

    Our own row first, then rows ``disk_aliases`` namespaces stored for the URL.
    """
    lookups = []
    for url in urls:
        target = rewrite_asset_url(url)
        _key, eligible, disk_key, _ttl = await _cache_coordinates(
            target,
            namespace=namespace,
            response_kind="bytes",
            params=None,
            headers=headers,
            ttl_seconds=ttl_seconds,
        )
        if eligible:
            aliases = await _alias_disk_keys(
                target,
                disk_aliases,
                namespace=namespace,
                response_kind="bytes",
                params=None,
                headers=headers,
                ttl_seconds=ttl_seconds,
            )
            lookups.append((url, target, (disk_key, *(key for _ns, key in aliases))))
    if not lookups:
        return {}
    disk = public_images

    def read() -> dict[str, HttpResource]:
        images = disk.get_many(
            [disk_key for _url, _target, disk_keys in lookups for disk_key in disk_keys],
            max_bytes,
        )
        found = {}
        for url, target, disk_keys in lookups:
            image = next((images[key] for key in disk_keys if key in images), None)
            if image is not None:
                found[url] = HttpResource(
                    image.content,
                    image.content_type,
                    200,
                    target,
                    expires_at=time.time(),
                    stale=True,
                    digest=image.digest,
                    disk_backed=True,
                )
        return found

    return await asyncio.to_thread(read)


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
    revalidate: bool = False,
) -> Any:
    """``revalidate=True`` asks the server again even when memory and disk are fresh."""
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
        revalidate=revalidate,
    )
    return resource.decoded if read_only else mutable_json(resource.decoded)


async def clear_http_cache(
    namespace_prefix: str | None = None, *, include_disk: bool = True
) -> int:
    disk_removed = 0
    if include_disk:
        for disk in (public_images, public_tables):
            try:
                disk_removed += await asyncio.to_thread(disk.clear, namespace_prefix)
            except (OSError, sqlite3.Error):
                pass  # Memory invalidation must still run if a disk is unavailable.
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
                "oversized": stat.oversized,
            }
            for name, pool, stat in zip(_POOL_NAMES, _cache_pools, stats)
        },
        "large_entry_bytes": ASSET_LARGE_ENTRY_BYTES,
        "namespaces": metrics,
        "broker": assets.broker.snapshot(),
    }


async def close_http_client() -> None:
    global _client, _client_lifetime, _request_semaphore, _semaphore_loop
    global _asset_client, _asset_client_lifetime
    global _asset_proxy_client, _asset_proxy_client_lifetime
    # 先让在途的素材下载与续传落盘（有上限），之后才关池与客户端。
    await assets.broker.close()
    await asyncio.gather(*(pool.close() for pool in _cache_pools))
    with _client_lock:
        clients = (_client, _asset_client, _asset_proxy_client)
        lifetimes = (_client_lifetime, _asset_client_lifetime, _asset_proxy_client_lifetime)
        _client = _asset_client = _asset_proxy_client = None
        _client_lifetime = _asset_client_lifetime = _asset_proxy_client_lifetime = None
    for lifetime in lifetimes:
        if lifetime is not None:
            await lifetime[1].aclose()
    for client in clients:
        if client is not None:
            await client.aclose()
    _request_semaphore = None
    _semaphore_loop = None
    # 熔断、直连记录与素材配置随客户端一起重置；下次使用时重新读取环境变量。
    configure_asset_settings(None)
    await clear_http_cache(include_disk=False)
    # close() 先把排队的磁盘写入落盘（有上限），再断开连接。
    await asyncio.to_thread(public_images.close)
    await asyncio.to_thread(public_tables.close)
