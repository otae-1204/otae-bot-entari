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
    form_descriptions: list[tuple[str, str]] = field(default_factory=list)
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


@dataclass(slots=True)
class OperatorCatalogItemView:
    name: str
    title: str
    operator_id: str = ""
    english_name: str = ""
    rarity: int = 0
    element: str = ""
    element_color: str = "#888888"
    profession: str = ""
    weapon_type: str = ""
    icon_url: str = ""
    element_icon_url: str = ""
    profession_icon_url: str = ""
    weapon_type_icon_url: str = ""


@dataclass(slots=True)
class OperatorCatalogProfessionView:
    name: str
    icon_url: str = ""
    items: list[OperatorCatalogItemView] = field(default_factory=list)


@dataclass(slots=True)
class OperatorCatalogElementView:
    name: str
    color: str = "#888888"
    icon_url: str = ""
    professions: list[OperatorCatalogProfessionView] = field(default_factory=list)


@dataclass(slots=True)
class OperatorCatalogView:
    title: str
    elements: list[OperatorCatalogElementView] = field(default_factory=list)
    total_count: int = 0
    element_filter: str = ""
    profession_filter: str = ""
    source_version: str = ""


@dataclass(slots=True)
class WeaponCatalogItemView:
    name: str
    title: str
    weapon_id: str = ""
    english_name: str = ""
    rarity: int = 0
    weapon_type: str = ""
    max_level: int = 0
    max_atk: int | str = "--"
    icon_url: str = ""
    weapon_type_icon_url: str = ""
    substrate_icon_url: str = ""
    terms_main: list[str] = field(default_factory=list)
    terms_sub: list[str] = field(default_factory=list)
    terms_skill: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WeaponCatalogGroupView:
    name: str
    icon_url: str = ""
    items: list[WeaponCatalogItemView] = field(default_factory=list)


@dataclass(slots=True)
class WeaponCatalogView:
    title: str
    groups: list[WeaponCatalogGroupView] = field(default_factory=list)
    total_count: int = 0
    weapon_type_filter: str = ""
    source_version: str = ""


@dataclass(slots=True)
class EquipmentStatView:
    label: str
    value: str
    values: list[str] = field(default_factory=list)
    icon_key: str = ""


@dataclass(slots=True)
class EquipmentPieceView:
    name: str
    slot_type: str = "装备"
    icon_url: str = ""


@dataclass(slots=True)
class EquipmentView:
    name: str
    title: str
    equipment_id: str = ""
    rarity: int = 0
    max_level: int = 0
    part_type: str = ""
    slot_type: str = "装备"
    suit_name: str = ""
    group_name: str = ""
    description: str = ""
    flavor: str = ""
    icon_url: str = ""
    stats: list[EquipmentStatView] = field(default_factory=list)
    suit_required_count: int = 0
    suit_description: str = ""
    suit_pieces: list[EquipmentPieceView] = field(default_factory=list)
    acquisition: str = "未知方式"
    term_styles: dict[str, TermStyleView] = field(default_factory=dict)
    source_version: str = ""


@dataclass(slots=True)
class EquipmentCatalogAttributeView:
    label: str
    value: str = ""


@dataclass(slots=True)
class EquipmentCatalogItemView:
    name: str
    title: str
    group_name: str
    equipment_id: str = ""
    level: int = 0
    rarity: int = 0
    slot_type: str = "装备"
    icon_url: str = ""
    attributes: list[EquipmentCatalogAttributeView] = field(default_factory=list)


@dataclass(slots=True)
class EquipmentCatalogGroupView:
    name: str
    items: list[EquipmentCatalogItemView] = field(default_factory=list)
    suit_name: str = ""
    suit_required_count: int = 0
    suit_effect_description: str = ""


@dataclass(slots=True)
class EquipmentCatalogView:
    title: str
    groups: list[EquipmentCatalogGroupView] = field(default_factory=list)
    total_count: int = 0
    rarity_filter: str = "gold"
    source_version: str = ""


@dataclass(slots=True)
class LoadoutPanelStatView:
    key: str
    label: str
    value: str
    detail: str = ""


@dataclass(slots=True)
class LoadoutEquipmentView:
    name: str
    slot_type: str
    enhance_levels: tuple[int, ...] = ()
    icon_url: str = ""
    suit_name: str = ""


@dataclass(slots=True)
class LoadoutEffectView:
    source: str
    description: str
    active: bool = False


@dataclass(slots=True)
class LoadoutStatusLevelView:
    level: int
    value: str
    detail: str
    duration: str


@dataclass(slots=True)
class LoadoutStatusEffectView:
    name: str
    source: str
    forced: bool = False
    levels: list[LoadoutStatusLevelView] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class LoadoutView:
    operator_name: str
    weapon_name: str
    operator_level: int
    weapon_level: int
    weapon_potential: int
    main_attribute: str
    sub_attribute: str
    weapon_type: str
    operator_icon_url: str = ""
    weapon_icon_url: str = ""
    equipment: list[LoadoutEquipmentView] = field(default_factory=list)
    primary_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    ability_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    advanced_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    status_effect_bonus: float = 0.0
    status_effects: list[LoadoutStatusEffectView] = field(default_factory=list)
    effects: list[LoadoutEffectView] = field(default_factory=list)
    source_version: str = ""
    term_styles: dict[str, TermStyleView] = field(default_factory=dict)
