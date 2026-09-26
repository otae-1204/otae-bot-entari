"""Link parsing and short-link expansion (moved out of client.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..models import KIND_LIVE, KIND_VIDEO
from .mapping import bvid_from_url


BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
LIVE_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d+)")
SHORT_LINK_HOST = "b23.tv"
# Punctuation that commonly trails a pasted link in a chat message.
LINK_TRAILING_CHARS = "),\uff0c\u3002]"


@dataclass(slots=True)
class ParsedLink:
    """One recognised link: kind is live/video, value the room number or bvid."""

    kind: str
    value: str
    url: str


async def parse_link(api: Any, text: str) -> ParsedLink | None:
    """Return the first bilibili link in the text, else the first bare BV number."""
    for raw in URL_RE.findall(text):
        url = raw.rstrip(LINK_TRAILING_CHARS)
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if SHORT_LINK_HOST in host:
            url = await expand_short_url(api, url)
            parsed = urlparse(url)
            host = parsed.netloc.lower()
        if "live.bilibili.com" in host:
            match = LIVE_RE.search(url)
            if match:
                return ParsedLink(KIND_LIVE, match.group(1), url)
        if "bilibili.com" in host:
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
