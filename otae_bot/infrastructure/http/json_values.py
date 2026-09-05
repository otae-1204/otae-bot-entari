"""Read-only JSON trees that retain dict/list type compatibility with parsers."""

from __future__ import annotations

import sys
from typing import Any


def _immutable(*_args, **_kwargs):
    raise TypeError("Cached JSON is read-only; request a mutable copy to edit it")


class FrozenDict(dict):
    __slots__ = ()
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = (
        __ior__
    ) = _immutable

    def __deepcopy__(self, _memo):
        return mutable_json(self)


class FrozenList(list):
    __slots__ = ()
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = (
        reverse
    ) = sort = _immutable
    __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, _memo):
        return mutable_json(self)


def freeze_json(value: Any) -> Any:
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, dict):
        return FrozenDict((key, freeze_json(item)) for key, item in value.items())
    if isinstance(value, list):
        return FrozenList(freeze_json(item) for item in value)
    return value


def freeze_json_object(value: dict) -> FrozenDict:
    """JSON decoder hook: child objects are already frozen, avoid a second walk."""
    for key, item in value.items():
        if isinstance(item, list):
            value[key] = freeze_json(item)
    return FrozenDict(value)


def mutable_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mutable_json(item) for item in value]
    return value


def json_memory_size(value: Any) -> int:
    """Count retained tree allocations once, not just serialized UTF-8 bytes."""
    pending = [value]
    seen: set[int] = set()
    size = 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return size
