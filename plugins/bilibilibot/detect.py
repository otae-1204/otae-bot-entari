"""Pure detection layer for bilibilibot.

Turns "previous state + this round's observation" into "new state + events".
Nothing in this module performs I/O: the current moment arrives as the `now`
argument, and the caller (the poller) owns the transport, the database and
delivery. Keeping these rules free of I/O is what makes the live transition,
the duration estimate and the feed dedup rules unit-testable.

The plan (section 5.3) is the source of truth for the behaviour moved here from
the old `service` poll workers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from .models import (
    BiliCard,
    BiliEvent,
    KIND_DYNAMIC,
    KIND_LIVE,
    KIND_VIDEO,
    LiveObservation,
    SeenItem,
    TargetInfo,
)

# Polling normally runs every minute. A longer gap makes the end time unknowable,
# so the end card falls back to "duration unknown".
LIVE_TIMING_MAX_GAP_SECONDS = 180

BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")


def refined_name(current: str, uid: str, candidate: str) -> str:
    """Adopt the API name only while the stored one is empty or just the uid.

    A name the user chose is never overwritten by the feed.
    """
    candidate = (candidate or "").strip()
    current = (current or "").strip()
    if candidate and (not current or current == uid):
        return candidate
    return current


def bvid_from_card(card: BiliCard) -> str:
    """The BV id a card refers to, if any; used to spot video dynamics."""
    for value in (card.item_id, card.url, card.description):
        match = BV_RE.search(value or "")
        if match:
            return match.group(0)
    return ""


def detect_live(
    prev: TargetInfo, obs: LiveObservation | None, now: int
) -> tuple[TargetInfo, list[BiliEvent]]:
    """Compare one live observation against the stored state.

    `obs` of None means this round failed, or the room was missing from the
    batch response. Such a round must not move the state, must not advance
    `live_last_seen_at` and must not announce the end of a stream.
    """
    if obs is None:
        return prev, []

    # Only a stream we saw recently can carry timing across a failed round.
    recent_observation = (
        prev.is_live
        and 0 < prev.live_last_seen_at <= now
        and now - prev.live_last_seen_at <= LIVE_TIMING_MAX_GAP_SECONDS
    )

    duration: int | None = None
    started_at = obs.started_at
    if obs.is_live:
        if not 0 < started_at <= now:
            # The reported start is unusable, so keep the previous session start
            # instead of resetting it, but only while it is still trustworthy.
            started_at = (
                prev.live_started_at
                if recent_observation
                and 0 < prev.live_started_at <= prev.live_last_seen_at
                else 0
            )
        last_seen_at = now
    else:
        if recent_observation and 0 < prev.live_started_at <= prev.live_last_seen_at:
            duration = now - prev.live_started_at
        started_at = 0
        last_seen_at = 0

    updated = replace(
        prev,
        # The batch endpoint refreshes the display name and avatar every round;
        # blank values keep the stored ones rather than erasing them.
        name=obs.uname or prev.name,
        room_id=obs.room_id or prev.room_id,
        avatar_url=obs.face or prev.avatar_url,
        is_live=obs.is_live,
        last_title=obs.title or prev.last_title,
        last_cover=obs.cover or prev.last_cover,
        live_started_at=started_at,
        live_last_seen_at=last_seen_at,
    )

    if obs.is_live == prev.is_live:
        return updated, []

    card = BiliCard(
        "live_on" if obs.is_live else "live_off",
        updated.last_title or "直播状态变化",
        author=updated.name,
        subtitle="正在直播" if obs.is_live else "直播已结束",
        cover_url=updated.last_cover,
        avatar_url=updated.avatar_url,
        url=f"https://live.bilibili.com/{updated.room_id}",
        badge="LIVE" if obs.is_live else "ENDED",
        uid=updated.uid,
        room_id=updated.room_id,
        live_duration_seconds=duration,
    )
    return updated, [BiliEvent(KIND_LIVE, updated.uid, card)]


def detect_video(
    prev: TargetInfo, latest: BiliCard, *, already_seen: bool
) -> tuple[TargetInfo, list[BiliEvent]]:
    """One UP's newest video: push it only when it is new and not yet recorded."""
    next_name = refined_name(prev.name, prev.uid, latest.author)
    next_avatar = prev.avatar_url or latest.avatar_url
    item_id = latest.item_id or ""

    events: list[BiliEvent] = []
    if item_id and item_id != prev.latest_id and not already_seen:
        latest.author = latest.author or next_name or prev.uid
        latest.avatar_url = latest.avatar_url or next_avatar
        events.append(
            BiliEvent(
                KIND_VIDEO,
                prev.uid,
                latest,
                seen=SeenItem(KIND_VIDEO, prev.uid, item_id, latest.published_at),
            )
        )

    updated = replace(
        prev,
        name=next_name,
        avatar_url=next_avatar,
        latest_id=item_id or prev.latest_id,
        latest_ts=latest.published_at or prev.latest_ts,
        last_title=latest.title or prev.last_title,
        last_cover=latest.cover_url or prev.last_cover,
        last_desc=latest.description or prev.last_desc,
    )
    return updated, events


def detect_dynamic(
    prev: TargetInfo,
    cards: list[BiliCard],
    *,
    seen_ids: set[str],
    video_subscribed: bool,
) -> tuple[TargetInfo, list[BiliEvent], list[SeenItem]]:
    """Handle a feed page oldest-first.

    Returns the new state, the events to push and the items that are only to be
    recorded. The third list carries video dynamics when the same UP is also
    watched for videos: the video subscription already pushes them, so the
    dynamic copy is marked as read without a second notification.
    """
    if not cards:
        return prev, [], []

    events: list[BiliEvent] = []
    marked: list[SeenItem] = []
    next_name = prev.name
    next_avatar = prev.avatar_url
    newest_ts = prev.latest_ts
    newest_id = prev.latest_id

    for card in sorted(cards, key=lambda item: item.published_at):
        next_name = refined_name(next_name, prev.uid, card.author)
        next_avatar = next_avatar or card.avatar_url
        item_id = card.item_id or ""
        if not item_id or card.published_at <= prev.latest_ts or item_id in seen_ids:
            continue
        newest_ts = max(newest_ts, card.published_at)
        newest_id = item_id
        seen = SeenItem(KIND_DYNAMIC, prev.uid, item_id, card.published_at)
        if video_subscribed and bvid_from_card(card):
            marked.append(seen)
            continue
        card.author = card.author or next_name or prev.uid
        card.avatar_url = card.avatar_url or next_avatar
        events.append(BiliEvent(KIND_DYNAMIC, prev.uid, card, seen=seen))

    latest = max(cards, key=lambda item: item.published_at)
    updated = replace(
        prev,
        name=next_name,
        avatar_url=next_avatar,
        latest_id=newest_id or latest.item_id or prev.latest_id,
        latest_ts=newest_ts or latest.published_at or prev.latest_ts,
        last_title=latest.title or prev.last_title,
        last_cover=latest.cover_url or prev.last_cover,
        last_desc=latest.description or prev.last_desc,
    )
    return updated, events, marked


def seen_items_of(events: Iterable[BiliEvent]) -> list[SeenItem]:
    """The items an event batch has already handled, ready to be persisted."""
    return [event.seen for event in events if event.seen is not None]


__all__ = [
    "LIVE_TIMING_MAX_GAP_SECONDS",
    "bvid_from_card",
    "detect_dynamic",
    "detect_live",
    "detect_video",
    "refined_name",
    "seen_items_of",
]
