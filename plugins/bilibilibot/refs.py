from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse


UID_PREFIX_RE = re.compile(r"^uid:(\d+)$", re.IGNORECASE)
ROOM_PREFIX_RE = re.compile(r"^room:(\d+)$", re.IGNORECASE)
DIGITS_RE = re.compile(r"^\d+$")

SPACE_HOSTS = ("space.bilibili.com",)
LIVE_HOSTS = ("live.bilibili.com",)


@dataclass(frozen=True, slots=True)
class TargetRef:
    by: Literal["uid", "room"]
    value: str  # plain digit string
    raw: str  # what the user actually typed, for replies


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _first_digit_segment(path: str) -> str:
    for segment in path.split("/"):
        if DIGITS_RE.match(segment):
            return segment
    return ""


def parse_target_ref(raw: str) -> TargetRef:
    """Resolve one command argument into an explicit UID or live room reference.

    Plain digits are always a UID; a live room requires the `room:` prefix or a
    live room link. Unrecognised input raises ValueError with a user-facing
    message.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(_unrecognised(raw))

    if DIGITS_RE.match(text):
        return TargetRef("uid", text, raw)

    match = UID_PREFIX_RE.match(text)
    if match:
        return TargetRef("uid", match.group(1), raw)

    match = ROOM_PREFIX_RE.match(text)
    if match:
        return TargetRef("room", match.group(1), raw)

    parsed = urlparse(text if "//" in text else f"https://{text}")
    host = (parsed.netloc or "").lower()
    if host:
        host = host.split("@")[-1].split(":")[0]
        if _host_matches(host, SPACE_HOSTS):
            uid = _first_digit_segment(parsed.path)
            if uid:
                return TargetRef("uid", uid, raw)
        if _host_matches(host, LIVE_HOSTS):
            room_id = _first_digit_segment(parsed.path)
            if room_id:
                return TargetRef("room", room_id, raw)

    raise ValueError(_unrecognised(raw))


def _unrecognised(raw: str) -> str:
    return f'无法识别 "{raw}"，请使用 UID、room:直播间号 或直播间链接'
