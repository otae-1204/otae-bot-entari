"""Domain models for the Tibo reset radar.

The models intentionally keep source evidence next to the normalized values.  A
radar message is useful only when a user can follow it back to the public post
which caused the conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


SOURCE_CODEXRADAR = "codexradar"
SOURCE_CODEX_RESET = "codex-reset"

RELEVANCE_DIRECT = "direct"
RELEVANCE_INDIRECT = "indirect"
RELEVANCE_NONE = "none"
RELEVANCES = {RELEVANCE_DIRECT, RELEVANCE_INDIRECT, RELEVANCE_NONE}

EVENT_OFFICIAL_ANNOUNCEMENT = "official_announcement"
EVENT_EXPECTED_WINDOW = "expected_window"
EVENT_SUSPECTED = "suspected"
EVENT_CONFIRMED = "confirmed"
EVENT_UNCONFIRMED = "unconfirmed"
EVENT_REJECTED = "rejected"
EVENT_KINDS = {
    EVENT_OFFICIAL_ANNOUNCEMENT,
    EVENT_EXPECTED_WINDOW,
    EVENT_SUSPECTED,
    EVENT_CONFIRMED,
    EVENT_UNCONFIRMED,
    EVENT_REJECTED,
}


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp and always return an aware UTC datetime."""

    if isinstance(value, datetime):
        result = value
    elif value:
        text = str(value).strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso_or_empty(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


@dataclass(slots=True)
class TiboPost:
    post_id: str
    text: str
    url: str
    source_time: datetime | None = None
    translation: str = ""
    analysis: str = ""
    relevance: str = RELEVANCE_NONE
    phrases: str = ""
    page_updated_at: datetime | None = None
    content_fingerprint: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.post_id = str(self.post_id).strip()
        self.text = str(self.text or "").strip()
        self.url = str(self.url or "").strip()
        self.relevance = self.relevance if self.relevance in RELEVANCES else RELEVANCE_NONE


@dataclass(slots=True)
class ResetEvent:
    event_id: str
    summary: str
    url: str
    announced_at: datetime | None = None
    effective_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_label: str = ""
    window_timezone: str = ""
    preview: bool = False
    scope: str = ""
    confidence: str = ""
    source: str = ""
    source_label: str = ""
    reset_kind: str = ""
    event_type: str = ""
    group: str = ""
    audience: tuple[str, ...] = ()
    reason_tags: tuple[str, ...] = ()
    announcement_state: str = ""
    observation_result: str = ""
    reset_verification_status: str = ""
    status: str = EVENT_UNCONFIRMED
    localized_summary: str = ""
    content_fingerprint: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_names: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.event_id = str(self.event_id).strip()
        self.summary = str(self.summary or "").strip()
        self.url = str(self.url or "").strip()
        if self.status not in EVENT_KINDS:
            self.status = EVENT_UNCONFIRMED

    @property
    def is_confirmed(self) -> bool:
        return self.status == EVENT_CONFIRMED


@dataclass(slots=True)
class SourceState:
    source_name: str
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    upstream_updated_at: datetime | None = None
    fingerprint: str = ""
    stale: bool = False
    last_error: str = ""
    consecutive_failures: int = 0


@dataclass(slots=True)
class RadarSnapshot:
    posts: list[TiboPost]
    events: list[ResetEvent]
    source_states: list[SourceState]
    collected_at: datetime

    @property
    def relevant_posts(self) -> list[TiboPost]:
        return [post for post in self.posts if post.relevance != RELEVANCE_NONE]
