"""Link parsing and short-link expansion (moved out of client.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..models import KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO
from .mapping import bvid_from_url


BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
# Chat text pastes links straight into the sentence, so the match must end at
# the first full-width mark: those never appear in a Bilibili link (they would
# arrive percent-encoded), and swallowing them made the anchored path patterns
# below reject the whole URL.
LINK_STOP_CHARS = (
    "\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a\uff09\u3011\u300b\u201d\u2019"
    "\u2026\u301c\uff5e\u3000"
)
URL_RE = re.compile(rf"https?://[^\s<>\"{LINK_STOP_CHARS}]+")
# Path-only patterns: the host is checked separately, and a full match keeps
# unrelated pages (e.g. live.bilibili.com/activity/...) from parsing as ids.
LIVE_RE = re.compile(r"^/(?:blanc/|h5/)?(\d+)/?$")
T_DYNAMIC_RE = re.compile(r"^/(\d+)/?$")
OPUS_RE = re.compile(r"^/opus/(\d+)/?$")
DYNAMIC_RE = re.compile(r"^/dynamic/(\d+)/?$")
SHORT_LINK_HOST = "b23.tv"
# ASCII punctuation that commonly trails a pasted link in a chat message.
LINK_TRAILING_CHARS = ")]}>,.;:!?'\""


def _hostname(parsed: Any) -> str:
    """Lower-cased host with the port and a trailing root dot removed.

    ``netloc`` keeps both, so ``host:443`` and ``bilibili.com.`` used to fail
    the exact/suffix host checks.
    """
    host = (parsed.hostname or "").lower()
    return host[:-1] if host.endswith(".") else host


def _is_host(host: str, expected: str) -> bool:
    return host == expected or host.endswith("." + expected)


def _is_bilibili_host(host: str) -> bool:
    return host == "bilibili.com" or host.endswith(".bilibili.com")


@dataclass(slots=True)
class ParsedLink:
    """One recognised link: kind is live/video/dynamic, value is its id."""

    kind: str
    value: str
    url: str


async def parse_link(api: Any, text: str) -> ParsedLink | None:
    """Return the first bilibili link in the text, else the first bare BV number."""
    for raw in URL_RE.findall(text):
        url = raw.rstrip(LINK_TRAILING_CHARS)
        parsed = urlparse(url)
        host = _hostname(parsed)
        if _is_host(host, SHORT_LINK_HOST):
            url = await expand_short_url(api, url)
            parsed = urlparse(url)
            host = _hostname(parsed)
        if _is_host(host, "t.bilibili.com"):
            match = T_DYNAMIC_RE.match(parsed.path)
            if match:
                return ParsedLink(KIND_DYNAMIC, match.group(1), url)
        if _is_host(host, "live.bilibili.com"):
            match = LIVE_RE.match(parsed.path)
            if match:
                return ParsedLink(KIND_LIVE, match.group(1), url)
        if _is_bilibili_host(host):
            match = OPUS_RE.match(parsed.path)
            if match:
                return ParsedLink(KIND_DYNAMIC, match.group(1), url)
            match = DYNAMIC_RE.match(parsed.path)
            if match:
                return ParsedLink(KIND_DYNAMIC, match.group(1), url)
            bvid = bvid_from_url(url)
            if bvid:
                return ParsedLink(KIND_VIDEO, bvid, url)
    bvid_match = BV_RE.search(text)
    if bvid_match:
        bvid = bvid_match.group(0)
        return ParsedLink(KIND_VIDEO, bvid, f"https://www.bilibili.com/video/{bvid}")
    return None


async def expand_short_url(api: Any, url: str) -> str:
    """Resolve a b23.tv short link through the shared session."""
    return await api.session.head_location(url)
