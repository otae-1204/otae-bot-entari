"""终末地资料查询（干员、武器、装备、图鉴、档案）、配装、随机百科、别名与 /ef dev 的实现；指令注册与派发在 plugins/endfield/handlers.py。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from time import perf_counter

from arclet.entari import Session
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.messaging.prompts import prompt

from ..caches import (
    _cache_status_lines,
    _CARD_CACHE,
    _clear_endfield_caches,
    _LOADOUT_CACHE,
    _normalize_cache_scope,
    CARD_RENDER_VERSION,
)
from ..cold_start import cold_start_command, notice_default_ake_public
from ..encyclopedia import (
    archives as encyclopedia_archives,
    draw as encyclopedia_draw,
    index as encyclopedia_index,
    service as encyclopedia_service,
)
from ..providers.registry import source_label, source_order
from ..providers.repository import AkeDataIncomplete, query_snapshot
from ..providers.warfarin import WarfarinAPIError
from ..rendering.cards import (
    draw_equipment_card,
    draw_equipment_catalog_card,
    draw_loadout_card_with_status,
    draw_operator_card,
    draw_operator_catalog_card,
    draw_weapon_card,
    draw_weapon_catalog_card,
)
from ..rendering.health import track_render_health
from ..replies import _finish_png, _finish_pngs, _IncompletePages
from ..stages.handlers import (
    _render_stage,
    _render_stage_catalog,
    _resolve_stage_candidates,
    _resolve_stage_candidates_akedata,
)
from ..state import archive_store, client, random_wiki_service, service
from .aliases import add_alias, alias_targets, FILE_KINDS as FILE_ALIAS_KINDS, normalize_alias_text
from .commands import (
    CANDIDATE_SCORE_THRESHOLD,
    choose_candidate,
    EndfieldCandidate,
    EquipmentAttributeFilter,
    format_candidates,
    format_equipment_attribute_filters,
    format_not_found,
    normalize_alias_kind,
    parse_equipment_attribute_filters,
    parse_loadout_spec,
    ParsedEndfieldCommand,
    ParsedLoadoutSpec,
    ROOT_ALIASES,
    SCOPE_LABELS,
    score_candidate,
    score_entity_candidate,
)
from .models import LoadoutView
from .random_wiki import is_public_wiki_candidate
from .service import build_fz_operator_catalog_view, build_fz_weapon_catalog_view


Resolver = Callable[..., Awaitable[list[EndfieldCandidate]]]


Renderer = Callable[[str, str], Awaitable[bytes | tuple[bytes, ...] | None]]


CONTENT_RESOLVERS: dict[str, Resolver] = {
    "operator": lambda query: _resolve_candidates_from_sources("operator", query),
    "weapon": lambda query: _resolve_candidates_from_sources("weapon", query),
    "equipment": lambda query: _resolve_candidates_from_sources("equipment", query),
    "stage": lambda query: _resolve_candidates_from_sources("stage", query),
    "item": lambda query: _resolve_candidates_from_sources("item", query),
    "prop": lambda query: _resolve_candidates_from_sources("prop", query),
    "enemy": lambda query: _resolve_candidates_from_sources("enemy", query),
    "term": lambda query: _resolve_candidates_from_sources("term", query),
    "archive_entry": lambda query: _resolve_archive_entry_candidates(query),
}


CONTENT_RENDERERS: dict[str, Renderer] = {
    "operator": lambda key, source: _render_operator(key, source),
    "operator_catalog": lambda key, source: _render_operator_catalog(key, source),
    "weapon": lambda key, source: _render_weapon(key, source),
    "weapon_catalog": lambda key, source: _render_weapon_catalog(key, source),
    "equipment": lambda key, source: _render_equipment(key, source),
    "equipment_catalog": lambda key, source: _render_equipment_catalog(key, source),
    "equipment_attribute": lambda key, source: _render_equipment_attribute(key, source),
    "stage": lambda key, source: _render_stage(key, source),
    "stage_catalog": lambda key, source: _render_stage_catalog(key, source),
    "item": lambda key, source: _render_encyclopedia("item", key, source),
    "item_catalog": lambda key, source: _render_encyclopedia("item", key, source, catalog=True),
    "prop": lambda key, source: _render_encyclopedia("prop", key, source),
    "prop_catalog": lambda key, source: _render_encyclopedia("prop", key, source, catalog=True),
    "enemy": lambda key, source: _render_encyclopedia("enemy", key, source),
    "enemy_catalog": lambda key, source: _render_encyclopedia("enemy", key, source, catalog=True),
    "term": lambda key, source: _render_encyclopedia("term", key, source),
    "term_catalog": lambda key, source: _render_encyclopedia("term", key, source, catalog=True),
    # 档案条目永远不打开 AkeSnapshot，渲染器自己取 archive_store 的当前快照。
    "archive_entry": lambda key, source: _render_archive_entry(key, source),
}


SOURCE_CANDIDATE_RESOLVERS: dict[str, dict[str, Resolver]] = {
    "operator": {
        "akedata": lambda query: _resolve_candidates_akedata("operator", query),
        "fz": lambda query: _resolve_operator_candidates_fz(query),
        "warfarin": lambda query: _resolve_operator_candidates_warfarin(query),
    },
    "weapon": {
        "akedata": lambda query: _resolve_candidates_akedata("weapon", query),
        "fz": lambda query: _resolve_weapon_candidates_fz(query),
        "warfarin": lambda query: _resolve_weapon_candidates_warfarin(query),
    },
    "equipment": {
        "akedata": lambda query, rarity: _resolve_candidates_akedata("equipment", query, rarity),
        "fz": lambda query, rarity: _resolve_equipment_candidates_fz(query, rarity),
    },
    "stage": {
        "akedata": lambda query: _resolve_stage_candidates_akedata(query),
    },
    # 图鉴的四个 AKE kind 只登记 akedata；lambda 是单参，只有 equipment 走双参。
    "item": {"akedata": lambda query: _resolve_encyclopedia_candidates("item", query)},
    "prop": {"akedata": lambda query: _resolve_encyclopedia_candidates("prop", query)},
    "enemy": {"akedata": lambda query: _resolve_encyclopedia_candidates("enemy", query)},
    "term": {"akedata": lambda query: _resolve_encyclopedia_candidates("term", query)},
    # 档案条目不走 SOURCE_CANDIDATE_RESOLVERS：快照来自 archive_store，不是 AkeData。
}


# 需要 `query_snapshot(candidate.revision)` 包住的 kind。
# archive_entry 永不加入：档案快照的 version 对不上 AKE manifest。
_AKE_SNAPSHOT_KINDS: frozenset[str] = frozenset(
    {
        "operator",
        "weapon",
        "equipment",
        "operator_catalog",
        "weapon_catalog",
        "equipment_catalog",
        "equipment_attribute",
        "item",
        "prop",
        "item_catalog",
        "prop_catalog",
        "enemy",
        "enemy_catalog",
        "term",
        "term_catalog",
    }
)


# AKE 渲染输入不完整时整卡回退 FZ 的 kind。新 kind 一律不进：
# _render_candidate 的 except 元组含 ValueError，而 PropEffectIncomplete 就是它。
_FZ_WHOLE_VIEW_FALLBACK_KINDS: frozenset[str] = frozenset(
    {
        "operator",
        "weapon",
        "equipment",
        "operator_catalog",
        "weapon_catalog",
        "equipment_catalog",
        "equipment_attribute",
    }
)


async def _handle_loadout(session, command: ParsedEndfieldCommand) -> None:
    try:
        if command.query:
            spec, error = parse_loadout_spec(command.query, command.enhance)
        else:
            spec, error = await _prompt_loadout_spec(session, command.enhance)
        if error or spec is None:
            await session.send(f"配装参数有误：{error or '已取消操作'}。")
            return

        with cold_start_command(session):
            await notice_default_ake_public()
            resolved: list[tuple[EndfieldCandidate, tuple[tuple[int, int], ...]]] = []
            for index, item in enumerate(spec.items):
                candidate_kind = "operator" if index == 0 else "gear"
                candidate = await _resolve_loadout_candidate(session, candidate_kind, item.name)
                if candidate is None:
                    label = "干员" if index == 0 else "武器或装备"
                    await session.send(f"未匹配到目标{label}：{item.name}。")
                    return
                if item.forge_levels and candidate.kind != "equipment":
                    await session.send(f"仅装备支持设定词条锻造等级：{item.name}。")
                    return
                resolved.append((candidate, item.forge_levels))

            operators = [item for item, _ in resolved if item.kind == "operator"]
            weapons = [item for item, _ in resolved if item.kind == "weapon"]
            if len(operators) != 1:
                await session.send("配装指令必须且仅可指定一位干员。")
                return
            if len(weapons) > 1:
                await session.send("配装指令最多仅可携带一把武器。")
                return
            operator = operators[0]
            weapon_title = weapons[0].key if weapons else await service.get_recommended_weapon_title(operator.key)
            equipment = [
                (candidate.key, command.enhance, forge_levels)
                for candidate, forge_levels in resolved
                if candidate.kind == "equipment"
            ]

            started = perf_counter()
            view = await service.get_loadout_view(
                operator.key,
                weapon_title,
                equipment,
                operator_level=command.char_level,
                operator_potential=command.char_potential,
                weapon_level=command.weapon_level,
                weapon_potential=command.weapon_potential,
                weapon_skill_levels=command.weapon_skill_levels,
            )
            data_seconds = perf_counter() - started
            png = await _render_loadout_view(view)
            logger.info(
                f"[endfield] perf action=loadout data={data_seconds:.3f}s "
                f"draw={perf_counter() - started - data_seconds:.3f}s"
            )
            return await _finish_png(session, png)
    except ExitState:
        raise
    except (WarfarinAPIError, ValueError) as exc:
        logger.warning(f"[endfield] loadout rejected: {exc}")
        await session.send(f"配装计算异常：{exc}")
        return
    except Exception as exc:
        logger.exception(f"[endfield] loadout failed: {exc}")
        await session.send("配装图生成失败，请稍后再试。")
        return


class _IncompleteLoadoutImage(Exception):
    def __init__(self, png: bytes):
        self.png = png


async def _render_loadout_view(view: LoadoutView) -> bytes:
    # Fetch/build the current view as before. Cache its exact contents, not
    # just the command: a recovered data source or changed stat invalidates it.
    digest = hashlib.sha256(
        json.dumps(asdict(view), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()

    async def render() -> bytes:
        png, complete = await draw_loadout_card_with_status(view)
        if not complete:
            raise _IncompleteLoadoutImage(png)
        return png

    try:
        png, hit = await _LOADOUT_CACHE.get_or_create_with_status((CARD_RENDER_VERSION, digest), render)
    except _IncompleteLoadoutImage as exc:
        return exc.png
    logger.info(f"[endfield] loadout-cache hit={str(hit).lower()} bytes={len(png)}")
    return png


async def _prompt_loadout_spec(session: Session, default_enhance: int) -> tuple[ParsedLoadoutSpec | None, str]:
    answer = await prompt(
        session,
        "请输入干员名称及可选的武器、装备名称（空格分隔，武器与装备位置不限）。\n"
        "单独自定义词条可在对应装备后追加（如：词条2锻造2）。",
        timeout=90,
    )
    if answer is None:
        return None, "等待输入超时。"
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if text.lower() in {"取消", "cancel", "q", "quit"}:
        return None, "已取消配置。"
    return parse_loadout_spec(text, default_enhance)


async def _resolve_loadout_candidate(session: Session, kind: str, query: str) -> EndfieldCandidate | None:
    def title_candidate(item):
        if item is not None and item.source == "akedata":
            prefix = {"operator": "干员", "weapon": "武器", "equipment": "装备"}[item.kind]
            return replace(item, key=f"{prefix}/{item.display_name}")
        return item
    raw_candidates = (
        await _collect_candidates("all", query, "", "all")
        if kind in {"all", "gear"}
        else await _resolve_candidates_from_sources(kind, query, "", "all")
    )
    if kind == "all":
        allowed_kinds = {"operator", "weapon", "equipment"}
    elif kind == "gear":
        allowed_kinds = {"weapon", "equipment"}
    else:
        allowed_kinds = {kind}
    candidates = [item for item in raw_candidates if item.kind in allowed_kinds and item.source in {"akedata", "fz"}]
    selected, ambiguous = choose_candidate(candidates)
    if selected is not None:
        return title_candidate(selected)
    options = ambiguous or sorted(candidates, key=lambda item: item.score, reverse=True)
    if not options:
        return None
    options = options[:8]
    lines = [f"“{query}”匹配到多个候选结果，请引用本条消息并回复对应编号："]
    lines.extend(f"{index}. {item.display_name}" for index, item in enumerate(options, 1))
    answer = await prompt(session, "\n".join(lines), timeout=60)
    if answer is None:
        return None
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    try:
        index = int(text.strip()) - 1
    except ValueError:
        return None
    return title_candidate(options[index]) if 0 <= index < len(options) else None


async def _collect_candidates(
    scope: str,
    query: str,
    source: str = "",
    rarity: str = "",
) -> list[EndfieldCandidate]:
    kinds = CONTENT_RESOLVERS if scope == "all" else (scope,)
    tasks = [_resolve_candidates_from_sources(kind, query, source, rarity) for kind in kinds]
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[EndfieldCandidate] = []
    errors: list[Exception] = []
    for result in results:
        # AkeDataIncomplete 不能掉进下面的 warning 分支被当成空列表。
        if isinstance(result, (WarfarinAPIError, AkeDataIncomplete)):
            errors.append(result)
            continue
        if isinstance(result, Exception):
            logger.warning(f"[endfield] resolver failed for {scope} {query}: {result}")
            continue
        candidates.extend(result)
    if not candidates and errors:
        raise errors[0]
    return _dedupe_candidates(candidates)


async def _resolve_candidates_from_sources(
    kind: str,
    query: str,
    requested_source: str = "",
    rarity: str = "",
) -> list[EndfieldCandidate]:
    if kind == "stage" and not requested_source:
        return await _resolve_stage_candidates(query)
    resolvers = SOURCE_CANDIDATE_RESOLVERS.get(kind, {})
    errors: list[Exception] = []
    # 某个 resolver 返回了列表（包括空列表）就算这个源成功；只有全部尝试都抛了
    # 才把最后一个错误抛出去，否则「前一个源失败、后一个源返回空」会误报成数据源故障。
    succeeded = False
    sources = (requested_source,) if requested_source else source_order(kind)
    for source in sources:
        resolver = resolvers.get(source)
        if resolver is None:
            continue
        try:
            candidates = await resolver(query, rarity) if kind == "equipment" else await resolver(query)
        except (WarfarinAPIError, AkeDataIncomplete) as exc:
            errors.append(exc)
            logger.warning(f"[endfield] {source_label(source)} resolver failed for {kind} {query}: {exc}")
            continue
        except Exception as exc:
            logger.warning(f"[endfield] {source_label(source)} resolver failed for {kind} {query}: {exc}")
            continue
        succeeded = True
        if candidates:
            return candidates
    if succeeded:
        return []
    if errors:
        raise errors[-1]
    return []


async def _item_scope_fallback(
    command: ParsedEndfieldCommand,
    candidates: list[EndfieldCandidate],
) -> list[EndfieldCandidate] | str | None:
    """`/ef 物品 <词>` 的补查：只在物品候选为空时做，顺序固定。

    返回列表交给 choose_candidate；返回 "medal" 时 handler 提示走 /ef 奖章；
    返回 None 表示候选已非空或范围不是物品，保持原样。
    """
    if command.scope != "item":
        return None
    if not command.query or command.query == encyclopedia_service.ALL_QUERY:
        return None
    if candidates:
        return None
    async with query_snapshot() as data:
        index = await encyclopedia_index.get_index(data, "prop")
        props = encyclopedia_service.prop_candidates(index, command.query)
    if props:
        return props
    equipment = await _resolve_candidates_from_sources("equipment", command.query, command.source, command.rarity)
    if equipment:
        return equipment
    weapons = await _resolve_candidates_from_sources("weapon", command.query, command.source)
    if weapons:
        return weapons
        
    async with query_snapshot() as data:
        index = await encyclopedia_index.get_index(data, "item")
        if encyclopedia_service.medal_redirect(index, command.query):
            return "medal"
    return []


async def _render_encyclopedia(
    kind: str, key: str, source: str = "", *, catalog: bool = False
) -> bytes | tuple[bytes, ...] | None:
    """图鉴卡渲染：只认 AkeData，绝不回退 FZ。"""
    if source and source != "akedata":
        return None
    candidate = EndfieldCandidate(
        kind=f"{kind}_catalog" if catalog else kind,
        key=key,
        display_name=key,
        score=100,
        source="akedata",
    )
    async with query_snapshot() as data:
        if catalog:
            view = await encyclopedia_service.catalog_view(data, kind, key)
            return await encyclopedia_draw.draw_catalog_cards(view)
        view = await encyclopedia_service.build_view(data, candidate)
    return await _DRAWERS[kind](view)


async def _render_archive_entry(key: str, source: str = "") -> bytes | None:
    """档案条目卡：只读 archive_store 的当前快照，不开 AkeSnapshot。"""
    del source
    view = archive_store.load_current_view()
    if view is None:
        raise _CardNotFound
    entry = encyclopedia_archives.entry_view(view, key)
    return await encyclopedia_draw.draw_archive_entry_card(entry)


_DRAWERS = {
    "item": encyclopedia_draw.draw_item_card,
    "prop": encyclopedia_draw.draw_prop_card,
    "enemy": encyclopedia_draw.draw_enemy_card,
    "term": encyclopedia_draw.draw_term_card,
}


async def _resolve_encyclopedia_candidates(kind: str, query: str) -> list[EndfieldCandidate]:
    """图鉴的四个 AKE kind：自己开 snapshot，取索引，再打分。"""
    query = query.strip()
    if not query:
        return []
    async with query_snapshot() as data:
        index = await encyclopedia_index.get_index(data, kind)
        return encyclopedia_service.candidates(index, kind, query)


async def _resolve_archive_entry_candidates(query: str) -> list[EndfieldCandidate]:
    """档案条目：快照为 None 时返回空列表，不抛、不打 warning。"""
    query = query.strip()
    if not query:
        return []
    view = archive_store.load_current_view()
    if view is None:
        return []
    entries = encyclopedia_archives.build_entries(view)
    candidates: list[EndfieldCandidate] = []
    for entry in entries:
        score = score_entity_candidate(entry.kind, query, entry.display_name, *entry.extra_names)
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        candidates.append(
            EndfieldCandidate(
                kind="archive_entry",
                key=entry.key,
                display_name=entry.display_name,
                score=score,
                source="akedata",
                revision=str(view.version or ""),
            )
        )
    return candidates


async def _resolve_candidates_akedata(
    kind: str, query: str, rarity: str = ""
) -> list[EndfieldCandidate]:
    """Resolve stable IDs, aliases and all existing catalog/filter commands."""
    from ..providers.repository import query_snapshot

    query = query.strip()
    if not query:
        return []
    rarity = rarity or "gold"
    async with query_snapshot() as data:
        candidates = []

        def add(content, key, name, score=100, reason="catalog"):
            if score >= CANDIDATE_SCORE_THRESHOLD:
                candidates.append(
                    EndfieldCandidate(
                        kind=content,
                        key=key,
                        display_name=name,
                        score=score,
                        source="akedata",
                        reason=reason,
                        revision=data.revision,
                    )
                )

        if query == "__all__":
            key = {
                "operator": _operator_catalog_key("", ""),
                "weapon": "",
                "equipment": _equipment_catalog_key("", rarity),
            }[kind]
            add(
                kind + "_catalog",
                key,
                {
                    "operator": "全部干员",
                    "weapon": "全部武器",
                    "equipment": "全部装备套组",
                }[kind],
            )
            return candidates
        if query.startswith(
            {"operator": "chr_", "weapon": "wpn_", "equipment": "item_equip_"}[kind]
        ):
            from ..providers.repository import localize

            table = {
                "operator": "CharGrowthTable",
                "weapon": "WeaponBasicTable",
                "equipment": "EquipTable",
            }[kind]
            entities, items, texts = await data.tables(
                table, "ItemTable", "I18nTextTable_CN"
            )
            if query in entities and query in items:
                add(
                    kind,
                    query,
                    localize(items[query]["name"], texts),
                    reason="stable-id",
                )
                return candidates
        if kind == "equipment":
            filters = parse_equipment_attribute_filters(query)
            if filters:
                add(
                    "equipment_attribute",
                    _equipment_attribute_key(filters, rarity),
                    format_equipment_attribute_filters(filters),
                    reason="attribute",
                )
                return candidates
        if kind == "operator":
            view = await service.get_operator_catalog_view(source="akedata")
            professions = set()
            for element in view.elements:
                add(
                    "operator_catalog",
                    _operator_catalog_key(element.name, ""),
                    f"{element.name}干员",
                    score_candidate(query, element.name, f"{element.name}干员"),
                    "element",
                )
                for profession in element.professions:
                    professions.add(profession.name)
                    for item in profession.items:
                        add(
                            kind,
                            item.operator_id,
                            item.name,
                            score_entity_candidate(
                                kind,
                                query,
                                item.name,
                                item.english_name,
                                item.title,
                                item.operator_id,
                            ),
                            "catalog-item",
                        )
            for name in professions:
                add(
                    "operator_catalog",
                    _operator_catalog_key("", name),
                    f"{name}干员",
                    score_candidate(query, name, f"{name}干员"),
                    "profession",
                )
        elif kind == "weapon":
            view = await service.get_weapon_catalog_view(source="akedata")
            for group in view.groups:
                add(
                    "weapon_catalog",
                    group.name,
                    f"{group.name}武器",
                    score_candidate(query, group.name, f"{group.name}武器"),
                    "weapon-type",
                )
                for item in group.items:
                    add(
                        kind,
                        item.weapon_id,
                        item.name,
                        score_entity_candidate(
                            kind,
                            query,
                            item.name,
                            item.english_name,
                            item.title,
                            item.weapon_id,
                        ),
                        "catalog-item",
                    )
        else:
            view = await service.get_equipment_catalog_view(
                rarity_filter=rarity, include_details=False, source="akedata"
            )
            for group in view.groups:
                base = _equipment_group_base(group.name)
                add(
                    "equipment_catalog",
                    _equipment_catalog_key(group.name, rarity),
                    group.name,
                    score_candidate(query, group.name, base, f"{base}套装"),
                    "group",
                )
                for item in group.items:
                    add(
                        kind,
                        item.equipment_id,
                        item.name,
                        score_entity_candidate(
                            kind, query, item.name, item.title, item.equipment_id
                        ),
                        "catalog-item",
                    )
        return candidates


async def _resolve_operator_candidates_fz(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    if query == "__all__":
        return [
            EndfieldCandidate(
                kind="operator_catalog",
                key=_operator_catalog_key("", ""),
                display_name="全部干员",
                score=100,
                source="fz",
                reason="catalog",
            )
        ]
    title_prefix = "干员/"
    if query.startswith(title_prefix):
        name = query.split("/", 1)[-1]
        return [
            EndfieldCandidate(
                kind="operator",
                key=query,
                display_name=name,
                score=100,
                source="fz",
                reason="title",
            )
        ]

    candidates: list[EndfieldCandidate] = []
    professions: dict[str, str] = {}
    try:
        catalog = build_fz_operator_catalog_view(await client.fz_article_by_title("干员"))
    except Exception:
        catalog = None
    if catalog is not None:
        for element in catalog.elements:
            element_score = score_candidate(query, element.name, f"{element.name}干员")
            if element_score >= CANDIDATE_SCORE_THRESHOLD:
                candidates.append(
                    EndfieldCandidate(
                        kind="operator_catalog",
                        key=_operator_catalog_key(element.name, ""),
                        display_name=f"{element.name}干员",
                        score=element_score,
                        source="fz",
                        reason="element",
                    )
                )
            for profession in element.professions:
                professions.setdefault(profession.name, profession.name)
                for item in profession.items:
                    score = score_entity_candidate("operator", query, item.name, item.english_name, item.title)
                    if score >= CANDIDATE_SCORE_THRESHOLD:
                        candidates.append(
                            EndfieldCandidate(
                                kind="operator",
                                key=item.title,
                                display_name=item.name,
                                score=score,
                                source="fz",
                                reason="catalog-item",
                            )
                        )
    for profession in professions:
        profession_score = score_candidate(query, profession, f"{profession}干员")
        if profession_score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="operator_catalog",
                    key=_operator_catalog_key("", profession),
                    display_name=f"{profession}干员",
                    score=profession_score,
                    source="fz",
                    reason="profession",
                )
            )
    if candidates:
        return candidates

    errors: list[WarfarinAPIError] = []
    try:
        summaries = await client.fz_article_summaries(title_prefix)
    except WarfarinAPIError as exc:
        summaries = {}
        errors.append(exc)
    for item in summaries.get("articles") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        name = title.split("/", 1)[-1]
        score = score_entity_candidate("operator", query, name, title)
        if score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="operator",
                    key=title,
                    display_name=name,
                    score=score,
                    source="fz",
                    reason="summary",
                )
            )

    if not candidates:
        try:
            search_data = await client.fz_search(query)
        except WarfarinAPIError as exc:
            search_data = {}
            errors.append(exc)
        for item in search_data.get("hits") or []:
            title = str(item.get("title") or "").strip()
            if not title.startswith(title_prefix):
                continue
            name = title.split("/", 1)[-1]
            score = score_entity_candidate("operator", query, name, title)
            if score < CANDIDATE_SCORE_THRESHOLD:
                continue
            candidates.append(
                EndfieldCandidate(
                    kind="operator",
                    key=title,
                    display_name=name,
                    score=score,
                    source="fz",
                    reason="search",
                )
            )
    if candidates:
        return candidates
    if errors:
        raise errors[-1]
    return []


async def _resolve_operator_candidates_warfarin(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    query = _strip_title_prefix(query, "干员/")
    candidates: list[EndfieldCandidate] = []

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") != "operators" or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_entity_candidate("operator", query, name, slug)
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        candidates.append(
            EndfieldCandidate(
                kind="operator",
                key=slug,
                display_name=name,
                score=score,
                source="warfarin",
                reason="search",
            )
        )

    operators_data = await client.operators()
    for item in operators_data.get("data") or []:
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        if not slug or not name:
            continue
        score = score_entity_candidate("operator", query, name, slug)
        if score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="operator",
                    key=slug,
                    display_name=name,
                    score=score,
                    source="warfarin",
                    reason="name",
                )
            )
    return candidates


async def _resolve_weapon_candidates_fz(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    if query == "__all__":
        return [
            EndfieldCandidate(
                kind="weapon_catalog",
                key="",
                display_name="全部武器",
                score=100,
                source="fz",
                reason="catalog",
            )
        ]
    title_prefix = "武器/"
    if query.startswith(title_prefix):
        name = query.split("/", 1)[-1]
        return [
            EndfieldCandidate(
                kind="weapon",
                key=query,
                display_name=name,
                score=100,
                source="fz",
                reason="title",
            )
        ]

    catalog = build_fz_weapon_catalog_view(await client.fz_article_by_title("武器"))
    candidates: list[EndfieldCandidate] = []
    for group in catalog.groups:
        group_score = score_candidate(query, group.name, f"{group.name}武器")
        if group_score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="weapon_catalog",
                    key=group.name,
                    display_name=f"{group.name}武器",
                    score=group_score,
                    source="fz",
                    reason="weapon-type",
                )
            )
        for item in group.items:
            score = score_entity_candidate("weapon", query, item.name, item.english_name, item.title)
            if score >= CANDIDATE_SCORE_THRESHOLD:
                candidates.append(
                    EndfieldCandidate(
                        kind="weapon",
                        key=item.title,
                        display_name=item.name,
                        score=score,
                        source="fz",
                        reason="catalog-item",
                    )
                )
    return candidates


async def _resolve_weapon_candidates_warfarin(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    query = _strip_title_prefix(query, "武器/")
    candidates: list[EndfieldCandidate] = []

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") not in {"weapons", "weapon"} or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_entity_candidate("weapon", query, name, slug)
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        candidates.append(
            EndfieldCandidate(
                kind="weapon",
                key=slug,
                display_name=name,
                score=score,
                source="warfarin",
                reason="search",
            )
        )

    weapons_data = await client.weapons()
    for item in weapons_data.get("data") or []:
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        if not slug or not name:
            continue
        score = score_entity_candidate("weapon", query, name, slug)
        if score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="weapon",
                    key=slug,
                    display_name=name,
                    score=score,
                    source="warfarin",
                    reason="name",
                )
            )
    return candidates


async def _resolve_equipment_candidates_fz(
    query: str,
    rarity_filter: str = "",
) -> list[EndfieldCandidate]:
    query = query.strip()
    rarity_filter = rarity_filter or "gold"
    if not query:
        return []
    if query == "__all__":
        return [
            EndfieldCandidate(
                kind="equipment_catalog",
                key=_equipment_catalog_key("", rarity_filter),
                display_name="全部装备套组",
                score=100,
                source="fz",
                reason="catalog",
            )
        ]
    title_prefix = "装备/"
    if query.startswith(title_prefix):
        name = query.split("/", 1)[-1]
        return [
            EndfieldCandidate(
                kind="equipment",
                key=query,
                display_name=name,
                score=100,
                source="fz",
                reason="title",
            )
        ]

    attribute_filters = parse_equipment_attribute_filters(query)
    if attribute_filters:
        return [
            EndfieldCandidate(
                kind="equipment_attribute",
                key=_equipment_attribute_key(attribute_filters, rarity_filter),
                display_name=format_equipment_attribute_filters(attribute_filters),
                score=100,
                source="fz",
                reason="attribute",
            )
        ]

    catalog = await service.get_equipment_catalog_view_from_fz(rarity_filter=rarity_filter, include_details=False)
    candidates: list[EndfieldCandidate] = []
    for group in catalog.groups:
        group_base = _equipment_group_base(group.name)
        score = score_candidate(query, group.name, group_base, f"{group_base}套装")
        if score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="equipment_catalog",
                    key=_equipment_catalog_key(group.name, rarity_filter),
                    display_name=group.name,
                    score=score,
                    source="fz",
                    reason="group",
                )
            )
        for item in group.items:
            item_score = score_entity_candidate("equipment", query, item.name, item.title)
            if item_score < CANDIDATE_SCORE_THRESHOLD:
                continue
            candidates.append(
                EndfieldCandidate(
                    kind="equipment",
                    key=item.title,
                    display_name=item.name,
                    score=item_score,
                    source="fz",
                    reason="title",
                )
            )
    return candidates


async def _handle_random_wiki(session) -> None:
    with cold_start_command(session):
        await notice_default_ake_public()
        async for candidate in random_wiki_service.candidates():
            # Keep the boundary even if the service or renderer registry changes.
            if not is_public_wiki_candidate(candidate):
                continue
            try:
                pngs = await _render_candidate(candidate)
            except ExitState:
                raise
            except Exception as exc:  # noqa: BLE001 - Try another entry after any renderer failure.
                logger.warning(
                    "[endfield] random Wiki render failed kind={} error={}",
                    candidate.kind, type(exc).__name__,
                )
                continue
            if not pngs:
                continue
            try:
                return await _finish_pngs(session, pngs)
            except ExitState:
                raise
            except Exception:  # noqa: BLE001 - Transport failure must stop random selection.
                logger.exception("[endfield] random Wiki send failed")
                await session.send("图片发送失败，请稍后再试。")
                return
    await session.send("公开 Wiki 资料暂时不可用，请稍后再试。")


async def _render_candidate(
    candidate: EndfieldCandidate, requested_source: str = ""
) -> tuple[bytes, ...] | None:
    renderer = CONTENT_RENDERERS.get(candidate.kind)
    if renderer is None:
        return None
    effective_source = requested_source or candidate.source
    cache_source = effective_source or "auto"
    cache_key = (
        CARD_RENDER_VERSION,
        candidate.kind,
        cache_source,
        candidate.key,
        candidate.revision,
        candidate.mode,
        candidate.variant,
    )

    degraded = False

    async def render() -> tuple[bytes, ...]:
        nonlocal degraded
        with track_render_health() as health:
            if candidate.kind == "stage":
                output, degraded = await _render_stage(
                    candidate.key,
                    effective_source,
                    mode=candidate.mode or "detail",
                    selector=candidate.variant,
                )
            elif effective_source == "akedata" and candidate.kind in _AKE_SNAPSHOT_KINDS:
                try:
                    async with query_snapshot(candidate.revision):
                        output = await renderer(candidate.key, effective_source)
                except (WarfarinAPIError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    if requested_source or candidate.kind not in _FZ_WHOLE_VIEW_FALLBACK_KINDS:
                        raise
                    logger.warning("[endfield] AKE render input incomplete; whole-view FZ fallback ({}: {})", type(exc).__name__, exc)
                    prefix = {"operator": "干员", "weapon": "武器", "equipment": "装备"}.get(candidate.kind)
                    key = f"{prefix}/{candidate.display_name}" if prefix else candidate.key
                    if candidate.kind == "operator":
                        key = await service.find_fz_operator_title(candidate.key)
                        if not key:
                            raise _CardNotFound
                    output = await _render_candidate(replace(candidate, source="fz", key=key, revision=""), "fz")
                    degraded = True  # Never label/cache a fallback result as AKE-complete.
            else:
                output = await renderer(candidate.key, effective_source)
            if output is None:
                raise _CardNotFound
            pages = (output,) if isinstance(output, bytes) else tuple(output)
            if degraded or not health.complete:
                raise _IncompletePages(pages)
            return pages

    try:
        pages, cache_hit = await _CARD_CACHE.get_or_create_with_status(cache_key, render)
    except _CardNotFound:
        return None
    except _IncompletePages as exc:
        return exc.pages
    logger.info(
        f"[endfield] card-cache kind={candidate.kind} source={cache_source} "
        f"hit={str(cache_hit).lower()} pages={len(pages)} "
        f"bytes={sum(len(page) for page in pages)}"
    )
    return pages


async def _render_operator(key: str, source: str = "") -> bytes | None:
    started = perf_counter()
    if source == "fz":
        view = await service.get_operator_view_from_fz(key)
    elif source == "akedata":
        view = await service.get_operator_view_from_akedata(key)
    elif source == "warfarin":
        view = await service.get_operator_view_from_warfarin(key)
    else:
        view = await service.get_operator_view(key)
    if view is None:
        return None
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_operator_card(view)
    logger.info(
        f"[endfield] render kind=operator data={data_seconds:.3f}s "
        f"draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _render_weapon(key: str, source: str = "") -> bytes | None:
    started = perf_counter()
    if source == "fz":
        view = await service.get_weapon_view_from_fz(key)
    elif source == "akedata":
        view = await service.get_weapon_view_from_akedata(key)
    elif source == "warfarin":
        view = await service.get_weapon_view_from_warfarin(key)
    else:
        view = await service.get_weapon_view(key)
    if view is None:
        return None
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_weapon_card(view)
    logger.info(
        f"[endfield] render kind=weapon data={data_seconds:.3f}s "
        f"draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _render_equipment(key: str, source: str = "") -> bytes | None:
    if source and source not in {"fz", "akedata"}:
        return None
    started = perf_counter()
    if source == "fz":
        view = await service.get_equipment_view_from_fz(key)
    elif source == "akedata":
        view = await service.get_equipment_view_from_akedata(key)
    else:
        view = await service.get_equipment_view(key)
    if view is None:
        return None
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_equipment_card(view)
    logger.info(
        f"[endfield] render kind=equipment data={data_seconds:.3f}s "
        f"draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _render_operator_catalog(key: str, source: str = "") -> bytes | None:
    if source and source not in {"fz", "akedata"}:
        return None
    element, profession = _parse_operator_catalog_key(key)
    view = await service.get_operator_catalog_view(element, profession, source=source)
    return await draw_operator_catalog_card(view)


async def _render_weapon_catalog(key: str, source: str = "") -> bytes | None:
    if source and source not in {"fz", "akedata"}:
        return None
    view = await service.get_weapon_catalog_view(key, source=source)
    return await draw_weapon_catalog_card(view)


async def _render_equipment_catalog(key: str, source: str = "") -> bytes | None:
    if source and source not in {"fz", "akedata"}:
        return None
    started = perf_counter()
    group_name, rarity_filter = _parse_equipment_catalog_key(key)
    view = await service.get_equipment_catalog_view(group_name, rarity_filter, source=source)
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_equipment_catalog_card(view)
    logger.info(
        f"[endfield] render kind=equipment_catalog data={data_seconds:.3f}s "
        f"draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _render_equipment_attribute(key: str, source: str = "") -> bytes | None:
    if source and source not in {"fz", "akedata"}:
        return None
    filters, rarity_filter = _parse_equipment_attribute_key(key)
    if not filters:
        return None
    started = perf_counter()
    try:
        view = await service.get_equipment_attribute_catalog_view(filters, rarity_filter, source=source)
    except ValueError:
        return None
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_equipment_catalog_card(view)
    logger.info(
        f"[endfield] render kind=equipment_attribute items={view.total_count} "
        f"data={data_seconds:.3f}s draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _handle_dev_command(command: ParsedEndfieldCommand) -> str:
    if command.dev_action == "status":
        cache_lines = await _cache_status_lines()
        return "\n".join(
            [
                "Endfield dev status",
                f"根命令: {', '.join('/' + item for item in ROOT_ALIASES)}",
                f"内容类型: {', '.join(CONTENT_RESOLVERS)}",
                *cache_lines,
            ]
        )
    if command.dev_action == "resolve":
        query = " ".join(command.args).strip()
        if not query:
            return "指令格式：/ef dev resolve <关键词>。"
        candidates = await _collect_candidates("all", query)
        if not candidates:
            return "未匹配到解析候选。"
        lines = ["解析候选列表："]
        for item in sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:10]:
            lines.append(f"- {item.kind} {item.display_name} key={item.key} score={item.score} source={item.source}")
        return "\n".join(lines)
    if command.dev_action == "refresh":
        scope = _normalize_cache_scope(command.args[0] if command.args else "all")
        if scope is None or scope == "icon":
            return "指令格式：/ef dev refresh <all|干员|武器|装备|关卡> [关键词]。"
        query = " ".join(command.args[1:]).strip()
        removed = await _clear_endfield_caches(scope)
        if not query:
            return f"已刷新 {scope} 缓存，清理 {removed} 项。"
        candidates = await _collect_candidates(scope, query)
        selected, ambiguous = choose_candidate(candidates)
        if ambiguous:
            return format_candidates(ambiguous, title="刷新时匹配到多个可能结果")
        if selected is None:
            return format_not_found(scope, query)
        started = perf_counter()
        output = await _render_candidate(selected)
        if output is None:
            return format_not_found(selected.kind, query)
        return f"已成功刷新并预热 {selected.display_name}，耗时 {perf_counter() - started:.2f} 秒。"
    if command.dev_action == "cache":
        action = command.args[0].lower() if command.args else "status"
        if action == "clear":
            scope = _normalize_cache_scope(command.args[1] if len(command.args) > 1 else "all")
            if scope is None:
                return "指令格式：/ef dev cache clear <all|operator|weapon|equipment|stage|icon>。"
            removed = await _clear_endfield_caches(scope)
            return f"已清空 {scope} 缓存，共清除 {removed} 项。"
        return "\n".join(await _cache_status_lines())
    return "dev 支持的子指令：status | resolve | refresh | cache。"


async def _handle_alias_command(command: ParsedEndfieldCommand) -> str:
    usage = "指令格式：/ef 别名 添加 <干员|武器|装备|物品|道具|敌人|词条|档案> <正式名称> <新别名>。"
    if command.alias_action != "add" or len(command.args) < 3:
        return usage
    kind = normalize_alias_kind(command.args[0])
    if not kind:
        return usage
    label = SCOPE_LABELS.get(kind, kind)
    canonical_name = command.args[1]
    alias = " ".join(command.args[2:]).strip()
    lookup = None
    if kind not in FILE_ALIAS_KINDS:
        # 档案条目没有 AkeSnapshot 索引，正式名来自 archive_store 的当前快照。
        if kind != "archive_entry" and kind not in encyclopedia_index.supported_kinds():
            return f"{label}分类暂未开放，无法新增别名。"
        lookup = await _encyclopedia_lookup(kind)
    try:
        canonical, added = add_alias(kind, canonical_name, alias, lookup=lookup)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[endfield] alias update rejected: {exc}")
        return f"别名添加失败：{exc}"
    if not added:
        return f"{label}别名已存在：{alias} → {canonical}。"
    targets = alias_targets(kind, alias)
    collision = f"\n该别名同时指向：{'、'.join(targets)}" if len(targets) > 1 else ""
    return f"已成功添加{label}别名：{alias} → {canonical}。{collision}"


async def _encyclopedia_lookup(kind: str):
    """给 add_alias 的同步 lookup 预取一份正式名快照。

    档案条目不碰 AkeSnapshot：它的正式名只存在于档案库快照里。
    """
    if kind == "archive_entry":
        entries = encyclopedia_archives.build_entries(archive_store.load_current_view())
        return lambda query: _exact_archive_names(entries, query)

    async with query_snapshot() as data:
        index = await encyclopedia_index.get_index(data, kind)

    def lookup(query: str) -> tuple[str, ...]:
        return index.exact_names(kind, query)

    return lookup


def _exact_archive_names(entries, query: str) -> tuple[str, ...]:
    """档案条目的全等命中；与 EncyclopediaIndex.exact_names 同口径。"""
    normalized = normalize_alias_text(query)
    if not normalized:
        return ()
    return tuple(
        sorted(
            {
                entry.display_name
                for entry in entries
                if normalize_alias_text(entry.display_name) == normalized
            }
        )
    )


class _CardNotFound(Exception):
    pass


def _dedupe_candidates(candidates: list[EndfieldCandidate]) -> list[EndfieldCandidate]:
    by_key: dict[tuple[str, str], EndfieldCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, candidate.key)
        current = by_key.get(key)
        if current is None or candidate.score > current.score:
            by_key[key] = candidate
    return sorted(by_key.values(), key=lambda item: item.score, reverse=True)


def _strip_title_prefix(query: str, prefix: str) -> str:
    query = str(query or "").strip()
    if query.startswith(prefix):
        return query[len(prefix):]
    return query


def _equipment_group_base(name: str) -> str:
    name = str(name or "").strip()
    return name[:-3] if name.endswith("装备组") else name


def _operator_catalog_key(element: str, profession: str) -> str:
    return f"{element}::{profession}"


def _parse_operator_catalog_key(key: str) -> tuple[str, str]:
    element, separator, profession = str(key or "").partition("::")
    return (element, profession) if separator else (element, "")


def _equipment_catalog_key(group_name: str, rarity_filter: str) -> str:
    return f"{rarity_filter or 'gold'}::{group_name}"


def _parse_equipment_catalog_key(key: str) -> tuple[str, str]:
    rarity_filter, separator, group_name = str(key or "").partition("::")
    if not separator:
        return ("" if key == "__all__" else str(key or ""), "gold")
    return group_name, rarity_filter or "gold"


def _equipment_attribute_key(
    filters: tuple[EquipmentAttributeFilter, ...],
    rarity_filter: str,
) -> str:
    spec = "|".join(f"{item.role}:{item.attribute}" for item in filters)
    return f"{rarity_filter or 'gold'}::{spec}"


def _parse_equipment_attribute_key(key: str) -> tuple[tuple[EquipmentAttributeFilter, ...], str]:
    rarity_filter, separator, spec = str(key or "").partition("::")
    if not separator:
        return (), "gold"
    filters: list[EquipmentAttributeFilter] = []
    for part in spec.split("|"):
        role, _, attribute = part.partition(":")
        if attribute:
            filters.append(EquipmentAttributeFilter(attribute, role or "any"))
    return tuple(filters), rarity_filter or "gold"
