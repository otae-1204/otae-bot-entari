from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from time import perf_counter

import aiohttp
from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import At, Cleanup, Event, Text, listen
from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from nepattern import AnyString

from otae_bot.config.settings import Config
from otae_bot.attendance_registry import (
    AttendanceCapability,
    AttendanceResult,
    register_attendance_capability,
)
from otae_bot.infrastructure.cache import AsyncTTLCache, CacheStats
from otae_bot.adapters.entari import (
    ArgVal,
    ChainMsg,
    event_chain,
    event_user_id,
    get_bot,
    get_group_id,
    is_group,
    make_image,
    on_alconna,
    on_ready,
    prompt,
    prompt_silently,
    send_forward,
    timer,
)
from otae_bot.adapters.message_log import sensitive_input
from otae_bot.adapters.onebot import send_forward_images
from otae_bot.infrastructure.rendering.help_runtime import cached_help_image
from otae_bot.infrastructure.http.asset_policy import asset_render_budget
from otae_bot.infrastructure.http.client import clear_http_cache, get_http_cache_stats
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from .providers.warfarin import WarfarinAPIError, WarfarinClient
from .rendering.health import track_render_health
from .account.detail.names import clear_account_detail_name_map
from .account.investment.service import clear_account_investment_catalog
from .account.challenge.i18n import clear_challenge_locale, close_challenge_locale, fetch_challenge_locale
from .account.client import (
    ACCOUNT_PROVIDER_CN,
    ACCOUNT_PROVIDER_SKPORT,
    CURRENCY_TYPES,
    EndfieldAPIError,
    EndfieldOfficialClient,
    encode_account_credential,
    decode_account_credential,
    is_asia_role,
)
from .account.crypto import CredentialCipher, CredentialKeyError
from .account.store import EndfieldRole, EndfieldStore, RoleCandidate
from .account.i18n import server_label
from .account.currency.service import (
    aggregate_currency_logs,
    date_bounds as currency_date_bounds,
    earliest_currency_log_date,
    format_currency_log_report,
    format_all_history_period_label,
    resolve_query_dates,
    split_report,
)
from .account.currency.draw import draw_currency_log_cards
from .catalog.aliases import (
    FILE_KINDS as FILE_ALIAS_KINDS,
    add_alias,
    alias_targets,
    normalize_alias_text,
)
from .catalog.commands import (
    EndfieldCandidate,
    CANDIDATE_SCORE_THRESHOLD,
    EquipmentAttributeFilter,
    ParsedEndfieldCommand,
    ParsedLoadoutSpec,
    ROOT_ALIASES,
    choose_candidate,
    candidate_options,
    dev_visible_for_user,
    ENCYCLOPEDIA_SCOPES,
    format_candidates,
    format_equipment_attribute_filters,
    format_error,
    format_help,
    format_not_found,
    format_source,
    format_unknown,
    normalize_alias_kind,
    parse_command,
    parse_candidate_selection,
    parse_equipment_attribute_filters,
    parse_loadout_spec,
    parse_shortcut_command,
    SCOPE_LABELS,
    score_candidate,
    score_entity_candidate,
    strip_message_mentions,
)
from .rendering.cards import (
    clear_render_asset_caches,
    draw_archive_progress_card,
    draw_archive_stats_card,
    draw_equipment_card,
    draw_equipment_catalog_card,
    draw_loadout_card_with_status,
    draw_medal_stats_card,
    draw_medal_missing_card,
    draw_operator_card,
    draw_operator_catalog_card,
    draw_weapon_card,
    draw_weapon_catalog_card,
    draw_attendance_card,
    draw_daily_dashboard_card,
    draw_gacha_analysis_cards,
    draw_gacha_history_card,
)
from .calendar.draw import draw_version_calendar
from .calendar.official import OfficialVersionCalendarSource
from .calendar.official_draw import draw_official_version_calendar
from .account.detail.draw import draw_account_detail_cards
from .account.detail.service import build_account_detail_view, build_daily_account_view
from .account.detail.names import fetch_account_detail_name_map
from .account.base.draw import draw_account_base_card
from .account.base.service import build_account_base_view
from .account.investment.draw import draw_account_investment_cards
from .account.investment.service import (
    InvestmentDataUnavailable,
    build_account_investment_view,
    fetch_account_investment_catalog,
)
from .account.challenge.draw import (
    ChallengeAmbiguousError,
    ChallengeIdentity,
    ChallengeResolutionError,
    draw_challenge_empty,
    draw_monument_detail,
    draw_monument_history,
    draw_monument_history_pages,
    draw_monument_overview,
    draw_war_detail,
    draw_war_history,
    draw_war_history_pages,
    draw_war_overview,
    parse_monument,
    parse_war_echoes,
    resolve_monument_detail,
    resolve_war_detail,
)
from .account.challenge.i18n import ChallengeLocale, get_challenge_locale, start_challenge_locale_warmup
from .account.challenge.parsing import (
    _difficulty_label as _challenge_difficulty_label,
    _monument_difficulty_label as _challenge_monument_difficulty_label,
)
from .stages.draw import draw_stage_card, draw_stage_catalog_cards
from .stages.service import EndfieldStageService, StageVariantNotFound
from .stages.fz import StageDataIncomplete
from .gacha.service import (
    EndfieldGachaService,
    ROLE_TASKS,
    TaskAlreadyRunning,
    filter_xhh_import_six_stars,
    format_timestamp,
)
from .gacha.assets import EndfieldGachaAssetCache, apply_gacha_metadata
from .gacha.xhh import XhhAPIError, XhhLoginSession
from .catalog.models import (
    AttendanceCardView,
    DailyAccountView,
    DailyDashboardView,
    GachaHistoryItemView,
    GachaHistoryView,
    LoadoutView,
)
from .catalog.service import (
    EndfieldService,
    build_fz_operator_catalog_view,
    build_fz_weapon_catalog_view,
    format_status_quick_calc,
)
from .medals.store import MedalSnapshotStore
from .archives.store import ArchiveSnapshotStore
from .ownership.service import (
    GroupMemberListError,
    OwnershipRefreshResult,
    OwnershipStatsRendererUnavailable,
    OwnershipStatsService,
    collect_group_member_ids,
    is_group_manager,
    register_ownership_stats_renderer,
    render_ownership_stats,
)
from .ownership.draw import draw_ownership_stats
from .providers.akedata import clear_i18n_process_warm
from .providers.registry import source_label, source_order
from .cold_start import (
    cold_start_command,
    notice_before_public_data,
    notice_default_ake_public,
)
from .calendar.akedata import AkeDataVersionCalendarSource, VersionCalendarError
from .paths import HELP_IMAGE_PATH as ENDFIELD_HELP_IMAGE_PATH
from .attendance import (
    format_attendance_report,
    sign_roles as sign_attendance_roles,
)
from .providers.repository import AkeDataIncomplete, query_snapshot
from .encyclopedia import archives as encyclopedia_archives
from .encyclopedia import draw as encyclopedia_draw
from .encyclopedia import index as encyclopedia_index
from .encyclopedia import service as encyclopedia_service
from .encyclopedia.props import PropEffectIncomplete


client = WarfarinClient()
service = EndfieldService(client)
stage_service = EndfieldStageService(client)
gacha_asset_cache = EndfieldGachaAssetCache(service)
account_store = EndfieldStore()
official_client = EndfieldOfficialClient()
ownership_stats_service = OwnershipStatsService(account_store, official_client)
register_ownership_stats_renderer(draw_ownership_stats)
calendar_source = AkeDataVersionCalendarSource(client)
official_calendar_source = OfficialVersionCalendarSource()
medal_store = MedalSnapshotStore()
_MEDAL_LOCK = asyncio.Lock()
archive_store = ArchiveSnapshotStore()
_ARCHIVE_LOCK = asyncio.Lock()
_FORWARD_SENDER_NAME = "Endfield"
# 抽卡分析：超过 N 张图改为一条合并转发（每节点 1 张）；ENDFIELD_GACHA_FORWARD_ABOVE=0 关闭转发。
GACHA_FORWARD_ABOVE_ENV = "ENDFIELD_GACHA_FORWARD_ABOVE"
GACHA_FORWARD_ABOVE_DEFAULT = 3
GACHA_FALLBACK_BATCH = 3                # 合并转发不可用时，每条消息最多几张图
GACHA_FORWARD_TIMEOUT_SECONDS = 60.0    # OneBot 回退（多页大图）的 HTTP 超时
FORWARD_MAX_NODES = 50                  # 单条合并转发的节点上限，超出拆成多条
_GACHA_FORWARD_SENDER_NAME = "终末地抽卡分析"
CARD_CACHE_TTL_SECONDS = 600.0
CARD_CACHE_MAX_BYTES = 48 * 1024 * 1024
CARD_RENDER_VERSION = "endfield-card-v50"
CardCacheKey = tuple[str, str, str, str, str, str, str]
_CARD_CACHE: AsyncTTLCache[CardCacheKey, tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=CARD_CACHE_TTL_SECONDS,
    max_bytes=CARD_CACHE_MAX_BYTES,
    max_entries=64,
    # A card can render as several images, so bound the cache on total bytes, not page count.
    sizeof=lambda pages: sum(len(page) for page in pages),
)
_LOADOUT_CACHE: AsyncTTLCache[tuple[str, str], bytes] = AsyncTTLCache(
    ttl_seconds=60.0, max_bytes=24 * 1024 * 1024, max_entries=32, sizeof=len,
)
_ACCOUNT_PAGE_CACHE: AsyncTTLCache[tuple[str, ...], tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=60.0, max_bytes=32 * 1024 * 1024, max_entries=24,
    sizeof=lambda pages: sum(map(len, pages)),
)
_ASSET_GENERATION = 0
_CALENDAR_CACHE: AsyncTTLCache[str, bytes] = AsyncTTLCache(
    ttl_seconds=600.0,
    max_bytes=8 * 1024 * 1024,
    max_entries=4,
    sizeof=len,
)
CHALLENGE_CACHE_TTL_SECONDS = 60.0
ChallengeCacheKey = tuple
_CHALLENGE_DATA_CACHE: AsyncTTLCache[tuple[str, str, str], dict] = AsyncTTLCache(
    ttl_seconds=CHALLENGE_CACHE_TTL_SECONDS,
    max_bytes=12 * 1024 * 1024,
    max_entries=16,
    sizeof=lambda payload: len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
)
_CHALLENGE_RENDER_CACHE: AsyncTTLCache[ChallengeCacheKey, tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=CHALLENGE_CACHE_TTL_SECONDS,
    max_bytes=48 * 1024 * 1024,
    max_entries=64,
    sizeof=lambda pages: sum(len(page) for page in pages),
)

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
endfield_equipment_shortcut = on_alconna(
    Alconna(["efeq", "efequipment", "终末地装备"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)
endfield_search_shortcut = on_alconna(
    Alconna(["efs", "efsearch", "终末地搜索"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)


@endfield_cmd.handle()
async def handle_endfield(event: Event, rest: ArgVal, bot=None):
    command_rest = _rest(rest)
    command = parse_command(command_rest)
    if command.action == "challenge":
        command = parse_command(strip_message_mentions(command_rest))
    await _handle_command(endfield_cmd, event, command, bot=bot)


@endfield_operator_shortcut.handle()
async def handle_endfield_operator_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_operator_shortcut, event, parse_shortcut_command("efop", _rest(rest)))


@endfield_weapon_shortcut.handle()
async def handle_endfield_weapon_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_weapon_shortcut, event, parse_shortcut_command("efwp", _rest(rest)))


@endfield_equipment_shortcut.handle()
async def handle_endfield_equipment_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_equipment_shortcut, event, parse_shortcut_command("efeq", _rest(rest)))


@endfield_search_shortcut.handle()
async def handle_endfield_search_shortcut(event: Event, rest: ArgVal):
    await _handle_command(endfield_search_shortcut, event, parse_shortcut_command("efs", _rest(rest)))


async def _handle_command(matcher, event: Event, command: ParsedEndfieldCommand, bot=None) -> None:
    if command.error:
        return await matcher.finish(format_error(command.error))
    if command.action == "help":
        return await _finish_endfield_help(matcher)
    if command.action == "source":
        return await matcher.finish(format_source())
    if command.action == "calendar":
        with cold_start_command(matcher):
            await notice_default_ake_public()
            try:
                png = await _render_current_version_calendar()
                return await _finish_png(matcher, png)
            except _ExitException:
                raise
            except (VersionCalendarError, WarfarinAPIError, StageDataIncomplete) as exc:
                logger.error(f"[endfield] version calendar unavailable: {exc}")
                return await matcher.finish("版本日历数据暂不可用，请稍后再试。")
            except Exception:
                logger.exception("[endfield] version calendar render failed")
                return await matcher.finish("版本日历图生成失败，请稍后再试。")
    if command.action == "dev":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            return await matcher.finish(format_unknown())
        return await matcher.finish(await _handle_dev_command(command))
    if command.action == "alias":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            return await matcher.finish(format_unknown())
        return await matcher.finish(await _handle_alias_command(command))
    if command.action == "quick_calc":
        return await matcher.finish(
            format_status_quick_calc(command.status_name, command.status_level, command.arts_strength)
        )
    if command.action in {"medal_view", "medal_refresh"}:
        return await _handle_medal(matcher, command)
    if command.action in {"archive_view", "archive_refresh"}:
        return await _handle_archive(matcher, command)
    if command.action in {"ownership_stats", "ownership_refresh"}:
        return await _handle_ownership_stats(matcher, event, command, bot=bot)
    if command.action in {"bind", "accounts", "account_base", "account_investment", "currency_log", "primary", "unbind", "attendance", "daily", "gacha", "gacha_history", "gacha_sync", "gacha_import", "medal_missing", "archive_progress", "challenge"}:
        return await _handle_personal_command(matcher, event, command, bot=bot)
    if command.action == "loadout":
        return await _handle_loadout(matcher, command)
    if command.action not in {"query", "search"}:
        return await matcher.finish(format_unknown())
    if command.scope == "stage" and command.source and command.source != "akedata":
        return await matcher.finish(f"{source_label(command.source)} 暂不支持关卡资料，关卡数据仅支持 AkeData。")
    if (
        command.scope in ENCYCLOPEDIA_SCOPES
        and command.source
        and command.source not in source_order(command.scope)
    ):
        return await matcher.finish("该类资料仅由 AkeData 提供。")
    if command.scope == "archive_entry" and archive_store.load_current_view() is None:
        # 显式档案范围且没有快照：直接回文案，不抛异常、不走「资料暂时不可用」。
        return await matcher.finish("档案数据尚未构建，请先发送 /ef 档案 刷新。")
    if not command.query:
        if command.action == "query" and command.scope in {"operator", "weapon", "equipment"}:
            command = ParsedEndfieldCommand(
                "query",
                scope=command.scope,
                query="__all__",
                source=command.source,
                rarity=command.rarity,
            )
        elif command.action == "query" and command.scope == "stage":
            command = ParsedEndfieldCommand(
                "query",
                scope=command.scope,
                query="__all__",
                source=command.source,
                rarity=command.rarity,
            )
        elif command.action == "query" and command.scope in {"item", "prop", "enemy", "term"}:
            command = ParsedEndfieldCommand(
                "query",
                scope=command.scope,
                query="__all__",
                source=command.source,
                rarity=command.rarity,
            )
        else:
            return await _finish_endfield_help(matcher)

    started = perf_counter()
    try:
        with cold_start_command(matcher):
            if command.scope != "archive_entry":
                await notice_before_public_data(
                    source=command.source or "",
                    scope=command.scope,
                    query=command.query,
                )
            candidate_started = perf_counter()
            candidates = await _collect_candidates(command.scope, command.query, command.source, command.rarity)
            fallback = await _item_scope_fallback(command, candidates)
            if fallback == "medal":
                return await matcher.finish("奖章查询请使用 /ef 奖章 指令。")
            if fallback is not None:
                candidates = fallback
            candidate_seconds = perf_counter() - candidate_started
            if command.action == "search":
                title = "检索结果" if candidates else "未匹配到相关条目"
                logger.info(
                    f"[endfield] perf action=search scope={command.scope} "
                    f"candidate={candidate_seconds:.3f}s total={perf_counter() - started:.3f}s"
                )
                return await matcher.finish(
                    format_candidates(candidates, title=title, scope=command.scope, query=command.query)
                )

            selected, ambiguous = choose_candidate(candidates)
            if ambiguous:
                options = candidate_options(ambiguous, query=command.query)
                if not options:
                    return await matcher.finish(format_not_found(command.scope, command.query))
                answer = await prompt_silently(format_candidates(options, interactive=True), timeout=60)
                if answer is None:
                    return await matcher.finish()
                text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
                text = text.strip()
                if text.casefold() in {"取消", "cancel", "q", "quit"}:
                    return await matcher.finish("已取消候选选择。")
                selection = parse_candidate_selection(text, len(options))
                if selection is None:
                    return await matcher.finish(f"序号输入有误，请输入 1–{len(options)}。")
                selected = options[selection]
            if selected is None:
                return await matcher.finish(format_not_found(command.scope, command.query))

            render_started = perf_counter()
            pngs = await _render_candidate(selected, command.source)
            render_seconds = perf_counter() - render_started
            if pngs is None:
                return await matcher.finish(format_not_found(selected.kind, command.query))
            logger.info(
                f"[endfield] perf action=query scope={command.scope} kind={selected.kind} "
                f"candidate={candidate_seconds:.3f}s render={render_seconds:.3f}s "
                f"pages={len(pngs)} total_before_send={perf_counter() - started:.3f}s"
            )
            try:
                return await _finish_pngs(matcher, pngs)
            except _ExitException:
                raise
            except Exception as exc:
                logger.exception(f"[endfield] send failed for {selected.kind} {command.query}: {exc}")
                return await matcher.finish("图片发送失败，请稍后再试。")
    except _ExitException:
        raise
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {command.scope} {command.query}: {exc}")
        if command.scope == "stage":
            return await matcher.finish("关卡数据源暂时响应异常，请稍后再试。")
        return await matcher.finish("数据源暂时响应异常，请稍后再试。")
    except (StageVariantNotFound, StageDataIncomplete) as exc:
        return await matcher.finish(str(exc))
    except PropEffectIncomplete as exc:
        logger.warning(f"[endfield] prop effect incomplete for {command.scope} {command.query}: {exc}")
        return await matcher.finish("该道具效果数值暂未收录。")
    except AkeDataIncomplete as exc:
        logger.warning(f"[endfield] AKE data incomplete for {command.scope} {command.query}: {exc}")
        return await matcher.finish("该资料暂时不可用，请稍后再试。")
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("卡片图片生成失败，请稍后再试。")


async def _handle_medal(matcher, command: ParsedEndfieldCommand) -> None:
    """F1：查看蚀刻章统计/新增；刷新时重抓 AKEData 数据 + 上一版本基线（源和源对比）。"""
    if command.action == "medal_refresh":
        async with _MEDAL_LOCK:
            await matcher.send("正在同步 AKEData 蚀刻章最新数据…")
            started = perf_counter()
            try:
                snapshot = await service.fetch_medal_snapshot_akedata()
            except Exception as exc:
                logger.warning(f"[endfield] medal refresh failed: {exc}")
                return await matcher.finish("AKEData 奖章数据源响应异常，请稍后再试。")
            # 先抓基线，再成对写盘；基线暂时不可用时保留旧基线，避免丢失版本对比。
            try:
                baseline = await service.fetch_akedata_baseline()
            except Exception as exc:
                logger.warning(f"[endfield] medal baseline unavailable; keeping previous: {exc}")
                baseline = None
                baseline_available = False
            else:
                baseline_available = True
            try:
                if baseline_available:
                    await medal_store.replace_current_and_baseline(snapshot, baseline)
                else:
                    await medal_store.replace_current(snapshot)
            except Exception as exc:
                logger.exception(f"[endfield] medal snapshot persistence failed: {exc}")
                return await matcher.finish("蚀刻章本地快照保存失败，请稍后再试。")
            stored_baseline = medal_store.load_baseline_view()
            baseline_info = (
                f"{stored_baseline.version}({len(stored_baseline.ids)} ids)"
                if stored_baseline else "none"
            )
            logger.info(
                f"[endfield] medal snapshot refreshed medals={snapshot.total_count} "
                f"baseline={baseline_info} time={perf_counter() - started:.1f}s"
            )

    current = medal_store.load_current_view()
    if current is None:
        return await matcher.finish("本地暂无蚀刻章数据快照，请先执行 /ef 奖章 刷新。")
    baseline = medal_store.load_baseline_view()
    try:
        diff = service.build_medal_diff(current, baseline)
        if command.action == "medal_view":
            with cold_start_command(matcher):
                pngs = await draw_medal_stats_card(diff)
        else:
            pngs = await draw_medal_stats_card(diff)
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] medal card data failed: {exc}")
        return await matcher.finish("数据源暂时响应异常，请稍后再试。")
    except Exception as exc:
        logger.exception(f"[endfield] medal card failed: {exc}")
        return await matcher.finish("蚀刻章统计卡生成失败，请稍后再试。")
    return await _finish_pngs(matcher, pngs)


async def _handle_archive(matcher, command: ParsedEndfieldCommand) -> None:
    """档案库版本统计/新增；刷新时重抓 AKEData 档案表 + 上一版本基线（源和源对比）。"""
    if command.action == "archive_refresh":
        async with _ARCHIVE_LOCK:
            await matcher.send("正在同步 AKEData 档案库最新数据…")
            started = perf_counter()
            try:
                snapshot = await service.fetch_archive_snapshot_akedata()
            except Exception as exc:
                logger.warning(f"[endfield] archive refresh failed: {exc}")
                return await matcher.finish("AKEData 档案数据源响应异常，请稍后再试。")
            # 先抓基线，再成对写盘；基线暂时不可用时保留旧基线，避免丢失版本对比。
            try:
                baseline = await service.fetch_archive_baseline()
            except Exception as exc:
                logger.warning(f"[endfield] archive baseline unavailable; keeping previous: {exc}")
                baseline = None
                baseline_available = False
            else:
                baseline_available = True
            try:
                if baseline_available:
                    await archive_store.replace_current_and_baseline(snapshot, baseline)
                else:
                    await archive_store.replace_current(snapshot)
            except Exception as exc:
                logger.exception(f"[endfield] archive snapshot persistence failed: {exc}")
                return await matcher.finish("档案库本地快照保存失败，请稍后再试。")
            stored_baseline = archive_store.load_baseline_view()
            baseline_info = (
                f"{stored_baseline.version}({len(stored_baseline.ids)} ids)"
                if stored_baseline else "none"
            )
            logger.info(
                f"[endfield] archive snapshot refreshed items={snapshot.total_count} "
                f"baseline={baseline_info} time={perf_counter() - started:.1f}s"
            )

    current = archive_store.load_current_view()
    if current is None:
        return await matcher.finish("本地暂无档案库数据快照，请先执行 /ef 档案 刷新。")
    baseline = archive_store.load_baseline_view()
    try:
        diff = service.build_archive_diff(current, baseline)
        if command.action == "archive_view":
            with cold_start_command(matcher):
                pngs = await draw_archive_stats_card(diff)
        else:
            pngs = await draw_archive_stats_card(diff)
    except Exception as exc:
        logger.exception(f"[endfield] archive card failed: {exc}")
        return await matcher.finish("档案库统计卡生成失败，请稍后再试。")
    return await _finish_pngs(matcher, pngs)


async def _handle_ownership_stats(
    matcher,
    event: Event,
    command: ParsedEndfieldCommand,
    *,
    bot=None,
) -> None:
    group_chat = is_group(event)
    scope = command.scope
    if scope == "auto":
        scope = "group" if group_chat else "global"
    if scope == "group" and not group_chat:
        return await matcher.finish("私聊环境不支持群内统计，请使用 /ef 持有率 全局。")

    user_id = str(event_user_id(event))
    active_bot = bot
    if scope == "group" and active_bot is None:
        try:
            active_bot = get_bot()
        except RuntimeError:
            return await matcher.finish("群成员列表获取失败，请稍后再试。")

    if command.action == "ownership_refresh":
        if scope == "global":
            if not _is_endfield_superuser(user_id):
                return await matcher.finish("权限不足：仅机器人管理员可刷新全局持有率快照。")
        else:
            guild_id = get_group_id(event)
            if not _is_endfield_superuser(user_id) and not await is_group_manager(
                active_bot, event, guild_id, user_id
            ):
                return await matcher.finish("权限不足：仅群主、群管理员或机器人管理员可刷新本群持有率快照。")

    if scope == "group":
        try:
            member_ids = await collect_group_member_ids(active_bot, get_group_id(event))
        except GroupMemberListError as exc:
            logger.warning(
                "[endfield-ownership] group member listing failed "
                f"standard_error_type={exc.standard_error_type} "
                f"fallback_error_type={exc.fallback_error_type}"
            )
            return await matcher.finish("获取本群成员列表失败，已终止统计任务。")
        roles = account_store.list_all_roles(member_ids)
    else:
        roles = account_store.list_all_roles()

    refresh = None
    if command.action == "ownership_refresh":
        try:
            cipher = CredentialCipher.from_env()
            with cold_start_command(matcher):
                refresh = await ownership_stats_service.refresh_roles(
                    roles,
                    cipher,
                    force=True,
                    trigger=f"manual-{scope}",
                )
        except CredentialKeyError as exc:
            return await matcher.finish(str(exc))
        return await matcher.finish(_format_ownership_refresh_result(scope, refresh))

    report = ownership_stats_service.build_report(scope, roles, refresh=refresh)
    try:
        with cold_start_command(matcher):
            rendered = await render_ownership_stats(report)
    except OwnershipStatsRendererUnavailable:
        return await matcher.finish("持有率数据已统计完成，但渲染展示模块暂未就绪。")
    if isinstance(rendered, bytes):
        return await _finish_png(matcher, rendered)
    if isinstance(rendered, (list, tuple)) and all(isinstance(item, bytes) for item in rendered):
        return await _finish_pngs(matcher, tuple(rendered))
    return await matcher.finish(rendered)


def _is_endfield_superuser(user_id: str) -> bool:
    configured = Config.SUPERUSERS or ()
    if isinstance(configured, str):
        configured = [configured]
    return str(user_id) in {str(value) for value in configured}


def _format_ownership_refresh_result(scope: str, refresh: OwnershipRefreshResult) -> str:
    scope_label = "全局" if scope == "global" else "当前群"
    if refresh.catalog_error:
        catalog_label = f"干员目录核对失败（{refresh.catalog_error}）"
    elif not refresh.catalog_checked:
        catalog_label = "未核对干员目录"
    else:
        catalog_label = "干员目录已更新" if refresh.catalog_updated else "干员目录无变动"
    elapsed = max(0, int(refresh.finished_at) - int(refresh.started_at))
    eligible = refresh.eligible or refresh.attempted
    result = (
        f"{scope_label}干员持有率刷新完毕：符合条件 {eligible} 个，加入队列 {refresh.attempted} 个，"
        f"发起查询 {refresh.requested} 个，成功 {refresh.succeeded} 个，失败 {refresh.failed} 个，"
        f"跳过 {refresh.skipped} 个，延后 {refresh.deferred} 个；{catalog_label}；"
        f"总计耗时 {elapsed} 秒。"
    )
    if refresh.issues:
        result += "异常归类：" + "、".join(
            f"{item.label} × {item.count}" for item in refresh.issues[:5]
        ) + "。"
    if refresh.stopped_early:
        result += f"{refresh.stop_reason}；历史快照在 48 小时有效期内仍将继续生效。"
    elif refresh.failed:
        result += "更新失败通常由于登录态失效或接口波动，账号持有者可私聊发送 /ef 绑定 重新授权。"
    return result


async def _handle_medal_missing(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    """F2：查询绑定账号未获得/未升满/未镀层的蚀刻章。"""
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    snapshot = medal_store.load_current_view()
    if snapshot is None:
        return await matcher.finish("本地暂无蚀刻章数据快照，请先发送 /ef 奖章 刷新。")
    try:
        async with ROLE_TASKS.claim(role):
            token = account_store.decrypt_token(role, cipher)
            raw_progress = await official_client.endfield_card_detail(token, role)
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-medal] player progress API failed: {exc}")
        return await matcher.finish("奖章进度查询失败，请稍后再试。")
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except Exception as exc:
        logger.exception(f"[endfield-medal] progress query failed: {exc}")
        return await matcher.finish("奖章进度查询异常，请稍后再试。")
    view = service.build_medal_missing_view(
        raw_progress, snapshot,
        nickname=role.nickname, uid=role.masked_uid, server_name=server_label(role.server_name or role.server_id),
    )
    try:
        pngs = await draw_medal_missing_card(view)
    except Exception as exc:
        logger.exception(f"[endfield-medal] missing card failed: {exc}")
        return await matcher.finish("缺章统计图生成失败，请稍后再试。")
    return await _finish_pngs(matcher, pngs)


async def _handle_archive_progress(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    """查询绑定账号的档案收集进度。森空岛只给 docNum 总数、无逐条明细，仅展示已获得/总数。"""
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    snapshot = archive_store.load_current_view()
    if snapshot is None:
        return await matcher.finish("本地暂无档案库数据快照，请先发送 /ef 档案 刷新。")
    try:
        async with ROLE_TASKS.claim(role):
            token = account_store.decrypt_token(role, cipher)
            raw_detail = await official_client.endfield_card_detail(token, role)
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-archive] player progress API failed: {exc}")
        return await matcher.finish("档案进度查询失败，请稍后再试。")
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except Exception as exc:
        logger.exception(f"[endfield-archive] progress query failed: {exc}")
        return await matcher.finish("档案进度查询异常，请稍后再试。")
    view = service.build_archive_progress_view(
        raw_detail, snapshot,
        nickname=role.nickname, uid=role.masked_uid, server_name=server_label(role.server_name or role.server_id),
    )
    try:
        pngs = await draw_archive_progress_card(view)
    except Exception as exc:
        logger.exception(f"[endfield-archive] progress card failed: {exc}")
        return await matcher.finish("档案进度图生成失败，请稍后再试。")
    return await _finish_pngs(matcher, pngs)


async def _handle_personal_command(matcher, event: Event, command: ParsedEndfieldCommand, bot=None) -> None:
    private_only = {"bind", "primary", "unbind", "gacha_import"}
    if command.action in private_only and is_group(event):
        return await matcher.finish("该功能涉及个人隐私凭证或手机号，请在私聊中发起。")
    qq_user_id = str(event_user_id(event))

    try:
        if command.action == "bind":
            cipher = CredentialCipher.from_env()
            # The [message] log hides codes and tokens this user sends until the dialog ends.
            with sensitive_input(qq_user_id):
                return await _handle_binding(matcher, qq_user_id, cipher)
        if command.action == "challenge":
            cipher = CredentialCipher.from_env()
            return await _handle_challenge(matcher, event, qq_user_id, command, cipher, bot=bot)
        if command.action == "accounts":
            cipher = CredentialCipher.from_env()
            return await _handle_accounts(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "account_base":
            cipher = CredentialCipher.from_env()
            return await _handle_account_base(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "account_investment":
            cipher = CredentialCipher.from_env()
            return await _handle_account_investment(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "currency_log":
            cipher = CredentialCipher.from_env()
            return await _handle_account_currency(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "primary":
            role = account_store.set_primary(qq_user_id, command.account_selector)
            return await matcher.finish(
                f"已将 {role.nickname}（{role.role_id}）设为默认主账号。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
            )
        if command.action == "unbind":
            role = account_store.unbind(qq_user_id, command.account_selector)
            return await matcher.finish(
                f"已解除绑定角色：{role.nickname}（{role.role_id}）。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
            )
        if command.action == "attendance":
            cipher = CredentialCipher.from_env()
            return await _handle_attendance(matcher, qq_user_id, command, cipher)
        if command.action == "daily":
            cipher = CredentialCipher.from_env()
            return await _handle_daily(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "medal_missing":
            cipher = CredentialCipher.from_env()
            return await _handle_medal_missing(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "archive_progress":
            cipher = CredentialCipher.from_env()
            return await _handle_archive_progress(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action in {"gacha", "gacha_sync"}:
            cipher = CredentialCipher.from_env()
            return await _handle_gacha(
                matcher, qq_user_id, command, cipher, group=is_group(event), event=event, bot=bot,
            )
        if command.action == "gacha_import":
            with sensitive_input(qq_user_id):
                return await _handle_xhh_import(matcher, qq_user_id, command)
        if command.action == "gacha_history":
            return await _handle_gacha_history(matcher, qq_user_id, command, group=is_group(event))
    except TaskAlreadyRunning:
        return await matcher.finish("当前任务正在处理中，请稍候。")
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-account] official API request failed: operation={exc.operation} code={exc.code}")
        return await matcher.finish(str(exc))
    except InvestmentDataUnavailable as exc:
        logger.warning(f"[endfield-investment] AKEData unavailable: {exc}")
        return await matcher.finish("角色养成数据源暂时响应异常，请稍后再试。")
    except ChallengeAmbiguousError as exc:
        if command.action == "challenge":
            monument = command.challenge_kind == "monument"
            head = "影拓" if monument else "回响"
            candidate = exc.candidates[0] if exc.candidates else ("名称" if monument else "赛季")
            # Prefix the suggested command with the season/rotation that holds
            # the top candidate: echoing the bare name just repeats the query
            # that already failed.
            scope = " ".join(exc.path)
            label = _challenge_monument_difficulty_label if monument else _challenge_difficulty_label
            difficulty = label(command.challenge_difficulty) if command.challenge_difficulty else ""
            example = " ".join(item for item in (head, scope, candidate, difficulty) if item)
            return await matcher.finish(f"{exc}\n参考格式：/ef {example}")
        return await matcher.finish(str(exc))
    except ChallengeResolutionError as exc:
        return await matcher.finish(str(exc))
    except XhhAPIError as exc:
        return await matcher.finish(str(exc))
    except aiohttp.ClientConnectionError:
        logger.warning(f"[endfield-account] message connection interrupted: action={command.action}")
        return await matcher.finish("网络连接中断，请重新发送指令。")
    except _ExitException:
        raise
    except Exception as exc:
        logger.opt(exception=exc).error(
            f"[endfield-account] action failed: action={command.action} "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return await matcher.finish("账号服务暂时响应异常，请稍后再试。")


def _challenge_mention_targets(event: Event, bot=None) -> tuple[str, ...]:
    """Return distinct user mentions, excluding a leading mention of the bot."""
    bot_ids = {
        str(value)
        for value in (
            getattr(bot, "self_id", ""),
            getattr(bot, "id", ""),
        )
        if value
    }
    targets: list[str] = []
    for segment in event_chain(event):
        if not isinstance(segment, At):
            continue
        target = str(
            getattr(segment, "id", "")
            or getattr(segment, "target", "")
            or getattr(segment, "user_id", "")
            or ""
        ).strip()
        if not target or target in bot_ids or target.casefold() in {"all", "here"}:
            continue
        if target not in targets:
            targets.append(target)
    return tuple(targets)


async def _handle_challenge(
    matcher,
    event: Event,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    bot=None,
) -> None:
    """Render one personal challenge query without prompting in group chats."""
    mentioned_users = _challenge_mention_targets(event, bot)
    if mentioned_users and not is_group(event):
        return await matcher.finish("通过 @群友 查询他人挑战数据仅限在群聊中使用。")
    if len(mentioned_users) > 1:
        return await matcher.finish("单次查询仅支持 @ 一位群友。")

    target_user_id = mentioned_users[0] if mentioned_users else qq_user_id
    roles = account_store.list_roles(target_user_id)
    if not roles:
        if mentioned_users:
            return await matcher.finish("所选群友暂未绑定终末地账号。")
        return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    # Mention queries deliberately use the target user's primary account.  An
    # account selector remains meaningful only for the command sender's own
    # bindings, so it cannot expose a target user's secondary account by index.
    role = account_store.resolve_role(
        target_user_id,
        "" if mentioned_users else command.account_selector,
    )
    if role is None:
        candidates = _challenge_account_candidates(roles, command.account_selector)
        if len(candidates) == 1:
            role = candidates[0]
        elif candidates:
            group_chat = is_group(event)
            lines = ["匹配到多个候选账号，请明确选择序号："]
            for candidate in candidates[:5]:
                index = roles.index(candidate) + 1
                uid = candidate.masked_uid if group_chat else candidate.role_id
                lines.append(f"{index}. {candidate.nickname}（UID {uid}）")
            lines.append("参考格式：/ef 影拓 账号 1")
            return await matcher.finish("\n".join(lines))
        else:
            return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")

    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        return await matcher.finish("影拓丰碑与战争回响目前仅支持国服角色，亚服暂未接入。")

    group_chat = is_group(event)
    identity = ChallengeIdentity(
        nickname=role.nickname,
        server_name=server_label(role.server_name or role.server_id),
        uid=role.masked_uid if group_chat else role.role_id,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    variant = "b"
    with cold_start_command(matcher):
        await notice_default_ake_public()
        return await _render_challenge_cards(
            matcher,
            event,
            command,
            bot,
            role,
            token,
            identity,
            group_chat,
            variant,
        )


async def _render_challenge_cards(
    matcher,
    event,
    command,
    bot,
    role,
    token,
    identity,
    group_chat: bool,
    variant: str,
) -> None:
    async def load_data(kind: str) -> dict:
        key = (str(role.role_id), str(role.server_id), kind)
        if kind == "monument":
            return await _CHALLENGE_DATA_CACHE.get_or_create(
                key, lambda: official_client.indie_hard(token, role)
            )
        return await _CHALLENGE_DATA_CACHE.get_or_create(
            key, lambda: official_client.war_echoes(token, role)
        )

    async def load_locale() -> ChallengeLocale | None:
        try:
            # A cold AKEData load includes two large i18n tables and normally
            # takes several seconds.  Do not render and cache an English card
            # merely because the background warmup missed the old 750 ms window.
            locale = await get_challenge_locale(max_wait_seconds=12.0)
            if locale is None:
                logger.info("[endfield-challenge] i18n is warming up; using API copy for this request")
            return locale
        except Exception as exc:
            logger.warning(f"[endfield-challenge] AKEData i18n unavailable, using API copy: {exc}")
            return None

    async def cached_pages(
        kind: str,
        view: str,
        terms: str,
        difficulty: str,
        page: int,
        factory,
    ) -> tuple[bytes, ...]:
        key: ChallengeCacheKey = (
            str(role.role_id),
            str(role.server_id),
            kind,
            view,
            terms,
            difficulty,
            int(page),
            group_chat,
            variant,
            locale.version if locale is not None else "api-copy",
            CARD_RENDER_VERSION,
            _ASSET_GENERATION,
            hashlib.sha256(json.dumps(
                [raw, asdict(identity)], sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            ).encode()).hexdigest(),
        )
        async def render_complete():
            # 数据已就绪，这里只剩出图：整张卡共用一个素材预算，到点按已取到的图出图。
            with track_render_health() as health, asset_render_budget():
                pages = tuple(await factory())
                if not health.complete or locale is None:
                    raise _IncompletePages(pages)
                return pages
        try:
            return await _CHALLENGE_RENDER_CACHE.get_or_create(key, render_complete)
        except _IncompletePages as exc:
            return exc.pages

    if command.challenge_kind == "monument":
        raw, locale = await asyncio.gather(load_data("monument"), load_locale())
        payload = parse_monument(raw, locale)
        if command.challenge_view == "overview":
            pngs = await cached_pages(
                "monument", "overview", "", "", 0,
                lambda: _one_page(
                    draw_challenge_empty(identity, "影拓丰碑", variant=variant)
                    if not payload.has_records
                    else draw_monument_overview(identity, payload, variant=variant)
                ),
            )
            return await _finish_pngs(matcher, pngs)
        pages = payload.history_pages() if payload.has_records else ()
        if command.challenge_view == "history":
            if not pages:
                pngs = await cached_pages(
                    "monument", "history", "all", "", 0,
                    lambda: _one_page(draw_challenge_empty(identity, "影拓丰碑", query="暂无历史主题", variant=variant)),
                )
                return await _finish_pngs(matcher, pngs)
            if command.all_history:
                rendered = await cached_pages(
                    "monument", "history", "all", "", 0,
                    lambda: draw_monument_history_pages(identity, pages, variant=variant),
                )
                return await _finish_challenge_pages(matcher, event, bot, rendered, "/ef 影拓 历史 第N页")
            if command.page > len(pages):
                return await matcher.finish(f"影拓历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
            rendered = await cached_pages(
                "monument", "history", "", "", command.page,
                lambda: _one_page(draw_monument_history(identity, pages[command.page - 1], page=command.page, page_count=len(pages), variant=variant)),
            )
            return await _finish_pngs(matcher, rendered)
        group, dungeon = resolve_monument_detail(payload, command.challenge_terms, command.challenge_difficulty or "hard")
        rendered = await cached_pages(
            "monument", "detail", " ".join(command.challenge_terms), command.challenge_difficulty or "hard", 0,
            lambda: _one_page(draw_monument_detail(identity, group, dungeon, variant=variant)),
        )
        return await _finish_pngs(matcher, rendered)

    raw, locale = await asyncio.gather(load_data("war_echo"), load_locale())
    payload = parse_war_echoes(raw, locale)
    if command.challenge_view == "overview":
        pngs = await cached_pages(
            "war_echo", "overview", "", "", 0,
            lambda: _one_page(
                draw_challenge_empty(identity, "战争回响", variant=variant)
                if not payload.has_records else draw_war_overview(identity, payload, variant=variant)
            ),
        )
        return await _finish_pngs(matcher, pngs)
    pages = payload.history_pages() if payload.has_records else ()
    if command.challenge_view == "history":
        if not pages:
            rendered = await cached_pages(
                "war_echo", "history", "all", "", 0,
                lambda: _one_page(draw_challenge_empty(identity, "战争回响", query="暂无历史赛季", variant=variant)),
            )
            return await _finish_pngs(matcher, rendered)
        if command.all_history:
            rendered = await cached_pages(
                "war_echo", "history", "all", "", 0,
                lambda: draw_war_history_pages(
                    identity,
                    tuple(season for (season,) in pages),
                    achievements=payload.achievements,
                    variant=variant,
                ),
            )
            return await _finish_challenge_pages(matcher, event, bot, rendered, "/ef 回响 历史 第N页")
        if command.page > len(pages):
            return await matcher.finish(f"战争回响历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
        season = pages[command.page - 1][0]
        rendered = await cached_pages(
            "war_echo", "history", "", "", command.page,
            lambda: _one_page(draw_war_history(identity, season, page=command.page, page_count=len(pages), achievements=payload.achievements, variant=variant)),
        )
        return await _finish_pngs(matcher, rendered)
    season, week, war_group, dungeon = resolve_war_detail(payload, command.challenge_terms, command.challenge_difficulty or "cruel")
    rendered = await cached_pages(
        "war_echo", "detail", " ".join(command.challenge_terms), command.challenge_difficulty or "cruel", 0,
        lambda: _one_page(draw_war_detail(identity, season, week, war_group, dungeon, variant=variant)),
    )
    return await _finish_pngs(matcher, rendered)


def _challenge_account_candidates(roles: list[EndfieldRole], selector: str) -> list[EndfieldRole]:
    value = str(selector or "").strip().casefold()
    if not value or value.isdigit():
        return []
    matches: list[EndfieldRole] = []
    for role in roles:
        nickname = str(role.nickname or "").strip().casefold()
        suffix = str(role.role_id or "")[-4:].casefold()
        if value == nickname or (len(value) >= 4 and value == suffix) or value in nickname or nickname in value:
            matches.append(role)
    return matches


async def _finish_challenge_pages(matcher, event, bot, pngs: tuple[bytes, ...], page_hint: str) -> None:
    if len(pngs) <= 2:
        return await _finish_pngs(matcher, pngs)
    try:
        await _send_forward_pngs(bot, event, pngs)
    except _ExitException:
        raise
    except Exception as exc:
        logger.warning(f"[endfield-challenge] merged forward unavailable: {type(exc).__name__}")
        return await matcher.finish(f"历史记录共 {len(pngs)} 页，当前环境暂不支持合并转发，请通过 {page_hint} 分页浏览。")
    return await matcher.finish()


async def _send_forward_pngs(
    bot,
    event,
    pngs: tuple[bytes, ...],
    *,
    sender_name: str = _FORWARD_SENDER_NAME,
    log_tag: str = "endfield-challenge",
    onebot_timeout: float | None = None,
) -> str:
    """Send PNG pages as one merged forward; returns the path used (``satori`` / ``onebot``).

    Satori's standard ``<message forward>`` element is tried first: LLOneBot's
    Satori encoder turns it into a native QQ merged forward, so no OneBot
    action is needed.  Implementations that ignore ``forward`` would silently
    fan the pages out as separate messages, so a raised error (or a missing
    session) falls back to the OneBot ``send_*_forward_msg`` action.

    ``onebot_timeout`` opts a caller into the large-forward fallback: nodes
    reference the temp files already written for Satori (``file://``) instead
    of base64, under ``sender_name`` and the given HTTP timeout.  Without it
    the fallback is the original base64 action (challenge history).
    """
    uin = str(getattr(bot, "self_id", "") or getattr(bot, "id", "") or "") or None
    images: list = []
    try:
        images = [_png_image(png) for png in pngs]
        await send_forward(images, name=sender_name, uin=uin)
        return "satori"
    except _ExitException:
        raise
    except Exception as exc:
        logger.warning(
            f"[{log_tag}] satori forward element failed, "
            f"falling back to OneBot action: {type(exc).__name__}: {exc}"
        )
    if onebot_timeout is None:
        await send_forward_images(bot, event, pngs)
    else:
        await send_forward_images(
            bot, event, pngs, name=sender_name, timeout=onebot_timeout,
            file_uris=[str(getattr(image, "src", "") or "") for image in images],
        )
    return "onebot"


def _gacha_forward_above() -> int:
    """抽卡分析超过多少张图改为合并转发；0 = 关闭合并转发（回滚开关）。"""
    raw = os.getenv(GACHA_FORWARD_ABOVE_ENV, "").strip()
    if not raw:
        return GACHA_FORWARD_ABOVE_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(f"[endfield-gacha] invalid {GACHA_FORWARD_ABOVE_ENV}={raw!r}, using {GACHA_FORWARD_ABOVE_DEFAULT}")
        return GACHA_FORWARD_ABOVE_DEFAULT


async def _finish_gacha_pngs(matcher, event, bot, pngs: tuple[bytes, ...]) -> None:
    """抽卡分析投递：≤ 阈值与其他命令一样一条消息发出；超过阈值发 QQ 合并转发（每节点 1 张）。

    回退链：Satori ``<message forward>`` → OneBot ``send_*_forward_msg`` → 每条 ≤3 张分批发送。
    抽卡没有分页命令，所以最后一级是分批而不是文字提示（影拓历史仍按原规则：>2 页转发，失败回文字）。
    """
    threshold = _gacha_forward_above()
    if not threshold or len(pngs) <= threshold:
        return await _finish_pngs(matcher, pngs)
    started = perf_counter()
    sent = 0
    vias: list[str] = []
    try:
        for start in range(0, len(pngs), FORWARD_MAX_NODES):
            chunk = pngs[start:start + FORWARD_MAX_NODES]
            vias.append(await _send_forward_pngs(
                bot, event, chunk, sender_name=_GACHA_FORWARD_SENDER_NAME, log_tag="endfield-gacha",
                onebot_timeout=GACHA_FORWARD_TIMEOUT_SECONDS,
            ))
            sent += len(chunk)
    except _ExitException:
        raise
    except Exception as exc:
        logger.warning(
            f"[endfield-gacha] merged forward unavailable, fallback=batches sent={sent}/{len(pngs)}: "
            f"{type(exc).__name__}: {exc}"
        )
    else:
        logger.info(
            f"[endfield-gacha] deliver mode=forward via={'+'.join(vias)} pages={len(pngs)} forwards={len(vias)} "
            f"bytes={sum(map(len, pngs))} elapsed={perf_counter() - started:.2f}s"
        )
        return await matcher.finish()
    remaining = pngs[sent:]
    batches = [remaining[index:index + GACHA_FALLBACK_BATCH] for index in range(0, len(remaining), GACHA_FALLBACK_BATCH)]
    notice = (
        f"抽卡分析长图共 {len(pngs)} 页，当前环境无法合并转发，已分 {len(batches)} 条消息发送。" if not sent
        else f"抽卡分析长图共 {len(pngs)} 页，第 {sent + 1}–{len(pngs)} 页转发异常，已分 {len(batches)} 条消息补发。"
    )
    for index, batch in enumerate(batches):
        prefix = [Text(notice)] if index == 0 else []
        await matcher.send(ChainMsg([*prefix, *(_png_image(png) for png in batch)]))
    logger.info(
        f"[endfield-gacha] deliver mode=batches pages={len(pngs)} forwarded={sent} batches={len(batches)} "
        f"elapsed={perf_counter() - started:.2f}s"
    )
    return await matcher.finish()


async def _one_page(page) -> tuple[bytes, ...]:
    return (await page,)


BINDING_SELECT_ATTEMPTS = 3
_BINDING_STOPPED = {
    "cancel": "已取消绑定。",
    "timeout": "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。",
    "invalid": f"连续 {BINDING_SELECT_ATTEMPTS} 次未选中有效角色，绑定已取消；需要时请重新发送 /ef 绑定。",
}


class _BindingStopped(Exception):
    """The user cancelled, a prompt timed out, or role selection stayed invalid."""

    def __init__(self, stage: str, reason: str):
        super().__init__(stage, reason)
        self.stage, self.reason = stage, reason


def _binding_log(qq_user_id: str, step: str, **fields) -> None:
    # Steps and counts only: never a phone number, SMS code or token.
    details = "".join(f" {key}={value}" for key, value in fields.items())
    logger.info(f"[endfield-bind] user={qq_user_id} step={step}{details}")


async def _prompt_binding(message: str, *, timeout: int, stage: str) -> str:
    answer = await prompt(message, timeout=timeout)
    if answer is None:
        raise _BindingStopped(stage, "timeout")
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        raise _BindingStopped(stage, "cancel")
    return text


async def _handle_binding(matcher, qq_user_id: str, cipher: CredentialCipher) -> None:
    _binding_log(qq_user_id, "start")
    try:
        return await _run_binding(matcher, qq_user_id, cipher)
    except _BindingStopped as stopped:
        _binding_log(qq_user_id, stopped.reason, at=stopped.stage)
        return await matcher.finish(_BINDING_STOPPED[stopped.reason])
    except EndfieldAPIError as exc:
        _binding_log(qq_user_id, "failed", operation=exc.operation, code=exc.code)
        raise


async def _run_binding(matcher, qq_user_id: str, cipher: CredentialCipher) -> None:
    region = await _prompt_binding(
        "请选择服务器分区：\n1. 国服（森空岛，支持 Token 与短信验证，二维码暂未开放）\n"
        "2. 亚服（SKPORT，当前支持 Token）\n"
        "回复数字 1 或 2，如需放弃请回复“取消”。",
        timeout=90,
        stage="region",
    )
    normalized_region = region.casefold()
    if normalized_region in {"1", "国服", "cn", "china"}:
        provider = ACCOUNT_PROVIDER_CN
    elif normalized_region in {"2", "亚服", "亞洲", "亚洲", "asia", "skport"}:
        provider = ACCOUNT_PROVIDER_SKPORT
    else:
        _binding_log(qq_user_id, "abort", at="region", reason="unrecognized")
        return await matcher.finish("无法识别所选服务器，绑定流程已终止。")
    _binding_log(qq_user_id, "region", provider=provider)

    if provider == ACCOUNT_PROVIDER_SKPORT:
        await matcher.send(
            "请在浏览器登录 SKPORT（https://www.skport.com/）后访问：\n"
            "https://web-api.skport.com/cookie_store/account_token\n"
            "页面将返回类似以下示例内容（仅为格式示意，切勿直接发送范例 Token）：\n"
            '{"code":0,"data":{"content":"FlJTn48gU1OwP9R7lQUpDFZJ"},"msg":""}\n'
            "示例中需提取的关键内容为：\n"
            "FlJTn48gU1OwP9R7lQUpDFZJ\n"
            "请从你访问的页面中，仅提取 content 双引号包裹的字符串。\n"
            "切勿包含外层双引号或完整 JSON 数据。\n"
            "切勿发送上述示范 Token。\n"
            "请妥善保管凭证，切勿在群聊或公共渠道公开。"
        )
        raw_account_token = await _prompt_binding(
            "请发送 content 对应的 Token 字符串，如需放弃请回复“取消”。", timeout=150, stage="token"
        )
        account_token = encode_account_credential(raw_account_token, provider)
        _binding_log(qq_user_id, "token_received", provider=provider)
    else:
        account_token = await _bind_cn_account_token(matcher, qq_user_id)
        if account_token is None:
            return None

    roles = await official_client.discover_roles(account_token)
    if provider == ACCOUNT_PROVIDER_SKPORT:
        roles = [role for role in roles if is_asia_role(role)]
    _binding_log(qq_user_id, "roles", count=len(roles))
    if not roles:
        return await matcher.finish(
            "该 Gryphline 通行证下未检索到终末地亚服角色。" if provider == ACCOUNT_PROVIDER_SKPORT
            else "该鹰角网络通行证下未检索到终末地角色。"
        )
    selected = await _select_binding_roles(roles, qq_user_id)
    previous_roles = account_store.list_roles(qq_user_id)
    previous_keys = {(role.role_id, role.server_id) for role in previous_roles}
    bound_roles = account_store.bind_roles(qq_user_id, account_token, selected, cipher)
    added_count = sum(
        (role.role_id, role.server_id) not in previous_keys for role in selected
    )
    updated_count = len(selected) - added_count
    region_label = "亚服" if provider == ACCOUNT_PROVIDER_SKPORT else "国服"
    summary = f"{region_label}角色绑定成功：新增 {added_count} 个账号"
    if updated_count:
        summary += f"，更新 {updated_count} 个账号"
    summary += f"；目前累计绑定 {len(bound_roles)} 个账号。"
    selected_keys = {(role.role_id, role.server_id) for role in selected}
    try:
        refresh = await ownership_stats_service.refresh_roles(
            [role for role in bound_roles if (role.role_id, role.server_id) in selected_keys],
            cipher,
            force=True,
            trigger="binding",
        )
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] binding refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
    _binding_log(qq_user_id, "done", provider=provider, added=added_count, updated=updated_count, total=len(bound_roles))
    return await matcher.finish(
        summary + "\n" + "\n".join(
            f"- {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}" for role in selected
        )
    )


async def _bind_cn_account_token(matcher, qq_user_id: str) -> str | None:
    method = await _prompt_binding(
        "请选择绑定授权途径：\n1. Token 授权绑定\n2. 手机短信验证码绑定\n"
        "二维码扫码绑定暂未开放。\n"
        "支持追加绑定多个鹰角账号，已绑定的角色数据不会被覆盖。\n回复数字 1 或 2，如需放弃请回复“取消”。",
        timeout=90,
        stage="method",
    )
    normalized = method.casefold()
    if normalized in {"1", "token", "t"}:
        _binding_log(qq_user_id, "method", method="token")
        await matcher.send(
            "请在浏览器中登录森空岛并访问：\nhttps://web-api.skland.com/account/info/hg\n"
            "复制页面响应中 data.content 对应的完整字符串并发送。请妥善保管凭据，切勿向他人公开。"
        )
        account_token = await _prompt_binding("请发送 data.content 内容，如需放弃请回复“取消”。", timeout=150, stage="token")
        _binding_log(qq_user_id, "token_received", provider=ACCOUNT_PROVIDER_CN)
    elif normalized in {"2", "短信", "手机", "sms"}:
        _binding_log(qq_user_id, "method", method="sms")
        phone = await _prompt_binding("请输入鹰角网络账号对应的 11 位手机号，如需放弃请回复“取消”。", timeout=90, stage="phone")
        if not re.fullmatch(r"1\d{10}", phone):
            _binding_log(qq_user_id, "abort", at="phone", reason="format")
            await matcher.finish("手机号格式不符合标准，绑定流程已终止。")
            return None
        _binding_log(qq_user_id, "send_code")
        await official_client.send_phone_code(phone)
        _binding_log(qq_user_id, "code_sent")
        code = await _prompt_binding("短信验证码已下发，请输入收到的验证码，如需放弃请回复“取消”。", timeout=120, stage="code")
        if not re.fullmatch(r"\d{4,8}", code):
            _binding_log(qq_user_id, "abort", at="code", reason="format")
            await matcher.finish("验证码格式输入有误，绑定流程已终止。")
            return None
        _binding_log(qq_user_id, "verify")
        account_token = await official_client.token_by_phone_code(phone, code)
        _binding_log(qq_user_id, "verified")
    # 暂时禁用二维码绑定，保留以下代码以便后续恢复：
    # elif normalized in {"3", "二维码", "扫码", "qr", "qrcode"}:
    #     account_token = await _bind_cn_qr_account(matcher)
    #     if account_token is None:
    #         return None
    else:
        qr = normalized in {"3", "二维码", "扫码", "qr", "qrcode"}
        _binding_log(qq_user_id, "abort", at="method", reason="qr" if qr else "unrecognized")
        await matcher.finish("二维码扫码暂未开放，请回复 1 或 2 选择其他方式。" if qr else "无法识别所选绑定方式，流程已终止。")
        return None
    return encode_account_credential(account_token, ACCOUNT_PROVIDER_CN)


# 暂时注释二维码绑定实现；恢复时一并取消本段注释。
# async def _bind_cn_qr_account(matcher) -> str | None:
#     ticket = await official_client.create_qr_login()
#     qr_png = _render_qr_png(ticket.scan_url)
#     await matcher.send(
#         "请使用森空岛或《明日方舟：终末地》App 扫描下方二维码并确认登录。"
#         "二维码有效期较短，请勿转发给他人。"
#     )
#     await matcher.send(ChainMsg([make_image(raw=qr_png)]))
#
#     loop = asyncio.get_running_loop()
#     deadline = loop.time() + 150
#     scan_notice_sent = False
#     while loop.time() < deadline:
#         status = await official_client.check_qr_login(ticket.scan_id)
#         if status.state == "confirmed":
#             return await official_client.token_by_scan_code(status.scan_code)
#         if status.state == "scanned" and not scan_notice_sent:
#             await matcher.send("扫码成功，请在手机上确认登录。")
#             scan_notice_sent = True
#         if status.state == "expired":
#             await matcher.finish("二维码已过期，请重新执行绑定命令。")
#             return None
#         await asyncio.sleep(min(2, max(0, deadline - loop.time())))
#
#     await matcher.finish("等待扫码确认超时，请重新执行绑定命令。")
#     return None
#
#
# def _render_qr_png(content: str) -> bytes:
#     import cv2
#
#     matrix = cv2.QRCodeEncoder_create().encode(content)
#     matrix = cv2.copyMakeBorder(matrix, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
#     matrix = cv2.resize(matrix, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
#     encoded, png = cv2.imencode(".png", matrix)
#     if not encoded:
#         raise RuntimeError("二维码图片生成失败")
#     return png.tobytes()


def parse_binding_selection(answer: str, roles: list[RoleCandidate]) -> list[RoleCandidate] | None:
    """Listed numbers and/or UIDs, in any mix; None unless every item names a listed role."""
    text = unicodedata.normalize("NFKC", answer).strip()
    if text.casefold() in {"全部", "all"}:
        return list(roles)
    items = [item.strip(".。号#") for item in re.split(r"[\s,，、;；/|]+|(?i:uid)[:：]?", text)]
    items = [item for item in items if item]
    if not items:
        return None
    picked: set[int] = set()
    for item in items:
        if not item.isdecimal():
            return None
        if len(item) <= 3 and 1 <= int(item) <= len(roles):
            picked.add(int(item) - 1)
            continue
        matches = {index for index, role in enumerate(roles) if role.role_id == item}
        if not matches:
            return None
        picked |= matches
    return [role for index, role in enumerate(roles) if index in picked]


async def _select_binding_roles(roles: list[RoleCandidate], qq_user_id: str) -> list[RoleCandidate]:
    if len(roles) == 1:
        _binding_log(qq_user_id, "select", selected=1, total=1)
        return roles
    listing = "\n".join(
        f"{index}. {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}"
        for index, role in enumerate(roles, 1)
    )
    message = (
        "检测到多个终末地角色，请回复序号或 UID，可一次回复多个（用空格、逗号或顿号分隔），"
        "也可回复“全部”；回复“取消”退出：\n" + listing
    )
    for attempt in range(1, BINDING_SELECT_ATTEMPTS + 1):
        selected = parse_binding_selection(await _prompt_binding(message, timeout=120, stage="select"), roles)
        if selected:
            _binding_log(qq_user_id, "select", selected=len(selected), total=len(roles), attempt=attempt)
            return selected
        _binding_log(qq_user_id, "select_invalid", attempt=attempt, limit=BINDING_SELECT_ATTEMPTS)
        message = "编号无效，请回复列表前面的序号（或列表中的 UID）：\n" + listing
    raise _BindingStopped("select", "invalid")


async def _handle_accounts(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
        return await _render_account_detail(matcher, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_detail(matcher, roles[0], cipher, group=group)

    answer = await prompt_silently(
        _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return await matcher.finish()
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return await matcher.finish("已取消账号查询。")
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
    return await _render_account_detail(matcher, role, cipher, group=group)


async def _card_detail_with_snapshot(token: str, role: EndfieldRole) -> dict:
    async with ROLE_TASKS.claim(role):
        detail = await official_client.card_detail(token, role)
    try:
        await ownership_stats_service.persist_detail(role, detail)
    except Exception:
        # Opportunistic writes must not affect the account-detail response;
        # refresh batches report aggregate failures without logging identities.
        pass
    return detail


async def _render_account_detail(
    matcher, role: EndfieldRole, cipher: CredentialCipher, *, group: bool
) -> None:
    with cold_start_command(matcher):
        token = account_store.decrypt_token(role, cipher)
        await notice_default_ake_public()
        async def load_currency_balances() -> dict[int, int]:
            try:
                return await official_client.currency_balances(token, role)
            except EndfieldAPIError as exc:
                logger.warning(f"[endfield] account currency unavailable operation={exc.operation}")
                return {}

        async def load_name_map():
            try:
                return await fetch_account_detail_name_map()
            except Exception as exc:
                logger.warning(f"[endfield] account AKE name map unavailable: {exc}")
                return None

        detail, currency_balances, name_map = await asyncio.gather(
            _card_detail_with_snapshot(token, role),
            load_currency_balances(),
            load_name_map(),
        )
        view = build_account_detail_view(
            detail,
            uid=role.masked_uid if group else role.role_id,
            nickname=role.nickname,
            server_name=role.server_name or role.server_id,
            currency_balances=currency_balances,
            name_map=name_map,
        )
        if re.search(r"[A-Za-z]", view.main_mission) and not re.search(r"[\u3400-\u9fff]", view.main_mission):
            # Some official profiles omit the mission ID and ignore the CN locale.
            # Reuse the existing versioned CN/EN reverse map; never guess a title
            # from similar English wording or write it into the account payload.
            try:
                locale = await fetch_challenge_locale()
                view = replace(view, main_mission=locale.text(view.main_mission))
            except Exception as exc:
                logger.warning("[endfield] account mission localization unavailable ({})", type(exc).__name__)
        pages = await _render_account_pages("detail", role, group, view, lambda: draw_account_detail_cards(view))
        return await _finish_pngs(matcher, pages)


async def _handle_account_investment(
    matcher,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
        return await _render_account_investment(matcher, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_investment(matcher, roles[0], cipher, group=group)

    answer = await prompt_silently(
        _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return await matcher.finish()
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return await matcher.finish("已取消账号养成统计查询。")
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
    return await _render_account_investment(matcher, role, cipher, group=group)


async def _render_account_investment(
    matcher,
    role: EndfieldRole,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        return await matcher.finish("养成统计功能目前仅支持国服角色，亚服暂未接入。")

    with cold_start_command(matcher):
        await notice_default_ake_public()
        async def load_name_map():
            try:
                return await fetch_account_detail_name_map()
            except Exception as exc:
                logger.warning(f"[endfield] investment AKE name map unavailable: {exc}")
                return None

        detail, catalog, name_map = await asyncio.gather(
            _card_detail_with_snapshot(token, role),
            fetch_account_investment_catalog(),
            load_name_map(),
        )
        view = build_account_investment_view(
            detail,
            uid=role.masked_uid if group else role.role_id,
            nickname=role.nickname,
            server_name=role.server_name or role.server_id,
            catalog=catalog,
            name_map=name_map,
        )
        pages = await _render_account_pages("investment", role, group, view, lambda: draw_account_investment_cards(view))
        return await _finish_pngs(matcher, pages)


async def _handle_account_currency(
    matcher,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
        return await _render_account_currency(matcher, role, command, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_currency(matcher, roles[0], command, cipher, group=group)

    answer = await prompt_silently(
        _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return await matcher.finish()
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return await matcher.finish("已取消资源流水查询。")
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
    return await _render_account_currency(matcher, role, command, cipher, group=group)


async def _render_account_currency(
    matcher,
    role: EndfieldRole,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        return await matcher.finish("资源流水查询目前仅支持国服角色，亚服暂未接入。")

    try:
        start, end = resolve_query_dates(
            command.start_date,
            command.end_date,
            days=command.days or None,
        )
        display_start_ts, display_end_ts = (
            (None, None)
            if command.all_history
            else currency_date_bounds(start, end)
        )
    except ValueError as exc:
        return await matcher.finish(str(exc))

    currency_types = command.currency_types or CURRENCY_TYPES
    # Every query refreshes the complete official history for all resources.
    # The local table is an incremental, seqId-keyed backup; display filters
    # are applied only after the refresh has been persisted.
    fetched_logs = await official_client.currency_logs(
        token,
        role,
        currency_types=CURRENCY_TYPES,
        start_ts=None,
        end_ts=None,
        change_type=0,
    )
    backed_up = sum(
        account_store.upsert_currency_logs(role, items)
        for items in fetched_logs.values()
    )
    logger.info(
        f"[endfield] currency log backup role={role.masked_uid} fetched={backed_up}"
    )
    if command.all_history:
        backed_up_logs = account_store.list_currency_logs(
            role,
            currency_types,
            start_ts=None,
            end_ts=None,
            change_type=0,
        )
        period_label = format_all_history_period_label(
            (item for items in backed_up_logs.values() for item in items),
            end=end,
            quota_start=earliest_currency_log_date(backed_up_logs.get(3, ())),
        )
    else:
        period_label = f"{start.isoformat()} ~ {end.isoformat()}"
    logs = account_store.list_currency_logs(
        role,
        currency_types,
        start_ts=display_start_ts,
        end_ts=display_end_ts,
        change_type=command.change_type,
    )
    summaries = tuple(
        aggregate_currency_logs(logs.get(currency_type, ()), currency_type)
        for currency_type in currency_types
    )
    role_uid = role.masked_uid if group else role.role_id
    role_label = f"{role.nickname} / CN / UID {role_uid}"
    try:
        cards = await draw_currency_log_cards(
            summaries,
            role_label=role_label,
            start=start,
            end=end,
            change_type=command.change_type,
            period_label=period_label,
        )
        return await _finish_pngs(matcher, cards)
    except _ExitException:
        raise
    except Exception:
        logger.exception("[endfield] currency log card render failed")
        report = format_currency_log_report(
            summaries,
            role_label=role_label,
            start=start,
            end=end,
            change_type=command.change_type,
            period_label=period_label,
        )
        chunks = split_report(report)
        for chunk in chunks[:-1]:
            await matcher.send(chunk)
        return await matcher.finish(chunks[-1])


async def _handle_account_base(
    matcher,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        return await matcher.finish("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到指定账号，发送 /ef 账号 可查看有效编号。")
        return await _render_account_base(matcher, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_base(matcher, roles[0], cipher, group=group)

    answer = await prompt_silently(
        _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return await matcher.finish()
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return await matcher.finish("已取消账号查询。")
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        return await matcher.finish(f"序号输入有误，请输入 1–{len(roles)}。")
    return await _render_account_base(matcher, role, cipher, group=group)


async def _render_account_base(
    matcher,
    role: EndfieldRole,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    with cold_start_command(matcher):
        token = account_store.decrypt_token(role, cipher)
        await notice_default_ake_public()

        async def load_name_map():
            try:
                return await fetch_account_detail_name_map()
            except Exception as exc:
                logger.warning(f"[endfield] account AKE name map unavailable: {exc}")
                return None

        detail, name_map = await asyncio.gather(
            _card_detail_with_snapshot(token, role),
            load_name_map(),
        )
        view = build_account_base_view(
            detail,
            uid=role.masked_uid if group else role.role_id,
            role_id=role.role_id,
            server_id=role.server_id,
            nickname=role.nickname,
            server_name=role.server_name or role.server_id,
            store=account_store,
            name_map=name_map,
        )
        async def render():
            return (await draw_account_base_card(view),)

        return await _finish_pngs(matcher, await _render_account_pages("base", role, group, view, render))


class _IncompletePages(Exception):
    def __init__(self, pages: tuple[bytes, ...]):
        self.pages = pages


async def _render_account_pages(
    kind: str, role, group: bool, view, render
) -> tuple[bytes, ...]:
    """Cache only rendering after current data and authorization were resolved."""
    digest = hashlib.sha256(
        json.dumps(
            asdict(view),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    key = (
        CARD_RENDER_VERSION,
        str(_ASSET_GENERATION),
        kind,
        str(role.role_id),
        str(role.server_id),
        "group" if group else "private",
        digest,
    )

    async def create_pages():
        # 整张卡共用一个素材预算；到点未取到的素材留空，页面照常出图（不进缓存）。
        with track_render_health() as health, asset_render_budget():
            pages = tuple(await render())
            if not health.complete:
                raise _IncompletePages(pages)
            return pages

    started = perf_counter()
    try:
        pages, hit = await _ACCOUNT_PAGE_CACHE.get_or_create_with_status(
            key, create_pages
        )
    except _IncompletePages as exc:
        pages, hit = exc.pages, False
    logger.info(
        f"[endfield] account-render kind={kind} reuse={hit} "
        f"seconds={perf_counter() - started:.3f} pages={len(pages)}"
    )
    return pages


async def _handle_attendance(
    matcher,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
) -> None:
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    view = await sign_attendance_roles(account_store, official_client, cipher, roles)
    return await _finish_attendance_view(matcher, view)


async def _finish_attendance_view(matcher, view: AttendanceCardView) -> None:
    """Send the card, or the full text result when the renderer is unavailable."""
    png = await _attendance_png(view)
    if png is None:
        return await matcher.finish(format_attendance_report(view))
    return await _finish_png(matcher, png)


async def _attendance_png(view: AttendanceCardView) -> bytes | None:
    """Render the card, or return ``None`` so the caller sends full text."""
    try:
        return await draw_attendance_card(view)
    except _ExitException:
        raise
    except Exception as exc:  # noqa: BLE001 - the text result must still be delivered
        logger.warning(
            f"[endfield-account] attendance card render failed error_type={type(exc).__name__}"
        )
        return None


# ------------------------------------------------- unified /签到 registration


def _signin_roles(user_id: str) -> list[EndfieldRole]:
    """Every role this user bound, without touching credentials."""
    return account_store.list_roles(user_id)


async def _signin_attendance(user_id: str, *, group: bool) -> AttendanceResult:
    """Run the whole Endfield sign-in for the unified ``/签到`` entry point.

    ``group`` is part of the capability contract; this game's attendance card
    always carries masked UIDs, so there is nothing to hide or reveal here.
    """
    roles = account_store.list_roles(user_id)
    if not roles:
        return AttendanceResult(ok=False, text="尚未绑定终末地账号，请私聊使用 /zmd 绑定。")
    try:
        cipher = CredentialCipher.from_env()
    except CredentialKeyError as exc:
        return AttendanceResult(ok=False, text=str(exc))
    view = await sign_attendance_roles(account_store, official_client, cipher, roles)
    return AttendanceResult(
        png=await _attendance_png(view),
        text=format_attendance_report(view),
    )


register_attendance_capability(
    AttendanceCapability(
        game="endfield",
        owner=__name__,
        module=sys.modules[__name__],
        roles=_signin_roles,
        sign=_signin_attendance,
    )
)


async def _handle_daily(
    matcher,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    """日常仪表盘：逐账号读取森空岛 card/detail，展示理智/活跃度/每周事务/通行证。"""
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    accounts: list[DailyAccountView] = []
    for role in roles:
        try:
            async with ROLE_TASKS.claim(role):
                token = account_store.decrypt_token(role, cipher)
                # card_detail 返回已解包的 data.detail；endfield_card_detail 是完整响应，
                # 仅供奖章/档案等自行解包的视图使用。
                detail = await official_client.card_detail(token, role)
            accounts.append(build_daily_account_view(
                detail,
                nickname=role.nickname, uid=role.masked_uid,
                server_name=server_label(role.server_name or role.server_id),
            ))
        except TaskAlreadyRunning:
            accounts.append(DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message="当前任务正在处理中"))
        except EndfieldAPIError as exc:
            accounts.append(DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message=str(exc)))
        except CredentialKeyError as exc:
            accounts.append(DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message=str(exc)))
        except Exception as exc:
            logger.error(
                f"[endfield-account] daily dashboard failed: stored_role={role.id} error_type={type(exc).__name__}"
            )
            accounts.append(DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message="数据获取失败，请稍后再试"))
    png = await draw_daily_dashboard_card(
        DailyDashboardView(accounts, format_timestamp(int(__import__("time").time())))
    )
    return await _finish_png(matcher, png)


async def _handle_gacha(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool,
    event=None, bot=None,
) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    gacha_service = EndfieldGachaService(account_store, official_client, cipher)
    states = account_store.list_sync_states(role)
    effective_full = command.full or not states
    existing_records = account_store.list_gacha_records(role, limit=100000)
    existing_pool_rules = await gacha_asset_cache.prepare_pool_rules(existing_records)
    result = await gacha_service.sync(
        role, full=effective_full, pool_rules=existing_pool_rules,
    )
    if command.action == "gacha_sync":
        failed = f"，{len(result.failed)} 个卡池拉取失败" if result.failed else ""
        mode = "近 90 天全量窗口" if effective_full else "增量"
        suffix = "；本地已同步记录将持续保留" if effective_full else ""
        return await matcher.finish(f"{role.nickname} 抽卡记录{mode}同步完毕：新增收录 {result.inserted} 条{failed}{suffix}。")
    records = account_store.list_gacha_records(role, limit=100000)
    xhh_import = account_store.get_xhh_gacha_import(role)
    xhh_names = [item.item_name for item in xhh_import.six_stars] if xhh_import else []
    metadata, pool_rules, xhh_metadata = await asyncio.gather(
        gacha_asset_cache.prepare(records),
        gacha_asset_cache.prepare_pool_rules(records),
        gacha_asset_cache.prepare_names(xhh_names),
    )
    keepsake_metadata, pool_banners = await asyncio.gather(
        gacha_asset_cache.prepare_keepsakes(pool_rules),
        gacha_asset_cache.prepare_pool_banners(pool_rules),
    )
    analysis = gacha_service.analysis(
        role, metadata, pool_rules, xhh_metadata, keepsake_metadata, pool_banners,
    )
    pngs = await draw_gacha_analysis_cards(analysis, uid=role.masked_uid)
    return await _finish_gacha_pngs(matcher, event, bot, pngs)


async def _handle_gacha_history(matcher, qq_user_id: str, command: ParsedEndfieldCommand, *, group: bool) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    total = account_store.count_gacha_records(role, command.pool_filter)
    total_pages = max(1, (total + 19) // 20)
    if command.page > total_pages and total:
        return await matcher.finish(f"页码超出有效范围，当前记录共 {total_pages} 页。")
    records = account_store.list_gacha_records(
        role, page=command.page, page_size=20, pool_filter=command.pool_filter
    )
    metadata = await gacha_asset_cache.prepare(records, download_all=True)
    records = apply_gacha_metadata(records, metadata)
    view = GachaHistoryView(
        nickname=role.nickname, uid=role.masked_uid,
        server_name=server_label(role.server_name or role.server_id), page=command.page, total_pages=total_pages, total=total,
        pool_filter=command.pool_filter,
        items=[
            GachaHistoryItemView(
                time=format_timestamp(item.gacha_ts), pool_name=item.pool_name,
                item_name=item.item_name, rarity=item.rarity, item_type=item.item_type,
                detail=item.weapon_type,
                icon_path=metadata.get(item.item_id).icon_path if item.item_id in metadata else "",
            )
            for item in records
        ],
    )
    return await _finish_png(matcher, await draw_gacha_history_card(view))


async def _handle_xhh_import(matcher, qq_user_id: str, command: ParsedEndfieldCommand) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
    phone = await _prompt_text("请输入小黑盒账号绑定的 11 位手机号，如需放弃请回复“取消”。", timeout=90)
    if phone is None:
        return await matcher.finish("导入流程已取消或等待超时。")
    if not re.fullmatch(r"1\d{10}", phone):
        return await matcher.finish("手机号格式不符合规范，导入已取消。")

    session: XhhLoginSession | None = None
    try:
        async with ROLE_TASKS.claim(role):
            session = await XhhLoginSession.start(phone)
            code = await _prompt_text(
                "小黑盒短信验证码已发送，请输入验证码，如需放弃请回复“取消”。", timeout=120
            )
            if code is None:
                return await matcher.finish("导入流程已取消或等待超时。")
            if not re.fullmatch(r"\d{4,8}", code):
                return await matcher.finish("验证码格式输入有误，导入已取消。")
            imported = await session.login_and_fetch(code)
            if imported.source_uid != role.role_id:
                return await matcher.finish(
                    f"小黑盒绑定的终末地 UID 与当前所选账号不一致，请切换对应账号后再试。当前账号 UID：{role.masked_uid}。"
                )
            candidate_names = [item.item_name for item in imported.six_stars]
            xhh_metadata = await gacha_asset_cache.prepare_names(candidate_names)
            unresolved_names = {
                item.item_name
                for item in imported.six_stars
                if "".join(item.item_name.split()).casefold() not in xhh_metadata
            }
            if unresolved_names:
                return await matcher.finish(
                    "FZ Wiki 星级数据暂未覆盖本次小黑盒记录，为避免星级误判已终止导入，请稍后再试。"
                )
            imported = filter_xhh_import_six_stars(imported, xhh_metadata)
            account_store.replace_xhh_gacha_import(role, imported)
    finally:
        if session is not None:
            await session.close()

    return await matcher.finish(
        f"{role.nickname} 小黑盒历史抽卡统计导入成功：涵盖 {len(imported.pools)} 个卡池，"
        f"累计 {imported.total_count} 抽，共包含 {len(imported.six_stars)} 条六星记录。\n"
        "发送 /ef 抽卡 即可查阅合并统计后的分析卡片；逐抽明细页仍以官方接口记录为准。"
    )


def _format_accounts(roles: list[EndfieldRole], *, reveal_uid: bool, detail_hint: bool = False) -> str:
    if not roles:
        return "尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。"
    lines = ["已绑定的终末地账号列表："]
    for index, role in enumerate(roles, 1):
        marker = " [主账号]" if role.is_primary else ""
        uid = role.role_id if reveal_uid else role.masked_uid
        lines.append(f"{index}. {role.nickname}{marker} · {server_label(role.server_name or role.server_id)} · UID {uid}")
    if detail_hint:
        lines.append("引用本条消息并回复对应编号即可查看该账号详情，如需放弃请回复“取消”。")
    lines.append("发送 /ef 添加账号 可追加新账号，发送 /ef 主账号 <编号> 或 /ef 解绑 <编号> 可进行管理。")
    return "\n".join(lines)


async def _prompt_text(message: str, *, timeout: int) -> str | None:
    answer = await prompt(message, timeout=timeout)
    if answer is None:
        return None
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return None
    return text


async def _handle_loadout(matcher, command: ParsedEndfieldCommand) -> None:
    try:
        if command.query:
            spec, error = parse_loadout_spec(command.query, command.enhance)
        else:
            spec, error = await _prompt_loadout_spec(command.enhance)
        if error or spec is None:
            return await matcher.finish(f"配装参数有误：{error or '已取消操作'}。")

        with cold_start_command(matcher):
            await notice_default_ake_public()
            resolved: list[tuple[EndfieldCandidate, tuple[tuple[int, int], ...]]] = []
            for index, item in enumerate(spec.items):
                candidate_kind = "operator" if index == 0 else "gear"
                candidate = await _resolve_loadout_candidate(candidate_kind, item.name)
                if candidate is None:
                    label = "干员" if index == 0 else "武器或装备"
                    return await matcher.finish(f"未匹配到目标{label}：{item.name}。")
                if item.forge_levels and candidate.kind != "equipment":
                    return await matcher.finish(f"仅装备支持设定词条锻造等级：{item.name}。")
                resolved.append((candidate, item.forge_levels))

            operators = [item for item, _ in resolved if item.kind == "operator"]
            weapons = [item for item, _ in resolved if item.kind == "weapon"]
            if len(operators) != 1:
                return await matcher.finish("配装指令必须且仅可指定一位干员。")
            if len(weapons) > 1:
                return await matcher.finish("配装指令最多仅可携带一把武器。")
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
            return await _finish_png(matcher, png)
    except _ExitException:
        raise
    except (WarfarinAPIError, ValueError) as exc:
        logger.warning(f"[endfield] loadout rejected: {exc}")
        return await matcher.finish(f"配装计算异常：{exc}")
    except Exception as exc:
        logger.exception(f"[endfield] loadout failed: {exc}")
        return await matcher.finish("配装图生成失败，请稍后再试。")


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


async def _prompt_loadout_spec(default_enhance: int) -> tuple[ParsedLoadoutSpec | None, str]:
    answer = await prompt(
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


async def _resolve_loadout_candidate(kind: str, query: str) -> EndfieldCandidate | None:
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
    answer = await prompt("\n".join(lines), timeout=60)
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


async def _resolve_stage_candidates_akedata(query: str) -> list[EndfieldCandidate]:
    return await _resolve_stage_candidates(query, "akedata")


async def _resolve_stage_candidates(query: str, source: str = "") -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    if query == "__all__":
        catalog = await stage_service.get_catalog_view(source)
        return [
            EndfieldCandidate(
                kind="stage_catalog",
                key="",
                display_name="关卡资料目录",
                score=100,
                source=source,
                reason="catalog",
                mode="catalog",
                revision=catalog.revision,
            )
        ]
    candidates: list[EndfieldCandidate] = []
    for match in await stage_service.discover_matches(query, source):
        score = score_candidate(match.query_text, match.display_name, match.title)
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        candidates.append(
            EndfieldCandidate(
                kind="stage",
                key=match.key,
                display_name=match.display_name,
                score=score,
                source=match.source,
                reason="stage-directory",
                variant=match.selector,
                mode=match.mode,
                revision=match.revision,
            )
        )
    return candidates


async def _resolve_candidates_akedata(
    kind: str, query: str, rarity: str = ""
) -> list[EndfieldCandidate]:
    """Resolve stable IDs, aliases and all existing catalog/filter commands."""
    from .providers.repository import query_snapshot

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
            from .providers.repository import localize

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


async def _render_stage(
    key: str,
    source: str = "",
    *,
    mode: str = "detail",
    selector: str = "",
) -> tuple[bytes | None, bool]:
    """Returns the card and whether it is missing data purely because a fetch failed."""
    if source and source != "akedata":
        return None, False
    started = perf_counter()
    view = await stage_service.get_stage_view(
        key,
        mode=mode,
        selector=selector,
        source=source or "akedata",
    )
    data_seconds = perf_counter() - started
    output = await draw_stage_card(view)
    logger.info(
        f"[endfield] render kind=stage mode={mode} data={data_seconds:.3f}s "
        f"draw={perf_counter() - started - data_seconds:.3f}s "
        f"unreachable={len(view.unreachable_enemies)}"
    )
    return output, bool(view.unreachable_enemies)


async def _render_stage_catalog(key: str, source: str = "") -> tuple[bytes, ...] | None:
    del key
    if source and source != "akedata":
        return None
    return await draw_stage_catalog_cards(
        await stage_service.get_catalog_view(source or "akedata")
    )


async def _finish_png(matcher, png: bytes) -> None:
    return await _finish_pngs(matcher, (png,))


async def _render_current_version_calendar() -> bytes:
    generation = _ASSET_GENERATION
    try:
        calendar = await calendar_source.current_ake_primary()
        return await _CALENDAR_CACHE.get_or_create(
            f"{generation}:akedata:{calendar.version}:{calendar.revision}",
            lambda: draw_version_calendar(calendar),
        )
    except Exception as exc:
        logger.warning("[endfield] AKE calendar coverage unavailable; official fallback ({})", type(exc).__name__)
    try:
        official = await official_calendar_source.current()
        return await _CALENDAR_CACHE.get_or_create(
            f"{generation}:official:{official.revision}",
            lambda: draw_official_version_calendar(official),
        )
    except Exception as exc:
        logger.warning(
            f"[endfield] official calendar unavailable, use AkeData fallback: "
            f"{type(exc).__name__}: {exc}"
        )
    calendar = await calendar_source.current()
    return await _CALENDAR_CACHE.get_or_create(
        f"{generation}:generated:{calendar.version}:{calendar.revision}",
        lambda: draw_version_calendar(calendar),
    )


async def _finish_endfield_help(matcher) -> None:
    # 按页面比例随机挑插画并运行时渲染；渲染不可用时回退到仓库里的静态帮助图。
    image = await cached_help_image("endfield") or ENDFIELD_HELP_IMAGE_PATH
    if image and Path(image).is_file():
        return await matcher.finish(ChainMsg([make_image(path=image)]))
    return await matcher.finish(format_help())


async def _finish_pngs(matcher, pngs: tuple[bytes, ...]) -> None:
    await matcher.finish(ChainMsg([_png_image(png) for png in pngs]))


def _png_image(png: bytes):
    """Persist one PNG to a temp file and wrap it as an image element.

    LLOneBot resolves ``file://`` locally, so a temp file avoids inflating the
    payload with a base64 data URI for every page of a merged forward.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        return make_image(path=file.name)


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


def _normalize_cache_scope(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"all", "全部"}:
        return "all"
    if normalized in {"operator", "op", "干员"}:
        return "operator"
    if normalized in {"weapon", "wp", "武器"}:
        return "weapon"
    if normalized in {"equipment", "equip", "eq", "装备"}:
        return "equipment"
    if normalized in {"stage", "stages", "关卡", "副本"}:
        return "stage"
    if normalized in {"icon", "icons", "图标", "素材"}:
        return "icon"
    return None


async def _clear_endfield_caches(scope: str) -> int:
    global _ASSET_GENERATION
    removed = 0
    if scope in {"all", "icon", "operator", "weapon", "equipment", "stage"}:
        _ASSET_GENERATION += 1
        removed += await _ACCOUNT_PAGE_CACHE.clear()
    if scope == "all":
        removed += clear_render_asset_caches()
        removed += gacha_asset_cache.clear_caches()
        removed += await _CARD_CACHE.clear()
        removed += await _LOADOUT_CACHE.clear()
        removed += await _CALENDAR_CACHE.clear()
        removed += await _CHALLENGE_DATA_CACHE.clear()
        removed += await _CHALLENGE_RENDER_CACHE.clear()
        removed += await service.clear_query_caches()
        removed += await clear_http_cache("endfield-")
        removed += await clear_http_cache("akedata")
        clear_i18n_process_warm()
        removed += stage_service.clear_caches()
        removed += calendar_source.clear_caches()
        removed += clear_account_detail_name_map()
        removed += clear_account_investment_catalog()
        removed += clear_challenge_locale()
    elif scope == "icon":
        removed += clear_render_asset_caches()
        removed += gacha_asset_cache.clear_caches()
        removed += await _CARD_CACHE.clear()
        removed += await _LOADOUT_CACHE.clear()
        removed += await _CALENDAR_CACHE.clear()
        removed += await _CHALLENGE_RENDER_CACHE.clear()
        removed += await clear_http_cache("endfield-assets")
        removed += await clear_http_cache("endfield-account")
        removed += await clear_http_cache("endfield-gacha")
        removed += await clear_http_cache("endfield-official-calendar")
    elif scope in {"operator", "weapon", "equipment", "stage"}:
        removed += await _LOADOUT_CACHE.clear()
        removed += await service.clear_query_caches()
        cache_kinds = (
            {scope, "equipment_catalog", "equipment_attribute"}
            if scope == "equipment" else {scope, f"{scope}_catalog"}
        )
        removed += await _CARD_CACHE.clear(lambda key: key[1] in cache_kinds)
        removed += await clear_http_cache("endfield-api")
        removed += await clear_http_cache("akedata")
        clear_i18n_process_warm()
        removed += clear_account_detail_name_map()
        removed += clear_account_investment_catalog()
        if scope == "stage":
            removed += stage_service.clear_caches()
    return removed


async def _cache_status_lines() -> list[str]:
    api_stats = await get_http_cache_stats("endfield-api")
    asset_stats = await get_http_cache_stats("endfield-assets")
    table_stats = await get_http_cache_stats("akedata")
    card_stats = await _CARD_CACHE.stats()
    loadout_stats = await _LOADOUT_CACHE.stats()
    account_stats = await _ACCOUNT_PAGE_CACHE.stats()
    return [
        _format_cache_stats("API", api_stats),
        _format_cache_stats("AKEData", table_stats),
        _format_cache_stats("远程素材", asset_stats),
        _format_cache_stats("成品卡片", card_stats),
        _format_cache_stats("配装卡片", loadout_stats),
        _format_cache_stats("账号卡片", account_stats),
        f"缓存策略: TTL {int(CARD_CACHE_TTL_SECONDS)}s / 下载并发 8",
    ]


def _format_cache_stats(label: str, stats: CacheStats) -> str:
    return (
        f"{label}: {stats.entries} 项 / {stats.bytes / 1024 / 1024:.1f} MiB / "
        f"直接命中 {stats.direct_hits} / 未命中 {stats.misses} / 等待合并 {stats.coalesced} / "
        f"过期 {stats.expirations} / 容量淘汰 {stats.capacity_evictions}"
    )


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


_ownership_startup_started = False
_ownership_startup_task: asyncio.Task | None = None


@on_ready
async def _warmup_challenge_i18n(_bot=None) -> None:
    start_challenge_locale_warmup()


async def _refresh_due_ownership_snapshots() -> None:
    try:
        cipher = CredentialCipher.from_env()
        await ownership_stats_service.refresh_due(cipher)
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] scheduled refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return


async def _refresh_ownership_catalog() -> None:
    try:
        await ownership_stats_service.refresh_catalog()
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] catalog refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return


@on_ready
async def _warmup_ownership_snapshots(_bot=None) -> None:
    global _ownership_startup_started, _ownership_startup_task
    if _ownership_startup_started:
        return
    _ownership_startup_started = True
    _ownership_startup_task = asyncio.create_task(_refresh_due_ownership_snapshots())


timer.add_job(
    _refresh_due_ownership_snapshots,
    "interval",
    minutes=10,
    id="endfield_ownership_refresh",
    replace_existing=True,
    max_instances=1,
)

timer.add_job(
    _refresh_ownership_catalog,
    "interval",
    hours=6,
    id="endfield_ownership_catalog_refresh",
    replace_existing=True,
    max_instances=1,
)


@listen(Cleanup)
async def _close_ownership_startup_task() -> None:
    if _ownership_startup_task is not None and not _ownership_startup_task.done():
        _ownership_startup_task.cancel()
        await asyncio.gather(_ownership_startup_task, return_exceptions=True)
    await close_challenge_locale()
    await asyncio.gather(*(
        cache.close() for cache in (
            _CARD_CACHE, _LOADOUT_CACHE, _CALENDAR_CACHE,
            _CHALLENGE_DATA_CACHE, _CHALLENGE_RENDER_CACHE, service._weapon_relations, service._ake_views,
            _ACCOUNT_PAGE_CACHE,
        )
    ))
    await official_client.close()
