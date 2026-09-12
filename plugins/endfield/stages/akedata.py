from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

from loguru import logger

from ..account.i18n import localized_text
from ..providers.warfarin import WarfarinClient
from .models import (
    BossRushStageDetails,
    MonumentStageDetails,
    Stage,
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
)
from .fz import StageDataIncomplete


MONUMENT_FAMILY_KEY = "monument"
MONUMENT_FAMILY_NAME = "影拓丰碑"
HIGH_DIFFICULTY_CATEGORY = "dungeon_highdifficulty"
SERIES_STAGE_KEY_PREFIX = "series:"
_SERIES_WIDE_CATEGORIES = frozenset({"dungeon_bossrush", "dungeon_resource"})
_CATEGORY_FAMILIES: dict[str, tuple[str, str]] = {
    "dungeon_bossrush": ("boss_rush", "危境再现"),
    HIGH_DIFFICULTY_CATEGORY: (MONUMENT_FAMILY_KEY, MONUMENT_FAMILY_NAME),
    "dungeon_resource": ("resource", "资源副本"),
    "dungeon_contract": ("crisis_contract", "危机合约"),
    "dungeon_seasontower": ("war_echo", "战争回响"),
    "dungeon_actmonster": ("challenge_activity", "挑战活动"),
}
_FAMILY_ORDER = (
    "boss_rush",
    "crisis_fragment",
    MONUMENT_FAMILY_KEY,
    "resource",
    "crisis_contract",
    "war_echo",
    "challenge_activity",
    "other",
)
_FAMILY_NAMES = {
    "boss_rush": "危境再现",
    "crisis_fragment": "危境碎片",
    MONUMENT_FAMILY_KEY: MONUMENT_FAMILY_NAME,
    "resource": "资源副本",
    "crisis_contract": "危机合约",
    "war_echo": "战争回响",
    "challenge_activity": "挑战活动",
    "other": "其他玩法",
}
AKEDATA_ASSET_BASE_URL = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)
_RICH_TAG_RE = re.compile(r"<@[^>]+>|</>")
_DIFFICULTY_SUFFIX_RE = re.compile(r"[·・\s]*(?:普通|困难|困難|苦难|残酷|全限)$")
_POISE_REC_TIME_SCALAR = "PoiseRecTimeScalar"
_ATTRIBUTE_TYPES: dict[str, int | str] = {
    "MaxHp": 1,
    # BuffData spells these "Atk"/"Def"; the long forms never occur in the data and are
    # kept only so existing fixtures keep resolving.
    "Atk": 2,
    "Attack": 2,
    "Def": 3,
    "Defense": 3,
    "PoiseRecTime": 21,
    _POISE_REC_TIME_SCALAR: _POISE_REC_TIME_SCALAR,
}
_FORMULA_MODIFIER_TYPES = {
    "Addition": 0,
    "Multiplier": 1,
    "FinalAddition": 3,
    "FinalMultiplier": 4,
    "BaseAddition": 5,
    "BaseMultiplier": 6,
    "BaseFinalAddition": 7,
    "BaseFinalMultiplier": 8,
}
_MODIFIER_STAGES = (
    (5, "add", False),
    (6, "multiply", True),
    (7, "add", False),
    (8, "multiply", False),
    (3, "add", False),
    (4, "multiply", False),
    (0, "add", False),
    (1, "multiply", True),
)


@dataclass(frozen=True, slots=True)
class AkeDataVersion:
    id: str
    table_cfg_path: str
    updated_at: str


class AkeDataStageSource:
    def __init__(self, client: WarfarinClient):
        self.client = client
        self._version: AkeDataVersion | None = None
        self._tables: dict[str, dict[str, Any]] = {}
        self._resources: dict[str, dict[str, Any] | list[Any]] = {}
        self._asset_index: dict[str, Any] | None = None
        self._generation = 0

    def clear_caches(self) -> int:
        self._generation += 1
        removed = len(self._tables) + len(self._resources) + int(self._asset_index is not None)
        self._version = None
        # Replace containers: already running loaders may finish in the old
        # generation without repopulating the new one.
        self._tables = {}
        self._resources = {}
        self._asset_index = None
        return removed

    async def catalog(self) -> StageCatalogView:
        version = await self._latest_version()
        series, dungeons, texts = await self._load_tables(
            "DungeonSeriesTable", "DungeonTable", "I18nTextTable_CN"
        )
        return parse_akedata_catalog(version, series, dungeons, texts)

    async def stage(self, key: str) -> tuple[Stage, tuple[str, ...]]:
        version = await self._latest_version()
        table_names = (
            "DungeonSeriesTable",
            "DungeonTable",
            "I18nTextTable_CN",
            "RewardTable",
            "ItemTable",
            "EnemyTable",
            "EnemyTemplateDisplayInfoTable",
            "EnemyAttributeTemplateTable",
        )
        tables = await self._load_tables(*table_names)
        records, _series = _resolve_stage_records(key, tables[0], tables[1])
        if not records:
            raise StageDataIncomplete(f"AkeData 中没有关卡“{key}”。")
        spawners_by_scene, scripts_by_scene = await self._load_stage_spawners(records)
        buff_ids = _stage_buff_ids(records, tables[5], spawners_by_scene, scripts_by_scene)
        buff_table = await self._load_buffs(buff_ids)
        stage = parse_akedata_stage(
            version,
            key,
            *tables,
            spawners_by_scene=spawners_by_scene,
            scripts_by_scene=scripts_by_scene,
            buff_table=buff_table,
        )
        return stage, ()

    async def _latest_version(self) -> AkeDataVersion:
        generation = self._generation
        manifest = await self.client.akedata_manifest()
        version = parse_akedata_version(manifest)
        if generation != self._generation:
            return version
        if self._version is None or self._version.id != version.id:
            self._tables = {}
            self._resources = {}
            self._asset_index = None
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
            table = await self.client.akedata_table(version.table_cfg_path, name)
            target[name] = table
            return table

        return tuple(await asyncio.gather(*(load(name) for name in names)))

    async def _load_resource(self, path: str) -> dict[str, Any] | list[Any]:
        target = self._resources
        normalized = str(path or "").strip().lstrip("/")
        cached = target.get(normalized)
        if cached is not None:
            return cached
        resource = await self.client.akedata_public_json(normalized)
        target[normalized] = resource
        return resource

    async def _load_stage_spawners(
        self, records: tuple[dict[str, Any], ...]
    ) -> tuple[
        dict[str, tuple[dict[str, Any], ...]], dict[str, tuple[dict[str, Any], ...]]
    ]:
        scene_ids = tuple(
            dict.fromkeys(str(row.get("sceneId") or "").strip() for row in records)
        )
        scene_ids = tuple(scene_id for scene_id in scene_ids if scene_id)
        results = await asyncio.gather(
            *(self._load_scene_spawners(scene_id) for scene_id in scene_ids),
            return_exceptions=True,
        )
        configs: dict[str, tuple[dict[str, Any], ...]] = {}
        scripts: dict[str, tuple[dict[str, Any], ...]] = {}
        for scene_id, result in zip(scene_ids, results):
            if isinstance(result, BaseException):
                # Spawner exports only enrich enemy panels with instance/born-buff
                # modifiers. Some valid stages have no exported manifest, so a
                # missing optional resource must not make the whole stage unusable.
                logger.warning(
                    f"[endfield] AkeData spawner config unavailable "
                    f"scene={scene_id} error={type(result).__name__}: {result}"
                )
                configs[scene_id] = ()
                scripts[scene_id] = ()
            else:
                configs[scene_id], scripts[scene_id] = result
        return configs, scripts

    async def _load_scene_spawners(
        self, scene_id: str
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        """Load a scene's spawner configs and its level-script enemy placements.

        The two are distinct layers on AkeData: ``SpawnerConfig`` carries the
        ``enemyLibrary`` (with ``bornBuffList``) that waves spawn, while
        ``LevelScriptData`` carries scripted per-enemy buffs that apply on top of it
        (``AKECombatData.staticEnemyBuffs``).  Merging them into one library — as this
        loader used to — both dropped the scripted buffs and invented spawner entries.
        """
        spawner_paths = await self._asset_json_paths(f"SpawnerConfig/{scene_id}")
        script_paths = await self._asset_json_paths(f"LevelScriptData/{scene_id}")
        if not spawner_paths and not script_paths:
            raise StageDataIncomplete(f"AkeData 场景 {scene_id} 缺少刷怪配置清单。")
        resources = await asyncio.gather(
            *(
                self._load_resource(f"public/Json/{path}")
                for path in (*spawner_paths, *script_paths)
            )
        )
        spawner_count = len(spawner_paths)
        configs = tuple(
            resource
            for resource in resources[:spawner_count]
            if isinstance(resource, dict) and isinstance(resource.get("enemyLibrary"), list)
        )
        scripts = tuple(
            script
            for resource in resources[spawner_count:]
            if isinstance(resource, dict)
            if (script := _level_script_enemies(resource)) is not None
        )
        return configs, scripts

    async def _asset_json_paths(self, prefix: str) -> tuple[str, ...]:
        generation = self._generation
        index = self._asset_index
        if index is None:
            index = await self.client.akedata_asset_index()
            if generation == self._generation:
                self._asset_index = index
        datasets = index.get("datasets") or {}
        json_dataset = datasets.get("json") if isinstance(datasets, dict) else {}
        files = json_dataset.get("files") if isinstance(json_dataset, dict) else {}
        normalized = str(prefix or "").strip("/")
        prefix_parts = normalized.split("/") if normalized else []
        paths: list[str] = []
        for raw_path in files if isinstance(files, dict) else ():
            path = str(raw_path or "").strip("/")
            parts = path.split("/")
            if (
                len(parts) == len(prefix_parts) + 1
                and parts[: len(prefix_parts)] == prefix_parts
                and parts[-1].endswith(".json")
            ):
                paths.append(path)
        return tuple(sorted(paths))

    async def _load_buffs(self, buff_ids: set[str]) -> dict[str, dict[str, Any]]:
        ordered_ids = sorted(buff_ids)
        resources = await asyncio.gather(
            *(
                self._load_resource(f"public/Json/BuffData/{buff_id}.json")
                for buff_id in ordered_ids
            )
        )
        return {
            buff_id: resource
            for buff_id, resource in zip(ordered_ids, resources)
            if isinstance(resource, dict)
        }


def parse_akedata_version(manifest: dict[str, Any]) -> AkeDataVersion:
    latest = str(manifest.get("latest") or "").strip()
    versions = manifest.get("versions") or ()
    row = next(
        (
            item
            for item in versions
            if isinstance(item, dict) and str(item.get("id") or "").strip() == latest
        ),
        None,
    )
    if not latest or row is None:
        raise StageDataIncomplete("AkeData 版本清单缺少当前版本。")
    table_cfg_path = str(row.get("tableCfgPath") or "").strip("/")
    if not table_cfg_path:
        raise StageDataIncomplete("AkeData 当前版本缺少表路径。")
    updated_at = str(
        manifest.get("updatedAt") or row.get("publishedAt") or manifest.get("sharedRevision") or ""
    ).strip()
    return AkeDataVersion(latest, table_cfg_path, updated_at)


def parse_akedata_catalog(
    version: AkeDataVersion,
    series_table: dict[str, Any],
    dungeon_table: dict[str, Any],
    text_table: dict[str, Any],
) -> StageCatalogView:
    grouped: dict[str, list[StageCatalogItem]] = {key: [] for key in _FAMILY_ORDER}
    for series in _stage_series(series_table):
        series_name = _translated(series.get("name"), text_table) or str(series.get("id") or "")
        for stage_key, records in _series_stage_groups(series, dungeon_table):
            if not records:
                continue
            family_key, family_name = _akedata_family(records[0], series)
            name = _record_group_name(records, series_name, family_name, text_table)
            if not name:
                continue
            levels = [
                level
                for row in records
                if (level := _optional_int(row.get("recommendLv"))) is not None
            ]
            description = _translated(series.get("desc"), text_table)
            if not description:
                description = _translated(records[0].get("dungeonDesc"), text_table)
            region = series_name if family_key == MONUMENT_FAMILY_KEY else ""
            grouped[family_key].append(
                StageCatalogItem(
                    title=stage_key,
                    name=name,
                    family_key=family_key,
                    family_name=family_name,
                    revision=version.id,
                    updated_at=version.updated_at,
                    description=description,
                    recommended_level=max(levels) if levels else None,
                    region=region,
                    source="akedata",
                )
            )
    groups = tuple(
        StageCatalogGroup(
            key,
            _FAMILY_NAMES[key],
            tuple(sorted(grouped[key], key=lambda item: (item.name, item.title))),
        )
        for key in _FAMILY_ORDER
        if grouped[key]
    )
    return StageCatalogView(groups, "AkeData", version.id, version.updated_at)


def parse_akedata_stage(
    version: AkeDataVersion,
    key: str,
    series_table: dict[str, Any],
    dungeon_table: dict[str, Any],
    text_table: dict[str, Any],
    reward_table: dict[str, Any],
    item_table: dict[str, Any],
    enemy_table: dict[str, Any],
    enemy_display_table: dict[str, Any],
    enemy_attribute_table: dict[str, Any],
    *,
    spawners_by_scene: dict[str, tuple[dict[str, Any], ...]] | None = None,
    scripts_by_scene: dict[str, tuple[dict[str, Any], ...]] | None = None,
    buff_table: dict[str, dict[str, Any]] | None = None,
) -> Stage:
    records, series = _resolve_stage_records(key, series_table, dungeon_table)
    if not records or series is None:
        raise StageDataIncomplete(f"AkeData 中没有关卡“{key}”的详情。")
    series_id = str(series.get("id") or "")
    series_name = _translated(series.get("name"), text_table) or series_id
    representative = records[0]
    family_key, family_name = _akedata_family(representative, series)
    name = _record_group_name(records, series_name, family_name, text_table) or str(key)
    summary = _translated(series.get("desc"), text_table) or _translated(
        representative.get("dungeonDesc"), text_table
    )
    variants = tuple(
        _variant(
            row,
            text_table,
            reward_table,
            item_table,
            enemy_table,
            enemy_display_table,
            enemy_attribute_table,
            spawners_by_scene or {},
            scripts_by_scene or {},
            buff_table or {},
            label=_variant_label(row, index, len(records), text_table),
            sort_order=index,
        )
        for index, row in enumerate(records, 1)
    )
    aliases = tuple(
        dict.fromkeys(
            value
            for value in (
                name,
                *(_translated(row.get("dungeonName"), text_table) for row in records),
                f"{series_name} {name}",
                f"{family_name} {name}",
            )
            if value
        )
    )
    extension: MonumentStageDetails | BossRushStageDetails | None
    if family_key == MONUMENT_FAMILY_KEY:
        extension = MonumentStageDetails(series_id, series_name)
    elif family_key in {"boss_rush", "crisis_fragment"}:
        extension = BossRushStageDetails(
            boss_name=name,
            series_id=series_id,
            series_name=series_name,
            depth_count=len(variants),
            icon_url=_first_enemy_icon(variants),
        )
    else:
        extension = None
    source_target = (
        f"DungeonSeriesTable/{series_id}"
        if str(key).startswith(SERIES_STAGE_KEY_PREFIX)
        else f"DungeonTable/{key}"
    )
    return Stage(
        id=series_id if str(key).startswith(SERIES_STAGE_KEY_PREFIX) else str(key),
        name=name,
        aliases=aliases,
        family_key=family_key,
        family_name=family_name,
        summary=summary,
        location="",
        unlock_condition="",
        source=StageSourceRef(
            "AkeData",
            source_target,
            revision=version.id,
            updated_at=version.updated_at,
        ),
        variants=variants,
        extension=extension,
        icon_url=_first_enemy_icon(variants),
        template_name=str(representative.get("dungeonCategory") or "DungeonTable"),
        facts=(StageFact("关卡系列", series_name),) if series_name else (),
    )


def _variant(
    row: dict[str, Any],
    text_table: dict[str, Any],
    reward_table: dict[str, Any],
    item_table: dict[str, Any],
    enemy_table: dict[str, Any],
    enemy_display_table: dict[str, Any],
    enemy_attribute_table: dict[str, Any],
    spawners_by_scene: dict[str, tuple[dict[str, Any], ...]],
    scripts_by_scene: dict[str, tuple[dict[str, Any], ...]],
    buff_table: dict[str, dict[str, Any]],
    *,
    label: str = "",
    sort_order: int = 0,
) -> StageVariant:
    dungeon_id = str(row.get("dungeonId") or "")
    hard = dungeon_id.endswith("_s")
    rewards = _rewards(row.get("firstPassRewardId"), reward_table, item_table, text_table)
    reward_sets = None
    if rewards is not None:
        reward_sets = StageRewards(
            (StageRewardGroup("首通", rewards),),
            title="首通奖励",
        )
    return StageVariant(
        id=dungeon_id,
        label=label or ("苦难" if hard else "普通"),
        sort_order=sort_order or (2 if hard else 1),
        recommended_level=_optional_int(row.get("recommendLv")),
        stamina_cost=_optional_int(row.get("costStamina")),
        mechanics=_mechanics(_translated(row.get("featureDesc"), text_table)),
        enemies=_enemies(
            row,
            text_table,
            enemy_table,
            enemy_display_table,
            enemy_attribute_table,
            spawners_by_scene.get(str(row.get("sceneId") or ""), ()),
            scripts_by_scene.get(str(row.get("sceneId") or ""), ()),
            buff_table,
        ),
        rewards=rewards,
        reward_sets=reward_sets,
        facts=(StageFact("关卡 ID", dungeon_id),),
    )


def _stage_series(series_table: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = [
        row
        for row in series_table.values()
        if isinstance(row, dict)
        and str(row.get("gameCategory") or "")
        and row.get("includeDungeonIds")
    ]
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                _optional_int(row.get("sortId")) or 0,
                str(row.get("gameCategory") or ""),
                str(row.get("id") or ""),
            ),
        )
    )


def _series_stage_groups(
    series: dict[str, Any], dungeon_table: dict[str, Any]
) -> tuple[tuple[str, tuple[dict[str, Any], ...]], ...]:
    category = str(series.get("gameCategory") or "")
    rows = tuple(
        row
        for raw_id in series.get("includeDungeonIds") or ()
        if isinstance((row := dungeon_table.get(str(raw_id or ""))), dict)
        and str(row.get("dungeonCategory") or "") == category
    )
    if not rows:
        return ()
    if category in _SERIES_WIDE_CATEGORIES:
        return ((f"{SERIES_STAGE_KEY_PREFIX}{series.get('id')}", rows),)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        dungeon_id = str(row.get("dungeonId") or "")
        base_id = _variant_base_id(dungeon_id, category)
        grouped.setdefault(base_id, []).append(row)
    return tuple((key, tuple(group)) for key, group in grouped.items())


def _variant_base_id(dungeon_id: str, category: str) -> str:
    if category == HIGH_DIFFICULTY_CATEGORY:
        return dungeon_id.removesuffix("_s")
    if category == "dungeon_seasontower":
        return re.sub(r"_(?:s|ex)$", "", dungeon_id)
    if category == "dungeon_takestwo":
        return dungeon_id.removesuffix("_hard")
    return dungeon_id


def _resolve_stage_records(
    key: str,
    series_table: dict[str, Any],
    dungeon_table: dict[str, Any],
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any] | None]:
    normalized = str(key or "")
    requested_series = (
        normalized[len(SERIES_STAGE_KEY_PREFIX) :]
        if normalized.startswith(SERIES_STAGE_KEY_PREFIX)
        else ""
    )
    for series in _stage_series(series_table):
        if requested_series and str(series.get("id") or "") != requested_series:
            continue
        for stage_key, records in _series_stage_groups(series, dungeon_table):
            if stage_key == normalized or any(
                str(row.get("dungeonId") or "") == normalized for row in records
            ):
                return records, series
    return (), None


def _akedata_family(
    row: dict[str, Any], series: dict[str, Any]
) -> tuple[str, str]:
    category = str(row.get("dungeonCategory") or series.get("gameCategory") or "")
    if category == "dungeon_bossrush" and "minibossrush" in str(series.get("id") or ""):
        return "crisis_fragment", _FAMILY_NAMES["crisis_fragment"]
    return _CATEGORY_FAMILIES.get(category, ("other", _FAMILY_NAMES["other"]))


def _record_group_name(
    records: tuple[dict[str, Any], ...],
    series_name: str,
    family_name: str,
    text_table: dict[str, Any],
) -> str:
    category = str(records[0].get("dungeonCategory") or "")
    if category in _SERIES_WIDE_CATEGORIES:
        name = series_name
    else:
        name = _stage_name(records[0], text_table)
    prefixes = (
        f"{family_name}·",
        f"{family_name}・",
        "协议空间·",
        "协议空间・",
    )
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :].strip()
    return name.strip()


def _variant_label(
    row: dict[str, Any], index: int, count: int, text_table: dict[str, Any]
) -> str:
    dungeon_id = str(row.get("dungeonId") or "")
    category = str(row.get("dungeonCategory") or "")
    if category == HIGH_DIFFICULTY_CATEGORY:
        return "苦难" if dungeon_id.endswith("_s") else "普通"
    if category == "dungeon_seasontower":
        if dungeon_id.endswith("_ex"):
            return "残酷"
        return "困难" if dungeon_id.endswith("_s") else "普通"
    if category == "dungeon_takestwo":
        return "困难" if dungeon_id.endswith("_hard") else "普通"
    level_desc = _translated(row.get("dungeonLevelDesc"), text_table)
    if level_desc:
        return level_desc
    if count > 1:
        return f"第{index}档"
    return "详情"


def _first_enemy_icon(variants: tuple[StageVariant, ...]) -> str:
    return next(
        (
            enemy.icon_url
            for variant in variants
            for enemy in variant.enemies or ()
            if enemy.icon_url
        ),
        "",
    )


def _stage_name(row: dict[str, Any], text_table: dict[str, Any]) -> str:
    return _DIFFICULTY_SUFFIX_RE.sub("", _translated(row.get("dungeonName"), text_table)).strip()


def _translated(reference: Any, text_table: dict[str, Any]) -> str:
    return localized_text(reference, translations=text_table)


def _mechanics(value: str) -> tuple[str, ...]:
    cleaned = _RICH_TAG_RE.sub("", str(value or "")).replace("\r", "")
    return tuple(
        line
        for raw in cleaned.split("\n")
        if (line := raw.strip().lstrip("-• ").strip())
    )


def _rewards(
    reward_id: Any,
    reward_table: dict[str, Any],
    item_table: dict[str, Any],
    text_table: dict[str, Any],
) -> tuple[StageReward, ...] | None:
    reward_id = str(reward_id or "")
    if not reward_id:
        return ()
    row = reward_table.get(reward_id)
    if not isinstance(row, dict):
        return None
    rewards: list[StageReward] = []
    for bundle in row.get("itemBundles") or ():
        if not isinstance(bundle, dict):
            continue
        item_id = str(bundle.get("id") or "")
        item = item_table.get(item_id)
        if not item_id or not isinstance(item, dict):
            continue
        icon_id = str(item.get("iconId") or item_id)
        rewards.append(
            StageReward(
                item_id=item_id,
                name=_translated(item.get("name"), text_table) or item_id,
                icon_url=f"{AKEDATA_ASSET_BASE_URL}/itemiconbig/{icon_id}.png",
                quantity_text=_quantity_text(bundle.get("count")),
                rarity=_optional_int(item.get("rarity")),
            )
        )
    return tuple(rewards)


def _enemies(
    dungeon: dict[str, Any],
    text_table: dict[str, Any],
    enemy_table: dict[str, Any],
    enemy_display_table: dict[str, Any],
    enemy_attribute_table: dict[str, Any],
    spawner_configs: tuple[dict[str, Any], ...] = (),
    level_script_enemies: tuple[dict[str, Any], ...] = (),
    buff_table: dict[str, dict[str, Any]] | None = None,
) -> tuple[StageEnemy, ...]:
    buff_table = buff_table or {}
    spawner_configs = _applicable_spawner_configs(spawner_configs, dungeon)
    result: list[StageEnemy] = []
    levels = dungeon.get("enemyLevels") or ()
    for index, enemy_id in enumerate(dungeon.get("enemyIds") or ()):
        enemy_id = str(enemy_id or "")
        enemy = enemy_table.get(enemy_id)
        if not enemy_id or not isinstance(enemy, dict):
            continue
        template_id = str(enemy.get("templateId") or enemy_id)
        display = enemy_display_table.get(template_id)
        display = display if isinstance(display, dict) else {}
        attr_id = str(enemy.get("attrTemplateId") or template_id)
        attributes = enemy_attribute_table.get(attr_id)
        attributes = attributes if isinstance(attributes, dict) else {}
        level = _optional_int(levels[index]) if index < len(levels) else None
        library_buffs = _matching_spawner_buffs(spawner_configs, enemy_id, enemy_table)
        library_buffs += _matching_script_buffs(
            level_script_enemies, enemy_id, level, enemy_table
        )
        modifiers = _enemy_modifiers(enemy, library_buffs, buff_table)
        hp, attack, defense = _enemy_metrics(attributes, level, modifiers)
        result.append(
            StageEnemy(
                enemy_id=enemy_id,
                name=_translated(display.get("name"), text_table) or template_id,
                icon_url=f"{AKEDATA_ASSET_BASE_URL}/monstericonbig/{template_id}.png",
                level=level,
                hp=hp,
                attack=attack,
                defense=defense,
                resistances=_enemy_resistances(attributes),
                poise=_enemy_poise(attributes, modifiers),
            )
        )
    return tuple(result)


def _enemy_metrics(
    attributes: dict[str, Any],
    level: int | None,
    modifiers: tuple[dict[str, int | float | str], ...] = (),
) -> tuple[int | float | None, int | float | None, int | float | None]:
    if level is None:
        return None, None, None
    for row in attributes.get("levelDependentAttributes") or ():
        values = {
            _optional_int(item.get("attrType")): item.get("attrValue")
            for item in row.get("attrs") or ()
            if isinstance(item, dict)
        }
        if _optional_int(values.get(0)) == level:
            return tuple(
                _integer_combat_stat(
                    _apply_modifiers(
                        _optional_number(values.get(attr_type)), modifiers, attr_type
                    )
                )
                for attr_type in (1, 2, 3)
            )
    return None, None, None


def _integer_combat_stat(value: int | float | None) -> int | None:
    """Game-facing HP/ATK/DEF are integer stats; positive fractions are truncated."""
    return int(value) if value is not None else None


def _stage_buff_ids(
    records: tuple[dict[str, Any], ...],
    enemy_table: dict[str, Any],
    spawners_by_scene: dict[str, tuple[dict[str, Any], ...]],
    scripts_by_scene: dict[str, tuple[dict[str, Any], ...]] | None = None,
) -> set[str]:
    buff_ids: set[str] = set()
    scripts_by_scene = scripts_by_scene or {}
    for row in records:
        levels = row.get("enemyLevels") or ()
        scene_id = str(row.get("sceneId") or "")
        configs = _applicable_spawner_configs(
            spawners_by_scene.get(scene_id, ()), row
        )
        scripts = scripts_by_scene.get(scene_id, ())
        for index, raw_enemy_id in enumerate(row.get("enemyIds") or ()):
            enemy_id = str(raw_enemy_id or "")
            enemy = enemy_table.get(enemy_id)
            if isinstance(enemy, dict):
                buff_ids.update(str(buff_id) for buff_id in enemy.get("bornBuffs") or () if buff_id)
            level = _optional_int(levels[index]) if index < len(levels) else None
            for buff in (
                *_matching_spawner_buffs(configs, enemy_id, enemy_table),
                *_matching_script_buffs(scripts, enemy_id, level, enemy_table),
            ):
                if buff.get("buffId"):
                    buff_ids.add(str(buff["buffId"]))
    return buff_ids


def _level_script_enemies(resource: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a ``LevelScriptData`` export to ``{scriptId, enemyLibrary}``.

    These are scripted per-enemy buffs, not spawner wave data: AkeData applies them
    through ``AKECombatData.staticEnemyBuffs``, keyed by enemy id and level.
    """
    instances = resource.get("enemies")
    if not isinstance(instances, dict):
        return None
    library: list[dict[str, Any]] = []
    for instance in instances.values():
        if not isinstance(instance, dict):
            continue
        enemy_id = str(instance.get("entityDataIdKey") or "")
        if not enemy_id:
            continue
        library.append(
            {
                "enemyId": enemy_id,
                "enemyLevel": instance.get("level"),
                "bornBuffList": instance.get("buffs") or (),
            }
        )
    if not library:
        return None
    return {
        "configId": str(resource.get("scriptId") or resource.get("configId") or ""),
        "enemyLibrary": library,
    }


def _enemy_instance_config(resource: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a spawner or level-script export to the ``enemyLibrary`` shape."""
    if isinstance(resource.get("enemyLibrary"), list):
        return resource
    return _level_script_enemies(resource)


def _enemy_template_id(enemy_id: str, enemy_table: dict[str, Any]) -> str:
    """The monster identity that ``DungeonTable`` and spawner libraries agree on.

    ``DungeonTable.enemyIds`` name the base monster, while a spawner's ``enemyLibrary``
    names the concrete variant placed in that stage (``_dungeon``, ``_hdg019``,
    ``_activityspecial_01``, ``_tower011`` ...).  Both sides share
    ``EnemyTable.templateId``, so library entries are joined on it instead of on the
    variant id — joining on the id silently dropped every variant's ``bornBuffList``.
    """
    row = enemy_table.get(str(enemy_id or ""))
    if isinstance(row, dict):
        template_id = str(row.get("templateId") or "").strip()
        if template_id:
            return template_id
    return str(enemy_id or "")


def _wave_library_entries(config: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """The ``enemyLibrary`` entries a spawner config's waves actually place.

    Only entries referenced by an action's ``libraryKey`` count, and paused actions are
    skipped — a library may list monsters the waves never place, and those must not
    contribute their ``bornBuffList``.
    """
    library = [row for row in config.get("enemyLibrary") or () if isinstance(row, dict)]
    by_key = {row.get("key"): row for row in library}
    entries: list[dict[str, Any]] = []
    for wave in (config.get("waveMap") or {}).values():
        if not isinstance(wave, dict):
            continue
        for group in (wave.get("groupMap") or {}).values():
            if not isinstance(group, dict):
                continue
            for action in (group.get("actionMap") or {}).values():
                if not isinstance(action, dict):
                    continue
                if "Pause" in str(action.get("$type") or ""):
                    continue
                library_enemy = by_key.get(action.get("libraryKey"))
                if library_enemy is not None:
                    entries.append(library_enemy)
    return tuple(entries)


def _applicable_spawner_configs(
    configs: tuple[dict[str, Any], ...],
    row: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Keep the spawner configs AkeData itself would use for this stage.

    A scene holds one config per difficulty.  The site keeps a config only when its
    ``enemyLibrary`` levels are not exactly ``{0}`` and, for a stage with a
    ``recommendLv``, when those levels contain it; otherwise every config of the scene
    applies, which is how cross-difficulty buffs legitimately stack on stages such as
    ``dung01_takestwo*`` (``recommendLv`` 0).  When nothing qualifies the caller falls
    back to ``DungeonTable.enemyIds`` with no library buffs at all.
    """
    if not configs:
        return ()
    recommend_level = _optional_int(row.get("recommendLv")) or 0
    applicable: list[dict[str, Any]] = []
    for config in configs:
        library = [e for e in config.get("enemyLibrary") or () if isinstance(e, dict)]
        if not library:
            continue
        levels = {_optional_int(entry.get("enemyLevel")) for entry in library}
        if levels == {0}:
            continue
        if recommend_level > 0 and recommend_level not in levels:
            continue
        applicable.append(config)
    return tuple(applicable)


def _matching_spawner_buffs(
    configs: tuple[dict[str, Any], ...],
    enemy_id: str,
    enemy_table: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Union the ``bornBuffList`` a stage's applicable configs give one enemy.

    Buffs are joined on ``EnemyTable.templateId`` (the monster identity both
    ``DungeonTable`` and the libraries agree on) and deliberately not on the entry's
    level: the site buckets them per spawned enemy instance, so the same monster's
    entries across a scene's configs all apply.  Duplicates are resolved first-wins by
    ``buffId``, matching the site's own dedup over its alphabetically ordered configs.
    """
    enemy_table = enemy_table or {}
    target_template = _enemy_template_id(enemy_id, enemy_table)
    buffs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for config in configs:
        for library_enemy in _wave_library_entries(config):
            if _enemy_template_id(
                str(library_enemy.get("enemyId") or ""), enemy_table
            ) != target_template:
                continue
            for buff in library_enemy.get("bornBuffList") or ():
                if not isinstance(buff, dict):
                    continue
                buff_id = str(buff.get("buffId") or "")
                if not buff_id or buff_id in seen_ids:
                    continue
                seen_ids.add(buff_id)
                buffs.append(buff)
    return tuple(buffs)


def _matching_script_buffs(
    level_script_enemies: tuple[dict[str, Any], ...],
    enemy_id: str,
    level: int | None,
    enemy_table: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Scripted buffs for one enemy, mirroring ``AKECombatData.staticEnemyBuffs``.

    Entries match on enemy id (variant or base) at the exact level; when no entry
    matches the level, a single same-enemy entry still applies, as on the site.
    """
    enemy_table = enemy_table or {}
    target_template = _enemy_template_id(enemy_id, enemy_table)
    matches = [
        entry
        for script in level_script_enemies
        for entry in script.get("enemyLibrary") or ()
        if isinstance(entry, dict)
        and (
            str(entry.get("enemyId") or "") == enemy_id
            or _enemy_template_id(str(entry.get("enemyId") or ""), enemy_table)
            == target_template
        )
    ]
    exact = [entry for entry in matches if _optional_int(entry.get("enemyLevel")) == level]
    selected = exact or (matches if len(matches) == 1 else [])
    return tuple(
        buff
        for entry in selected
        for buff in entry.get("bornBuffList") or ()
        if isinstance(buff, dict)
    )


def _enemy_modifiers(
    enemy: dict[str, Any],
    library_buffs: tuple[dict[str, Any], ...],
    buff_table: dict[str, dict[str, Any]],
) -> tuple[dict[str, int | float | str], ...]:
    modifiers = list(_inline_modifiers(enemy.get("attrModifiers")))
    for buff_id in enemy.get("bornBuffs") or ():
        buff = buff_table.get(str(buff_id or ""))
        if isinstance(buff, dict):
            modifiers.extend(_buff_modifiers(buff))
    for library_buff in library_buffs:
        buff = buff_table.get(str(library_buff.get("buffId") or ""))
        if isinstance(buff, dict):
            modifiers.extend(_buff_modifiers(buff, library_buff.get("blackboard")))
    return tuple(modifiers)


def _inline_modifiers(value: Any) -> tuple[dict[str, int | float | str], ...]:
    modifiers: list[dict[str, int | float | str]] = []
    for row in value or ():
        if not isinstance(row, dict):
            continue
        attr_type = _optional_int(row.get("attrType"))
        modifier_type = _optional_int(row.get("modifierType"))
        attr_value = _optional_number(row.get("attrValue"))
        if attr_type is None or modifier_type is None or attr_value is None:
            continue
        modifiers.append(
            {"attrType": attr_type, "modifierType": modifier_type, "attrValue": attr_value}
        )
    return tuple(modifiers)


def _buff_modifiers(
    buff: dict[str, Any], blackboard_overrides: Any = ()
) -> tuple[dict[str, int | float | str], ...]:
    blackboard = _blackboard_values(buff.get("blackboard"))
    blackboard.update(_blackboard_values(blackboard_overrides))
    attribute_modifier = buff.get("attributeModifier")
    attribute_modifier = attribute_modifier if isinstance(attribute_modifier, dict) else {}
    modifiers: list[dict[str, int | float | str]] = []
    for row in attribute_modifier.get("attributeModifiers") or ():
        if not isinstance(row, dict):
            continue
        attr_type = _ATTRIBUTE_TYPES.get(str(row.get("attributeType") or ""))
        modifier_type = _FORMULA_MODIFIER_TYPES.get(str(row.get("formulaItem") or ""))
        param = row.get("param")
        param = param if isinstance(param, dict) else {}
        value = param.get("value")
        if param.get("useBlackboardKey") and param.get("blackboardKey"):
            value = blackboard.get(str(param.get("blackboardKey")), value)
        attr_value = _optional_number(value)
        if attr_type is None or modifier_type is None or attr_value is None:
            continue
        modifiers.append(
            {"attrType": attr_type, "modifierType": modifier_type, "attrValue": attr_value}
        )
    return tuple(modifiers)


def _blackboard_values(rows: Any) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if not key:
            continue
        for field in ("valueFloat", "valueDouble", "valueInt", "valueLong", "value"):
            if field not in row:
                continue
            value = _optional_number(row.get(field))
            if value is not None:
                values[key] = value
                break
    return values


def _apply_modifiers(
    base_value: int | float | None,
    modifiers: tuple[dict[str, int | float | str], ...],
    attr_type: int | str,
) -> int | float | None:
    if base_value is None:
        return None
    relevant = tuple(row for row in modifiers if row.get("attrType") == attr_type)
    if not relevant:
        return base_value
    value = float(base_value)
    for modifier_type, operation, one_plus in _MODIFIER_STAGES:
        for row in relevant:
            if row.get("modifierType") != modifier_type:
                continue
            operand = float(row["attrValue"])
            if operation == "add":
                value += operand
            else:
                value *= 1.0 + operand if one_plus else operand
    normalized = round(value, 8)
    return int(normalized) if normalized.is_integer() else normalized


def _enemy_resistances(
    attributes: dict[str, Any],
) -> tuple[StageEnemyResistance, ...] | None:
    if not attributes:
        return None
    definitions = (
        ("Physical", "物理", "physicalResistance", "888888"),
        ("Fire", "灼热", "fireResistance", "FF623D"),
        ("Pulse", "电磁", "pulseResistance", "FFC000"),
        ("Cryst", "寒冷", "crystResistance", "21C6D0"),
        ("Natural", "自然", "naturalResistance", "9EDC23"),
    )
    rows: list[StageEnemyResistance] = []
    for element, label, field, color in definitions:
        resistance = _optional_float(attributes.get(field))
        if resistance is None:
            continue
        percent = 100.0 - resistance
        rows.append(StageEnemyResistance(element, label, percent, percent / 100.0, color))
    return tuple(rows)


def _enemy_poise(
    attributes: dict[str, Any],
    modifiers: tuple[dict[str, int | float | str], ...] = (),
) -> StageEnemyPoise | None:
    independent = attributes.get("levelIndependentAttributes")
    independent = independent if isinstance(independent, dict) else {}
    values = {
        _optional_int(item.get("attrType")): item.get("attrValue")
        for item in independent.get("attrs") or ()
        if isinstance(item, dict)
    }
    raw_knots = attributes.get("poiseKnotPctList")
    knots = (
        tuple(
            value
            for item in raw_knots
            if (value := _optional_float(item)) is not None
        )
        if isinstance(raw_knots, list) and raw_knots
        else None
    )
    poise = StageEnemyPoise(
        max_value=_optional_float(values.get(20)),
        damage_scalar=_optional_float(values.get(27)),
        recover_seconds=_optional_float(
            _apply_modifiers(_optional_number(values.get(21)), modifiers, 21)
        ),
        recover_scalar=_modified_scalar(modifiers, _POISE_REC_TIME_SCALAR),
        knots=knots,
    )
    return None if poise.is_empty else poise


def _modified_scalar(
    modifiers: tuple[dict[str, int | float | str], ...], attr_type: int | str
) -> float | None:
    if not any(row.get("attrType") == attr_type for row in modifiers):
        return None
    value = _apply_modifiers(1.0, modifiers, attr_type)
    return None if value is None else float(value)


def _quantity_text(value: Any) -> str:
    number = _optional_int(value)
    return f"×{number:,}" if number is not None else ""


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _optional_number(value: Any) -> int | float | None:
    number = _optional_float(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else number
