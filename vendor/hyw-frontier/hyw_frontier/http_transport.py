"""Task-owned, thread-safe HTTP pools; no cookies, redirects or automatic retries."""
from contextlib import contextmanager
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import urllib3


class PooledOpener:
    """Small urllib opener adapter so pooled and one-shot calls share error handling."""

    def __init__(self):
        self._proxies = getproxies()  # Preserve environment/macOS proxy discovery.
        self._pools = {}
        self._lock = Lock()
        self._closed = False

    def _pool(self, url):
        target = urlsplit(url)
        proxy = None if proxy_bypass(target.netloc) else self._proxies.get(target.scheme)
        with self._lock:
            if self._closed:
                raise URLError("HTTP transport is closed")
            if proxy not in self._pools:
                options = {"num_pools": 4, "maxsize": 6, "block": True, "retries": False}
                if proxy:
                    parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
                    proxy_headers = {}
                    if parts.username is not None:
                        proxy_headers = urllib3.make_headers(proxy_basic_auth=
                            unquote(parts.username) + ":" + unquote(parts.password or ""))
                    address = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, parts.query, ""))
                    pool = urllib3.ProxyManager(address, proxy_headers=proxy_headers, **options)
                else:
                    pool = urllib3.PoolManager(**options)
                self._pools[proxy] = pool
            return self._pools[proxy]

    @contextmanager
    def open(self, request, timeout):
        response = None
        try:
            response = self._pool(request.full_url).request(
                request.get_method(), request.full_url, body=request.data,
                headers={"Accept-Encoding": "identity", **dict(request.header_items())},
                timeout=timeout, pool_timeout=timeout, retries=False, redirect=False,
                preload_content=False, decode_content=False,
            )
            if not 200 <= response.status < 300:
                raise HTTPError(request.full_url, response.status, "Upstream HTTP error", {}, None)
            yield response
        except urllib3.exceptions.HTTPError:
            # Never forward proxy URLs, credentials or raw upstream diagnostics.
            raise URLError("HTTP connection failed") from None
        finally:
            if response is not None:
                # Full reads return the reusable socket automatically. Partial/error
                # reads discard it, then return the pool slot without draining a large body.
                response.close()
                response.release_conn()

    def close(self):
        # The task owner closes only after its tool executors have joined.
        with self._lock:
            self._closed = True
            for pool in self._pools.values():
                pool.clear()
            self._pools.clear()
