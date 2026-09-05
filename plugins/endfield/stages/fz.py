from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from hashlib import sha1
from typing import Any

from loguru import logger

from ..account.i18n import localized_text
from ..providers.warfarin import WarfarinClient
from .models import (
    BossRushStageDetails,
    CrisisContractMetric,
    CrisisContractShop,
    CrisisContractStageDetails,
    CrisisContractTaskGroup,
    EnergyDepositStageDetails,
    Stage,
    StageBlock,
    StageBlockEntry,
    StageCatalogGroup,
    StageCatalogItem,
    StageCatalogView,
    StageEnemy,
    StageEnemyPoise,
    StageEnemyResistance,
    StageFact,
    StageReward,
    StageRewardGroup,
    StageRewards,
    StageSourceRef,
    StageVariant,
    StageWave,
    WarEchoCycle,
    WarEchoStageDetails,
    split_stage_key,
)


STAGE_DIRECTORY_RULE_VERSION = "stage-directory-v2"
ENEMY_ARTICLE_PREFIX = "敌人/"
GENERIC_FAMILY_KEY = "other"


@dataclass(frozen=True, slots=True)
class StageFamily:
    """How one gameplay is discovered. New gameplays are added here, never in the renderer."""

    key: str
    name: str
    category: str = ""
    expand: bool = False
    """Its articles hold several stages, so the catalog has to open them to enumerate."""


# Ordered as the catalog renders them. A gameplay FZ publishes later still lands in the
# archive through GENERIC_FAMILY_KEY, so a new template is queryable before it is modelled.
STAGE_FAMILIES: tuple[StageFamily, ...] = (
    StageFamily("boss_rush", "危境再现", category="危境再现"),
    StageFamily("crisis_fragment", "危境碎片", category="危境碎片"),
    StageFamily("energy_deposit", "能量淤积点", category="能量淤积点"),
    StageFamily("resource", "资源副本", category="协议空间"),
    StageFamily("crisis_contract", "危机合约", category="危机合约"),
    StageFamily("war_echo", "战争回响", category="战争回响", expand=True),
    StageFamily("challenge_activity", "挑战活动", category="挑战活动"),
    StageFamily(GENERIC_FAMILY_KEY, "其他玩法", category="副本"),
)
FAMILY_ORDER = tuple(family.key for family in STAGE_FAMILIES)
FAMILY_NAMES = {family.key: family.name for family in STAGE_FAMILIES}
_FAMILY_BY_KEY = {family.key: family for family in STAGE_FAMILIES}

# Hub articles describe a gameplay rather than a stage; they carry no template to parse.
HUB_TITLES = frozenset({"副本/资源", "副本/干员养成", "副本/武器养成", "危境再现", "活动/战争回响"})
ENERGY_HEAVY_PREFIX = "重度"


class FZStageSource:
    def __init__(self, client: WarfarinClient):
        self.client = client

    async def catalog(self) -> StageCatalogView:
        items = await self._expand(_merge_energy_intensities(await self._discover()))
        groups = tuple(
            StageCatalogGroup(
                key=family_key,
                name=FAMILY_NAMES[family_key],
                items=tuple(
                    sorted(
                        (item for item in items if item.family_key == family_key),
                        key=lambda item: item.name,
                    )
                ),
            )
            for family_key in FAMILY_ORDER
            if any(item.family_key == family_key for item in items)
        )
        digest_input = "|".join(
            f"{item.stage_key}:{item.revision}:{item.updated_at}"
            for group in groups
            for item in group.items
        )
        revision = sha1(f"{STAGE_DIRECTORY_RULE_VERSION}|{digest_input}".encode("utf-8")).hexdigest()[:16]
        updated_at = max((item.updated_at for item in items), default="")
        return StageCatalogView(groups, "FZ Wiki", revision, updated_at)

    async def _discover(self) -> tuple[StageCatalogItem, ...]:
        """One catalog request per registered gameplay; the first family to claim a title wins."""
        payloads = await asyncio.gather(
            *(self.client.fz_articles(category=family.category) for family in STAGE_FAMILIES),
            return_exceptions=True,
        )
        items: dict[str, StageCatalogItem] = {}
        for family, payload in zip(STAGE_FAMILIES, payloads):
            if isinstance(payload, BaseException):
                logger.warning(
                    f"[endfield] stage catalog family={family.key} error={type(payload).__name__}"
                )
                continue
            for raw in _articles(payload):
                item = _catalog_item(raw, family)
                if item is not None and item.title not in items:
                    items[item.title] = item
        return tuple(items.values())

    async def _expand(self, items: tuple[StageCatalogItem, ...]) -> tuple[StageCatalogItem, ...]:
        """Open the articles that publish several stages so each one is separately queryable."""
        targets = [item for item in items if _FAMILY_BY_KEY[item.family_key].expand]
        if not targets:
            return items
        payloads = await asyncio.gather(
            *(self.client.fz_article_by_title(item.title) for item in targets),
            return_exceptions=True,
        )
        expanded: dict[str, list[StageCatalogItem]] = {}
        for item, payload in zip(targets, payloads):
            if isinstance(payload, BaseException):
                logger.warning(
                    f"[endfield] stage expand title={item.title} error={type(payload).__name__}"
                )
                continue
            entries = _expand_entries(payload, item)
            if entries:
                expanded[item.title] = entries
        if not expanded:
            return items
        result: list[StageCatalogItem] = []
        for item in items:
            result.extend(expanded.get(item.title, [item]))
        return _dedupe_entries(tuple(result))

    async def stage(self, key: str) -> tuple[Stage, tuple[str, ...]]:
        """Returns the stage plus the enemy articles this call could not reach."""
        title, entry_key = split_stage_key(key)
        if title in HUB_TITLES:
            raise StageDataIncomplete(f"“{title}”只是玩法说明条目，暂无可查询的关卡资料。")
        item = await self._catalog_item_for(title, entry_key)
        titles = (title, *(item.extra_titles if item is not None else ()))
        payloads = await asyncio.gather(*(self.client.fz_article_by_title(one) for one in titles))
        stage = parse_fz_stage(payloads[0], entry_key=entry_key, family_key=_family_key_of(item))
        if len(payloads) > 1:
            stage = _merge_sibling_variants(stage, payloads[1:])
        return await self.attach_enemy_resistances(stage)

    async def _catalog_item_for(self, title: str, entry_key: str) -> StageCatalogItem | None:
        """The catalog knows the family and the sibling articles a single title cannot carry."""
        try:
            catalog = await self.catalog()
        except Exception as exc:  # noqa: BLE001 - a stage query must survive a catalog hiccup
            logger.warning(f"[endfield] stage catalog lookup error={type(exc).__name__}")
            return None
        for group in catalog.groups:
            for item in group.items:
                if item.title == title and item.entry_key == entry_key:
                    return item
        return None

    async def attach_enemy_resistances(self, stage: Stage) -> tuple[Stage, tuple[str, ...]]:
        """Enemy articles hold the resistances and poise knots the stage rows do not carry."""
        titles = sorted(
            {
                enemy.article_title
                for variant in stage.variants
                for enemy in _variant_enemies(variant)
                if enemy.article_title and _needs_enemy_article(enemy)
            }
        )
        if not titles:
            return stage, ()
        lookup, unreachable = await self._fetch_enemy_details(titles)
        return (_with_enemy_details(stage, lookup) if lookup else stage), unreachable

    async def _fetch_enemy_details(
        self, titles: list[str]
    ) -> tuple[dict[str, _EnemyDetails], tuple[str, ...]]:
        async def one(title: str) -> tuple[str, _EnemyDetails | None, bool]:
            try:
                data = await self.client.fz_article_by_title(title)
            except Exception as exc:  # noqa: BLE001 - a missing enemy article must not fail the card
                logger.warning(f"[endfield] stage enemy detail source=fz error={type(exc).__name__}")
                return title, None, True
            return title, _EnemyDetails(parse_enemy_resistances(data), parse_enemy_poise(data)), False

        results = await asyncio.gather(*(one(title) for title in titles))
        # A present-but-empty payload must stay distinct from "we never got an answer".
        lookup = {title: detail for title, detail, _ in results if detail is not None}
        return lookup, tuple(title for title, _, failed in results if failed)


class StageDataIncomplete(ValueError):
    pass


def _articles(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if not isinstance(data, dict):
        return ()
    return tuple(item for item in data.get("articles") or () if isinstance(item, dict))


def _catalog_item(raw: dict[str, Any], family: StageFamily) -> StageCatalogItem | None:
    title = _text(raw.get("title"))
    if not title or title in HUB_TITLES:
        return None
    if family.key == GENERIC_FAMILY_KEY and _claimed_by_named_family(raw):
        return None
    description = _text(raw.get("description"))
    recommended_level, region = _catalog_hints(description)
    return StageCatalogItem(
        title=title,
        name=_catalog_name(title, family),
        family_key=family.key,
        family_name=family.name,
        revision=_text(raw.get("currentRevisionId")),
        updated_at=_text(raw.get("updatedAt")),
        description=description,
        recommended_level=recommended_level,
        region=region,
        source="fz",
    )


def _claimed_by_named_family(raw: dict[str, Any]) -> bool:
    """Keep the catch-all family for titles no registered gameplay recognises."""
    categories = {_text(item) for item in raw.get("categories") or ()}
    return any(
        family.category in categories
        for family in STAGE_FAMILIES
        if family.key != GENERIC_FAMILY_KEY
    )


def _catalog_name(title: str, family: StageFamily) -> str:
    """The name a player types: the last path segment, without the gameplay prefix."""
    name = title.rsplit("/", 1)[-1]
    for prefix in (f"{family.name}·", "协议空间·"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    if family.key == "energy_deposit":
        return name.split("·", 1)[-1]
    return name


def _catalog_hints(description: str) -> tuple[int | None, str]:
    """Pull the level and region the catalog summary already carries."""
    return _integer(_capture(description, r"推荐等级：\s*(\d+)")), _capture(description, r"地区：\s*([^\s·]+)")


def _merge_energy_intensities(items: tuple[StageCatalogItem, ...]) -> tuple[StageCatalogItem, ...]:
    """`重度能量淤积点·X` is the same location at a higher intensity, so it is a variant of X."""
    by_name: dict[str, StageCatalogItem] = {}
    heavy: dict[str, list[StageCatalogItem]] = {}
    passthrough: list[StageCatalogItem] = []
    for item in items:
        if item.family_key != "energy_deposit":
            passthrough.append(item)
        elif item.title.startswith(ENERGY_HEAVY_PREFIX):
            heavy.setdefault(item.name, []).append(item)
        else:
            by_name[item.name] = item
    merged: list[StageCatalogItem] = []
    for name, item in by_name.items():
        siblings = heavy.pop(name, [])
        merged.append(
            replace(item, extra_titles=tuple(sibling.title for sibling in siblings))
            if siblings
            else item
        )
    # A heavy variant whose base article is missing still deserves its own entry.
    for siblings in heavy.values():
        merged.extend(siblings)
    return tuple((*passthrough, *merged))


def _expand_entries(data: dict[str, Any], item: StageCatalogItem) -> list[StageCatalogItem]:
    """Turn one multi-stage article into one catalog entry per stage it publishes."""
    attrs = _template_attrs(data)
    themes = _dicts(_dig(attrs, "stages", "themes"))
    if not themes:
        return []
    season = _localized(_dig(attrs, "overview", "name")) or item.name
    entries = []
    for theme in themes:
        name = _localized(theme.get("name"))
        group_id = _text(theme.get("groupId"))
        if not name or not group_id:
            continue
        levels = [
            _integer(one.get("recommendLv"))
            for one in _dicts(theme.get("difficulties"))
            if _integer(one.get("recommendLv")) is not None
        ]
        entries.append(
            replace(
                item,
                name=name,
                entry_key=group_id,
                description=season,
                region=season,
                recommended_level=max(levels) if levels else None,
            )
        )
    return entries


def _dedupe_entries(items: tuple[StageCatalogItem, ...]) -> tuple[StageCatalogItem, ...]:
    """Seasons reuse themes; keep the newest article for each so the catalog lists it once."""
    best: dict[tuple[str, str], StageCatalogItem] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        identity = (item.family_key, item.entry_key or item.title)
        current = best.get(identity)
        if current is None:
            order.append(identity)
            best[identity] = item
        elif item.updated_at > current.updated_at:
            best[identity] = item
    return tuple(best[identity] for identity in order)


def _family_key_of(item: StageCatalogItem | None) -> str:
    return item.family_key if item is not None else ""


def parse_fz_stage(
    data: dict[str, Any], *, entry_key: str = "", family_key: str = ""
) -> Stage:
    article = data.get("article") if isinstance(data.get("article"), dict) else {}
    revision = data.get("revision") if isinstance(data.get("revision"), dict) else {}
    title = _text(article.get("title"))
    if title.startswith("危境再现/"):
        return _parse_boss_stage(article, revision)
    if "能量淤积点·" in title:
        return _parse_energy_stage(article, revision)
    attrs = _template_attrs(data)
    template = _text(attrs.get("templateName"))
    if template == "协议空间":
        return _parse_resource_stage(article, revision, attrs)
    if template == "战争回响":
        return _parse_war_echo_stage(article, revision, attrs, entry_key)
    if template == "危机合约":
        return _parse_crisis_contract_stage(article, revision, attrs)
    if _activity_cards(revision):
        return _parse_activity_stage(article, revision, family_key)
    if attrs:
        # An unmodelled template still renders: the generic reader turns whatever the
        # source publishes into facts, variants and blocks the card already knows.
        return _parse_generic_stage(article, revision, attrs, family_key)
    raise StageDataIncomplete(f"暂不支持该关卡条目：{title or '未知条目'}")


def _parse_boss_stage(article: dict[str, Any], revision: dict[str, Any]) -> Stage:
    attrs = _first_node_attrs(revision.get("contentJson"), "wikiTemplateInstance")
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    depths_wrapper = attrs.get("depths") if isinstance(attrs.get("depths"), dict) else {}
    depths = [item for item in depths_wrapper.get("depths") or () if isinstance(item, dict)]
    if not depths:
        raise StageDataIncomplete("该危境关卡暂未提供深度资料。")
    categories = {_text(item) for item in article.get("categories") or ()}
    family_key = "crisis_fragment" if "危境碎片" in categories else "boss_rush"
    family_name = FAMILY_NAMES[family_key]
    title = _text(article.get("title"))
    name = _text(hero.get("bossName")) or title.split("/", 1)[-1]
    variants: list[StageVariant] = []
    for sort_order, depth in enumerate(depths, 1):
        raw_enemies = depth.get("enemies")
        enemies = None if raw_enemies is None else tuple(
            _boss_enemy(item) for item in raw_enemies if isinstance(item, dict)
        )
        flavor = _text(depth.get("flavor"))
        variants.append(
            StageVariant(
                id=_text(depth.get("dungeonId")) or f"{title}:{sort_order}",
                label=_text(depth.get("depthLabel")) or f"第{sort_order}级",
                sort_order=sort_order,
                recommended_level=_integer(depth.get("recommendLv")),
                mechanics=(flavor,) if flavor else None,
                enemies=enemies,
                rewards=None,
            )
        )
    extension = BossRushStageDetails(
        boss_name=name,
        series_id=_text(hero.get("seriesId")),
        series_name=_text(hero.get("seriesName")),
        depth_count=_integer(hero.get("depthCount")),
        icon_url=_text(hero.get("iconUrl")),
    )
    summary = _text(hero.get("intro")) or _text(article.get("description"))
    return Stage(
        id=_text(hero.get("seriesId")) or title,
        name=name,
        aliases=tuple(dict.fromkeys((title, f"{family_name}·{name}", _text(hero.get("seriesName"))))),
        family_key=family_key,
        family_name=family_name,
        summary=summary,
        location="",
        unlock_condition="",
        source=_source_ref(article, revision),
        variants=tuple(variants),
        extension=extension,
    )


def _boss_enemy(raw: dict[str, Any]) -> StageEnemy:
    name = _text(raw.get("name"))
    return StageEnemy(
        enemy_id=_text(raw.get("enemyId")) or _text(raw.get("templateId")),
        name=name,
        icon_url=_text(raw.get("iconUrl")),
        level=_integer(raw.get("level")),
        hp=_integer(raw.get("hp")),
        attack=_integer(raw.get("atk")),
        defense=_integer(raw.get("def")),
        # Embedded boss rows carry no link, but their article is reliably named after the enemy.
        article_title=_text(raw.get("title")) or (f"{ENEMY_ARTICLE_PREFIX}{name}" if name else ""),
        resistances=_resistances(raw.get("resistances")),
        poise=_poise_from_groups(raw.get("groups"), raw.get("poiseKnots")),
    )


def _poise_from_groups(groups: Any, knots: Any = None) -> StageEnemyPoise | None:
    """Read the `poise` attribute group; absent rows stay None so the card can say so."""
    rows: dict[str, Any] = {}
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and _text(group.get("key")) == "poise":
                for row in group.get("rows") or ():
                    if isinstance(row, dict):
                        rows[_text(row.get("attrType"))] = row.get("value")
                break
    poise = StageEnemyPoise(
        max_value=_number(rows.get("MaxPoise")),
        damage_scalar=_number(rows.get("BreakingAttackDamageTakenScalar")),
        recover_seconds=_number(rows.get("PoiseRecTime")),
        recover_scalar=_number(rows.get("PoiseRecTimeScalar")),
        knots=_knots(knots),
    )
    return None if poise.is_empty else poise


def _knots(raw: Any) -> tuple[float, ...] | None:
    if not isinstance(raw, list):
        return None
    values = [_number(item) for item in raw]
    return tuple(value for value in values if value is not None)


def _resistances(raw: Any) -> tuple[StageEnemyResistance, ...] | None:
    """None when the source omits the field; an empty tuple when it explicitly carries no rows."""
    if not isinstance(raw, list):
        return None
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("elementLabel"))
        element = _text(item.get("element"))
        if not label and not element:
            continue
        rows.append(
            StageEnemyResistance(
                element=element,
                label=label or element,
                percent=_number(item.get("percent")),
                scalar=_number(item.get("scalar")),
                color=_text(item.get("color")),
            )
        )
    return tuple(rows)


def parse_enemy_resistances(data: dict[str, Any]) -> tuple[StageEnemyResistance, ...] | None:
    """Read the resistance card off a standalone `敌人/<name>` article."""
    revision = data.get("revision") if isinstance(data.get("revision"), dict) else {}
    attrs = _first_node_attrs(revision.get("contentJson"), "endfieldCardEnemyResistances")
    return _resistances(attrs.get("rows"))


def parse_enemy_poise(data: dict[str, Any]) -> StageEnemyPoise | None:
    """Poise knots live only on the enemy article, never on the embedded stage rows."""
    revision = data.get("revision") if isinstance(data.get("revision"), dict) else {}
    attrs = _first_node_attrs(revision.get("contentJson"), "endfieldCardEnemyStats")
    return _poise_from_groups(attrs.get("groups"), attrs.get("poiseKnots"))


def _merge_poise(inline: StageEnemyPoise | None, fetched: StageEnemyPoise | None) -> StageEnemyPoise | None:
    """The stage row wins on values it already has; the article supplies whatever is still missing."""
    if inline is None:
        return fetched
    if fetched is None:
        return inline
    return StageEnemyPoise(
        max_value=inline.max_value if inline.max_value is not None else fetched.max_value,
        damage_scalar=inline.damage_scalar if inline.damage_scalar is not None else fetched.damage_scalar,
        recover_seconds=(
            inline.recover_seconds if inline.recover_seconds is not None else fetched.recover_seconds
        ),
        recover_scalar=(
            inline.recover_scalar if inline.recover_scalar is not None else fetched.recover_scalar
        ),
        knots=inline.knots if inline.knots is not None else fetched.knots,
    )


@dataclass(frozen=True, slots=True)
class _EnemyDetails:
    resistances: tuple[StageEnemyResistance, ...] | None
    poise: StageEnemyPoise | None


def _needs_enemy_article(enemy: StageEnemy) -> bool:
    return enemy.resistances is None or enemy.poise is None or enemy.poise.knots is None


def _variant_enemies(variant: StageVariant) -> tuple[StageEnemy, ...]:
    enemies = list(variant.enemies or ())
    extension = variant.extension
    if isinstance(extension, EnergyDepositStageDetails):
        enemies.extend(wave.enemy for wave in extension.waves or ())
    return tuple(enemies)


def _with_enemy_details(stage: Stage, lookup: dict[str, _EnemyDetails]) -> Stage:
    variants = tuple(_variant_with_details(variant, lookup) for variant in stage.variants)
    extension = stage.extension
    if isinstance(extension, EnergyDepositStageDetails):
        extension = _extension_with_details(extension, lookup)
    return replace(stage, variants=variants, extension=extension)


def _variant_with_details(variant: StageVariant, lookup: dict[str, _EnemyDetails]) -> StageVariant:
    enemies = (
        None
        if variant.enemies is None
        else tuple(_enemy_with_details(enemy, lookup) for enemy in variant.enemies)
    )
    extension = variant.extension
    if isinstance(extension, EnergyDepositStageDetails):
        extension = _extension_with_details(extension, lookup)
    return replace(variant, enemies=enemies, extension=extension)


def _extension_with_details(
    extension: EnergyDepositStageDetails, lookup: dict[str, _EnemyDetails]
) -> EnergyDepositStageDetails:
    if extension.waves is None:
        return extension
    waves = tuple(
        replace(wave, enemy=_enemy_with_details(wave.enemy, lookup)) for wave in extension.waves
    )
    return replace(extension, waves=waves)


def _enemy_with_details(enemy: StageEnemy, lookup: dict[str, _EnemyDetails]) -> StageEnemy:
    detail = lookup.get(enemy.article_title) if enemy.article_title else None
    if detail is None:
        return enemy
    resistances = enemy.resistances if enemy.resistances is not None else detail.resistances
    poise = _merge_poise(enemy.poise, detail.poise)
    if resistances is enemy.resistances and poise is enemy.poise:
        return enemy
    return replace(enemy, resistances=resistances, poise=poise)


def _parse_energy_stage(article: dict[str, Any], revision: dict[str, Any]) -> Stage:
    content = _content_nodes(revision.get("contentJson"))
    paragraphs = [_node_text(node) for node in content if node.get("type") == "paragraph"]
    meta_text = paragraphs[0] if paragraphs else ""
    summary = paragraphs[1] if len(paragraphs) > 1 else ""
    intensity = _capture(meta_text, r"强度：\s*([^·]+)")
    recommended_level = _integer(_capture(meta_text, r"推荐等级：\s*(\d+)"))
    location = _capture(meta_text, r"地区：\s*([^·]+)")
    tables = [
        node.get("attrs")
        for node in content
        if node.get("type") == "wikiIndexTable" and isinstance(node.get("attrs"), dict)
    ]
    matrix_entries = _table_entries(tables, {"name", "rarity", "skill"})
    weapon_entries = _table_entries(tables, {"name", "weaponType", "maxLv"})
    wave_entries = _table_entries(tables, {"name", "wave", "cond", "count", "level"})
    rewards = None if matrix_entries is None else tuple(
        StageReward(
            item_id=_text(item.get("id")) or _text(item.get("name")),
            name=_text(item.get("name")),
            rarity=_integer(item.get("rarity")),
        )
        for item in matrix_entries
    )
    waves = None if wave_entries is None else tuple(_energy_wave(item) for item in wave_entries)
    enemies = None if waves is None else tuple(
        dict.fromkeys(wave.enemy.enemy_id or wave.enemy.name for wave in waves)
    )
    enemy_by_key = {wave.enemy.enemy_id or wave.enemy.name: wave.enemy for wave in waves or ()}
    enemy_values = None if enemies is None else tuple(enemy_by_key[key] for key in enemies)
    extension = EnergyDepositStageDetails(
        region=location,
        intensity=intensity,
        matrices=None if matrix_entries is None else tuple(_text(item.get("name")) for item in matrix_entries),
        weapon_references=None if weapon_entries is None else tuple(_text(item.get("name")) for item in weapon_entries),
        waves=waves,
    )
    title = _text(article.get("title"))
    name = title.split("·", 1)[-1]
    variant = StageVariant(
        id=title,
        label=intensity or "普通",
        sort_order=2 if title.startswith(ENERGY_HEAVY_PREFIX) else 1,
        recommended_level=recommended_level,
        mechanics=(summary,) if summary else None,
        enemies=enemy_values,
        rewards=rewards,
        extension=extension,
        waves=waves,
    )
    return Stage(
        id=f"能量淤积点·{name}",
        name=name,
        aliases=_aliases(title, f"能量淤积点·{name}", f"能量淤积点 {name}"),
        family_key="energy_deposit",
        family_name=FAMILY_NAMES["energy_deposit"],
        summary=summary,
        location=location,
        unlock_condition="",
        source=_source_ref(article, revision),
        variants=(variant,),
        extension=extension,
    )


def _energy_wave(raw: dict[str, Any]) -> StageWave:
    enemy = StageEnemy(
        enemy_id=_text(raw.get("enemyId")),
        name=_text(raw.get("name")),
        level=_integer(raw.get("level")),
        count=_integer(raw.get("count")),
        article_title=_text(raw.get("title")),
        resistances=_resistances(raw.get("resistances")),
    )
    return StageWave(
        wave=_integer(raw.get("wave")),
        condition=_text(raw.get("cond")),
        time=_number(raw.get("time")),
        enemy=enemy,
    )


def _merge_sibling_variants(stage: Stage, payloads: list[dict[str, Any]]) -> Stage:
    """Fold sibling articles of the same stage in as extra variants, ordered by level."""
    variants = list(stage.variants)
    for payload in payloads:
        try:
            sibling = parse_fz_stage(payload)
        except StageDataIncomplete as exc:
            logger.warning(f"[endfield] stage sibling skipped reason={exc}")
            continue
        variants.extend(sibling.variants)
    ordered = sorted(
        variants,
        key=lambda variant: (variant.recommended_level or 0, variant.sort_order),
    )
    return replace(
        stage,
        variants=tuple(
            replace(variant, sort_order=index) for index, variant in enumerate(ordered, 1)
        ),
    )


# ------------------------------------------------------------- 资源副本（协议空间）


def _parse_resource_stage(
    article: dict[str, Any], revision: dict[str, Any], attrs: dict[str, Any]
) -> Stage:
    hero = _dict(attrs.get("hero"))
    tiers = _dicts(_dig(attrs, "tiers", "tiers"))
    if not tiers:
        raise StageDataIncomplete("该资源副本暂未提供层数资料。")
    feature_title = _text(_dig(attrs, "tiers", "featureTitle")) or _text(hero.get("featureTitle"))
    series_name = _text(hero.get("seriesName"))
    name = series_name.split("·", 1)[-1] if "·" in series_name else series_name
    variants = tuple(
        _resource_variant(tier, index, feature_title) for index, tier in enumerate(tiers, 1)
    )
    facts = _facts(
        ("材料类别", _text(hero.get("materialCat"))),
        ("产出页签", _text(hero.get("tabLabel"))),
        ("层数", f"{_integer(hero.get('tierCount')) or len(tiers)} 级"),
        ("理智消耗", _text(hero.get("staminaText"))),
    )
    return Stage(
        id=_text(hero.get("seriesId")) or _text(article.get("title")),
        name=name or _text(article.get("title")).rsplit("/", 1)[-1],
        aliases=_aliases(_text(article.get("title")), series_name, f"资源副本·{name}"),
        family_key="resource",
        family_name=FAMILY_NAMES["resource"],
        summary=_text(hero.get("intro")) or _text(article.get("description")),
        location="",
        unlock_condition="",
        source=_source_ref(article, revision),
        variants=variants,
        icon_url=_text(hero.get("iconUrl")),
        template_name="协议空间",
        facts=facts,
    )


def _resource_variant(tier: dict[str, Any], index: int, feature_title: str) -> StageVariant:
    mechanics = _lines(_text(tier.get("flavor")))
    feature = _text(tier.get("feature"))
    blocks = []
    unlocks = tuple(
        StageBlockEntry(name=_text(one.get("text")))
        for one in _dicts(tier.get("unlocks"))
        if _text(one.get("text"))
    )
    if unlocks:
        blocks.append(StageBlock(key="unlocks", title="解锁条件", entries=unlocks))
    buffs = _buff_entries(tier.get("buffs"))
    if buffs:
        blocks.append(StageBlock(key="buffs", title="关卡增益", entries=buffs))
    if feature:
        blocks.append(
            StageBlock(
                key="feature",
                title=feature_title or "机制特性",
                entries=tuple(StageBlockEntry(name=line) for line in _lines(feature)),
            )
        )
    return StageVariant(
        id=_text(tier.get("dungeonId")) or f"tier-{index}",
        label=_text(tier.get("tierLabel")) or f"第{index}级",
        sort_order=_integer(tier.get("tierIndex")) or index,
        recommended_level=_integer(tier.get("recommendLv")),
        stamina_cost=_integer(tier.get("costStamina")),
        mechanics=mechanics,
        enemies=_enemy_rows(tier.get("enemies")),
        rewards=_flat_rewards(tier.get("rewards")),
        reward_sets=_reward_sets(tier.get("rewards")),
        facts=_facts(("关卡名", _text(tier.get("name")))),
        blocks=tuple(blocks),
    )


# ----------------------------------------------------------------------- 战争回响


def _parse_war_echo_stage(
    article: dict[str, Any], revision: dict[str, Any], attrs: dict[str, Any], entry_key: str
) -> Stage:
    overview = _dict(attrs.get("overview"))
    themes = _dicts(_dig(attrs, "stages", "themes"))
    theme = next(
        (one for one in themes if _text(one.get("groupId")) == entry_key),
        themes[0] if themes and not entry_key else None,
    )
    if theme is None:
        raise StageDataIncomplete("该战争回响赛季暂未提供该关卡资料。")
    difficulties = _dicts(theme.get("difficulties"))
    if not difficulties:
        raise StageDataIncomplete("该战争回响关卡暂未提供难度资料。")
    season = _localized(overview.get("name"))
    name = _localized(theme.get("name"))
    variants = tuple(
        _war_echo_variant(one, index) for index, one in enumerate(difficulties, 1)
    )
    cycles = _war_echo_cycles(attrs)
    week_count = _integer(overview.get("weekCount")) or len(cycles) or None
    rank_stars = [
        _integer(rank.get("stars"))
        for rank in _dicts(overview.get("ranks"))
        if _integer(rank.get("stars")) is not None
    ]
    max_stars = _integer(overview.get("maxStars")) or (max(rank_stars) if rank_stars else None)
    facts = _facts(
        ("赛季", season),
        ("轮换周期", f"{week_count} 期" if week_count is not None else ""),
        ("满星", f"{max_stars} 星" if max_stars is not None else ""),
        ("参与条件", "；".join(_strings(overview.get("conditions")))),
    )
    blocks = []
    rotation = _rotation_block(attrs, _text(theme.get("groupId")))
    if rotation is not None:
        blocks.append(rotation)
    rules = _strings(overview.get("commonRules"))
    if rules:
        blocks.append(
            StageBlock(
                key="rules",
                title="通用规则",
                entries=tuple(StageBlockEntry(name=_plain_markup(line)) for line in rules),
            )
        )
    feature_lines = _strings(theme.get("featureLines"))
    if feature_lines:
        blocks.append(
            StageBlock(
                key="feature",
                title="关卡特性",
                entries=tuple(StageBlockEntry(name=_plain_markup(line)) for line in feature_lines),
            )
        )
    return Stage(
        id=_text(theme.get("groupId")),
        name=name,
        aliases=_aliases(name, f"{season}·{name}", f"战争回响·{name}"),
        family_key="war_echo",
        family_name=FAMILY_NAMES["war_echo"],
        summary=_text(overview.get("desc")),
        location="",
        unlock_condition="；".join(_strings(overview.get("conditions"))),
        source=_source_ref(article, revision),
        variants=variants,
        icon_url=_text(theme.get("iconUrl")),
        template_name="战争回响",
        facts=facts,
        blocks=tuple(blocks),
        extension=WarEchoStageDetails(
            season_name=season,
            stage_group_id=_text(theme.get("groupId")),
            week_count=week_count,
            max_stars=max_stars,
            cycles=cycles,
        ),
    )


def _war_echo_variant(difficulty: dict[str, Any], index: int) -> StageVariant:
    star = _integer(difficulty.get("star"))
    label = _text(difficulty.get("label")) or f"难度{index}"
    mechanics = _lines(_text(difficulty.get("flavor")))
    blocks = []
    extra = _strings(difficulty.get("extraFeatureLines"))
    buff = _plain_markup(_text(difficulty.get("specialBuff")))
    lines = [*(_plain_markup(line) for line in extra), *( [buff] if buff else [] )]
    if lines:
        blocks.append(
            StageBlock(
                key="feature",
                title="本难度特性",
                entries=tuple(StageBlockEntry(name=line) for line in lines),
            )
        )
    return StageVariant(
        id=f"{_text(difficulty.get('gameId'))}-{star or index}",
        label=label,
        sort_order=star or index,
        recommended_level=_integer(difficulty.get("recommendLv")),
        mechanics=mechanics,
        enemies=_enemy_rows(difficulty.get("enemies")),
        rewards=_reward_rows(difficulty.get("rewards")),
        waves=_named_waves(difficulty.get("waves"), difficulty.get("enemies")),
        facts=_facts(
            ("星级", f"{star} 星" if star is not None else ""),
            ("解锁条件", _text(difficulty.get("unlockDesc"))),
        ),
        blocks=tuple(blocks),
    )


def _rotation_block(attrs: dict[str, Any], group_id: str) -> StageBlock | None:
    cycles = _dicts(_dig(attrs, "cycles", "cycles"))
    if not cycles:
        return None
    entries = []
    for cycle in cycles:
        themes = _dicts(cycle.get("themes"))
        if not any(_text(one.get("groupId")) == group_id for one in themes):
            continue
        week = _integer(cycle.get("week"))
        entries.append(
            StageBlockEntry(
                name=_localized(cycle.get("name")),
                meta=f"第 {week} 期" if week is not None else "",
                desc="、".join(_localized(one.get("name")) for one in themes),
            )
        )
    if not entries:
        return None
    return StageBlock(key="cycles", title="轮换周期", note="本关卡开放的周期", entries=tuple(entries))


def _war_echo_cycles(attrs: dict[str, Any]) -> tuple[WarEchoCycle, ...]:
    return tuple(
        WarEchoCycle(
            week=_integer(cycle.get("week")),
            name=_localized(cycle.get("name")),
            stage_group_ids=tuple(
                _text(theme.get("groupId"))
                for theme in _dicts(cycle.get("themes"))
                if _text(theme.get("groupId"))
            ),
        )
        for cycle in _dicts(_dig(attrs, "cycles", "cycles"))
    )


# ----------------------------------------------------------------------- 危机合约


def _parse_crisis_contract_stage(
    article: dict[str, Any], revision: dict[str, Any], attrs: dict[str, Any]
) -> Stage:
    overview = _dict(attrs.get("overview"))
    board = _dict(attrs.get("board"))
    dungeon = _dict(board.get("dungeon"))
    title = _text(overview.get("title")) or _text(article.get("title"))
    configs = _dicts(dungeon.get("configs"))
    variants = tuple(
        _crisis_variant(config, index, dungeon) for index, config in enumerate(configs, 1)
    ) or (
        StageVariant(
            id=_text(dungeon.get("dungeonId")) or "standard",
            label="标准",
            sort_order=1,
            recommended_level=_integer(dungeon.get("recommendLv")),
            mechanics=_lines(_text(dungeon.get("desc"))),
        ),
    )
    blocks = [
        block
        for block in (
            _contract_block(board),
            _level_block(attrs.get("levels") or board),
            _task_block(attrs.get("tasks")),
            _shop_block(attrs.get("shop")),
        )
        if block is not None
    ]
    return Stage(
        id=_text(board.get("activityId")) or _text(article.get("title")),
        name=title,
        aliases=_aliases(title, _text(article.get("title")), "危机合约"),
        family_key="crisis_contract",
        family_name=FAMILY_NAMES["crisis_contract"],
        summary=_plain_markup(_text(overview.get("intro"))),
        location="",
        unlock_condition="",
        source=_source_ref(article, revision),
        variants=variants,
        template_name="危机合约",
        facts=_facts(
            ("指标数量", f"{len(_dicts(board.get('contracts')))} 项" if board.get("contracts") else ""),
        ),
        blocks=tuple(blocks),
        extension=_crisis_contract_details(attrs),
    )


def _crisis_variant(config: dict[str, Any], index: int, dungeon: dict[str, Any]) -> StageVariant:
    waves = _dicts(config.get("waves"))
    enemies = _config_enemies(waves, _dicts(config.get("enemies")))
    return StageVariant(
        id=_text(config.get("configId")) or f"{_text(dungeon.get('dungeonId')) or 'config'}-{index}",
        label=_text(config.get("label")) or f"配置{index}",
        sort_order=index,
        recommended_level=_integer(dungeon.get("recommendLv")),
        mechanics=_lines(_text(dungeon.get("desc"))),
        enemies=enemies,
        waves=_config_waves(waves, enemies),
        facts=_facts(("波次", f"{len(waves)} 波" if waves else "")),
        blocks=(
            (
                StageBlock(
                    key="feature",
                    title="机制特性",
                    entries=tuple(
                        StageBlockEntry(name=line)
                        for line in _lines(_text(dungeon.get("featureDesc")))
                    ),
                ),
            )
            if _text(dungeon.get("featureDesc"))
            else ()
        ),
    )


def _config_waves(
    waves: list[dict[str, Any]], enemies: tuple[StageEnemy, ...] | None
) -> tuple[StageWave, ...] | None:
    if not waves:
        return None
    lookup = {(enemy.enemy_id or enemy.name): enemy for enemy in enemies or ()}
    rows: list[StageWave] = []
    for index, wave in enumerate(waves, 1):
        number = _integer(wave.get("waveIdx")) or index
        for raw_enemy, count in _counted(_dicts(wave.get("enemies"))):
            parsed = _enemy_row(raw_enemy)
            enemy = lookup.get(parsed.enemy_id or parsed.name, parsed)
            rows.append(
                StageWave(
                    wave=number,
                    condition="",
                    time=None,
                    enemy=replace(enemy, count=count),
                )
            )
    return tuple(rows)


def _config_enemies(
    waves: list[dict[str, Any]], roster: list[dict[str, Any]]
) -> tuple[StageEnemy, ...] | None:
    if not waves and not roster:
        return None
    totals: dict[str, int] = {}
    for wave in waves:
        for raw_enemy, count in _counted(_dicts(wave.get("enemies"))):
            row = _enemy_row(raw_enemy)
            key = row.enemy_id or row.name
            totals[key] = totals.get(key, 0) + count
    source_rows = roster or [raw for wave in waves for raw in _dicts(wave.get("enemies"))]
    merged: dict[str, StageEnemy] = {}
    for raw_enemy in source_rows:
        row = _enemy_row(raw_enemy)
        key = row.enemy_id or row.name
        merged.setdefault(key, replace(row, count=totals.get(key, row.count)))
    return tuple(merged.values())


def _counted(rows: list[dict[str, Any]]) -> tuple[tuple[dict[str, Any], int], ...]:
    """The board lists one row per spawn, so identical rows collapse into a count."""
    totals: dict[str, tuple[dict[str, Any], int]] = {}
    for row in rows:
        key = _text(row.get("enemyId")) or _text(row.get("name"))
        entry, count = totals.get(key, (row, 0))
        totals[key] = (entry, count + (_integer(row.get("count")) or 1))
    return tuple(totals.values())


def _crisis_contract_details(attrs: dict[str, Any]) -> CrisisContractStageDetails:
    board = _dict(attrs.get("board"))
    dungeon = _dict(board.get("dungeon"))
    metrics = tuple(
        CrisisContractMetric(
            metric_id=_text(metric.get("tagId")),
            name=_localized(metric.get("name")),
            score=_integer(metric.get("score")),
            level=_integer(metric.get("level")),
            group_id=_text(metric.get("groupId")),
            lock_ids=tuple(_text(value) for value in metric.get("lockIds") or () if _text(value)),
            conflict_id=_text(metric.get("conflictId")),
        )
        for metric in _dicts(board.get("contracts"))
    )
    level_scores = tuple(
        score
        for level in _dicts(_dict(attrs.get("levels") or board).get("levels"))
        if (score := _integer(level.get("score"))) is not None
    )
    task_groups = tuple(
        CrisisContractTaskGroup(
            group_id=_text(group.get("groupId")),
            name=_localized(group.get("name")),
            task_ids=tuple(
                _text(task.get("taskId"))
                for task in _dicts(group.get("tasks"))
                if _text(task.get("taskId"))
            ),
        )
        for group in _dicts(_dict(attrs.get("tasks")).get("groups"))
    )
    shops = tuple(
        CrisisContractShop(
            shop_id=_text(shop.get("shopId")),
            name=_localized(shop.get("name")),
            currency_name=_localized(
                shop.get("currencyName") or _dict(attrs.get("shop")).get("currencyName")
            ),
            goods_count=len(_dicts(shop.get("goods"))),
        )
        for shop in _dicts(_dict(attrs.get("shop")).get("shops"))
    )
    return CrisisContractStageDetails(
        activity_id=_text(board.get("activityId")),
        dungeon_id=_text(dungeon.get("dungeonId")),
        metrics=metrics,
        level_scores=level_scores,
        task_groups=task_groups,
        shops=shops,
    )


def _contract_block(board: dict[str, Any]) -> StageBlock | None:
    contracts = _dicts(board.get("contracts"))
    if not contracts:
        return None
    entries = tuple(
        StageBlockEntry(
            name=_localized(one.get("name")),
            desc=_plain_markup(_localized(one.get("desc"))),
            meta=f"{_integer(one.get('score'))} 分" if one.get("score") is not None else "",
            badges=tuple(
                badge
                for badge in (
                    f"Lv.{_integer(one.get('level'))}" if one.get("level") else "",
                    "锁闭" if _strings(one.get("lockIds")) else "",
                )
                if badge
            ),
        )
        for one in contracts
    )
    regions = "、".join(
        f"{_text(one.get('label'))}（{_integer(one.get('score'))} 分开放）"
        for one in _dicts(board.get("unlockRegions"))
        if _text(one.get("label"))
    )
    return StageBlock(key="contracts", title="指标", note=regions, entries=entries)


def _level_block(levels: Any) -> StageBlock | None:
    rows = _dicts(_dict(levels).get("levels"))
    if not rows:
        return None
    entries = tuple(
        StageBlockEntry(
            name=f"等级 {_integer(one.get('level'))}",
            meta=f"合约分 {_integer(one.get('score'))}" if one.get("score") is not None else "",
            rewards=_reward_rows(one.get("rewards")) or (),
        )
        for one in rows
    )
    return StageBlock(
        key="levels",
        title=_text(_dict(levels).get("title")) or "等级奖励",
        note=_text(_dict(levels).get("scoreNote")),
        entries=entries,
    )


def _task_block(tasks: Any) -> StageBlock | None:
    groups = _dicts(_dict(tasks).get("groups"))
    if not groups:
        return None
    entries: list[StageBlockEntry] = []
    for group in groups:
        label = _localized(group.get("name"))
        for one in _dicts(group.get("tasks")):
            entries.append(
                StageBlockEntry(
                    name=label,
                    desc=_plain_markup(_localized(one.get("desc"))),
                    rewards=_reward_rows(one.get("rewards")) or (),
                )
            )
    if not entries:
        return None
    return StageBlock(
        key="tasks",
        title=_text(_dict(tasks).get("title")) or "作战任务",
        note=_text(_dict(tasks).get("note")),
        entries=tuple(entries),
    )


def _shop_block(shop: Any) -> StageBlock | None:
    shops = _dicts(_dict(shop).get("shops"))
    if not shops:
        return None
    entries: list[StageBlockEntry] = []
    for one in shops:
        label = _localized(one.get("name"))
        for goods in _dicts(one.get("goods")):
            items = _reward_rows(goods.get("items")) or ()
            price = _integer(goods.get("actualPrice"))
            entries.append(
                StageBlockEntry(
                    name=items[0].name if items else label,
                    meta=f"{price} {_localized(goods.get('currencyName'))}" if price is not None else "",
                    badges=tuple(
                        badge for badge in (label, _text(goods.get("limitLabel"))) if badge
                    ),
                    rewards=items,
                )
            )
    if not entries:
        return None
    return StageBlock(
        key="shop",
        title=_text(_dict(shop).get("title")) or "兑换商店",
        note=_text(_dict(shop).get("note")),
        entries=tuple(entries),
    )


# ------------------------------------------------------------------- 挑战活动卡片


def _activity_cards(revision: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The activity pages use one node per section instead of a single template."""
    cards: dict[str, dict[str, Any]] = {}
    for node in _content_nodes(revision.get("contentJson")):
        node_type = _text(node.get("type"))
        if node_type.startswith("endfieldCardActivity") and isinstance(node.get("attrs"), dict):
            cards.setdefault(node_type, node["attrs"])
    return cards


def _parse_activity_stage(
    article: dict[str, Any], revision: dict[str, Any], family_key: str
) -> Stage:
    cards = _activity_cards(revision)
    overview = cards.get("endfieldCardActivityOverview", {})
    name = _localized(overview.get("name")) or _text(article.get("title")).rsplit("/", 1)[-1]
    stages = _dicts(_dig(cards.get("endfieldCardActivityStages", {}), "stages"))
    phases = _dicts(_dig(cards.get("endfieldCardActivityPhases", {}), "phases"))
    rows = stages or phases
    if not rows:
        raise StageDataIncomplete(f"“{name}”暂无可查询的关卡阶段资料。")
    entries = tuple(
        StageBlockEntry(
            name=_localized(one.get("name")),
            desc=_localized(one.get("desc")),
            meta=_text(one.get("condition")),
            rewards=_reward_rows(one.get("rewards")) or (),
        )
        for one in rows
    )
    section_title = (
        _text(_dig(cards.get("endfieldCardActivityStages", {}), "title"))
        or _text(_dig(cards.get("endfieldCardActivityPhases", {}), "title"))
        or "关卡 / 阶段"
    )
    variant = StageVariant(
        id=_text(overview.get("activityId")) or _text(article.get("title")),
        label="全部阶段",
        sort_order=1,
        mechanics=_lines(_plain_markup(_text(_dig(cards.get("endfieldCardActivityRules", {}), "text")))),
        blocks=(StageBlock(key="stages", title=section_title, entries=entries),),
    )
    rewards = _reward_rows(_dig(cards.get("endfieldCardActivityRewards", {}), "rewards"))
    if rewards:
        variant = replace(variant, rewards=rewards)
    key = family_key or "challenge_activity"
    return Stage(
        id=_text(overview.get("activityId")) or _text(article.get("title")),
        name=name,
        aliases=_aliases(name, _text(article.get("title"))),
        family_key=key,
        family_name=FAMILY_NAMES.get(key, FAMILY_NAMES["challenge_activity"]),
        summary=_text(overview.get("desc")),
        location="",
        unlock_condition="；".join(_strings(overview.get("conditions"))),
        source=_source_ref(article, revision),
        variants=(variant,),
        icon_url=_text(overview.get("tabImgUrl")),
        template_name="挑战活动",
        facts=_facts(
            ("活动类型", "、".join(_strings(overview.get("tags")))),
            ("开放时间", _time_range(overview.get("timeRanges"))),
            ("参与条件", "；".join(_strings(overview.get("conditions")))),
            ("阶段数", f"{len(rows)} 项"),
        ),
    )


def _time_range(raw: Any) -> str:
    ranges = _dicts(raw)
    if not ranges:
        return ""
    first = ranges[0]
    start, end = _short_time(first.get("open")), _short_time(first.get("close"))
    return f"{start} – {end}" if start and end else start or end


def _short_time(value: Any) -> str:
    return _text(value).split(" ", 1)[0]


# ------------------------------------------------------------------- 通用模板兜底


VARIANT_LIST_KEYS = ("tiers", "depths", "difficulties", "stages", "levels", "configs")
VARIANT_LABEL_KEYS = ("tierLabel", "depthLabel", "label", "name", "title")
VARIANT_ORDER_KEYS = ("tierIndex", "sortId", "star", "difficulty", "level", "depth")
GENERIC_FACT_LABELS = {
    "recommendLv": "推荐等级",
    "costStamina": "理智消耗",
    "seasonId": "赛季",
    "weekCount": "周期数",
    "maxStars": "满星",
    "tierCount": "层数",
    "materialCat": "材料类别",
    "staminaText": "理智消耗",
    "sceneName": "场景",
    "unlockDesc": "解锁条件",
}
GENERIC_TEXT_KEYS = ("intro", "desc", "description", "flavor", "summary")


def _parse_generic_stage(
    article: dict[str, Any], revision: dict[str, Any], attrs: dict[str, Any], family_key: str
) -> Stage:
    """Read an unmodelled FZ template by shape, so a new gameplay needs no new card."""
    title = _text(article.get("title"))
    header = _dict(attrs.get("hero")) or _dict(attrs.get("overview"))
    name = (
        _localized(header.get("name"))
        or _text(header.get("seriesName"))
        or _text(header.get("title"))
        or title.rsplit("/", 1)[-1]
    )
    variants = _generic_variants(attrs)
    if not variants:
        raise StageDataIncomplete(f"“{name}”的资料结构暂未被识别，无法生成关卡卡。")
    blocks = tuple(
        block
        for block in (_generic_block(key, value) for key, value in attrs.items())
        if block is not None
    )
    key = family_key or GENERIC_FAMILY_KEY
    return Stage(
        id=_text(header.get("seriesId")) or _text(header.get("activityId")) or title,
        name=name,
        aliases=_aliases(name, title),
        family_key=key,
        family_name=FAMILY_NAMES.get(key, FAMILY_NAMES[GENERIC_FAMILY_KEY]),
        summary=_first_text(header),
        location="",
        unlock_condition="；".join(_strings(header.get("conditions"))),
        source=_source_ref(article, revision),
        variants=variants,
        icon_url=_text(header.get("iconUrl")) or _text(header.get("tabImgUrl")),
        template_name=_text(attrs.get("templateName")),
        facts=_generic_facts(header),
        blocks=blocks,
    )


def _generic_variants(attrs: dict[str, Any]) -> tuple[StageVariant, ...]:
    rows = _find_variant_rows(attrs)
    if not rows:
        return ()
    return tuple(_generic_variant(row, index) for index, row in enumerate(rows, 1))


def _find_variant_rows(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer a list named like a difficulty ladder; otherwise take the first list shaped like one."""
    candidates: list[list[dict[str, Any]]] = []
    for node in _walk_nodes(attrs):
        for key, value in node.items():
            rows = _dicts(value)
            if not rows or not _looks_like_variants(rows):
                continue
            if key in VARIANT_LIST_KEYS:
                return rows
            candidates.append(rows)
    return candidates[0] if candidates else []


def _looks_like_variants(rows: list[dict[str, Any]]) -> bool:
    labelled = any(any(key in row for key in VARIANT_LABEL_KEYS) for row in rows)
    ordered = any(any(key in row for key in VARIANT_ORDER_KEYS) for row in rows)
    detailed = any("enemies" in row or "rewards" in row or "waves" in row for row in rows)
    return labelled and (ordered or detailed)


def _generic_variant(row: dict[str, Any], index: int) -> StageVariant:
    label = next(
        (_localized(row[key]) for key in VARIANT_LABEL_KEYS if _localized(row.get(key))),
        f"变体{index}",
    )
    order = next(
        (_integer(row[key]) for key in VARIANT_ORDER_KEYS if _integer(row.get(key)) is not None),
        index,
    )
    blocks = tuple(
        block
        for block in (_generic_block(key, value) for key, value in row.items())
        if block is not None
    )
    return StageVariant(
        id=_text(row.get("dungeonId")) or _text(row.get("gameId")) or f"variant-{index}",
        label=label,
        sort_order=order or index,
        recommended_level=_integer(row.get("recommendLv")),
        stamina_cost=_integer(row.get("costStamina")),
        mechanics=_lines(_first_text(row)),
        enemies=_enemy_rows(row.get("enemies")),
        rewards=_flat_rewards(row.get("rewards")) or _reward_rows(row.get("rewards")),
        reward_sets=_reward_sets(row.get("rewards")),
        waves=_named_waves(row.get("waves"), row.get("enemies")),
        facts=_generic_facts(row),
        blocks=blocks,
    )


def _generic_block(key: str, value: Any) -> StageBlock | None:
    """Any remaining list of named things becomes a titled block instead of being dropped."""
    if key in {"enemies", "rewards", "waves", "difficulties", "tiers", "depths", "themes"}:
        return None
    rows = _dicts(value)
    if not rows or _looks_like_variants(rows):
        return None
    entries = tuple(
        entry
        for entry in (
            StageBlockEntry(
                name=_localized(row.get("name")) or _localized(row.get("label")) or _text(row.get("text")),
                desc=_plain_markup(_first_text(row)),
                rewards=_reward_rows(row.get("rewards")) or (),
            )
            for row in rows
        )
        if entry.name or entry.desc
    )
    if not entries:
        return None
    return StageBlock(key=key, title=GENERIC_FACT_LABELS.get(key, key), entries=entries)


def _generic_facts(source: dict[str, Any]) -> tuple[StageFact, ...]:
    return _facts(
        *(
            (label, _scalar_text(source[key]))
            for key, label in GENERIC_FACT_LABELS.items()
            if key in source
        )
    )


def _first_text(source: dict[str, Any]) -> str:
    for key in GENERIC_TEXT_KEYS:
        text = _plain_markup(_localized(source.get(key)))
        if text:
            return text
    return ""


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    if isinstance(value, dict):
        return _localized(value)
    if isinstance(value, list):
        return "、".join(_strings(value))
    return _text(value)


# ------------------------------------------------------------------ 共用解析原语


def _enemy_rows(raw: Any) -> tuple[StageEnemy, ...] | None:
    if not isinstance(raw, list):
        return None
    return tuple(_enemy_row(row) for row in raw if isinstance(row, dict))


def _enemy_row(raw: dict[str, Any]) -> StageEnemy:
    name = _localized(raw.get("name"))
    target = _text(raw.get("target")) or _text(raw.get("title"))
    base_attrs = _dict(raw.get("baseAttrs"))
    return StageEnemy(
        enemy_id=_text(raw.get("enemyId")) or _text(raw.get("templateId")),
        name=name,
        icon_url=_text(raw.get("iconUrl")),
        level=_integer(raw.get("level")),
        count=_integer(raw.get("count")),
        hp=_integer(raw.get("hp")) or _integer(base_attrs.get("MaxHp")),
        attack=_integer(raw.get("atk")) or _integer(base_attrs.get("Atk")),
        defense=_integer(raw.get("def")) or _integer(base_attrs.get("Def")),
        article_title=target or (f"{ENEMY_ARTICLE_PREFIX}{name}" if name else ""),
        resistances=_resistances(raw.get("resistances")),
        poise=_poise_from_groups(raw.get("groups"), raw.get("poiseKnots")),
    )


def _named_waves(raw: Any, enemies: Any) -> tuple[StageWave, ...] | None:
    """Wave rows name their enemies; join them back to the full rows for levels and icons."""
    waves = _dicts(raw)
    if not waves:
        return None
    lookup = {
        _localized(row.get("name")): row for row in _dicts(enemies) if _localized(row.get("name"))
    }
    rows: list[StageWave] = []
    for index, wave in enumerate(waves, 1):
        number = _integer(wave.get("wave")) or index
        for entry in _dicts(wave.get("entries")):
            name = _localized(entry.get("name"))
            enemy = _enemy_row(lookup.get(name, {"name": name}))
            rows.append(
                StageWave(
                    wave=number,
                    condition=_text(wave.get("cond")),
                    time=_number(wave.get("time")),
                    enemy=replace(enemy, count=_integer(entry.get("count")) or enemy.count),
                )
            )
    return tuple(rows) if rows else None


def _reward_rows(raw: Any) -> tuple[StageReward, ...] | None:
    if not isinstance(raw, list):
        return None
    return tuple(_reward_row(row) for row in raw if isinstance(row, dict))


def _reward_row(raw: dict[str, Any]) -> StageReward:
    count = _integer(raw.get("count"))
    return StageReward(
        item_id=_text(raw.get("itemId")) or _localized(raw.get("name")),
        name=_localized(raw.get("name")),
        icon_url=_text(raw.get("iconUrl")),
        quantity_text=f"×{count:,}" if count is not None and count > 1 else "",
        rarity=_integer(raw.get("rarity")),
    )


def _reward_sets(raw: Any) -> StageRewards | None:
    """FZ wraps rewards in groups; `selectCount` marks the ones the player picks between."""
    block = _dict(raw)
    groups = _dicts(block.get("groups"))
    if not groups:
        return None
    rows = tuple(
        StageRewardGroup(label=_text(group.get("label")), items=_reward_rows(group.get("items")) or ())
        for group in groups
    )
    rows = tuple(group for group in rows if group.items)
    if not rows:
        return None
    return StageRewards(
        groups=rows,
        title=_text(block.get("title")),
        select_count=_integer(block.get("selectCount")) or 0,
    )


def _flat_rewards(raw: Any) -> tuple[StageReward, ...] | None:
    """The flat list the stat strip counts; grouped sources flatten, plain lists pass through."""
    sets = _reward_sets(raw)
    if sets is not None:
        return sets.items
    return _reward_rows(raw)


def _buff_entries(raw: Any) -> tuple[StageBlockEntry, ...]:
    entries = []
    for buff in _dicts(raw):
        effects = [
            f"{_text(effect.get('label'))} {_text(effect.get('text'))}".strip()
            for effect in _dicts(buff.get("effects"))
        ]
        interval = _number(buff.get("triggerIntervalSec"))
        entries.append(
            StageBlockEntry(
                name="、".join(effect for effect in effects if effect) or _text(buff.get("buffId")),
                meta=f"每 {interval:g}s" if interval else "",
            )
        )
    return tuple(entry for entry in entries if entry.name)


def _facts(*pairs: tuple[str, str]) -> tuple[StageFact, ...]:
    return tuple(StageFact(label=label, value=value) for label, value in pairs if value)


def _aliases(*values: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _lines(value: str) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    rows = tuple(line.strip() for line in value.splitlines() if line.strip())
    return rows or None


def _strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_localized(item) for item in raw if _localized(item))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dig(source: Any, *keys: str) -> Any:
    current: Any = source
    for key in keys:
        current = _dict(current).get(key)
    return current


def _localized(value: Any) -> str:
    """FZ ships `{zh, en}` pairs in some slots and a bare string in others."""
    return localized_text(value)


_MARKUP_TAG = re.compile(r"<[^<>]{0,200}?>")


def _plain_markup(value: str) -> str:
    """Strip FZ's inline colour/image markup; the card has its own visual language."""
    text = _MARKUP_TAG.sub("", str(value or ""))
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _template_attrs(data: dict[str, Any]) -> dict[str, Any]:
    revision = data.get("revision") if isinstance(data.get("revision"), dict) else {}
    return _first_node_attrs(revision.get("contentJson"), "wikiTemplateInstance")


def _table_entries(
    tables: Iterable[dict[str, Any]], required_keys: set[str]
) -> list[dict[str, Any]] | None:
    for attrs in tables:
        columns = attrs.get("columns") or ()
        keys = {_text(column.get("key")) for column in columns if isinstance(column, dict)}
        if required_keys.issubset(keys):
            return [entry for entry in attrs.get("entries") or () if isinstance(entry, dict)]
    return None


def _first_node_attrs(content_json: Any, node_type: str) -> dict[str, Any]:
    for node in _walk_nodes(content_json):
        if node.get("type") == node_type and isinstance(node.get("attrs"), dict):
            return node["attrs"]
    return {}


def _content_nodes(content_json: Any) -> list[dict[str, Any]]:
    if not isinstance(content_json, dict):
        return []
    return [item for item in content_json.get("content") or () if isinstance(item, dict)]


def _walk_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item)


def _node_text(node: dict[str, Any]) -> str:
    return "".join(
        _text(item.get("text"))
        for item in node.get("content") or ()
        if isinstance(item, dict)
    ).strip()


def _capture(value: str, pattern: str) -> str:
    match = re.search(pattern, value)
    return match.group(1).strip() if match else ""


def _source_ref(article: dict[str, Any], revision: dict[str, Any]) -> StageSourceRef:
    return StageSourceRef(
        source="FZ Wiki",
        article_title=_text(article.get("title")),
        revision=_text(revision.get("id")) or _text(article.get("currentRevisionId")),
        updated_at=_text(article.get("updatedAt")) or _text(revision.get("createdAt")),
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
