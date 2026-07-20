from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


ALIAS_DATA_PATH = Path(__file__).with_name("alias_data.json")
SUPPORTED_KINDS = frozenset({"operator", "weapon", "equipment"})


def aliases_for(kind: str, canonical_name: str) -> tuple[str, ...]:
    normalized_kind = _normalize_kind(kind)
    normalized_name = _normalize(canonical_name)
    if not normalized_kind or not normalized_name:
        return ()
    entry = _alias_data()[normalized_kind].get(normalized_name)
    return entry[1] if entry else ()


def alias_targets(kind: str, alias: str) -> tuple[str, ...]:
    normalized_kind = _normalize_kind(kind)
    normalized_alias = _normalize(alias)
    if not normalized_kind or not normalized_alias:
        return ()
    return _alias_index()[normalized_kind].get(normalized_alias, ())


@lru_cache(maxsize=1)
def _alias_data() -> dict[str, dict[str, tuple[str, tuple[str, ...]]]]:
    raw = json.loads(ALIAS_DATA_PATH.read_text(encoding="utf-8"))
    result = {kind: {} for kind in SUPPORTED_KINDS}
    for kind in SUPPORTED_KINDS:
        entries = raw.get(kind, {})
        if not isinstance(entries, dict):
            continue
        for canonical_name, aliases in entries.items():
            canonical = str(canonical_name or "").strip()
            normalized_name = _normalize(canonical)
            if not canonical or not normalized_name or not isinstance(aliases, list):
                continue
            cleaned = tuple(
                dict.fromkeys(
                    str(alias or "").strip()
                    for alias in aliases
                    if str(alias or "").strip()
                )
            )
            result[kind][normalized_name] = (canonical, cleaned)
    return result


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict[str, tuple[str, ...]]]:
    result: dict[str, dict[str, list[str]]] = {kind: {} for kind in SUPPORTED_KINDS}
    for kind, entries in _alias_data().items():
        for canonical, aliases in entries.values():
            for alias in aliases:
                normalized_alias = _normalize(alias)
                if normalized_alias:
                    result[kind].setdefault(normalized_alias, []).append(canonical)
    return {
        kind: {alias: tuple(dict.fromkeys(names)) for alias, names in entries.items()}
        for kind, entries in result.items()
    }


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    return normalized if normalized in SUPPORTED_KINDS else ""


def _normalize(value: str) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())
