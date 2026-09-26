"""候选与视图。不 import `handlers`，也不打开 `AkeSnapshot`。

`build_view` 只在成功时返回视图：主表缺失抛 `AkeDataIncomplete`，道具效果
填不满抛 `PropEffectIncomplete`，这里不捕这两个异常。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..catalog.commands import (
    CANDIDATE_SCORE_THRESHOLD,
    EndfieldCandidate,
    score_entity_candidate,
)
from ..catalog.aliases import normalize_alias_text
from ..providers.repository import AkeDataIncomplete, AkeSnapshot
from . import archives, enemies, items, props, terms
from .models import (
    CatalogGroup,
    CatalogItem,
    EncyclopediaCatalogView,
    EncyclopediaIndex,
)

ALL_QUERY = "__all__"


def candidates(index: EncyclopediaIndex, kind: str, query: str) -> list[EndfieldCandidate]:
    if query == ALL_QUERY:
        return [
            EndfieldCandidate(
                kind=f"{kind}_catalog",
                key="",
                display_name=_catalog_title(kind),
                score=100,
                source="akedata",
                revision=index.revision,
            )
        ]
    normalized = normalize_alias_text(query)
    result: list[EndfieldCandidate] = []
    for entry in index.entries:
        if not entry.listed:
            # 从条目只有全名精确命中才进候选，且只返回那一条。
            if normalized and normalize_alias_text(entry.display_name) == normalized:
                return [
                    EndfieldCandidate(
                        kind=entry.kind,
                        key=entry.key,
                        display_name=entry.display_name,
                        score=100,
                        source="akedata",
                        revision=index.revision,
                    )
                ]
            continue
        score = score_entity_candidate(
            entry.kind, query, entry.display_name, *entry.extra_names
        )
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        result.append(
            EndfieldCandidate(
                kind=entry.kind,
                key=entry.key,
                display_name=entry.display_name,
                score=score,
                source="akedata",
                revision=index.revision,
            )
        )
    for type_key, type_name in index.types:
        if normalized and normalize_alias_text(type_name) == normalized:
            result.append(
                EndfieldCandidate(
                    kind=f"{kind}_catalog",
                    key=str(type_key),
                    display_name=type_name,
                    score=100,
                    source="akedata",
                    reason="type",
                    revision=index.revision,
                )
            )
            break
    return result


def prop_candidates(index: EncyclopediaIndex, query: str) -> list[EndfieldCandidate]:
    return candidates(index, "prop", query)


def medal_redirect(index: EncyclopediaIndex, query: str) -> bool:
    normalized = normalize_alias_text(query)
    return bool(normalized) and normalized in index.redirects


async def build_view(snapshot: AkeSnapshot, candidate: EndfieldCandidate):
    kind = candidate.kind
    if kind == "item":
        return await items.build_item_view(snapshot, candidate.key)
    if kind == "prop":
        return await props.build_prop_view(snapshot, candidate.key)
    if kind == "enemy":
        return await enemies.build_enemy_view(snapshot, candidate.key)
    if kind == "term":
        return await terms.build_term_view(snapshot, candidate.key)
    if kind.endswith("_catalog"):
        base = kind[: -len("_catalog")]
        return await catalog_view(snapshot, base, candidate.key)
    raise AkeDataIncomplete(f"unsupported encyclopedia kind: {kind}")


async def catalog_view(
    snapshot: AkeSnapshot, kind: str, type_key: str = ""
) -> EncyclopediaCatalogView:
    from .index import get_index

    index = await get_index(snapshot, kind)
    groups: list[CatalogGroup] = []
    for group_key, group_name in index.types:
        entries = index.group_entries(group_key)
        if not entries:
            continue
        show_items = bool(type_key) and str(group_key) == str(type_key)
        groups.append(
            CatalogGroup(
                key=str(group_key),
                name=group_name,
                count=len(entries),
                items=tuple(_catalog_item(entry) for entry in entries) if show_items else (),
            )
        )
    return EncyclopediaCatalogView(
        kind=kind,
        title=_catalog_title(kind),
        groups=tuple(groups),
        total=len(index.entries),
        source="akedata",
        revision=index.revision,
    )


def build_catalog_from_index(
    index: EncyclopediaIndex, type_key: str = ""
) -> EncyclopediaCatalogView:
    groups: list[CatalogGroup] = []
    for group_key, group_name in index.types:
        entries = index.group_entries(group_key)
        if not entries:
            continue
        show_items = bool(type_key) and str(group_key) == str(type_key)
        groups.append(
            CatalogGroup(
                key=str(group_key),
                name=group_name,
                count=len(entries),
                items=tuple(_catalog_item(entry) for entry in entries) if show_items else (),
            )
        )
    return EncyclopediaCatalogView(
        kind=index.kind,
        title=_catalog_title(index.kind),
        groups=tuple(groups),
        total=len(index.entries),
        source="akedata",
        revision=index.revision,
    )


def build_archive_catalog(entries: Sequence, version: str = "") -> EncyclopediaCatalogView:
    groups: dict[str, list] = {}
    for entry in entries:
        groups.setdefault(str(entry.group or ""), []).append(entry)
    return EncyclopediaCatalogView(
        kind="archive_entry",
        title="档案条目",
        groups=tuple(
            CatalogGroup(
                key=key,
                name=key,
                count=len(rows),
                items=tuple(_catalog_item(entry) for entry in rows),
            )
            for key, rows in groups.items()
        ),
        total=len(entries),
        source="akedata",
        revision=version,
    )


def _catalog_item(entry) -> CatalogItem:
    return CatalogItem(
        key=entry.key,
        name=entry.display_name,
        icon_url=entry.icon_url,
        rarity=entry.rarity,
        subtitle=entry.subtitle,
    )


def _catalog_title(kind: str) -> str:
    return {
        "item": "物品目录",
        "prop": "道具目录",
        "enemy": "敌人目录",
        "term": "词条目录",
        "archive_entry": "档案条目",
    }.get(kind, "目录")
