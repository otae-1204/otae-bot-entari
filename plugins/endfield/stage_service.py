from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha1

from loguru import logger

from .client import WarfarinClient
from .akedata_stage_source import AkeDataStageSource
from .stage_models import (
    Stage,
    StageCardView,
    StageCatalogGroup,
    StageCatalogItem,
    StageCatalogView,
    StageVariant,
)
from .stage_source import StageDataIncomplete


@dataclass(frozen=True, slots=True)
class StageMatch:
    key: str
    """Addresses the stage, including which entry of a multi-stage article it is."""
    title: str
    display_name: str
    query_text: str
    selector: str
    mode: str
    revision: str
    updated_at: str
    queryable: bool
    source: str


class StageVariantNotFound(ValueError):
    def __init__(self, stage_name: str, selector: str, variants: tuple[StageVariant, ...]):
        self.stage_name = stage_name
        self.selector = selector
        self.valid_labels = tuple(variant.label for variant in variants)
        super().__init__(
            f"{stage_name} 没有“{selector}”变体；可选：{'、'.join(self.valid_labels)}"
        )


class EndfieldStageService:
    def __init__(self, client: WarfarinClient):
        self.sources = {"akedata": AkeDataStageSource(client)}

    async def get_catalog_view(self, source: str = "") -> StageCatalogView:
        if source:
            return await self._source(source).catalog()
        results = await asyncio.gather(
            *(adapter.catalog() for adapter in self.sources.values()),
            return_exceptions=True,
        )
        catalogs: list[StageCatalogView] = []
        errors: list[BaseException] = []
        for source_key, result in zip(self.sources, results):
            if isinstance(result, BaseException):
                errors.append(result)
                logger.warning(
                    f"[endfield] stage catalog source={source_key} error={type(result).__name__}"
                )
            else:
                catalogs.append(result)
        if not catalogs:
            if errors:
                raise errors[-1]
            raise StageDataIncomplete("暂无可用的关卡数据源。")
        return _merge_catalogs(catalogs)

    async def discover_matches(self, query: str, source: str = "") -> tuple[StageMatch, ...]:
        catalog = await self.get_catalog_view(source)
        matches: list[StageMatch] = []
        for group in catalog.groups:
            for item in group.items:
                match = _match_item(query, item, source or item.source)
                if match is not None:
                    matches.append(match)
        return tuple(matches)

    async def get_stage_view(
        self,
        key: str,
        *,
        mode: str = "detail",
        selector: str = "",
        source: str = "akedata",
    ) -> StageCardView:
        stage, unreachable = await self._source(source or "akedata").stage(key)
        if mode == "overview":
            return StageCardView(stage, "overview", unreachable_enemies=unreachable)
        selected = select_variant(stage, selector)
        return StageCardView(stage, "detail", selected, unreachable_enemies=unreachable)

    def _source(self, source: str):
        adapter = self.sources.get(source)
        if adapter is None:
            raise StageDataIncomplete(f"{source or '该数据源'} 暂不支持关卡资料。")
        return adapter


def select_variant(stage: Stage, selector: str = "") -> StageVariant:
    variants = tuple(sorted(stage.variants, key=lambda item: item.sort_order))
    if not variants:
        raise StageVariantNotFound(stage.name, selector or "默认", variants)
    if not selector:
        return variants[-1]
    normalized = _normalize(selector)
    for variant in variants:
        if normalized in {_normalize(variant.label), _normalize(variant.id)}:
            return variant
    raise StageVariantNotFound(stage.name, selector, variants)


def _match_item(query: str, item: StageCatalogItem, source: str = "") -> StageMatch | None:
    raw_query = str(query or "").strip()
    if not raw_query:
        return None
    # An expanded entry shares its article title with its siblings, so matching on the
    # title would make every stage in that article answer to the same query.
    shared = (item.title, item.title.replace("/", "·")) if not item.entry_key else ()
    aliases = tuple(
        dict.fromkeys(
            value
            for value in (
                item.name,
                *shared,
                f"{item.region} {item.name}" if item.region else "",
                f"{item.region}·{item.name}" if item.region else "",
                f"{item.family_name} {item.name}",
                f"{item.family_name}·{item.name}",
            )
            if value
        )
    )
    exact_aliases = sorted(aliases, key=lambda value: len(_normalize(value)), reverse=True)
    normalized_query = _normalize(raw_query)
    for alias in exact_aliases:
        normalized_alias = _normalize(alias)
        if normalized_query == normalized_alias:
            return _stage_match(item, item.name, "", "detail", source)
        if normalized_query.startswith(normalized_alias):
            remainder = _selector_remainder(raw_query, alias)
            if remainder and len(remainder.split()) == 1:
                mode = "overview" if remainder == "总览" else "detail"
                selector = "" if mode == "overview" else remainder
                return _stage_match(item, item.name, selector, mode, source)
    return _stage_match(item, raw_query, "", "detail", source)


def _stage_match(
    item: StageCatalogItem,
    query_text: str,
    selector: str,
    mode: str,
    source: str,
) -> StageMatch:
    return StageMatch(
        key=item.stage_key,
        title=item.title,
        display_name=item.name,
        query_text=query_text,
        selector=selector,
        mode=mode,
        revision=item.revision,
        updated_at=item.updated_at,
        queryable=item.queryable,
        source=source or item.source,
    )


def _merge_catalogs(catalogs: list[StageCatalogView]) -> StageCatalogView:
    grouped: dict[str, tuple[str, list[StageCatalogItem]]] = {}
    for catalog in catalogs:
        for group in catalog.groups:
            if group.key not in grouped:
                grouped[group.key] = (group.name, [])
            grouped[group.key][1].extend(group.items)
    groups = tuple(
        StageCatalogGroup(key, name, tuple(items))
        for key, (name, items) in grouped.items()
    )
    digest = "|".join(f"{catalog.source}:{catalog.revision}" for catalog in catalogs)
    revision = sha1(digest.encode("utf-8")).hexdigest()[:16]
    updated_at = max((catalog.updated_at for catalog in catalogs), default="")
    return StageCatalogView(
        groups,
        "、".join(catalog.source for catalog in catalogs),
        revision,
        updated_at,
    )


def _selector_remainder(query: str, alias: str) -> str:
    if query.startswith(alias):
        return query[len(alias) :].strip(" ·/：:")
    normalized_alias = _normalize(alias)
    consumed = ""
    for index, char in enumerate(query):
        if char.isalnum():
            consumed += char.lower()
        if consumed == normalized_alias:
            return query[index + 1 :].strip(" ·/：:")
    return ""


def _normalize(value: str) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())
