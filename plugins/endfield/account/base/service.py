from __future__ import annotations

import re
import statistics
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .models import (
    AccountBaseView,
    MoodOperatorView,
    MoodSkillView,
    SettlementRate,
    SettlementRegionView,
    SettlementView,
    SpaceshipRoomView,
)
from ..detail.names import AccountDetailNameMap
from ..i18n import localized_text, server_label
from ..store import EndfieldStore, SettlementSnapshot
from ...gacha.service import format_timestamp


MIN_SAMPLE_SECONDS = 60
MAX_SAMPLE_SECONDS = 72 * 60 * 60
SNAPSHOT_RETENTION_SECONDS = 30 * 24 * 60 * 60
BASE_DRAIN_PERCENT_PER_HOUR = 7.2
BASE_RECOVERY_PERCENT_PER_HOUR = 12.0

_MOOD_REDUCTION_RE = re.compile(r"心情消耗降低\s*(\d+(?:\.\d+)?)%")
_MOOD_RECOVERY_RE = re.compile(r"心情恢复(?:速度)?提升\s*(\d+(?:\.\d+)?)%")
_ROMAN_NUMERALS = ("I", "II", "III", "IV", "V", "VI")
_ROOM_ORDER = {0: 0, 5: 1, 1: 2, 2: 3}
_ROOM_NAMES = {0: "总控中枢", 1: "制造舱", 2: "培养舱", 5: "会客室"}


def build_account_base_view(
    detail: Mapping[str, Any],
    *,
    uid: str,
    role_id: str,
    server_id: str,
    nickname: str = "",
    server_name: str = "",
    store: EndfieldStore | None = None,
    captured_at: int | None = None,
    name_map: AccountDetailNameMap | None = None,
) -> AccountBaseView:
    detail = detail or {}
    base = _mapping(detail.get("base"))
    current_ts = (
        _int_or_none(captured_at)
        or _int_or_none(detail.get("currentTs"))
        or int(time.time())
    )
    names = name_map or AccountDetailNameMap()
    characters = _character_index(detail, names)
    current_snapshots: list[SettlementSnapshot] = []
    regions: list[SettlementRegionView] = []

    for domain in _sequence(detail.get("domain")):
        region = _mapping(domain)
        settlements: list[SettlementView] = []
        for raw_settlement in _sequence(region.get("settlements")):
            settlement = _mapping(raw_settlement)
            settlement_id = _text(settlement.get("id"))
            if not settlement_id:
                continue
            level = max(0, _int_or_none(settlement.get("level")) or 0)
            current_money = max(0, _int_or_none(settlement.get("remainMoney")) or 0)
            money_max = max(0, _int_or_none(settlement.get("moneyMax")) or 0)
            officer_ids = _identifier_tuple(settlement.get("officerCharIds"))
            officer_signature = ",".join(sorted(officer_ids))
            officer = next(
                (characters.get(identifier) for identifier in officer_ids if characters.get(identifier)),
                {},
            )
            snapshot = SettlementSnapshot(
                role_id=str(role_id),
                server_id=str(server_id),
                settlement_id=settlement_id,
                settlement_level=level,
                money_max=money_max,
                officer_signature=officer_signature,
                remain_money=current_money,
                captured_at=current_ts,
            )
            current_snapshots.append(snapshot)
            history = (
                store.list_settlement_snapshots(
                    str(role_id),
                    str(server_id),
                    settlement_id,
                    since=current_ts - MAX_SAMPLE_SECONDS,
                )
                if store is not None
                else []
            )
            settlements.append(
                SettlementView(
                    settlement_id=settlement_id,
                    name=_mapped_text(
                        names.settlement_names,
                        settlement_id,
                        settlement.get("name"),
                    ) or settlement_id,
                    level=level,
                    current_money=current_money,
                    money_max=money_max,
                    officer_signature=officer_signature,
                    officer_name=_text(officer.get("name")),
                    officer_avatar_url=_text(officer.get("avatar_url")),
                    rate=estimate_settlement_rate(history, snapshot),
                )
            )
        if settlements:
            regions.append(
                SettlementRegionView(
                    name=_mapped_text(
                        names.domain_names,
                        region.get("domainId"),
                        region.get("name"),
                    ) or "未知地区",
                    region_id=_text(region.get("domainId")),
                    settlements=tuple(settlements),
                )
            )

    if store is not None and current_snapshots:
        store.add_settlement_snapshots(
            current_snapshots,
            retention_seconds=SNAPSHOT_RETENTION_SECONDS,
            now=current_ts,
        )

    rooms = _build_rooms(detail, characters, names)
    return AccountBaseView(
        nickname=_text(base.get("name")) or nickname or "未知管理员",
        uid=str(uid or "--"),
        server_name=server_label(server_name or _text(base.get("serverName"))),
        saved_at=format_timestamp(current_ts),
        regions=tuple(regions),
        rooms=rooms,
    )


def estimate_settlement_rate(
    history: Sequence[SettlementSnapshot],
    current: SettlementSnapshot,
) -> SettlementRate:
    compatible = [
        row
        for row in history
        if row.settlement_level == current.settlement_level
        and row.money_max == current.money_max
        and row.officer_signature == current.officer_signature
        and current.captured_at - MAX_SAMPLE_SECONDS <= row.captured_at <= current.captured_at
    ]
    by_timestamp = {row.captured_at: row for row in compatible}
    by_timestamp[current.captured_at] = current
    points = [by_timestamp[key] for key in sorted(by_timestamp)]
    if len(points) < 2 or current.money_max <= 0 or current.remain_money >= current.money_max:
        return SettlementRate()

    segment: list[SettlementSnapshot] = []
    for point in points:
        if not segment:
            segment = [point]
            continue
        previous = segment[-1]
        if (
            point.remain_money < previous.remain_money
            or previous.remain_money >= previous.money_max
            or point.settlement_level != previous.settlement_level
            or point.money_max != previous.money_max
            or point.officer_signature != previous.officer_signature
        ):
            segment = [point]
            continue
        segment.append(point)

    slopes: list[float] = []
    valid_points: set[int] = set()
    for previous, point in zip(segment, segment[1:]):
        elapsed = point.captured_at - previous.captured_at
        if not MIN_SAMPLE_SECONDS <= elapsed <= MAX_SAMPLE_SECONDS:
            continue
        if previous.remain_money >= previous.money_max or point.remain_money >= point.money_max:
            continue
        delta = point.remain_money - previous.remain_money
        if delta <= 0:
            continue
        slopes.append(delta * 3600.0 / elapsed)
        valid_points.update((previous.captured_at, point.captured_at))

    if not slopes:
        return SettlementRate()
    rate = float(statistics.median(slopes))
    span = max(valid_points) - min(valid_points) if len(valid_points) > 1 else 0
    if len(slopes) >= 4 and span >= 60 * 60:
        confidence = "high"
    elif len(slopes) >= 2 and span >= 10 * 60:
        confidence = "medium"
    else:
        confidence = "low"
    return SettlementRate(
        value_per_hour=rate,
        source="sampled",
        confidence=confidence,
        sample_count=len(valid_points),
        sample_span_seconds=span,
    )


def _build_rooms(
    detail: Mapping[str, Any],
    characters: Mapping[str, Mapping[str, str]],
    name_map: AccountDetailNameMap,
) -> tuple[SpaceshipRoomView, ...]:
    raw_rooms = [
        _mapping(item)
        for item in _sequence(_mapping(detail.get("spaceShip")).get("rooms"))
        if isinstance(item, Mapping)
    ]
    occurrence: dict[int, int] = {}
    prepared: list[tuple[int, int, Mapping[str, Any], str]] = []
    for source_index, room in enumerate(raw_rooms):
        room_type = _int_or_none(room.get("type"))
        if room_type is None:
            room_type = -1
        room_chars = _sequence(room.get("chars"))
        if room_type == 3 and not room_chars and not _text(room.get("id")):
            continue
        number = occurrence.get(room_type, 0)
        occurrence[room_type] = number + 1
        name = _room_name(room_type, number, name_map.spaceship_room_names)
        prepared.append((_ROOM_ORDER.get(room_type, 99), source_index, room, name))
    prepared.sort(key=lambda item: (item[0], item[1]))

    recovery_bonus = _global_recovery_bonus(prepared, characters)
    rooms: list[SpaceshipRoomView] = []
    for _, _, room, name in prepared:
        room_type = _int_or_none(room.get("type"))
        room_type = -1 if room_type is None else room_type
        entries = [_mapping(item) for item in _sequence(room.get("chars"))]
        drain_reduction = _room_drain_reduction(room_type, entries, characters)
        drain = BASE_DRAIN_PERCENT_PER_HOUR * max(0.0, 1.0 - drain_reduction)
        recovery = BASE_RECOVERY_PERCENT_PER_HOUR * (1.0 + recovery_bonus)
        operators = tuple(
            _build_mood_operator(entry, characters, drain=drain, recovery=recovery)
            for entry in entries[:3]
        )
        rooms.append(
            SpaceshipRoomView(
                room_type=room_type,
                name=name,
                level=max(0, _int_or_none(room.get("level")) or 0),
                operators=operators,
            )
        )
    return tuple(rooms)


def _build_mood_operator(
    entry: Mapping[str, Any],
    characters: Mapping[str, Mapping[str, str]],
    *,
    drain: float,
    recovery: float,
) -> MoodOperatorView:
    char_id = _text(entry.get("charId"))
    character = characters.get(char_id, {})
    mood = (_float_or_none(entry.get("physicalStrength")) or 0.0) / 100.0
    skills = tuple(
        MoodSkillView(
            name=_text(skill.get("name")),
            description=_text(skill.get("description")),
            icon_url=_text(skill.get("icon_url")),
            mood_effect=_mood_effect(_text(skill.get("description"))),
        )
        for skill in character.get("skills", ())
        if isinstance(skill, Mapping)
    )
    return MoodOperatorView(
        char_id=char_id,
        name=_text(character.get("name")) or char_id or "未知干员",
        avatar_url=_text(entry.get("avatarUrl")) or _text(character.get("avatar_url")),
        mood_percent=max(0.0, min(100.0, mood)),
        skills=skills,
        drain_percent_per_hour=drain,
        recovery_percent_per_hour=recovery,
    )


def _global_recovery_bonus(
    prepared_rooms: Sequence[tuple[int, int, Mapping[str, Any], str]],
    characters: Mapping[str, Mapping[str, str]],
) -> float:
    total = 0.0
    for _, _, room, _name in prepared_rooms:
        if _int_or_none(room.get("type")) != 0:
            continue
        for entry in _sequence(room.get("chars")):
            character = characters.get(_text(_mapping(entry).get("charId")), {})
            for skill in character.get("skills", ()):
                description = _text(_mapping(skill).get("description"))
                match = _MOOD_RECOVERY_RE.search(description)
                if match and "总控中枢" in description:
                    total += float(match.group(1)) / 100.0
    return total


def _room_drain_reduction(
    room_type: int,
    entries: Sequence[Mapping[str, Any]],
    characters: Mapping[str, Mapping[str, str]],
) -> float:
    room_keyword = _ROOM_NAMES.get(room_type, "")
    total = 0.0
    for entry in entries:
        character = characters.get(_text(entry.get("charId")), {})
        for skill in character.get("skills", ()):
            description = _text(_mapping(skill).get("description"))
            match = _MOOD_REDUCTION_RE.search(description)
            if match and room_keyword and room_keyword in description:
                total += float(match.group(1)) / 100.0
    return total


def _mood_effect(description: str) -> str:
    reduction = _MOOD_REDUCTION_RE.search(description)
    if reduction:
        return f"消耗 -{reduction.group(1)}%"
    recovery = _MOOD_RECOVERY_RE.search(description)
    if recovery:
        return f"回复 +{recovery.group(1)}%"
    return ""


def _character_index(
    detail: Mapping[str, Any], name_map: AccountDetailNameMap
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw_character in _sequence(detail.get("chars")):
        character = _mapping(raw_character)
        char_data = _mapping(character.get("charData"))
        active_ids = {
            _text(value)
            for value in _sequence(_mapping(character.get("talent")).get("latestSpaceshipSkillNodes"))
            if _text(value)
        }
        active_skills = tuple(
            {
                "id": _text(skill.get("id")),
                "name": _mapped_text(
                    name_map.spaceship_skill_names,
                    skill.get("id"),
                    skill.get("name"),
                ),
                "description": _mapped_text(
                    name_map.spaceship_skill_descriptions,
                    skill.get("id"),
                    skill.get("desc"),
                ),
                "icon_url": _text(skill.get("iconUrl")),
            }
            for item in _sequence(char_data.get("cultivationTalents"))
            if (skill := _mapping(item)) and _text(skill.get("id")) in active_ids
        )
        record: Mapping[str, Any] = {
            "name": _mapped_text(
                name_map.character_names,
                char_data.get("id") or character.get("charId") or character.get("id"),
                char_data.get("name"),
            ),
            "avatar_url": _text(char_data.get("avatarSqUrl")),
            "skills": active_skills,
        }
        identifiers = {
            _text(character.get("id")),
            _text(char_data.get("id")),
        }
        for item in _sequence(char_data.get("cultivationTalents")):
            derived = _spaceship_skill_char_id(_text(_mapping(item).get("id")))
            if derived:
                identifiers.add(derived)
        for node_id in active_ids:
            derived = _spaceship_skill_char_id(node_id)
            if derived:
                identifiers.add(derived)
        for identifier in identifiers:
            if identifier:
                result[identifier] = record
    return result


def _spaceship_skill_char_id(skill_id: str) -> str:
    match = re.fullmatch(r"spaceship_skill_(chr_.+)_\d+_\d+", str(skill_id or ""))
    return match.group(1) if match else ""


def _room_name(
    room_type: int,
    occurrence: int,
    localized_names: Mapping[str, str] | None = None,
) -> str:
    base = _mapped_text(localized_names or {}, str(room_type), _ROOM_NAMES.get(room_type))
    if not base:
        return f"舱室 {room_type}" if room_type >= 0 else "未知舱室"
    if room_type != 1:
        return base
    suffix = _ROMAN_NUMERALS[occurrence] if occurrence < len(_ROMAN_NUMERALS) else str(occurrence + 1)
    return f"{base} {suffix}"


def _identifier_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(_text(item) for item in value if _text(item))
    text = _text(value)
    if not text:
        return ()
    return tuple(item for item in re.split(r"[,，\s]+", text) if item)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _text(value: Any) -> str:
    return localized_text(value)


def _mapped_text(names: Mapping[str, str], identifier: Any, fallback: Any) -> str:
    key = _text(identifier)
    if key:
        value = _text(names.get(key))
        if value:
            return value
    return _text(fallback)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
