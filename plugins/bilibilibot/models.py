from __future__ import annotations

import json
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
    live_started_at: int = 0  # API start time, persisted across bot restarts.
    live_last_seen_at: int = 0  # Last successful observation of an ongoing stream.


@dataclass(slots=True)
class LiveObservation:
    """One round's view of a live room; `is_live` never means "we failed to look"."""

    uid: str
    room_id: str = ""
    is_live: bool = False
    title: str = ""
    cover: str = ""
    started_at: int = 0
    uname: str = ""
    face: str = ""


@dataclass(frozen=True, slots=True)
class SeenItem:
    """One item the poller has already handled, keyed by (kind, uid, item_id)."""

    kind: str
    uid: str
    item_id: str
    published_at: int = 0


@dataclass(slots=True)
class BiliEvent:
    """A detected change waiting to be turned into outbox rows."""

    kind: str
    uid: str
    card: "BiliCard"
    seen: SeenItem | None = None
    event_key: str = ""


@dataclass(slots=True)
class OutboxRow:
    """One pending notification for one subscriber."""

    id: int
    event_key: str
    kind: str
    uid: str
    card_type: str
    subscriber_type: str
    subscriber_id: str
    card_json: str
    created_at: int
    attempts: int = 0
    next_attempt_at: int = 0
    last_error: str = ""

    def card(self) -> BiliCard:
        return BiliCard(**json.loads(self.card_json))


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
    live_started_at: int = 0
    live_duration_seconds: int | None = None  # Estimated at the poll detecting the end.
