from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter

import aiohttp
from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Event
from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from nepattern import AnyString

from configs.config import Config
from utils.async_cache import AsyncTTLCache, CacheStats
from utils.entari_native import (
    ArgVal,
    ChainMsg,
    event_user_id,
    get_bot,
    get_group_id,
    is_group,
    make_image,
    on_alconna,
    on_ready,
    prompt,
    prompt_silently,
    timer,
)
from utils.http_client import clear_http_cache, get_http_cache_stats
from utils.temp_files import schedule_temp_file_cleanup

from .client import WarfarinAPIError, WarfarinClient
from .account_client import (
    ACCOUNT_PROVIDER_CN,
    ACCOUNT_PROVIDER_SKPORT,
    AttendanceResult,
    CURRENCY_TYPES,
    EndfieldAPIError,
    EndfieldOfficialClient,
    encode_account_credential,
    decode_account_credential,
    is_asia_role,
)
from .account_crypto import CredentialCipher, CredentialKeyError
from .account_store import EndfieldRole, EndfieldStore, RoleCandidate
from .account_i18n import server_label
from .account_currency import (
    aggregate_currency_logs,
    date_bounds as currency_date_bounds,
    earliest_currency_log_date,
    format_currency_log_report,
    format_all_history_period_label,
    resolve_query_dates,
    split_report,
)
from .account_currency_draw import draw_currency_log_cards
from .aliases import add_alias, alias_targets
from .commands import (
    EndfieldCandidate,
    CANDIDATE_SCORE_THRESHOLD,
    EquipmentAttributeFilter,
    ParsedEndfieldCommand,
    ParsedLoadoutSpec,
    ROOT_ALIASES,
    choose_candidate,
    candidate_options,
    dev_visible_for_user,
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
    score_candidate,
    score_entity_candidate,
)
from .draw import (
    draw_equipment_card,
    draw_equipment_catalog_card,
    draw_loadout_card,
    draw_medal_stats_card,
    draw_medal_missing_card,
    draw_operator_card,
    draw_operator_catalog_card,
    draw_weapon_card,
    draw_weapon_catalog_card,
    draw_attendance_card,
    draw_gacha_analysis_cards,
    draw_gacha_history_card,
)
from .version_calendar_draw import draw_version_calendar
from .official_calendar import OfficialVersionCalendarSource
from .official_calendar_draw import draw_official_version_calendar
from .account_detail_draw import draw_account_detail_cards
from .account_detail_service import build_account_detail_view
from .account_detail_names import fetch_account_detail_name_map
from .account_base_draw import draw_account_base_card
from .account_base_service import build_account_base_view
from .account_investment_draw import draw_account_investment_cards
from .account_investment_service import (
    InvestmentDataUnavailable,
    build_account_investment_view,
    fetch_account_investment_catalog,
)
from .stage_draw import draw_stage_card, draw_stage_catalog_cards
from .stage_service import EndfieldStageService, StageVariantNotFound
from .stage_source import StageDataIncomplete
from .gacha import (
    EndfieldGachaService,
    ROLE_TASKS,
    TaskAlreadyRunning,
    filter_xhh_import_six_stars,
    format_timestamp,
)
from .gacha_assets import EndfieldGachaAssetCache, apply_gacha_metadata
from .xhh_client import XhhAPIError, XhhLoginSession
from .models import (
    AttendanceCardView,
    AttendanceRewardView,
    AttendanceRoleView,
    GachaHistoryItemView,
    GachaHistoryView,
)
from .service import (
    EndfieldService,
    build_fz_operator_catalog_view,
    build_fz_weapon_catalog_view,
    format_status_quick_calc,
)
from .medal_store import MedalSnapshotStore
from .ownership_stats import (
    GroupMemberListError,
    OwnershipRefreshResult,
    OwnershipStatsRendererUnavailable,
    OwnershipStatsService,
    collect_group_member_ids,
    is_group_manager,
    register_ownership_stats_renderer,
    render_ownership_stats,
)
from .ownership_stats_draw import draw_ownership_stats
from .sources import source_label, source_order
from .version_calendar import AkeDataVersionCalendarSource, VersionCalendarError


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
ENDFIELD_HELP_IMAGE_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "image" / "help" / "endfield.png"
)
CARD_CACHE_TTL_SECONDS = 600.0
CARD_CACHE_MAX_BYTES = 48 * 1024 * 1024
CARD_RENDER_VERSION = "endfield-card-v41"
CardCacheKey = tuple[str, str, str, str, str, str, str]
_CARD_CACHE: AsyncTTLCache[CardCacheKey, tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=CARD_CACHE_TTL_SECONDS,
    max_bytes=CARD_CACHE_MAX_BYTES,
    max_entries=64,
    # A card can render as several images, so bound the cache on total bytes, not page count.
    sizeof=lambda pages: sum(len(page) for page in pages),
)
_CALENDAR_CACHE: AsyncTTLCache[str, bytes] = AsyncTTLCache(
    ttl_seconds=600.0,
    max_bytes=8 * 1024 * 1024,
    max_entries=4,
    sizeof=len,
)

Resolver = Callable[..., Awaitable[list[EndfieldCandidate]]]
Renderer = Callable[[str, str], Awaitable[bytes | tuple[bytes, ...] | None]]


CONTENT_RESOLVERS: dict[str, Resolver] = {
    "operator": lambda query: _resolve_candidates_from_sources("operator", query),
    "weapon": lambda query: _resolve_candidates_from_sources("weapon", query),
    "equipment": lambda query: _resolve_candidates_from_sources("equipment", query),
    "stage": lambda query: _resolve_candidates_from_sources("stage", query),
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
    "equipment": {
        "fz": lambda query, rarity: _resolve_equipment_candidates_fz(query, rarity),
    },
    "stage": {
        "fz": lambda query: _resolve_stage_candidates_fz(query),
        "akedata": lambda query: _resolve_stage_candidates_akedata(query),
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
    await _handle_command(endfield_cmd, event, parse_command(_rest(rest)), bot=bot)


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
        try:
            png = await _render_current_version_calendar()
            return await _finish_png(matcher, png)
        except _ExitException:
            raise
        except (VersionCalendarError, WarfarinAPIError, StageDataIncomplete) as exc:
            logger.error(f"[endfield] version calendar unavailable: {exc}")
            return await matcher.finish("当前版本日历暂不可用，请稍后重试")
        except Exception:
            logger.exception("[endfield] version calendar render failed")
            return await matcher.finish("当前版本日历生成失败，请稍后重试")
    if command.action == "dev":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            return await matcher.finish(format_unknown())
        return await matcher.finish(await _handle_dev_command(command))
    if command.action == "alias":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            return await matcher.finish(format_unknown())
        return await matcher.finish(_handle_alias_command(command))
    if command.action == "quick_calc":
        return await matcher.finish(
            format_status_quick_calc(command.status_name, command.status_level, command.arts_strength)
        )
    if command.action in {"medal_view", "medal_refresh"}:
        return await _handle_medal(matcher, command)
    if command.action in {"ownership_stats", "ownership_refresh"}:
        return await _handle_ownership_stats(matcher, event, command, bot=bot)
    if command.action in {"bind", "accounts", "account_base", "account_investment", "currency_log", "primary", "unbind", "attendance", "gacha", "gacha_history", "gacha_sync", "gacha_import", "medal_missing"}:
        return await _handle_personal_command(matcher, event, command)
    if command.action == "loadout":
        return await _handle_loadout(matcher, command)
    if command.action not in {"query", "search"}:
        return await matcher.finish(format_unknown())
    if command.scope == "stage" and command.source == "warfarin":
        return await matcher.finish("Warfarin Wiki 暂不支持关卡资料。")
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
        else:
            return await _finish_endfield_help(matcher)

    started = perf_counter()
    try:
        candidate_started = perf_counter()
        candidates = await _collect_candidates(command.scope, command.query, command.source, command.rarity)
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
            options = candidate_options(ambiguous, query=command.query)
            if not options:
                return await matcher.finish(format_not_found(command.scope, command.query))
            answer = await prompt_silently(format_candidates(options, interactive=True), timeout=60)
            if answer is None:
                return await matcher.finish()
            text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
            text = text.strip()
            if text.casefold() in {"取消", "cancel", "q", "quit"}:
                return await matcher.finish("已取消候选查询。")
            selection = parse_candidate_selection(text, len(options))
            if selection is None:
                return await matcher.finish(f"编号无效，请输入 1-{len(options)}。")
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
            return await matcher.finish("图片发送失败，请稍后重试")
    except _ExitException:
        raise
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {command.scope} {command.query}: {exc}")
        if command.scope == "stage":
            return await matcher.finish("关卡数据源暂时不可用")
        return await matcher.finish("数据源暂时不可用")
    except (StageVariantNotFound, StageDataIncomplete) as exc:
        return await matcher.finish(str(exc))
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("图片生成失败")


async def _handle_medal(matcher, command: ParsedEndfieldCommand) -> None:
    """F1：查看蚀刻章统计/新增；刷新时重抓 AKEData 数据 + 上一版本基线（源和源对比）。"""
    if command.action == "medal_refresh":
        async with _MEDAL_LOCK:
            await matcher.send("正在抓取 AKEData 蚀刻章数据…")
            started = perf_counter()
            try:
                snapshot = await service.fetch_medal_snapshot_akedata()
            except Exception as exc:
                logger.warning(f"[endfield] medal refresh failed: {exc}")
                return await matcher.finish("AKEData 数据源暂时不可用，请稍后重试。")
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
                return await matcher.finish("蚀刻章数据保存失败，请稍后重试。")
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
        return await matcher.finish("暂无蚀刻章数据，请先发送「/ef 奖章 刷新」。")
    baseline = medal_store.load_baseline_view()
    try:
        diff = service.build_medal_diff(current, baseline)
        pngs = await draw_medal_stats_card(diff)
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] medal card data failed: {exc}")
        return await matcher.finish("数据源暂时不可用。")
    except Exception as exc:
        logger.exception(f"[endfield] medal card failed: {exc}")
        return await matcher.finish("蚀刻章图片生成失败")
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
        return await matcher.finish("私聊中无法统计“群内”范围，请改用 /ef 持有率 全局。")

    user_id = str(event_user_id(event))
    active_bot = bot
    if scope == "group" and active_bot is None:
        try:
            active_bot = get_bot()
        except RuntimeError:
            return await matcher.finish("当前无法获取群成员列表，请稍后重试。")

    if command.action == "ownership_refresh":
        if scope == "global":
            if not _is_endfield_superuser(user_id):
                return await matcher.finish("仅 SUPERUSER 可以刷新全局持有率快照。")
        else:
            guild_id = get_group_id(event)
            if not _is_endfield_superuser(user_id) and not await is_group_manager(
                active_bot, event, guild_id, user_id
            ):
                return await matcher.finish("仅群主、群管理员或 SUPERUSER 可以刷新当前群快照。")

    if scope == "group":
        try:
            member_ids = await collect_group_member_ids(active_bot, get_group_id(event))
        except GroupMemberListError as exc:
            logger.warning(
                "[endfield-ownership] group member listing failed "
                f"standard_error_type={exc.standard_error_type} "
                f"fallback_error_type={exc.fallback_error_type}"
            )
            return await matcher.finish("获取当前群成员列表失败，已取消统计；不会回退为全局范围。")
        roles = account_store.list_all_roles(member_ids)
    else:
        roles = account_store.list_all_roles()

    refresh = None
    if command.action == "ownership_refresh":
        try:
            cipher = CredentialCipher.from_env()
            refresh = await ownership_stats_service.refresh_roles(roles, cipher, force=True)
        except CredentialKeyError as exc:
            return await matcher.finish(str(exc))
        logger.info(
            "[endfield-ownership] manual refresh "
            f"scope={scope} attempted={refresh.attempted} succeeded={refresh.succeeded} "
            f"failed={refresh.failed} skipped={refresh.skipped}"
        )
        return await matcher.finish(_format_ownership_refresh_result(scope, refresh))

    report = ownership_stats_service.build_report(scope, roles, refresh=refresh)
    try:
        rendered = await render_ownership_stats(report)
    except OwnershipStatsRendererUnavailable:
        return await matcher.finish("持有率统计数据已生成，但展示组件尚未接入。")
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
    catalog_label = "目录已更新" if refresh.catalog_updated else "目录无变化"
    elapsed = max(0, int(refresh.finished_at) - int(refresh.started_at))
    result = (
        f"{scope_label}持有率刷新完成：尝试 {refresh.attempted}，成功 {refresh.succeeded}，"
        f"失败 {refresh.failed}，跳过 {refresh.skipped}；{catalog_label}；耗时 {elapsed} 秒。"
    )
    if refresh.failed:
        result += "失败通常由绑定登录过期或官方接口异常导致；账号所有者可私聊使用 /ef 绑定更新凭证。"
    return result


async def _handle_medal_missing(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    """F2：查询绑定账号未获得/未升满/未镀层的蚀刻章。"""
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
    snapshot = medal_store.load_current_view()
    if snapshot is None:
        return await matcher.finish("暂无蚀刻章数据，请先发送「/ef 奖章 刷新」建立快照。")
    try:
        async with ROLE_TASKS.claim(role):
            token = account_store.decrypt_token(role, cipher)
            raw_progress = await official_client.endfield_card_detail(token, role)
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-medal] player progress API failed: {exc}")
        return await matcher.finish("奖章进度查询失败，请稍后重试。")
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except Exception as exc:
        logger.exception(f"[endfield-medal] progress query failed: {exc}")
        return await matcher.finish("奖章进度查询失败。")
    view = service.build_medal_missing_view(
        raw_progress, snapshot,
        nickname=role.nickname, uid=role.masked_uid, server_name=server_label(role.server_name or role.server_id),
    )
    try:
        pngs = await draw_medal_missing_card(view)
    except Exception as exc:
        logger.exception(f"[endfield-medal] missing card failed: {exc}")
        return await matcher.finish("缺章图片生成失败")
    return await _finish_pngs(matcher, pngs)


async def _handle_personal_command(matcher, event: Event, command: ParsedEndfieldCommand) -> None:
    private_only = {"bind", "primary", "unbind", "gacha_import"}
    if command.action in private_only and is_group(event):
        return await matcher.finish("该命令涉及账号凭据或手机号，仅支持私聊使用。")
    qq_user_id = str(event_user_id(event))

    try:
        if command.action == "bind":
            cipher = CredentialCipher.from_env()
            return await _handle_binding(matcher, qq_user_id, cipher)
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
                f"已将 {role.nickname}（{role.role_id}）设为主账号。" if role else "未找到对应账号，请使用 /ef 账号 查看编号。"
            )
        if command.action == "unbind":
            role = account_store.unbind(qq_user_id, command.account_selector)
            return await matcher.finish(
                f"已解绑 {role.nickname}（{role.role_id}）。" if role else "未找到对应账号，请使用 /ef 账号 查看编号。"
            )
        if command.action == "attendance":
            cipher = CredentialCipher.from_env()
            return await _handle_attendance(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "medal_missing":
            cipher = CredentialCipher.from_env()
            return await _handle_medal_missing(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action in {"gacha", "gacha_sync"}:
            cipher = CredentialCipher.from_env()
            return await _handle_gacha(matcher, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "gacha_import":
            return await _handle_xhh_import(matcher, qq_user_id, command)
        if command.action == "gacha_history":
            return await _handle_gacha_history(matcher, qq_user_id, command, group=is_group(event))
    except TaskAlreadyRunning:
        return await matcher.finish("任务正在进行")
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-account] official API request failed: operation={exc.operation} code={exc.code}")
        return await matcher.finish(str(exc))
    except InvestmentDataUnavailable as exc:
        logger.warning(f"[endfield-investment] AKEData unavailable: {exc}")
        return await matcher.finish("终末地养成数据源暂时不可用，请稍后重试。")
    except XhhAPIError as exc:
        return await matcher.finish(str(exc))
    except aiohttp.ClientConnectionError:
        logger.warning(f"[endfield-account] message connection interrupted: action={command.action}")
        return await matcher.finish("QQ 消息连接中断，请重新执行当前命令。")
    except _ExitException:
        raise
    except Exception as exc:
        logger.error(
            f"[endfield-account] action failed: action={command.action} "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return await matcher.finish("终末地账号功能暂时不可用，请稍后重试。")


async def _handle_binding(matcher, qq_user_id: str, cipher: CredentialCipher) -> None:
    region = await _prompt_text(
        "请选择服务器：\n1. 国服（森空岛，支持 Token/短信；二维码绑定暂不支持）\n"
        "2. 亚服（SKPORT，当前仅支持 Token）\n"
        "回复 1 或 2；回复“取消”退出。",
        timeout=90,
    )
    if region is None:
        return await matcher.finish("绑定已取消或等待超时。")
    normalized_region = region.casefold()
    if normalized_region in {"1", "国服", "cn", "china"}:
        provider = ACCOUNT_PROVIDER_CN
    elif normalized_region in {"2", "亚服", "亞洲", "亚洲", "asia", "skport"}:
        provider = ACCOUNT_PROVIDER_SKPORT
    else:
        return await matcher.finish("未识别服务器，绑定已取消。")

    if provider == ACCOUNT_PROVIDER_SKPORT:
        await matcher.send(
            "请在浏览器登录 SKPORT（https://www.skport.com/）后打开：\n"
            "https://web-api.skport.com/cookie_store/account_token\n"
            "页面会返回类似下面的内容（仅为格式范例，范例 Token 不能用于绑定）：\n"
            '{"code":0,"data":{"content":"FlJTn48gU1OwP9R7lQUpDFZJ"},"msg":""}\n'
            "上面的例子中，正确复制的内容是：\n"
            "FlJTn48gU1OwP9R7lQUpDFZJ\n"
            "请从你自己的页面中，只复制 content 后面双引号里的 Token。\n"
            "不要复制双引号，也不要复制整段 JSON。\n"
            "不要发送上面的范例 Token。\n"
            "不要在群聊或其他平台公开该内容。"
        )
        raw_account_token = await _prompt_text(
            "请发送 content 双引号内的 Token；回复“取消”退出。", timeout=150
        )
        if raw_account_token is None:
            return await matcher.finish("绑定已取消或等待超时。")
        account_token = encode_account_credential(raw_account_token, provider)
    else:
        account_token = await _bind_cn_account_token(matcher)
        if account_token is None:
            return None

    roles = await official_client.discover_roles(account_token)
    if provider == ACCOUNT_PROVIDER_SKPORT:
        roles = [role for role in roles if is_asia_role(role)]
        if not roles:
            return await matcher.finish("该 Gryphline 账号下未找到终末地亚服角色。")
    elif not roles:
        return await matcher.finish("该鹰角账号下未找到终末地角色。")
    selected = await _select_binding_roles(roles)
    if selected is None:
        return await matcher.finish("绑定已取消或等待超时。")
    previous_roles = account_store.list_roles(qq_user_id)
    previous_keys = {(role.role_id, role.server_id) for role in previous_roles}
    bound_roles = account_store.bind_roles(qq_user_id, account_token, selected, cipher)
    added_count = sum(
        (role.role_id, role.server_id) not in previous_keys for role in selected
    )
    updated_count = len(selected) - added_count
    region_label = "亚服" if provider == ACCOUNT_PROVIDER_SKPORT else "国服"
    summary = f"{region_label}绑定完成：新增 {added_count} 个账号"
    if updated_count:
        summary += f"，更新 {updated_count} 个账号"
    summary += f"；当前共绑定 {len(bound_roles)} 个账号。"
    selected_keys = {(role.role_id, role.server_id) for role in selected}
    try:
        refresh = await ownership_stats_service.refresh_roles(
            [role for role in bound_roles if (role.role_id, role.server_id) in selected_keys],
            cipher,
            force=True,
        )
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] binding refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
    else:
        logger.info(
            "[endfield-ownership] binding refresh "
            f"attempted={refresh.attempted} succeeded={refresh.succeeded} "
            f"failed={refresh.failed} skipped={refresh.skipped}"
        )
    return await matcher.finish(
        summary + "\n" + "\n".join(
            f"- {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}" for role in selected
        )
    )


async def _bind_cn_account_token(matcher) -> str | None:
    method = await _prompt_text(
        "请选择绑定方式：\n1. Token 绑定\n2. 手机号验证码绑定\n"
        "二维码绑定暂不支持。\n"
        "可重复绑定其他鹰角账号，已有账号不会被覆盖。\n回复 1 或 2；回复“取消”退出。",
        timeout=90,
    )
    if method is None:
        await matcher.finish("绑定已取消或等待超时。")
        return None
    normalized = method.casefold()
    if normalized in {"1", "token", "t"}:
        await matcher.send(
            "请在浏览器登录森空岛后打开：\nhttps://web-api.skland.com/account/info/hg\n"
            "复制响应中 data.content 的完整内容并发送。不要在群聊或其他平台公开该内容。"
        )
        account_token = await _prompt_text("请发送 data.content；回复“取消”退出。", timeout=150)
        if account_token is None:
            await matcher.finish("绑定已取消或等待超时。")
            return None
    elif normalized in {"2", "短信", "手机", "sms"}:
        phone = await _prompt_text("请输入用于鹰角账号登录的手机号；回复“取消”退出。", timeout=90)
        if phone is None:
            await matcher.finish("绑定已取消或等待超时。")
            return None
        if not re.fullmatch(r"1\d{10}", phone):
            await matcher.finish("手机号格式不正确，绑定已取消。")
            return None
        await official_client.send_phone_code(phone)
        code = await _prompt_text("验证码已发送，请输入短信验证码；回复“取消”退出。", timeout=120)
        if code is None:
            await matcher.finish("绑定已取消或等待超时。")
            return None
        if not re.fullmatch(r"\d{4,8}", code):
            await matcher.finish("验证码格式不正确，绑定已取消。")
            return None
        account_token = await official_client.token_by_phone_code(phone, code)
    # 暂时禁用二维码绑定，保留以下代码以便后续恢复：
    # elif normalized in {"3", "二维码", "扫码", "qr", "qrcode"}:
    #     account_token = await _bind_cn_qr_account(matcher)
    #     if account_token is None:
    #         return None
    else:
        if normalized in {"3", "二维码", "扫码", "qr", "qrcode"}:
            await matcher.finish("二维码绑定暂不支持，请选择 1 或 2。")
        else:
            await matcher.finish("未识别绑定方式，绑定已取消。")
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


async def _select_binding_roles(roles: list[RoleCandidate]) -> list[RoleCandidate] | None:
    if len(roles) == 1:
        return roles
    lines = ["检测到多个终末地角色，请回复编号、逗号分隔的多个编号，或“全部”："]
    lines.extend(
        f"{index}. {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}"
        for index, role in enumerate(roles, 1)
    )
    answer = await _prompt_text("\n".join(lines), timeout=120)
    if answer is None:
        return None
    if answer.casefold() in {"全部", "all"}:
        return roles
    try:
        indexes = {int(item.strip()) - 1 for item in re.split(r"[,，\s]+", answer) if item.strip()}
    except ValueError:
        return None
    if not indexes or any(index < 0 or index >= len(roles) for index in indexes):
        return None
    return [role for index, role in enumerate(roles) if index in indexes]


async def _handle_accounts(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
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
        return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
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
    token = account_store.decrypt_token(role, cipher)
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
    return await _finish_pngs(matcher, await draw_account_detail_cards(view))


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
        return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
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
        return await matcher.finish("已取消账号养成统计。")
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
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
        return await matcher.finish("养成统计目前仅支持国服账号，亚服暂不支持。")

    async def load_name_map():
        try:
            return await fetch_account_detail_name_map()
        except Exception as exc:
            logger.warning(f"[endfield] investment AKE name map unavailable: {exc}")
            return None

    detail, catalog = await asyncio.gather(
        _card_detail_with_snapshot(token, role),
        fetch_account_investment_catalog(),
    )
    name_map = await load_name_map()
    view = build_account_investment_view(
        detail,
        uid=role.masked_uid if group else role.role_id,
        nickname=role.nickname,
        server_name=role.server_name or role.server_id,
        catalog=catalog,
        name_map=name_map,
    )
    return await _finish_pngs(matcher, await draw_account_investment_cards(view))


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
        return await matcher.finish("尚未绑定终末地账号。请先私聊使用 /ef 绑定。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
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
        return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
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
        return await matcher.finish("资源流水查询目前仅支持国服账号，亚服暂不支持。")

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
        return await matcher.finish("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            return await matcher.finish("未找到对应账号，请使用 /ef 账号 查看编号。")
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
        return await matcher.finish(f"编号无效，请输入 1-{len(roles)}。")
    return await _render_account_base(matcher, role, cipher, group=group)


async def _render_account_base(
    matcher,
    role: EndfieldRole,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    token = account_store.decrypt_token(role, cipher)

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
    return await _finish_pngs(matcher, (await draw_account_base_card(view),))


async def _handle_attendance(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
    views: list[AttendanceRoleView] = []
    for role in roles:
        try:
            async with ROLE_TASKS.claim(role):
                token = account_store.decrypt_token(role, cipher)
                result = await official_client.attendance(token, role)
            views.append(_attendance_view(role, result))
        except TaskAlreadyRunning:
            views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "任务正在进行"))
        except EndfieldAPIError as exc:
            views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", str(exc)))
        except CredentialKeyError as exc:
            views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", str(exc)))
        except Exception as exc:
            logger.error(
                f"[endfield-account] attendance failed: stored_role={role.id} error_type={type(exc).__name__}"
            )
            views.append(AttendanceRoleView(role.nickname, role.masked_uid, role.server_name, "failed", "签到失败，请稍后重试"))
    png = await draw_attendance_card(
        AttendanceCardView(views, format_timestamp(int(__import__("time").time())))
    )
    return await _finish_png(matcher, png)


async def _handle_gacha(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
    gacha_service = EndfieldGachaService(account_store, official_client, cipher)
    states = account_store.list_sync_states(role)
    effective_full = command.full or not states
    existing_records = account_store.list_gacha_records(role, limit=100000)
    existing_pool_rules = await gacha_asset_cache.prepare_pool_rules(existing_records)
    result = await gacha_service.sync(
        role, full=effective_full, pool_rules=existing_pool_rules,
    )
    if command.action == "gacha_sync":
        failed = f"，{len(result.failed)} 个卡池失败" if result.failed else ""
        mode = "官方近 90 天窗口全量" if effective_full else "增量"
        suffix = "；本地会持续保留已同步记录" if effective_full else ""
        return await matcher.finish(f"{role.nickname} {mode}同步完成：新增 {result.inserted} 条{failed}{suffix}。")
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
    return await _finish_pngs(matcher, pngs)


async def _handle_gacha_history(matcher, qq_user_id: str, command: ParsedEndfieldCommand, *, group: bool) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
    total = account_store.count_gacha_records(role, command.pool_filter)
    total_pages = max(1, (total + 19) // 20)
    if command.page > total_pages and total:
        return await matcher.finish(f"页码超出范围，当前共 {total_pages} 页。")
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
        return await matcher.finish("未找到对应账号，请先私聊使用 /ef 绑定。")
    phone = await _prompt_text("请输入小黑盒账号绑定的手机号；回复“取消”退出。", timeout=90)
    if phone is None:
        return await matcher.finish("导入已取消或等待超时。")
    if not re.fullmatch(r"1\d{10}", phone):
        return await matcher.finish("手机号格式不正确，导入已取消。")

    session: XhhLoginSession | None = None
    try:
        async with ROLE_TASKS.claim(role):
            session = await XhhLoginSession.start(phone)
            code = await _prompt_text(
                "小黑盒验证码已发送，请输入短信验证码；回复“取消”退出。", timeout=120
            )
            if code is None:
                return await matcher.finish("导入已取消或等待超时。")
            if not re.fullmatch(r"\d{4,8}", code):
                return await matcher.finish("验证码格式不正确，导入已取消。")
            imported = await session.login_and_fetch(code)
            if imported.source_uid != role.role_id:
                return await matcher.finish(
                    f"小黑盒终末地 UID 与所选账号不一致，请切换账号后重试。所选账号 UID {role.masked_uid}。"
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
                    "FZ Wiki 星级目录暂未覆盖本次小黑盒记录，已取消导入以避免误判星级，请稍后重试。"
                )
            imported = filter_xhh_import_six_stars(imported, xhh_metadata)
            account_store.replace_xhh_gacha_import(role, imported)
    finally:
        if session is not None:
            await session.close()

    return await matcher.finish(
        f"{role.nickname} 的小黑盒历史统计导入完成：{len(imported.pools)} 个卡池，"
        f"{imported.total_count} 抽，{len(imported.six_stars)} 条六星记录。\n"
        "发送 /ef 抽卡 查看补齐后的分析卡；逐抽历史页仍只展示官方明细。"
    )


def _attendance_view(role: EndfieldRole, result: AttendanceResult) -> AttendanceRoleView:
    return AttendanceRoleView(
        nickname=role.nickname,
        uid=role.masked_uid,
        server_name=server_label(role.server_name or role.server_id),
        status=result.status,
        message=result.message,
        rewards=[AttendanceRewardView(item.name, item.count, item.icon_url) for item in result.rewards],
        monthly_count=result.monthly_count,
    )


def _format_accounts(roles: list[EndfieldRole], *, reveal_uid: bool, detail_hint: bool = False) -> str:
    if not roles:
        return "尚未绑定终末地账号。使用 /ef 绑定 开始绑定。"
    lines = ["已绑定的终末地账号："]
    for index, role in enumerate(roles, 1):
        marker = " [主账号]" if role.is_primary else ""
        uid = role.role_id if reveal_uid else role.masked_uid
        lines.append(f"{index}. {role.nickname}{marker} · {server_label(role.server_name or role.server_id)} · UID {uid}")
    if detail_hint:
        lines.append("回复编号查看该账号详情，或回复“取消”退出。")
    lines.append("可使用 /ef 添加账号 继续绑定，或用 /ef 主账号 <编号>、/ef 解绑 <编号> 管理。")
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
            return await matcher.finish(f"配装参数错误：{error or '已取消'}")

        resolved: list[tuple[EndfieldCandidate, tuple[tuple[int, int], ...]]] = []
        for index, item in enumerate(spec.items):
            candidate_kind = "operator" if index == 0 else "gear"
            candidate = await _resolve_loadout_candidate(candidate_kind, item.name)
            if candidate is None:
                label = "干员" if index == 0 else "武器或装备"
                return await matcher.finish(f"未找到{label}：{item.name}")
            if item.forge_levels and candidate.kind != "equipment":
                return await matcher.finish(f"只有装备可以设置词条锻造：{item.name}")
            resolved.append((candidate, item.forge_levels))

        operators = [item for item, _ in resolved if item.kind == "operator"]
        weapons = [item for item, _ in resolved if item.kind == "weapon"]
        if len(operators) != 1:
            return await matcher.finish("配装命令需要且只能包含一个干员")
        if len(weapons) > 1:
            return await matcher.finish("配装命令最多包含一把武器")
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
        png = await draw_loadout_card(view)
        logger.info(
            f"[endfield] perf action=loadout data={data_seconds:.3f}s "
            f"draw={perf_counter() - started - data_seconds:.3f}s"
        )
        return await _finish_png(matcher, png)
    except _ExitException:
        raise
    except (WarfarinAPIError, ValueError) as exc:
        logger.warning(f"[endfield] loadout rejected: {exc}")
        return await matcher.finish(f"配装计算失败：{exc}")
    except Exception as exc:
        logger.exception(f"[endfield] loadout failed: {exc}")
        return await matcher.finish("配装图片生成失败")


async def _prompt_loadout_spec(default_enhance: int) -> tuple[ParsedLoadoutSpec | None, str]:
    answer = await prompt(
        "请先发送干员名称，再填写可选武器和装备名称，使用空格分隔；武器与装备顺序任意。\n"
        "单独调整词条可在装备后追加：词条2锻造2",
        timeout=90,
    )
    if answer is None:
        return None, "等待输入超时"
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if text.lower() in {"取消", "cancel", "q", "quit"}:
        return None, "已取消"
    return parse_loadout_spec(text, default_enhance)


async def _resolve_loadout_candidate(kind: str, query: str) -> EndfieldCandidate | None:
    raw_candidates = (
        await _collect_candidates("all", query, "fz", "all")
        if kind in {"all", "gear"}
        else await _resolve_candidates_from_sources(kind, query, "fz", "all")
    )
    if kind == "all":
        allowed_kinds = {"operator", "weapon", "equipment"}
    elif kind == "gear":
        allowed_kinds = {"weapon", "equipment"}
    else:
        allowed_kinds = {kind}
    candidates = [item for item in raw_candidates if item.kind in allowed_kinds and item.source == "fz"]
    selected, ambiguous = choose_candidate(candidates)
    if selected is not None:
        return selected
    options = ambiguous or sorted(candidates, key=lambda item: item.score, reverse=True)
    if not options:
        return None
    options = options[:8]
    lines = [f"“{query}”有多个匹配结果，请回复编号："]
    lines.extend(f"{index}. {item.display_name}" for index, item in enumerate(options, 1))
    answer = await prompt("\n".join(lines), timeout=60)
    if answer is None:
        return None
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    try:
        index = int(text.strip()) - 1
    except ValueError:
        return None
    return options[index] if 0 <= index < len(options) else None


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


async def _resolve_candidates_from_sources(
    kind: str,
    query: str,
    requested_source: str = "",
    rarity: str = "",
) -> list[EndfieldCandidate]:
    if kind == "stage" and not requested_source:
        return await _resolve_stage_candidates(query)
    resolvers = SOURCE_CANDIDATE_RESOLVERS.get(kind, {})
    errors: list[WarfarinAPIError] = []
    sources = (requested_source,) if requested_source else source_order(kind)
    for source in sources:
        resolver = resolvers.get(source)
        if resolver is None:
            continue
        try:
            candidates = await resolver(query, rarity) if kind == "equipment" else await resolver(query)
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


async def _resolve_stage_candidates_fz(query: str) -> list[EndfieldCandidate]:
    return await _resolve_stage_candidates(query, "fz")


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

    catalog = await service.get_equipment_catalog_view(rarity_filter=rarity_filter)
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
        if candidate.kind == "stage":
            output, degraded = await _render_stage(
                candidate.key,
                effective_source,
                mode=candidate.mode or "detail",
                selector=candidate.variant,
            )
        else:
            output = await renderer(candidate.key, effective_source)
        if output is None:
            raise _CardNotFound
        # Renderers that never overflow still return a single image; normalize here so the
        # cache and the send path only ever deal with pages.
        return (output,) if isinstance(output, bytes) else tuple(output)

    try:
        pages, cache_hit = await _CARD_CACHE.get_or_create_with_status(cache_key, render)
    except _CardNotFound:
        return None
    if degraded:
        # A card missing data only because a fetch failed must not be served for the full TTL.
        await _CARD_CACHE.clear(lambda key: key == cache_key)
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
    if source and source != "fz":
        return None
    started = perf_counter()
    if source == "fz":
        view = await service.get_equipment_view_from_fz(key)
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
    if source and source != "fz":
        return None
    element, profession = _parse_operator_catalog_key(key)
    view = await service.get_operator_catalog_view(element, profession)
    return await draw_operator_catalog_card(view)


async def _render_weapon_catalog(key: str, source: str = "") -> bytes | None:
    if source and source != "fz":
        return None
    view = await service.get_weapon_catalog_view(key)
    return await draw_weapon_catalog_card(view)


async def _render_equipment_catalog(key: str, source: str = "") -> bytes | None:
    if source and source != "fz":
        return None
    started = perf_counter()
    group_name, rarity_filter = _parse_equipment_catalog_key(key)
    view = await service.get_equipment_catalog_view(group_name, rarity_filter)
    data_seconds = perf_counter() - started
    draw_started = perf_counter()
    output = await draw_equipment_catalog_card(view)
    logger.info(
        f"[endfield] render kind=equipment_catalog data={data_seconds:.3f}s "
        f"draw={perf_counter() - draw_started:.3f}s"
    )
    return output


async def _render_equipment_attribute(key: str, source: str = "") -> bytes | None:
    if source and source != "fz":
        return None
    filters, rarity_filter = _parse_equipment_attribute_key(key)
    if not filters:
        return None
    started = perf_counter()
    try:
        view = await service.get_equipment_attribute_catalog_view(filters, rarity_filter)
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
    if source and source not in {"fz", "akedata"}:
        return None, False
    started = perf_counter()
    view = await stage_service.get_stage_view(
        key,
        mode=mode,
        selector=selector,
        source=source or "fz",
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
    if source and source not in {"fz", "akedata"}:
        return None
    return await draw_stage_catalog_cards(await stage_service.get_catalog_view(source))


async def _finish_png(matcher, png: bytes) -> None:
    return await _finish_pngs(matcher, (png,))


async def _render_current_version_calendar() -> bytes:
    try:
        official = await official_calendar_source.current()
        return await _CALENDAR_CACHE.get_or_create(
            f"official:{official.revision}",
            lambda: draw_official_version_calendar(official),
        )
    except Exception as exc:
        logger.warning(
            f"[endfield] official calendar unavailable, use AkeData fallback: "
            f"{type(exc).__name__}: {exc}"
        )
    calendar = await calendar_source.current()
    return await _CALENDAR_CACHE.get_or_create(
        f"generated:{calendar.version}:{calendar.revision}",
        lambda: draw_version_calendar(calendar),
    )


async def _finish_endfield_help(matcher) -> None:
    if ENDFIELD_HELP_IMAGE_PATH.exists():
        return await matcher.finish(ChainMsg([make_image(path=ENDFIELD_HELP_IMAGE_PATH)]))
    return await matcher.finish(format_help())


async def _finish_pngs(matcher, pngs: tuple[bytes, ...]) -> None:
    images = []
    for png in pngs:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
            file.write(png)
            file.flush()
            schedule_temp_file_cleanup(file.name)
            images.append(make_image(path=file.name))
    await matcher.finish(ChainMsg(images))


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
            return "用法：/ef dev refresh <all|干员|武器|装备|关卡> [关键词]"
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
                return "用法：/ef dev cache clear <all|operator|weapon|equipment|stage|icon>"
            removed = await _clear_endfield_caches(scope)
            return f"已清理 {scope} 缓存，共 {removed} 项。"
        return "\n".join(await _cache_status_lines())
    return "dev 命令：status | resolve | refresh | cache"


def _handle_alias_command(command: ParsedEndfieldCommand) -> str:
    usage = "用法：/ef 别名 添加 <干员|武器|装备> <正式名称> <新别名>"
    if command.alias_action != "add" or len(command.args) < 3:
        return usage
    kind = normalize_alias_kind(command.args[0])
    if not kind:
        return usage
    canonical_name = command.args[1]
    alias = " ".join(command.args[2:]).strip()
    try:
        canonical, added = add_alias(kind, canonical_name, alias)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[endfield] alias update rejected: {exc}")
        return f"添加别名失败：{exc}"
    label = {"operator": "干员", "weapon": "武器", "equipment": "装备"}[kind]
    if not added:
        return f"{label}别名已存在：{alias} → {canonical}"
    targets = alias_targets(kind, alias)
    collision = f"\n该别名同时匹配：{'、'.join(targets)}" if len(targets) > 1 else ""
    return f"已添加{label}别名：{alias} → {canonical}{collision}"


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
    removed = 0
    if scope == "all":
        removed += await _CARD_CACHE.clear()
        removed += await clear_http_cache("endfield-")
    elif scope == "icon":
        removed += await clear_http_cache("endfield-assets")
    elif scope in {"operator", "weapon", "equipment", "stage"}:
        cache_kinds = (
            {scope, "equipment_catalog", "equipment_attribute"} if scope == "equipment" else {scope}
        )
        removed += await _CARD_CACHE.clear(lambda key: key[1] in cache_kinds)
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


async def _refresh_due_ownership_snapshots() -> None:
    try:
        cipher = CredentialCipher.from_env()
        refresh = await ownership_stats_service.refresh_due(cipher)
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] scheduled refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return
    logger.info(
        "[endfield-ownership] scheduled refresh "
        f"attempted={refresh.attempted} succeeded={refresh.succeeded} "
        f"failed={refresh.failed} skipped={refresh.skipped}"
    )


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
    hours=24,
    id="endfield_ownership_refresh",
    replace_existing=True,
    max_instances=1,
)
