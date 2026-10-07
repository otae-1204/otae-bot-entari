"""Unmodified artwork extracted from Skland's public exploration component."""

from __future__ import annotations

import base64
import json
from functools import lru_cache

from ...paths import IMAGE_DIR

ARTWORK_DIR = IMAGE_DIR / "exploration"


@lru_cache(maxsize=1)
def artwork_manifest() -> dict:
    return json.loads((ARTWORK_DIR / "manifest.json").read_text(encoding="utf-8"))


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
