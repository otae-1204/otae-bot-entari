from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import LiveObservation, TargetInfo, KIND_LIVE


BATCH_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
LIVE_USER_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"
DEFAULT_BATCH_CHUNK = 50


def parse_beijing_live_time(value: str, now: int) -> int:
    """Parse Bilibili's Beijing wall-clock live_time; reject anything but 0 < ts <= now."""
    try:
        started_at = int(
            datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone(timedelta(hours=8)))
            .timestamp()
        )
    except (ValueError, OverflowError, OSError):
        return 0
    return started_at if 0 < started_at <= now else 0


def observation_from_status(entry: dict[str, Any], uid: str, *, now: int) -> LiveObservation:
    """Map one batch entry; live_status 0 (offline) and 2 (carousel) are both not live."""
    live_status = int(entry.get("live_status") or 0)
    return LiveObservation(
        uid=str(entry.get("uid") or uid),
        room_id=str(entry.get("room_id") or ""),
        is_live=live_status == 1,
        title=str(entry.get("title") or ""),
        cover=str(entry.get("cover_from_user") or entry.get("keyframe") or ""),
        # The batch endpoint reports live_time as a unix second, not a string.
        started_at=(
            int(entry.get("live_time") or 0)
            if live_status == 1 and 0 < int(entry.get("live_time") or 0) <= now
            else 0
        ),
        uname=str(entry.get("uname") or ""),
        face=str(entry.get("face") or ""),
    )


async def batch_live_status(
    session, uids: list[str], *, chunk: int = DEFAULT_BATCH_CHUNK, now: int | None = None
) -> dict[str, LiveObservation]:
    """uid -> observation for one chunked request set.

    UIDs missing from the response were not observed this round: callers must not
    treat that as "went offline".
    """
    now = int(time.time()) if now is None else int(now)
    result: dict[str, LiveObservation] = {}
    for start in range(0, len(uids), max(1, chunk)):
        # The endpoint documents numeric uids; non-numeric ids are passed through.
        batch = [int(uid) if str(uid).isdigit() else uid for uid in uids[start : start + max(1, chunk)] if uid]
        if not batch:
            continue
        data = await session.post_json(BATCH_URL, json={"uids": batch}, label="live batch")
        payload = data.get("data") or {}
        for uid, entry in payload.items():
            if isinstance(entry, dict):
                result[str(uid)] = observation_from_status(entry, str(uid), now=now)
    return result


async def room_info(session, room_id: str) -> dict[str, Any]:
    data = await session.fetch_json(ROOM_INFO_URL, params={"room_id": room_id})
    if data.get("code") == 1:
        return {}
    return session.require_ok(data, f"live room {room_id}").get("data") or {}


async def live_user(session, uid: str) -> dict[str, Any]:
    data = session.require_ok(
        await session.fetch_json(LIVE_USER_URL, params={"uid": uid}), f"live user {uid}"
    )
    return data.get("data") or {}


def live_start_timestamp(live: dict[str, Any], now: int | None = None) -> int:
    """Single-room get_info still reports live_time as a Beijing wall-clock string."""
    now = int(time.time()) if now is None else int(now)
    if int(live.get("live_status") or 0) != 1:
        return 0
    return parse_beijing_live_time(str(live.get("live_time") or ""), now)


def live_observation_from_room(room: dict[str, Any], uid: str, *, now: int) -> LiveObservation:
    live_status = int(room.get("live_status") or 0)
    return LiveObservation(
        uid=uid,
        room_id=str(room.get("room_id") or ""),
        is_live=live_status == 1,
        title=str(room.get("title") or ""),
        cover=str(room.get("user_cover") or room.get("cover") or ""),
        started_at=live_start_timestamp(room, now),
        uname=str(room.get("uname") or ""),
        face=str(room.get("face") or ""),
    )


def target_from_room(room: dict[str, Any], uid: str, *, now: int) -> TargetInfo:
    observation = live_observation_from_room(room, uid, now=now)
    return TargetInfo(
        KIND_LIVE,
        uid,
        name=observation.uname or uid,
        room_id=observation.room_id,
        is_live=observation.is_live,
        last_title=observation.title,
        last_cover=observation.cover,
        live_started_at=observation.started_at,
        live_last_seen_at=now if observation.is_live else 0,
    )