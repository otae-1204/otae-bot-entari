"""Unmodified artwork extracted from Skland's public exploration component."""

from __future__ import annotations

import base64
import json
from functools import lru_cache

from ...paths import IMAGE_DIR

ARTWORK_DIR = IMAGE_DIR / "exploration"
# card/detail calls the parent regions domain_N; the packaged artwork and the
# public map tree call the same regions mapNN.
DOMAIN_MAP_IDS = {"domain_1": "map01", "domain_2": "map02"}


@lru_cache(maxsize=1)
def artwork_manifest() -> dict:
    return json.loads((ARTWORK_DIR / "manifest.json").read_text(encoding="utf-8"))


def map_id(region_id: str) -> str:
    return DOMAIN_MAP_IDS.get(region_id, region_id)


@lru_cache(maxsize=1)
def artwork_levels() -> dict[str, tuple[dict, dict]]:
    """Level ID -> (parent map, level), for IDs listed exactly once in the manifest."""
    found: dict[str, list[tuple[dict, dict]]] = {}
    for parent in artwork_manifest()["maps"]:
        for level in parent["levels"]:
            found.setdefault(level["id"], []).append((parent, level))
    return {key: rows[0] for key, rows in found.items() if len(rows) == 1}


@lru_cache(maxsize=32)
def artwork_url(filename: str) -> str:
    path = ARTWORK_DIR / filename
    if not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(
        "ascii"
    )


def collection_icons() -> dict[str, str]:
    return {
        key: artwork_url(filename)
        for key, filename in artwork_manifest()["collections"].items()
    }
