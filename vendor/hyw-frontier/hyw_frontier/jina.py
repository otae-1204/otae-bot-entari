"""Jina's direct Search API and browser-engine Reader; no local browser fallback."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from datetime import date
import ipaddress
import json
import os
from pathlib import Path
from threading import Lock, BoundedSemaphore
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .cleanup import clear_exception_frames
from .costs import ToolCosts
from .http_transport import PooledOpener

SEARCH_ENDPOINT = "https://svip.jina.ai/"
PAGE_ENDPOINT = "https://r.jina.ai/"
READER_ENGINE = "browser"
READER_ENGINES = ('default', 'browser')
READER_FORMAT = "markdown"
PAGESHOT_FORMAT = "pageshot"
READER_CONFIG = {"endpoint": PAGE_ENDPOINT, "engine": READER_ENGINE, "format": READER_FORMAT,
                 "pageshot_format": PAGESHOT_FORMAT, "authentication": "anonymous"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class JinaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_jina_key(home: Path) -> str:
    key = os.environ.get("JINA_API_KEY", "").strip()
    if key:
        return key
    try:
        data = json.loads((home / "jina.json").read_text(encoding="utf-8"))
        key = data.get("api_key", "")
    except FileNotFoundError:
        key = ""
    except (ValueError, OSError, AttributeError) as exc:
        raise JinaError("invalid_config", "Jina 配置无效，请检查独立目录的 jina.json") from exc
    if not isinstance(key, str) or not key.strip():
        raise JinaError("missing_key", "未配置 Jina：设置 JINA_API_KEY 或在独立目录 jina.json 配置 api_key")
    return key.strip()


def public_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme not in ("https", "http") or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 80, 443)
                or any(ord(char) <= 32 for char in value)):
            raise ValueError
        lowered = host.lower().rstrip(".")
        if lowered in ("localhost", "localhost.localdomain") or lowered.endswith((".local", ".internal", ".localhost")):
            raise ValueError
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            # Block shorthand/numeric IP spellings as well as single-label hosts.
            if "." not in lowered or all(c in "0123456789." for c in lowered) or lowered.startswith("0x"):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError as exc:
        raise JinaError("invalid_url", "仅支持无登录信息的公开 HTTP(S) URL，端口限80/443；不支持本地或私有地址") from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API credentials to a redirected endpoint.


def request_json(endpoint: str, body: dict, headers: dict, *, provider: str = "Jina", opener=None) -> dict:
    request = Request(endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with (opener if opener is not None else build_opener(NoRedirect)).open(request, timeout=30) as response:
            raw = bytearray()
            # A bounded read may return a short chunk before EOF (notably on the
            # production HTTP transport). Keep reading rather than parsing a prefix.
            while len(raw) <= MAX_RESPONSE_BYTES:
                chunk = response.read(MAX_RESPONSE_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise JinaError("response_too_large", f"{provider} 原始响应超过2MB上限，请缩小搜索范围或换一个更精确的来源")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        advice = {
            401: "认证失败，请检查密钥", 402: "额度不足，请检查账号余额",
            403: "拒绝访问，请检查权限或换用可替代来源", 429: "限流，请稍后再试，不要立即重复检索",
        }.get(code, "请检查网址或换用可替代来源；未自动重试")
        raise JinaError(f"http_{code}", f"{provider} HTTP {code}：{advice}") from None
    except (URLError, TimeoutError, OSError):
        raise JinaError("network_error", f"{provider} 网络连接失败或超时；未自动重试，可换用可替代来源") from None
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        if isinstance(result.get("code"), int) and result["code"] >= 400:
            raise JinaError("upstream_error", f"{provider} 返回错误状态；不要将错误页作为证据")
        return result
    except (ValueError, UnicodeError):
        raise JinaError("invalid_response", f"{provider} 返回无效 JSON；不要将此响应作为证据") from None


def request_pageshot_url(url: str, *, reader_engine: str = READER_ENGINE, opener=None) -> str:
    """Request Jina's full-page screenshot and return the public image URL.

    Hosted Reader answers pageshot requests with a redirect to the generated PNG.
    Keep redirects disabled here so the signed image URL can be handed to the
    bounded image fetcher instead of loading arbitrary binary data in this client.
    """
    body = json.dumps({"url": url}).encode()
    headers = {"Content-Type": "application/json", "X-Return-Format": PAGESHOT_FORMAT}
    if reader_engine != "default":
        headers["X-Engine"] = reader_engine
    request = Request(PAGE_ENDPOINT, data=body, headers=headers, method="POST")
    client = opener if opener is not None else build_opener(NoRedirect)
    try:
        response = client.open(request, timeout=30)
    except HTTPError as exc:
        try:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if location:
                    return public_url(location)
            code = exc.code
        finally:
            exc.close()
        advice = {
            401: "认证失败，请检查密钥", 402: "额度不足，请检查账号余额",
            403: "拒绝访问，请检查权限或换用可替代来源", 429: "限流，请稍后再试，不要立即重复检索",
        }.get(code, "请检查网址或换用可替代来源；未自动重试")
        raise JinaError(f"http_{code}", f"Jina Reader HTTP {code}：{advice}") from None
    except (URLError, TimeoutError, OSError):
        raise JinaError("network_error", "Jina Reader 整页截图请求失败或超时；未自动重试") from None
    try:
        location = response.headers.get("Location")
        if response.status in (301, 302, 303, 307, 308) and location:
            return public_url(location)
        raise JinaError("invalid_response", "Jina Reader pageshot 未返回截图地址")
    finally:
        response.close()


class JsonTransport:
    """Owned by one search client; one-shot request_json remains available to callers."""

    def __init__(self, provider: str):
        self.provider = provider
        self.opener = PooledOpener()

    def __call__(self, endpoint: str, body: dict, headers: dict) -> dict:
        return request_json(endpoint, body, headers, provider=self.provider, opener=self.opener)

    def close(self):
        self.opener.close()


class JinaClient:
    """Per-task single-flight cache. Identical concurrent operations share one request."""
    def __init__(self, home: Path, transport: Callable | None = None, *, reader_engine: str = READER_ENGINE):
        if reader_engine not in READER_ENGINES:
            raise ValueError('reader_engine must be default or browser')
        self.reader_engine = reader_engine
        self.home = home
        self._transport = JsonTransport("Jina") if transport is None else None
        self.transport = self._transport if transport is None else transport
        self._cache: dict[str, Future] = {}
        self._lock = Lock()
        self._network_slots = BoundedSemaphore(6)
        self._closed = False
        self.costs = ToolCosts()

    def close(self):
        # Owners join tool executors before closing; drop cached results AND exceptions.
        with self._lock:
            self._closed = True
            futures = tuple(self._cache.values())
            self._cache.clear()
        for future in futures:
            if future.done() and not future.cancelled():
                error = future.exception()
                if error is not None:
                    clear_exception_frames(error)
        self.costs.clear()
        if self._transport is not None:
            self._transport.close()

    def _cached(self, identity: str, work: Callable) -> dict:
        with self._lock:
            if self._closed:
                raise JinaError("client_closed", "搜索客户端已关闭")
            cached = identity in self._cache
            future = self._cache.setdefault(identity, Future())
        if not cached:
            try:
                future.set_result(work())
            except Exception as exc:
                future.set_exception(exc)
        result = deepcopy(future.result())
        result["cached"] = cached
        return result

    def _request(self, endpoint: str, body: dict, *, authenticated: bool = True) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if endpoint == PAGE_ENDPOINT:
            if self.reader_engine != 'default':
                headers["X-Engine"] = self.reader_engine
            headers["X-Respond-With"] = READER_FORMAT
        if authenticated:
            headers["Authorization"] = f"Bearer {load_jina_key(self.home)}"
        payload = None
        operation = "search_images" if body.get("type") == "images" else "web_search" if endpoint == SEARCH_ENDPOINT else "reader"
        try:
            with self._network_slots:
                payload = self.transport(endpoint, body, headers)
                return payload
        finally:
            self.costs.record("jina", operation, payload, free=not authenticated)

    def search(self, item: dict) -> dict:
        return self._search(item, images=False)

    def search_images(self, item: dict) -> dict:
        return self._search(item, images=True)

    def _search(self, item: dict, *, images: bool) -> dict:
        query = " ".join(item["query"].split())
        if not query:
            raise JinaError("invalid_query", "查询不能为空或仅包含空白")
        body: dict[str, Any] = {"q": query}
        if images:
            body["type"] = "images"
        if "location" in item:
            body["gl"] = item["location"]
        if "after_date" in item:
            after = date.fromisoformat(item["after_date"])
            body["tbs"] = f"cdr:1,cd_min:{after.month}/{after.day}/{after.year}"

        def fetch():
            payload = self._request(SEARCH_ENDPOINT, body)
            if not isinstance(payload.get("results"), list):
                raise JinaError("invalid_response", "Jina SVIP Search 未返回结果列表")
            rows = []
            for row in payload["results"]:
                if not isinstance(row, dict):
                    continue
                try:
                    url = public_url(str(row.get("url", "")))
                    image_url = public_url(str(row.get("imageUrl", ""))) if images else None
                except JinaError:
                    continue
                snippet = str(row.get("snippet") or "")
                rows.append({
                    "rank": len(rows) + 1, "title": str(row.get("title") or url),
                    "url": url, "snippet": snippet, "snippet_truncated": False,
                    "evidence_type": "image_search" if images else "search_excerpt",
                    **({"image_url": image_url} if images else {}),
                })
            return {"query": query, "provider": "jina", "mode": None, "endpoint": SEARCH_ENDPOINT,
                    "results": rows, "results_truncated": False, "empty": not rows, "untrusted_content": True}

        return self._cached("search:" + json.dumps(body, sort_keys=True), fetch)

    def read_url(self, item: dict) -> dict:
        """Anonymous Reader with browser rendering and no local content cropping."""
        url = public_url(item["url"])

        def fetch():
            # Jina executes page JavaScript; no selectors or hidden local fallback.
            payload = self._request(PAGE_ENDPOINT, {"url": url}, authenticated=False)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise JinaError("invalid_response", "Jina Reader 未返回正文")
            if not data["content"].strip():
                raise JinaError("empty_page", "Jina Reader 浏览器渲染后没有可读正文，请寻找替代来源或报告证据缺失")
            return {
                "url": url, "source_url": str(data.get("url") or url)[:2000],
                "title": str(data.get("title") or url), "content": data["content"],
                "evidence_type": "reader_extract", "untrusted_content": True,
            }

        return self._cached(f"reader:{url}", fetch)

    def pageshot_url(self, item: dict) -> dict:
        """Anonymous Reader full-page screenshot; binary download happens separately."""
        url = public_url(item["url"])

        def fetch():
            payload = None
            try:
                with self._network_slots:
                    image_url = request_pageshot_url(url, reader_engine=self.reader_engine)
                return {"url": url, "image_url": image_url, "evidence_type": "pageshot",
                        "untrusted_content": True}
            finally:
                self.costs.record("jina", "pageshot", payload, free=True)

        return self._cached(f"pageshot:{url}", fetch)
