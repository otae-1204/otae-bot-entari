"""按 `(snapshot.revision, kind)` 分片建索引。

索引只依赖 `ItemTable` 一类基础表与本地化文本：`/ef 物品` 不该为了列目录
去读 `SkillPatchTable`。缓存超两个 revision 就丢更早的，与 akedata 负缓存同口径。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..providers.repository import AkeDataIncomplete, AkeSnapshot
from . import akedata, enemies, items, props, terms
from .models import EncyclopediaIndex

Builder = Callable[[AkeSnapshot], Awaitable[EncyclopediaIndex]]

# `archive_entry` 不在这里：档案条目不碰 `AkeSnapshot`，走 `archives.build_entries`。
_BUILDERS: dict[str, Builder] = {
    "item": items.build_item_index,
    "prop": props.build_prop_index,
    "enemy": enemies.build_enemy_index,
    "term": terms.build_term_index,
}

_INDEX_CACHE: dict[tuple[str, str], EncyclopediaIndex] = {}
_REVISION_ORDER: list[str] = []


def supported_kinds() -> tuple[str, ...]:
    return tuple(_BUILDERS)


async def get_index(snapshot: AkeSnapshot, kind: str) -> EncyclopediaIndex:
    key = (snapshot.revision, kind)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError("该资料尚未开放")
    index = await builder(snapshot)
    _INDEX_CACHE[key] = index
    _touch(snapshot.revision)
    return index


def clear_index_caches() -> None:
    _INDEX_CACHE.clear()
    _REVISION_ORDER.clear()
    akedata.clear_caches()


def _touch(revision: str) -> None:
    if revision in _REVISION_ORDER:
        _REVISION_ORDER.remove(revision)
    _REVISION_ORDER.append(revision)
    while len(_REVISION_ORDER) > 2:
        stale = _REVISION_ORDER.pop(0)
        for key in [key for key in _INDEX_CACHE if key[0] == stale]:
            _INDEX_CACHE.pop(key, None)


__all__ = ["AkeDataIncomplete", "get_index", "supported_kinds", "clear_index_caches"]
