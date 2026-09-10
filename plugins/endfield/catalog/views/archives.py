"""Archive library view construction; no I/O or command registration.

表层级与归属链（2026-09-10 实测 1.5.3）：
- ``PrtsPage``：3 个真实页签 ``document``(中枢档案)/``multi_media``(音像存档)/``text``(见闻辑录)。
- ``PrtsAllItem.type`` 与页签 ``pageType`` 一一对应（495 条全部可归属，无混合类型组），
  故条目归属页签 = 其 ``type``；``type`` 不在 PrtsPage 里的条目视为虚拟分类残留、不计入。
- ``PrtsFirstLv``：档案组（``firstLvId`` → ``categoryId`` + ``itemIds[]``）；从条目侧反向
  引用组（实测 0 孤儿条目），组缺失时组名留空但条目仍计数。
- 名字/副标题全是 ``{id, text}`` text-id，查 ``I18nTextTable_CN``（负数 int id 也按 str 索引）。
"""

from __future__ import annotations

from typing import Any

from ...providers.akedata import AKEDATA_PRTS_ICON_BASE
from ..models import ArchiveItemView, ArchiveSnapshotView
from .common import _to_int

# 游戏内档案库入口的页签展示顺序（2026-09-10 游戏截图实测）：见闻辑录 → 音像存档 → 中枢档案。
# PrtsPage 表内顺序为 document/multi_media/text，与游戏 UI 不一致，故展示顺序以此常量为准；
# 未知新页签按表内顺序追加在末尾。
_PAGE_DISPLAY_ORDER = ("text", "multi_media", "document")


def _i18n_text(i18n: dict[str, Any], obj: Any) -> str:
    """``{id, text}`` → I18nTextTable 解析出的中文文本；id=0 视为空。"""
    if isinstance(obj, dict) and "id" in obj:
        return str(i18n.get(str(obj["id"])) or obj.get("text") or "")
    return ""


def build_akedata_archive_snapshot(
    page_table: dict[str, Any],
    category_table: dict[str, Any],
    first_lv_table: dict[str, Any],
    all_item_table: dict[str, Any],
    i18n: dict[str, Any],
    *,
    fetched_at: int = 0,
    version_label: str | None = None,
) -> ArchiveSnapshotView:
    """聚合 AKEData Prts* 四表 + I18n → 档案库全量快照（三大页签口径）。

    排序按游戏 UI 展示顺序：页签（见闻辑录→音像存档→中枢档案）→ 分类（PrtsCategory
    ``order``，与游戏筛选行一致）→ 组 order → 条目 order，名称兜底，保证新增清单展示稳定。
    """
    page_names: dict[str, str] = {}
    page_order: dict[str, int] = {}
    for entry in (page_table or {}).values():
        if not isinstance(entry, dict) or not entry.get("pageType"):
            continue
        page_type = str(entry["pageType"])
        page_names[page_type] = _i18n_text(i18n, entry.get("name")) or page_type
        page_order[page_type] = (
            _PAGE_DISPLAY_ORDER.index(page_type)
            if page_type in _PAGE_DISPLAY_ORDER
            else len(_PAGE_DISPLAY_ORDER) + len(page_order)
        )

    category_names: dict[str, str] = {}
    category_order: dict[str, int] = {}
    for cat_key, entry in (category_table or {}).items():
        if isinstance(entry, dict) and entry.get("categoryId"):
            cat_id = str(entry["categoryId"])
            category_names[cat_id] = _i18n_text(i18n, entry.get("name")) or cat_id
            category_order[cat_id] = _to_int(entry.get("order")) or 0

    # firstLvId -> (category_id, group_name, sub_name, group_order, icon)
    groups: dict[str, tuple[str, str, str, int, str]] = {}
    for group_key, entry in (first_lv_table or {}).items():
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("firstLvId") or group_key)
        icon = str(entry.get("icon") or "")
        groups[gid] = (
            str(entry.get("categoryId") or ""),
            _i18n_text(i18n, entry.get("name")),
            _i18n_text(i18n, entry.get("subName")),
            _to_int(entry.get("order")) or 0,
            f"{AKEDATA_PRTS_ICON_BASE}/{icon}.png" if icon else "",
        )

    items: list[ArchiveItemView] = []
    for item_key, entry in (all_item_table or {}).items():
        if not isinstance(entry, dict):
            continue
        item_type = str(entry.get("type") or "")
        page_name = page_names.get(item_type)
        if page_name is None:
            continue  # 不在三大真实页签内（未来虚拟分类/异常数据），不计入口径
        gid = str(entry.get("firstLvId") or "")
        category_id, group_name, group_sub_name, _group_order, icon_url = groups.get(
            gid, ("", "", "", 0, "")
        )
        items.append(
            ArchiveItemView(
                item_id=str(entry.get("id") or item_key),
                name=_i18n_text(i18n, entry.get("name")) or str(item_key),
                page_id=item_type,
                page_name=page_name,
                category_id=category_id,
                category_name=category_names.get(category_id, category_id),
                group_id=gid,
                group_name=group_name,
                group_sub_name=group_sub_name,
                item_type=item_type,
                order=_to_int(entry.get("order")) or 0,
                icon_url=icon_url,
            )
        )

    items.sort(key=lambda item: (
        page_order.get(item.page_id, 99),
        category_order.get(item.category_id, 99),
        _group_order_of(groups, item.group_id),
        item.order,
        item.name,
    ))

    page_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    seen_groups: set[str] = set()
    for item in items:
        page_counts[item.page_name] = page_counts.get(item.page_name, 0) + 1
        if item.category_name:
            category_counts[item.category_name] = category_counts.get(item.category_name, 0) + 1
        if item.group_id:
            seen_groups.add(item.group_id)

    return ArchiveSnapshotView(
        items=items,
        version=version_label or "",
        fetched_at=fetched_at,
        source="akedata",
        total_count=len(items),
        page_counts=page_counts,
        category_counts=category_counts,
        group_count=len(seen_groups),
    )


def _group_order_of(groups: dict[str, tuple[str, str, str, int, str]], group_id: str) -> int:
    entry = groups.get(group_id)
    return entry[3] if entry else 0
