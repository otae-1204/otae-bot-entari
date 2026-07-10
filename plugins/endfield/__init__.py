from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from time import perf_counter

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Event
from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from nepattern import AnyString

from configs.config import Config
from utils.async_cache import AsyncTTLCache, CacheStats
from utils.entari_native import ArgVal, ChainMsg, event_user_id, make_image, on_alconna
from utils.http_client import clear_http_cache, get_http_cache_stats
from utils.temp_files import schedule_temp_file_cleanup

from .client import WarfarinAPIError, WarfarinClient
from .commands import (
    EndfieldCandidate,
    CANDIDATE_SCORE_THRESHOLD,
    ParsedEndfieldCommand,
    ROOT_ALIASES,
    choose_candidate,
    dev_visible_for_user,
    format_candidates,
    format_help,
    format_not_found,
    format_source,
    format_unknown,
    parse_command,
    parse_shortcut_command,
    score_candidate,
)
from .draw import draw_operator_card, draw_weapon_card
from .service import EndfieldService
from .sources import source_label, source_order


client = WarfarinClient()
service = EndfieldService(client)
CARD_CACHE_TTL_SECONDS = 600.0
CARD_CACHE_MAX_BYTES = 48 * 1024 * 1024
CARD_RENDER_VERSION = "endfield-card-v7"
CardCacheKey = tuple[str, str, str, str]
_CARD_CACHE: AsyncTTLCache[CardCacheKey, bytes] = AsyncTTLCache(
    ttl_seconds=CARD_CACHE_TTL_SECONDS,
    max_bytes=CARD_CACHE_MAX_BYTES,
    max_entries=64,
    sizeof=len,
)

Resolver = Callable[[str], Awaitable[list[EndfieldCandidate]]]
Renderer = Callable[[str], Awaitable[bytes | None]]


CONTENT_RESOLVERS: dict[str, Resolver] = {
    "operator": lambda query: _resolve_candidates_from_sources("operator", query),
    "weapon": lambda query: _resolve_candidates_from_sources("weapon", query),
}

CONTENT_RENDERERS: dict[str, Renderer] = {
    "operator": lambda key: _render_operator(key),
    "weapon": lambda key: _render_weapon(key),
}

SOURCE_CANDIDATE_RESOLVERS: dict[str, dict[str, Resolver]] = {
    "operator": {
        "fz": lambda query: _resolve_operator_candidates_fz(query),
        "warfarin": lambda query: _resolve_operator_candidates_warfarin(query),
    },
    "weapon": {
        "fz": lambda query: _resolve_weapon_candidates_fz(query),
        "warfarin": lambda query: _resolve_weapon_candidates_warfarin(query),
    },
}


endfield_cmd = on_alconna(
    Alconna(list(ROOT_ALIASES), Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)

endfield_operator_shortcut = on_alconna(
    Alconna(["efop", "efoperator", "终末地干员"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)
endfield_weapon_shortcut = on_alconna(
    Alconna(["efwp", "efweapon", "终末地武器"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)
endfield_search_shortcut = on_alconna(
    Alconna(["efs", "efsearch", "终末地搜索"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)


@endfield_cmd.handle()
async def handle_endfield(event: Event, rest: ArgVal):
    await _handle_command(endfield_cmd, event, parse_command(_rest(rest)))


@endfield_operator_shortcut.handle()
async def handle_endfield_operator_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_operator_shortcut, event, parse_shortcut_command("efop", _rest(rest)))


@endfield_weapon_shortcut.handle()
async def handle_endfield_weapon_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_weapon_shortcut, event, parse_shortcut_command("efwp", _rest(rest)))


@endfield_search_shortcut.handle()
async def handle_endfield_search_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_search_shortcut, event, parse_shortcut_command("efs", _rest(rest)))


async def _handle_command(matcher, event: Event, command: ParsedEndfieldCommand) -> None:
    if command.action == "help":
        return await matcher.finish(format_help())
    if command.action == "source":
        return await matcher.finish(format_source())
    if command.action == "dev":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            return await matcher.finish(format_unknown())
        return await matcher.finish(await _handle_dev_command(command))
    if command.action not in {"query", "search"}:
        return await matcher.finish(format_unknown())
    if not command.query:
        return await matcher.finish(format_help())

    started = perf_counter()
    try:
        candidate_started = perf_counter()
        candidates = await _collect_candidates(command.scope, command.query)
        candidate_seconds = perf_counter() - candidate_started
        if command.action == "search":
            title = "搜索结果" if candidates else "未找到相关结果"
            logger.info(
                f"[endfield] perf action=search scope={command.scope} "
                f"candidate={candidate_seconds:.3f}s total={perf_counter() - started:.3f}s"
            )
            return await matcher.finish(format_candidates(candidates, title=title))

        selected, ambiguous = choose_candidate(candidates)
        if ambiguous:
            return await matcher.finish(format_candidates(ambiguous))
        if selected is None:
            return await matcher.finish(format_not_found(command.scope, command.query))

        render_started = perf_counter()
        png = await _render_candidate(selected)
        render_seconds = perf_counter() - render_started
        if png is None:
            return await matcher.finish(format_not_found(selected.kind, command.query))
        logger.info(
            f"[endfield] perf action=query scope={command.scope} kind={selected.kind} "
            f"candidate={candidate_seconds:.3f}s render={render_seconds:.3f}s "
            f"total_before_send={perf_counter() - started:.3f}s"
        )
        try:
            return await _finish_png(matcher, png)
        except _ExitException:
            raise
        except Exception as exc:
            logger.exception(f"[endfield] send failed for {selected.kind} {command.query}: {exc}")
            return await matcher.finish("图片发送失败，请稍后重试")
    except _ExitException:
        raise
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("数据源暂时不可用")
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("图片生成失败")


async def _collect_candidates(scope: str, query: str) -> list[EndfieldCandidate]:
    resolver_items = CONTENT_RESOLVERS.items() if scope == "all" else [(scope, CONTENT_RESOLVERS.get(scope))]
    tasks = [resolver(query) for _, resolver in resolver_items if resolver is not None]
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[EndfieldCandidate] = []
    api_errors: list[WarfarinAPIError] = []
    for result in results:
        if isinstance(result, WarfarinAPIError):
            api_errors.append(result)
            continue
        if isinstance(result, Exception):
            logger.warning(f"[endfield] resolver failed for {scope} {query}: {result}")
            continue
        candidates.extend(result)
    if not candidates and api_errors:
        raise api_errors[0]
    return _dedupe_candidates(candidates)


async def _resolve_candidates_from_sources(kind: str, query: str) -> list[EndfieldCandidate]:
    resolvers = SOURCE_CANDIDATE_RESOLVERS.get(kind, {})
    errors: list[WarfarinAPIError] = []
    for source in source_order(kind):
        resolver = resolvers.get(source)
        if resolver is None:
            continue
        try:
            candidates = await resolver(query)
        except WarfarinAPIError as exc:
            errors.append(exc)
            logger.warning(f"[endfield] {source_label(source)} resolver failed for {kind} {query}: {exc}")
            continue
        except Exception as exc:
            logger.warning(f"[endfield] {source_label(source)} resolver failed for {kind} {query}: {exc}")
            continue
        if candidates:
            return candidates
    if errors:
        raise errors[-1]
    return []


async def _resolve_operator_candidates_fz(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
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
        score = score_candidate(query, name, title)
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
            score = score_candidate(query, name, title)
            candidates.append(
                EndfieldCandidate(
                    kind="operator",
                    key=title,
                    display_name=name,
                    score=score or 70,
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
    if _looks_like_operator_slug(query):
        candidates.append(
            EndfieldCandidate(
                kind="operator",
                key=query,
                display_name=query,
                score=94,
                source="warfarin",
                reason="slug",
            )
        )

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") != "operators" or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_candidate(query, name, slug)
        candidates.append(
            EndfieldCandidate(
                kind="operator",
                key=slug,
                display_name=name,
                score=score or 70,
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
        score = score_candidate(query, name, slug)
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

    summaries = await client.fz_article_summaries(title_prefix)
    candidates: list[EndfieldCandidate] = []
    for item in summaries.get("articles") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        name = title.split("/", 1)[-1]
        score = score_candidate(query, name, title)
        if score >= CANDIDATE_SCORE_THRESHOLD:
            candidates.append(
                EndfieldCandidate(
                    kind="weapon",
                    key=title,
                    display_name=name,
                    score=score,
                    source="fz",
                    reason="title",
                )
            )
    return candidates


async def _resolve_weapon_candidates_warfarin(query: str) -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    query = _strip_title_prefix(query, "武器/")
    candidates: list[EndfieldCandidate] = []
    if _looks_like_operator_slug(query):
        candidates.append(
            EndfieldCandidate(
                kind="weapon",
                key=query,
                display_name=query,
                score=94,
                source="warfarin",
                reason="slug",
            )
        )

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") not in {"weapons", "weapon"} or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_candidate(query, name, slug)
        candidates.append(
            EndfieldCandidate(
                kind="weapon",
                key=slug,
                display_name=name,
                score=score or 70,
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
        score = score_candidate(query, name, slug)
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


async def _render_candidate(candidate: EndfieldCandidate) -> bytes | None:
    renderer = CONTENT_RENDERERS.get(candidate.kind)
    if renderer is None:
        return None
    cache_key = (CARD_RENDER_VERSION, candidate.kind, candidate.source, candidate.key)

    async def render() -> bytes:
        output = await renderer(candidate.key)
        if output is None:
            raise _CardNotFound
        return output

    try:
        output, cache_hit = await _CARD_CACHE.get_or_create_with_status(cache_key, render)
    except _CardNotFound:
        return None
    logger.info(
        f"[endfield] card-cache kind={candidate.kind} source={candidate.source} "
        f"hit={str(cache_hit).lower()} bytes={len(output)}"
    )
    return output


async def _render_operator(key: str) -> bytes | None:
    started = perf_counter()
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


async def _render_weapon(key: str) -> bytes | None:
    started = perf_counter()
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


async def _finish_png(matcher, png: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        await matcher.finish(ChainMsg([make_image(path=file.name)]))


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
            return "用法：/ef dev resolve <关键词>"
        candidates = await _collect_candidates("all", query)
        if not candidates:
            return "未找到候选。"
        lines = ["解析候选："]
        for item in sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:10]:
            lines.append(f"- {item.kind} {item.display_name} key={item.key} score={item.score} source={item.source}")
        return "\n".join(lines)
    if command.dev_action == "refresh":
        scope = _normalize_cache_scope(command.args[0] if command.args else "all")
        if scope is None or scope == "icon":
            return "用法：/ef dev refresh <all|干员|武器> [关键词]"
        query = " ".join(command.args[1:]).strip()
        removed = await _clear_endfield_caches(scope)
        if not query:
            return f"已刷新 {scope} 缓存，清除 {removed} 项。"
        candidates = await _collect_candidates(scope, query)
        selected, ambiguous = choose_candidate(candidates)
        if ambiguous:
            return format_candidates(ambiguous, title="刷新时找到多个可能结果")
        if selected is None:
            return format_not_found(scope, query)
        started = perf_counter()
        output = await _render_candidate(selected)
        if output is None:
            return format_not_found(selected.kind, query)
        return f"已刷新并预热 {selected.display_name}，耗时 {perf_counter() - started:.2f}s。"
    if command.dev_action == "cache":
        action = command.args[0].lower() if command.args else "status"
        if action == "clear":
            scope = _normalize_cache_scope(command.args[1] if len(command.args) > 1 else "all")
            if scope is None:
                return "用法：/ef dev cache clear <all|operator|weapon|icon>"
            removed = await _clear_endfield_caches(scope)
            return f"已清理 {scope} 缓存，共 {removed} 项。"
        return "\n".join(await _cache_status_lines())
    return "dev 命令：status | resolve | refresh | cache"


class _CardNotFound(Exception):
    pass


def _normalize_cache_scope(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"all", "全部"}:
        return "all"
    if normalized in {"operator", "op", "干员"}:
        return "operator"
    if normalized in {"weapon", "wp", "武器"}:
        return "weapon"
    if normalized in {"icon", "icons", "图标", "素材"}:
        return "icon"
    return None


async def _clear_endfield_caches(scope: str) -> int:
    removed = 0
    if scope == "all":
        removed += await _CARD_CACHE.clear()
        removed += await clear_http_cache("endfield-")
    elif scope == "icon":
        removed += await clear_http_cache("endfield-assets")
    elif scope in {"operator", "weapon"}:
        removed += await _CARD_CACHE.clear(lambda key: key[1] == scope)
        removed += await clear_http_cache("endfield-api")
    return removed


async def _cache_status_lines() -> list[str]:
    api_stats = await get_http_cache_stats("endfield-api")
    asset_stats = await get_http_cache_stats("endfield-assets")
    card_stats = await _CARD_CACHE.stats()
    return [
        _format_cache_stats("API", api_stats),
        _format_cache_stats("远程素材", asset_stats),
        _format_cache_stats("成品卡片", card_stats),
        f"缓存策略: TTL {int(CARD_CACHE_TTL_SECONDS)}s / 下载并发 8",
    ]


def _format_cache_stats(label: str, stats: CacheStats) -> str:
    return (
        f"{label}: {stats.entries} 项 / {stats.bytes / 1024 / 1024:.1f} MiB / "
        f"命中 {stats.hits} / 未命中 {stats.misses} / 合并 {stats.coalesced}"
    )


def _dedupe_candidates(candidates: list[EndfieldCandidate]) -> list[EndfieldCandidate]:
    by_key: dict[tuple[str, str], EndfieldCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, candidate.key)
        current = by_key.get(key)
        if current is None or candidate.score > current.score:
            by_key[key] = candidate
    return sorted(by_key.values(), key=lambda item: item.score, reverse=True)


def _looks_like_operator_slug(query: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I) is not None


def _strip_title_prefix(query: str, prefix: str) -> str:
    query = str(query or "").strip()
    if query.startswith(prefix):
        return query[len(prefix):]
    return query


def _rest(match: ArgVal) -> str:
    if not match.available:
        return ""
    value = match.result
    if isinstance(value, tuple):
        return " ".join(str(item) for item in value).strip()
    return str(value or "").strip()


def _parse_operator_query(rest: str) -> str:
    return _parse_query(rest)[1]


def _parse_query(rest: str) -> tuple[str, str]:
    command = parse_command(rest)
    return command.scope, command.query
