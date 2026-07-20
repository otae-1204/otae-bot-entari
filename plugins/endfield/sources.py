from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class EndfieldSource:
    key: str
    label: str
    priority: int
    kinds: frozenset[str]


DATA_SOURCES: tuple[EndfieldSource, ...] = (
    EndfieldSource("fz", "FZ Wiki", 10, frozenset({"operator", "weapon", "equipment"})),
    EndfieldSource("warfarin", "Warfarin Wiki", 20, frozenset({"operator", "weapon"})),
)

_SOURCE_BY_KEY = {source.key: source for source in DATA_SOURCES}
_SOURCE_ALIASES = {
    "fz": "fz",
    "fz-wiki": "fz",
    "fz_wiki": "fz",
    "warfarin": "warfarin",
    "warfarin-wiki": "warfarin",
    "warfarin_wiki": "warfarin",
    "wf": "warfarin",
}


def source_order(kind: str) -> tuple[str, ...]:
    return tuple(
        source.key
        for source in sorted(DATA_SOURCES, key=lambda item: item.priority)
        if kind in source.kinds
    )


def source_label(key: str) -> str:
    source = _SOURCE_BY_KEY.get(key)
    return source.label if source else key


def normalize_source(value: str) -> str:
    return _SOURCE_ALIASES.get(str(value or "").strip().lower(), "")


def source_labels(keys: Iterable[str]) -> str:
    return "、".join(source_label(key) for key in keys)
