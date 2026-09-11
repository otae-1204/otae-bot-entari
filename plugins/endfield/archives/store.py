"""档案库全量快照存储。

与 ``medals.store.MedalSnapshotStore`` 同构的双槽设计：``current`` 槽存当前版本全量快照
（命令读取的性能缓存，避免每次 `档案` 都实时抓取）；``baseline`` 槽存版本对比基线
（akedata 上一游戏版本 nar_ id 集合，源和源对比）。

底层用 ``utils.json_store.JsonStore``（文件 JSON，每次 set 全量重写）。写盘放线程池、
模块级 ``asyncio.Lock`` 串行化，避免并发刷新互相覆盖。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from otae_bot.infrastructure.storage.json_store import JsonStore

from ..catalog.models import ArchiveBaselineView, ArchiveItemView, ArchiveSnapshotView

_DEFAULT_PATH = str(Path("data") / "endfield" / "archive_snapshot.json")

# 仅取已知字段，容忍磁盘上多余/缺失键（手动编辑或未来字段增删）
_ARCHIVE_ITEM_FIELDS = frozenset(ArchiveItemView.__dataclass_fields__)


class ArchiveSnapshotStore:
    """档案库全量快照：current/baseline 两槽，手动刷新时成对滚动。"""

    def __init__(self, file_path: str = _DEFAULT_PATH) -> None:
        self._store = JsonStore(file_path)
        self._lock = asyncio.Lock()

    async def replace_current(self, snapshot: ArchiveSnapshotView) -> None:
        current_dict = _snapshot_to_dict(snapshot)
        async with self._lock:
            await asyncio.to_thread(self._persist_current, current_dict)

    async def replace_current_and_baseline(
        self,
        snapshot: ArchiveSnapshotView,
        baseline: ArchiveBaselineView | None,
    ) -> None:
        """Persist a current snapshot and its matching baseline in one locked save."""
        current_dict = _snapshot_to_dict(snapshot)
        baseline_dict = _baseline_to_dict(baseline) if baseline else None
        async with self._lock:
            await asyncio.to_thread(self._persist_current_and_baseline, current_dict, baseline_dict)

    def _persist_current(self, current_dict: dict[str, Any]) -> None:
        # 直接改底层 _data 再一次 _save，避免 set() 两次全量写盘
        self._store._data["current"] = current_dict
        self._store._save()

    def _persist_current_and_baseline(
        self,
        current_dict: dict[str, Any],
        baseline_dict: dict[str, Any] | None,
    ) -> None:
        self._store._data["current"] = current_dict
        self._store._data["baseline"] = baseline_dict
        self._store._save()

    async def replace_baseline(self, baseline: ArchiveBaselineView | None) -> None:
        """写入版本对比基线（akedata 上一游戏版本的 nar_ id 集合）；None 清空。"""
        baseline_dict = _baseline_to_dict(baseline) if baseline else None
        async with self._lock:
            await asyncio.to_thread(self._persist_baseline, baseline_dict)

    def _persist_baseline(self, baseline_dict: dict[str, Any] | None) -> None:
        self._store._data["baseline"] = baseline_dict
        self._store._save()

    def load_current_view(self) -> ArchiveSnapshotView | None:
        data = self._store.get("current")
        return _dict_to_snapshot(data) if isinstance(data, dict) else None

    def load_baseline_view(self) -> ArchiveBaselineView | None:
        data = self._store.get("baseline")
        return _dict_to_baseline(data) if isinstance(data, dict) else None


def _snapshot_to_dict(snapshot: ArchiveSnapshotView) -> dict[str, Any]:
    """View → 可 JSON 序列化的 dict（counts 的键本就是 str 页签/分类名，无需转换）。"""
    return {
        "version": snapshot.version,
        "fetched_at": snapshot.fetched_at,
        "source": snapshot.source,
        "total_count": snapshot.total_count,
        "page_counts": dict(snapshot.page_counts),
        "category_counts": dict(snapshot.category_counts),
        "group_count": snapshot.group_count,
        "items": [asdict(item) for item in snapshot.items],
    }


def _dict_to_snapshot(data: dict[str, Any]) -> ArchiveSnapshotView:
    items: list[ArchiveItemView] = []
    for raw in data.get("items") or []:
        if isinstance(raw, dict):
            items.append(
                ArchiveItemView(**{k: v for k, v in raw.items() if k in _ARCHIVE_ITEM_FIELDS})
            )
    page_raw = data.get("page_counts")
    page_counts = (
        {str(k): int(v) for k, v in page_raw.items()}
        if isinstance(page_raw, dict)
        else {}
    )
    category_raw = data.get("category_counts")
    category_counts = (
        {str(k): int(v) for k, v in category_raw.items()}
        if isinstance(category_raw, dict)
        else {}
    )
    return ArchiveSnapshotView(
        items=items,
        version=str(data.get("version") or ""),
        fetched_at=int(data.get("fetched_at") or 0),
        source=str(data.get("source") or "akedata"),
        total_count=int(data.get("total_count") or len(items)),
        page_counts=page_counts,
        category_counts=category_counts,
        group_count=int(data.get("group_count") or 0),
    )


def _baseline_to_dict(baseline: ArchiveBaselineView) -> dict[str, Any]:
    """ArchiveBaselineView → 可 JSON 序列化的 dict。"""
    return {
        "version": baseline.version,
        "version_id": baseline.version_id,
        "ids": list(baseline.ids),
        "fetched_at": baseline.fetched_at,
    }


def _dict_to_baseline(data: dict[str, Any]) -> ArchiveBaselineView:
    raw_ids = data.get("ids")
    ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
    return ArchiveBaselineView(
        version=str(data.get("version") or ""),
        version_id=str(data.get("version_id") or ""),
        ids=ids,
        fetched_at=int(data.get("fetched_at") or 0),
    )
