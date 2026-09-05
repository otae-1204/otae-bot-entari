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
    icon_fallbacks: tuple[str, ...] = ()
    preserve_metric_rows: bool = False


@dataclass(slots=True)
class EffectView:
    effect_id: str
    title: str
    description: str
    kind: str
    icon_url: str = ""
    icon_fallbacks: tuple[str, ...] = ()


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
    source_name: str = ""


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
    source_name: str = "api.fz.wiki"


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
    source_name: str = "api.fz.wiki"


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
    equipment_id: str = ""


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
    source_name: str = "api.fz.wiki"


@dataclass(slots=True)
class EquipmentCatalogAttributeView:
    label: str
    value: str = ""
    role: str = ""


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
    main_attribute: str = ""
    sub_attribute: str = ""


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
    attribute_filter: str = ""
    source_version: str = ""
    source_name: str = "api.fz.wiki"


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
    equipment_id: str = ""
    suit_name: str = ""
    stats: list[EquipmentStatView] = field(default_factory=list)


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
    operator_potential: int
    weapon_level: int
    weapon_potential: int
    main_attribute: str
    sub_attribute: str
    weapon_type: str
    operator_icon_url: str = ""
    weapon_icon_url: str = ""
    operator_id: str = ""
    weapon_id: str = ""
    equipment: list[LoadoutEquipmentView] = field(default_factory=list)
    primary_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    ability_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    advanced_stats: list[LoadoutPanelStatView] = field(default_factory=list)
    status_effect_bonus: float = 0.0
    status_effects: list[LoadoutStatusEffectView] = field(default_factory=list)
    effects: list[LoadoutEffectView] = field(default_factory=list)
    source_version: str = ""
    term_styles: dict[str, TermStyleView] = field(default_factory=dict)
    source_name: str = "api.fz.wiki"


@dataclass(slots=True)
class AttendanceRewardView:
    name: str
    count: int = 1
    icon_url: str = ""


@dataclass(slots=True)
class AttendanceRoleView:
    nickname: str
    uid: str
    server_name: str
    status: str
    message: str
    rewards: list[AttendanceRewardView] = field(default_factory=list)
    monthly_count: int | None = None


@dataclass(slots=True)
class AttendanceCardView:
    roles: list[AttendanceRoleView] = field(default_factory=list)
    generated_at: str = ""


@dataclass(slots=True)
class GachaHistoryItemView:
    time: str
    pool_name: str
    item_name: str
    rarity: int
    item_type: str
    detail: str = ""
    icon_path: str = ""


@dataclass(slots=True)
class GachaHistoryView:
    nickname: str
    uid: str
    server_name: str
    page: int
    total_pages: int
    total: int
    pool_filter: str = ""
    items: list[GachaHistoryItemView] = field(default_factory=list)


# ----- 蚀刻章/奖章（medal）模块 -----

@dataclass(slots=True)
class MedalItemView:
    medal_id: str
    name: str
    category_name: str = ""
    group_name: str = ""
    init_level: int = 0
    max_level: int = 0
    can_be_upgraded: bool = False
    can_be_plated: bool = False
    order: int = 0
    icon_url: str = ""
    description: str = ""
    condition: str = ""
    plate_condition: str = ""
    tier_desc: dict[int, str] = field(default_factory=dict)
    tier_cond: dict[int, str] = field(default_factory=dict)
    next_description: str = ""
    next_condition: str = ""
    next_icon_url: str = ""


@dataclass(slots=True)
class MedalSnapshotView:
    """奖章全量快照：既是版本对比基线，也是命令读取的性能缓存。"""
    medals: list[MedalItemView] = field(default_factory=list)
    version: str = ""                       # FZ 根条目 updatedAt[:10] 标签
    fetched_at: int = 0
    source: str = "fz"
    total_count: int = 0
    level_counts: dict[int, int] = field(default_factory=dict)      # {max_level: 数量}
    platable_count: int = 0
    upgradable_count: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)   # {category_name: 数量}


@dataclass(slots=True)
class MedalBaselineView:
    """版本对比基线：akedata 上一游戏版本的 achv_id 集合（源和源对比的 previous 方）。

    与本地 current 快照同为 akedata 源数据，口径一致；只存 diff 所需的 id 黑名单，
    不含名字/图标（新增章展示信息取自 current）。version 用 major.minor（如「1.3」）。
    """
    version: str = ""                       # 上一游戏版本 major.minor 标签
    version_id: str = ""                    # 完整 manifest id，如 "1.3.3@8190425-29"
    ids: list[str] = field(default_factory=list)   # 上一版本全部 achv_id
    fetched_at: int = 0


@dataclass(slots=True)
class MedalDiffView:
    """F1 版本对比视图：当前快照 + 相较上一版本的新增奖章。"""
    current: MedalSnapshotView = field(default_factory=MedalSnapshotView)
    previous_version: str = ""
    new_medals: list[MedalItemView] = field(default_factory=list)


# ----- Phase 2：个人缺章（F2） -----

@dataclass(slots=True)
class MedalProgressView:
    """SDK 玩家奖章进度归一化，按 medal_id 索引。"""
    medal_id: str = ""
    level: int = 0
    plated: bool = False
    init_level: int = 0       # achievementData.initLevel：用于校正森空岛 level 偏移（initLevel>1 时）
    plated_icon: str = ""     # achievementData.platedIcon：镀层后图标（未镀层双卡右卡用）


@dataclass(slots=True)
class MedalMissingView:
    """F2 个人缺章视图：未获得 / 未升满 / 未镀层。"""
    nickname: str = ""
    uid: str = ""
    server_name: str = ""
    snapshot_version: str = ""
    total_count: int = 0
    owned_count: int = 0
    not_obtained: list[MedalItemView] = field(default_factory=list)
    not_maxed: list[MedalItemView] = field(default_factory=list)
    not_plated: list[MedalItemView] = field(default_factory=list)
    not_obtained_count: int = 0       # 截断前的真实未获得总数（统计区用，非显示条目数）
    not_maxed_count: int = 0          # 截断前的真实未升满总数
    not_plated_count: int = 0         # 截断前的真实未镀层总数
    truncated: bool = False
    shown_count: int = 0
    level_counts: dict[int, int] = field(default_factory=dict)
