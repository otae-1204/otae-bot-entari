from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SettlementRate:
    value_per_hour: float | None = None
    source: str = "pending"
    confidence: str = ""
    sample_count: int = 0
    sample_span_seconds: int = 0

    @property
    def available(self) -> bool:
        return self.value_per_hour is not None and self.value_per_hour > 0


@dataclass(frozen=True, slots=True)
class SettlementView:
    settlement_id: str
    name: str
    level: int = 0
    current_money: int = 0
    money_max: int = 0
    officer_signature: str = ""
    officer_name: str = ""
    officer_avatar_url: str = ""
    rate: SettlementRate = SettlementRate()

    @property
    def fill_ratio(self) -> float:
        if self.money_max <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current_money / self.money_max))

    @property
    def is_full(self) -> bool:
        return self.money_max > 0 and self.current_money >= self.money_max

    @property
    def hours_to_full(self) -> float | None:
        if self.is_full:
            return 0.0
        if not self.rate.available or self.money_max <= self.current_money:
            return None
        return (self.money_max - self.current_money) / float(self.rate.value_per_hour or 1)


@dataclass(frozen=True, slots=True)
class SettlementRegionView:
    name: str
    region_id: str = ""
    settlements: tuple[SettlementView, ...] = ()


@dataclass(frozen=True, slots=True)
class MoodSkillView:
    name: str
    description: str = ""
    icon_url: str = ""
    mood_effect: str = ""


@dataclass(frozen=True, slots=True)
class MoodOperatorView:
    char_id: str
    name: str
    avatar_url: str = ""
    mood_percent: float = 0.0
    skills: tuple[MoodSkillView, ...] = ()
    drain_percent_per_hour: float = 7.2
    recovery_percent_per_hour: float = 12.0

    @property
    def continuous_work_hours(self) -> float | None:
        if self.drain_percent_per_hour <= 0:
            return None
        return max(0.0, self.mood_percent) / self.drain_percent_per_hour

    @property
    def full_recovery_hours(self) -> float | None:
        if self.recovery_percent_per_hour <= 0:
            return None
        return max(0.0, 100.0 - self.mood_percent) / self.recovery_percent_per_hour


@dataclass(frozen=True, slots=True)
class SpaceshipRoomView:
    room_type: int
    name: str
    level: int = 0
    operators: tuple[MoodOperatorView, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountBaseView:
    nickname: str
    uid: str
    server_name: str = ""
    saved_at: str = ""
    regions: tuple[SettlementRegionView, ...] = ()
    rooms: tuple[SpaceshipRoomView, ...] = ()

    @property
    def settlement_count(self) -> int:
        return sum(len(region.settlements) for region in self.regions)

    @property
    def room_count(self) -> int:
        return len(self.rooms)
