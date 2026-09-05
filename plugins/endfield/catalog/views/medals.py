"""Medals view construction; no I/O or command registration."""

from __future__ import annotations

from typing import (
    Any,
)

from ...account.i18n import (
    localized_text,
)
from ...providers.akedata import (
    AKEDATA_ICON_BASE,
)
from ..models import (
    MedalItemView,
    MedalProgressView,
    MedalSnapshotView,
)
from .common import (
    _clean_fz_rich_text,
    _first_text,
    _first_value,
    _fz_asset_raw_url,
    _fz_overview_entries,
    _ordered_fz_levels,
    _to_int,
    _unwrap_fz_list,
)


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
