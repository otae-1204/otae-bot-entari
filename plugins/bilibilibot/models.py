from __future__ import annotations

from dataclasses import dataclass


KIND_LIVE = "live"
KIND_VIDEO = "video"
KIND_DYNAMIC = "dynamic"
SUPPORTED_KINDS = {KIND_LIVE, KIND_VIDEO, KIND_DYNAMIC}


@dataclass(slots=True)
class TargetInfo:
    kind: str
    uid: str
    name: str = ""
    room_id: str = ""
    avatar_url: str = ""
    latest_id: str = ""
    latest_ts: int = 0
    is_live: bool = False
    last_title: str = ""
    last_cover: str = ""
    last_desc: str = ""
    url: str = ""


@dataclass(slots=True)
class Subscription:
    target_kind: str
    target_uid: str
    subscriber_type: str
    subscriber_id: str


@dataclass(slots=True)
class BiliCard:
    card_type: str
    title: str
    author: str = ""
    subtitle: str = ""
    description: str = ""
    cover_url: str = ""
    avatar_url: str = ""
    url: str = ""
    badge: str = ""
    uid: str = ""
    room_id: str = ""
    item_id: str = ""
    published_at: int = 0
