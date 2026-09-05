from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from typing import Any

from loguru import logger

from ..stages.akedata import AkeDataVersion, parse_akedata_version
from ..account.i18n import localized_text
from ..providers.warfarin import WarfarinAPIError, WarfarinClient
from ..stages.fz import StageDataIncomplete
from ..paths import CALENDAR_DIR


MANIFEST_PATH = CALENDAR_DIR / "current.json"
AKEDATA_IMAGE_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/"
    "dynamicassets/gameplay/ui/sprites"
)


class VersionCalendarError(RuntimeError):
    """Raised when the bundled current-version calendar is incomplete."""


@dataclass(frozen=True, slots=True)
class VersionCalendarSection:
    id: str
    label: str
    english: str
    symbol: str
    accent: str
    rows: int
    row_height: int


@dataclass(frozen=True, slots=True)
class VersionCalendarEntry:
    section: str
    lane: int
    title: str
    subtitle: str
    start_at: str
    end_at: str
    style: str
    accent: str
    art_url: str
    source_kind: str
    source_id: str
    source_time_id: str


@dataclass(frozen=True, slots=True)
class VersionCalendar:
    version: str
    title: str
    english_title: str
    starts_at: str
    ends_at: str
    official_source: str
    revision: str
    sections: tuple[VersionCalendarSection, ...]
    entries: tuple[VersionCalendarEntry, ...]


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise VersionCalendarError(f"版本日历缺少字段：{key}")
    return value


def _validate_timestamp(value: str, field: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise VersionCalendarError(f"版本日历时间格式无效：{field}={value}") from exc


@lru_cache(maxsize=1)
def load_calendar_manifest() -> VersionCalendar:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionCalendarError("无法读取当前版本日历清单") from exc

    starts_at = _required_text(payload, "starts_at")
    ends_at = _required_text(payload, "ends_at")
    _validate_timestamp(starts_at, "starts_at")
    _validate_timestamp(ends_at, "ends_at")

    raw_sections = payload.get("sections")
    raw_entries = payload.get("entries")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise VersionCalendarError("当前版本日历没有分区")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise VersionCalendarError("当前版本日历没有内容")

    sections: list[VersionCalendarSection] = []
    section_ids: set[str] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, dict):
            raise VersionCalendarError(f"版本日历第 {index + 1} 个分区格式无效")
        section = VersionCalendarSection(
            id=_required_text(raw, "id"),
            label=_required_text(raw, "label"),
            english=str(raw.get("english") or "").strip(),
            symbol=str(raw.get("symbol") or "◆").strip(),
            accent=str(raw.get("accent") or "#c90016").strip(),
            rows=max(1, int(raw.get("rows") or 1)),
            row_height=max(30, int(raw.get("row_height") or 42)),
        )
        if section.id in section_ids:
            raise VersionCalendarError(f"版本日历分区重复：{section.id}")
        section_ids.add(section.id)
        sections.append(section)

    entries: list[VersionCalendarEntry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise VersionCalendarError(f"版本日历第 {index + 1} 项格式无效")
        start_at = _required_text(raw, "start_at")
        end_at = str(raw.get("end_at") or "").strip()
        _validate_timestamp(start_at, f"entries[{index}].start_at")
        if end_at:
            _validate_timestamp(end_at, f"entries[{index}].end_at")
        section = _required_text(raw, "section")
        lane = int(raw.get("lane") or 0)
        matching_section = next((item for item in sections if item.id == section), None)
        if matching_section is None:
            raise VersionCalendarError(f"版本日历条目引用未知分区：{section}")
        if lane < 0 or lane >= matching_section.rows:
            raise VersionCalendarError(f"版本日历条目泳道越界：{raw.get('title') or index}")
        source = raw.get("ake") if isinstance(raw.get("ake"), dict) else {}
        entries.append(
            VersionCalendarEntry(
                section=section,
                lane=lane,
                title=_required_text(raw, "title"),
                subtitle=str(raw.get("subtitle") or "").strip(),
                start_at=start_at,
                end_at=end_at,
                style=str(raw.get("style") or "standard").strip(),
                accent=str(raw.get("accent") or "").strip(),
                art_url=_asset_url(str(raw.get("art") or "").strip()),
                source_kind=str(source.get("kind") or "").strip(),
                source_id=str(source.get("id") or "").strip(),
                source_time_id=str(source.get("time_id") or "").strip(),
            )
        )

    return VersionCalendar(
        version=_required_text(payload, "version"),
        title=_required_text(payload, "title"),
        english_title=str(payload.get("english_title") or "").strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        official_source=_required_text(payload, "official_source"),
        revision="bundled",
        sections=tuple(sections),
        entries=tuple(entries),
    )


def _asset_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith(("https://", "http://", "data:")):
        return path
    return f"{AKEDATA_IMAGE_BASE}/{path.lstrip('/')}"


class AkeDataVersionCalendarSource:
    """Hydrate the local visual manifest with current AkeData tables."""

    TABLES = (
        "ActivityTable",
        "TimeRangeTable",
        "GachaCharPoolTable",
        "GachaWeaponPoolTable",
        "I18nTextTable_CN",
    )

    def __init__(self, client: WarfarinClient):
        self.client = client
        self._version: AkeDataVersion | None = None
        self._tables: dict[str, dict[str, Any]] = {}
        self._generation = 0

    def clear_caches(self) -> int:
        self._generation += 1
        removed = len(self._tables)
        self._version = None
        self._tables = {}
        load_calendar_manifest.cache_clear()
        return removed

    async def current(self) -> VersionCalendar:
        calendar = load_calendar_manifest()
        try:
            version = await self._latest_version()
            tables = await self._load_tables(*self.TABLES)
        except (WarfarinAPIError, StageDataIncomplete) as exc:
            logger.warning(f"[endfield] AkeData calendar fallback to bundled manifest: {exc}")
            return calendar
        hydrated = hydrate_calendar_from_akedata(calendar, version, *tables)
        return hydrated

    async def current_ake_primary(self) -> VersionCalendar:
        """Do not relabel a bundled old calendar as the latest game version.

        TableCfg supplies event times but not all official-only regional/story
        milestones. A current complete coverage manifest remains mandatory.
        """
        from ..providers.akedata import game_version_label
        calendar = load_calendar_manifest()
        version = await self._latest_version()
        if calendar.version != game_version_label(version.id):
            raise VersionCalendarError("AKE 日历缺少当前版本的完整事件覆盖清单")
        tables = await self._load_tables(*self.TABLES)
        return hydrate_calendar_from_akedata(calendar, version, *tables)

    async def _latest_version(self) -> AkeDataVersion:
        generation = self._generation
        version = parse_akedata_version(await self.client.akedata_manifest())
        if generation != self._generation:
            return version
        if self._version is None or self._version.id != version.id:
            self._tables = {}
        self._version = version
        return version

    async def _load_tables(self, *names: str) -> tuple[dict[str, Any], ...]:
        generation = self._generation
        version = self._version or await self._latest_version()
        if generation != self._generation:
            return await self._load_tables(*names)
        target = self._tables

        async def load(name: str) -> dict[str, Any]:
            cached = target.get(name)
            if cached is not None:
                return cached
            value = await self.client.akedata_table(version.table_cfg_path, name)
            target[name] = value
            return value

        return tuple(await asyncio.gather(*(load(name) for name in names)))


def hydrate_calendar_from_akedata(
    calendar: VersionCalendar,
    version: AkeDataVersion,
    activity_table: dict[str, Any],
    time_table: dict[str, Any],
    char_pool_table: dict[str, Any],
    weapon_pool_table: dict[str, Any],
    text_table: dict[str, Any],
) -> VersionCalendar:
    entries = tuple(
        _hydrate_entry(
            entry,
            activity_table,
            time_table,
            char_pool_table,
            weapon_pool_table,
            text_table,
        )
        for entry in calendar.entries
    )
    return replace(calendar, revision=version.id, entries=entries)


def _hydrate_entry(
    entry: VersionCalendarEntry,
    activity_table: dict[str, Any],
    time_table: dict[str, Any],
    char_pool_table: dict[str, Any],
    weapon_pool_table: dict[str, Any],
    text_table: dict[str, Any],
) -> VersionCalendarEntry:
    if not entry.source_kind or not entry.source_id:
        return entry

    row: dict[str, Any] | None
    time_id = entry.source_time_id
    art_url = entry.art_url
    title = entry.title
    if entry.source_kind == "activity":
        value = activity_table.get(entry.source_id)
        row = value if isinstance(value, dict) else None
        if row:
            time_id = time_id or str(row.get("timeId") or "")
            tab_image = str(row.get("tabImg") or "").strip()
            if tab_image:
                art_url = _asset_url(f"activity/{tab_image}.png")
    elif entry.source_kind == "char_pool":
        value = char_pool_table.get(entry.source_id)
        row = value if isinstance(value, dict) else None
        if row:
            title = _translated(row.get("name"), text_table) or title
            up_ids = [str(item) for item in row.get("upCharIds") or () if item]
            if up_ids:
                art_url = _asset_url(f"charremoteicon/icon_{up_ids[0]}.png")
    elif entry.source_kind == "weapon_pool":
        value = weapon_pool_table.get(entry.source_id)
        row = value if isinstance(value, dict) else None
        if row:
            title = _translated(row.get("name"), text_table) or title
            time_id = time_id or str(row.get("clientTopTimeId") or "")
            up_ids = [str(item) for item in row.get("upWeaponIds") or () if item]
            if up_ids:
                art_url = _asset_url(f"itemiconbig/{up_ids[0]}.png")
    elif entry.source_kind == "time":
        row = {}
        time_id = time_id or entry.source_id
    else:
        return entry

    start_at, end_at = _time_range(time_table.get(time_id))
    return replace(
        entry,
        title=title,
        start_at=start_at or entry.start_at,
        end_at=end_at if end_at is not None else entry.end_at,
        art_url=art_url,
    )


def _translated(reference: Any, text_table: dict[str, Any]) -> str:
    return localized_text(reference, translations=text_table)


def _time_range(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        return "", None
    ranges = value.get("timeRangeList")
    if not isinstance(ranges, list) or not ranges or not isinstance(ranges[0], dict):
        return "", None
    row = ranges[0]
    start_at = _ake_timestamp(row.get("openTime"))
    raw_end = str(row.get("closeTime") or "").strip()
    return start_at, (_ake_timestamp(raw_end) if raw_end else None)


def _ake_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=datetime.fromisoformat("2026-01-01T00:00:00+08:00").tzinfo
        ).isoformat()
    except ValueError:
        return ""
