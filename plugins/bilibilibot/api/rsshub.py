"""RSSHub racing plus RSS/Atom parsing (moved out of client.py).

The feed helpers are pure functions; `first_item` races the configured
instances concurrently and returns as soon as one of them answers.
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Awaitable, Callable, Iterable

from loguru import logger

from .session import BiliAPIError


DEFAULT_BASE_URLS = [
    "https://rss.materium.io",
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.moeyy.cn",
    "https://rsshub.ktachibana.party",
]

# At most this many instance failures are spelled out in the summary error.
MAX_REPORTED_FAILURES = 3
ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
FETCH_BUDGET_SECONDS = 8.0

# Titles of the form "<author> 的 B站投稿视频"; the suffix is not part of the name.
AUTHOR_TITLE_SUFFIXES = [
    " 的 Bilibili 投稿视频",
    " 的 bilibili 投稿视频",
    " 的 B站投稿视频",
    " 的 Bilibili 视频",
    " 的 bilibili 视频",
    " 的 B站视频",
    " 的 Bilibili 动态",
    " 的 bilibili 动态",
    " 的 B站动态",
]

# fetch(base_url, route, deadline=...) -> {"base_url": str, "item": dict}
FetchFirstItem = Callable[..., Awaitable[dict[str, Any]]]


def merge_base_urls(configured: Iterable[str] | None) -> list[str]:
    """Configured instances come first, then the defaults, deduplicated."""
    result: list[str] = []
    for item in [*(configured or []), *DEFAULT_BASE_URLS]:
        normalized = item.strip().rstrip("/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def clean_author_title(value: str) -> str:
    text = " ".join(str(value or "").split())
    for suffix in AUTHOR_TITLE_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def first_text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def html_to_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(value.split())


def extract_image_url(value: str) -> str:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', value or "", flags=re.I)
    return html.unescape(match.group(1)) if match else ""


def parse_rss_time(value: str) -> int:
    if not value:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(value).timestamp())
    except Exception:
        try:
            return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:
            return 0


def parse_first_item(xml_text: str) -> dict[str, Any]:
    """First RSS item (or Atom entry) of a feed, normalised to one dict shape."""
    root = ET.fromstring(xml_text)
    channel_title = clean_author_title(first_text(root, ".//channel/title"))
    item = root.find(".//item")
    if item is not None:
        description_html = first_text(item, "description") or first_text(
            item, CONTENT_NS
        )
        return {
            "title": first_text(item, "title"),
            "link": first_text(item, "link"),
            "description": html_to_text(description_html),
            "cover_url": extract_image_url(description_html),
            "published_at": parse_rss_time(first_text(item, "pubDate")),
            "author": channel_title,
        }

    entry = root.find(f".//{ATOM_NS}entry")
    if entry is not None:
        description_html = first_text(entry, f"{ATOM_NS}summary") or first_text(
            entry, f"{ATOM_NS}content"
        )
        link_el = entry.find(f"{ATOM_NS}link")
        return {
            "title": first_text(entry, f"{ATOM_NS}title"),
            "link": (link_el.get("href") if link_el is not None else ""),
            "description": html_to_text(description_html),
            "cover_url": extract_image_url(description_html),
            "published_at": parse_rss_time(
                first_text(entry, f"{ATOM_NS}updated")
                or first_text(entry, f"{ATOM_NS}published")
            ),
            "author": clean_author_title(first_text(root, f"{ATOM_NS}title")),
        }
    raise BiliAPIError("RSSHub feed contains no item")


def compact_failures(errors: list[str]) -> str:
    compact = "; ".join(errors[:MAX_REPORTED_FAILURES])
    if len(errors) > MAX_REPORTED_FAILURES:
        compact += f"; ... and {len(errors) - MAX_REPORTED_FAILURES} more"
    return compact


async def first_item(
    fetch: FetchFirstItem,
    base_urls: list[str],
    route: str,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Race every instance and return the first item; the rest are cancelled."""
    if not base_urls:
        raise BiliAPIError(
            "all RSSHub instances unavailable: no RSSHub base urls configured"
        )

    tasks = [
        asyncio.create_task(fetch(base_url, route, deadline=deadline))
        for base_url in base_urls
    ]
    errors: list[str] = []
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                for rest in pending:
                    rest.cancel()
                logger.debug(
                    f"[bilibilibot] RSSHub fallback succeeded via {result['base_url']}{route}"
                )
                return result["item"]
        raise BiliAPIError(
            f"all RSSHub instances unavailable: {compact_failures(errors)}"
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def fetch_first_item(
    session,
    base_url: str,
    route: str,
    *,
    timeout: float,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Fetch one instance's feed under the shared absolute deadline."""
    url = base_url.rstrip("/") + route
    budget = min(timeout, FETCH_BUDGET_SECONDS)
    if deadline is not None:
        budget = min(budget, max(0.0, deadline - asyncio.get_running_loop().time()))
    try:
        text = await session.get_text(url, timeout=budget)
        return {"base_url": base_url.rstrip("/"), "item": parse_first_item(text)}
    except Exception as exc:
        logger.debug(f"[bilibilibot] RSSHub fallback failed for {url}: {exc}")
        raise BiliAPIError(f"{base_url.rstrip('/')}: {exc}") from exc
