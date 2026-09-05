"""Challenge models, independent of browser rendering and event handlers."""

from __future__ import annotations

from dataclasses import (
    dataclass,
)
from time import (
    time,
)
from typing import (
    Sequence,
)


@dataclass(frozen=True, slots=True)
class ChallengeIdentity:
    nickname: str
    server_name: str
    uid: str
    source: str = "森空岛"
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ChallengeMember:
    char_id: str
    avatar_url: str = ""
    level: int = 0
    potential: int = 0
    rarity: str = ""
    property: str = ""


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    members: tuple[ChallengeMember, ...] = ()
    record_ts: int = 0
    pass_time: int = 0
    first_pass_ts: int = 0

    @property
    def available(self) -> bool:
        return bool(self.members or self.record_ts or self.pass_time or self.first_pass_ts)


@dataclass(frozen=True, slots=True)
class ChallengeEnemy:
    name: str
    level: int = 0
    image_url: str = ""
    ability: str = ""
    desc: str = ""


@dataclass(frozen=True, slots=True)
class MonumentDungeon:
    id: str
    name: str
    difficulty: str
    passed: bool = False
    desc: str = ""
    feature: str = ""
    recommend_level: int = 0
    record: ChallengeRecord = ChallengeRecord()
    enemies: tuple[ChallengeEnemy, ...] = ()


@dataclass(frozen=True, slots=True)
class MonumentGroup:
    id: str
    name: str
    pic_url: str = ""
    activity_name: str = ""
    start_ts: int = 0
    end_ts: int = 0
    is_active: bool = False
    stages: tuple[tuple[MonumentDungeon, MonumentDungeon], ...] = ()
    medal_name: str = ""
    medal_icon_url: str = ""
    medal_plated_icon_url: str = ""
    medal_level: int = 0
    medal_plated: bool = False

    @property
    def total_stages(self) -> int:
        return sum(len(pair) for pair in self.stages)

    @property
    def passed_stages(self) -> int:
        return sum(1 for pair in self.stages for item in pair if item.passed)


@dataclass(frozen=True, slots=True)
class MonumentPayload:
    groups: tuple[MonumentGroup, ...] = ()

    @property
    def has_records(self) -> bool:
        """Whether the response contains any player-visible progress."""
        return any(
            dungeon.passed or dungeon.record.available
            for group in self.groups
            for pair in group.stages
            for dungeon in pair
        )

    def current(self, now_ts: int | None = None) -> MonumentGroup | None:
        return _current_item(self.groups, now_ts, active_attr="is_active")

    def history_pages(self, per_page: int = 2) -> tuple[tuple[MonumentGroup, ...], ...]:
        return _chunks(self.groups, per_page)


@dataclass(frozen=True, slots=True)
class WarDungeon:
    id: str
    name: str
    difficulty: str
    passed: bool = False
    first_pass_ts: int = 0
    desc: str = ""
    feature: str = ""
    recommend_level: int = 0
    plus_task: bool = False
    additional_target: str = ""
    record: ChallengeRecord = ChallengeRecord()
    enemies: tuple[ChallengeEnemy, ...] = ()


@dataclass(frozen=True, slots=True)
class WarGroup:
    name: str
    star: int = 0
    plus_task: bool = False
    normal: WarDungeon | None = None
    hard: WarDungeon | None = None
    cruel: WarDungeon | None = None

    def dungeon(self, difficulty: str) -> WarDungeon | None:
        return {"normal": self.normal, "hard": self.hard, "cruel": self.cruel}.get(difficulty)


@dataclass(frozen=True, slots=True)
class WarWeek:
    id: str
    name: str
    start_ts: int = 0
    end_ts: int = 0
    stars: int = 0
    all_plus_tasks: bool = False
    groups: tuple[WarGroup, ...] = ()

    def current(self, now_ts: int | None = None) -> bool:
        now = int(time()) if now_ts is None else int(now_ts)
        return bool(self.start_ts <= now <= self.end_ts) if self.start_ts and self.end_ts else False


@dataclass(frozen=True, slots=True)
class WarSeason:
    id: str
    name: str
    kv_url: str = ""
    header_url: str = ""
    start_ts: int = 0
    end_ts: int = 0
    stars: int = 0
    all_plus_tasks: bool = False
    weeks: tuple[WarWeek, ...] = ()

    def current(self, now_ts: int | None = None) -> bool:
        now = int(time()) if now_ts is None else int(now_ts)
        return bool(self.start_ts <= now <= self.end_ts) if self.start_ts and self.end_ts else False

    def current_week(self, now_ts: int | None = None) -> WarWeek | None:
        return _current_item(self.weeks, now_ts, active_attr="current")


@dataclass(frozen=True, slots=True)
class WarAchievement:
    name: str
    star: int = 0
    first_pass_ts: int = 0


@dataclass(frozen=True, slots=True)
class WarEchoPayload:
    seasons: tuple[WarSeason, ...] = ()
    achievements: tuple[WarAchievement, ...] = ()

    @property
    def has_records(self) -> bool:
        return bool(self.achievements) or any(
            dungeon.passed or dungeon.record.available
            for season in self.seasons
            for week in season.weeks
            for group in week.groups
            for dungeon in (group.normal, group.hard, group.cruel)
            if dungeon is not None
        )

    def current(self, now_ts: int | None = None) -> WarSeason | None:
        return _current_item(self.seasons, now_ts, active_attr="current")

    def history_pages(self) -> tuple[tuple[WarSeason, ...], ...]:
        return tuple((item,) for item in self.seasons)


class ChallengeResolutionError(ValueError):
    """A user-facing challenge name could not be resolved."""


class ChallengeAmbiguousError(ChallengeResolutionError):
    def __init__(self, query: str, candidates: Sequence[str]):
        self.query = query
        self.candidates = tuple(dict.fromkeys(str(item) for item in candidates if item))
        super().__init__(f"“{query}”有多个可能：{'、'.join(self.candidates[:5])}")


def _current_item(items, now_ts, *, active_attr):
    if not items:
        return None
    now = int(time()) if now_ts is None else int(now_ts)
    active = []
    for item in items:
        flag = getattr(item, active_attr, None)
        if callable(flag):
            try:
                flag = flag(now)
            except TypeError:
                flag = flag()
        if flag:
            active.append(item)
    if active:
        return max(active, key=lambda item: (_int(getattr(item, "end_ts", 0)), _int(getattr(item, "start_ts", 0))))
    return max(items, key=lambda item: (_int(getattr(item, "end_ts", 0)), _int(getattr(item, "start_ts", 0))))


def _chunks(items, size):
    size = max(1, int(size))
    return tuple(tuple(items[index:index + size]) for index in range(0, len(items), size))


def _int(value):
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0
