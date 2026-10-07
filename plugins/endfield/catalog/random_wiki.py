"""Random entries drawn only from public Wiki catalogs, never account commands."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from time import monotonic

import httpx
from loguru import logger

from ..encyclopedia.archives import build_entries
from ..encyclopedia.index import get_index
from ..providers.repository import query_snapshot
from ..providers.warfarin import WarfarinAPIError
from ..stages.service import EndfieldStageService
from .commands import EndfieldCandidate
from .models import ArchiveSnapshotView
from .service import EndfieldService

# Explicit allowlist: adding a command or renderer must not expand this pool.
PUBLIC_WIKI_KINDS = (
    "operator",
    "weapon",
    "equipment",
    "enemy",
    "stage",
    "item",
    "prop",
    "term",
    "archive_entry",
)
CATALOG_TIMEOUT_SECONDS = 15.0
SELECTION_BUDGET_SECONDS = 30.0
MAX_RENDER_ATTEMPTS = 3


def is_public_wiki_candidate(candidate: EndfieldCandidate) -> bool:
    return (
        candidate.kind in PUBLIC_WIKI_KINDS
        and candidate.source == "akedata"
        and bool(candidate.key.strip())
        and bool(candidate.display_name.strip())
    )


class RandomWikiService:
    def __init__(
        self,
        catalog: EndfieldService,
        stages: EndfieldStageService,
        archive_snapshot: Callable[[], ArchiveSnapshotView | None],
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.catalog = catalog
        self.stages = stages
        self.archive_snapshot = archive_snapshot
        self.rng = rng if rng is not None else random.SystemRandom()

    async def candidates(self) -> AsyncIterator[EndfieldCandidate]:
        """Try random categories lazily; callers stop after the first usable card."""
        kinds = list(PUBLIC_WIKI_KINDS)
        self.rng.shuffle(kinds)
        deadline = monotonic() + SELECTION_BUDGET_SECONDS
        attempts = 0
        for kind in kinds:
            remaining = deadline - monotonic()
            if remaining <= 0 or attempts >= MAX_RENDER_ATTEMPTS:
                return
            try:
                async with asyncio.timeout(min(CATALOG_TIMEOUT_SECONDS, remaining)):
                    entries = await self.load_candidates(kind)
            except (
                httpx.HTTPError,
                WarfarinAPIError,
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
            ) as exc:
                logger.warning(
                    "[endfield] random Wiki catalog unavailable kind={} error={}",
                    kind,
                    type(exc).__name__,
                )
                continue
            # Duplicate rows and hidden/empty entries must not skew selection.
            unique = {
                entry.key: entry
                for entry in entries
                if entry.kind == kind and is_public_wiki_candidate(entry)
            }
            if unique:
                attempts += 1
                yield self.rng.choice(tuple(unique.values()))

    async def load_candidates(self, kind: str) -> list[EndfieldCandidate]:
        if kind not in PUBLIC_WIKI_KINDS:
            raise ValueError("Unsupported public Wiki kind")
        if kind == "archive_entry":
            # The shared snapshot contains public entries, not collection progress.
            view = self.archive_snapshot()
            if view is None:
                return []
            return [
                EndfieldCandidate(
                    kind,
                    entry.key,
                    entry.display_name,
                    100,
                    source="akedata",
                    revision=f"{view.version}|{view.fetched_at}",
                )
                for entry in build_entries(view)
            ]
        if kind == "stage":
            catalog = await self.stages.get_catalog_view("akedata")
            return [
                EndfieldCandidate(
                    kind,
                    item.stage_key,
                    item.name,
                    100,
                    source="akedata",
                    revision=item.revision,
                    mode="detail",
                )
                for group in catalog.groups
                for item in group.items
                if item.queryable
            ]
        async with query_snapshot() as data:
            if kind == "operator":
                catalog = await self.catalog.get_operator_catalog_view(source="akedata")
                rows = [
                    (item.operator_id, item.name)
                    for element in catalog.elements
                    for profession in element.professions
                    for item in profession.items
                ]
            elif kind == "weapon":
                catalog = await self.catalog.get_weapon_catalog_view(source="akedata")
                rows = [
                    (item.weapon_id, item.name)
                    for group in catalog.groups
                    for item in group.items
                ]
            elif kind == "equipment":
                catalog = await self.catalog.get_equipment_catalog_view(
                    rarity_filter="all",
                    include_details=False,
                    source="akedata",
                )
                rows = [
                    (item.equipment_id, item.name)
                    for group in catalog.groups
                    for item in group.items
                ]
            else:
                index = await get_index(data, kind)
                rows = [
                    (entry.key, entry.display_name)
                    for entry in index.entries
                    if entry.kind == kind and entry.listed
                ]
            return [
                EndfieldCandidate(
                    kind, key, name, 100, source="akedata", revision=data.revision
                )
                for key, name in rows
            ]
