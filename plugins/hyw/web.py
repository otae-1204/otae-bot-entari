from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from loguru import logger
from lxml import etree, html

from .config import HywError
from .network_errors import classify_error


class WebReadError(HywError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def public_url(url: str) -> bool:
    if len(url) > 2048:
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return False
        if parts.port not in {None, 80, 443}:
            return False
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


async def validate_url(url: str):
    if not public_url(url):
        raise WebReadError("仅支持公开的 HTTP/HTTPS 网页或图片地址。", "address_policy")
    parts = urlsplit(url)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM,
        )
    except OSError:
        raise WebReadError("网页地址无法解析。", "dns") from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise WebReadError("不能读取本机或内网地址。", "address_policy")


async def download(client: httpx.AsyncClient, url: str, *, max_bytes: int = 2_000_000) -> tuple[bytes, str, str]:
    for _ in range(4):
        await validate_url(url)
        async with client.stream("GET", url, follow_redirects=False, timeout=15) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise HywError("网页重定向缺少目标地址。")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise WebReadError(f"网页读取失败（HTTP {response.status_code}）。", f"http_{response.status_code}")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise HywError("网页或图片超过大小限制。")
            return bytes(data), response.headers.get("content-type", ""), str(response.url)
    raise HywError("网页重定向次数过多。")


def parse_search(content: bytes) -> list[dict]:
    document = html.fromstring(content, parser=html.HTMLParser(encoding="utf-8"))
    results = []
    for anchor in document.xpath('//a[contains(@class,"result-link") or contains(@class,"result__a")]'):
        url = urljoin("https://duckduckgo.com/", anchor.get("href", ""))
        if urlsplit(url).hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
            url = parse_qs(urlsplit(url).query).get("uddg", [url])[0]
        if not public_url(url) or url in {item["url"] for item in results}:
            continue
        row = anchor.xpath('ancestor::div[contains(@class,"web-result") or @class="result"]')
        snippet = []
        if row:
            snippet = row[0].xpath('.//*[contains(@class,"result__snippet")]//text()')
        else:
            snippet = anchor.xpath('ancestor::tr/following-sibling::tr[1]//*[contains(@class,"result-snippet")]//text()')
        results.append({"title": anchor.text_content().strip()[:200], "url": url,
                        "snippet": re.sub(r"\s+", " ", " ".join(snippet)).strip()[:500]})
        if len(results) == 5:
            break
    return results


async def search(client: httpx.AsyncClient, query: str, *, time_range: str = "a", kl: str = "") -> dict:
    if not query.strip() or len(query) > 300 or time_range not in {"a", "d", "w", "m", "y"}:
        raise HywError("搜索参数无效：query 应为 1–300 字，time_range 应为 a/d/w/m/y。")
    if kl and not re.fullmatch(r"[a-z]{2}-[a-z]{2}", kl):
        raise HywError("搜索地区应使用 cn-zh、us-en 等格式。")
    params = {"q": query, "df": "" if time_range == "a" else time_range}
    if kl:
        params["kl"] = kl
    # Both official HTML interfaces share the same filters; never parse a challenge as evidence.
    failures = []
    for endpoint in ("https://lite.duckduckgo.com/lite/", "https://html.duckduckgo.com/html/"):
        url = str(httpx.URL(endpoint, params=params))
        host = urlsplit(endpoint).hostname
        try:
            content, _, _ = await download(client, url, max_bytes=1_000_000)
            if b"anomaly.js" in content or b"challenge-form" in content:
                reason = "challenge"
            else:
                results = parse_search(content)
                if results or b"no-results" in content or b"No results found" in content:
                    logger.info("[hyw] search endpoint={} results={}", host, len(results))
                    return {"query": query, "engine": "DuckDuckGo", "time_range": time_range, "kl": kl, "results": results}
                reason = "unexpected_page"
        except WebReadError as error:
            reason = error.code
        except httpx.HTTPError as error:
            reason = classify_error(error).code
        except HywError:
            reason = "web_read"
        except (ValueError, etree.LxmlError):
            reason = "parse_failed"
        # Fixed endpoint and reason only; no search terms, proxy credentials or raw exceptions.
        logger.warning("[hyw] search endpoint={} failed: reason={}", host, reason)
        failures.append(f"{host}: {reason}")
    raise HywError("DuckDuckGo 搜索服务暂时不可用（" + "；".join(failures) + "）。请检查 HYW_SEARCH_PROXY；遇到验证页时请稍后重试。")


async def fetch_page(client: httpx.AsyncClient, url: str) -> dict:
    content, content_type, final_url = await download(client, url)
    if "html" not in content_type and "text/plain" not in content_type:
        raise HywError("目前仅支持 HTML 或纯文本网页。")
    if "html" in content_type:
        charset = re.search(r"charset=[\"']?([\w-]+)", content_type, re.IGNORECASE)
        if charset is None:
            charset = re.search(r"charset=[\"']?([\w-]+)", content[:4096].decode("ascii", errors="ignore"), re.IGNORECASE)
        encoding = charset[1] if charset else "utf-8"
        try:
            parser = html.HTMLParser(encoding=encoding)
        except LookupError:
            parser = html.HTMLParser(encoding="utf-8")
        try:
            document = html.fromstring(content, parser=parser)
        except etree.LxmlError:
            raise HywError("网页没有可读取的正文。") from None
        title = " ".join(document.xpath("//title/text()"))[:200]
        for node in document.xpath("//script|//style|//nav|//footer|//header|//noscript|//form"):
            node.drop_tree()
        main = document.xpath("//main|//article")
        text = (main[0] if main else document).text_content()
    else:
        title, text = final_url, content.decode("utf-8", errors="replace")
    return {"title": title, "url": final_url, "content": re.sub(r"\s+", " ", text).strip()[:8000]}
