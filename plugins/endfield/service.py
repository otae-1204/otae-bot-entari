from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import replace
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from .client import WarfarinAPIError, WarfarinClient
from .akedata_client import (
    AKEDATA_ICON_BASE,
    fetch_akedata_achievement_table,
    fetch_akedata_manifest,
    game_version_label,
    pick_previous_game_version,
)
from .asset_urls import (
    apply_akedata_growth_icons,
    apply_operator_asset_donor,
    apply_weapon_asset_donor,
    element_icon_urls,
    item_icon_urls,
    operator_icon_urls,
    operator_needs_asset_donor,
    operator_portrait_urls,
    operator_round_icon_urls,
    profession_icon_urls,
    skill_icon_urls,
    static_sprite_url,
    weapon_needs_asset_donor,
    weapon_type_icon_urls,
)
from .commands import (
    AMBIGUITY_MARGIN,
    CLEAR_SCORE,
    EquipmentAttributeFilter,
    format_equipment_attribute_filters,
    score_candidate,
)
from .models import (
    EquipmentCatalogAttributeView,
    EquipmentCatalogGroupView,
    EquipmentCatalogItemView,
    EquipmentCatalogView,
    EquipmentPieceView,
    EquipmentStatView,
    EquipmentView,
    EffectView,
    LEVEL_COLUMNS,
    LoadoutEffectView,
    LoadoutEquipmentView,
    LoadoutPanelStatView,
    LoadoutStatusEffectView,
    LoadoutStatusLevelView,
    LoadoutView,
    MedalBaselineView,
    MedalDiffView,
    MedalItemView,
    MedalMissingView,
    MedalProgressView,
    MedalSnapshotView,
    OperatorCatalogElementView,
    OperatorCatalogItemView,
    OperatorCatalogProfessionView,
    OperatorCatalogView,
    OperatorView,
    SkillLevelView,
    SkillView,
    TermStyleView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponCatalogGroupView,
    WeaponCatalogItemView,
    WeaponCatalogView,
    WeaponView,
)
from .account_i18n import localized_text
from .sources import source_order
from .skill_metrics import fz_metric_replaces_generic


INDEPENDENT_EQUIPMENT_GROUP_NAMES = frozenset({"纾难装备组", "涉渊装备组"})
EQUIPMENT_ABILITY_ATTRIBUTES = {"Str": "力量", "Agi": "敏捷", "Wisd": "智识", "Will": "意志"}
EQUIPMENT_ABILITY_WILDCARD = "通用"
OPERATOR_ELEMENT_ORDER = {name: index for index, name in enumerate(("物理", "灼热", "电磁", "寒冷", "自然"))}
OPERATOR_PROFESSION_ORDER = {
    name: index for index, name in enumerate(("近卫", "术师", "突击", "先锋", "重装", "辅助"))
}
WEAPON_TYPE_ORDER = {
    name: index for index, name in enumerate(("单手剑", "双手剑", "施术单元", "长柄武器", "手铳"))
}
_MIN_AKEDATA_MEDAL_COMPLETENESS = 0.8
FZ_ASSET_HOST = "assets.fz.wiki"
WEAPON_OPTIONS = ("单手剑", "双手剑", "施术单元", "长枪", "手铳")

SKILL_CATEGORY_ORDER = {
    "普攻": 0,
    "战技": 1,
    "终结技": 2,
    "连携技": 3,
}

WEAPON_NAMES = {
    1: "单手剑",
    2: "双手剑",
    3: "施术单元",
    4: "长枪",
    5: "手铳",
}

TERM_SUFFIXES = (
    "附着",
    "异常",
    "伤害",
    "脆弱",
    "爆发",
    "增幅",
    "抗性",
    "击飞",
    "破防",
    "猛击",
    "倒地",
    "碎甲",
    "碎冰",
    "冻结",
    "燃烧",
    "导电",
    "腐蚀",
    "失衡",
    "消耗",
)

WARFARIN_METRIC_LABELS = {
    "atk_scale": "攻击倍率",
    "atk_scale_will": "阵诀·意伤害倍率",
    "atk_scale_wisd": "阵诀·智伤害倍率",
    "atk_scale_touch": "触碰伤害倍率",
    "atk_scale_boom": "爆发伤害倍率",
    "atk_scale_laser": "集束打击伤害倍率",
    "atk_scale_laser_will": "阵诀·意集束打击倍率",
    "atk_scale_laser1": "第一段集束打击倍率",
    "atk_scale_laser2": "第二段诀明伤害倍率",
    "poise": "失衡值",
    "poise_touch": "触碰失衡值",
    "poise_boom": "爆发失衡值",
    "poise_laser": "集束打击失衡值",
    "laser_count": "集束打击次数",
    "usp": "获得终结技能量",
    "atb": "技力",
    "duration": "持续时间（秒）",
    "duration2": "阵法持续时间（秒）",
    "duration_will": "阵诀·意持续时间（秒）",
    "duration_wisd": "阵诀·智持续时间（秒）",
    "spell_vul_per_will": "每点意志脆弱效果",
    "rate_pre": "基础脆弱效果",
    "atb_return_wisd": "阵诀·智技力返还",
    "max_spell_vul_will": "阵诀·意最大脆弱效果",
}

WARFARIN_PERCENT_METRIC_KEYS = {
    "spell_vul_per_will",
    "rate_pre",
    "max_spell_vul_will",
}


class EndfieldService:
    def __init__(self, client: WarfarinClient):
        self.client = client
        self._char_growth_table: dict[str, Any] | None = None
        self._char_growth_version = ""

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
    ) -> EquipmentCatalogView:
        raw = await self.client.fz_article_by_title("装备")
        view = build_fz_equipment_catalog_view(raw, group_name, rarity_filter)
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
        from .akedata_client import fetch_akedata_medal_tables

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
            details = await asyncio.gather(
                *(self.client.operator_detail(str(item["slug"])) for item in candidates),
                return_exceptions=True,
            )
        except Exception:
            return []

        default_names: list[str] = []
        recommended_names: list[str] = []
        for item, detail in zip(candidates, details):
            if isinstance(detail, Exception) or not isinstance(detail, dict):
                continue
            data = detail.get("data") or {}
            character = data.get("characterTable") or {}
            recommendations = data.get("charWpnRecommendTable") or {}
            name = _first_text(detail.get("meta") or {}, "name") or _first_text(item, "name")
            if not name:
                continue
            if str(character.get("defaultWeaponId") or "").strip() == weapon_id:
                default_names.append(name)
                continue
            recommended_ids = {
                str(candidate_id).strip()
                for key, values in recommendations.items()
                if str(key).startswith("weaponIds") and isinstance(values, list)
                for candidate_id in values
            }
            if weapon_id in recommended_ids:
                recommended_names.append(name)
        return _unique_names(default_names or recommended_names)

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


def build_operator_view(raw: dict[str, Any]) -> OperatorView:
    meta = raw.get("meta") or {}
    data = raw.get("data") or {}
    refs = raw.get("refs") or {}
    character = data.get("characterTable") or {}
    growth = data.get("charGrowthTable") or {}
    item_table = data.get("itemTable") or {}
    operator_id = str(meta.get("id") or character.get("charId") or "")

    profession_id = character.get("profession")
    profession_ref = (refs.get("charProfessionTable") or {}).get(str(profession_id), {})
    char_type_id = str(character.get("charTypeId") or "")
    type_ref = (refs.get("charTypeTable") or {}).get(char_type_id, {})
    tag_table = refs.get("tagDataTable") or {}

    tags: list[str] = []
    for tag_id in character.get("charBattleTagIds") or []:
        tag_name = str((tag_table.get(str(tag_id)) or {}).get("tagName") or "").strip()
        if tag_name:
            tags.append(tag_name)

    view = OperatorView(
        name=_first_text(meta, "name") or _first_text(character, "name"),
        slug=_first_text(meta, "slug"),
        operator_id=operator_id,
        english_name=_first_text(character, "engName"),
        rarity=int(character.get("rarity") or 0),
        profession=_first_text(profession_ref, "name") or "未知职业",
        damage_type=_first_text(type_ref, "name") or char_type_id or "未知属性",
        weapon_type=_weapon_name(character.get("weaponType"), item_table.get("desc")),
        species=_extract_species(character),
        tags=tags[:4],
        icon_url=(operator_icon_urls(operator_id) or ("",))[0],
        round_icon_url=(operator_round_icon_urls(operator_id) or ("",))[0],
        portrait_url=(operator_portrait_urls(operator_id) or ("",))[0],
        skills=_build_skills(data.get("skillPatchTable") or {}, growth.get("skillGroupMap") or {}),
        talents=_build_talents(
            data.get("potentialTalentEffectTable") or {},
            growth.get("talentNodeMap") or {},
        ),
        potentials=_build_potentials(
            data.get("characterPotentialTable") or {},
            data.get("potentialTalentEffectTable") or {},
        ),
        term_styles=_build_term_styles(refs),
        source_version=str(meta.get("version") or ""),
    )
    return view


def build_fz_operator_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> OperatorView:
    article = raw.get("article") or {}
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    skills = _build_fz_operator_skills(attrs.get("skills"))
    if not hero or not skills:
        raise ValueError("FZ operator article does not match the supported card schema")

    title = _first_text(article, "title")
    name = _first_text(hero, "name", "nameCn", "cnName", "title") or title.split("/", 1)[-1]
    if not name:
        raise ValueError("FZ operator article is missing name")

    rarity = _to_int(_first_value(hero, "rarity", "star", "stars"))
    species_label, species_value = _fz_species_info(attrs)
    return OperatorView(
        name=name,
        slug=title or name,
        operator_id=str(
            _first_value(hero, "id", "charId", "operatorId")
            or ((hero.get("meta") or {}) if isinstance(hero.get("meta"), dict) else {}).get("charId")
            or ""
        ),
        english_name=_first_text(hero, "nameEn", "englishName", "engName"),
        rarity=rarity,
        profession=_first_text(hero, "profession", "class", "job") or "未知职业",
        damage_type=_first_text(hero, "element", "damageType", "type") or "未知属性",
        weapon_type=_first_text(hero, "weaponType", "weapon") or "未知武器",
        species=species_value,
        species_label=species_label,
        tags=_text_list(_first_value(hero, "tags", "tagList"))[:4],
        icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "avatarUrl", "icon")),
        round_icon_url=_fz_asset_raw_url(_first_text(hero, "roundIconUrl", "avatarRoundUrl")),
        portrait_url=_fz_asset_raw_url(_first_text(hero, "portraitFile", "portraitUrl", "illustUrl", "imageUrl")),
        skills=skills,
        talents=_build_fz_effects(attrs.get("talents"), "talent"),
        potentials=_build_fz_effects(attrs.get("potentials"), "potential"),
        term_styles=_build_fz_term_styles(richtext or {}),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_fz_equipment_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> EquipmentView:
    article = raw.get("article") or {}
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    if not hero:
        raise ValueError("FZ equipment article does not match the supported card schema")

    title = _first_text(article, "title")
    name = _first_text(hero, "name", "title") or title.split("/", 1)[-1]
    if not name:
        raise ValueError("FZ equipment article is missing name")

    stats_raw = attrs.get("stats") if isinstance(attrs.get("stats"), dict) else {}
    stats: list[EquipmentStatView] = []
    for row in stats_raw.get("rows") or []:
        if not isinstance(row, dict):
            continue
        label = _first_text(row, "label", "name")
        raw_values = row.get("values") or []
        value = raw_values[0] if isinstance(raw_values, list) and raw_values else row.get("value")
        if not label or value in (None, ""):
            continue
        formatted_values = [
            _format_equipment_stat(item, _equipment_stat_is_percent(row))
            for item in (raw_values[:4] if isinstance(raw_values, list) else [value])
        ]
        while len(formatted_values) < 4:
            formatted_values.append(formatted_values[-1] if formatted_values else "--")
        stats.append(
            EquipmentStatView(
                label=label,
                value=formatted_values[0],
                values=formatted_values,
                icon_key=str(row.get("attrType") or ""),
            )
        )

    suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
    bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
    bonus_levels = _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
    bonus_level = bonus_levels[-1] if bonus_levels and isinstance(bonus_levels[-1], dict) else {}
    bonus_values = _first_value(bonus_level, "values", "blackboard", "params")
    suit_description = _format_fz_template(
        _first_text(bonus, "description", "desc"),
        bonus_values,
    )
    suit_required_count = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
    suit_name = _first_text(suit, "suitName", "name") or _first_text(hero, "suitName")
    group_name = _first_text(suit, "groupName") or _first_text(hero, "groupName")
    has_suit_effect = bool(clean_text(suit_description))
    pieces: list[EquipmentPieceView] = []
    self_equipment_id = str(suit.get("selfEquipId") or "")
    for piece in suit.get("pieces") or []:
        if not isinstance(piece, dict):
            continue
        piece_id = str(piece.get("equipId") or "")
        if piece_id and piece_id == self_equipment_id:
            continue
        piece_name = _first_text(piece, "name", "title")
        if not piece_name:
            continue
        pieces.append(
            EquipmentPieceView(
                name=piece_name.split("/", 1)[-1],
                slot_type=_first_text(piece, "slotType", "partType") or "装备",
                icon_url=_fz_asset_raw_url(_first_text(piece, "iconUrl", "icon")),
                equipment_id=piece_id,
            )
        )
    if not has_suit_effect:
        suit_name = "独立装备"
        group_name = "独立装备套组"
        suit_required_count = 0
        pieces = []

    materials = attrs.get("materials") if isinstance(attrs.get("materials"), dict) else {}
    return EquipmentView(
        name=name,
        title=title or f"装备/{name}",
        equipment_id=self_equipment_id,
        rarity=_to_int(_first_value(hero, "rarity", "star", "stars")),
        max_level=_to_int(_first_value(hero, "level", "maxLevel", "maxLv")),
        part_type=_first_text(hero, "partType"),
        slot_type=_first_text(hero, "slotType", "type") or "装备",
        suit_name=suit_name,
        group_name=group_name,
        description=_clean_fz_rich_text(_first_text(hero, "description", "desc")),
        flavor=_clean_fz_rich_text(_first_text(hero, "flavor", "quote")),
        icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "icon")),
        stats=stats,
        suit_required_count=suit_required_count,
        suit_description=suit_description,
        suit_pieces=pieces,
        acquisition=_equipment_acquisition(materials),
        term_styles=_build_fz_term_styles(richtext or {}),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


LOADOUT_ATTRIBUTE_NAMES = {
    "Str": "力量",
    "Agi": "敏捷",
    "Wisd": "智识",
    "Will": "意志",
    "CriticalRate": "暴击率",
    "CriticalDamageIncrease": "暴击伤害",
    "PhysicalAndSpellInflictionEnhance": "源石技艺强度",
    "HealOutputIncrease": "治疗效率加成",
    "HealTakenIncrease": "受治疗效率加成",
    "ComboSkillCooldownScalar": "连携技冷却缩减",
    "UltimateSpGainScalar": "终结技充能效率",
    "PoiseDamageOutputScalar": "失衡效率加成",
    "NormalAttackDamageIncrease": "普通攻击伤害加成",
    "NormalSkillDamageIncrease": "战技伤害加成",
    "ComboSkillDamageIncrease": "连携技伤害加成",
    "UltimateSkillDamageIncrease": "终结技伤害加成",
    "PhysicalDamageIncrease": "物理伤害加成",
    "SpellDamageIncrease": "法术伤害加成",
    "FireDamageIncrease": "灼热伤害加成",
    "PulseDamageIncrease": "电磁伤害加成",
    "CrystDamageIncrease": "寒冷伤害加成",
    "NaturalDamageIncrease": "自然伤害加成",
    "EtherDamageIncrease": "超域伤害加成",
    "AllDamageIncrease": "所有伤害加成",
    "AllDamageTakenScalar": "全伤害减免",
}
LOADOUT_PERCENT_ATTRIBUTES = frozenset(
    {
        "CriticalRate",
        "CriticalDamageIncrease",
        "HealOutputIncrease",
        "HealTakenIncrease",
        "ComboSkillCooldownScalar",
        "UltimateSpGainScalar",
        "PoiseDamageOutputScalar",
        "NormalAttackDamageIncrease",
        "NormalSkillDamageIncrease",
        "ComboSkillDamageIncrease",
        "UltimateSkillDamageIncrease",
        "PhysicalDamageIncrease",
        "SpellDamageIncrease",
        "FireDamageIncrease",
        "PulseDamageIncrease",
        "CrystDamageIncrease",
        "NaturalDamageIncrease",
        "EtherDamageIncrease",
        "AllDamageIncrease",
    }
)
LOADOUT_EFFECT_KEY_TARGETS = {
    "str": "Str",
    "agi": "Agi",
    "wisd": "Wisd",
    "will": "Will",
    "atk": "AtkPercent",
    "atk_up": "AtkPercent",
    "primary_attr_up": "MainPercent",
    "main_attr_up": "MainPercent",
    "main_attribute_up": "MainPercent",
    "hp_up": "MaxHpFinal",
    "max_hp": "MaxHpFinal",
    "critical_rate": "CriticalRate",
    "criticalrate": "CriticalRate",
    "crit_rate": "CriticalRate",
    "critical_damage": "CriticalDamageIncrease",
    "criticaldamageincrease": "CriticalDamageIncrease",
    "crit_damage": "CriticalDamageIncrease",
    "heal": "HealOutputIncrease",
    "heal_up": "HealOutputIncrease",
    "heal_output_up": "HealOutputIncrease",
    "second_attr_up": "SubPercent",
    "sub_attr_up": "SubPercent",
    "dmg_up": "AllDamageIncrease",
    "ultimate_gain_up": "UltimateSpGainScalar",
    "phy_spell_up": "PhysicalAndSpellInflictionEnhance",
    "physicalandspellinflictionenhance": "PhysicalAndSpellInflictionEnhance",
    "phy_dmg_up": "PhysicalDamageIncrease",
    "spell_dmg_up": "SpellDamageIncrease",
    "fire_dmg_up": "FireDamageIncrease",
    "pulse_dmg_up": "PulseDamageIncrease",
    "cryst_dmg_up": "CrystDamageIncrease",
    "natural_dmg_up": "NaturalDamageIncrease",
    "ether_dmg_up": "EtherDamageIncrease",
}

LOADOUT_STATUS_TAGS = {
    "导电": "ba.conduct",
    "腐蚀": "ba.corrupt",
    "碎甲": "ba.fracture",
}
LOADOUT_STATUS_DURATION_KEYS = {
    "导电": "duration_conduct",
    "腐蚀": "duration_corrupt",
    "碎甲": "duration_fracture",
}
LOADOUT_STATUS_LEVELS = {
    "导电": tuple((value, duration) for value, duration in zip((0.12, 0.16, 0.20, 0.24), (12, 18, 24, 30))),
    "腐蚀": tuple(zip((3.6, 4.8, 6.0, 7.2), (0.84, 1.12, 1.4, 1.68), (12.0, 16.0, 20.0, 24.0))),
    "碎甲": tuple((value, duration) for value, duration in zip((0.12, 0.16, 0.20, 0.24), (12, 18, 24, 30))),
}
LOADOUT_STATUS_REACTIONS = {
    "自然附着": "腐蚀",
    "电磁附着": "导电",
}


def build_fz_loadout_view(
    operator_raw: dict[str, Any],
    weapon_raw: dict[str, Any],
    equipment_raws: list[tuple[dict[str, Any], int, tuple[tuple[int, int], ...]]],
    *,
    operator_level: int = 90,
    operator_potential: int = 5,
    weapon_level: int = 90,
    weapon_potential: int = 5,
    weapon_skill_levels: tuple[tuple[int, int], ...] = (),
    richtext: dict[str, Any] | None = None,
    operator_growth: dict[str, Any] | None = None,
) -> LoadoutView:
    operator_attrs = _fz_template_attrs(operator_raw)
    weapon_attrs = _fz_template_attrs(weapon_raw)
    operator_hero = operator_attrs.get("hero") if isinstance(operator_attrs.get("hero"), dict) else {}
    weapon_hero = weapon_attrs.get("hero") if isinstance(weapon_attrs.get("hero"), dict) else {}
    if not operator_hero or not weapon_hero:
        raise ValueError("FZ loadout data is missing operator or weapon fields")

    operator_level = max(1, min(90, int(operator_level)))
    operator_potential = max(0, min(5, int(operator_potential)))
    weapon_level = max(1, min(90, int(weapon_level)))
    weapon_potential = max(0, min(5, int(weapon_potential)))
    operator_weapon_type = _first_text(operator_hero, "weaponType", "weapon")
    weapon_type = _first_text(weapon_hero, "weaponType", "weapon")
    if operator_weapon_type and weapon_type and operator_weapon_type != weapon_type:
        raise ValueError(f"武器类型不匹配：干员使用{operator_weapon_type}，所选武器为{weapon_type}")

    base_stats = _fz_operator_attributes_at_level(operator_attrs.get("attributes"), operator_level)
    additions: dict[str, float] = {}
    base_multipliers: dict[str, float] = {}
    final_additions: dict[str, float] = {}
    multipliers: dict[str, float] = {}
    effects: list[LoadoutEffectView] = []

    main_attribute, sub_attribute = _fz_main_sub_attributes(operator_hero)
    main_key = _loadout_attribute_key(main_attribute)
    sub_key = _loadout_attribute_key(sub_attribute)
    _apply_loadout_operator_growth(
        operator_growth,
        _fz_operator_break_stage_at_level(operator_attrs.get("attributes"), operator_level),
        additions,
        effects,
    )
    equipment_views: list[LoadoutEquipmentView] = []
    suits: dict[str, dict[str, Any]] = {}
    suit_counts: dict[str, int] = {}
    part_counts: dict[str, int] = {}
    for raw, default_enhance, forge_overrides in equipment_raws:
        attrs = _fz_template_attrs(raw)
        hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
        if not hero:
            raise ValueError("FZ equipment article does not match the loadout schema")
        actual_part = _first_text(hero, "partType")
        if actual_part not in {"Body", "Hand", "EDC"}:
            raise ValueError(f"无法识别装备槽位：{_first_text(hero, 'name', 'title')}")
        part_counts[actual_part] = part_counts.get(actual_part, 0) + 1
        maximum = 2 if actual_part == "EDC" else 1
        if part_counts[actual_part] > maximum:
            label = {"Body": "护甲", "Hand": "护手", "EDC": "配件"}[actual_part]
            raise ValueError(f"{label}数量超过槽位上限")
        default_enhance = max(0, min(3, int(default_enhance)))
        stat_rows = (attrs.get("stats") or {}).get("rows") or []
        forge_levels = _loadout_equipment_forge_levels(stat_rows, default_enhance, forge_overrides)
        forge_index = 0
        equipment_stats: list[EquipmentStatView] = []
        for row in stat_rows:
            if isinstance(row, dict):
                if bool(row.get("enhances", True)):
                    enhance = forge_levels[forge_index]
                    forge_index += 1
                else:
                    enhance = default_enhance
                _apply_loadout_equipment_row(
                    row,
                    enhance,
                    main_attribute,
                    sub_attribute,
                    additions,
                    base_multipliers,
                    final_additions,
                    multipliers,
                )
                stat = _build_loadout_equipment_stat(row, enhance, main_attribute, sub_attribute)
                if stat is not None:
                    equipment_stats.append(stat)
        suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
        suit_name = _first_text(suit, "suitName", "name") or _first_text(hero, "suitName")
        required = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        if suit_name and required > 0:
            suit_counts[suit_name] = suit_counts.get(suit_name, 0) + 1
            suits.setdefault(suit_name, suit)
        equipment_views.append(
            LoadoutEquipmentView(
                name=_first_text(hero, "name", "title") or str((raw.get("article") or {}).get("title") or "").split("/", 1)[-1],
                slot_type=_first_text(hero, "slotType", "partType") or "装备",
                enhance_levels=forge_levels,
                icon_url=_fz_asset_raw_url(_first_text(hero, "iconUrl", "icon")),
                equipment_id=str(suit.get("selfEquipId") or _first_value(hero, "id", "equipId") or ""),
                suit_name=suit_name,
                stats=equipment_stats,
            )
        )

    _apply_loadout_weapon_skills(
        weapon_attrs.get("skills"),
        weapon_potential,
        weapon_skill_levels,
        additions,
        final_additions,
        multipliers,
        effects,
        source=_first_text(weapon_hero, "name") or "武器",
    )
    _apply_loadout_operator_effects(
        operator_attrs,
        operator_potential,
        additions,
        final_additions,
        multipliers,
        effects,
    )
    for suit_name, suit in suits.items():
        required = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        if suit_counts.get(suit_name, 0) >= required:
            _apply_loadout_set_effect(
                suit_name,
                suit,
                additions,
                final_additions,
                multipliers,
                effects,
            )

    main_percent = additions.pop("MainPercent", 0.0)
    if main_percent:
        base_multipliers[main_key] = base_multipliers.get(main_key, 0.0) + main_percent
    sub_percent = additions.pop("SubPercent", 0.0)
    if sub_percent:
        base_multipliers[sub_key] = base_multipliers.get(sub_key, 0.0) + sub_percent
    stats = dict(base_stats)
    for key, value in additions.items():
        stats[key] = stats.get(key, 0.0) + value
    for key, value in base_multipliers.items():
        stats[key] = stats.get(key, 0.0) * (1 + value)
    operator_attack = base_stats.get("Atk", 0.0)
    weapon_attack = _fz_weapon_attack_at_level(weapon_attrs.get("stats"), weapon_level)
    attack_percent = additions.get("AtkPercent", 0.0)
    fixed_attack = final_additions.get("Atk", 0.0)
    main_value = math.floor(stats.get(main_key, 0.0))
    sub_value = math.floor(stats.get(sub_key, 0.0))
    ability_bonus = main_value * 0.005 + sub_value * 0.002
    attack = math.floor(((operator_attack + weapon_attack) * (1 + attack_percent) + fixed_attack) * (1 + ability_bonus))
    strength = math.floor(stats.get("Str", 0.0))
    hp = math.floor(
        base_stats.get("MaxHp", 0.0) * (1 + additions.get("MaxHpPercent", 0.0))
        + final_additions.get("MaxHp", 0.0)
        + strength * 5
    )
    defense = math.floor(stats.get("Def", 0.0) + final_additions.get("Def", 0.0))
    physical_resistance = 1 - 1 / (0.001 * math.floor(stats.get("Agi", 0.0)) + 1)
    spell_resistance = 1 - 1 / (0.001 * math.floor(stats.get("Wisd", 0.0)) + 1)
    healing_taken = stats.get("HealTakenIncrease", 0.0) + math.floor(stats.get("Will", 0.0)) * 0.001
    arts_strength = stats.get("PhysicalAndSpellInflictionEnhance", 0.0)
    status_effect_bonus = _loadout_status_effect_bonus(arts_strength)

    primary_stats = [
        LoadoutPanelStatView("Atk", "攻击力", str(attack), f"{int(operator_attack)} + {int(weapon_attack)}，攻击加成 {_format_loadout_percent(attack_percent)}，能力加成 {_format_loadout_percent(ability_bonus)}"),
        LoadoutPanelStatView("MaxHp", "生命值", str(hp), f"基础 {int(base_stats.get('MaxHp', 0))}，力量额外 +{strength * 5}"),
        LoadoutPanelStatView("Def", "防御力", str(defense)),
    ]
    ability_stats = [
        LoadoutPanelStatView(key, LOADOUT_ATTRIBUTE_NAMES[key], str(math.floor(stats.get(key, 0.0))))
        for key in ("Str", "Agi", "Wisd", "Will")
    ]
    advanced_values = dict(stats)
    advanced_values["PhysicalResistance"] = physical_resistance
    for key in ("FireResistance", "PulseResistance", "CrystResistance", "NaturalResistance", "EtherResistance"):
        advanced_values[key] = spell_resistance
    advanced_values["HealTakenIncrease"] = healing_taken
    if "AllDamageTakenScalar" in multipliers:
        advanced_values["AllDamageTakenScalar"] = 1 - multipliers["AllDamageTakenScalar"]
    advanced_stats = _build_loadout_advanced_stats(advanced_values)
    for row in advanced_stats:
        if row.key == "PhysicalAndSpellInflictionEnhance":
            row.detail = f"导电 / 腐蚀 / 碎甲附带效果 +{status_effect_bonus * 100:.1f}%"
    status_effects = _build_loadout_status_effects(operator_attrs, operator_potential, arts_strength)

    versions = [
        str((operator_raw.get("article") or {}).get("updatedAt") or "")[:10],
        str((weapon_raw.get("article") or {}).get("updatedAt") or "")[:10],
        *(str((raw.get("article") or {}).get("updatedAt") or "")[:10] for raw, _, _ in equipment_raws),
    ]
    return LoadoutView(
        operator_name=_first_text(operator_hero, "name", "title"),
        weapon_name=_first_text(weapon_hero, "name", "title"),
        operator_level=operator_level,
        operator_potential=operator_potential,
        weapon_level=weapon_level,
        weapon_potential=weapon_potential,
        main_attribute=main_attribute,
        sub_attribute=sub_attribute,
        weapon_type=weapon_type,
        operator_icon_url=_fz_asset_raw_url(_first_text(operator_hero, "iconUrl", "avatarUrl", "icon")),
        weapon_icon_url=_fz_asset_raw_url(_first_text(weapon_hero, "iconUrl", "icon")),
        operator_id=str(_first_value(operator_hero, "id", "charId", "operatorId") or ""),
        weapon_id=str(
            _first_value(weapon_hero, "id", "weaponId")
            or _fz_weapon_id(_unwrap_fz_list(weapon_attrs.get("skills"), "skills", "items", "list"))
        ),
        equipment=equipment_views,
        primary_stats=primary_stats,
        ability_stats=ability_stats,
        advanced_stats=advanced_stats,
        status_effect_bonus=status_effect_bonus,
        status_effects=status_effects,
        effects=effects,
        source_version=max((version for version in versions if version), default=""),
        term_styles=_build_fz_term_styles(richtext or {}),
    )


def _fz_operator_attributes_at_level(raw: Any, level: int) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("FZ operator attributes are missing")
    breaks = raw.get("breaks") or []
    rows = raw.get("rows") or []
    selected_group = -1
    selected_index = -1
    for group_index, group in enumerate(breaks):
        levels = group.get("levels") if isinstance(group, dict) else None
        if isinstance(levels, list) and level in levels:
            selected_group = group_index
            selected_index = levels.index(level)
    if selected_group < 0:
        raise ValueError(f"FZ operator level not found: {level}")
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get("cells") or []
        if selected_group >= len(cells) or selected_index >= len(cells[selected_group]):
            continue
        value = _to_float(cells[selected_group][selected_index])
        key = str(row.get("key") or row.get("hint") or "")
        if key and value is not None:
            result[key] = value
    return result


def _fz_operator_break_stage_at_level(raw: Any, level: int) -> int:
    if not isinstance(raw, dict):
        return 0
    selected_stage = 0
    for group in raw.get("breaks") or []:
        if not isinstance(group, dict):
            continue
        levels = group.get("levels")
        if isinstance(levels, list) and level in levels:
            selected_stage = _to_int(group.get("breakStage"))
    return selected_stage


LOADOUT_GROWTH_ATTRIBUTE_KEYS = {
    39: "Str",
    40: "Agi",
    41: "Wisd",
    42: "Will",
}


def _apply_loadout_operator_growth(
    raw: dict[str, Any] | None,
    break_stage: int,
    additions: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
    growth = data.get("charGrowthTable") if isinstance(data, dict) else None
    if not isinstance(growth, dict):
        return
    totals: dict[str, float] = {}
    for node in (growth.get("talentNodeMap") or {}).values():
        if not isinstance(node, dict):
            continue
        info = node.get("attributeNodeInfo")
        if not isinstance(info, dict) or _to_int(info.get("breakStage")) > break_stage:
            continue
        for modifier in info.get("attributeModifiers") or []:
            if not isinstance(modifier, dict):
                continue
            raw_type = modifier.get("attrType")
            key = LOADOUT_GROWTH_ATTRIBUTE_KEYS.get(_to_int(raw_type), _loadout_attribute_key(str(raw_type or "")))
            value = _to_float(modifier.get("attrValue"))
            if key not in LOADOUT_ATTRIBUTE_NAMES or value is None:
                continue
            totals[key] = totals.get(key, 0.0) + value
    if not totals:
        return
    for key, value in totals.items():
        additions[key] = additions.get(key, 0.0) + value
    description = "，".join(
        f"{LOADOUT_ATTRIBUTE_NAMES[key]}+{_format_plain_number(totals[key])}"
        for key in ("Str", "Agi", "Wisd", "Will")
        if key in totals
    )
    effects.append(LoadoutEffectView("干员 · 能力天赋", description, active=True))


def _fz_main_sub_attributes(hero: dict[str, Any]) -> tuple[str, str]:
    value = _fz_hero_meta_value(hero, "主 / 副属性", "主/副属性", "主副属性")
    parts = [part.strip() for part in re.split(r"[/／]", value) if part.strip()]
    if len(parts) != 2:
        raise ValueError("FZ operator data is missing main/sub attributes")
    return parts[0], parts[1]


def _loadout_attribute_key(label: str) -> str:
    return {"力量": "Str", "敏捷": "Agi", "智识": "Wisd", "意志": "Will"}.get(label, label)


def _loadout_equipment_forge_levels(
    rows: list[Any],
    default_enhance: int,
    overrides: tuple[tuple[int, int], ...],
) -> tuple[int, ...]:
    forgeable_count = sum(isinstance(row, dict) and bool(row.get("enhances", True)) for row in rows)
    override_map = dict(overrides)
    invalid = [index for index, level in override_map.items() if index < 1 or index > forgeable_count or not 0 <= level <= 3]
    if invalid:
        raise ValueError(f"词条编号超出范围：词条{invalid[0]}（该装备共有{forgeable_count}条可锻造词条）")
    return tuple(override_map.get(index, default_enhance) for index in range(1, forgeable_count + 1))


def _apply_loadout_equipment_row(
    row: dict[str, Any],
    enhance: int,
    main_attribute: str,
    sub_attribute: str,
    additions: dict[str, float],
    base_multipliers: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
) -> None:
    values = row.get("values") or []
    raw_value = values[min(enhance, len(values) - 1)] if isinstance(values, list) and values else row.get("value")
    value = _to_float(raw_value)
    if value is None:
        return
    target = str(row.get("compositeAttr") or row.get("attrType") or "")
    if target == "Main":
        target = _loadout_attribute_key(main_attribute)
    elif target == "Sub":
        target = _loadout_attribute_key(sub_attribute)
    if not target or target == "Level":
        return
    modifier = str(row.get("modifierType") or "BaseAddition")
    if modifier == "BaseFinalAddition":
        final_additions[target] = final_additions.get(target, 0.0) + value
    elif modifier == "BaseMultiplier":
        base_multipliers[target] = base_multipliers.get(target, 0.0) + value
    elif modifier == "BaseFinalMultiplier":
        multipliers[target] = multipliers.get(target, 1.0) * value
    elif target == "Atk" and _equipment_stat_is_percent(row):
        additions["AtkPercent"] = additions.get("AtkPercent", 0.0) + value
    elif target == "MaxHp" and _equipment_stat_is_percent(row):
        additions["MaxHpPercent"] = additions.get("MaxHpPercent", 0.0) + value
    else:
        additions[target] = additions.get(target, 0.0) + value


def _build_loadout_equipment_stat(
    row: dict[str, Any],
    enhance: int,
    main_attribute: str,
    sub_attribute: str,
) -> EquipmentStatView | None:
    if str(row.get("attrType") or "") == "Def":
        return None
    values = row.get("values") or []
    raw_value = values[min(enhance, len(values) - 1)] if isinstance(values, list) and values else row.get("value")
    if raw_value in (None, ""):
        return None
    label = _first_text(row, "label", "name")
    composite = str(row.get("compositeAttr") or "")
    if composite == "Main":
        label = f"{label or '主能力'}（{main_attribute}）"
    elif composite == "Sub":
        label = f"{label or '副能力'}（{sub_attribute}）"
    if not label:
        return None
    return EquipmentStatView(
        label=label,
        value=_format_equipment_stat(raw_value, _equipment_stat_is_percent(row)),
        icon_key=str(row.get("attrType") or ""),
    )


def _equipment_stat_is_percent(row: dict[str, Any]) -> bool:
    value_format = str(row.get("valueFormat") or "")
    if value_format:
        return "%" in value_format
    if str(row.get("modifierType") or "") == "BaseMultiplier":
        return True
    return bool(row.get("isPercent"))


def _fz_weapon_attack_at_level(raw: Any, level: int) -> float:
    stats = raw if isinstance(raw, dict) else {}
    curve = stats.get("curve") or []
    exact = next((row for row in curve if isinstance(row, dict) and _to_int(row.get("lv")) == level), None)
    if exact is None:
        raise ValueError(f"FZ weapon level not found: {level}")
    return _to_float(exact.get("atk")) or 0.0


def _apply_loadout_weapon_skills(
    raw: Any,
    potential: int,
    skill_levels: tuple[tuple[int, int], ...],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
    *,
    source: str,
) -> None:
    overrides = dict(skill_levels)
    skills = _unwrap_fz_list(raw, "skills", "items", "list")
    for skill_index, skill in enumerate(skills, 1):
        if not isinstance(skill, dict):
            continue
        maximum = min(9, _to_int(skill.get("zeroPotentialMaxLevel")) + potential)
        levels = _ordered_fz_levels(_unwrap_fz_list(skill.get("levels"), "levels", "items", "list"))
        requested_level = overrides.get(skill_index)
        if requested_level is not None and requested_level > maximum:
            raise ValueError(f"武器技能{skill_index}在当前潜能下最高为等级{maximum}")
        target_level = requested_level if requested_level is not None else maximum
        selected = next((item for item in levels if _to_int(item.get("level")) == target_level), None)
        if selected is None and requested_level is not None:
            available = [
                _to_int(item.get("level"))
                for item in levels
                if 0 < _to_int(item.get("level")) <= maximum
            ]
            choices = "、".join(str(level) for level in available) or "无"
            raise ValueError(f"武器技能{skill_index}不支持等级{requested_level}（可选：{choices}）")
        if selected is None:
            selected = levels[-1] if levels else None
        if not isinstance(selected, dict):
            continue
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        description = _first_text(skill, "description", "desc")
        _apply_loadout_description(
            description,
            values,
            additions,
            final_additions,
            multipliers,
            effects,
            f"{source} · {_first_text(skill, 'name', 'title') or '武器效果'} Lv.{_to_int(selected.get('level'))}",
        )
    unknown_indices = sorted(set(overrides) - set(range(1, len(skills) + 1)))
    if unknown_indices:
        raise ValueError(f"武器技能序号超出范围：武器技能{unknown_indices[0]}（该武器共有{len(skills)}个技能）")


def _apply_loadout_operator_effects(
    attrs: dict[str, Any],
    operator_potential: int,
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    item_groups = (
        ("talents", _unwrap_fz_list(attrs.get("talents"), "talents", "items", "list")),
        ("potentials", _loadout_operator_potentials(attrs.get("potentials"), operator_potential)),
    )
    for field, items in item_groups:
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            if isinstance(item, dict):
                latest[_first_text(item, "name", "title") or str(len(latest))] = item
        for name, item in latest.items():
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            _apply_loadout_description(
                _first_text(item, "description", "desc", "effect"),
                values,
                additions,
                final_additions,
                multipliers,
                effects,
                f"干员 · {name}",
            )


def _apply_loadout_set_effect(
    suit_name: str,
    suit: dict[str, Any],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
) -> None:
    bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
    levels = _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
    selected = levels[-1] if levels and isinstance(levels[-1], dict) else {}
    values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
    _apply_loadout_description(
        _first_text(bonus, "description", "desc"),
        values,
        additions,
        final_additions,
        multipliers,
        effects,
        f"{suit_name}套装",
    )


def _apply_loadout_description(
    description: str,
    values: dict[str, Any],
    additions: dict[str, float],
    final_additions: dict[str, float],
    multipliers: dict[str, float],
    effects: list[LoadoutEffectView],
    source: str,
) -> None:
    if not description or not values:
        return
    for clause in (part.strip() for part in re.split(r"[。；\n]+", description) if part.strip()):
        keys = [str(key) for key in values if re.search(rf"\b{re.escape(str(key))}\b", clause, flags=re.I)]
        if not keys:
            continue
        triggered = _loadout_clause_is_triggered(clause)
        rendered = _format_fz_template(clause, values)
        resolved: list[tuple[str, float]] = []
        for key in keys:
            value = _to_float(_case_insensitive_get(values, key))
            if value is None:
                continue
            target = _loadout_effect_target(key, clause, allow_label_fallback=len(keys) == 1)
            if not target:
                continue
            resolved.append((target, value))
        active = not triggered
        effects.append(LoadoutEffectView(source, rendered, active=active))
        if triggered:
            continue
        for target, value in resolved:
            if target == "AllDamageTakenScalar":
                multipliers[target] = multipliers.get(target, 1.0) * value
            elif target == "AtkFinal":
                final_additions["Atk"] = final_additions.get("Atk", 0.0) + value
            elif target == "MaxHpFinal":
                final_additions["MaxHp"] = final_additions.get("MaxHp", 0.0) + value
            else:
                additions[target] = additions.get(target, 0.0) + value


def _loadout_clause_is_triggered(clause: str) -> bool:
    plain = _clean_fz_rich_text(clause)
    return bool(
        re.search(
            r"(?:当|每|若|如果|期间|时[，,]|后[，,使]|根据|使(?:其他队友|敌人)|装备者施加|装备者造成|"
            r"对.+(?:敌人|目标)|(?:命中|击中|施放|释放|消耗|触发|使用|进入|离开).{0,32}?时(?!间)|"
            r"(?:连携技|终结技|战技|普通攻击).+的|所需)",
            plain,
        )
    )


def _loadout_effect_target(key: str, clause: str, *, allow_label_fallback: bool) -> str:
    lowered = _alias_key(key).lower()
    plain = _clean_fz_rich_text(clause)
    if lowered == "dmg_taken_down":
        return "AllDamageTakenScalar"
    target = LOADOUT_EFFECT_KEY_TARGETS.get(lowered)
    if target:
        # FZ data also uses names such as ``atk_up`` for skill damage ratios.
        # Only treat those ambiguous keys as a panel attack bonus when the
        # rendered description explicitly says 攻击力.
        if target == "AtkPercent" and "攻击力" not in plain:
            return ""
        return target
    if any(token in lowered for token in ("duration", "time", "count", "cost", "stack", "limit", "interval", "cooldown")):
        return ""
    semantic_targets = (
        (("phy", "physical"), "PhysicalDamageIncrease"),
        (("spell",), "SpellDamageIncrease"),
        (("fire",), "FireDamageIncrease"),
        (("pulse",), "PulseDamageIncrease"),
        (("cryst", "cold"), "CrystDamageIncrease"),
        (("natural",), "NaturalDamageIncrease"),
        (("ether",), "EtherDamageIncrease"),
    )
    if any(token in lowered for token in ("dmg", "damage", "up")):
        for tokens, semantic_target in semantic_targets:
            if any(token in lowered for token in tokens):
                return semantic_target
    if re.fullmatch(r"(?:owner_)?(?:atk|attack)(?:_(?:up|increase|bonus|percent|pct))?", lowered):
        return "AtkPercent"
    if not allow_label_fallback:
        return ""
    label_targets = (
        ("暴击伤害", "CriticalDamageIncrease"),
        ("暴击率", "CriticalRate"),
        ("治疗效率", "HealOutputIncrease"),
        ("主能力", "MainPercent"),
        ("副能力", "SubPercent"),
        ("终结技充能效率", "UltimateSpGainScalar"),
        ("源石技艺强度", "PhysicalAndSpellInflictionEnhance"),
        ("物理伤害", "PhysicalDamageIncrease"),
        ("法术伤害", "SpellDamageIncrease"),
        ("灼热伤害", "FireDamageIncrease"),
        ("电磁伤害", "PulseDamageIncrease"),
        ("寒冷伤害", "CrystDamageIncrease"),
        ("自然伤害", "NaturalDamageIncrease"),
        ("超域伤害", "EtherDamageIncrease"),
        ("攻击力", "AtkPercent" if "%" in clause else "AtkFinal"),
        ("生命值", "MaxHpPercent" if "%" in clause else "MaxHpFinal"),
        ("力量", "Str"),
        ("敏捷", "Agi"),
        ("智识", "Wisd"),
        ("意志", "Will"),
    )
    return next((target for label, target in label_targets if label in plain), "")


def _loadout_status_effect_bonus(arts_strength: float) -> float:
    strength = max(0.0, float(arts_strength))
    return 2 * strength / (strength + 300) if strength else 0.0


def format_status_quick_calc(status_name: str, level: int, arts_strength: int) -> str:
    if status_name not in LOADOUT_STATUS_LEVELS:
        raise ValueError("仅支持腐蚀、导电或碎甲")
    if level not in range(1, 5):
        raise ValueError("异常效果等级必须在 1–4 之间")
    if arts_strength < 0:
        raise ValueError("源石技艺强度不能小于 0")

    bonus = _loadout_status_effect_bonus(arts_strength)
    effect = _make_loadout_status_levels(status_name, bonus)[level - 1]
    return "\n".join(
        [
            f"Lv{level} {status_name}速算",
            f"源石技艺强度：{arts_strength}（附带效果 +{bonus * 100:.1f}%）",
            f"效果：{effect.value}",
            f"构成：{effect.detail}",
            f"持续：{effect.duration}",
        ]
    )


def _build_loadout_status_effects(
    attrs: dict[str, Any],
    operator_potential: int,
    arts_strength: float,
) -> list[LoadoutStatusEffectView]:
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    tags = hero.get("tags") if isinstance(hero.get("tags"), list) else []
    bonus = _loadout_status_effect_bonus(arts_strength)
    latest_talents = _latest_loadout_operator_items(attrs.get("talents"), "talents")
    potentials = _loadout_operator_potentials(attrs.get("potentials"), operator_potential)
    duration_additions = {name: 0.0 for name in LOADOUT_STATUS_TAGS}
    maximum_multipliers = {name: 1.0 for name in LOADOUT_STATUS_TAGS}
    for item in (*latest_talents, *potentials):
        description = _first_text(item, "description", "desc", "effect")
        plain = _clean_fz_rich_text(description)
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        for status_name in _loadout_status_names(description):
            if "自身施加" in plain and "效果持续时间" in plain:
                duration_additions[status_name] += sum(
                    _to_float(value) or 0.0
                    for key, value in values.items()
                    if "duration_add" in str(key).lower()
                )
            if status_name == "腐蚀" and "降低的最大抗性" in plain:
                maximum_multipliers[status_name] += sum(
                    _to_float(value) or 0.0
                    for key, value in values.items()
                    if "corrupt_rate" in str(key).lower()
                )

    status_sources = {
        status_name: "普通附带效果"
        for status_name in LOADOUT_STATUS_TAGS
        if status_name in tags
    }
    for attachment_name, status_name in LOADOUT_STATUS_REACTIONS.items():
        if attachment_name in tags and status_name not in status_sources:
            status_sources[status_name] = f"法术反应 · {attachment_name}"

    result: list[LoadoutStatusEffectView] = []
    for status_name in LOADOUT_STATUS_TAGS:
        source = status_sources.get(status_name)
        if not source:
            continue
        notes = [f"源石技艺增益 +{bonus * 100:.1f}%"]
        if duration_additions[status_name]:
            notes.append(f"特性持续 +{_format_status_number(duration_additions[status_name])}秒")
        if maximum_multipliers[status_name] != 1:
            notes.append(f"最大降抗 ×{maximum_multipliers[status_name]:.2f}")
        result.append(
            LoadoutStatusEffectView(
                name=status_name,
                source=source,
                levels=_make_loadout_status_levels(
                    status_name,
                    bonus,
                    duration_add=duration_additions[status_name],
                    maximum_multiplier=maximum_multipliers[status_name],
                ),
                note=" · ".join(notes),
            )
        )

    for skill in _unwrap_fz_list(attrs.get("skills"), "skills", "items", "list"):
        if not isinstance(skill, dict):
            continue
        description = _first_text(skill, "description", "desc", "effect")
        plain = _clean_fz_rich_text(description)
        if "强制施加" not in plain:
            continue
        skill_name = _first_text(skill, "name", "title") or "强制异常技能"
        levels = [item for item in skill.get("levels") or [] if isinstance(item, dict)]
        selected = levels[-1] if levels else {}
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        for status_name in _loadout_status_names(description):
            duration_key = LOADOUT_STATUS_DURATION_KEYS[status_name]
            duration = _to_float(_case_insensitive_get(values, duration_key))
            if duration is None:
                duration = _to_float(_case_insensitive_get(values, "duration"))
            duration = duration or 0.0
            characteristic_multiplier = 1.0
            characteristic_notes: list[str] = []
            for potential in potentials:
                potential_description = _first_text(potential, "description", "desc", "effect")
                potential_plain = _clean_fz_rich_text(potential_description)
                if skill_name not in potential_plain or status_name not in _loadout_status_names(potential_description):
                    continue
                potential_values = potential.get("values") if isinstance(potential.get("values"), dict) else {}
                for key, raw_value in potential_values.items():
                    value = _to_float(raw_value)
                    if value is None:
                        continue
                    lowered = str(key).lower()
                    if "duration" in lowered and "持续时间" in potential_plain:
                        if re.search(rf"\{{\s*{re.escape(str(key))}\s*-\s*1\s*:", potential_description, flags=re.I):
                            duration *= value
                            characteristic_notes.append(f"持续 ×{_format_status_number(value)}")
                        else:
                            duration += value
                            characteristic_notes.append(f"持续 +{_format_status_number(value)}秒")
                    if lowered in {"extra_scaling", "effect_scaling"} and "提升至原本" in potential_plain:
                        characteristic_multiplier *= value
                        characteristic_notes.append(f"效果 ×{_format_status_number(value)}")
            note_parts = ["强制异常按 Lv1 基础值", f"源石技艺增益 +{bonus * 100:.1f}%", *characteristic_notes]
            result.append(
                LoadoutStatusEffectView(
                    name=status_name,
                    source=skill_name,
                    forced=True,
                    levels=_make_loadout_status_levels(
                        status_name,
                        bonus,
                        characteristic_multiplier=characteristic_multiplier,
                        forced_duration=duration,
                        level_count=1,
                    ),
                    note=" · ".join(note_parts),
                )
            )
    return result


def _latest_loadout_operator_items(raw: Any, field: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in _unwrap_fz_list(raw, field, "items", "list"):
        if isinstance(item, dict):
            latest[_first_text(item, "name", "title") or str(len(latest))] = item
    return list(latest.values())


def _loadout_operator_potentials(raw: Any, operator_potential: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(_unwrap_fz_list(raw, "potentials", "items", "list"), 1):
        if not isinstance(item, dict):
            continue
        unlock_level = _to_int(_first_value(item, "level", "potentialLevel", "rank")) or index
        if unlock_level <= operator_potential:
            selected.append(item)
    return selected


def _loadout_status_names(description: str) -> list[str]:
    plain = _clean_fz_rich_text(description)
    return [
        name
        for name, richtext_id in LOADOUT_STATUS_TAGS.items()
        if name in plain or richtext_id in description
    ]


def _make_loadout_status_levels(
    status_name: str,
    bonus: float,
    *,
    duration_add: float = 0.0,
    maximum_multiplier: float = 1.0,
    characteristic_multiplier: float = 1.0,
    forced_duration: float = 0.0,
    level_count: int = 4,
) -> list[LoadoutStatusLevelView]:
    effect_multiplier = (1 + bonus) * characteristic_multiplier
    result: list[LoadoutStatusLevelView] = []
    for index, base in enumerate(LOADOUT_STATUS_LEVELS[status_name][:level_count], 1):
        if status_name == "腐蚀":
            initial, per_second, maximum = base
            value = f"最大降抗 {_format_status_number(maximum * effect_multiplier * maximum_multiplier)}"
            detail = (
                f"初始 {_format_status_number(initial * effect_multiplier)}"
                f" · 每秒 {_format_status_number(per_second * effect_multiplier)}"
            )
            duration = forced_duration or (15 + duration_add)
        else:
            base_value, base_duration = base
            label = "法术易伤" if status_name == "导电" else "物理易伤"
            value = f"{label} {_format_status_percent(base_value * effect_multiplier)}"
            detail = f"基础 {_format_status_percent(base_value)}"
            duration = forced_duration or (base_duration + duration_add)
        result.append(
            LoadoutStatusLevelView(
                level=index,
                value=value,
                detail=detail,
                duration=f"{_format_status_number(duration)}秒",
            )
        )
    return result


def _format_status_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_status_percent(value: float) -> str:
    return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _build_loadout_advanced_stats(values: dict[str, float]) -> list[LoadoutPanelStatView]:
    labels = dict(LOADOUT_ATTRIBUTE_NAMES)
    labels.update(
        {
            "PhysicalResistance": "物理抗性",
            "FireResistance": "灼热抗性",
            "PulseResistance": "电磁抗性",
            "CrystResistance": "寒冷抗性",
            "NaturalResistance": "自然抗性",
            "EtherResistance": "超域抗性",
        }
    )
    order = (
        "CriticalRate",
        "CriticalDamageIncrease",
        "PhysicalAndSpellInflictionEnhance",
        "PhysicalResistance",
        "FireResistance",
        "PulseResistance",
        "CrystResistance",
        "NaturalResistance",
        "EtherResistance",
        "HealOutputIncrease",
        "HealTakenIncrease",
        "UltimateSpGainScalar",
        "ComboSkillCooldownScalar",
        "PoiseDamageOutputScalar",
        "AllDamageIncrease",
        "AllDamageTakenScalar",
        "NormalAttackDamageIncrease",
        "NormalSkillDamageIncrease",
        "ComboSkillDamageIncrease",
        "UltimateSkillDamageIncrease",
        "PhysicalDamageIncrease",
        "SpellDamageIncrease",
        "FireDamageIncrease",
        "PulseDamageIncrease",
        "CrystDamageIncrease",
        "NaturalDamageIncrease",
        "EtherDamageIncrease",
    )
    resistance_keys = {
        "PhysicalResistance",
        "FireResistance",
        "PulseResistance",
        "CrystResistance",
        "NaturalResistance",
        "EtherResistance",
    }
    always_show = {"CriticalRate", "CriticalDamageIncrease", "PhysicalAndSpellInflictionEnhance", *resistance_keys}
    result: list[LoadoutPanelStatView] = []
    for key in order:
        value = values.get(key, 0.0)
        if key not in always_show and abs(value) < 1e-9:
            continue
        if key in LOADOUT_PERCENT_ATTRIBUTES or key in resistance_keys | {"AllDamageTakenScalar"}:
            formatted = _format_loadout_percent(value)
        else:
            formatted = str(math.floor(value))
        result.append(LoadoutPanelStatView(key, labels.get(key, key), formatted))
    return result


def _format_loadout_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_fz_equipment_catalog_view(
    raw: dict[str, Any],
    group_name: str = "",
    rarity_filter: str = "gold",
) -> EquipmentCatalogView:
    article = raw.get("article") or {}
    entries = _fz_equipment_roster_entries(raw)
    if not entries:
        raise ValueError("FZ equipment roster does not match the supported catalog schema")

    normalized_group_name = _normalize_equipment_group_name(group_name)
    grouped: dict[str, list[EquipmentCatalogItemView]] = {}
    rarity_value = {"gold": 5, "purple": 4, "blue": 3}.get(rarity_filter)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if rarity_value is not None and _to_int(entry.get("rarity")) != rarity_value:
            continue
        name = _first_text(entry, "name", "title")
        title = _first_text(entry, "title") or (f"装备/{name}" if name else "")
        current_group = _normalize_equipment_group_name(_first_text(entry, "group")) or "独立装备套组"
        if not name or not title:
            continue
        attributes = _fz_equipment_roster_attributes(entry)
        grouped.setdefault(current_group, []).append(
            EquipmentCatalogItemView(
                name=name,
                title=title,
                group_name=current_group,
                equipment_id=str(entry.get("equipId") or ""),
                level=_to_int(entry.get("level")),
                rarity=_to_int(entry.get("rarity")),
                slot_type=_first_text(entry, "slotType", "partType") or "装备",
                icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
                attributes=attributes,
            )
        )

    slot_order = {"护甲": 0, "护手": 1, "配件": 2}
    groups: list[EquipmentCatalogGroupView] = []
    for current_group, items in grouped.items():
        if normalized_group_name and current_group != normalized_group_name:
            continue
        items.sort(key=lambda item: (slot_order.get(item.slot_type, 9), item.name))
        groups.append(EquipmentCatalogGroupView(current_group, items))
    if normalized_group_name and not groups:
        raise ValueError(f"FZ equipment group not found: {group_name}")

    total_count = sum(len(group.items) for group in groups)
    return EquipmentCatalogView(
        title=normalized_group_name or "全部装备套组",
        groups=groups,
        total_count=total_count,
        rarity_filter=rarity_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def _fz_equipment_roster_attributes(entry: dict[str, Any]) -> list[EquipmentCatalogAttributeView]:
    attributes = [
        EquipmentCatalogAttributeView(
            label=_first_text(attribute, "label", "name"),
            value=clean_text(_first_value(attribute, "value", "text")),
        )
        for attribute in (entry.get("attrList") or [])
        if isinstance(attribute, dict) and _first_text(attribute, "label", "name")
    ]
    if attributes:
        return attributes
    return [
        EquipmentCatalogAttributeView(label=clean_text(label))
        for label in (entry.get("attrKeys") or [])
        if clean_text(label)
    ]


def build_fz_equipment_attribute_catalog_view(
    raw: dict[str, Any],
    detail_raws: dict[str, dict[str, Any]],
    filters: Sequence[EquipmentAttributeFilter],
    rarity_filter: str = "gold",
) -> EquipmentCatalogView:
    if not filters:
        raise ValueError("FZ equipment attribute filter is empty")

    view = build_fz_equipment_catalog_view(raw, "", rarity_filter)
    groups: list[EquipmentCatalogGroupView] = []
    for group in view.groups:
        items: list[EquipmentCatalogItemView] = []
        for item in group.items:
            detail = detail_raws.get(item.title)
            if not isinstance(detail, dict):
                continue
            _apply_fz_equipment_catalog_item_detail(item, detail)
            main, sub = item.main_attribute, item.sub_attribute
            if not _equipment_attributes_match(main, sub, filters):
                continue
            items.append(item)
        if items:
            groups.append(EquipmentCatalogGroupView(group.name, items))

    # 主/副能力套组对任何属性组合都成立，排在具体属性匹配之后展示。
    groups.sort(key=lambda group: all(_equipment_group_item_is_wildcard(item) for item in group.items))
    attribute_filter = format_equipment_attribute_filters(filters)
    if not groups:
        raise ValueError(f"FZ equipment attribute filter not found: {attribute_filter}")
    view.title = attribute_filter
    view.attribute_filter = attribute_filter
    view.groups = groups
    view.total_count = sum(len(group.items) for group in groups)
    return view


def _apply_fz_equipment_catalog_item_details(
    view: EquipmentCatalogView,
    detail_raws: dict[str, dict[str, Any]],
) -> None:
    for group in view.groups:
        for item in group.items:
            detail = detail_raws.get(item.title)
            if isinstance(detail, dict):
                _apply_fz_equipment_catalog_item_detail(item, detail)


def _apply_fz_equipment_catalog_item_detail(
    item: EquipmentCatalogItemView,
    detail: dict[str, Any],
) -> None:
    main, sub, extras = _fz_equipment_attribute_slots(detail)
    item.level = item.level or _fz_equipment_detail_level(detail)
    item.main_attribute = main
    item.sub_attribute = sub
    item.attributes = [
        attribute
        for attribute in (
            EquipmentCatalogAttributeView(label=main, role="main") if main else None,
            EquipmentCatalogAttributeView(label=sub, role="sub") if sub else None,
            *extras,
        )
        if attribute is not None
    ]


def _fz_equipment_detail_level(raw: dict[str, Any]) -> int:
    attrs = _fz_template_attrs(raw)
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    return _to_int(_first_value(hero, "level", "maxLevel", "maxLv"))


def _equipment_group_item_is_wildcard(item: EquipmentCatalogItemView) -> bool:
    return EQUIPMENT_ABILITY_WILDCARD in {item.main_attribute, item.sub_attribute}


def _fz_equipment_attribute_slots(
    raw: dict[str, Any],
) -> tuple[str, str, list[EquipmentCatalogAttributeView]]:
    """Split a FZ equipment card into its 主属性、副属性 and remaining 词条.

    FZ lists the ability rows right after the base 防御力 row, larger value
    first, so the first ability row is the 主属性 and the second the 副属性.
    The 集成实训 style rows carry no concrete attribute (they scale with the
    wearer's own 主/副能力) and become wildcards that match every filter.
    """
    attrs = _fz_template_attrs(raw)
    stats = attrs.get("stats") if isinstance(attrs.get("stats"), dict) else {}
    rows = stats.get("rows") or []
    slots: list[str] = []
    extras: list[EquipmentCatalogAttributeView] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("isBase"):
            continue
        label = _first_text(row, "label", "name")
        attribute = _fz_equipment_ability_attribute(row)
        if attribute and len(slots) < 2 and not extras:
            slots.append(attribute)
            continue
        if label:
            extras.append(
                EquipmentCatalogAttributeView(
                    label=label,
                    value=_format_equipment_stat(
                        (row.get("values") or [row.get("value")])[-1],
                        _equipment_stat_is_percent(row),
                    ),
                )
            )
    main = slots[0] if slots else ""
    sub = slots[1] if len(slots) > 1 else ""
    return main, sub, extras


def _fz_equipment_ability_attribute(row: dict[str, Any]) -> str:
    attribute = EQUIPMENT_ABILITY_ATTRIBUTES.get(str(row.get("attrType") or ""), "")
    if attribute:
        return attribute
    composite = str(row.get("compositeAttr") or "")
    if composite in {"Main", "Sub"} and str(row.get("modifierType") or "") == "BaseAddition":
        return EQUIPMENT_ABILITY_WILDCARD
    return ""


def _equipment_attributes_match(
    main: str,
    sub: str,
    filters: Sequence[EquipmentAttributeFilter],
) -> bool:
    for item in filters:
        if item.role == "main":
            matched = _equipment_attribute_slot_matches(main, item.attribute)
        elif item.role == "sub":
            matched = _equipment_attribute_slot_matches(sub, item.attribute)
        else:
            matched = _equipment_attribute_slot_matches(
                main, item.attribute
            ) or _equipment_attribute_slot_matches(sub, item.attribute)
        if not matched:
            return False
    return True


def _equipment_attribute_slot_matches(slot: str, attribute: str) -> bool:
    return bool(slot) and slot in {attribute, EQUIPMENT_ABILITY_WILDCARD}


def _apply_fz_equipment_catalog_suit_effects(
    view: EquipmentCatalogView,
    detail_raws: list[dict[str, Any]],
) -> None:
    groups = {group.name: group for group in view.groups}
    for raw in detail_raws:
        attrs = _fz_template_attrs(raw)
        suit = attrs.get("suit") if isinstance(attrs.get("suit"), dict) else {}
        bonus = suit.get("bonus") if isinstance(suit.get("bonus"), dict) else {}
        group_name = _normalize_equipment_group_name(_first_text(suit, "groupName"))
        group = groups.get(group_name)
        if group is None or not bonus:
            continue
        levels = [
            level
            for level in _unwrap_fz_list(bonus.get("levels"), "levels", "items", "list")
            if isinstance(level, dict)
        ]
        selected = levels[-1] if levels else {}
        values = selected.get("values") if isinstance(selected.get("values"), dict) else {}
        description = _format_fz_template(
            _first_text(bonus, "description", "desc"),
            values,
        )
        description = re.sub(r"^\s*\d+\s*件套组效果\s*[：:]\s*", "", description)
        group.suit_name = _first_text(suit, "suitName") or _first_text(bonus, "name")
        group.suit_required_count = _to_int(_first_value(suit, "equipCnt", "requiredCount"))
        group.suit_effect_description = description


def build_fz_operator_catalog_view(
    raw: dict[str, Any],
    element_filter: str = "",
    profession_filter: str = "",
) -> OperatorCatalogView:
    article = raw.get("article") or {}
    entries = _fz_overview_entries(raw)
    if not entries:
        raise ValueError("FZ operator roster does not match the supported catalog schema")

    element_filter = clean_text(element_filter)
    profession_filter = clean_text(profession_filter)
    grouped: dict[str, dict[str, list[OperatorCatalogItemView]]] = {}
    element_meta: dict[str, tuple[str, str]] = {}
    profession_icons: dict[str, str] = {}
    for entry in entries:
        name = _first_text(entry, "name")
        title = _first_text(entry, "title") or (f"干员/{name}" if name else "")
        element = _first_text(entry, "element") or "未知元素"
        profession = _first_text(entry, "profession") or "未知职业"
        if not name or not title:
            continue
        if element_filter and element != element_filter:
            continue
        if profession_filter and profession != profession_filter:
            continue
        element_icon_url = (element_icon_urls(element) or ("",))[0]
        profession_icon_url = (profession_icon_urls(profession) or ("",))[0]
        weapon_type_icon_url = (weapon_type_icon_urls(_first_text(entry, "weaponType")) or ("",))[0]
        item = OperatorCatalogItemView(
            name=name,
            title=title,
            operator_id=str(entry.get("charId") or ""),
            english_name=_first_text(entry, "nameEn", "englishName"),
            rarity=_to_int(entry.get("rarity")),
            element=element,
            element_color=_first_text(entry, "elementColor") or "#888888",
            profession=profession,
            weapon_type=_first_text(entry, "weaponType"),
            icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
            element_icon_url=element_icon_url,
            profession_icon_url=profession_icon_url,
            weapon_type_icon_url=weapon_type_icon_url,
        )
        grouped.setdefault(element, {}).setdefault(profession, []).append(item)
        element_meta.setdefault(element, (item.element_color, element_icon_url))
        profession_icons.setdefault(profession, profession_icon_url)

    elements: list[OperatorCatalogElementView] = []
    for element, professions in grouped.items():
        profession_views: list[OperatorCatalogProfessionView] = []
        for profession, items in professions.items():
            items.sort(key=lambda item: (-item.rarity, item.name))
            profession_views.append(
                OperatorCatalogProfessionView(profession, profession_icons.get(profession, ""), items)
            )
        profession_views.sort(key=lambda group: (OPERATOR_PROFESSION_ORDER.get(group.name, 99), group.name))
        color, icon_url = element_meta.get(element, ("#888888", ""))
        elements.append(OperatorCatalogElementView(element, color, icon_url, profession_views))
    elements.sort(key=lambda group: (OPERATOR_ELEMENT_ORDER.get(group.name, 99), group.name))
    total_count = sum(len(profession.items) for element in elements for profession in element.professions)
    if (element_filter or profession_filter) and not elements:
        raise ValueError(f"FZ operator catalog filter not found: {element_filter} {profession_filter}".strip())
    if element_filter and profession_filter:
        title = f"{element_filter} · {profession_filter}"
    elif element_filter:
        title = f"{element_filter}干员"
    elif profession_filter:
        title = f"{profession_filter}干员"
    else:
        title = "全部干员"
    return OperatorCatalogView(
        title=title,
        elements=elements,
        total_count=total_count,
        element_filter=element_filter,
        profession_filter=profession_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_fz_weapon_catalog_view(
    raw: dict[str, Any],
    weapon_type_filter: str = "",
) -> WeaponCatalogView:
    article = raw.get("article") or {}
    entries = _fz_overview_entries(raw)
    if not entries:
        raise ValueError("FZ weapon roster does not match the supported catalog schema")

    weapon_type_filter = clean_text(weapon_type_filter)
    grouped: dict[str, list[WeaponCatalogItemView]] = {}
    type_icons: dict[str, str] = {}
    for entry in entries:
        name = _first_text(entry, "name")
        title = _first_text(entry, "title") or (f"武器/{name}" if name else "")
        weapon_type = _first_text(entry, "weaponType") or "未知武器"
        if not name or not title:
            continue
        if weapon_type_filter and weapon_type != weapon_type_filter:
            continue
        type_icon_url = (weapon_type_icon_urls(weapon_type) or ("",))[0]
        grouped.setdefault(weapon_type, []).append(
            WeaponCatalogItemView(
                name=name,
                title=title,
                weapon_id=str(entry.get("weaponId") or ""),
                english_name=_first_text(entry, "nameEn", "englishName"),
                rarity=_to_int(entry.get("rarity")),
                weapon_type=weapon_type,
                max_level=_to_int(_first_value(entry, "maxLv", "maxLevel")),
                max_atk=_to_int(_first_value(entry, "maxAtk", "attack")) or "--",
                icon_url=_fz_asset_raw_url(_first_text(entry, "iconUrl", "icon")),
                weapon_type_icon_url=type_icon_url,
                substrate_icon_url=_fz_asset_raw_url(_first_text(entry, "substrateIconUrl")),
                terms_main=[clean_text(value) for value in (entry.get("termsMain") or []) if clean_text(value)],
                terms_sub=[clean_text(value) for value in (entry.get("termsSub") or []) if clean_text(value)],
                terms_skill=[clean_text(value) for value in (entry.get("termsSkill") or []) if clean_text(value)],
            )
        )
        type_icons.setdefault(weapon_type, type_icon_url)

    groups: list[WeaponCatalogGroupView] = []
    for weapon_type, items in grouped.items():
        items.sort(key=lambda item: (-item.rarity, -(_to_int(item.max_atk)), item.name))
        groups.append(WeaponCatalogGroupView(weapon_type, type_icons.get(weapon_type, ""), items))
    groups.sort(key=lambda group: (WEAPON_TYPE_ORDER.get(group.name, 99), group.name))
    total_count = sum(len(group.items) for group in groups)
    if weapon_type_filter and not groups:
        raise ValueError(f"FZ weapon type not found: {weapon_type_filter}")
    return WeaponCatalogView(
        title=f"{weapon_type_filter}武器" if weapon_type_filter else "全部武器",
        groups=groups,
        total_count=total_count,
        weapon_type_filter=weapon_type_filter,
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def _normalize_equipment_group_name(name: str) -> str:
    name = clean_text(name)
    if name in INDEPENDENT_EQUIPMENT_GROUP_NAMES or "独立装备组" in name or "独立装备套组" in name:
        return "独立装备套组"
    return name


def _fz_equipment_roster_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _fz_overview_entries(raw)


def _fz_overview_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        roster = attrs.get("roster") if isinstance(attrs, dict) else None
        if isinstance(roster, dict) and isinstance(roster.get("entries"), list):
            return [entry for entry in roster["entries"] if isinstance(entry, dict)]
    return []


def _format_equipment_stat(value: Any, is_percent: bool) -> str:
    number = _to_float(value)
    if number is None:
        return clean_text(value) or "--"
    if is_percent:
        if abs(number) <= 2:
            number *= 100
        return f"{number:.1f}".rstrip("0").rstrip(".") + "%"
    return _format_plain_number(number)


def _equipment_acquisition(materials: dict[str, Any]) -> str:
    unlock_type = str(materials.get("unlockType") or "").strip()
    return {
        "EquipFormulaChest": "装备制造",
        "DomainShop": "地区商店",
    }.get(unlock_type, clean_text(unlock_type) or "未知方式")


def _fz_template_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        if isinstance(attrs, dict) and isinstance(attrs.get("hero"), dict):
            return attrs
    return {}


# ----- 蚀刻章/奖章（FZ 单件模板「蚀刻章·单件档案」，数据在 attrs.entry） -----

def _fz_medal_entry_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    """返回含 ``entry`` 的模板 attrs（区别于干员/装备的 ``hero``）。"""
    content = ((raw.get("revision") or {}).get("contentJson") or {}).get("content") or []
    for node in content:
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs") or {}
        if isinstance(attrs, dict) and isinstance(attrs.get("entry"), dict):
            return attrs
    return {}


def _derive_medal_levels(entry: dict[str, Any]) -> tuple[int, bool]:
    """从 ``entry.levels`` 推导 (max_level, can_be_upgraded)。

    FZ 单件档案没有 maxLevel/canBeUpgraded 直字段，按 levels 数组推导：
    最高等级 = levels 末项的 level（缺省回退 initLevel / len）；可升级 = levels 多于 1 级。
    """
    levels = _ordered_fz_levels(_unwrap_fz_list(entry.get("levels"), "levels", "items", "list"))
    if levels:
        max_level = _to_int(_first_value(levels[-1], "level", "lv")) or len(levels)
        can_upgrade = len(levels) > 1
    else:
        max_level = _to_int(_first_value(entry, "initLevel", "level"))
        can_upgrade = False
    if max_level <= 0:
        max_level = 1
    return max_level, can_upgrade


# ----- F2：玩家蚀刻章进度（森空岛 card/detail）归一化 -----

def _norm_medal_name(name: str) -> str:
    """规范化奖章名用于跨源关联：去全部空白 + 去首尾中英文引号。

    FZ 用 ``achv_`` 语义 id、森空岛用 hex 哈希 id，命名空间不同（2026-07-27 实测），
    无法按 id 关联；两源 name 均为中文奖章名，按规范化 name 关联（实测 135/140 命中）。
    """
    return (
        "".join(str(name).split())
        .strip('"')
        .strip("'")
        .strip("“”‘’")
    )


def _parse_player_medal_progress(
    raw: dict[str, Any],
) -> tuple[dict[str, MedalProgressView], dict[str, MedalProgressView]]:
    """从森空岛 card/detail 响应提取奖章进度，返回 ``(按 hex id 索引, 按规范化 name 索引)``。

    路径 ``data.detail.achieve.achieveMedals[]``；每枚含 ``achievementData.id`` /
    ``name`` / ``level`` / ``isPlated``。只有**已获得**的奖章会出现在列表中——不在列表即未获得。

    **关联键（2026-07-28 实测 115/115 命中）**：森空岛 ``achievementData.id`` 是
    ``md5(游戏 achv_id)``（32 位 hex），与 FZ 的 ``achv_*`` 经 md5 一一对应，故**主键用 hex id**；
    name 索引仅作兜底，用于极少数 FZ 缺 ``achv_id`` 的条目（命名滞后会使 name 关联误判，
    详见 ``docs/skland_medal_id_mapping.md``）。
    """
    achieve = (((raw.get("data") or {}).get("detail") or {}).get("achieve") or {})
    by_hex: dict[str, MedalProgressView] = {}
    by_name: dict[str, MedalProgressView] = {}
    for item in achieve.get("achieveMedals") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("achievementData") or {}
        name = _first_text(meta, "name")
        hex_id = _first_text(meta, "id", "achievementId")
        if not (name or hex_id):
            continue
        plated_raw = item.get("isPlated")
        plated = plated_raw is True or (
            isinstance(plated_raw, str) and plated_raw.strip().lower() in ("true", "1", "yes")
        )
        init_level = _to_int(meta.get("initLevel")) or 0
        view = MedalProgressView(
            medal_id=hex_id,
            level=_to_int(item.get("level")),
            plated=plated,
            init_level=init_level,
            plated_icon=_first_text(meta, "platedIcon") or "",
        )
        if hex_id:
            by_hex[hex_id] = view
        if name:
            by_name[_norm_medal_name(name)] = view
    return by_hex, by_name


def build_fz_medal_item(
    detail_raw: dict[str, Any],
    roster_entry: dict[str, Any] | None = None,
) -> MedalItemView | None:
    """一条 FZ 蚀刻章详情（含 revision）→ MedalItemView；name 缺失返回 None。"""
    attrs = _fz_medal_entry_attrs(detail_raw)
    entry = attrs.get("entry") if isinstance(attrs.get("entry"), dict) else {}
    roster_entry = roster_entry if isinstance(roster_entry, dict) else {}
    name = _first_text(entry, "name") or _first_text(roster_entry, "name")
    if not name:
        return None
    medal_id = _first_text(entry, "id", "medalId", "achvId") or _first_text(roster_entry, "title")
    max_level, can_upgrade = _derive_medal_levels(entry)
    raw_plate = _first_value(entry, "canBePlated")
    can_be_plated = raw_plate is True or (
        isinstance(raw_plate, str) and raw_plate.strip().lower() in ("true", "1", "yes")
    )
    return MedalItemView(
        medal_id=medal_id,
        name=name,
        category_name=_first_text(entry, "categoryName") or _first_text(roster_entry, "categoryName"),
        group_name=_first_text(entry, "groupName") or _first_text(roster_entry, "groupName"),
        init_level=_to_int(_first_value(entry, "initLevel", "level")),
        max_level=max_level,
        can_be_upgraded=can_upgrade,
        can_be_plated=can_be_plated,
        order=_to_int(entry.get("order")),
        icon_url=_fz_asset_raw_url(
            _first_text(entry, "iconUrl", "icon") or _first_text(roster_entry, "icon")
        ),
        description=_clean_fz_rich_text(
            _first_value(entry, "desc", "description") or _first_value(roster_entry, "desc")
        ),
    )


def build_fz_medal_snapshot_view(
    roster_raw: dict[str, Any],
    detail_raws: list[dict[str, Any]],
    *,
    fetched_at: int = 0,
    version_label: str | None = None,
) -> MedalSnapshotView:
    """聚合 FZ roster（名称发现）+ 各详情（等级/镀层）→ 全量奖章快照。"""
    article = roster_raw.get("article") or {}
    roster_by_title: dict[str, dict[str, Any]] = {}
    for entry in _fz_overview_entries(roster_raw):
        title = _first_text(entry, "title")
        if title:
            roster_by_title[title] = entry

    medals: list[MedalItemView] = []
    for detail in detail_raws:
        if not isinstance(detail, dict):
            continue
        detail_article = detail.get("article") or {}
        roster_entry = roster_by_title.get(_first_text(detail_article, "title"))
        item = build_fz_medal_item(detail, roster_entry)
        if item is not None:
            medals.append(item)

    medals.sort(key=lambda medal: (medal.category_name, medal.order, medal.name))

    level_counts: dict[int, int] = {}
    category_counts: dict[str, int] = {}
    platable_count = 0
    upgradable_count = 0
    for medal in medals:
        level_counts[medal.max_level] = level_counts.get(medal.max_level, 0) + 1
        if medal.category_name:
            category_counts[medal.category_name] = category_counts.get(medal.category_name, 0) + 1
        if medal.can_be_plated:
            platable_count += 1
        if medal.can_be_upgraded:
            upgradable_count += 1

    return MedalSnapshotView(
        medals=medals,
        version=version_label or str(article.get("updatedAt") or "")[:10],
        fetched_at=fetched_at,
        source="fz",
        total_count=len(medals),
        level_counts=level_counts,
        platable_count=platable_count,
        upgradable_count=upgradable_count,
        category_counts=category_counts,
    )


# ----- 蚀刻章/奖章（AKEData TableCfg 源：权威主源，CDN 稳定） -----


def _i18n_text(i18n: dict[str, Any], obj: Any) -> str:
    """``{id, text}`` → 按 text-id 在 I18nTextTable 解析出的中文文本。"""
    return localized_text(obj, translations=i18n)


def _tier_text(d: dict, lv, default: str = "") -> str:
    """按等级取档位文本，兼容 int/str key（snapshot JSON round-trip 后 key 为 str）。"""
    return d.get(lv) or d.get(str(lv)) or default


def build_akedata_medal_snapshot(
    achievement_table: dict[str, Any],
    type_table: dict[str, Any],
    i18n: dict[str, Any],
    *,
    fetched_at: int = 0,
    version_label: str | None = None,
) -> MedalSnapshotView:
    """聚合 AKEData AchievementTable + TypeTable + I18nTextTable → 全量奖章快照。

    AKEData 是游戏客户端 TableCfg：``achv_*`` id 直接当 ``medal_id``（与森空岛 hex 经 md5
    关联），``canBeUpgraded``/``canBePlated`` 是直字段，``levelInfos`` 给逐档 ``achieveLevel``，
    名字/描述/分类名按 text-id 在 ``i18n`` 解析。图标路径规则：
    ``<ICON_BASE>/<achvId>_lv<maxLevel:02d>.png``（与站点 ``v3-table-data.js`` 一致）。
    """
    # groupId -> (categoryPriority, category_name, group_name)
    group_map: dict[str, tuple[int, str, str]] = {}
    for _type_key, tv in (type_table or {}).items():
        if not isinstance(tv, dict):
            continue
        priority = _to_int(tv.get("categoryPriority"))
        cat_name = _i18n_text(i18n, tv.get("categoryName"))
        for group in tv.get("achievementGroupData") or []:
            if not isinstance(group, dict):
                continue
            gid = str(group.get("groupId") or "")
            if gid:
                group_map[gid] = (priority, cat_name, _i18n_text(i18n, group.get("groupName")))

    medals: list[MedalItemView] = []
    for achv_id, entry in (achievement_table or {}).items():
        if not isinstance(entry, dict):
            continue
        name = _i18n_text(i18n, entry.get("name"))
        if not name:
            continue
        priority, cat_name, group_name = group_map.get(
            str(entry.get("groupId") or ""), (999, "", "")
        )
        level_infos = entry.get("levelInfos") or {}
        achieve_levels: list[int] = []
        tier_desc: dict[int, str] = {}
        tier_cond: dict[int, str] = {}
        for li in level_infos.values():
            if not isinstance(li, dict):
                continue
            al = _to_int(li.get("achieveLevel"))
            if al <= 0:
                continue
            achieve_levels.append(al)
            tier_desc[al] = _i18n_text(i18n, li.get("completeDesc"))
            seen: set[str] = set()
            cond_texts: list[str] = []
            for c in li.get("conditions") or []:
                if not isinstance(c, dict):
                    continue
                t = _i18n_text(i18n, c.get("desc"))
                if t and t not in seen:
                    seen.add(t)
                    cond_texts.append(t)
            tier_cond[al] = "；".join(cond_texts)
        achieve_levels.sort()
        init_level = _to_int(entry.get("initLevel")) or (achieve_levels[0] if achieve_levels else 0)
        max_level = achieve_levels[-1] if achieve_levels else (init_level or 1)
        plate_seen: set[str] = set()
        plate_texts: list[str] = []
        for c in entry.get("plateConditions") or []:
            if not isinstance(c, dict):
                continue
            t = _i18n_text(i18n, c.get("desc"))
            if t and t not in plate_seen:
                plate_seen.add(t)
                plate_texts.append(t)
        medals.append(
            MedalItemView(
                medal_id=achv_id,
                name=name,
                category_name=cat_name,
                group_name=group_name,
                init_level=init_level,
                max_level=max_level,
                can_be_upgraded=bool(entry.get("canBeUpgraded")),
                can_be_plated=bool(entry.get("canBePlated")),
                order=_to_int(entry.get("order")),
                icon_url=f"{AKEDATA_ICON_BASE}/{achv_id}_lv{max_level:02d}.png",
                description=_tier_text(tier_desc, init_level),
                condition=_tier_text(tier_cond, init_level),
                plate_condition="；".join(plate_texts),
                tier_desc=tier_desc,
                tier_cond=tier_cond,
            )
        )

    medals.sort(key=lambda medal: (medal.category_name, medal.order, medal.name))

    level_counts: dict[int, int] = {}
    category_counts: dict[str, int] = {}
    platable_count = 0
    upgradable_count = 0
    for medal in medals:
        level_counts[medal.max_level] = level_counts.get(medal.max_level, 0) + 1
        if medal.category_name:
            category_counts[medal.category_name] = category_counts.get(medal.category_name, 0) + 1
        if medal.can_be_plated:
            platable_count += 1
        if medal.can_be_upgraded:
            upgradable_count += 1

    return MedalSnapshotView(
        medals=medals,
        version=version_label or "",
        fetched_at=fetched_at,
        source="akedata",
        total_count=len(medals),
        level_counts=level_counts,
        platable_count=platable_count,
        upgradable_count=upgradable_count,
        category_counts=category_counts,
    )


def _fz_species_info(attrs: dict[str, Any]) -> tuple[str, str]:
    hero = attrs.get("hero") if isinstance(attrs.get("hero"), dict) else {}
    species = _first_text(hero, "species", "race")
    if species:
        return "种族", species
    meta_species = _fz_hero_meta_value(hero, "种族", "race", "species")
    if meta_species:
        return "种族", meta_species
    archive_species = _fz_archive_species(attrs.get("archive"))
    if archive_species:
        return "种族", archive_species
    faction = _first_text(hero, "faction", "camp", "organization") or _fz_hero_meta_value(hero, "所属", "阵营", "组织")
    if faction:
        return "所属", faction
    return "种族", "未知种族"


def _fz_archive_species(raw: Any) -> str:
    for text in _iter_fz_archive_text(raw):
        match = re.search(r"【种族】[^\S\r\n]*([^【\n\r]+)", text)
        if match:
            species = clean_text(match.group(1)).strip()
            if species:
                return species
    return ""


def _iter_fz_archive_text(raw: Any):
    if isinstance(raw, dict):
        for key in ("body", "text", "content", "desc", "description", "recordDesc"):
            value = raw.get(key)
            if isinstance(value, str):
                yield value
        for key in ("archive", "items", "list", "records"):
            yield from _iter_fz_archive_text(raw.get(key))
    elif isinstance(raw, list):
        for item in raw:
            yield from _iter_fz_archive_text(item)


def _fz_hero_meta_value(hero: dict[str, Any], *labels: str) -> str:
    wanted = {label.strip().lower() for label in labels if label.strip()}
    for item in hero.get("meta") or []:
        if not isinstance(item, dict):
            continue
        label = clean_text(_first_value(item, "label", "name", "title", "key")).strip().lower()
        if label in wanted:
            value = clean_text(_first_value(item, "value", "text", "content"))
            if value:
                return value
    return ""


def _build_fz_operator_skills(raw: Any) -> list[SkillView]:
    skills = _unwrap_fz_list(raw, "skills", "items", "list")
    result: list[SkillView] = []
    for index, item in enumerate(skills, 1):
        if not isinstance(item, dict):
            continue
        title = _first_text(item, "name", "title", "skillName")
        if not title:
            continue
        all_levels = _ordered_fz_levels(_unwrap_fz_list(_first_value(item, "levels", "levelData", "records"), "levels", "items", "records"))
        raw_levels = all_levels[-4:]
        selected_positions = [position for position, level in enumerate(all_levels) if any(level is selected for selected in raw_levels)]
        param_values = _fz_param_table_values(
            item.get("paramTable"),
            selected_positions,
            _fz_skill_condition_names(item),
        )
        levels = _build_fz_skill_levels(raw_levels, param_values)
        best_level = raw_levels[-1] if raw_levels else {}
        result.append(
            SkillView(
                skill_id=str(_first_value(item, "id", "skillId") or f"fz_skill_{index}"),
                title=title,
                icon_id=_fz_icon_url(item) or _first_text(item, "iconId"),
                category=_first_text(item, "category", "type") or _fz_skill_category(index),
                description=_format_fz_template(
                    _first_text(item, "description", "desc"),
                    _first_value(best_level, "values", "blackboard", "params"),
                ),
                form_descriptions=_build_fz_skill_form_descriptions(
                    item,
                    _first_value(best_level, "values", "blackboard", "params"),
                ),
                levels=levels,
            )
        )
    return result


def _build_fz_skill_form_descriptions(item: dict[str, Any], values: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for condition in _unwrap_fz_list(item.get("conditions"), "conditions", "items", "list"):
        if not isinstance(condition, dict):
            continue
        name = _first_text(condition, "name", "title", "label")
        raw_desc = _first_value(condition, "postDesc", "description", "desc")
        if not name or not raw_desc:
            continue
        raw_desc = re.sub(r"(?m)^\s*-\s*", "", str(raw_desc))
        description = _format_fz_template(raw_desc, values)
        description = re.sub(
            rf"(?:<[@#][A-Za-z0-9_.-]+>)?{re.escape(name)}(?:</>)?\s*[：:]\s*",
            "",
            description,
            count=1,
        ).strip()
        if description:
            result.append((name, description))
    return result


def _build_fz_skill_levels(raw: Any, param_values: list[dict[str, str]] | None = None) -> list[SkillLevelView]:
    levels: list[SkillLevelView] = []
    for index, item in enumerate(_select_fz_levels(_unwrap_fz_list(raw, "levels", "items", "records")), 1):
        if not isinstance(item, dict):
            continue
        level = _to_int(_first_value(item, "level", "lv")) or index
        values = _first_value(item, "values", "blackboard", "params")
        if not isinstance(values, dict):
            values = {}
        mapped_values = _map_fz_skill_values(values)
        if param_values and index - 1 < len(param_values):
            param_row = param_values[index - 1]
            _drop_generic_fz_metrics(mapped_values, param_row)
            mapped_values.update(param_row)
        cooldown = mapped_values.get("冷却") or str(_first_value(item, "cooldown", "coolDown", "cd") or "")
        if not cooldown or cooldown == "--":
            cooldown = "--"
        cost = mapped_values.get("所需能量") or str(_first_value(item, "cost", "costValue", "sp") or "")
        if not cost or cost == "--":
            cost = "--"
        levels.append(
            SkillLevelView(
                label=_level_label(level),
                level=level,
                values=mapped_values,
                cooldown=cooldown,
                cost=cost,
                charge=str(_first_value(item, "charge", "maxChargeTime") or "--"),
                description=_format_fz_template(_first_text(item, "description", "desc"), values),
            )
        )
    return levels


def _drop_generic_fz_metrics(mapped_values: dict[str, str], param_row: dict[str, str]) -> None:
    for generic_name in tuple(mapped_values):
        if any(fz_metric_replaces_generic(generic_name, specific_name) for specific_name in param_row):
            mapped_values.pop(generic_name, None)


def _build_fz_effects(raw: Any, kind: str) -> list[EffectView]:
    effects: list[EffectView] = []
    talent_by_title: dict[str, tuple[tuple[int, int], EffectView]] = {}
    for index, item in enumerate(_unwrap_fz_list(raw, "talents", "potentials", "items", "list"), 1):
        if not isinstance(item, dict):
            continue
        title = _first_text(item, "name", "title")
        values = _first_value(item, "values", "blackboard", "params")
        description = _format_fz_template(_first_text(item, "description", "desc", "effect"), values)
        if not title and not description:
            continue
        level = _to_int(_first_value(item, "level", "potentialLevel", "rank")) or index
        view = EffectView(
            effect_id=str(_first_value(item, "id", "effectId") or f"fz_{kind}_{index}"),
            title=_fz_effect_title(kind, title, level, index),
            description=description,
            kind="天赋" if kind == "talent" else "潜能" if kind == "potential" else kind,
            icon_url=_fz_icon_url(item) or _fz_asset_raw_url(_first_text(item, "iconUrl", "icon")),
        )
        if kind == "talent":
            dedupe_key = title or view.title
            rank = (_to_int(_first_value(item, "level", "rank")), _to_int(_first_value(item, "unlockStage", "stage")))
            previous = talent_by_title.get(dedupe_key)
            if previous is None or rank >= previous[0]:
                talent_by_title[dedupe_key] = (rank, view)
            continue
        effects.append(view)
    if kind == "talent":
        return [record[1] for record in talent_by_title.values()]
    return effects


def _fz_icon_url(item: dict[str, Any]) -> str:
    icon = item.get("icon")
    if isinstance(icon, dict):
        glyph = icon.get("glyph")
        if isinstance(glyph, dict):
            glyph_url = _first_text(glyph, "url", "src")
            if glyph_url:
                return _fz_asset_raw_url(glyph_url)
        direct = _first_text(icon, "url", "src", "iconUrl")
        if direct:
            return _fz_asset_raw_url(direct)
    return _fz_asset_raw_url(_first_text(item, "iconUrl", "avatarUrl"))


def _fz_skill_category(index: int) -> str:
    return {
        1: "普攻",
        2: "战技",
        3: "连携技",
        4: "终结技",
    }.get(index, "技能")


def _select_fz_levels(levels: list[Any]) -> list[dict[str, Any]]:
    return _ordered_fz_levels(levels)[-4:]


def _ordered_fz_levels(levels: list[Any]) -> list[dict[str, Any]]:
    records = [item for item in levels if isinstance(item, dict)]
    records.sort(key=lambda item: _to_int(_first_value(item, "level", "lv")))
    return records


def _fz_skill_condition_names(item: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for condition in _unwrap_fz_list(item.get("conditions"), "conditions", "items", "list"):
        if not isinstance(condition, dict):
            continue
        condition_id = _first_text(condition, "id", "conditionId", "key")
        name = _first_text(condition, "name", "title", "label")
        if condition_id and name:
            result[condition_id] = name
    return result


def _fz_param_table_values(
    raw: Any,
    selected_positions: list[int],
    condition_names: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = [{} for _ in selected_positions]
    if not isinstance(raw, dict) or not selected_positions:
        return result
    rows = _unwrap_fz_list(raw.get("rows") or raw, "rows", "items", "list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = _map_fz_param_label(_first_text(row, "label", "name", "title", "key"))
        if not label:
            continue
        condition_id = _first_text(row, "conditionId", "condition", "formId")
        condition_name = (condition_names or {}).get(condition_id, "")
        if condition_name:
            label = f"{condition_name}{label}"
        raw_values = _first_value(row, "values", "valueList", "data", "columns")
        for out_index, source_index in enumerate(selected_positions):
            value = _fz_param_value_at(raw_values, source_index)
            if value in (None, ""):
                continue
            result[out_index][label] = _format_fz_metric_value(value, percent=_fz_param_is_percent(label))
    return result


def _fz_param_value_at(values: Any, index: int) -> Any:
    if isinstance(values, list):
        if index >= len(values):
            return None
        value = values[index]
        if isinstance(value, dict):
            return _first_value(value, "value", "text", "display", "content")
        return value
    if isinstance(values, dict):
        value = _first_value(values, str(index), str(index + 1), f"Lv{index + 1}", f"lv{index + 1}")
        if isinstance(value, dict):
            return _first_value(value, "value", "text", "display", "content")
        return value
    return None


def _map_fz_param_label(label: str) -> str:
    label = clean_text(label)
    aliases = {
        "伤害倍率": "攻击倍率",
        "攻击倍率": "攻击倍率",
        "失衡值": "失衡值",
        "所需终结技能量": "所需能量",
        "所需能量": "所需能量",
        "冷却": "冷却",
        "冷却时间": "冷却",
        "技力消耗": "技力消耗",
        "获得终结技能量": "获得终结技能量",
        "持续时间": "持续时间",
    }
    return aliases.get(label, label)


def _fz_param_is_percent(label: str) -> bool:
    return "倍率" in label or "比例" in label


def _map_fz_skill_values(values: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}

    def add(label: str, *keys: str, percent: bool = False) -> None:
        for key in keys:
            value = _case_insensitive_get(values, key)
            if value not in (None, ""):
                result[label] = _format_fz_metric_value(value, percent=percent)
                return

    add("攻击倍率", "display_atk_scale", "atk_scale", percent=True)
    add("失衡值", "display_poise", "poise")
    add("持续时间", "duration")
    add("技力", "usp")
    add("冷却", "cooldown", "CoolDown", "coolDown")
    return result


def _format_fz_metric_value(value: Any, *, percent: bool = False) -> str:
    text = clean_text(value)
    if not text:
        return "--"
    if "%" in text:
        return text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return text
    if percent:
        if abs(number) <= 2:
            number *= 100
        return f"{number:.0f}%"
    return _format_plain_number(number)


def _format_fz_template(desc: Any, values: Any) -> str:
    return _clean_fz_rich_text(_substitute_fz_placeholders(desc, values))


def _substitute_fz_placeholders(desc: Any, values: Any) -> str:
    value_map = _normalized_value_map(values if isinstance(values, dict) else {})

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key_expr, _, fmt = expr.partition(":")
        value = _eval_fz_template_expr(key_expr.strip(), value_map)
        return _format_template_value(value, fmt)

    return re.sub(r"\{([^{}]+)\}", replace, str(desc or ""))


def _clean_fz_rich_text(value: Any) -> str:
    text = str(value or "")
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(r"</>|<[@#][A-Za-z0-9_.-]+>", protect, text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(
        r"\x00(\d+)\x00",
        lambda match: protected[int(match.group(1))],
        text,
    )
    text = text.replace("\\n", "\n")
    return " ".join(text.split())


def _normalized_value_map(values: dict[str, Any]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for key, value in values.items():
        number = _to_float(value)
        if number is None:
            continue
        normalized[str(key).strip().lower()] = number
    return normalized


def _eval_fz_template_expr(expr: str, values: dict[str, float]) -> float | None:
    expr = expr.strip().lower()
    if not expr:
        return None
    unary_match = re.fullmatch(r"([+-])([a-z0-9_]+)", expr)
    if unary_match:
        value = _fz_template_operand(unary_match.group(2), values)
        if value is None:
            return None
        return -value if unary_match.group(1) == "-" else value
    match = re.fullmatch(
        r"(-?\d+(?:\.\d+)?|[a-z0-9_]+)([+\-*/])(-?\d+(?:\.\d+)?|[a-z0-9_]+)",
        expr,
    )
    if match:
        left = _fz_template_operand(match.group(1), values)
        right = _fz_template_operand(match.group(3), values)
        if left is None or right is None:
            return None
        operator = match.group(2)
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        return None if right == 0 else left / right
    value = values.get(expr)
    if value is not None:
        return value
    return values.get(_alias_key(expr).lower())


def _fz_template_operand(operand: str, values: dict[str, float]) -> float | None:
    try:
        return float(operand)
    except ValueError:
        value = values.get(operand)
        if value is not None:
            return value
        return values.get(_alias_key(operand).lower())


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, str) and value.strip().endswith("%"):
            return float(value.strip().removesuffix("%")) / 100
        return float(value)
    except (TypeError, ValueError):
        return None


def _case_insensitive_get(values: dict[str, Any], key: str) -> Any:
    if key in values:
        return values[key]
    lowered = key.lower()
    for raw_key, value in values.items():
        if str(raw_key).lower() == lowered:
            return value
    return None


def _fz_effect_title(kind: str, title: str, level: int, index: int) -> str:
    if kind == "potential":
        return f"P{level or index} {title or '潜能'}"
    return title or f"天赋 {index}"


def _unwrap_fz_list(raw: Any, *keys: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_text(data: dict[str, Any], *keys: str) -> str:
    value = _first_value(data, *keys)
    text = localized_text(value)
    if text:
        return text
    if isinstance(value, dict):
        return localized_text(_first_value(value, "url"))
    return ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := localized_text(item))]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，/、\s]+", value) if item.strip()]
    return []


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _level_label(level: int) -> str:
    for expected, label in LEVEL_COLUMNS:
        if level == expected:
            return label
    return f"Lv{level}"


def _strip_title_prefix(query: str, prefix: str) -> str:
    query = str(query or "").strip()
    if query.startswith(prefix):
        return query[len(prefix):]
    return query


def build_weapon_view(raw: dict[str, Any], richtext: dict[str, Any] | None = None) -> WeaponView:
    article = raw.get("article") or {}
    revision = raw.get("revision") or {}
    content = ((revision.get("contentJson") or {}).get("content") or [{}])[0]
    attrs = content.get("attrs") or {}
    hero = attrs.get("hero") or {}
    stats = attrs.get("stats") or {}
    skills = (attrs.get("skills") or {}).get("skills") or []
    title = _first_text(article, "title")
    name = _first_text(hero, "name") or title.split("/", 1)[-1] or ""
    max_level = int(hero.get("maxLv") or 0)
    max_atk = next((row.get("atk") for row in stats.get("curve", []) if row.get("lv") == max_level), None)
    if max_atk is None:
        max_atk = next((row.get("atk") for row in reversed(stats.get("curve", []) or []) if row.get("atk") is not None), "--")
    richtext = richtext or {}
    return WeaponView(
        name=name,
        slug=_weapon_slug(title or name),
        title=title or f"武器/{name}",
        weapon_id=_fz_weapon_id(skills),
        source_name="api.fz.wiki",
        english_name=_first_text(hero, "nameEn"),
        rarity=int(hero.get("rarity") or 0),
        weapon_type=_first_text(hero, "weaponType") or "未知武器",
        max_level=max_level,
        max_atk=max_atk,
        icon_url=_fz_asset_raw_url(hero.get("iconUrl")),
        skills=[_build_weapon_skill(skill) for skill in skills],
        rich_text_styles=richtext.get("RICH_TEXT_STYLES") or {},
        rich_text_links=_fz_rich_text_links(richtext),
        source_version=str(article.get("updatedAt") or "")[:10],
    )


def build_warfarin_weapon_view(raw: dict[str, Any]) -> WeaponView:
    meta = raw.get("meta") or {}
    data = raw.get("data") or {}
    refs = raw.get("refs") or {}
    basic = data.get("weaponBasicTable") or {}
    item = data.get("itemTable") or {}
    upgrade = data.get("weaponUpgradeTemplateTable") or {}
    skill_table = data.get("skillPatchTable") or {}

    name = _first_text(meta, "name") or _first_text(item, "name")
    slug = _first_text(meta, "slug") or _weapon_slug(name)
    max_level = int(basic.get("maxLv") or 0)
    max_atk = _warfarin_weapon_max_atk(upgrade.get("list") or [], max_level)
    weapon_type_id = str(basic.get("weaponType") or "")
    weapon_type = str((refs.get("weaponTypes") or {}).get(weapon_type_id) or _weapon_name(basic.get("weaponType")))

    return WeaponView(
        name=name,
        slug=slug,
        title=f"Warfarin/{slug}",
        weapon_id=str(basic.get("weaponId") or meta.get("id") or ""),
        source_name="Warfarin Wiki",
        english_name=_first_text(basic, "engName"),
        rarity=int(basic.get("rarity") or item.get("rarity") or 0),
        weapon_type=weapon_type,
        max_level=max_level,
        max_atk=max_atk,
        icon_url=_warfarin_weapon_icon_url(str(item.get("iconId") or basic.get("weaponId") or meta.get("id") or "")),
        skills=_build_warfarin_weapon_skills(basic.get("weaponSkillList") or [], skill_table),
        rich_text_styles=_warfarin_rich_text_styles(refs.get("richTextStyleTable") or {}),
        rich_text_links=refs.get("hyperlinkTextTable") or {},
        source_version=str(meta.get("version") or ""),
    )


def _warfarin_weapon_max_atk(rows: list[dict[str, Any]], max_level: int) -> int | str:
    if not rows:
        return "--"
    for row in rows:
        if int(row.get("weaponLv") or 0) == max_level and row.get("baseAtk") is not None:
            return int(row["baseAtk"])
    for row in reversed(rows):
        if row.get("baseAtk") is not None:
            return int(row["baseAtk"])
    return "--"


def _build_warfarin_weapon_skills(skill_ids: list[Any], skill_table: dict[str, Any]) -> list[WeaponSkillView]:
    skills: list[WeaponSkillView] = []
    ordered_ids = [str(skill_id) for skill_id in skill_ids]
    if not ordered_ids:
        ordered_ids = list(skill_table)
    for skill_id in ordered_ids:
        bundle = (skill_table.get(skill_id) or {}).get("SkillPatchDataBundle") or []
        if not bundle:
            continue
        first = bundle[0]
        skills.append(
            WeaponSkillView(
                title=clean_text(first.get("skillName")) or "技能",
                description=str(first.get("description") or ""),
                levels=[
                    WeaponSkillLevelView(
                        level=int(item.get("level") or index + 1),
                        values=_blackboard_values(item.get("blackboard") or []),
                    )
                    for index, item in enumerate(bundle)
                ],
            )
        )
    return skills


def _blackboard_values(rows: list[dict[str, Any]]) -> dict[str, float | int | str]:
    values: dict[str, float | int | str] = {}
    for row in rows:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        value = row.get("valueStr")
        if value in (None, ""):
            value = row.get("value")
        values[key] = value
    return values


def _warfarin_rich_text_styles(raw: dict[str, Any]) -> dict[str, dict]:
    styles: dict[str, dict] = {}
    for key, item in raw.items():
        style: dict[str, str] = {"id": str(item.get("id") or key)}
        pre_defs = item.get("preDef") or []
        pre = str(pre_defs[0] if pre_defs else "")
        color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
        if color_match:
            style["color"] = f"#{color_match.group(1)}"
        styles[str(key)] = style
    return styles


def _warfarin_weapon_icon_url(icon_id: str) -> str:
    urls = item_icon_urls(icon_id)
    return urls[0] if urls else ""


def _fz_weapon_id(skills: list[dict[str, Any]]) -> str:
    for skill in reversed(skills):
        skill_id = str(skill.get("skillId") or skill.get("id") or "")
        match = re.fullmatch(r"sk_(wpn_[a-z0-9_]+)", skill_id, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _match_weapon_record(view: WeaponView, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if view.weapon_id:
        for record in records:
            if str(record.get("id") or "").strip() == view.weapon_id:
                return record
    target_name = clean_text(view.name).casefold()
    if not target_name:
        return None
    for record in records:
        if clean_text(record.get("name")).casefold() == target_name:
            return record
    return None


def _unique_names(names: list[str]) -> list[str]:
    return list(dict.fromkeys(name for name in names if name))


def _weapon_slug(title: str) -> str:
    name = str(title or "").split("/", 1)[-1]
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", name).strip("-").lower()
    return slug or "weapon"


def _build_weapon_skill(raw: dict[str, Any]) -> WeaponSkillView:
    return WeaponSkillView(
        title=clean_text(raw.get("name")) or "技能",
        description=str(raw.get("description") or ""),
        levels=[
            WeaponSkillLevelView(
                level=int(item.get("level") or index + 1),
                values=item.get("values") or {},
            )
            for index, item in enumerate((raw.get("levels") or [])[:9])
        ],
    )


def clean_text(value: Any) -> str:
    text = localized_text(value)
    text = re.sub(r"<[@#]?[A-Za-z0-9_.-]+>", "", text)
    text = re.sub(r"</>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    return " ".join(text.split())


def static_resource_url(path: str) -> str:
    return static_sprite_url(path)


def _fz_asset_raw_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text or text.startswith("data:"):
        return text
    parts = urlsplit(text)
    if parts.netloc.lower() != FZ_ASSET_HOST:
        return text
    if parts.path.endswith("@raw"):
        return text
    return urlunsplit((parts.scheme, parts.netloc, f"{parts.path}@raw", parts.query, parts.fragment))


def skill_icon_url(icon_id: str) -> str:
    urls = skill_icon_urls(icon_id)
    return urls[0] if urls else ""


def _build_term_styles(refs: dict[str, Any]) -> dict[str, TermStyleView]:
    hyperlink_table = refs.get("hyperlinkTextTable") or {}
    rich_text_table = refs.get("richTextStyleTable") or {}
    result: dict[str, TermStyleView] = {}
    for term_id, entry in hyperlink_table.items():
        if not str(term_id).startswith("ba."):
            continue
        rich_text_id = str(entry.get("richTextId") or term_id)
        color, style_icon = _rich_text_visual(rich_text_table.get(rich_text_id) or rich_text_table.get(str(term_id)) or {})
        icon_url = static_resource_url(entry.get("iconPath") or style_icon)
        own_terms, referenced_terms = _term_names_from_entry(entry)
        for term in own_terms:
            current = result.get(term)
            result[term] = TermStyleView(term=term, color=color or (current.color if current else ""), icon_url=icon_url or (current.icon_url if current else ""))
        for term in referenced_terms - own_terms:
            current = result.get(term)
            if current:
                if not current.color and color:
                    result[term] = TermStyleView(term=term, color=color, icon_url=current.icon_url)
                continue
            result[term] = TermStyleView(term=term, color=color, icon_url="")
    return result


def _build_fz_term_styles(richtext: dict[str, Any]) -> dict[str, TermStyleView]:
    rich_text_table = richtext.get("RICH_TEXT_STYLES") or {}
    hyperlink_table = richtext.get("HYPERLINK_TEXTS") or {}
    result: dict[str, TermStyleView] = {}
    for tag_id, entry in rich_text_table.items():
        if not str(tag_id).startswith("ba."):
            continue
        color = _fz_rich_text_color(entry if isinstance(entry, dict) else {})
        if color:
            result[str(tag_id)] = TermStyleView(term=str(tag_id), color=color, icon_url="")
    for tag_id, entry in hyperlink_table.items():
        if not str(tag_id).startswith("ba.") or not isinstance(entry, dict):
            continue
        rich_text_id = str(entry.get("richTextId") or tag_id)
        color = _fz_rich_text_color(rich_text_table.get(rich_text_id) or rich_text_table.get(str(tag_id)) or {})
        icon_path = str(entry.get("iconPath") or "").strip()
        icon_url = _fz_asset_raw_url(static_resource_url(icon_path)) if icon_path else ""
        style = TermStyleView(term=str(tag_id), color=color, icon_url=icon_url)
        result[str(tag_id)] = style
        name = clean_text(entry.get("name") or entry.get("text") or entry.get("title"))
        if name:
            result[name] = TermStyleView(term=name, color=color, icon_url=icon_url)
    return result


def _fz_rich_text_links(richtext: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag_id, entry in (richtext.get("HYPERLINK_TEXTS") or {}).items():
        if not isinstance(entry, dict):
            result[tag_id] = entry
            continue
        copied = dict(entry)
        icon_path = str(copied.get("iconPath") or "").strip()
        if icon_path:
            copied["iconPath"] = _fz_asset_raw_url(static_resource_url(icon_path))
        result[tag_id] = copied
    return result


def _fz_rich_text_color(entry: dict[str, Any]) -> str:
    color = str(entry.get("color") or "").strip()
    if color:
        return color
    pre_defs = entry.get("preDef") or []
    pre = str(pre_defs[0] if pre_defs else "")
    color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
    return f"#{color_match.group(1)}" if color_match else ""


def _term_names_from_entry(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    own_names: set[str] = set()
    referenced_names: set[str] = set()
    raw_name = clean_text(entry.get("name"))
    if raw_name:
        own_names.add(raw_name)
        if " - " in raw_name:
            left, right = raw_name.split(" - ", 1)
            suffix = _term_suffix_from_name(left)
            if len(right) >= 2 and suffix:
                own_names.add(f"{right}{suffix}")
        elif " - " not in raw_name and " " in raw_name:
            own_names.add(raw_name.rsplit(" ", 1)[-1])
    desc = str(entry.get("desc") or "")
    for _, text in re.findall(r"<[@#]([^>]+)>([^<]+)</>", desc):
        cleaned = clean_text(text)
        if cleaned and _looks_like_term(cleaned):
            referenced_names.add(cleaned)
    own_names = {name for name in own_names if 2 <= len(name) <= 12 and _looks_like_term(name)}
    referenced_names = {name for name in referenced_names if 2 <= len(name) <= 12 and _looks_like_term(name)}
    return own_names, referenced_names


def _term_suffix_from_name(left: str) -> str:
    candidates = sorted((suffix for suffix in TERM_SUFFIXES if suffix and left.endswith(suffix)), key=len, reverse=True)
    if candidates:
        return candidates[0]
    candidates = sorted((suffix for suffix in TERM_SUFFIXES if suffix and suffix in left), key=len, reverse=True)
    return candidates[0] if candidates else ""


def _looks_like_term(name: str) -> bool:
    return any(suffix in name for suffix in TERM_SUFFIXES)


def _rich_text_visual(style: dict[str, Any]) -> tuple[str, str]:
    pre_defs = style.get("preDef") or []
    pre = str(pre_defs[0] if pre_defs else "")
    color_match = re.search(r"color=#([0-9a-fA-F]{6})", pre)
    icon_match = re.search(r'image="([^"]+)"', pre)
    color = f"#{color_match.group(1)}" if color_match else ""
    icon = icon_match.group(1) if icon_match else ""
    return color, icon


def _weapon_name(weapon_type: Any, item_desc: Any = "") -> str:
    desc = clean_text(item_desc)
    for name in WEAPON_OPTIONS:
        if name in desc:
            return name
    return WEAPON_NAMES.get(weapon_type, "未知武器")


def _extract_species(character: dict[str, Any]) -> str:
    for record in character.get("profileRecord") or []:
        desc = clean_text(record.get("recordDesc"))
        match = re.search(r"【种族】([^【\s]+)", desc)
        if match:
            return match.group(1).strip()
    return "未知种族"


def _skill_group_meta(skill_group_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for group in skill_group_map.values():
        category = _skill_group_category(group.get("skillGroupType"))
        for skill_id in group.get("skillIdList") or []:
            result[str(skill_id)] = {
                "name": clean_text(group.get("name")),
                "category": category,
                "icon": str(group.get("icon") or ""),
                "desc": clean_text(group.get("desc")),
                "skillGroupType": str(group.get("skillGroupType") or ""),
            }
    return result


def _skill_group_category(group_type: Any) -> str:
    try:
        group_type_value = int(group_type)
    except (TypeError, ValueError):
        group_type_value = -1
    return {
        0: "普攻",
        1: "战技",
        2: "终结技",
        3: "连携技",
    }.get(group_type_value, "")


def _skill_records_for_group(skill_table: dict[str, Any], skill_ids: list[str], category: str = "") -> list[dict[str, Any]]:
    if category == "普攻":
        preferred_ids = [
            skill_id
            for skill_id in skill_ids
            if re.search(r"(?:attack5|attack_5|attack-5|combo5|combo_5|combo-5)$", skill_id)
        ]
        skill_ids = preferred_ids or skill_ids
    records: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        for record in bundle.get("SkillPatchDataBundle") or []:
            records.append(record)
        if records:
            break
    return records


def _all_skill_records_for_group(skill_table: dict[str, Any], skill_ids: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records.extend(bundle.get("SkillPatchDataBundle") or [])
    return records


def _talent_node_meta(talent_node_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for node in talent_node_map.values():
        info = (node or {}).get("passiveSkillNodeInfo") or {}
        effect_id = str(info.get("talentEffectId") or "")
        if not effect_id:
            continue
        result[effect_id] = {
            "name": clean_text(info.get("name")),
            "icon_id": str(info.get("iconId") or ""),
        }
    return result


def _build_skills(skill_table: dict[str, Any], skill_group_map: dict[str, Any]) -> list[SkillView]:
    items: list[SkillView] = []
    for group in skill_group_map.values():
        category = _skill_group_category(group.get("skillGroupType"))
        if category not in SKILL_CATEGORY_ORDER:
            continue
        skill_ids = [str(skill_id) for skill_id in (group.get("skillIdList") or [])]
        records = _skill_records_for_group(skill_table, skill_ids, category)
        group_records = _all_skill_records_for_group(skill_table, skill_ids)
        if not records:
            continue
        levels = [_build_level(records, level, label, category) for level, label in LEVEL_COLUMNS]
        _merge_additional_skill_levels(levels, skill_table, skill_ids[1:], category)
        sample = _record_by_level(records, 9) or records[0]
        skill_id = str(skill_ids[0] if skill_ids else sample.get("skillId") or group.get("skillGroupId") or "")
        title = clean_text(group.get("name")) or category or "技能"
        items.append(
            SkillView(
                skill_id=skill_id,
                title=title,
                icon_id=str(group.get("icon") or sample.get("iconId") or ""),
                category=category,
                description=_format_skill_desc(group.get("desc") or sample.get("description"), group_records or records, category),
                form_descriptions=_build_skill_form_descriptions(group, group_records or records, category),
                levels=levels,
                extra_levels=_build_extra_levels(skill_table, skill_ids, category),
            )
        )
    return sorted(items, key=lambda item: (SKILL_CATEGORY_ORDER.get(item.category, 99), item.skill_id))


def _build_skill_form_descriptions(
    group: dict[str, Any],
    records: list[dict[str, Any]],
    category: str,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for index in (1, 2):
        name = clean_text(group.get(f"conditionName{index}"))
        raw_desc = group.get(f"conditionPostDesc{index}")
        if not name or not raw_desc:
            continue
        description = _format_skill_desc(raw_desc, records, category)
        description = clean_text(description.replace(f"{name}：", "", 1))
        if description:
            result.append((name, description))
    return result


def _build_extra_levels(skill_table: dict[str, Any], skill_ids: list[str], category: str) -> dict[str, list[SkillLevelView]]:
    if category != "普攻":
        return {}
    result: dict[str, list[SkillLevelView]] = {}
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records = list(bundle.get("SkillPatchDataBundle") or [])
        if records:
            result[skill_id] = [_build_level(records, level, label, category) for level, label in LEVEL_COLUMNS]
    return result


def _merge_additional_skill_levels(
    levels: list[SkillLevelView],
    skill_table: dict[str, Any],
    skill_ids: list[str],
    category: str,
) -> None:
    if category != "终结技":
        return
    levels_by_number = {level.level: level for level in levels}
    for skill_id in skill_ids:
        bundle = skill_table.get(skill_id) or {}
        records = list(bundle.get("SkillPatchDataBundle") or [])
        for level_number, label in LEVEL_COLUMNS:
            target = levels_by_number.get(level_number)
            if target is None:
                continue
            additional = _build_level(records, level_number, label, category)
            target.values.update(additional.values)


def _build_talents(effect_table: dict[str, Any], talent_node_map: dict[str, Any]) -> list[EffectView]:
    result_by_title: dict[str, EffectView] = {}
    talent_meta = _talent_node_meta(talent_node_map)
    for effect_id, effect in sorted(effect_table.items(), key=lambda item: _effect_sort_key(item[0])):
        if "_talent_" not in effect_id:
            continue
        meta = talent_meta.get(str(effect_id)) or {}
        title = clean_text(meta.get("name")) or _talent_title(effect_id)
        result_by_title[title] = EffectView(
            effect_id=str(effect_id),
            title=title,
            description=_format_effect_desc(effect),
            kind="天赋",
            icon_url=skill_icon_url(str(meta.get("icon_id") or "")),
        )
    return list(result_by_title.values())


def _build_potentials(potential_table: dict[str, Any], effect_table: dict[str, Any]) -> list[EffectView]:
    result: list[EffectView] = []
    for item in potential_table.get("potentialUnlockBundle") or []:
        effect_id = str(item.get("potentialEffectId") or "")
        effect = effect_table.get(effect_id) or {}
        if not effect_id or not effect:
            continue
        level = int(item.get("level") or len(result) + 1)
        name = clean_text(item.get("name")) or f"潜能 {level}"
        result.append(
            EffectView(
                effect_id=effect_id,
                title=f"P{level} {name}",
                description=_format_effect_desc(effect),
                kind="潜能",
                icon_url=_potential_icon_url(potential_table, item),
            )
        )
    return result


def _build_level(records: list[dict[str, Any]], level: int, label: str, category: str = "") -> SkillLevelView:
    record = _record_by_level(records, level)
    if not record:
        return SkillLevelView(label=label, level=level)
    return SkillLevelView(
        label=label,
        level=level,
        values=_extract_values(record, category),
        cooldown=_format_plain_number(record.get("coolDown")),
        cost=_format_plain_number(record.get("costValue")),
        charge=_format_plain_number(record.get("maxChargeTime")),
        description=clean_text(record.get("description")),
    )


def _record_by_level(records: list[dict[str, Any]], level: int) -> dict[str, Any] | None:
    for record in records:
        if int(record.get("level") or 0) == level:
            return record
    return None


def _extract_values(record: dict[str, Any], category: str = "") -> dict[str, str]:
    values: dict[str, str] = {}
    names = list(record.get("subDescNameList") or [])
    raw_values = list(record.get("subDescList") or [])
    for index, name in enumerate(names):
        metric = _normalize_metric_name(clean_text(name), category)
        value = clean_text(raw_values[index] if index < len(raw_values) else "")
        if metric and value:
            values[metric] = value
    if values:
        return values
    for item in record.get("blackboard") or []:
        key = str(item.get("key") or "").strip()
        if not key or key.startswith("display_"):
            continue
        label = _metric_label_from_key(key, category, str(record.get("skillId") or ""))
        values[label] = _format_blackboard_value(key, item.get("value"), item.get("valueStr"))
    return values


def _format_effect_desc(effect: dict[str, Any]) -> str:
    desc = str(effect.get("desc") or "")
    values = _effect_values(effect)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        value = _template_expression_value(key, values)
        return _format_template_value(value, fmt)

    rendered = re.sub(r"\{([^{}]+)\}", replace, desc)
    rendered = re.sub(r"(?m)^\s*-\s*", "", rendered)
    return clean_text(rendered)


def _format_skill_desc(desc: Any, records: list[dict[str, Any]], category: str = "") -> str:
    text = _primary_skill_desc(str(desc or ""), category)
    values = _skill_template_values(records)

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        key, _, fmt = expr.partition(":")
        key = key.strip()
        value = _template_expression_value(key, values)
        return _format_template_value(value, fmt)

    rendered = re.sub(r"\{([^{}]+)\}", replace, text)
    rendered = re.sub(r"(?m)^\s*-\s*", "", rendered)
    return clean_text(rendered)


def _primary_skill_desc(desc: str, category: str) -> str:
    if category != "普攻":
        return desc
    for marker in ("\n\n下落攻击", "\n下落攻击", "下落攻击：", "\n\n处决攻击", "\n处决攻击", "处决攻击："):
        if marker in desc:
            return desc.split(marker, 1)[0]
    return desc


def _normalize_metric_name(name: str, category: str) -> str:
    if category == "普攻":
        name = re.sub(r"普攻第[一二三四五六七八九十]+段", "普攻", name)
        name = re.sub(r"普攻第\d+段", "普攻", name)
    return name


def _skill_template_values(records: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    # Prefer Lv9 values for the rendered description, but fall back to any
    # available level so Warfarin templates do not leak into the image.
    ordered_records = sorted(
        records,
        key=lambda record: 0 if int(record.get("level") or 0) == 9 else 1,
    )
    for record in ordered_records:
        for item in record.get("blackboard") or []:
            key = str(item.get("key") or "").strip()
            if not key or key.startswith("display_"):
                continue
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            current = values.get(key)
            if current is None or (abs(current) < 0.0001 and abs(value) >= 0.0001):
                values[key] = value
    return values


def _effect_values(effect: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in effect.get("dataList") or []:
        for bb in (item.get("attachBuff") or {}).get("blackboard") or []:
            _store_effect_value(values, bb.get("key"), bb.get("value"))
        for bb in (item.get("attachSkill") or {}).get("blackboard") or []:
            _store_effect_value(values, bb.get("key"), bb.get("value"))
        attr = item.get("attrModifier") or {}
        attr_type = int(attr.get("attrType") or 0)
        attr_value = attr.get("attrValue")
        if attr_type and attr_value not in (None, ""):
            _store_effect_value(values, _attribute_placeholder(attr_type), attr_value)
            if attr_type in {41, 42}:
                _store_effect_value(values, "Will", attr_value)
        skill_bb = item.get("skillBbModifier") or {}
        _store_effect_value(values, skill_bb.get("bbKey"), skill_bb.get("floatValue"))
        skill_param = item.get("skillParamModifier") or {}
        param_type = int(skill_param.get("paramType") or 0)
        if param_type:
            _store_effect_value(values, {1: "costvalue", 2: "coolDown"}.get(param_type, f"param_{param_type}"), skill_param.get("paramValue"))
    return values


def _store_effect_value(values: dict[str, float], key: Any, value: Any) -> None:
    key = str(key or "").strip()
    if not key:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    values.setdefault(key, number)


def _attribute_placeholder(attr_type: int) -> str:
    return {
        39: "Str",
        40: "Agi",
        41: "Int",
        42: "Will",
        50: "PhysicalDamageIncrease",
        51: "FireDamageIncrease",
        52: "PulseDamageIncrease",
        53: "CrystDamageIncrease",
        54: "NaturalDamageIncrease",
        55: "EtherDamageIncrease",
        87: "PhysicalAndSpellInflictionEnhance",
    }.get(attr_type, f"attr_{attr_type}")


def _alias_key(key: str) -> str:
    return {
        "costValue": "costvalue",
        "Wil": "Will",
    }.get(key, key)


def _template_expression_value(expr: str, values: dict[str, float]) -> float | None:
    direct = values.get(expr)
    if direct is not None:
        return direct
    alias = values.get(_alias_key(expr))
    if alias is not None:
        return alias
    match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)\s*([+-])\s*([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)",
        expr,
    )
    if not match:
        return None

    def operand(token: str) -> float | None:
        try:
            return float(token)
        except ValueError:
            value = values.get(token)
            if value is None:
                value = values.get(_alias_key(token))
            return value

    left = operand(match.group(1))
    right = operand(match.group(3))
    if left is None or right is None:
        return None
    return left + right if match.group(2) == "+" else left - right


def _format_template_value(value: Any, fmt: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if "%" in fmt:
        decimal_match = re.search(r"\.(0+)%", fmt)
        decimals = len(decimal_match.group(1)) if decimal_match else 0
        return f"{number * 100:.{decimals}f}%"
    decimal_match = re.search(r"\.(0+)$", fmt)
    if decimal_match:
        return f"{number:.{len(decimal_match.group(1))}f}"
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _potential_icon_url(potential_table: dict[str, Any], item: dict[str, Any]) -> str:
    item_ids = list(item.get("itemIds") or [])
    if item_ids:
        item_id = str(item_ids[0] or "")
        if item_id:
            urls = item_icon_urls(item_id)
            return urls[0] if urls else ""
    first_item_id = str(potential_table.get("firstItemId") or "")
    if first_item_id:
        urls = item_icon_urls(first_item_id)
        return urls[0] if urls else ""
    return ""


def _talent_title(effect_id: str) -> str:
    match = re.search(r"_talent_(\d+)_(\d+)$", effect_id)
    if not match:
        return "固有天赋"
    group = int(match.group(1))
    stage = int(match.group(2))
    roman = {1: "I", 2: "II", 3: "III"}.get(group, str(group))
    return f"固有天赋 {roman} · 阶段 {stage}"

def _effect_sort_key(effect_id: str) -> tuple[int, ...]:
    numbers = [int(item) for item in re.findall(r"\d+", effect_id)]
    return tuple(numbers or [999])


def _metric_label_from_key(key: str, category: str = "", skill_id: str = "") -> str:
    if category == "普攻" and key == "atk_scale":
        if "power_attack" in skill_id:
            return "处决攻击倍率"
        if "plunging_attack" in skill_id:
            return "下落攻击倍率"
        return "普攻倍率"
    if category == "终结技" and "lizhiyan" in skill_id:
        if "ultimate_skill2" in skill_id:
            if key == "atk_scale":
                return "阵诀·智诀明伤害倍率"
            if key == "atk_scale_will":
                return "阵诀·意诀明伤害倍率"
        if key == "atk_scale":
            return "破晦阵伤害倍率"
        if key == "atk_scale_laser":
            return "阵诀·智集束打击倍率"
        if key == "atk_scale_laser_will":
            return "阵诀·意集束打击倍率"
    return WARFARIN_METRIC_LABELS.get(key, key.replace("_", " ").strip())


def _format_blackboard_value(key: str, value: Any, value_str: Any = "") -> str:
    if value_str:
        return clean_text(value_str)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    key_parts = set(key.lower().split("_"))
    if key_parts.intersection({"scale", "rate", "ratio", "vul"}) or key in WARFARIN_PERCENT_METRIC_KEYS:
        return _format_percent(number)
    return _format_metric_number(number)


def _format_percent(number: float) -> str:
    return f"{number * 100:.4f}".rstrip("0").rstrip(".") + "%"


def _format_metric_number(number: float) -> str:
    if abs(number) < 0.0001:
        return "--"
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _format_plain_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(number) < 0.0001:
        return "--"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
