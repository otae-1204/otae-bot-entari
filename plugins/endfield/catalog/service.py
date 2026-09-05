"""Catalog data fetching, source fallback and cache orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import (
    replace,
)
from typing import (
    Any,
    Sequence,
)
from loguru import (
    logger,
)
from otae_bot.infrastructure.cache import AsyncTTLCache
from ..providers.warfarin import (
    WarfarinAPIError,
    WarfarinClient,
)
from ..providers.akedata import (
    AKEDATA_ICON_BASE,
    fetch_akedata_achievement_table,
    fetch_akedata_manifest,
    game_version_label,
    pick_previous_game_version,
)
from ..providers.assets import (
    apply_akedata_growth_icons,
    apply_operator_asset_donor,
    apply_weapon_asset_donor,
    operator_needs_asset_donor,
    weapon_needs_asset_donor,
)
from .commands import (
    AMBIGUITY_MARGIN,
    CLEAR_SCORE,
    EquipmentAttributeFilter,
    score_candidate,
)
from .models import (
    EquipmentCatalogView,
    EquipmentView,
    LoadoutView,
    MedalBaselineView,
    MedalDiffView,
    MedalItemView,
    MedalMissingView,
    MedalSnapshotView,
    OperatorCatalogView,
    OperatorView,
    WeaponCatalogView,
    WeaponView,
)
from ..providers.registry import (
    source_order,
)
from .views.constants import (
    _MIN_AKEDATA_MEDAL_COMPLETENESS,
)
from .views.common import (
    _alias_key as _alias_key,
    _build_fz_term_styles as _build_fz_term_styles,
    _build_term_styles as _build_term_styles,
    _case_insensitive_get as _case_insensitive_get,
    _clean_fz_rich_text as _clean_fz_rich_text,
    _equipment_stat_is_percent as _equipment_stat_is_percent,
    _eval_fz_template_expr as _eval_fz_template_expr,
    _first_text as _first_text,
    _first_value as _first_value,
    _format_equipment_stat as _format_equipment_stat,
    _format_fz_template as _format_fz_template,
    _format_plain_number as _format_plain_number,
    _format_template_value as _format_template_value,
    _fz_asset_raw_url as _fz_asset_raw_url,
    _fz_effect_title as _fz_effect_title,
    _fz_hero_meta_value as _fz_hero_meta_value,
    _fz_overview_entries as _fz_overview_entries,
    _fz_rich_text_color as _fz_rich_text_color,
    _fz_rich_text_links as _fz_rich_text_links,
    _fz_template_attrs as _fz_template_attrs,
    _fz_template_operand as _fz_template_operand,
    _fz_weapon_id as _fz_weapon_id,
    _level_label as _level_label,
    _looks_like_term as _looks_like_term,
    _normalized_value_map as _normalized_value_map,
    _ordered_fz_levels as _ordered_fz_levels,
    _rich_text_visual as _rich_text_visual,
    _strip_title_prefix as _strip_title_prefix,
    _substitute_fz_placeholders as _substitute_fz_placeholders,
    _term_names_from_entry as _term_names_from_entry,
    _term_suffix_from_name as _term_suffix_from_name,
    _text_list as _text_list,
    _to_float as _to_float,
    _to_int as _to_int,
    _unwrap_fz_list as _unwrap_fz_list,
    _weapon_name as _weapon_name,
    clean_text as clean_text,
    skill_icon_url as skill_icon_url,
    static_resource_url as static_resource_url,
)
from .views.equipment import (
    _apply_fz_equipment_catalog_item_detail as _apply_fz_equipment_catalog_item_detail,
    _apply_fz_equipment_catalog_item_details as _apply_fz_equipment_catalog_item_details,
    _apply_fz_equipment_catalog_suit_effects as _apply_fz_equipment_catalog_suit_effects,
    _equipment_acquisition as _equipment_acquisition,
    _equipment_attribute_slot_matches as _equipment_attribute_slot_matches,
    _equipment_attributes_match as _equipment_attributes_match,
    _equipment_group_item_is_wildcard as _equipment_group_item_is_wildcard,
    _fz_equipment_ability_attribute as _fz_equipment_ability_attribute,
    _fz_equipment_attribute_slots as _fz_equipment_attribute_slots,
    _fz_equipment_detail_level as _fz_equipment_detail_level,
    _fz_equipment_roster_attributes as _fz_equipment_roster_attributes,
    _fz_equipment_roster_entries as _fz_equipment_roster_entries,
    _normalize_equipment_group_name as _normalize_equipment_group_name,
    build_fz_equipment_attribute_catalog_view as build_fz_equipment_attribute_catalog_view,
    build_fz_equipment_catalog_view as build_fz_equipment_catalog_view,
    build_fz_equipment_view as build_fz_equipment_view,
)
from .views.loadout import (
    _apply_loadout_description as _apply_loadout_description,
    _apply_loadout_equipment_row as _apply_loadout_equipment_row,
    _apply_loadout_operator_effects as _apply_loadout_operator_effects,
    _apply_loadout_operator_growth as _apply_loadout_operator_growth,
    _apply_loadout_set_effect as _apply_loadout_set_effect,
    _apply_loadout_weapon_skills as _apply_loadout_weapon_skills,
    _build_loadout_advanced_stats as _build_loadout_advanced_stats,
    _build_loadout_equipment_stat as _build_loadout_equipment_stat,
    _build_loadout_status_effects as _build_loadout_status_effects,
    _format_loadout_percent as _format_loadout_percent,
    _format_status_number as _format_status_number,
    _format_status_percent as _format_status_percent,
    _fz_main_sub_attributes as _fz_main_sub_attributes,
    _fz_operator_attributes_at_level as _fz_operator_attributes_at_level,
    _fz_operator_break_stage_at_level as _fz_operator_break_stage_at_level,
    _fz_weapon_attack_at_level as _fz_weapon_attack_at_level,
    _latest_loadout_operator_items as _latest_loadout_operator_items,
    _loadout_attribute_key as _loadout_attribute_key,
    _loadout_clause_is_triggered as _loadout_clause_is_triggered,
    _loadout_effect_target as _loadout_effect_target,
    _loadout_equipment_forge_levels as _loadout_equipment_forge_levels,
    _loadout_operator_potentials as _loadout_operator_potentials,
    _loadout_status_effect_bonus as _loadout_status_effect_bonus,
    _loadout_status_names as _loadout_status_names,
    _make_loadout_status_levels as _make_loadout_status_levels,
    build_fz_loadout_view as build_fz_loadout_view,
    format_status_quick_calc as format_status_quick_calc,
)
from .views.medals import (
    _derive_medal_levels as _derive_medal_levels,
    _fz_medal_entry_attrs as _fz_medal_entry_attrs,
    _i18n_text as _i18n_text,
    _norm_medal_name as _norm_medal_name,
    _parse_player_medal_progress as _parse_player_medal_progress,
    _tier_text as _tier_text,
    build_akedata_medal_snapshot as build_akedata_medal_snapshot,
    build_fz_medal_item as build_fz_medal_item,
    build_fz_medal_snapshot_view as build_fz_medal_snapshot_view,
)
from .views.operators import (
    _all_skill_records_for_group as _all_skill_records_for_group,
    _attribute_placeholder as _attribute_placeholder,
    _build_extra_levels as _build_extra_levels,
    _build_fz_effects as _build_fz_effects,
    _build_fz_operator_skills as _build_fz_operator_skills,
    _build_fz_skill_form_descriptions as _build_fz_skill_form_descriptions,
    _build_fz_skill_levels as _build_fz_skill_levels,
    _build_level as _build_level,
    _build_potentials as _build_potentials,
    _build_skill_form_descriptions as _build_skill_form_descriptions,
    _build_skills as _build_skills,
    _build_talents as _build_talents,
    _drop_generic_fz_metrics as _drop_generic_fz_metrics,
    _effect_sort_key as _effect_sort_key,
    _effect_values as _effect_values,
    _extract_species as _extract_species,
    _extract_values as _extract_values,
    _format_blackboard_value as _format_blackboard_value,
    _format_effect_desc as _format_effect_desc,
    _format_fz_metric_value as _format_fz_metric_value,
    _format_metric_number as _format_metric_number,
    _format_percent as _format_percent,
    _format_skill_desc as _format_skill_desc,
    _fz_archive_species as _fz_archive_species,
    _fz_icon_url as _fz_icon_url,
    _fz_param_is_percent as _fz_param_is_percent,
    _fz_param_table_values as _fz_param_table_values,
    _fz_param_value_at as _fz_param_value_at,
    _fz_skill_category as _fz_skill_category,
    _fz_skill_condition_names as _fz_skill_condition_names,
    _fz_species_info as _fz_species_info,
    _iter_fz_archive_text as _iter_fz_archive_text,
    _map_fz_param_label as _map_fz_param_label,
    _map_fz_skill_values as _map_fz_skill_values,
    _merge_additional_skill_levels as _merge_additional_skill_levels,
    _metric_label_from_key as _metric_label_from_key,
    _normalize_metric_name as _normalize_metric_name,
    _potential_icon_url as _potential_icon_url,
    _primary_skill_desc as _primary_skill_desc,
    _record_by_level as _record_by_level,
    _select_fz_levels as _select_fz_levels,
    _skill_group_category as _skill_group_category,
    _skill_group_meta as _skill_group_meta,
    _skill_records_for_group as _skill_records_for_group,
    _skill_template_values as _skill_template_values,
    _store_effect_value as _store_effect_value,
    _talent_node_meta as _talent_node_meta,
    _talent_title as _talent_title,
    _template_expression_value as _template_expression_value,
    build_fz_operator_catalog_view as build_fz_operator_catalog_view,
    build_fz_operator_view as build_fz_operator_view,
    build_operator_view as build_operator_view,
)
from .views.weapons import (
    _blackboard_values as _blackboard_values,
    _build_warfarin_weapon_skills as _build_warfarin_weapon_skills,
    _build_weapon_skill as _build_weapon_skill,
    _match_weapon_record as _match_weapon_record,
    _unique_names as _unique_names,
    _warfarin_rich_text_styles as _warfarin_rich_text_styles,
    _warfarin_weapon_icon_url as _warfarin_weapon_icon_url,
    _warfarin_weapon_max_atk as _warfarin_weapon_max_atk,
    _weapon_slug as _weapon_slug,
    build_fz_weapon_catalog_view as build_fz_weapon_catalog_view,
    build_warfarin_weapon_view as build_warfarin_weapon_view,
    build_weapon_view as build_weapon_view,
)


class _IncompleteWeaponRelations(Exception):
    def __init__(self, index: dict[str, tuple[str, ...]]):
        self.index = index


class EndfieldService:
    def __init__(self, client: WarfarinClient):
        self.client = client
        self._char_growth_table: dict[str, Any] | None = None
        self._char_growth_version = ""
        self._weapon_relations: AsyncTTLCache[str, dict[str, tuple[str, ...]]] = AsyncTTLCache(
            ttl_seconds=60.0, max_bytes=1024 * 1024, max_entries=16,
            sizeof=lambda value: len(json.dumps(value, ensure_ascii=False).encode()),
        )

    async def clear_query_caches(self) -> int:
        return await self._weapon_relations.clear()

    async def get_operator_view(self, query: str) -> OperatorView | None:
        primary: OperatorView | None = None
        used_source = ""
        for source in source_order("operator"):
            try:
                if source == "fz":
                    view = await self.get_operator_view_from_fz(query)
                elif source == "warfarin":
                    view = await self.get_operator_view_from_warfarin(query)
                else:
                    continue
            except (WarfarinAPIError, ValueError, KeyError, TypeError):
                continue
            if view is not None:
                primary = view
                used_source = source
                break
        if primary is None:
            return None
        if used_source != "fz":
            await self._supplement_operator_assets(primary, query)
        return primary

    async def get_operator_view_from_warfarin(self, query: str) -> OperatorView | None:
        query = _strip_title_prefix(query, "干员/")
        slug = await self.find_operator_slug(query)
        if not slug:
            return None
        raw = await self.client.operator_detail(slug)
        return build_operator_view(raw)

    async def get_operator_view_from_fz(self, query: str) -> OperatorView | None:
        title = await self.find_fz_operator_title(query)
        if not title:
            return None
        raw, richtext = await _fz_article_and_richtext(self.client, title)
        view = build_fz_operator_view(raw, richtext)
        if not view.operator_id:
            view.operator_id = await self._lookup_fz_operator_id(view.name)
        await self._supplement_operator_assets(view, query)
        return view

    async def get_weapon_view(self, query: str) -> WeaponView | None:
        primary: WeaponView | None = None
        for source in source_order("weapon"):
            try:
                if source == "fz":
                    view = await self.get_weapon_view_from_fz(query)
                elif source == "warfarin":
                    view = await self.get_weapon_view_from_warfarin(query)
                else:
                    continue
            except (WarfarinAPIError, ValueError, KeyError, TypeError):
                continue
            if view is not None:
                primary = view
                break
        if primary is None:
            return None
        await self._supplement_weapon_assets(primary, query)
        return primary

    async def get_weapon_view_from_fz(self, query: str) -> WeaponView | None:
        title = await self.find_weapon_title(query)
        if not title:
            return None
        raw, richtext = await _fz_article_and_richtext(self.client, title)
        view = build_weapon_view(raw, richtext)
        view.operator_names = await self.find_weapon_operator_names(view)
        if not view.weapon_id:
            view.weapon_id = await self._lookup_fz_weapon_id(view.name)
        return view

    async def get_weapon_view_from_warfarin(self, query: str) -> WeaponView | None:
        query = _strip_title_prefix(query, "武器/")
        slug = await self.find_weapon_slug(query)
        if not slug:
            return None
        raw = await self.client.weapon_detail(slug)
        view = build_warfarin_weapon_view(raw)
        view.operator_names = await self.find_weapon_operator_names(view)
        return view

    async def get_equipment_view(self, query: str) -> EquipmentView | None:
        for source in source_order("equipment"):
            try:
                if source == "fz":
                    view = await self.get_equipment_view_from_fz(query)
                else:
                    continue
            except (WarfarinAPIError, ValueError, KeyError, TypeError):
                continue
            if view is not None:
                return view
        return None

    async def get_equipment_view_from_fz(self, query: str) -> EquipmentView | None:
        title = await self.find_equipment_title(query)
        if not title:
            return None
        raw, richtext = await _fz_article_and_richtext(self.client, title)
        return build_fz_equipment_view(raw, richtext)

    async def get_loadout_view(
        self,
        operator_title: str,
        weapon_title: str,
        equipment: list[tuple[str, int, tuple[tuple[int, int], ...]]],
        *,
        operator_level: int = 90,
        operator_potential: int = 5,
        weapon_level: int = 90,
        weapon_potential: int = 5,
        weapon_skill_levels: tuple[tuple[int, int], ...] = (),
    ) -> LoadoutView:
        titles = [operator_title, weapon_title, *(title for title, _, _ in equipment)]
        raw_results = await asyncio.gather(
            *(self.client.fz_article_by_title(title) for title in titles),
            self.client.fz_game_richtext(),
            self._get_loadout_operator_growth(operator_title),
            return_exceptions=True,
        )
        raws = raw_results[:-2]
        for raw in raws:
            if isinstance(raw, Exception):
                raise raw
        richtext_result = raw_results[-2]
        richtext = richtext_result if isinstance(richtext_result, dict) else {}
        growth_result = raw_results[-1]
        operator_growth = growth_result if isinstance(growth_result, dict) else {}
        equipment_raws = [(raw, equipment[index][1], equipment[index][2]) for index, raw in enumerate(raws[2:])]
        view = build_fz_loadout_view(
            raws[0],
            raws[1],
            equipment_raws,
            operator_level=operator_level,
            operator_potential=operator_potential,
            weapon_level=weapon_level,
            weapon_potential=weapon_potential,
            weapon_skill_levels=weapon_skill_levels,
            richtext=richtext,
            operator_growth=operator_growth,
        )
        if not view.operator_id:
            view.operator_id = await self._lookup_fz_operator_id(view.operator_name)
        if not view.weapon_id:
            view.weapon_id = await self._lookup_fz_weapon_id(view.weapon_name)
        return view

    async def _get_loadout_operator_growth(self, operator_title: str) -> dict[str, Any]:
        name = _strip_title_prefix(operator_title, "干员/")
        try:
            slug = await self.find_operator_slug(name)
            if not slug:
                return {}
            return await self.client.operator_detail(slug)
        except (WarfarinAPIError, ValueError, KeyError, TypeError):
            return {}

    async def get_recommended_weapon_title(self, operator_title: str) -> str:
        raw = await self.client.fz_article_by_title(operator_title)
        attrs = _fz_template_attrs(raw)
        weapons = attrs.get("weapons") if isinstance(attrs.get("weapons"), dict) else {}
        for group_name in ("group1", "group2"):
            for item in weapons.get(group_name) or []:
                if not isinstance(item, dict):
                    continue
                name = _first_text(item, "name", "title")
                if name:
                    return name if name.startswith("武器/") else f"武器/{name}"
        raise ValueError("FZ 干员数据没有推荐武器")

    async def get_equipment_catalog_view(
        self,
        group_name: str = "",
        rarity_filter: str = "gold",
        *,
        include_details: bool = True,
    ) -> EquipmentCatalogView:
        raw = await self.client.fz_article_by_title("装备")
        view = build_fz_equipment_catalog_view(raw, group_name, rarity_filter)
        # Name matching needs only the directory, not the suit effects from
        # one extra article per group. Full catalogue rendering is unchanged.
        if not include_details:
            return view
        if group_name:
            titles = [item.title for group in view.groups for item in group.items]
            detail_raws = await self._fz_equipment_details(titles)
            _apply_fz_equipment_catalog_item_details(view, detail_raws)
            _apply_fz_equipment_catalog_suit_effects(view, list(detail_raws.values()))
            return view
        representative_titles = [
            group.items[0].title
            for group in view.groups
            if group.items and group.name != "独立装备套组"
        ]
        detail_results = await asyncio.gather(
            *(self.client.fz_article_by_title(title) for title in representative_titles),
            return_exceptions=True,
        )
        _apply_fz_equipment_catalog_suit_effects(
            view,
            [result for result in detail_results if isinstance(result, dict)],
        )
        return view

    async def get_equipment_attribute_catalog_view(
        self,
        filters: Sequence[EquipmentAttributeFilter],
        rarity_filter: str = "gold",
    ) -> EquipmentCatalogView:
        if not filters:
            raise ValueError("FZ equipment attribute filter is empty")
        raw = await self.client.fz_article_by_title("装备")
        catalog = build_fz_equipment_catalog_view(raw, "", rarity_filter)
        titles = [item.title for group in catalog.groups for item in group.items]
        detail_raws = await self._fz_equipment_details(titles)
        view = build_fz_equipment_attribute_catalog_view(raw, detail_raws, filters, rarity_filter)
        # 套组效果对同组每件装备都一样，交全部详情让筛掉的那件也能补上说明。
        _apply_fz_equipment_catalog_suit_effects(view, list(detail_raws.values()))
        return view

    async def _fz_equipment_details(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        """按属性筛选要用到每一件装备的词条，所以详情必须齐。

        少一件详情就少一条结果，这里不做静默降级：失败的重试一轮（成功的已进
        HTTP 缓存，重试只补拉失败的那几件），仍失败就抛出去按数据源故障回复，
        而不是给出一份看起来完整、实际被截断的清单。
        """
        pending = list(titles)
        details: dict[str, dict[str, Any]] = {}
        error: Exception | None = None
        for _ in range(2):
            if not pending:
                break
            results = await asyncio.gather(
                *(self.client.fz_article_by_title(title) for title in pending),
                return_exceptions=True,
            )
            retry: list[str] = []
            for title, result in zip(pending, results):
                if isinstance(result, dict):
                    details[title] = result
                    continue
                retry.append(title)
                if isinstance(result, Exception):
                    error = result
            pending = retry
        if pending:
            raise error or WarfarinAPIError(f"FZ 装备详情获取失败：{pending[0]}")
        return details

    async def get_operator_catalog_view(
        self,
        element: str = "",
        profession: str = "",
    ) -> OperatorCatalogView:
        raw = await self.client.fz_article_by_title("干员")
        return build_fz_operator_catalog_view(raw, element, profession)

    async def get_weapon_catalog_view(self, weapon_type: str = "") -> WeaponCatalogView:
        raw = await self.client.fz_article_by_title("武器")
        return build_fz_weapon_catalog_view(raw, weapon_type)

    async def fetch_medal_snapshot_fz(self, *, fetched_at: int | None = None) -> MedalSnapshotView:
        """抓取 FZ 蚀刻章全量快照：roster（名称发现）→ 逐个详情（等级/镀层）。

        并发由 ``fetch_json`` 内置信号量限流；roster 仅含名称，等级等信息在详情里，
        故需对每枚奖章抓一次单件条目（首版约 140 次请求，结果落本地快照缓存）。
        """
        roster_raw = await self.client.fz_article_by_title("蚀刻章")
        titles = [
            _first_text(entry, "title")
            for entry in _fz_overview_entries(roster_raw)
            if _first_text(entry, "title")
        ]
        # FZ 单件页偶发超时/断连，对失败的 title 重试最多 3 轮把丢页补齐（避免快照残缺）
        detail_raws: list[dict[str, Any]] = []
        pending = list(titles)
        for _ in range(3):
            if not pending:
                break
            results = await asyncio.gather(
                *(self.client.fz_article_by_title(title) for title in pending),
                return_exceptions=True,
            )
            pending = [title for title, result in zip(pending, results) if isinstance(result, Exception)]
            detail_raws.extend(result for result in results if isinstance(result, dict))
        return build_fz_medal_snapshot_view(
            roster_raw,
            detail_raws,
            fetched_at=fetched_at or int(time.time()),
        )

    async def fetch_medal_snapshot_akedata(self, *, fetched_at: int | None = None) -> MedalSnapshotView:
        """抓取 AKEData 全量奖章快照（权威主源）。

        manifest → latest 版本 → AchievementTable + AchievementTypeTable + I18nTextTable_CN，
        聚合成快照。AKEData 的 ``achv_*`` id 与森空岛 hex 经 md5 关联，故 ``medal_id`` 直接用
        achv_id。详见 ``docs/skland_medal_id_mapping.md``。
        """
        from ..providers.akedata import fetch_akedata_medal_tables

        achievement, type_table, i18n, version = await fetch_akedata_medal_tables()
        if not isinstance(achievement, dict) or not achievement:
            raise ValueError("AKEData AchievementTable 为空")
        if not isinstance(type_table, dict) or not type_table:
            raise ValueError("AKEData AchievementTypeTable 为空")
        if not isinstance(i18n, dict) or not i18n:
            raise ValueError("AKEData I18nTextTable_CN 为空")

        expected_count = sum(1 for entry in achievement.values() if isinstance(entry, dict))
        snapshot = build_akedata_medal_snapshot(
            achievement,
            type_table,
            i18n,
            fetched_at=fetched_at or int(time.time()),
            version_label=game_version_label(version),
        )
        if snapshot.total_count <= 0:
            raise ValueError("AKEData 蚀刻章快照为空")
        # A manifest can become visible before all table/i18n files are consistent.
        # Do not replace a known-good snapshot with a silently truncated one.
        if expected_count and snapshot.total_count < math.ceil(
            expected_count * _MIN_AKEDATA_MEDAL_COMPLETENESS
        ):
            raise ValueError(
                f"AKEData 蚀刻章快照不完整：{snapshot.total_count}/{expected_count}"
            )
        return snapshot

    async def fetch_akedata_baseline(self, *, fetched_at: int | None = None) -> MedalBaselineView | None:
        """抓 akedata「上一游戏版本」基线（版本对比的 previous 方，源和源）。

        manifest → pick_previous_game_version → 抓其 AchievementTable（仅取 achv_id 集合）。
        无更早游戏版本时返回 None；抓取失败会抛出异常，由调用方保留已有基线。
        """
        try:
            manifest = await fetch_akedata_manifest()
            prev = pick_previous_game_version(manifest)
            if not prev or not prev.get("tableCfgPath"):
                return None
            table = await fetch_akedata_achievement_table(str(prev["tableCfgPath"]).lstrip("/"))
            if not isinstance(table, dict) or not table:
                raise ValueError("AKEData 历史 AchievementTable 为空")
            ids = [aid for aid, entry in table.items() if isinstance(entry, dict)]
            if not ids:
                raise ValueError("AKEData 历史蚀刻章基线为空")
            return MedalBaselineView(
                version=game_version_label(str(prev.get("id") or "")),
                version_id=str(prev.get("id") or ""),
                ids=ids,
                fetched_at=fetched_at or int(time.time()),
            )
        except Exception as exc:
            logger.warning(f"[endfield] medal baseline fetch failed: {exc}")
            raise

    def build_medal_diff(
        self,
        current: MedalSnapshotView,
        baseline: MedalBaselineView | None,
    ) -> MedalDiffView:
        """对比 current 快照与上一版本基线筛出新增奖章（id 集合差集）。

        baseline 为 None（无更早版本）时无对比基线，new_medals 为空。
        双方同为 akedata 源数据，口径一致；previous_version 用 baseline 的 major.minor。
        """
        if baseline is None:
            return MedalDiffView(current=current, previous_version="", new_medals=[])
        baseline_ids = set(baseline.ids)
        new_medals = [
            medal
            for medal in current.medals
            if medal.medal_id and medal.medal_id not in baseline_ids
        ]
        return MedalDiffView(
            current=current,
            previous_version=baseline.version,
            new_medals=new_medals,
        )

    def build_medal_missing_view(
        self,
        raw_progress: dict[str, Any],
        snapshot: MedalSnapshotView,
        *,
        nickname: str,
        uid: str,
        server_name: str,
        limit: int = 30,
    ) -> MedalMissingView:
        """F2：SDK 玩家进度 × 全量快照，得出未获得 / 未升满 / 未镀层。

        关联键：``md5(FZ.medal_id) == 森空岛 achievementData.id``（2026-07-28 实测 115/115），
        比按 name 关联可靠——不受命名滞后影响（如「武陵调度专家奖章·Ⅳ/·Ⅴ」撞名）。
        FZ 条目缺 ``achv_`` id 时回退按规范化 name（实测 FZ 单件档案均含 achv_ id，兜底基本不触发）。
        """
        progress_by_hex, progress_by_name = _parse_player_medal_progress(raw_progress)
        not_obtained: list[MedalItemView] = []
        not_maxed: list[MedalItemView] = []
        not_plated: list[MedalItemView] = []
        # 等级分布按账号已拥有奖章的「当前档位（颜色）」统计。
        # 森空岛 level 对 initLevel>1 的章有偏移（如「谷地调查者奖章」initLevel=2：银记 1、金记 2），
        # 实际档位 = skland level + initLevel - 1；否则会把 2→3 升级章误判。
        # 详见 docs/bugfix_medal_investigator_max_tier.md。AKEData max_level 本身正确。
        owned_level_counts: dict[int, int] = {}
        for medal in snapshot.medals:
            achv_id = medal.medal_id or ""
            info = (
                progress_by_hex.get(hashlib.md5(achv_id.encode()).hexdigest())
                if achv_id.startswith("achv_")
                else None
            )
            if info is None and medal.name:  # 兜底：FZ 条目无 achv_ id 时按 name
                info = progress_by_name.get(_norm_medal_name(medal.name))
            if info is None:
                init_lv = medal.init_level or 1
                not_obtained.append(replace(
                    medal,
                    icon_url=f"{AKEDATA_ICON_BASE}/{medal.medal_id}_lv{init_lv:02d}.png",
                ))
                continue
            offset = info.init_level - 1 if info.init_level > 0 else 0
            real_level = info.level + offset
            owned_level_counts[real_level] = owned_level_counts.get(real_level, 0) + 1
            if medal.can_be_upgraded and real_level < medal.max_level:
                target = real_level + 1
                not_maxed.append(replace(
                    medal,
                    icon_url=f"{AKEDATA_ICON_BASE}/{medal.medal_id}_lv{real_level:02d}.png",
                    description=_tier_text(medal.tier_desc, real_level, medal.description),
                    condition=_tier_text(medal.tier_cond, real_level, medal.condition),
                    next_description=_tier_text(medal.tier_desc, target),
                    next_condition=_tier_text(medal.tier_cond, target),
                    next_icon_url=f"{AKEDATA_ICON_BASE}/{medal.medal_id}_lv{target:02d}.png",
                ))
            if medal.can_be_plated and not info.plated:
                not_plated.append(replace(
                    medal,
                    description=_tier_text(medal.tier_desc, medal.max_level, medal.description),
                    condition=_tier_text(medal.tier_cond, medal.max_level),
                    next_description=_tier_text(medal.tier_desc, medal.max_level, medal.description),
                    next_condition=medal.plate_condition or "",
                    next_icon_url=info.plated_icon or "",
                ))
        not_obtained_count = len(not_obtained)
        not_maxed_count = len(not_maxed)
        not_plated_count = len(not_plated)
        owned_count = snapshot.total_count - not_obtained_count
        truncated = False
        if not_obtained_count + not_maxed_count + not_plated_count > limit:
            truncated = True
            per = max(1, limit // 3)
            not_obtained = not_obtained[:per]
            not_maxed = not_maxed[:per]
            not_plated = not_plated[:per]
        return MedalMissingView(
            nickname=nickname,
            uid=uid,
            server_name=server_name,
            snapshot_version=snapshot.version,
            total_count=snapshot.total_count,
            owned_count=owned_count,
            not_obtained=not_obtained,
            not_maxed=not_maxed,
            not_plated=not_plated,
            not_obtained_count=not_obtained_count,
            not_maxed_count=not_maxed_count,
            not_plated_count=not_plated_count,
            truncated=truncated,
            shown_count=len(not_obtained) + len(not_maxed) + len(not_plated),
            level_counts=owned_level_counts,
        )

    async def find_weapon_operator_names(self, view: WeaponView) -> list[str]:
        try:
            weapons_data, operators_data = await asyncio.gather(
                self.client.weapons(),
                self.client.operators(),
            )
            weapon = _match_weapon_record(view, weapons_data.get("data") or [])
            weapon_id = str((weapon or {}).get("id") or view.weapon_id).strip()
            weapon_type = str((weapon or {}).get("weaponType") or "").strip()
            if not weapon_id or not weapon_type:
                return []
            candidates = [
                item
                for item in operators_data.get("data") or []
                if str(item.get("weaponType") or "").strip() == weapon_type and item.get("slug")
            ]
        except Exception:
            return []
        key = hashlib.sha256(json.dumps(
            [weapon_type, candidates], sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        try:
            index = await self._weapon_relations.get_or_create(
                key, lambda: self._build_weapon_relation_index(candidates),
            )
        except _IncompleteWeaponRelations as exc:
            # Preserve the original partial-result behavior, but do not
            # cache a transiently incomplete index as authoritative.
            index = exc.index
        return list(index.get(weapon_id, ()))

    async def _build_weapon_relation_index(
        self, candidates: list[dict[str, Any]],
    ) -> dict[str, tuple[str, ...]]:
        details = await asyncio.gather(
            *(self.client.operator_detail(str(item["slug"])) for item in candidates),
            return_exceptions=True,
        )
        defaults: dict[str, list[str]] = {}
        recommendations_by_weapon: dict[str, list[str]] = {}
        incomplete = False
        for item, detail in zip(candidates, details):
            if isinstance(detail, Exception) or not isinstance(detail, dict):
                incomplete = True
                continue
            data = detail.get("data") or {}
            character = data.get("characterTable") or {}
            recommendations = data.get("charWpnRecommendTable") or {}
            name = _first_text(detail.get("meta") or {}, "name") or _first_text(item, "name")
            if not name:
                incomplete = True
                continue
            default_id = str(character.get("defaultWeaponId") or "").strip()
            if default_id:
                defaults.setdefault(default_id, []).append(name)
            recommended_ids = {
                str(candidate_id).strip()
                for key, values in recommendations.items()
                if str(key).startswith("weaponIds") and isinstance(values, list)
                for candidate_id in values
            }
            for identifier in recommended_ids:
                if identifier != default_id:
                    recommendations_by_weapon.setdefault(identifier, []).append(name)
        index = {
            identifier: tuple(_unique_names(defaults.get(identifier) or recommendations_by_weapon.get(identifier, [])))
            for identifier in defaults.keys() | recommendations_by_weapon.keys()
        }
        if incomplete:
            raise _IncompleteWeaponRelations(index)
        return index

    async def find_weapon_title(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query.startswith("武器/"):
            return query
        exact_title = f"武器/{query}"
        try:
            summaries = await self.client.fz_article_summaries("武器/")
        except Exception:
            return exact_title
        lowered = query.lower()
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if name == query or name.lower() == lowered:
                return title
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if query in name or lowered in name.lower():
                return title
        return exact_title

    async def find_equipment_title(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query.startswith("装备/"):
            return query
        exact_title = f"装备/{query}"
        try:
            summaries = await self.client.fz_article_summaries("装备/")
        except WarfarinAPIError:
            summaries = {}
        lowered = query.lower()
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if name == query or name.lower() == lowered:
                return title
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if query in name or lowered in name.lower():
                return title
        return exact_title

    async def find_fz_operator_title(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query.startswith("干员/"):
            return query
        exact_title = f"干员/{query}"
        try:
            summaries = await self.client.fz_article_summaries("干员/")
        except WarfarinAPIError:
            summaries = {}
        lowered = query.lower()
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if name == query or name.lower() == lowered:
                return title
        for item in summaries.get("articles") or []:
            title = str(item.get("title") or "")
            name = title.split("/", 1)[-1]
            if query in name or lowered in name.lower():
                return title
        try:
            search_data = await self.client.fz_search(query)
        except WarfarinAPIError:
            search_data = {}
        for item in search_data.get("hits") or []:
            title = str(item.get("title") or "")
            if title.startswith("干员/"):
                return title
        return exact_title

    async def find_operator_slug(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I):
            return query
        data = await self.client.search(query)
        for item in data.get("results") or []:
            if str(item.get("type") or "") == "operators" and item.get("slug"):
                return str(item["slug"])
        return await self._match_operator_by_name(query)

    async def _match_operator_by_name(self, query: str) -> str | None:
        data = await self.client.operators()
        return _best_slug_match(query, data.get("data") or [])

    async def find_weapon_slug(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I):
            return query
        data = await self.client.search(query)
        for item in data.get("results") or []:
            if str(item.get("type") or "") in {"weapons", "weapon"} and item.get("slug"):
                return str(item["slug"])
        return await self._match_weapon_by_name(query)

    async def _match_weapon_by_name(self, query: str) -> str | None:
        data = await self.client.weapons()
        return _best_slug_match(query, data.get("data") or [])

    async def _supplement_operator_assets(self, view: OperatorView, query: str) -> None:
        if not view.operator_id:
            view.operator_id = await self._lookup_fz_operator_id(view.name)
        growth = await self._akedata_char_growth(view.operator_id)
        if growth:
            apply_akedata_growth_icons(view, growth)
        if not operator_needs_asset_donor(view):
            return
        try:
            donor = await self.get_operator_view_from_warfarin(view.name or query)
        except (WarfarinAPIError, ValueError, KeyError, TypeError):
            return
        if donor is not None:
            apply_operator_asset_donor(view, donor)

    async def _akedata_char_growth(self, operator_id: str) -> dict[str, Any]:
        operator_id = str(operator_id or "").strip()
        if not operator_id:
            return {}
        table = await self._load_char_growth_table()
        row = table.get(operator_id)
        return row if isinstance(row, dict) else {}

    async def _load_char_growth_table(self) -> dict[str, Any]:
        try:
            manifest = await self.client.akedata_manifest()
            latest = str(manifest.get("latest") or "")
            path = ""
            for entry in manifest.get("versions") or []:
                if isinstance(entry, dict) and str(entry.get("id") or "") == latest:
                    path = str(entry.get("tableCfgPath") or "").strip("/")
                    break
        except (WarfarinAPIError, ValueError, TypeError, KeyError):
            return self._char_growth_table or {}
        if (
            latest
            and latest == self._char_growth_version
            and isinstance(self._char_growth_table, dict)
        ):
            return self._char_growth_table
        table: dict[str, Any] = {}
        try:
            if path:
                loaded = await self.client.akedata_table(path, "CharGrowthTable")
                if isinstance(loaded, dict):
                    table = loaded
        except (WarfarinAPIError, ValueError, TypeError, KeyError):
            return self._char_growth_table or {}
        self._char_growth_table = table
        self._char_growth_version = latest
        return table

    async def _supplement_weapon_assets(self, view: WeaponView, query: str) -> None:
        if not view.weapon_id:
            view.weapon_id = await self._lookup_fz_weapon_id(view.name)
        if not weapon_needs_asset_donor(view):
            return
        try:
            donor = await self.get_weapon_view_from_warfarin(view.name or query)
        except (WarfarinAPIError, ValueError, KeyError, TypeError):
            return
        if donor is not None:
            apply_weapon_asset_donor(view, donor)

    async def _lookup_fz_operator_id(self, name: str) -> str:
        name = clean_text(name)
        if not name:
            return ""
        try:
            catalog = build_fz_operator_catalog_view(await self.client.fz_article_by_title("干员"))
        except (WarfarinAPIError, ValueError, KeyError, TypeError):
            return ""
        for element in catalog.elements:
            for profession in element.professions:
                for item in profession.items:
                    if item.name == name or item.english_name == name:
                        return item.operator_id
        return ""

    async def _lookup_fz_weapon_id(self, name: str) -> str:
        name = clean_text(name)
        if not name:
            return ""
        try:
            catalog = build_fz_weapon_catalog_view(await self.client.fz_article_by_title("武器"))
        except (WarfarinAPIError, ValueError, KeyError, TypeError):
            return ""
        for group in catalog.groups:
            for item in group.items:
                if item.name == name or item.english_name == name:
                    return item.weapon_id
        return ""


async def _fz_article_and_richtext(client: WarfarinClient, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    article_result, richtext_result = await asyncio.gather(
        client.fz_article_by_title(title),
        client.fz_game_richtext(),
        return_exceptions=True,
    )
    if isinstance(article_result, Exception):
        raise article_result
    if isinstance(richtext_result, Exception):
        richtext = {}
    else:
        richtext = richtext_result
    return article_result, richtext


def _best_slug_match(query: str, records: list[dict[str, Any]]) -> str | None:
    scored: list[tuple[int, str]] = []
    for record in records:
        slug = str(record.get("slug") or "").strip()
        name = str(record.get("name") or "").strip()
        if not slug or not name:
            continue
        score = score_candidate(query, name, slug)
        if score >= CLEAR_SCORE:
            scored.append((score, slug))
    scored.sort(reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN:
        return None
    return scored[0][1]
