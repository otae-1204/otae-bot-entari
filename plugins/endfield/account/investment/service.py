from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from fractions import Fraction
from hashlib import md5
from typing import Any, Mapping

from .models import (
    AccountInvestmentView,
    InvestmentCategoryView,
    InvestmentContributionView,
    InvestmentResourceView,
)
from ..detail.names import AccountDetailNameMap
from ..i18n import localized_text, server_label
from ...providers.akedata import _get, fetch_akedata_manifest
from ...gacha.service import format_timestamp


AKEDATA_SPRITES = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/"
    "gameplay/ui/sprites"
)
_TABLE_MAX_BYTES = 48 * 1024 * 1024
_I18N_MAX_BYTES = 64 * 1024 * 1024
_RECIPE_MAX_BYTES = 32 * 1024 * 1024
_BANNED_RECIPE_ITEMS = frozenset(
    {
        "item_ap",
        "item_diamond",
        "item_originium_recharge",
        "item_gold",
        "item_ticketgacha_standard_single",
        "item_ticketgacha_special_single",
        "item_ticketgacha_beginner_ten",
    }
)
# Protocol Space's five dedicated rare operator-material stages are encoded
# as ``dung_ss01`` .. ``dung_ss05``.  The fixed high-tier cultivation reward
# rows use ``count=0`` in RewardTable; the game grants six copies of the
# stage-specific material per completed run.  The run cost comes from the
# ``DungeonTable.hunterModeCostStamina`` field (80 in the current data).  The
# series ``staminaText`` is only the AKE display tag and is not the repeatable
# reward-mode cost used for this calculation.
_PROTOCOL_SPECIALIZATION_ITEMS = frozenset(
    f"item_char_skill_specialize_{index}" for index in range(1, 6)
)
_PROTOCOL_SPECIALIZATION_FIXED_COUNT = 6


class InvestmentDataUnavailable(RuntimeError):
    """AKEData did not provide enough static data to calculate a report."""


@dataclass(slots=True)
class _Cost:
    resources: dict[str, int] = field(default_factory=dict)
    character_exp: int = 0
    weapon_exp: int = 0
    gold: int = 0

    def add_resource(self, item_id: Any, count: Any) -> None:
        key = _text(item_id)
        amount = _int(count) or 0
        if key and amount:
            if key == "item_gold":
                self.gold += amount
                return
            self.resources[key] = self.resources.get(key, 0) + amount

    def add(self, other: _Cost | None) -> None:
        if other is None:
            return
        for item_id, count in other.resources.items():
            self.add_resource(item_id, count)
        self.character_exp += other.character_exp
        self.weapon_exp += other.weapon_exp
        self.gold += other.gold

    def copy(self) -> _Cost:
        return _Cost(dict(self.resources), self.character_exp, self.weapon_exp, self.gold)


@dataclass(frozen=True, slots=True)
class _NodeCost:
    node_id: str
    node_type: int = 0
    break_stage: int = 0
    index: int = 0
    level: int = 0
    cost: _Cost = field(default_factory=_Cost)


@dataclass(frozen=True, slots=True)
class _CharacterSpec:
    char_id: str
    break_nodes: Mapping[str, _NodeCost]
    nodes: Mapping[str, _NodeCost]
    skill_groups: Mapping[str, str]
    skill_rows: Mapping[str, tuple[tuple[int, _Cost], ...]]
    name: str = ""


@dataclass(frozen=True, slots=True)
class _WeaponSpec:
    weapon_id: str
    level_costs: Mapping[int, tuple[int, int]]
    breakthrough_costs: tuple[tuple[int, _Cost], ...]


@dataclass(frozen=True, slots=True)
class _ItemMeta:
    item_id: str
    name: str
    icon_url: str = ""


@dataclass(frozen=True, slots=True)
class _Recipe:
    output_count: int
    ingredients: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class InvestmentCatalog:
    version: str
    characters: Mapping[str, _CharacterSpec]
    weapons: Mapping[str, _WeaponSpec]
    items: Mapping[str, _ItemMeta]
    char_level_costs: Mapping[int, tuple[int, int]]
    resource_rates: Mapping[str, Fraction]
    character_exp_rate: Fraction | None
    weapon_exp_rate: Fraction | None
    gold_rate: Fraction | None

    def stamina_for(self, cost: _Cost) -> float:
        value = Fraction(0, 1)
        for item_id, count in cost.resources.items():
            if item_id == "item_gold":
                continue
            rate = self.resource_rates.get(item_id)
            if rate is not None:
                value += rate * count
        if self.character_exp_rate is not None:
            value += self.character_exp_rate * cost.character_exp
        if self.weapon_exp_rate is not None:
            value += self.weapon_exp_rate * cost.weapon_exp
        if self.gold_rate is not None:
            value += self.gold_rate * cost.gold
        return round(float(value), 1)


@dataclass(slots=True)
class _OperatorResult:
    operator_id: str
    name: str
    portrait_url: str
    rarity: int
    body: _Cost
    skills: _Cost
    weapon: _Cost
    missing: list[str]
    covered: int
    expected: int
    weapon_present: bool


_catalog_cache: InvestmentCatalog | None = None
_catalog_lock = asyncio.Lock()


async def fetch_account_investment_catalog() -> InvestmentCatalog:
    """Load and compact the current AKEData cost tables.

    The raw HTTP responses are cached by ``utils.http_client``.  This second
    cache avoids reparsing the large tables for every account query and is
    invalidated automatically when AKEData changes its revision.
    """
    global _catalog_cache
    try:
        manifest = await fetch_akedata_manifest()
    except Exception as exc:  # pragma: no cover - exercised by integration failures
        raise InvestmentDataUnavailable("AKEData 版本清单暂时不可用") from exc
    latest = _text(manifest.get("latest"))
    if not latest:
        raise InvestmentDataUnavailable("AKEData 未返回当前版本")
    if _catalog_cache is not None and _catalog_cache.version == latest:
        return _catalog_cache

    async with _catalog_lock:
        if _catalog_cache is not None and _catalog_cache.version == latest:
            return _catalog_cache
        version_entry = next(
            (
                item
                for item in manifest.get("versions") or ()
                if isinstance(item, Mapping) and _text(item.get("id")) == latest
            ),
            None,
        )
        table_cfg = _text((version_entry or {}).get("tableCfgPath")).strip("/")
        if not table_cfg:
            raise InvestmentDataUnavailable(f"AKEData 版本缺少 TableCfg：{latest}")

        names = (
            "CharGrowthTable",
            "CharLevelUpTable",
            "CharBreakNodeTable",
            "WeaponBasicTable",
            "WeaponUpgradeTemplateTable",
            "WeaponUpgradeTemplateSumTable",
            "WeaponBreakThroughTemplateTable",
            "ItemTable",
            "DungeonSeriesTable",
            "DungeonTable",
            "RewardTable",
            "ExpItemDataMap",
            "FactoryManualCraftTable",
            "I18nTextTable_CN",
        )
        try:
            values = await asyncio.gather(
                *(
                    _get(
                        f"/{table_cfg}/{name}.json",
                        max_bytes=_I18N_MAX_BYTES if name == "I18nTextTable_CN" else (
                            _RECIPE_MAX_BYTES if name == "FactoryManualCraftTable" else _TABLE_MAX_BYTES
                        ),
                    )
                    for name in names
                )
            )
        except Exception as exc:  # pragma: no cover - exercised by integration failures
            raise InvestmentDataUnavailable("AKEData 养成表暂时不可用") from exc

        tables = dict(zip(names, values, strict=True))
        try:
            catalog = _build_catalog(tables, latest)
        except Exception as exc:  # malformed upstream tables should not escape as a 500
            raise InvestmentDataUnavailable("AKEData 养成表结构异常") from exc
        _catalog_cache = catalog
        return catalog


def build_account_investment_view(
    detail: Mapping[str, Any],
    *,
    uid: str,
    nickname: str = "",
    server_name: str = "",
    catalog: InvestmentCatalog,
    name_map: AccountDetailNameMap | None = None,
) -> AccountInvestmentView:
    """Calculate current, visible account investment from a card/detail payload."""
    detail = detail if isinstance(detail, Mapping) else {}
    base = _mapping(detail.get("base"))
    operators: list[_OperatorResult] = []
    missing: list[str] = []
    covered = 0
    expected = 0
    equipped_weapon_count = 0
    all_cost = _Cost()
    category_costs = {
        "character_level": _Cost(),
        "character_growth": _Cost(),
        "skills": _Cost(),
        "weapon_level": _Cost(),
        "weapon_breakthrough": _Cost(),
    }

    for raw in _sequence(detail.get("chars")):
        if not isinstance(raw, Mapping):
            continue
        result = _operator_investment(raw, catalog, name_map or AccountDetailNameMap())
        operators.append(result)
        covered += result.covered
        expected += result.expected
        missing.extend(result.missing)
        if result.weapon_present:
            equipped_weapon_count += 1
        all_cost.add(result.body)
        all_cost.add(result.skills)
        all_cost.add(result.weapon)
        _split_operator_cost(raw, catalog, result, category_costs)

    operators.sort(key=lambda item: (-catalog.stamina_for(_sum_cost(item.body, item.skills, item.weapon)), item.name))
    contributions = tuple(
        InvestmentContributionView(
            operator_id=item.operator_id,
            name=item.name,
            portrait_url=item.portrait_url,
            rarity=item.rarity,
            body_stamina=catalog.stamina_for(item.body),
            skill_stamina=catalog.stamina_for(item.skills),
            weapon_stamina=catalog.stamina_for(item.weapon),
            missing=tuple(dict.fromkeys(item.missing)),
            exact_total_stamina=catalog.stamina_for(_sum_cost(item.body, item.skills, item.weapon)),
        )
        for item in operators[:10]
    )

    categories = tuple(
        InvestmentCategoryView(key, label, catalog.stamina_for(category_costs[key]))
        for key, label in (
            ("character_level", "干员升级"),
            ("character_growth", "干员突破 / 成长节点"),
            ("skills", "技能升级"),
            ("weapon_level", "武器升级"),
            ("weapon_breakthrough", "武器突破"),
        )
    )
    resources = _resource_views(all_cost, catalog)
    if all_cost.character_exp and catalog.character_exp_rate is None:
        missing.append("干员经验理智换算")
    if all_cost.weapon_exp and catalog.weapon_exp_rate is None:
        missing.append("武器经验理智换算")
    if all_cost.gold and catalog.gold_rate is None:
        missing.append("折金票理智换算")
    missing = list(dict.fromkeys(missing))
    return AccountInvestmentView(
        nickname=_text(base.get("name")) or nickname or "未知管理员",
        uid=_text(uid),
        server_name=server_label(server_name or _text(base.get("serverName"))),
        saved_at=format_timestamp(_int(base.get("saveTime")) or 0),
        source_revision=catalog.version,
        operator_count=len(operators),
        equipped_weapon_count=equipped_weapon_count,
        character_exp=all_cost.character_exp,
        weapon_exp=all_cost.weapon_exp,
        gold=all_cost.gold,
        stamina=catalog.stamina_for(all_cost),
        categories=categories,
        resources=resources,
        contributions=contributions,
        covered_components=covered,
        expected_components=expected,
        missing=tuple(missing),
    )


def _build_catalog(tables: Mapping[str, Any], version: str) -> InvestmentCatalog:
    critical_tables = (
        "CharGrowthTable",
        "CharLevelUpTable",
        "CharBreakNodeTable",
        "WeaponBasicTable",
        "WeaponBreakThroughTemplateTable",
        "ItemTable",
        "DungeonSeriesTable",
        "DungeonTable",
        "RewardTable",
    )
    if any(not isinstance(tables.get(name), Mapping) or not tables.get(name) for name in critical_tables):
        raise InvestmentDataUnavailable("AKEData 核心养成表为空")
    if not (
        isinstance(tables.get("WeaponUpgradeTemplateSumTable"), Mapping)
        and tables.get("WeaponUpgradeTemplateSumTable")
    ) and not (
        isinstance(tables.get("WeaponUpgradeTemplateTable"), Mapping)
        and tables.get("WeaponUpgradeTemplateTable")
    ):
        raise InvestmentDataUnavailable("AKEData 武器升级表为空")
    translations = _mapping(tables.get("I18nTextTable_CN"))
    items = _build_items(tables.get("ItemTable"), translations)
    characters = _build_characters(
        tables.get("CharGrowthTable"),
        tables.get("CharBreakNodeTable"),
        translations,
    )
    char_level_costs = _build_char_level_costs(tables.get("CharLevelUpTable"))
    weapons = _build_weapons(
        tables.get("WeaponBasicTable"),
        tables.get("WeaponUpgradeTemplateTable"),
        tables.get("WeaponUpgradeTemplateSumTable"),
        tables.get("WeaponBreakThroughTemplateTable"),
    )
    rates = _build_resource_rates(
        tables.get("DungeonTable"),
        tables.get("RewardTable"),
        tables.get("ExpItemDataMap"),
        tables.get("FactoryManualCraftTable"),
        tables.get("DungeonSeriesTable"),
        translations,
    )
    return InvestmentCatalog(
        version=version,
        characters=characters,
        weapons=weapons,
        items=items,
        char_level_costs=char_level_costs,
        resource_rates=rates[0],
        character_exp_rate=rates[1],
        weapon_exp_rate=rates[2],
        gold_rate=rates[3],
    )


def _build_items(value: Any, translations: Mapping[str, Any]) -> dict[str, _ItemMeta]:
    result: dict[str, _ItemMeta] = {}
    for key, row in _rows(value):
        item_id = _text(row.get("id")) or key
        if not item_id:
            continue
        icon_id = _text(row.get("iconId")) or item_id
        result[item_id] = _ItemMeta(
            item_id,
            _localized(translations, row.get("name")) or item_id,
            f"{AKEDATA_SPRITES}/itemiconbig/{icon_id}.png",
        )
    return result


def _build_characters(
    value: Any,
    break_node_table: Any = None,
    translations: Mapping[str, Any] | None = None,
) -> dict[str, _CharacterSpec]:
    equipment_break_stages = {
        _text(row.get("nodeId")) or key: _int(row.get("breakStage")) or 0
        for key, row in _rows(break_node_table)
        if (_text(row.get("nodeId")) or key).startswith("equipBreak")
    }
    result: dict[str, _CharacterSpec] = {}
    variant_groups: dict[tuple[str, ...], list[_CharacterSpec]] = {}
    for key, row in _rows(value):
        char_id = _text(row.get("charId")) or key
        if not char_id:
            continue
        break_nodes = {
            node_id: _node_from_row(node_id, node, default_type=_int(node.get("nodeType")) or 1)
            for node_id, node in _rows(row.get("charBreakCostMap"))
        }
        nodes = {
            node_id: _node_from_row(node_id, node, default_type=_int(node.get("nodeType")) or 0)
            for node_id, node in _rows(row.get("talentNodeMap"))
        }
        for node_id, node in tuple(nodes.items()):
            break_stage = equipment_break_stages.get(node_id)
            if break_stage is not None:
                nodes[node_id] = _NodeCost(
                    node.node_id,
                    node.node_type,
                    break_stage,
                    node.index,
                    node.level,
                    node.cost,
                )
        # The official account payload identifies AKEData entities by the
        # MD5 of their canonical id (for example md5("chr_1")). Keep
        # canonical ids as the source of truth, but expose the same spec
        # under the payload form so all calculation paths use one lookup.
        for node_id, node in tuple(nodes.items()):
            for candidate in (node_id, node.node_id):
                if candidate:
                    nodes.setdefault(_id_alias(candidate), node)
                    # ``SpaceshipSkillTable`` exposes the user-facing base
                    # skill as spaceship_skill_<char>_<slot>_<level>, while
                    # the material/gold cost lives on the corresponding
                    # factory growth node fac_<char>_<slot-1>_<level> in
                    # CharGrowthTable.  Expose the former as an alias of the
                    # latter so both account payload fields resolve to one
                    # canonical cost row and are de-duplicated when summed.
                    spaceship_id = _spaceship_skill_from_factory_node(candidate)
                    if spaceship_id:
                        nodes.setdefault(spaceship_id, node)
                        nodes.setdefault(_id_alias(spaceship_id), node)
        skill_groups: dict[str, str] = {}
        for group_key, group in _rows(row.get("skillGroupMap")):
            group_id = _text(group.get("skillGroupId")) or group_key
            for skill_id in _sequence(group.get("skillIdList")):
                skill_key = _text(skill_id)
                if skill_key:
                    skill_groups[skill_key] = group_id
                    skill_groups.setdefault(_id_alias(skill_key), group_id)
            skill_groups[group_id] = group_id
            skill_groups.setdefault(_id_alias(group_id), group_id)
        skill_rows: dict[str, list[tuple[int, _Cost]]] = {}
        for raw in _sequence(row.get("skillLevelUp")):
            if not isinstance(raw, Mapping):
                continue
            group_id = _text(raw.get("skillGroupId"))
            level = _int(raw.get("level")) or 0
            if not group_id or level <= 0:
                continue
            cost = _cost_from_items(raw.get("itemBundle"), gold=_int(raw.get("goldCost")) or 0)
            skill_rows.setdefault(group_id, []).append((level, cost))
        spec = _CharacterSpec(
            char_id,
            break_nodes,
            nodes,
            skill_groups,
            {group: tuple(sorted(rows, key=lambda item: item[0])) for group, rows in skill_rows.items()},
            _localized(translations or {}, row.get("name"))
            or _text(row.get("engName")),
        )
        result[char_id] = spec
        result.setdefault(_id_alias(char_id), spec)
        variant_key = (
            _text(row.get("engName")).casefold(),
            _text(row.get("defaultWeaponId")),
            _text(row.get("charTypeId")),
            str(_semantic_int(row.get("rarity")) or ""),
            str(_semantic_int(row.get("weaponType")) or ""),
        )
        if variant_key[0]:
            variant_groups.setdefault(variant_key, []).append(spec)
    _link_variant_specs(variant_groups)
    return result


def _link_variant_specs(groups: Mapping[tuple[str, ...], list[_CharacterSpec]]) -> None:
    """Link equivalent character variants used by different account payloads.

    The two Endministrator records are a known example: the account can use
    chr_9000_endmin for charData.id while its talent and skill ids still carry
    the chr_0003_endminf prefix. Their costs are identical, so we safely
    expose the sibling ids on the selected spec after comparing each
    node/skill cost.
    """
    for siblings in groups.values():
        if len(siblings) < 2:
            continue
        for target in siblings:
            target_nodes = {
                _variant_suffix(target.char_id, node.node_id): node
                for node in _canonical_nodes(target.nodes)
                if _variant_suffix(target.char_id, node.node_id) is not None
            }
            target_skills = {
                _variant_suffix(target.char_id, key): (key, group_id)
                for key, group_id in _canonical_skill_groups(target.skill_groups)
                if _variant_suffix(target.char_id, key) is not None
            }
            for source in siblings:
                if source is target:
                    continue
                for source_node in _canonical_nodes(source.nodes):
                    suffix = _variant_suffix(source.char_id, source_node.node_id)
                    if suffix is None:
                        continue
                    target_node = target_nodes.get(suffix)
                    if target_node is None or target_node.cost != source_node.cost:
                        continue
                    target.nodes.setdefault(source_node.node_id, target_node)
                    target.nodes.setdefault(_id_alias(source_node.node_id), target_node)
                for source_key, source_group in _canonical_skill_groups(source.skill_groups):
                    suffix = _variant_suffix(source.char_id, source_key)
                    if suffix is None:
                        continue
                    target_skill = target_skills.get(suffix)
                    if target_skill is None:
                        continue
                    _, target_group = target_skill
                    if source.skill_rows.get(source_group) != target.skill_rows.get(target_group):
                        continue
                    target.skill_groups.setdefault(source_key, target_group)
                    target.skill_groups.setdefault(_id_alias(source_key), target_group)


def _canonical_nodes(nodes: Mapping[str, _NodeCost]) -> tuple[_NodeCost, ...]:
    seen: set[str] = set()
    result: list[_NodeCost] = []
    for node in nodes.values():
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        result.append(node)
    return tuple(result)


def _canonical_skill_groups(groups: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for key, group_id in groups.items():
        if len(key) == 32 or key in seen:
            continue
        seen.add(key)
        result.append((key, group_id))
    return tuple(result)


def _variant_suffix(char_id: str, value: str) -> str | None:
    for marker, prefix in (
        ("char:", f"{char_id}_"),
        ("factory:", f"fac_{char_id}_"),
        ("spaceship:", f"spaceship_skill_{char_id}_"),
    ):
        if value.startswith(prefix):
            return f"{marker}{value[len(prefix):]}"
    if value.startswith("charBreak") or value.startswith("equipBreak"):
        return f"global:{value}"
    return None


def _spaceship_skill_from_factory_node(node_id: str) -> str:
    """Map an AKE factory growth node to its displayed spaceship skill id."""
    value = _text(node_id)
    if not value.startswith("fac_chr_"):
        return ""
    try:
        prefix, index_text, level_text = value.rsplit("_", 2)
        index = int(index_text)
        level = int(level_text)
    except (TypeError, ValueError):
        return ""
    if index < 0 or level <= 0:
        return ""
    return f"spaceship_skill_{prefix[4:]}_{index + 1}_{level}"


def _build_char_level_costs(value: Any) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for key, row in _rows(value):
        level = _int(key)
        if level is None:
            continue
        result[level] = (_int(row.get("exp")) or 0, _int(row.get("gold")) or 0)
    return result


def _build_weapons(
    basic_value: Any,
    upgrade_value: Any,
    upgrade_sum_value: Any,
    breakthrough_value: Any,
) -> dict[str, _WeaponSpec]:
    upgrade_rows = {key: _sequence(row.get("list")) for key, row in _rows(upgrade_value)}
    upgrade_sums = {key: _sequence(row.get("list")) for key, row in _rows(upgrade_sum_value)}
    breakthroughs = {
        key: _sequence(row.get("list")) for key, row in _rows(breakthrough_value)
    }
    result: dict[str, _WeaponSpec] = {}
    for key, row in _rows(basic_value):
        weapon_id = _text(row.get("weaponId")) or key
        if not weapon_id:
            continue
        template = _text(row.get("levelTemplateId"))
        curve_rows = upgrade_sums.get(template) or upgrade_rows.get(template) or ()
        level_costs: dict[int, tuple[int, int]] = {}
        running_exp = 0
        running_gold = 0
        for raw in curve_rows:
            if not isinstance(raw, Mapping):
                continue
            level = _int(raw.get("weaponLv")) or 0
            if level <= 0:
                continue
            if "lvUpExpSum" in raw or "lvUpGoldSum" in raw:
                level_costs[level] = (_int(raw.get("lvUpExpSum")) or 0, _int(raw.get("lvUpGoldSum")) or 0)
            else:
                running_exp += _int(raw.get("lvUpExp")) or 0
                running_gold += _int(raw.get("lvUpGold")) or 0
                level_costs[level] = (running_exp, running_gold)
        break_costs: list[tuple[int, _Cost]] = []
        for raw in breakthroughs.get(_text(row.get("breakthroughTemplateId")), ()):
            if not isinstance(raw, Mapping):
                continue
            show_level = _int(raw.get("breakthroughShowLv")) or 0
            if show_level <= 0:
                continue
            cost = _cost_from_items(
                raw.get("breakItemList"),
                gold=_int(raw.get("breakthroughGold")) or 0,
            )
            break_costs.append((show_level, cost))
        spec = _WeaponSpec(weapon_id, level_costs, tuple(break_costs))
        result[weapon_id] = spec
        result.setdefault(_id_alias(weapon_id), spec)
    return result


def _build_resource_rates(
    dungeon_value: Any,
    reward_value: Any,
    exp_value: Any,
    recipe_value: Any,
    series_value: Any = None,
    translations: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Fraction], Fraction | None, Fraction | None, Fraction | None]:
    rewards = {key: row for key, row in _rows(reward_value)}
    exp_items = {key: row for key, row in _rows(exp_value)}
    translations = translations or {}
    resource_rates: dict[str, Fraction] = {}
    best_char_exp: Fraction | None = None
    best_weapon_exp: Fraction | None = None
    for _, dungeon in _rows(dungeon_value):
        category = _text(dungeon.get("dungeonCategory"))
        if category not in {"dungeon_resource", "dungeon_ss", "dungeon_bossrush"}:
            continue
        # AKEData stores ordinary resource stages in costStamina/rewardId,
        # while RE-Crisis repeatable rewards use the hunterMode* pair.  The
        # fixed Protocol Space high-tier cultivation stages are different:
        # their repeatable reward mode is the DungeonTable hunterMode* pair,
        # and their RewardTable item count is a zero placeholder.  Keep the
        # source kind so only that fixed stage gets the six-item override.
        reward_sources: list[tuple[int, str, bool]] = []
        stamina = _int(dungeon.get("costStamina")) or 0
        reward_id = _text(dungeon.get("rewardId"))
        if stamina > 0 and reward_id:
            reward_sources.append((stamina, reward_id, False))
        hunter_stamina = _int(dungeon.get("hunterModeCostStamina")) or 0
        hunter_reward_id = _text(dungeon.get("hunterModeRewardId"))
        if hunter_stamina > 0 and hunter_reward_id:
            fixed_protocol = category == "dungeon_ss"
            if not any(
                (hunter_stamina, hunter_reward_id, fixed_protocol)
                == (row_stamina, row_reward, row_fixed_protocol)
                for row_stamina, row_reward, row_fixed_protocol in reward_sources
            ):
                reward_sources.append((hunter_stamina, hunter_reward_id, fixed_protocol))
        for stamina, reward_id, fixed_protocol in reward_sources:
            reward = rewards.get(reward_id)
            if not reward:
                continue
            bundles: dict[str, int] = {}
            for item in _sequence(reward.get("itemBundles")):
                if not isinstance(item, Mapping):
                    continue
                item_id = _text(item.get("id"))
                count = _int(item.get("count")) or 0
                # The fixed high-tier cultivation stage intentionally
                # publishes count=0 in the static reward row.  Its actual
                # fixed run reward is six copies of the stage's own material.
                if (
                    count <= 0
                    and fixed_protocol
                    and category == "dungeon_ss"
                    and item_id in _PROTOCOL_SPECIALIZATION_ITEMS
                ):
                    count = _PROTOCOL_SPECIALIZATION_FIXED_COUNT
                if item_id and count > 0:
                    bundles[item_id] = bundles.get(item_id, 0) + count
            char_exp = 0
            weapon_exp = 0
            for item_id, count in bundles.items():
                if item_id == "item_adventureexp":
                    continue
                exp_gain = _int(_mapping(exp_items.get(item_id)).get("expGain")) or 0
                exp_type = _int(_mapping(exp_items.get(item_id)).get("expType"))
                if exp_gain > 0 and (
                    item_id.startswith("item_expcard_stage1_") or exp_type == 0
                ):
                    char_exp += exp_gain * count
                    continue
                if exp_gain > 0 and (
                    item_id.startswith("item_weapon_expcard_")
                    or item_id.startswith("item_expcard_stage2_")
                    or exp_type in {1, 2}
                ):
                    weapon_exp += exp_gain * count
                    continue
                rate = Fraction(stamina, count)
                old = resource_rates.get(item_id)
                if old is None or rate < old:
                    resource_rates[item_id] = rate
            if char_exp:
                rate = Fraction(stamina, char_exp)
                if best_char_exp is None or rate < best_char_exp:
                    best_char_exp = rate
            if weapon_exp:
                rate = Fraction(stamina, weapon_exp)
                if best_weapon_exp is None or rate < best_weapon_exp:
                    best_weapon_exp = rate

    recipes = _build_safe_recipes(recipe_value)
    memo: dict[str, Fraction | None] = {}

    def resolve(item_id: str, stack: frozenset[str] = frozenset()) -> Fraction | None:
        if item_id in resource_rates:
            return resource_rates[item_id]
        if item_id in memo:
            return memo[item_id]
        if item_id in stack:
            return None
        best: Fraction | None = None
        for recipe in recipes.get(item_id, ()):
            total = Fraction(0, 1)
            possible = True
            for ingredient_id, count in recipe.ingredients:
                rate = resolve(ingredient_id, stack | {item_id})
                if rate is None:
                    possible = False
                    break
                total += rate * count
            if possible:
                candidate = total / recipe.output_count
                if best is None or candidate < best:
                    best = candidate
        memo[item_id] = best
        return best

    for item_id in list(recipes):
        rate = resolve(item_id)
        if rate is not None:
            resource_rates[item_id] = rate
    gold_rate = resource_rates.get("item_gold")
    return resource_rates, best_char_exp, best_weapon_exp, gold_rate


def _build_safe_recipes(value: Any) -> dict[str, tuple[_Recipe, ...]]:
    result: dict[str, list[_Recipe]] = {}
    for _, row in _rows(value):
        ingredients: list[tuple[str, int]] = []
        outputs = []
        for item in _sequence(row.get("ingredients")):
            if not isinstance(item, Mapping):
                ingredients = []
                break
            item_id = _text(item.get("id"))
            count = _int(item.get("count")) or 0
            if not item_id or count <= 0 or item_id in _BANNED_RECIPE_ITEMS:
                ingredients = []
                break
            ingredients.append((item_id, count))
        for item in _sequence(row.get("outcomes")):
            if isinstance(item, Mapping):
                item_id = _text(item.get("id"))
                count = _int(item.get("count")) or 0
                if item_id and count > 0:
                    outputs.append((item_id, count))
        if not ingredients or len(outputs) != 1:
            continue
        output_id, output_count = outputs[0]
        if output_id in _BANNED_RECIPE_ITEMS:
            continue
        result.setdefault(output_id, []).append(_Recipe(output_count, tuple(ingredients)))
    return {key: tuple(value) for key, value in result.items()}


def _operator_investment(
    raw: Mapping[str, Any], catalog: InvestmentCatalog, name_map: AccountDetailNameMap
) -> _OperatorResult:
    char_data = _mapping(raw.get("charData"))
    char_id = next(
        (
            _text(value)
            for value in (
                char_data.get("id"),
                char_data.get("charId"),
                raw.get("charId"),
                raw.get("wikiItemId"),
                raw.get("id"),
            )
            if _text(value)
        ),
        "",
    )
    spec = catalog.characters.get(char_id)
    name = _mapped_name(
        name_map.character_names,
        (char_data.get("id"), char_data.get("charId"), raw.get("charId"), raw.get("id")),
        (spec.name if spec is not None else "")
        or _text(char_data.get("name"))
        or _text(raw.get("name"))
        or char_id
        or "未知干员",
    )
    portrait = (
        _text(char_data.get("avatarSqUrl"))
        or _text(char_data.get("avatarRtUrl"))
        or _text(char_data.get("avatarUrl"))
        or _text(char_data.get("iconUrl"))
        or _text(char_data.get("illustrationUrl"))
    )
    rarity = _semantic_int(char_data.get("rarity")) or 0
    missing: list[str] = []
    covered = 0
    expected = 0
    body = _Cost()
    skills = _Cost()
    weapon = _Cost()
    if spec is None:
        expected += 1
        missing.append(f"{name}：干员成长表")
    else:
        expected += 1
        covered += 1
        level = max(1, _int(raw.get("level")) or 1)
        for transition in range(1, level):
            costs = catalog.char_level_costs.get(transition)
            if costs is None:
                expected += 1
                missing.append(f"{name}：等级 {transition}→{transition + 1}")
                continue
            exp, gold = costs
            body.character_exp += exp
            body.gold += gold
        evolve_phase = max(0, _int(raw.get("evolvePhase")) or 0)
        seen_nodes: set[str] = set()
        for node_id, node in spec.break_nodes.items():
            if node.break_stage <= evolve_phase and node.node_id.startswith("charBreak"):
                expected += 1
                covered += 1
                body.add(node.cost)
                seen_nodes.add(node_id)
                seen_nodes.add(node.node_id)

        talent = _mapping(raw.get("talent"))
        node_targets: list[str] = []
        node_targets.extend(
            node_id
            for value in _sequence(talent.get("attrNodes"))
            if (node_id := _node_id(value)) and not _is_default_node(node_id)
        )
        for field_name in ("latestPassiveSkillNodes", "latestFactorySkillNodes", "latestSpaceshipSkillNodes"):
            for value in _sequence(talent.get(field_name)):
                node_id = _node_id(value)
                if not node_id or _is_default_node(node_id):
                    continue
                if node_id not in spec.nodes:
                    expected += 1
                    missing.append(_missing_node_label(name, node_id))
                    continue
                node_targets.extend(_expand_latest_node(spec, node_id))
        latest_break = _node_id(talent.get("latestBreakNode"))
        if _is_default_node(latest_break):
            latest_break = ""
        if latest_break and latest_break.startswith("charBreak"):
            # Character break rows are driven by evolvePhase and are counted
            # above; latestBreakNode is not authoritative for them.
            latest_break = ""
        if latest_break:
            if latest_break not in spec.nodes:
                expected += 1
                missing.append(_missing_node_label(name, latest_break))
            else:
                latest_node = spec.nodes[latest_break]
                if latest_node.node_type == 2:
                    node_targets.extend(
                        node.node_id
                        for node in spec.nodes.values()
                        if node.node_type == 2 and node.break_stage <= latest_node.break_stage
                    )
                # A nodeType=1 latestBreakNode is one of the character
                # breakthrough rows already included above.  It must not be
                # counted as a second growth component.
        for node_id in node_targets:
            if not node_id or _is_default_node(node_id) or node_id in seen_nodes:
                continue
            node = spec.nodes.get(node_id)
            if node is None:
                expected += 1
                missing.append(_missing_node_label(name, node_id))
                continue
            if node.node_type == 1:
                # Character breakthrough costs are already accounted for above.
                seen_nodes.add(node_id)
                seen_nodes.add(node.node_id)
                continue
            expected += 1
            covered += 1
            body.add(node.cost)
            seen_nodes.add(node_id)
            seen_nodes.add(node.node_id)

        for skill_id, learned in _mapping(raw.get("userSkills")).items():
            level_value = _int(_mapping(learned).get("level")) or 0
            if level_value <= 1:
                continue
            expected += 1
            group_id = spec.skill_groups.get(_text(skill_id)) or spec.skill_groups.get(
                _text(_mapping(learned).get("skillId"))
            )
            if not group_id or group_id not in spec.skill_rows:
                missing.append(f"{name}：技能 {_text(skill_id)}")
                continue
            covered += 1
            skill_costs = dict(spec.skill_rows[group_id])
            for skill_level in range(2, level_value + 1):
                cost = skill_costs.get(skill_level)
                if cost is None:
                    expected += 1
                    missing.append(f"{name}：技能 {_text(skill_id)} Lv{skill_level}")
                    continue
                skills.add(cost)

    weapon_raw = _mapping(raw.get("weapon"))
    weapon_present = bool(weapon_raw)
    if weapon_present:
        weapon_data = _mapping(weapon_raw.get("weaponData"))
        weapon_id = next(
            (
                _text(value)
                for value in (
                weapon_data.get("id"),
                weapon_data.get("weaponId"),
                weapon_data.get("wikiItemId"),
                weapon_raw.get("wikiItemId"),
                weapon_raw.get("id"),
                weapon_raw.get("weaponId"),
                )
                if _text(value)
            ),
            "",
        )
        expected += 1
        weapon_spec = catalog.weapons.get(weapon_id)
        if weapon_spec is None:
            missing.append(f"{name}：武器 {_text(weapon_data.get('name')) or weapon_id or '未知'}")
        else:
            covered += 1
            level = max(1, _int(weapon_raw.get("level")) or 1)
            if weapon_spec.level_costs:
                level_row = max(
                    (entry for entry in weapon_spec.level_costs if entry <= level),
                    default=min(weapon_spec.level_costs),
                )
                exp, gold = weapon_spec.level_costs[level_row]
                weapon.weapon_exp += exp
                weapon.gold += gold
                if level > 1 and level not in weapon_spec.level_costs:
                    expected += 1
                    missing.append(f"{name}：武器等级 {level}")
            elif level > 1:
                expected += 1
                missing.append(f"{name}：武器等级 {level}")
            breakthrough_level = max(0, _int(weapon_raw.get("breakthroughLevel")) or 0)
            required_breaks = [
                (show_level, cost)
                for show_level, cost in weapon_spec.breakthrough_costs
                if show_level <= breakthrough_level
            ]
            if breakthrough_level > 0 and not required_breaks:
                expected += 1
                missing.append(f"{name}：武器突破 {breakthrough_level}")
            else:
                for show_level, cost in required_breaks:
                    weapon.add(cost)
    return _OperatorResult(char_id, name, portrait, rarity, body, skills, weapon, missing, covered, expected, weapon_present)


def _split_operator_cost(
    raw: Mapping[str, Any],
    catalog: InvestmentCatalog,
    result: _OperatorResult,
    target: dict[str, _Cost],
) -> None:
    # Rebuild the same five buckets from the compact per-entity result.  The
    # exact material totals remain on the result; this pass only feeds the
    # category strip and intentionally keeps the operation deterministic.
    char_data = _mapping(raw.get("charData"))
    char_id = next(
        (
            _text(value)
            for value in (
                char_data.get("id"),
                char_data.get("charId"),
                raw.get("charId"),
                raw.get("wikiItemId"),
            )
            if _text(value)
        ),
        "",
    )
    spec = catalog.characters.get(char_id)
    if spec is not None:
        level = max(1, _int(raw.get("level")) or 1)
        level_cost = _Cost()
        for transition, (exp, gold) in catalog.char_level_costs.items():
            if transition < level:
                level_cost.character_exp += exp
                level_cost.gold += gold
        target["character_level"].add(level_cost)
        growth = result.body.copy()
        growth.character_exp -= level_cost.character_exp
        growth.gold -= level_cost.gold
        # Keep negative values out if an upstream table is malformed.
        growth.character_exp = max(0, growth.character_exp)
        growth.gold = max(0, growth.gold)
        target["character_growth"].add(growth)
        target["skills"].add(result.skills)
    weapon_data = _mapping(_mapping(raw.get("weapon")).get("weaponData"))
    weapon_id = next(
        (
            _text(value)
            for value in (
                weapon_data.get("id"),
                weapon_data.get("weaponId"),
                weapon_data.get("wikiItemId"),
                _mapping(raw.get("weapon")).get("wikiItemId"),
                _mapping(raw.get("weapon")).get("id"),
            )
            if _text(value)
        ),
        "",
    )
    weapon_spec = catalog.weapons.get(weapon_id)
    if weapon_spec is None:
        return
    weapon_level_cost = _Cost()
    level = max(1, _int(_mapping(raw.get("weapon")).get("level")) or 1)
    if weapon_spec.level_costs:
        row = max((entry for entry in weapon_spec.level_costs if entry <= level), default=min(weapon_spec.level_costs))
        exp, gold = weapon_spec.level_costs[row]
        weapon_level_cost.weapon_exp = exp
        weapon_level_cost.gold = gold
    target["weapon_level"].add(weapon_level_cost)
    weapon_break = result.weapon.copy()
    weapon_break.weapon_exp -= weapon_level_cost.weapon_exp
    weapon_break.gold -= weapon_level_cost.gold
    weapon_break.weapon_exp = max(0, weapon_break.weapon_exp)
    weapon_break.gold = max(0, weapon_break.gold)
    target["weapon_breakthrough"].add(weapon_break)


def _resource_views(cost: _Cost, catalog: InvestmentCatalog) -> tuple[InvestmentResourceView, ...]:
    result: list[InvestmentResourceView] = []
    raw_resources = dict(cost.resources)
    if cost.gold > 0:
        raw_resources["item_gold"] = raw_resources.get("item_gold", 0) + cost.gold
    for item_id, count in sorted(raw_resources.items(), key=lambda item: (item[0] != "item_gold", item[0])):
        if count <= 0:
            continue
        meta = catalog.items.get(item_id)
        result.append(
            InvestmentResourceView(
                item_id=item_id,
                name=meta.name if meta else item_id,
                count=count,
                icon_url=meta.icon_url if meta else f"{AKEDATA_SPRITES}/itemiconbig/{item_id}.png",
                stamina_cost=(float(catalog.resource_rates[item_id]) if item_id in catalog.resource_rates else None),
            )
        )
    return tuple(result)


def _sum_cost(*costs: _Cost) -> _Cost:
    result = _Cost()
    for cost in costs:
        result.add(cost)
    return result


def _node_from_row(node_id: str, row: Mapping[str, Any], *, default_type: int) -> _NodeCost:
    node_type = _int(row.get("nodeType")) or default_type
    info_by_type = {
        3: _mapping(row.get("attributeNodeInfo")),
        4: _mapping(row.get("passiveSkillNodeInfo")),
        5: _mapping(row.get("factorySkillNodeInfo")),
        6: _mapping(row.get("spaceshipSkillNodeInfo")),
    }
    info = info_by_type.get(node_type) or {}
    if not info:
        info = next(
            (
                _mapping(row.get(field_name))
                for field_name in (
                    "attributeNodeInfo",
                    "passiveSkillNodeInfo",
                    "factorySkillNodeInfo",
                    "spaceshipSkillNodeInfo",
                )
                if _mapping(row.get(field_name))
            ),
            {},
        )
    return _NodeCost(
        node_id=_text(row.get("nodeId")) or node_id,
        node_type=node_type,
        break_stage=_int(info.get("breakStage")) or _int(row.get("breakStage")) or 0,
        index=_int(info.get("index")) or 0,
        level=_int(info.get("level")) or 0,
        cost=_cost_from_items(row.get("requiredItem")),
    )


def _expand_latest_node(spec: _CharacterSpec, node_id: str) -> tuple[str, ...]:
    latest = spec.nodes.get(node_id)
    if latest is None:
        return ()
    if latest.level <= 0:
        return (latest.node_id,)
    candidates = [
        node
        for node in spec.nodes.values()
        if node.node_type == latest.node_type and node.index == latest.index and node.level > 0 and node.level <= latest.level
    ]
    candidates.sort(key=lambda node: (node.level, node.node_id))
    seen: set[str] = set()
    expanded: list[str] = []
    for node in candidates:
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        expanded.append(node.node_id)
    return tuple(expanded) or (latest.node_id,)


def _cost_from_items(items: Any, *, gold: int = 0) -> _Cost:
    cost = _Cost(gold=gold)
    for item in _sequence(items):
        if isinstance(item, Mapping):
            cost.add_resource(item.get("id"), item.get("count"))
    return cost


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple((str(key), row) for key, row in value.items() if isinstance(row, Mapping))


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _text(value: Any) -> str:
    return localized_text(value)


def _mapped_name(names: Mapping[str, str], identifiers: tuple[Any, ...], fallback: Any) -> str:
    for identifier in identifiers:
        key = _text(identifier)
        if key:
            mapped = _text(names.get(key))
            if mapped:
                return mapped
    return _text(fallback)


def _id_alias(value: str) -> str:
    """Return the account-payload id form used by the official API."""
    return md5(value.encode("utf-8")).hexdigest() if value else ""


def _int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Mapping):
            for candidate in (
                value.get("value"),
                value.get("level"),
                value.get("id"),
                value.get("key"),
            ):
                parsed = _int(candidate)
                if parsed is not None:
                    return parsed
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _semantic_int(value: Any) -> int | None:
    if isinstance(value, Mapping):
        return _int(value.get("value")) or _int(value.get("key"))
    return _int(value)


def _node_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("id") or value.get("nodeId") or value.get("key"))
    return _text(value)


def _is_default_node(node_id: str) -> bool:
    return node_id.startswith("default_")


def _missing_node_label(name: str, node_id: str) -> str:
    if node_id.startswith("spaceship_skill_"):
        return f"{name}：成长节点 {node_id}（AKEData 当前无成本行）"
    return f"{name}：成长节点 {node_id}"


def _localized(translations: Mapping[str, Any], value: Any) -> str:
    return localized_text(value, translations=translations)
