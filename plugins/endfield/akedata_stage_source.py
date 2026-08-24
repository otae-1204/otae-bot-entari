from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

from loguru import logger

from .account_i18n import localized_text
from .client import WarfarinClient
from .stage_models import (
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
from .stage_source import StageDataIncomplete


MONUMENT_FAMILY_KEY = "monument"
MONUMENT_FAMILY_NAME = "影拓丰碑"
HIGH_DIFFICULTY_CATEGORY = "dungeon_highdifficulty"
AKEDATA_ASSET_BASE_URL = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)
_RICH_TAG_RE = re.compile(r"<@[^>]+>|</>")
_DIFFICULTY_SUFFIX_RE = re.compile(r"[·・\s]*(?:苦难|困難)$")
_POISE_REC_TIME_SCALAR = "PoiseRecTimeScalar"
_ATTRIBUTE_TYPES: dict[str, int | str] = {
    "MaxHp": 1,
    "Attack": 2,
    "Defense": 3,
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
        dungeon_table = tables[1]
        base_id = str(key or "").removesuffix("_s")
        records = tuple(
            row
            for dungeon_id in (base_id, f"{base_id}_s")
            if isinstance((row := dungeon_table.get(dungeon_id)), dict)
        )
        spawners_by_scene = await self._load_stage_spawners(records)
        buff_ids = _stage_buff_ids(records, tables[5], spawners_by_scene)
        buff_table = await self._load_buffs(buff_ids)
        stage = parse_akedata_stage(
            version,
            key,
            *tables,
            spawners_by_scene=spawners_by_scene,
            buff_table=buff_table,
        )
        return stage, ()

    async def _latest_version(self) -> AkeDataVersion:
        manifest = await self.client.akedata_manifest()
        version = parse_akedata_version(manifest)
        if self._version is None or self._version.id != version.id:
            self._tables.clear()
            self._resources.clear()
        self._version = version
        return version

    async def _load_tables(self, *names: str) -> tuple[dict[str, Any], ...]:
        version = self._version or await self._latest_version()

        async def load(name: str) -> dict[str, Any]:
            cached = self._tables.get(name)
            if cached is not None:
                return cached
            table = await self.client.akedata_table(version.table_cfg_path, name)
            self._tables[name] = table
            return table

        return tuple(await asyncio.gather(*(load(name) for name in names)))

    async def _load_resource(self, path: str) -> dict[str, Any] | list[Any]:
        normalized = str(path or "").strip().lstrip("/")
        cached = self._resources.get(normalized)
        if cached is not None:
            return cached
        resource = await self.client.akedata_public_json(normalized)
        self._resources[normalized] = resource
        return resource

    async def _load_stage_spawners(
        self, records: tuple[dict[str, Any], ...]
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        scene_ids = tuple(
            dict.fromkeys(str(row.get("sceneId") or "").strip() for row in records)
        )
        scene_ids = tuple(scene_id for scene_id in scene_ids if scene_id)
        results = await asyncio.gather(
            *(self._load_scene_spawners(scene_id) for scene_id in scene_ids),
            return_exceptions=True,
        )
        configs: dict[str, tuple[dict[str, Any], ...]] = {}
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
            else:
                configs[scene_id] = result
        return configs

    async def _load_scene_spawners(self, scene_id: str) -> tuple[dict[str, Any], ...]:
        base = f"public/Json/SpawnerConfig/{scene_id}"
        manifest = await self._load_resource(f"{base}/manifest.json")
        if not isinstance(manifest, list):
            raise StageDataIncomplete(f"AkeData 场景 {scene_id} 缺少刷怪配置清单。")
        entries = sorted(
            (entry for entry in manifest if isinstance(entry, dict) and not entry.get("hidden")),
            key=lambda entry: (
                _optional_int(entry.get("priority")) or 999,
                str(entry.get("id") or ""),
            ),
        )
        paths = [
            str(entry.get("contentFile") or f"{base}/{entry.get('id')}.json")
            for entry in entries
            if entry.get("contentFile") or entry.get("id")
        ]
        resources = await asyncio.gather(*(self._load_resource(path) for path in paths))
        return tuple(resource for resource in resources if isinstance(resource, dict))

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
    items: list[StageCatalogItem] = []
    for series in _monument_series(series_table):
        series_name = _translated(series.get("name"), text_table) or str(series.get("id") or "")
        for dungeon_id in series.get("includeDungeonIds") or ():
            dungeon_id = str(dungeon_id or "")
            if not dungeon_id or dungeon_id.endswith("_s"):
                continue
            normal = dungeon_table.get(dungeon_id)
            hard = dungeon_table.get(f"{dungeon_id}_s")
            if not isinstance(normal, dict) and not isinstance(hard, dict):
                continue
            representative = normal if isinstance(normal, dict) else hard
            if representative.get("dungeonCategory") != HIGH_DIFFICULTY_CATEGORY:
                continue
            name = _stage_name(representative, text_table)
            if not name:
                continue
            levels = [
                _optional_int(row.get("recommendLv"))
                for row in (normal, hard)
                if isinstance(row, dict)
            ]
            known_levels = [level for level in levels if level is not None]
            items.append(
                StageCatalogItem(
                    title=dungeon_id,
                    name=name,
                    family_key=MONUMENT_FAMILY_KEY,
                    family_name=MONUMENT_FAMILY_NAME,
                    revision=version.id,
                    updated_at=version.updated_at,
                    description=f"丰碑系列 · {series_name}",
                    recommended_level=max(known_levels) if known_levels else None,
                    region=series_name,
                    source="akedata",
                )
            )
    group = StageCatalogGroup(MONUMENT_FAMILY_KEY, MONUMENT_FAMILY_NAME, tuple(items))
    return StageCatalogView((group,), "AkeData", version.id, version.updated_at)


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
    buff_table: dict[str, dict[str, Any]] | None = None,
) -> Stage:
    base_id = str(key or "").removesuffix("_s")
    series = next(
        (
            row
            for row in _monument_series(series_table)
            if base_id in {str(item or "").removesuffix("_s") for item in row.get("includeDungeonIds") or ()}
        ),
        None,
    )
    if series is None:
        raise StageDataIncomplete(f"AkeData 中没有丰碑关卡“{key}”。")
    records = [
        dungeon_table.get(base_id),
        dungeon_table.get(f"{base_id}_s"),
    ]
    records = [
        row
        for row in records
        if isinstance(row, dict) and row.get("dungeonCategory") == HIGH_DIFFICULTY_CATEGORY
    ]
    if not records:
        raise StageDataIncomplete(f"AkeData 中没有丰碑关卡“{key}”的详情。")

    series_id = str(series.get("id") or "")
    series_name = _translated(series.get("name"), text_table) or series_id
    normal = next((row for row in records if not str(row.get("dungeonId") or "").endswith("_s")), None)
    representative = normal or records[0]
    name = _stage_name(representative, text_table) or base_id
    summary = _translated(representative.get("dungeonDesc"), text_table)
    variants = tuple(
        sorted(
            (
                _variant(
                    row,
                    text_table,
                    reward_table,
                    item_table,
                    enemy_table,
                    enemy_display_table,
                    enemy_attribute_table,
                    spawners_by_scene or {},
                    buff_table or {},
                )
                for row in records
            ),
            key=lambda item: item.sort_order,
        )
    )
    aliases = tuple(
        dict.fromkeys(
            value
            for value in (
                name,
                *(_translated(row.get("dungeonName"), text_table) for row in records),
                f"{series_name} {name}",
                f"{MONUMENT_FAMILY_NAME} {name}",
            )
            if value
        )
    )
    extension = MonumentStageDetails(series_id, series_name)
    return Stage(
        id=base_id,
        name=name,
        aliases=aliases,
        family_key=MONUMENT_FAMILY_KEY,
        family_name=MONUMENT_FAMILY_NAME,
        summary=summary,
        location="",
        unlock_condition="",
        source=StageSourceRef(
            "AkeData",
            f"DungeonTable/{base_id}",
            revision=version.id,
            updated_at=version.updated_at,
        ),
        variants=variants,
        extension=extension,
        template_name="DungeonTable",
        facts=(StageFact("丰碑系列", series_name),),
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
    buff_table: dict[str, dict[str, Any]],
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
        label="苦难" if hard else "普通",
        sort_order=2 if hard else 1,
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
            buff_table,
        ),
        rewards=rewards,
        reward_sets=reward_sets,
        facts=(StageFact("关卡 ID", dungeon_id),),
    )


def _monument_series(series_table: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = [
        row
        for row in series_table.values()
        if isinstance(row, dict) and row.get("gameCategory") == HIGH_DIFFICULTY_CATEGORY
    ]
    return tuple(sorted(rows, key=lambda row: (_optional_int(row.get("sortId")) or 0, str(row.get("id") or ""))))


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
    buff_table: dict[str, dict[str, Any]] | None = None,
) -> tuple[StageEnemy, ...]:
    buff_table = buff_table or {}
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
        library_buffs = _matching_spawner_buffs(spawner_configs, enemy_id, level)
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
                _apply_modifiers(_optional_number(values.get(attr_type)), modifiers, attr_type)
                for attr_type in (1, 2, 3)
            )
    return None, None, None


def _stage_buff_ids(
    records: tuple[dict[str, Any], ...],
    enemy_table: dict[str, Any],
    spawners_by_scene: dict[str, tuple[dict[str, Any], ...]],
) -> set[str]:
    buff_ids: set[str] = set()
    for row in records:
        levels = row.get("enemyLevels") or ()
        configs = spawners_by_scene.get(str(row.get("sceneId") or ""), ())
        for index, raw_enemy_id in enumerate(row.get("enemyIds") or ()):
            enemy_id = str(raw_enemy_id or "")
            enemy = enemy_table.get(enemy_id)
            if isinstance(enemy, dict):
                buff_ids.update(str(buff_id) for buff_id in enemy.get("bornBuffs") or () if buff_id)
            level = _optional_int(levels[index]) if index < len(levels) else None
            buff_ids.update(
                str(buff.get("buffId"))
                for buff in _matching_spawner_buffs(configs, enemy_id, level)
                if buff.get("buffId")
            )
    return buff_ids


def _matching_spawner_buffs(
    configs: tuple[dict[str, Any], ...], enemy_id: str, level: int | None
) -> tuple[dict[str, Any], ...]:
    buffs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for config in configs:
        for library_enemy in config.get("enemyLibrary") or ():
            if not isinstance(library_enemy, dict):
                continue
            if str(library_enemy.get("enemyId") or "") != enemy_id:
                continue
            if level is not None and _optional_int(library_enemy.get("enemyLevel")) != level:
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
        recover_seconds=_optional_float(values.get(21)),
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
