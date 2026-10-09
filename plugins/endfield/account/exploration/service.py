from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..i18n import localized_text, server_label
from .artwork import artwork_levels, artwork_manifest, map_id
from .models import (
    COLLECTION_COLUMNS,
    CollectionProgress,
    ExplorationLevel,
    ExplorationRegion,
    ExplorationView,
)

BEIJING = timezone(timedelta(hours=8))


def build_exploration_view(
    detail: Mapping[str, Any],
    *,
    uid: str,
    nickname: str = "",
    server_name: str = "",
    version: str = "",
) -> ExplorationView:
    """Read every domain/level; unknown counts must never become zero or completed.

    Names of regions in the packaged artwork come from its local manifest, since
    card/detail often answers in English or with bare IDs; nothing is fetched.
    """
    detail = _mapping(detail)
    base = _mapping(detail.get("base"))
    warnings: list[str] = []
    regions: list[ExplorationRegion] = []
    raw_domains = detail.get("domain")
    if raw_domains is not None and not isinstance(raw_domains, list):
        warnings.append("地区数据格式异常，暂时无法读取。")
    for index, raw_region in enumerate(_rows(raw_domains)):
        if not isinstance(raw_region, Mapping):
            warnings.append("部分地区数据格式异常，未能展示。")
            continue
        region_id = _id(raw_region.get("domainId")) or f"region-{index + 1}"
        levels: list[ExplorationLevel] = []
        raw_collections = raw_region.get("collections")
        collected = {
            _id(row.get("levelId")): row
            for row in _rows(raw_collections)
            if isinstance(row, Mapping) and _id(row.get("levelId"))
        }
        raw_levels = raw_region.get("levels")
        if raw_levels is not None and not isinstance(raw_levels, list):
            warnings.append("部分地区明细格式异常，仅展示可读取的收集记录。")
        seen: set[str] = set()
        for level_index, raw_level in enumerate(_rows(raw_levels)):
            if not isinstance(raw_level, Mapping):
                warnings.append("部分地区数据格式异常，未能展示。")
                continue
            level_id = _id(raw_level.get("levelId"))
            if level_id and level_id in seen:
                warnings.append("接口返回重复地区，按所属地图和地区标识保留首条。")
                continue
            if level_id:
                seen.add(level_id)
            levels.append(_level(raw_level, collected.get(level_id, {}), level_index))
        # Older responses may expose collected amounts before adding level totals.
        for level_id, row in collected.items():
            if level_id not in seen:
                levels.append(
                    _level(
                        {"levelId": level_id, "name": row.get("name")}, row, len(levels)
                    )
                )
        if any(
            progress.inconsistent for level in levels for progress in level.collections
        ):
            warnings.append(
                "部分已收集数大于总量，已保留原值并标注，请以游戏内记录为准。"
            )
        regions.append(
            ExplorationRegion(
                region_id=region_id,
                name=_artwork_region_name(region_id, levels)
                or localized_text(raw_region.get("name"))
                or _id(raw_region.get("domainId"))
                or "未命名地区",
                levels=tuple(levels),
            )
        )
    return ExplorationView(
        nickname=localized_text(base.get("name")) or nickname or "管理员",
        uid=uid,
        server_name=server_label(server_name or localized_text(base.get("serverName"))),
        # currentTs is request time, not the game's last synchronization time.
        saved_at=_timestamp(base.get("saveTime")),
        # The API lists the oldest map first; the card puts the newest on top.
        regions=tuple(reversed(regions)),
        warnings=tuple(dict.fromkeys(warnings)),
        version=version,
    )


def _level(raw: Mapping, fallback: Mapping, index: int) -> ExplorationLevel:
    level_id = _id(raw.get("levelId")) or f"level-{index + 1}"
    values: list[CollectionProgress] = []
    for key, _ in COLLECTION_COLUMNS:
        value = raw.get(key)
        if isinstance(value, Mapping):
            # An explicit invalid count stays unknown; do not conceal it with older data.
            count = (
                _number(value["count"])
                if "count" in value
                else _number(fallback.get(key))
            )
            total = _number(value.get("total"))
        else:
            count = _number(value) if key in raw else _number(fallback.get(key))
            total = None
        values.append(CollectionProgress(count, total))
    known = artwork_levels().get(level_id)
    return ExplorationLevel(
        level_id=level_id,
        name=(known[1]["name"] if known else "")
        or localized_text(raw.get("name"))
        or _id(raw.get("levelId"))
        or "未命名地区",
        collections=tuple(values),
    )


def _artwork_region_name(region_id: str, levels: list[ExplorationLevel]) -> str:
    """The packaged map's name, by (aliased) ID or else the one map its levels share."""
    maps = artwork_manifest()["maps"]
    matched = [item for item in maps if item["id"] == map_id(region_id)]
    if not matched:
        parents = {
            known[0]["id"]: known[0]
            for level in levels
            if (known := artwork_levels().get(level.level_id))
        }
        matched = list(parents.values())
    return matched[0]["name"] if len(matched) == 1 else ""


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _id(value: Any) -> str:
    return (
        str(value).strip()
        if isinstance(value, (str, int)) and not isinstance(value, bool)
        else ""
    )


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _timestamp(value: Any) -> str:
    timestamp = _number(value)
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, BEIJING).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return ""
