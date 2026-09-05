from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class StageSourceRef:
    source: str
    article_title: str
    revision: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class StageEnemyResistance:
    """One element row exactly as FZ publishes it: `percent` is damage TAKEN, not damage reduced."""

    element: str
    label: str
    percent: float | None = None
    scalar: float | None = None
    color: str = ""

    @property
    def is_standard(self) -> bool:
        return self.percent is not None and abs(self.percent - 100.0) < 1e-9

    @property
    def reduction(self) -> float | None:
        return None if self.percent is None else 100.0 - self.percent


@dataclass(frozen=True, slots=True)
class StageEnemyPoise:
    """The FZ `poise` attribute group; `knots` are the marks on the poise bar, as 0–1 ratios."""

    max_value: float | None = None
    damage_scalar: float | None = None
    recover_seconds: float | None = None
    recover_scalar: float | None = None
    knots: tuple[float, ...] | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.max_value is None
            and self.damage_scalar is None
            and self.recover_seconds is None
            and self.recover_scalar is None
            and self.knots is None
        )


@dataclass(frozen=True, slots=True)
class StageEnemy:
    enemy_id: str
    name: str
    icon_url: str = ""
    level: int | None = None
    count: int | None = None
    hp: int | float | None = None
    attack: int | float | None = None
    defense: int | float | None = None
    article_title: str = ""
    resistances: tuple[StageEnemyResistance, ...] | None = None
    poise: StageEnemyPoise | None = None


@dataclass(frozen=True, slots=True)
class StageWave:
    wave: int | None
    condition: str
    time: float | None
    enemy: StageEnemy


@dataclass(frozen=True, slots=True)
class StageReward:
    item_id: str
    name: str
    icon_url: str = ""
    quantity_text: str = ""
    rarity: int | None = None


@dataclass(frozen=True, slots=True)
class StageFact:
    """One labelled value for the stat strip. Adapters emit these instead of growing the model."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class StageRewardGroup:
    label: str
    items: tuple[StageReward, ...]


@dataclass(frozen=True, slots=True)
class StageRewards:
    """Reward groups plus how many of them the player actually receives."""

    groups: tuple[StageRewardGroup, ...]
    title: str = ""
    select_count: int = 0

    @property
    def is_choice(self) -> bool:
        return self.select_count > 0 and len(self.groups) > 1

    @property
    def items(self) -> tuple[StageReward, ...]:
        return tuple(item for group in self.groups for item in group.items)


@dataclass(frozen=True, slots=True)
class StageBlockEntry:
    name: str
    desc: str = ""
    meta: str = ""
    badges: tuple[str, ...] = ()
    rewards: tuple[StageReward, ...] = ()


@dataclass(frozen=True, slots=True)
class StageBlock:
    """A titled list the card renders without knowing what the gameplay calls it.

    This is the seam that lets a newly published gameplay render on the existing card:
    an adapter turns whatever the source publishes into blocks, and nothing in the
    renderer has to learn the new template.
    """

    key: str
    title: str
    note: str = ""
    entries: tuple[StageBlockEntry, ...] = ()
    facts: tuple[StageFact, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.entries and not self.facts


@dataclass(frozen=True, slots=True)
class BossRushStageDetails:
    boss_name: str
    series_id: str = ""
    series_name: str = ""
    depth_count: int | None = None
    icon_url: str = ""


@dataclass(frozen=True, slots=True)
class EnergyDepositStageDetails:
    region: str = ""
    intensity: str = ""
    matrices: tuple[str, ...] | None = None
    weapon_references: tuple[str, ...] | None = None
    waves: tuple[StageWave, ...] | None = None


@dataclass(frozen=True, slots=True)
class CrisisContractMetric:
    metric_id: str
    name: str
    score: int | None = None
    level: int | None = None
    group_id: str = ""
    lock_ids: tuple[str, ...] = ()
    conflict_id: str = ""


@dataclass(frozen=True, slots=True)
class CrisisContractTaskGroup:
    group_id: str
    name: str
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CrisisContractShop:
    shop_id: str
    name: str
    currency_name: str = ""
    goods_count: int = 0


@dataclass(frozen=True, slots=True)
class CrisisContractStageDetails:
    activity_id: str
    dungeon_id: str
    metrics: tuple[CrisisContractMetric, ...] = ()
    level_scores: tuple[int, ...] = ()
    task_groups: tuple[CrisisContractTaskGroup, ...] = ()
    shops: tuple[CrisisContractShop, ...] = ()


@dataclass(frozen=True, slots=True)
class WarEchoCycle:
    week: int | None
    name: str
    stage_group_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WarEchoStageDetails:
    season_name: str
    stage_group_id: str
    week_count: int | None = None
    max_stars: int | None = None
    cycles: tuple[WarEchoCycle, ...] = ()


@dataclass(frozen=True, slots=True)
class MonumentStageDetails:
    series_id: str
    series_name: str


# Only gameplays whose card needs dedicated semantics get an extension type. Everything
# else — including gameplays FZ has not published yet — travels as StageFact/StageBlock.
StageExtension: TypeAlias = (
    BossRushStageDetails
    | EnergyDepositStageDetails
    | CrisisContractStageDetails
    | WarEchoStageDetails
    | MonumentStageDetails
)


@dataclass(frozen=True, slots=True)
class StageVariant:
    id: str
    label: str
    sort_order: int
    recommended_level: int | None = None
    stamina_cost: int | None = None
    mechanics: tuple[str, ...] | None = None
    enemies: tuple[StageEnemy, ...] | None = None
    rewards: tuple[StageReward, ...] | None = None
    extension: StageExtension | None = None
    facts: tuple[StageFact, ...] = ()
    waves: tuple[StageWave, ...] | None = None
    reward_sets: StageRewards | None = None
    blocks: tuple[StageBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class Stage:
    id: str
    name: str
    aliases: tuple[str, ...]
    family_key: str
    family_name: str
    summary: str
    location: str
    unlock_condition: str
    source: StageSourceRef
    variants: tuple[StageVariant, ...]
    extension: StageExtension | None = None
    icon_url: str = ""
    template_name: str = ""
    facts: tuple[StageFact, ...] = ()
    blocks: tuple[StageBlock, ...] = ()


STAGE_KEY_SEPARATOR = "#"


def make_stage_key(title: str, entry_key: str = "") -> str:
    """One opaque key for a stage, so an article holding several stages stays addressable."""
    return f"{title}{STAGE_KEY_SEPARATOR}{entry_key}" if entry_key else title


def split_stage_key(key: str) -> tuple[str, str]:
    title, separator, entry_key = str(key or "").partition(STAGE_KEY_SEPARATOR)
    return title, (entry_key if separator else "")


@dataclass(frozen=True, slots=True)
class StageCatalogItem:
    title: str
    name: str
    family_key: str
    family_name: str
    revision: str
    updated_at: str
    description: str = ""
    queryable: bool = True
    recommended_level: int | None = None
    region: str = ""
    entry_key: str = ""
    """Selects one stage inside an article that publishes several (a war-echo season, say)."""
    extra_titles: tuple[str, ...] = ()
    """Sibling articles merged into this stage as extra variants."""
    source: str = ""

    @property
    def stage_key(self) -> str:
        return make_stage_key(self.title, self.entry_key)


@dataclass(frozen=True, slots=True)
class StageCatalogGroup:
    key: str
    name: str
    items: tuple[StageCatalogItem, ...]


@dataclass(frozen=True, slots=True)
class StageCatalogView:
    groups: tuple[StageCatalogGroup, ...]
    source: str
    revision: str
    updated_at: str
    page_number: int = 1
    page_count: int = 1
    catalog_family_count: int | None = None
    catalog_queryable_count: int | None = None
    catalog_pending_count: int | None = None
    """Whole-catalog totals. Set only when this view holds one page of several, so a
    sliced page keeps reporting the real counts instead of just what it happens to show."""

    @property
    def family_count(self) -> int:
        if self.catalog_family_count is not None:
            return self.catalog_family_count
        return len(self.groups)

    @property
    def queryable_count(self) -> int:
        if self.catalog_queryable_count is not None:
            return self.catalog_queryable_count
        return sum(item.queryable for group in self.groups for item in group.items)

    @property
    def pending_count(self) -> int:
        if self.catalog_pending_count is not None:
            return self.catalog_pending_count
        return sum(not item.queryable for group in self.groups for item in group.items)


@dataclass(frozen=True, slots=True)
class StageCardView:
    stage: Stage
    mode: str
    selected_variant: StageVariant | None = None
    unreachable_enemies: tuple[str, ...] = ()
    """Enemy articles this render could not fetch — distinct from a source that has no such data."""
