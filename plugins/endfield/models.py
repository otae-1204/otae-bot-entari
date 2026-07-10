from __future__ import annotations

from dataclasses import dataclass, field


LEVEL_COLUMNS: tuple[tuple[int, str], ...] = (
    (9, "Lv9"),
    (10, "M1"),
    (11, "M2"),
    (12, "M3"),
)


@dataclass(slots=True)
class SkillLevelView:
    label: str
    level: int
    values: dict[str, str] = field(default_factory=dict)
    cooldown: str = "--"
    cost: str = "--"
    charge: str = "--"
    description: str = ""


@dataclass(slots=True)
class SkillView:
    skill_id: str
    title: str
    icon_id: str = ""
    category: str = "技能"
    description: str = ""
    levels: list[SkillLevelView] = field(default_factory=list)
    extra_levels: dict[str, list[SkillLevelView]] = field(default_factory=dict)


@dataclass(slots=True)
class EffectView:
    effect_id: str
    title: str
    description: str
    kind: str
    icon_url: str = ""


@dataclass(slots=True)
class TermStyleView:
    term: str
    color: str = ""
    icon_url: str = ""


@dataclass(slots=True)
class OperatorView:
    name: str
    slug: str
    operator_id: str
    english_name: str = ""
    rarity: int = 0
    profession: str = "未知职业"
    damage_type: str = "未知属性"
    weapon_type: str = "未知武器"
    species: str = "未知种族"
    species_label: str = "种族"
    tags: list[str] = field(default_factory=list)
    icon_url: str = ""
    round_icon_url: str = ""
    portrait_url: str = ""
    skills: list[SkillView] = field(default_factory=list)
    talents: list[EffectView] = field(default_factory=list)
    potentials: list[EffectView] = field(default_factory=list)
    term_styles: dict[str, TermStyleView] = field(default_factory=dict)
    source_version: str = ""


@dataclass(slots=True)
class WeaponSkillLevelView:
    level: int
    values: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(slots=True)
class WeaponSkillView:
    title: str
    description: str = ""
    levels: list[WeaponSkillLevelView] = field(default_factory=list)


@dataclass(slots=True)
class WeaponView:
    name: str
    slug: str
    title: str
    weapon_id: str = ""
    source_name: str = "api.fz.wiki"
    english_name: str = ""
    rarity: int = 0
    weapon_type: str = "未知武器"
    operator_names: list[str] = field(default_factory=list)
    max_level: int = 0
    max_atk: int | str = "--"
    icon_url: str = ""
    skills: list[WeaponSkillView] = field(default_factory=list)
    rich_text_styles: dict[str, dict] = field(default_factory=dict)
    rich_text_links: dict[str, dict] = field(default_factory=dict)
    source_version: str = ""
