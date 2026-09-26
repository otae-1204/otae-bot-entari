"""档案条目：解析分流后的单条卡视图。

`build_entries` 与 `entry_view` 只接收 `ArchiveSnapshotView`，不构造
`ArchiveSnapshotStore`：`JsonStore` 只在构造时读一次文件，第二个实例看不到
`handlers.archive_store` 刷新后的内存。
"""

from __future__ import annotations

from ..catalog.models import ArchiveItemView, ArchiveSnapshotView
from .models import ArchiveEntryView, IndexEntry

_ENTRY_CACHE: dict[tuple[str, int], tuple[IndexEntry, ...]] = {}
_ENTRY_ORDER: list[tuple[str, int]] = []


def build_entries(view: ArchiveSnapshotView | None) -> tuple[IndexEntry, ...]:
    """条目名进 `display_name`，组名进 `extra_names`。按 (版本, 抓取时间) 缓存。"""
    if view is None:
        return ()
    key = (str(view.version or ""), int(view.fetched_at or 0))
    cached = _ENTRY_CACHE.get(key)
    if cached is not None:
        return cached
    entries: list[IndexEntry] = []
    for item in view.items:
        name = str(item.name or "").strip()
        if not name:
            continue
        entries.append(
            IndexEntry(
                kind="archive_entry",
                key=str(item.item_id),
                display_name=name,
                extra_names=_extra_names(item),
                summary=str(item.category_name or ""),
                group=str(item.group_id or ""),
                icon_url=str(item.icon_url or ""),
                subtitle=str(item.page_name or ""),
            )
        )
    entries.sort(key=lambda entry: entry.display_name)
    result = tuple(entries)
    _ENTRY_CACHE[key] = result
    _touch(key)
    return result


def entry_view(view: ArchiveSnapshotView | None, item_id: str) -> ArchiveEntryView:
    """只拷贝快照已有字段：id、名字、页签、分类、组名、图标、版本。"""
    for item in (view.items if view is not None else ()):
        if str(item.item_id) != str(item_id):
            continue
        return ArchiveEntryView(
            item_id=str(item.item_id),
            name=str(item.name or ""),
            page_name=str(item.page_name or ""),
            category_name=str(item.category_name or ""),
            group_name=str(item.group_name or ""),
            group_sub_name=str(item.group_sub_name or ""),
            icon_url=str(item.icon_url or ""),
            version=str(view.version or ""),
        )
    return ArchiveEntryView(item_id=str(item_id), version=str(view.version or ""))


def clear_caches() -> None:
    _ENTRY_CACHE.clear()
    _ENTRY_ORDER.clear()


def _extra_names(item: ArchiveItemView) -> tuple[str, ...]:
    names = [str(item.group_name or "").strip(), str(item.group_sub_name or "").strip()]
    return tuple(dict.fromkeys(name for name in names if name))


def _touch(key: tuple[str, int]) -> None:
    if key in _ENTRY_ORDER:
        _ENTRY_ORDER.remove(key)
    _ENTRY_ORDER.append(key)
    while len(_ENTRY_ORDER) > 2:
        stale = _ENTRY_ORDER.pop(0)
        _ENTRY_CACHE.pop(stale, None)
