"""蚀刻章/奖章全量快照存储。

``current`` 槽存当前版本全量快照（命令读取的性能缓存，避免每次 `奖章` 都实时抓取）；
``baseline`` 槽存版本对比基线（akedata 上一游戏版本 achv_id 集合，源和源对比）。

底层用 ``utils.json_store.JsonStore``（文件 JSON，每次 set 全量重写）。写盘放线程池、
模块级 ``asyncio.Lock`` 串行化，避免并发刷新互相覆盖。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from otae_bot.infrastructure.storage.json_store import JsonStore

from ..catalog.models import MedalBaselineView, MedalItemView, MedalSnapshotView

_DEFAULT_PATH = str(Path("data") / "endfield" / "medal_snapshot.json")

# 仅取已知字段，容忍磁盘上多余/缺失键（手动编辑或未来字段增删）
_MEDAL_ITEM_FIELDS = frozenset(MedalItemView.__dataclass_fields__)


class MedalSnapshotStore:
    """奖章全量快照：current/previous 两槽，手动刷新时滚动。"""

    def __init__(self, file_path: str = _DEFAULT_PATH) -> None:
        self._store = JsonStore(file_path)
        self._lock = asyncio.Lock()

    async def replace_current(self, snapshot: MedalSnapshotView) -> None:
        """新快照写入 current。版本对比不再用滚动 previous，改用 baseline（akedata 历史版本）。"""
        current_dict = _snapshot_to_dict(snapshot)
        async with self._lock:
            await asyncio.to_thread(self._persist_current, current_dict)

    async def replace_current_and_baseline(
        self,
        snapshot: MedalSnapshotView,
        baseline: MedalBaselineView | None,
    ) -> None:
        """Persist a current snapshot and its matching baseline in one locked save."""
        current_dict = _snapshot_to_dict(snapshot)
        baseline_dict = _baseline_to_dict(baseline) if baseline else None
        async with self._lock:
            await asyncio.to_thread(self._persist_current_and_baseline, current_dict, baseline_dict)

    def _persist_current(self, current_dict: dict[str, Any]) -> None:
        # 直接改底层 _data 再一次 _save，避免 set() 两次全量写盘
        self._store._data["current"] = current_dict
        self._store._data.pop("previous", None)  # 清理旧的滚动基线残留
        self._store._save()

    def _persist_current_and_baseline(
        self,
        current_dict: dict[str, Any],
        baseline_dict: dict[str, Any] | None,
    ) -> None:
        self._store._data["current"] = current_dict
        self._store._data["baseline"] = baseline_dict
        self._store._data.pop("previous", None)
        self._store._save()

    async def replace_baseline(self, baseline: MedalBaselineView | None) -> None:
        """写入版本对比基线（akedata 上一游戏版本的 achv_id 集合）；None 清空。串行 + 写盘放线程池。"""
        baseline_dict = _baseline_to_dict(baseline) if baseline else None
        async with self._lock:
            await asyncio.to_thread(self._persist_baseline, baseline_dict)

    def _persist_baseline(self, baseline_dict: dict[str, Any] | None) -> None:
        self._store._data["baseline"] = baseline_dict
        self._store._save()

    def load_current_view(self) -> MedalSnapshotView | None:
        data = self._store.get("current")
        return _dict_to_snapshot(data) if isinstance(data, dict) else None

    def load_baseline_view(self) -> MedalBaselineView | None:
        data = self._store.get("baseline")
        return _dict_to_baseline(data) if isinstance(data, dict) else None


def _snapshot_to_dict(snapshot: MedalSnapshotView) -> dict[str, Any]:
    """View → 可 JSON 序列化的 dict（level_counts 的 int 键转 str 以便 JSON 存储）。"""
    return {
        "version": snapshot.version,
        "fetched_at": snapshot.fetched_at,
        "source": snapshot.source,
        "total_count": snapshot.total_count,
        "level_counts": {str(k): v for k, v in snapshot.level_counts.items()},
        "platable_count": snapshot.platable_count,
        "upgradable_count": snapshot.upgradable_count,
        "category_counts": dict(snapshot.category_counts),
        "medals": [asdict(m) for m in snapshot.medals],
    }


def _dict_to_snapshot(data: dict[str, Any]) -> MedalSnapshotView:
    medals: list[MedalItemView] = []
    for raw in data.get("medals") or []:
        if isinstance(raw, dict):
            medals.append(
                MedalItemView(**{k: v for k, v in raw.items() if k in _MEDAL_ITEM_FIELDS})
            )
    level_raw = data.get("level_counts")
    level_counts = (
        {int(k): int(v) for k, v in level_raw.items()}
        if isinstance(level_raw, dict)
        else {}
    )
    category_raw = data.get("category_counts")
    category_counts = (
        {str(k): int(v) for k, v in category_raw.items()}
        if isinstance(category_raw, dict)
        else {}
    )
    return MedalSnapshotView(
        medals=medals,
        version=str(data.get("version") or ""),
        fetched_at=int(data.get("fetched_at") or 0),
        source=str(data.get("source") or "fz"),
        total_count=int(data.get("total_count") or len(medals)),
        level_counts=level_counts,
        platable_count=int(data.get("platable_count") or 0),
        upgradable_count=int(data.get("upgradable_count") or 0),
        category_counts=category_counts,
    )


def _baseline_to_dict(baseline: MedalBaselineView) -> dict[str, Any]:
    """MedalBaselineView → 可 JSON 序列化的 dict。"""
    return {
        "version": baseline.version,
        "version_id": baseline.version_id,
        "ids": list(baseline.ids),
        "fetched_at": baseline.fetched_at,
    }


def _dict_to_baseline(data: dict[str, Any]) -> MedalBaselineView:
    raw_ids = data.get("ids")
    ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
    return MedalBaselineView(
        version=str(data.get("version") or ""),
        version_id=str(data.get("version_id") or ""),
        ids=ids,
        fetched_at=int(data.get("fetched_at") or 0),
    )
