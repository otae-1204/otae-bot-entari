"""Public map artwork only; account data never leaves the local matching step."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace

import httpx

from otae_bot.infrastructure.http.client import fetch_json

from ...providers.akedata import AKEDATA_DATA_BASE, AKEDATA_HEADERS
from ...rendering.health import record_assets
from .artwork import artwork_levels, artwork_manifest, artwork_url, map_id
from .models import ExplorationView

logger = logging.getLogger(__name__)
MAP_TREE_URL = "https://zonai.skland.com/web/v1/game/endfield/map/tree"
ASSET_INDEX_URL = f"{AKEDATA_DATA_BASE}/asset-sync-index.json"
_TILE_PATH = re.compile(
    r"assets/beyond/dynamicassets/gameplay/ui/(sprites|textures)/levelmap/levelmapchunks/"
    r"[a-z0-9]+/l_([a-z0-9_]+)_([1-9][0-9]*)_([1-9][0-9]*)\.png"
)


@dataclass(frozen=True, slots=True)
class MapTile:
    url: str
    column: int
    row: int


@dataclass(frozen=True, slots=True)
class MapThumbnail:
    columns: int
    rows: int
    tiles: tuple[MapTile, ...]
    official: bool = False


ThumbnailMap = dict[tuple[str, str], MapThumbnail]


def match_thumbnails(view: ExplorationView, tree: dict, index: dict) -> ThumbnailMap:
    """Use exact IDs or an unambiguous name within the same named parent region."""
    maps = tree.get("data", {}).get("maps") if tree.get("code") == 0 else None
    files = index.get("datasets", {}).get("images", {}).get("files")
    if not isinstance(maps, list) or not maps or not isinstance(files, dict):
        raise ValueError("Exploration artwork metadata is incomplete")
    tiles: dict[tuple[str, str], dict[tuple[int, int], str]] = {}
    for path in files:
        match = _TILE_PATH.fullmatch(path)
        if match:
            family, level_id, column, row = match.groups()
            tiles.setdefault((level_id, family), {})[int(column), int(row)] = (
                f"{AKEDATA_DATA_BASE}/public/images/{path}"
            )
    result: ThumbnailMap = {}
    for key, matched in _matching_levels(view, maps):
        # Prefer transparent sprites, then a complete texture set. Never mix sets.
        for family in ("sprites", "textures"):
            grid = tiles.get((matched.get("id"), family), {})
            if not grid:
                continue
            columns = max(x for x, _ in grid)
            rows = max(y for _, y in grid)
            # Reject partial maps and bound the size of untrusted grids.
            if columns * rows != len(grid) or len(grid) > 64:
                continue
            result[key] = MapThumbnail(
                columns,
                rows,
                tuple(
                    MapTile(url, x - 1, rows - y)
                    for (x, y), url in sorted(grid.items())
                ),
            )
            break
    return result


def _matching_levels(view: ExplorationView, maps: list[dict]):
    for region in view.regions:
        # card/detail's domain_N is the map tree's mapNN; try it before the name.
        for key, value in (
            ("id", region.region_id),
            ("id", map_id(region.region_id)),
            ("name", region.name),
        ):
            parents = [
                item
                for item in maps
                if isinstance(item, dict) and item.get(key) == value
            ]
            if parents:
                break
        if len(parents) != 1:
            continue
        candidates = parents[0].get("levels")
        if not isinstance(candidates, list):
            continue
        candidates = [item for item in candidates if isinstance(item, dict)]
        for level in region.levels:
            matched = [item for item in candidates if item.get("id") == level.level_id]
            if not matched:
                matched = [
                    item for item in candidates if item.get("name") == level.name
                ]
            if len(matched) != 1:
                continue
            yield (region.region_id, level.level_id), matched[0]


def match_official_thumbnails(view: ExplorationView) -> ThumbnailMap:
    """Packaged artwork, by parent region first, else by a globally unique level ID."""
    scoped = dict(_matching_levels(view, artwork_manifest()["maps"]))
    result: ThumbnailMap = {}
    for region in view.regions:
        for level in region.levels:
            key = (region.region_id, level.level_id)
            known = scoped.get(key)
            if known is None and level.level_id in artwork_levels():
                known = artwork_levels()[level.level_id][1]
            url = artwork_url(known["image"]) if known else ""
            if url:
                result[key] = MapThumbnail(1, 1, (MapTile(url, 0, 0),), official=True)
    return result


async def fetch_exploration_thumbnails(view: ExplorationView) -> ThumbnailMap:
    if not view.level_count:
        return {}
    official = match_official_thumbnails(view)
    missing = replace(
        view,
        regions=tuple(
            replace(
                region,
                levels=tuple(
                    level
                    for level in region.levels
                    if (region.region_id, level.level_id) not in official
                ),
            )
            for region in view.regions
        ),
    )
    if not missing.level_count:
        return official
    try:
        tree, index = await asyncio.gather(
            fetch_json(
                MAP_TREE_URL,
                namespace="endfield_exploration_maps",
                read_only=True,
                ttl_seconds=3600,
                timeout_seconds=10,
                max_bytes=4 * 1024 * 1024,
            ),
            fetch_json(
                ASSET_INDEX_URL,
                namespace="akedata",
                headers=AKEDATA_HEADERS,
                read_only=True,
                ttl_seconds=3600,
                timeout_seconds=10,
                max_bytes=32 * 1024 * 1024,
            ),
        )
        if not isinstance(tree, dict) or not isinstance(index, dict):
            raise TypeError("Exploration artwork metadata is not an object")
        return {**official, **match_thumbnails(missing, tree, index)}
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        TypeError,
        RuntimeError,
        AttributeError,
    ) as exc:
        # A temporary metadata outage must not poison the completed-image cache.
        record_assets((MAP_TREE_URL, ASSET_INDEX_URL), ())
        logger.warning(
            "[endfield] exploration thumbnails unavailable (%s)", type(exc).__name__
        )
        return official
