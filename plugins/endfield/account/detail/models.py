from __future__ import annotations

from dataclasses import dataclass


COMPACT_THRESHOLD = 44
# Tried in order when the roster needs splitting; the first budget that fits wins.
ACCOUNT_DETAIL_PAGE_BUDGETS = (60, 40, 25)
ACCOUNT_DETAIL_PAGE_LIMIT = ACCOUNT_DETAIL_PAGE_BUDGETS[0]
"""Split past this many operators without waiting for the height ceiling. One image
still renders up to ~105, but by then it is a 12000px, multi-megabyte picture that is
unreadable on a phone — the ceiling is a crash guard, not a size policy."""


@dataclass(frozen=True, slots=True)
class AccountEquipView:
    slot_label: str
    name: str = ""
    icon_url: str = ""
    rarity: int = 0
    type_label: str = ""
    level_label: str = ""
    suit_name: str = ""


@dataclass(frozen=True, slots=True)
class AccountSkillView:
    name: str
    icon_url: str = ""
    level: int | None = None
    max_level: int | None = None
    type_label: str = ""
    damage_type: str = ""
    damage_color: str = "#969a99"
    is_ultimate: bool = False

    @property
    def mastery_level(self) -> int:
        if self.level is None:
            return 0
        return max(0, min(3, self.level - 9))


@dataclass(frozen=True, slots=True)
class AccountWeaponView:
    name: str
    icon_url: str = ""
    rarity: int = 0
    level: int | None = None
    potential_level: int | None = None
    breakthrough_level: int | None = None
    type_label: str = ""
    gem_name: str = ""
    gem_icon_url: str = ""


@dataclass(frozen=True, slots=True)
class AccountOperatorView:
    name: str
    rarity: int = 0
    level: int | None = None
    evolve_phase: int | None = None
    potential_level: int | None = None
    profession: str = ""
    element: str = ""
    element_color: str = "#888888"
    weapon_type: str = ""
    portrait_url: str = ""
    skills: tuple[AccountSkillView, ...] = ()
    weapon: AccountWeaponView | None = None
    equips: tuple[AccountEquipView | None, ...] = (None, None, None, None)
    tactical_name: str = ""
    tactical_icon_url: str = ""

    @property
    def equipped_count(self) -> int:
        return sum(1 for equip in self.equips if equip is not None)


@dataclass(frozen=True, slots=True)
class AccountStatView:
    label: str
    value: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class AccountDetailView:
    nickname: str
    uid: str
    server_name: str = ""
    level: int | None = None
    world_level: int | None = None
    main_mission: str = ""
    avatar_url: str = ""
    saved_at: str = ""
    stats: tuple[AccountStatView, ...] = ()
    operators: tuple[AccountOperatorView, ...] = ()
    page_number: int = 1
    page_count: int = 1
    roster_count: int | None = None
    """Whole-roster size. Set only when this view holds one page of several, so every
    page picks the same row density and still reports the real operator count."""

    @property
    def operator_count(self) -> int:
        if self.roster_count is not None:
            return self.roster_count
        return len(self.operators)

    @property
    def page_operator_count(self) -> int:
        return len(self.operators)

    @property
    def compact(self) -> bool:
        """Tighten every operator row once the roster would outgrow one image."""
        return self.operator_count > COMPACT_THRESHOLD

    @property
    def identity_column(self) -> int:
        return 300 if self.compact else 320

    @property
    def portrait_size(self) -> int:
        return 68 if self.compact else 84

    @property
    def slot_size(self) -> int:
        return 54 if self.compact else 62

    @property
    def skill_icon_size(self) -> int:
        return 44 if self.compact else 54
