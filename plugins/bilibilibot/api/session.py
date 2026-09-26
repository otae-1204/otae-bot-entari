from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid
from random import randint
import re
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from otae_bot.infrastructure.http.tls import ashared_ssl_context

from .wbi import sign_params


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "application/json, text/plain, */*",
}

# Only Bilibili's own API hosts are throttled; RSSHub instances are not.
RATE_LIMITED_HOSTS = ("api.bilibili.com", "api.live.bilibili.com")


class BiliAPIError(Exception):
    pass


class BiliRiskControlError(BiliAPIError):
    def __init__(self, label: str):
        super().__init__(
            f"B站风控校验失败，已尝试临时 Cookie；可配置 BILI_SESSDATA/BILI_BUVID3 提高稳定性 ({label})"
        )


class _SendOnlyCookiePolicy(DefaultCookiePolicy):
    """Send configured cookies but never absorb Set-Cookie from API responses."""

    def set_ok(self, cookie, request) -> bool:
        return False


class BiliSession:
    """Long-lived transport for the Bilibili APIs.

    Owns the HTTP client, the cookie jar, the WBI keys and the risk-control
    refresh. Requests that hit risk control share one refresh: the first caller
    performs it and the others reuse the result instead of piling on more
    requests.
    """

    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    RISK_COOKIE_URL = "https://www.bilibili.com/1/dynamic"
    RISK_GATEWAY_URL = "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi"

    def __init__(
        self,
        *,
        timeout: float = 15,
        sessdata: str = "",
        buvid3: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 0.2,
    ):
        self.timeout = timeout
        self.min_interval = max(0.0, float(min_interval))
        self._transport = transport
        self._http_client: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._locks_loop: asyncio.AbstractEventLoop | None = None
        self._rate_lock: asyncio.Lock | None = None
        self._last_request_at = 0.0
        self._risk_lock: asyncio.Lock | None = None
        self._wbi_lock: asyncio.Lock | None = None
        self.headers = dict(DEFAULT_HEADERS)
        # The long-lived HTTP client shares this jar, so cookie refreshes apply
        # to it immediately.
        self.cookies = httpx.Cookies(CookieJar(policy=_SendOnlyCookiePolicy()))
        self._login_cookies = httpx.Cookies()
        if sessdata:
            self._login_cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        if buvid3:
            self._login_cookies.set("buvid3", buvid3, domain=".bilibili.com")
        self.cookies.update(self._login_cookies)
        self.img_key = ""
        self.sub_key = ""
        self._wbi_updated_at = 0
        self._risk_cookie_updated_at = 0
        self._risk_generation = 0
        self._wbi_generation = 0

    # --- lifecycle ----------------------------------------------------------

    async def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._http_client
        if client is not None and not client.is_closed and self._http_loop is loop:
            return client
        verify: ssl.SSLContext | bool = True if self._transport else await ashared_ssl_context(trust_env=False)
        client = self._http_client
        # Re-check after the await: a concurrent caller may have created it.
        if client is None or client.is_closed or self._http_loop is not loop:
            client = httpx.AsyncClient(
                headers=self.headers,
                cookies=self.cookies.jar,
                timeout=self.timeout,
                follow_redirects=True,
                trust_env=False,
                verify=verify,
                transport=self._transport,
            )
            self._http_client = client
            self._http_loop = loop
        return client

    async def aclose(self) -> None:
        client, self._http_client = self._http_client, None
        if client is not None:
            await client.aclose()

    def _locks(self) -> None:
        """asyncio primitives bind to the loop that created them; rebuild on change."""
        loop = asyncio.get_running_loop()
        if self._locks_loop is loop and self._risk_lock is not None:
            return
        self._locks_loop = loop
        self._risk_lock = asyncio.Lock()
        self._wbi_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def _throttle(self, url: str) -> None:
        if not self.min_interval or not any(host in url for host in RATE_LIMITED_HOSTS):
            return
        self._locks()
        assert self._rate_lock is not None
        async with self._rate_lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    # --- WBI ----------------------------------------------------------------

    def sign(self, params: dict[str, Any]) -> dict[str, Any]:
        return sign_params(params, self.img_key, self.sub_key)

    async def refresh_wbi_keys(self) -> None:
        # The nav endpoint answers -101 for anonymous callers but still ships the
        # keys, so read the payload instead of demanding code 0.
        data = await self.fetch_json(self.NAV_URL)
        img = (data.get("data") or {}).get("wbi_img") or {}
        img_url = img.get("img_url", "")
        sub_url = img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BiliAPIError("Bilibili nav response did not include WBI keys")
        self.img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        self.sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._wbi_updated_at = int(time.time())

    def _wbi_fresh(self) -> bool:
        return bool(self.img_key and self.sub_key and int(time.time()) - self._wbi_updated_at < 3600)

    async def ensure_wbi_keys(self) -> None:
        if self._wbi_fresh():
            return
        await self._refresh_wbi_once(None)

    async def _refresh_wbi_once(self, seen_generation: int | None) -> None:
        """Rotate the WBI keys once; concurrent callers reuse the same rotation.

        `seen_generation=None` means "only if the keys are stale", otherwise the
        keys are rotated unless somebody else already did so after that generation.
        """
        self._locks()
        assert self._wbi_lock is not None
        async with self._wbi_lock:
            if seen_generation is None:
                if self._wbi_fresh():
                    return
            elif self._wbi_generation != seen_generation:
                return  # somebody refreshed while we waited
            await self.refresh_wbi_keys()
            self._wbi_generation += 1

    # --- risk control -------------------------------------------------------

    async def refresh_risk_cookies(self) -> None:
        headers = {
            **self.headers,
            "Host": "space.bilibili.com",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        verify: ssl.SSLContext | bool = True if self._transport else await ashared_ssl_context(trust_env=False)
        # A short-lived client of its own: the temporary cookie must not leak
        # into the long-lived jar (and httpx 0.28 deprecates per-request cookies).
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True,
            trust_env=False,
            verify=verify,
            transport=self._transport,
        ) as client:
            client.cookies.set(
                "_uuid",
                f"{str(uuid.uuid4()).upper()}{randint(0, 99999):05d}infoc",
                domain=".bilibili.com",
            )
            response = await client.get(self.RISK_COOKIE_URL)
            response.raise_for_status()
            spm_match = re.search(r'<meta name="spm_prefix" content="([^"]+?)">', response.text)
            spm_prefix = spm_match.group(1) if spm_match else "333.999"
            payload = {
                "3064": 1,
                "39c8": f"{spm_prefix}.fp.risk",
                "3c43": {"adca": "Linux"},
            }
            try:
                gateway = await client.post(
                    self.RISK_GATEWAY_URL,
                    json={"payload": json.dumps(payload, separators=(",", ":"))},
                )
                gateway.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.debug("[bilibilibot] ExClimbWuzhi gateway returned 404; continuing with page cookies")
                else:
                    raise
            temp_cookies = client.cookies
        self.cookies.clear()
        self.cookies.update(temp_cookies)
        self.cookies.update(self._login_cookies)
        self._risk_cookie_updated_at = int(time.time())

    async def ensure_risk_cookies(self) -> None:
        if self.cookies and int(time.time()) - self._risk_cookie_updated_at < 6 * 3600:
            return
        await self.refresh_risk_cookies()

    async def _refresh_risk_once(self, seen_generation: int) -> None:
        self._locks()
        assert self._risk_lock is not None
        async with self._risk_lock:
            if self._risk_generation != seen_generation:
                return  # the refresh we were waiting for is already done
            await self.refresh_risk_cookies()
            self._risk_generation += 1

    # --- requests -----------------------------------------------------------

    def require_ok(self, data: dict[str, Any], label: str) -> dict[str, Any]:
        code = data.get("code", 0)
        if code == -352:
            raise BiliRiskControlError(label)
        if code != 0:
            raise BiliAPIError(
                f"Bilibili API error for {label}: {code} {data.get('message') or data.get('msg') or ''}"
            )
        return data

    async def fetch_json(
        self, url: str, *, params: dict[str, Any] | None = None, method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One request without risk-control handling or code validation."""
        try:
            await self._throttle(url)
            client = await self._http()
            if method == "POST":
                resp = await client.post(url, json=json_body)
            else:
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            raise BiliAPIError(f"Bilibili request timeout: {url}") from exc
        except httpx.HTTPStatusError as exc:
            raise BiliAPIError(f"Bilibili HTTP {exc.response.status_code}: {url}") from exc
        except ValueError as exc:
            raise BiliAPIError(f"Bilibili response is not JSON: {url}") from exc
        except httpx.HTTPError as exc:
            raise BiliAPIError(f"Bilibili request failed: {exc}") from exc

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        label: str = "",
        signed: bool = False,
        fetcher: Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]] | None = None,
        sign_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Rate-limited request that retries once after a single shared risk refresh.

        `signed` asks for a WBI signature; `sign_params` re-signs the request on
        retry (used when a risk refresh also rotates the keys).
        """
        label = label or url
        wbi_generation: int | None = None
        if signed:
            await self.ensure_wbi_keys()
            wbi_generation = self._wbi_generation
            params = self.sign(dict(sign_params if sign_params is not None else (params or {})))
        send = fetcher or (lambda target, query: self.fetch_json(target, params=query))
        generation = self._risk_generation
        data = await send(url, params)
        if data.get("code") != -352:
            return self.require_ok(data, label)

        await self._refresh_risk_once(generation)
        if signed:
            await self._refresh_wbi_once(wbi_generation)
            params = self.sign(dict(sign_params if sign_params is not None else (params or {})))
        retry = await send(url, params)
        if retry.get("code") == -352:
            raise BiliRiskControlError(label)
        return self.require_ok(retry, label)

    async def post_json(self, url: str, *, json: dict[str, Any] | None = None, label: str = "") -> dict[str, Any]:
        data = await self.fetch_json(url, method="POST", json_body=json)
        return self.require_ok(data, label or url)

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        try:
            client = await self._http()
            resp = await client.get(url, timeout=timeout if timeout is not None else self.timeout)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            raise BiliAPIError(f"Bilibili request failed: {url}: {exc}") from exc

    async def head_location(self, url: str) -> str:
        """Resolve a short link (b23.tv) to its redirect target."""
        client = await self._http()
        resp = await client.get(url, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            return resp.headers["location"]
        if resp.next_request is not None:
            return str(resp.next_request.url)
        return str(resp.url)
