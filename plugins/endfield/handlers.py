from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from time import perf_counter

import aiohttp
from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import At, Cleanup, Event, Image, MessageChain, Session, command, listen
from arclet.entari.command import Match
from arclet.letoderea import ExitState
from loguru import logger
from nepattern import AnyString

from otae_bot.config.settings import Config
from otae_bot.hypergryph.attendance_delivery import build_delivery, deliver
from otae_bot.hypergryph.attendance_registry import (
    AttendanceCapability,
    AttendanceResult,
    collect_companion_outcomes,
    register_attendance_capability,
    signed_outcome,
)
from otae_bot.groups.features import feature_store, scope_from_event
from otae_bot.accounts.ready import get_bot, on_ready
from otae_bot.messaging.inbound import event_chain, event_user_id, get_group_id, is_group
from otae_bot.messaging.prompts import prompt_silently
from otae_bot.scheduling.jobs import timer
from otae_bot.pipeline.message_log import sensitive_input
from otae_bot.command_usage import describe_command
from otae_bot.infrastructure.rendering.help_runtime import cached_help_image
from otae_bot.infrastructure.http.asset_policy import asset_render_budget
from otae_bot.infrastructure.http.assets import broker as asset_broker

from .providers.warfarin import WarfarinAPIError
from .rendering.health import track_render_health
from .announcements.runtime import AnnouncementRuntime
from .account.challenge.i18n import close_challenge_locale
from .account.client import (
    ACCOUNT_PROVIDER_SKPORT,
    EndfieldAPIError,
    decode_account_credential,
    is_asia_role,
)
from .account.crypto import CredentialCipher, CredentialKeyError
from .account.store import EndfieldRole
from .account.i18n import server_label
from .catalog.commands import (
    ParsedEndfieldCommand,
    ROOT_ALIASES,
    usage_subcommand_labels,
    choose_candidate,
    candidate_options,
    dev_visible_for_user,
    ENCYCLOPEDIA_SCOPES,
    format_candidates,
    format_error,
    format_help,
    format_not_found,
    format_source,
    format_unknown,
    parse_command,
    parse_candidate_selection,
    parse_shortcut_command,
    strip_message_mentions,
)
from .rendering.cards import (
    draw_archive_progress_card,
    draw_archive_stats_card,
    draw_medal_stats_card,
    draw_medal_missing_card,
    draw_attendance_card,
)
from .account.investment.service import InvestmentDataUnavailable
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
    resolve_war_season_page,
)
from .account.challenge.i18n import ChallengeLocale, get_challenge_locale, start_challenge_locale_warmup
from .account.challenge.parsing import (
    _difficulty_label as _challenge_difficulty_label,
    _monument_difficulty_label as _challenge_monument_difficulty_label,
)
from .stages.service import StageVariantNotFound
from .stages.fz import StageDataIncomplete
from .gacha.service import ROLE_TASKS, TaskAlreadyRunning
from .gacha.xhh import XhhAPIError
from .catalog.models import AttendanceCardView
from .catalog.service import format_status_quick_calc
from .catalog.refresh import SnapshotPersistenceError
from .ownership.service import (
    GroupMemberListError,
    OwnershipRefreshResult,
    OwnershipStatsRendererUnavailable,
    collect_group_member_ids,
    is_group_manager,
    render_ownership_stats,
)
from .usage.draw import (
    draw_usage_history,
    draw_usage_overview,
    draw_usage_stage,
)
from .usage.models import (
    FAMILY_LABELS as USAGE_FAMILY_LABELS,
    UsageStageReport,
)
from .usage.service import (
    FAMILY_MONUMENT as USAGE_FAMILY_MONUMENT,
    FAMILY_WAR_ECHO as USAGE_FAMILY_WAR_ECHO,
    UsageRefreshResult as UsageStatsRefreshResult,
    UsageResolutionError,
    UsageUnavailableError,
)
from .providers.registry import source_label, source_order
from .cold_start import (
    cold_start_command,
    notice_before_public_data,
    notice_default_ake_public,
)
from .calendar.akedata import VersionCalendarError
from .paths import HELP_IMAGE_PATH as ENDFIELD_HELP_IMAGE_PATH
from .attendance import (
    format_attendance_report,
    sign_roles as sign_attendance_roles,
)
from .providers.repository import AkeDataIncomplete
from .encyclopedia.props import PropEffectIncomplete
from .state import (
    account_store,
    archive_store,
    collection_refresher,
    medal_store,
    official_client,
    ownership_stats_service,
    service,
    usage_stats_service,
)
from . import caches
from .caches import (
    CARD_RENDER_VERSION,
    ChallengeCacheKey,
    _ACCOUNT_PAGE_CACHE,
    _CALENDAR_CACHE,
    _CARD_CACHE,
    _CARD_DETAIL_CACHE,
    _CHALLENGE_DATA_CACHE,
    _CHALLENGE_RENDER_CACHE,
    _LOADOUT_CACHE,
)
from .replies import (
    _IncompletePages,
    _finish_png,
    _finish_pngs,
    _is_endfield_superuser,
    _one_page,
    _send_forward_pngs,
)
from .calendar.handlers import _render_current_version_calendar
from .catalog.handlers import (
    _collect_candidates,
    _handle_alias_command,
    _handle_dev_command,
    _handle_loadout,
    _handle_random_wiki,
    _item_scope_fallback,
    _render_candidate,
)
from .account.binding import _handle_binding
from .account.handlers import (
    _background_tasks,
    _forget_card_detail,
    _handle_account_base,
    _handle_account_currency,
    _handle_account_investment,
    _handle_accounts,
    _handle_daily,
    _handle_exploration,
)
from .gacha.handlers import _handle_gacha, _handle_gacha_history, _handle_xhh_import


endfield_cmd = Alconna(list(ROOT_ALIASES), Args["rest;?", MultiVar(AnyString)])

# 指令统计：/终末地 /ef /zmd 合并显示为 /ef；不是登记子命令的第一个词按资料查询计。
describe_command(ROOT_ALIASES, display="/ef", subcommands=usage_subcommand_labels(), default_sub="资料查询")

endfield_operator_shortcut = Alconna(["efop", "efoperator", "终末地干员"], Args["rest;?", MultiVar(AnyString)])
endfield_weapon_shortcut = Alconna(["efwp", "efweapon", "终末地武器"], Args["rest;?", MultiVar(AnyString)])
endfield_equipment_shortcut = Alconna(["efeq", "efequipment", "终末地装备"], Args["rest;?", MultiVar(AnyString)])
endfield_search_shortcut = Alconna(["efs", "efsearch", "终末地搜索"], Args["rest;?", MultiVar(AnyString)])


@command.on(endfield_cmd)
async def handle_endfield(session: Session, rest: Match[tuple[str, ...]]):
    command_rest = _rest(rest)
    command = parse_command(command_rest)
    if command.action == "challenge":
        command = parse_command(strip_message_mentions(command_rest))
    await _handle_command(session, session.event, command, bot=session.account)


@command.on(endfield_operator_shortcut)
async def handle_endfield_operator_shortcut(session: Session, rest: Match[tuple[str, ...]]):
    await _handle_command(session, session.event, parse_shortcut_command("efop", _rest(rest)))


@command.on(endfield_weapon_shortcut)
async def handle_endfield_weapon_shortcut(session: Session, rest: Match[tuple[str, ...]]):
    await _handle_command(session, session.event, parse_shortcut_command("efwp", _rest(rest)))


@command.on(endfield_equipment_shortcut)
async def handle_endfield_equipment_shortcut(session: Session, rest: Match[tuple[str, ...]]):
    await _handle_command(session, session.event, parse_shortcut_command("efeq", _rest(rest)))


@command.on(endfield_search_shortcut)
async def handle_endfield_search_shortcut(session: Session, rest: Match[tuple[str, ...]]):
    await _handle_command(session, session.event, parse_shortcut_command("efs", _rest(rest)))


async def _handle_command(session, event: Event, command: ParsedEndfieldCommand, bot=None) -> None:
    if command.error:
        await session.send(format_error(command.error))
        return
    if command.action == "help":
        return await _finish_endfield_help(session)
    if command.action == "source":
        await session.send(format_source())
        return
    if command.action == "announcements":
        await session.send(await announcement_runtime.command(event, command.args, bot))
        return
    if command.action == "random_wiki":
        return await _handle_random_wiki(session)
    if command.action == "calendar":
        with cold_start_command(session):
            await notice_default_ake_public()
            try:
                png = await _render_current_version_calendar()
                return await _finish_png(session, png)
            except ExitState:
                raise
            except (VersionCalendarError, WarfarinAPIError, StageDataIncomplete) as exc:
                logger.error(f"[endfield] version calendar unavailable: {exc}")
                await session.send("版本日历数据暂不可用，请稍后再试。")
                return
            except Exception:
                logger.exception("[endfield] version calendar render failed")
                await session.send("版本日历图生成失败，请稍后再试。")
                return
    if command.action == "dev":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            await session.send(format_unknown())
            return
        await session.send(await _handle_dev_command(command))
        return
    if command.action == "alias":
        if not dev_visible_for_user(str(event_user_id(event)), Config.SUPERUSERS):
            await session.send(format_unknown())
            return
        await session.send(await _handle_alias_command(command))
        return
    if command.action == "quick_calc":
        await session.send(
            format_status_quick_calc(command.status_name, command.status_level, command.arts_strength)
        )
        return
    if command.action in {"medal_view", "medal_refresh"}:
        return await _handle_medal(session, command)
    if command.action in {"archive_view", "archive_refresh"}:
        return await _handle_archive(session, command)
    if command.action in {"ownership_stats", "ownership_refresh"}:
        return await _handle_ownership_stats(session, event, command, bot=bot)
    if command.action in {"bind", "accounts", "account_base", "account_investment", "currency_log", "primary", "unbind", "attendance", "daily", "exploration", "gacha", "gacha_history", "gacha_sync", "gacha_import", "medal_missing", "archive_progress", "challenge"}:
        return await _handle_personal_command(session, event, command, bot=bot)
    if command.action == "loadout":
        return await _handle_loadout(session, command)
    if command.action not in {"query", "search"}:
        await session.send(format_unknown())
        return
    if command.scope == "stage" and command.source and command.source != "akedata":
        await session.send(f"{source_label(command.source)} 暂不支持关卡资料，关卡数据仅支持 AkeData。")
        return
    if (
        command.scope in ENCYCLOPEDIA_SCOPES
        and command.source
        and command.source not in source_order(command.scope)
    ):
        await session.send("该类资料仅由 AkeData 提供。")
        return
    if command.scope == "archive_entry" and archive_store.load_current_view() is None:
        # 显式档案范围且没有快照：直接回文案，不抛异常、不走「资料暂时不可用」。
        await session.send("档案数据尚未构建，请先发送 /ef 档案 刷新。")
        return
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
            return await _finish_endfield_help(session)

    started = perf_counter()
    try:
        with cold_start_command(session):
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
                await session.send("奖章查询请使用 /ef 奖章 指令。")
                return
            if fallback is not None:
                candidates = fallback
            candidate_seconds = perf_counter() - candidate_started
            if command.action == "search":
                title = "检索结果" if candidates else "未匹配到相关条目"
                logger.info(
                    f"[endfield] perf action=search scope={command.scope} "
                    f"candidate={candidate_seconds:.3f}s total={perf_counter() - started:.3f}s"
                )
                await session.send(
                    format_candidates(candidates, title=title, scope=command.scope, query=command.query)
                )
                return

            selected, ambiguous = choose_candidate(candidates)
            if ambiguous:
                options = candidate_options(ambiguous, query=command.query)
                if not options:
                    await session.send(format_not_found(command.scope, command.query))
                    return
                answer = await prompt_silently(session, format_candidates(options, interactive=True), timeout=60)
                if answer is None:
                    return
                text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
                text = text.strip()
                if text.casefold() in {"取消", "cancel", "q", "quit"}:
                    await session.send("已取消候选选择。")
                    return
                selection = parse_candidate_selection(text, len(options))
                if selection is None:
                    await session.send(f"序号输入有误，请输入 1–{len(options)}。")
                    return
                selected = options[selection]
            if selected is None:
                await session.send(format_not_found(command.scope, command.query))
                return

            render_started = perf_counter()
            pngs = await _render_candidate(selected, command.source)
            render_seconds = perf_counter() - render_started
            if pngs is None:
                await session.send(format_not_found(selected.kind, command.query))
                return
            logger.info(
                f"[endfield] perf action=query scope={command.scope} kind={selected.kind} "
                f"candidate={candidate_seconds:.3f}s render={render_seconds:.3f}s "
                f"pages={len(pngs)} total_before_send={perf_counter() - started:.3f}s"
            )
            try:
                return await _finish_pngs(session, pngs)
            except ExitState:
                raise
            except Exception as exc:
                logger.exception(f"[endfield] send failed for {selected.kind} {command.query}: {exc}")
                await session.send("图片发送失败，请稍后再试。")
                return
    except ExitState:
        raise
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {command.scope} {command.query}: {exc}")
        if command.scope == "stage":
            await session.send("关卡数据源暂时响应异常，请稍后再试。")
            return
        await session.send("数据源暂时响应异常，请稍后再试。")
        return
    except (StageVariantNotFound, StageDataIncomplete) as exc:
        await session.send(str(exc))
        return
    except PropEffectIncomplete as exc:
        logger.warning(f"[endfield] prop effect incomplete for {command.scope} {command.query}: {exc}")
        await session.send("该道具效果数值暂未收录。")
        return
    except AkeDataIncomplete as exc:
        logger.warning(f"[endfield] AKE data incomplete for {command.scope} {command.query}: {exc}")
        await session.send("该资料暂时不可用，请稍后再试。")
        return
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {command.scope} {command.query}: {exc}")
        await session.send("卡片图片生成失败，请稍后再试。")
        return


async def _handle_medal(session, command: ParsedEndfieldCommand) -> None:
    """F1：查看蚀刻章统计/新增；刷新时重抓 AKEData 数据 + 上一版本基线（源和源对比）。"""
    if command.action == "medal_refresh":
        await session.send("正在同步 AKEData 蚀刻章最新数据…")
        try:
            await collection_refresher.refresh("medal")
        except SnapshotPersistenceError as exc:
            logger.exception(f"[endfield] medal snapshot persistence failed: {exc}")
            await session.send("蚀刻章本地快照保存失败，请稍后再试。")
            return
        except Exception as exc:
            logger.warning(f"[endfield] medal refresh failed: {exc}")
            await session.send("AKEData 奖章数据源响应异常，请稍后再试。")
            return
    else:
        await collection_refresher.ensure_fresh()

    current = medal_store.load_current_view()
    if current is None:
        await session.send("本地暂无蚀刻章数据快照，请稍后查询，或发送 /ef 奖章 刷新 重试。")
        return
    baseline = medal_store.load_baseline_view()
    try:
        diff = service.build_medal_diff(current, baseline)
        if command.action == "medal_view":
            with cold_start_command(session):
                pngs = await draw_medal_stats_card(diff)
        else:
            pngs = await draw_medal_stats_card(diff)
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] medal card data failed: {exc}")
        await session.send("数据源暂时响应异常，请稍后再试。")
        return
    except Exception as exc:
        logger.exception(f"[endfield] medal card failed: {exc}")
        await session.send("蚀刻章统计卡生成失败，请稍后再试。")
        return
    return await _finish_pngs(session, pngs)


async def _handle_archive(session, command: ParsedEndfieldCommand) -> None:
    """档案库版本统计/新增；刷新时重抓 AKEData 档案表 + 上一版本基线（源和源对比）。"""
    if command.action == "archive_refresh":
        await session.send("正在同步 AKEData 档案库最新数据…")
        try:
            await collection_refresher.refresh("archive")
        except SnapshotPersistenceError as exc:
            logger.exception(f"[endfield] archive snapshot persistence failed: {exc}")
            await session.send("档案库本地快照保存失败，请稍后再试。")
            return
        except Exception as exc:
            logger.warning(f"[endfield] archive refresh failed: {exc}")
            await session.send("AKEData 档案数据源响应异常，请稍后再试。")
            return
    else:
        await collection_refresher.ensure_fresh()

    current = archive_store.load_current_view()
    if current is None:
        await session.send("本地暂无档案库数据快照，请稍后查询，或发送 /ef 档案 刷新 重试。")
        return
    baseline = archive_store.load_baseline_view()
    try:
        diff = service.build_archive_diff(current, baseline)
        if command.action == "archive_view":
            with cold_start_command(session):
                pngs = await draw_archive_stats_card(diff)
        else:
            pngs = await draw_archive_stats_card(diff)
    except Exception as exc:
        logger.exception(f"[endfield] archive card failed: {exc}")
        await session.send("档案库统计卡生成失败，请稍后再试。")
        return
    return await _finish_pngs(session, pngs)


async def _handle_ownership_stats(
    session,
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
        await session.send("私聊环境不支持群内统计，请使用 /ef 持有率 全局。")
        return

    user_id = str(event_user_id(event))
    active_bot = bot
    if scope == "group" and active_bot is None:
        try:
            active_bot = get_bot()
        except RuntimeError:
            await session.send("群成员列表获取失败，请稍后再试。")
            return

    if command.action == "ownership_refresh":
        if scope == "global":
            if not _is_endfield_superuser(user_id):
                await session.send("权限不足：仅机器人管理员可刷新全局持有率快照。")
                return
        else:
            guild_id = get_group_id(event)
            if not _is_endfield_superuser(user_id) and not await is_group_manager(
                active_bot, event, guild_id, user_id
            ):
                await session.send("权限不足：仅群主、群管理员或机器人管理员可刷新本群持有率快照。")
                return

    if scope == "group":
        try:
            member_ids = await collect_group_member_ids(active_bot, get_group_id(event))
        except GroupMemberListError as exc:
            logger.warning(
                "[endfield-ownership] group member listing failed "
                f"standard_error_type={exc.standard_error_type}"
            )
            await session.send("获取本群成员列表失败，已终止统计任务。")
            return
        roles = account_store.list_all_roles(member_ids)
    else:
        roles = account_store.list_all_roles()

    refresh = None
    if command.action == "ownership_refresh":
        try:
            cipher = CredentialCipher.from_settings()
            with cold_start_command(session):
                refresh = await ownership_stats_service.refresh_roles(
                    roles,
                    cipher,
                    force=True,
                    trigger=f"manual-{scope}",
                )
        except CredentialKeyError as exc:
            await session.send(str(exc))
            return
        await session.send(_format_ownership_refresh_result(scope, refresh))
        return

    report = ownership_stats_service.build_report(scope, roles, refresh=refresh)
    try:
        with cold_start_command(session):
            rendered = await render_ownership_stats(report)
    except OwnershipStatsRendererUnavailable:
        await session.send("持有率数据已统计完成，但渲染展示模块暂未就绪。")
        return
    if isinstance(rendered, bytes):
        return await _finish_png(session, rendered)
    if isinstance(rendered, (list, tuple)) and all(isinstance(item, bytes) for item in rendered):
        return await _finish_pngs(session, tuple(rendered))
    if isinstance(rendered, (list, tuple)):
        rendered = MessageChain(rendered)
    await session.send(rendered if isinstance(rendered, MessageChain) else str(rendered))


async def _handle_usage_stats(
    session,
    event: Event,
    command: ParsedEndfieldCommand,
    *,
    bot=None,
) -> None:
    """群内关卡成员使用率:读快照聚合出卡,刷新走权限校验。"""
    group_chat = is_group(event)
    scope = command.scope if command.scope in {"group", "global"} else "auto"
    if scope == "auto":
        scope = "group" if group_chat else "global"
    if scope == "group" and not group_chat:
        await session.send("私聊环境不支持群内统计，请使用 /ef 影拓 使用率 全局。")
        return

    family = USAGE_FAMILY_WAR_ECHO if command.challenge_kind == "war_echo" else USAGE_FAMILY_MONUMENT
    # 统一刷新入口不带玩法前缀，两族一起刷，文案也不能只说一边。
    refresh_all = command.challenge_view == "usage_refresh" and not command.challenge_kind
    family_label = "影拓与回响" if refresh_all else USAGE_FAMILY_LABELS[family]
    user_id = str(event_user_id(event))
    active_bot = bot
    mentions = _challenge_mention_targets(event, bot)
    if len(mentions) > 1:
        await session.send("单次查询仅支持 @ 一位群友。")
        return
    if mentions and not group_chat:
        await session.send("通过 @群友 查询使用率仅限在群聊中使用。")
        return
    if mentions and command.challenge_view == "usage_refresh":
        await session.send("刷新只能对统计范围做，@群友 仅用于查看。")
        return
    subject_label = "个人统计" if mentions else ""
    if scope == "group" and active_bot is None:
        try:
            active_bot = get_bot()
        except RuntimeError:
            await session.send("群成员列表获取失败，请稍后再试。")
            return

    if command.challenge_view == "usage_refresh":
        if scope == "global":
            if not _is_endfield_superuser(user_id):
                await session.send(f"权限不足：仅机器人管理员可刷新全局{family_label}使用率快照。")
                return
        else:
            guild_id = get_group_id(event)
            if not _is_endfield_superuser(user_id) and not await is_group_manager(
                active_bot, event, guild_id, user_id
            ):
                await session.send(
                    f"权限不足：仅群主、群管理员或机器人管理员可刷新本群{family_label}使用率快照。"
                )
                return

    if scope == "group":
        try:
            member_ids = await collect_group_member_ids(active_bot, get_group_id(event))
        except GroupMemberListError as exc:
            logger.warning(
                "[endfield-usage] group member listing failed "
                f"standard_error_type={exc.standard_error_type}"
            )
            await session.send("获取本群成员列表失败，已终止统计任务。")
            return
        roles = account_store.list_all_roles(member_ids)
    else:
        roles = account_store.list_all_roles()
    if mentions:
        # @群友:只看这个人绑定的角色，其余人不下场。
        roles = account_store.list_all_roles(list(mentions))
        if not roles:
            await session.send("该群友暂未绑定终末地账号，无法查询使用率。")
            return
    if not roles:
        await session.send("统计范围内还没有绑定终末地账号的成员。")
        return

    if command.challenge_view == "usage_refresh":
        try:
            cipher = CredentialCipher.from_settings()
            with cold_start_command(session):
                refresh = await usage_stats_service.refresh_roles(
                    roles,
                    cipher,
                    force=True,
                    trigger=f"manual-{scope}",
                )
        except CredentialKeyError as exc:
            await session.send(str(exc))
            return
        await session.send(_format_usage_refresh_result(scope, refresh, family_label))
        return

    try:
        with cold_start_command(session):
            locale = await _usage_challenge_locale()
            if command.challenge_view == "usage_history":
                history = await asyncio.to_thread(
                    usage_stats_service.build_history_report,
                    scope,
                    roles,
                    family,
                    terms=command.challenge_terms,
                    difficulty=command.challenge_difficulty,
                    locale=locale,
                    subject_label=subject_label,
                )
                pngs = await draw_usage_history(history)
            else:
                report = await asyncio.to_thread(
                    usage_stats_service.build_report,
                    scope,
                    roles,
                    family,
                    terms=command.challenge_terms,
                    difficulty=command.challenge_difficulty,
                    locale=locale,
                    subject_label=subject_label,
                )
                if isinstance(report, UsageStageReport):
                    pngs = await draw_usage_stage(report)
                else:
                    pngs = await draw_usage_overview(report)
    except UsageResolutionError as exc:
        await session.send(str(exc))
        return
    except UsageUnavailableError:
        family_cmd = "影拓" if family == USAGE_FAMILY_MONUMENT else "回响"
        started = usage_stats_service.request_background_refresh(
            roles, trigger=f"auto-{scope}"
        )
        if started:
            await session.send(
                f"统计范围内还没有{family_label}挑战记录，已开始后台抓取"
                f"（每批 {usage_stats_service.batch_size} 个角色，约 1 分钟）。"
                f"稍后再发一次 /ef {family_cmd} 使用率；群管理也可以发 "
                f"/ef 使用率 刷新 立刻补一批。"
            )
            return
        await session.send(
            f"统计范围内还没有{family_label}挑战记录，请先让群友发一次 /ef {family_cmd}，"
            f"或由群管理发送 /ef 使用率 刷新。"
        )
        return
    except Exception as exc:
        logger.exception(f"[endfield-usage] report failed: {exc}")
        await session.send("使用率统计失败，请稍后再试。")
        return
    return await _finish_pngs(session, pngs)


def _format_usage_refresh_result(
    scope: str, refresh: UsageStatsRefreshResult, family_label: str
) -> str:
    scope_label = "全局" if scope == "global" else "本群"
    lines = [
        f"{family_label}使用率快照已刷新（{scope_label}）：",
        f"成功 {refresh.succeeded} 个角色 · 失败 {refresh.failed} 个 · 跳过 {refresh.skipped} 个",
    ]
    if refresh.deferred:
        lines.append(f"另有 {refresh.deferred} 个任务留到下一批。")
    if refresh.stopped_early and refresh.stop_reason:
        lines.append(f"已提前结束：{refresh.stop_reason}")
    for issue in refresh.issues[:3]:
        lines.append(f"· {issue.label}：{issue.count} 次")
    lines.append("发送 /ef 影拓 使用率 或 /ef 回响 使用率 查看统计卡。")
    return "\n".join(lines)


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
    session, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    """F2：查询绑定账号未获得/未升满/未镀层的蚀刻章。"""
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    await collection_refresher.ensure_fresh()
    snapshot = medal_store.load_current_view()
    if snapshot is None:
        await session.send("本地暂无蚀刻章数据快照，请稍后查询，或发送 /ef 奖章 刷新 重试。")
        return
    try:
        async with ROLE_TASKS.claim(role):
            token = account_store.decrypt_token(role, cipher)
            raw_progress = await official_client.endfield_card_detail(token, role)
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-medal] player progress API failed: {exc}")
        await session.send("奖章进度查询失败，请稍后再试。")
        return
    except CredentialKeyError as exc:
        await session.send(str(exc))
        return
    except Exception as exc:
        logger.exception(f"[endfield-medal] progress query failed: {exc}")
        await session.send("奖章进度查询异常，请稍后再试。")
        return
    view = service.build_medal_missing_view(
        raw_progress, snapshot,
        nickname=role.nickname, uid=role.masked_uid, server_name=server_label(role.server_name or role.server_id),
    )
    try:
        pngs = await draw_medal_missing_card(view)
    except Exception as exc:
        logger.exception(f"[endfield-medal] missing card failed: {exc}")
        await session.send("缺章统计图生成失败，请稍后再试。")
        return
    return await _finish_pngs(session, pngs)


async def _handle_archive_progress(
    session, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    """查询绑定账号的档案收集进度。森空岛只给 docNum 总数、无逐条明细，仅展示已获得/总数。"""
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    await collection_refresher.ensure_fresh()
    snapshot = archive_store.load_current_view()
    if snapshot is None:
        await session.send("本地暂无档案库数据快照，请稍后查询，或发送 /ef 档案 刷新 重试。")
        return
    try:
        async with ROLE_TASKS.claim(role):
            token = account_store.decrypt_token(role, cipher)
            raw_detail = await official_client.endfield_card_detail(token, role)
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-archive] player progress API failed: {exc}")
        await session.send("档案进度查询失败，请稍后再试。")
        return
    except CredentialKeyError as exc:
        await session.send(str(exc))
        return
    except Exception as exc:
        logger.exception(f"[endfield-archive] progress query failed: {exc}")
        await session.send("档案进度查询异常，请稍后再试。")
        return
    view = service.build_archive_progress_view(
        raw_detail, snapshot,
        nickname=role.nickname, uid=role.masked_uid, server_name=server_label(role.server_name or role.server_id),
    )
    try:
        pngs = await draw_archive_progress_card(view)
    except Exception as exc:
        logger.exception(f"[endfield-archive] progress card failed: {exc}")
        await session.send("档案进度图生成失败，请稍后再试。")
        return
    return await _finish_pngs(session, pngs)


async def _handle_personal_command(session, event: Event, command: ParsedEndfieldCommand, bot=None) -> None:
    private_only = {"bind", "primary", "unbind", "gacha_import"}
    if command.action in private_only and is_group(event):
        await session.send("该功能涉及个人隐私凭证或手机号，请在私聊中发起。")
        return
    qq_user_id = str(event_user_id(event))

    try:
        if command.action == "bind":
            cipher = CredentialCipher.from_settings()
            # The [message] log hides codes and tokens this user sends until the dialog ends.
            with sensitive_input(qq_user_id):
                return await _handle_binding(session, qq_user_id, cipher)
        if command.action == "challenge":
            cipher = CredentialCipher.from_settings()
            return await _handle_challenge(session, event, qq_user_id, command, cipher, bot=bot)
        if command.action == "accounts":
            cipher = CredentialCipher.from_settings()
            return await _handle_accounts(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "account_base":
            cipher = CredentialCipher.from_settings()
            return await _handle_account_base(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "account_investment":
            cipher = CredentialCipher.from_settings()
            return await _handle_account_investment(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "currency_log":
            cipher = CredentialCipher.from_settings()
            return await _handle_account_currency(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "primary":
            role = account_store.set_primary(qq_user_id, command.account_selector)
            await session.send(
                f"已将 {role.nickname}（{role.role_id}）设为默认主账号。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
            )
            return
        if command.action == "unbind":
            role = account_store.unbind(qq_user_id, command.account_selector)
            if role is not None:
                await _forget_card_detail(role)
            await session.send(
                f"已解除绑定角色：{role.nickname}（{role.role_id}）。" if role else "未找到指定账号，发送 /ef 账号 可查看有效编号。"
            )
            return
        if command.action == "attendance":
            cipher = CredentialCipher.from_settings()
            return await _handle_attendance(session, event, qq_user_id, command, cipher, bot=bot)
        if command.action == "daily":
            cipher = CredentialCipher.from_settings()
            return await _handle_daily(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "exploration":
            cipher = CredentialCipher.from_settings()
            return await _handle_exploration(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "medal_missing":
            cipher = CredentialCipher.from_settings()
            return await _handle_medal_missing(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action == "archive_progress":
            cipher = CredentialCipher.from_settings()
            return await _handle_archive_progress(session, qq_user_id, command, cipher, group=is_group(event))
        if command.action in {"gacha", "gacha_sync"}:
            cipher = CredentialCipher.from_settings()
            return await _handle_gacha(
                session, qq_user_id, command, cipher, group=is_group(event), event=event, bot=bot,
            )
        if command.action == "gacha_import":
            with sensitive_input(qq_user_id):
                return await _handle_xhh_import(session, qq_user_id, command)
        if command.action == "gacha_history":
            return await _handle_gacha_history(session, qq_user_id, command, group=is_group(event))
    except TaskAlreadyRunning:
        await session.send("当前任务正在处理中，请稍候。")
        return
    except CredentialKeyError as exc:
        await session.send(str(exc))
        return
    except EndfieldAPIError as exc:
        logger.warning(f"[endfield-account] official API request failed: operation={exc.operation} code={exc.code}")
        await session.send(str(exc))
        return
    except InvestmentDataUnavailable as exc:
        logger.warning(f"[endfield-investment] AKEData unavailable: {exc}")
        await session.send("角色养成数据源暂时响应异常，请稍后再试。")
        return
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
            await session.send(f"{exc}\n参考格式：/ef {example}")
            return
        await session.send(str(exc))
        return
    except ChallengeResolutionError as exc:
        await session.send(str(exc))
        return
    except XhhAPIError as exc:
        await session.send(str(exc))
        return
    except aiohttp.ClientConnectionError:
        logger.warning(f"[endfield-account] message connection interrupted: action={command.action}")
        await session.send("网络连接中断，请重新发送指令。")
        return
    except ExitState:
        raise
    except Exception as exc:
        logger.opt(exception=exc).error(
            f"[endfield-account] action failed: action={command.action} "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        await session.send("账号服务暂时响应异常，请稍后再试。")
        return


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
    session,
    event: Event,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    bot=None,
) -> None:
    """Render one personal challenge query without prompting in group chats."""
    if command.challenge_view in {"usage", "usage_history", "usage_refresh"}:
        return await _handle_usage_stats(session, event, command, bot=bot)
    mentioned_users = _challenge_mention_targets(event, bot)
    if mentioned_users and not is_group(event):
        await session.send("通过 @群友 查询他人挑战数据仅限在群聊中使用。")
        return
    if len(mentioned_users) > 1:
        await session.send("单次查询仅支持 @ 一位群友。")
        return

    target_user_id = mentioned_users[0] if mentioned_users else qq_user_id
    roles = account_store.list_roles(target_user_id)
    if not roles:
        if mentioned_users:
            await session.send("所选群友暂未绑定终末地账号。")
            return
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
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
            await session.send("\n".join(lines))
            return
        else:
            await session.send("未找到指定账号，发送 /ef 账号 可查看有效编号。")
            return

    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        await session.send("影拓丰碑与战争回响目前仅支持国服角色，亚服暂未接入。")
        return

    group_chat = is_group(event)
    identity = ChallengeIdentity(
        nickname=role.nickname,
        server_name=server_label(role.server_name or role.server_id),
        uid=role.masked_uid if group_chat else role.role_id,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    variant = "b"
    with cold_start_command(session):
        await notice_default_ake_public()
        return await _render_challenge_cards(
            session,
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
    session,
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
            caches._ASSET_GENERATION,
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
        await _piggyback_usage_snapshot(role, USAGE_FAMILY_MONUMENT, raw, locale)
        if command.challenge_view == "overview":
            pngs = await cached_pages(
                "monument", "overview", "", "", 0,
                lambda: _one_page(
                    draw_challenge_empty(identity, "影拓丰碑", variant=variant)
                    if not payload.has_records
                    else draw_monument_overview(identity, payload, variant=variant)
                ),
            )
            return await _finish_pngs(session, pngs)
        pages = payload.history_pages() if payload.has_records else ()
        if command.challenge_view == "history":
            if not pages:
                pngs = await cached_pages(
                    "monument", "history", "all", "", 0,
                    lambda: _one_page(draw_challenge_empty(identity, "影拓丰碑", query="暂无历史主题", variant=variant)),
                )
                return await _finish_pngs(session, pngs)
            if command.all_history:
                rendered = await cached_pages(
                    "monument", "history", "all", "", 0,
                    lambda: draw_monument_history_pages(identity, pages, variant=variant),
                )
                return await _finish_challenge_pages(session, event, bot, rendered, "/ef 影拓 历史 第N页")
            if command.page > len(pages):
                await session.send(f"影拓历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
                return
            rendered = await cached_pages(
                "monument", "history", "", "", command.page,
                lambda: _one_page(draw_monument_history(identity, pages[command.page - 1], page=command.page, page_count=len(pages), variant=variant)),
            )
            return await _finish_pngs(session, rendered)
        group, dungeon = resolve_monument_detail(payload, command.challenge_terms, command.challenge_difficulty or "hard")
        rendered = await cached_pages(
            "monument", "detail", " ".join(command.challenge_terms), command.challenge_difficulty or "hard", 0,
            lambda: _one_page(draw_monument_detail(identity, group, dungeon, variant=variant)),
        )
        return await _finish_pngs(session, rendered)

    raw, locale = await asyncio.gather(load_data("war_echo"), load_locale())
    payload = parse_war_echoes(raw, locale)
    await _piggyback_usage_snapshot(role, USAGE_FAMILY_WAR_ECHO, raw, locale)
    if command.challenge_view == "detail":
        # 只点到赛季/轮换（「错视赛季」「错视轮换」）就看赛季卡，不进关卡卡：
        # 赛季名要整个赛季，翻到列出全部轮换的历史页（当期赛季也一样）；
        # 当期正在显示的轮换落回总览，其它轮换翻到所在赛季的历史页。
        season_page = resolve_war_season_page(payload, command.challenge_terms)
        if season_page == 0:
            command = replace(command, challenge_view="overview")
        elif season_page is not None:
            command = replace(command, challenge_view="history", page=season_page, all_history=False)
    if command.challenge_view == "overview":
        pngs = await cached_pages(
            "war_echo", "overview", "", "", 0,
            lambda: _one_page(
                draw_challenge_empty(identity, "战争回响", variant=variant)
                if not payload.has_records else draw_war_overview(identity, payload, variant=variant)
            ),
        )
        return await _finish_pngs(session, pngs)
    pages = payload.history_pages() if payload.has_records else ()
    if command.challenge_view == "history":
        if not pages:
            rendered = await cached_pages(
                "war_echo", "history", "all", "", 0,
                lambda: _one_page(draw_challenge_empty(identity, "战争回响", query="暂无历史赛季", variant=variant)),
            )
            return await _finish_pngs(session, rendered)
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
            return await _finish_challenge_pages(session, event, bot, rendered, "/ef 回响 历史 第N页")
        if command.page > len(pages):
            await session.send(f"战争回响历史记录共 {len(pages)} 页，请输入 1–{len(pages)} 范围内的页码。")
            return
        season = pages[command.page - 1][0]
        rendered = await cached_pages(
            "war_echo", "history", "", "", command.page,
            lambda: _one_page(draw_war_history(identity, season, page=command.page, page_count=len(pages), achievements=payload.achievements, variant=variant)),
        )
        return await _finish_pngs(session, rendered)
    season, week, war_group, dungeon = resolve_war_detail(payload, command.challenge_terms, command.challenge_difficulty or "cruel")
    rendered = await cached_pages(
        "war_echo", "detail", " ".join(command.challenge_terms), command.challenge_difficulty or "cruel", 0,
        lambda: _one_page(draw_war_detail(identity, season, week, war_group, dungeon, variant=variant)),
    )
    return await _finish_pngs(session, rendered)


async def _usage_challenge_locale():
    """使用率卡片要中文关卡名;AKEData 对照取不到时降级用官方原文。"""
    try:
        return await get_challenge_locale(max_wait_seconds=5.0)
    except Exception as exc:
        logger.debug(
            "[endfield-usage] challenge locale unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return None


async def _piggyback_usage_snapshot(role, family: str, raw, locale) -> None:
    """挑战卡已经拿到官方响应,顺手把最佳记录队伍写进使用率快照。

    失败只记 debug:这条路径是搭便车,不能影响用户这次查询。
    """
    try:
        await usage_stats_service.persist_raw(role, family, raw, locale=locale)
    except Exception as exc:
        logger.debug(
            "[endfield-usage] piggyback persist skipped "
            f"family={family} error_type={type(exc).__module__}.{type(exc).__name__}"
        )


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


async def _finish_challenge_pages(session, event, bot, pngs: tuple[bytes, ...], page_hint: str) -> None:
    if len(pngs) <= 2:
        return await _finish_pngs(session, pngs)
    try:
        await _send_forward_pngs(session, bot, pngs)
    except ExitState:
        raise
    except Exception as exc:
        logger.warning(f"[endfield-challenge] merged forward unavailable: {type(exc).__name__}")
        await session.send(f"历史记录共 {len(pngs)} 页，当前环境暂不支持合并转发，请通过 {page_hint} 分页浏览。")
        return


# 暂时注释二维码绑定实现；恢复时一并取消本段注释。
# async def _bind_cn_qr_account(session) -> str | None:
#     ticket = await official_client.create_qr_login()
#     qr_png = _render_qr_png(ticket.scan_url)
#     await session.send(
#         "请使用森空岛或《明日方舟：终末地》App 扫描下方二维码并确认登录。"
#         "二维码有效期较短，请勿转发给他人。"
#     )
#     await session.send(MessageChain([Image.of(raw=qr_png)]))
#
#     loop = asyncio.get_running_loop()
#     deadline = loop.time() + 150
#     scan_notice_sent = False
#     while loop.time() < deadline:
#         status = await official_client.check_qr_login(ticket.scan_id)
#         if status.state == "confirmed":
#             return await official_client.token_by_scan_code(status.scan_code)
#         if status.state == "scanned" and not scan_notice_sent:
#             await session.send("扫码成功，请在手机上确认登录。")
#             scan_notice_sent = True
#         if status.state == "expired":
#             await session.send("二维码已过期，请重新执行绑定命令。")
#             return None
#         await asyncio.sleep(min(2, max(0, deadline - loop.time())))
#
#     await session.send("等待扫码确认超时，请重新执行绑定命令。")
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


async def _handle_attendance(
    session,
    event: Event,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    bot=None,
) -> None:
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    view = await sign_attendance_roles(account_store, official_client, cipher, roles)
    outcomes = [
        signed_outcome(
            "endfield", png=await _attendance_png(view), text=format_attendance_report(view)
        )
    ]
    # The selector only chose Endfield roles.  A bound Arknights account is
    # signed in full below, unless this group switched Arknights off.
    scope = scope_from_event(bot, event)
    outcomes.extend(
        await collect_companion_outcomes(
            qq_user_id,
            current="endfield",
            group=is_group(event),
            enabled=lambda feature: feature_store.is_enabled(scope, feature),
        )
    )
    await deliver(session.send, await build_delivery(outcomes))


async def _attendance_png(view: AttendanceCardView) -> bytes | None:
    """Render the card, or return ``None`` so the caller sends full text."""
    try:
        return await draw_attendance_card(view)
    except ExitState:
        raise
    except Exception as exc:  # noqa: BLE001 - the text result must still be delivered
        logger.warning(
            f"[endfield-account] attendance card render failed error_type={type(exc).__name__}"
        )
        return None


# ------------------------------------- attendance started by /ak 签到


def _companion_roles(user_id: str) -> list[EndfieldRole]:
    """Every role this user bound, without touching credentials."""
    return account_store.list_roles(user_id)


async def _companion_attendance(user_id: str, *, group: bool) -> AttendanceResult:
    """Sign every bound Endfield role after ``/ak 签到`` signed Arknights.

    ``group`` is part of the capability contract; this game's attendance card
    always carries masked UIDs, so there is nothing to hide or reveal here.
    """
    roles = account_store.list_roles(user_id)
    if not roles:
        return AttendanceResult(ok=False, text="尚未绑定终末地账号，请私聊使用 /zmd 绑定。")
    try:
        cipher = CredentialCipher.from_settings()
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
        roles=_companion_roles,
        sign=_companion_attendance,
    )
)


async def _finish_endfield_help(session) -> None:
    # 按页面比例随机挑插画并运行时渲染；渲染不可用时回退到仓库里的静态帮助图。
    image = await cached_help_image("endfield") or ENDFIELD_HELP_IMAGE_PATH
    if image and Path(image).is_file():
        await session.send(MessageChain([Image.of(path=Path(image))]))
        return
    await session.send(format_help())


def _rest(match: Match) -> str:
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
_asset_hotset_task: asyncio.Task | None = None
# 启动后先让 manifest 与挑战文本预热跑一会儿，再在后台预取热集。
ASSET_HOTSET_DELAY_SECONDS = 5.0

announcement_runtime = AnnouncementRuntime()


@on_ready
async def _start_announcement_reminders(_bot=None) -> None:
    announcement_runtime.ready(_bot)


async def _poll_announcement_reminders() -> None:
    await announcement_runtime.tick()


timer.add_job(
    _poll_announcement_reminders, "interval", minutes=1,
    id="endfield_announcement_reminders", replace_existing=True, max_instances=1,
)


@on_ready
async def _warmup_challenge_i18n(_bot=None) -> None:
    start_challenge_locale_warmup()


async def _prefetch_asset_hotset() -> None:
    await asyncio.sleep(ASSET_HOTSET_DELAY_SECONDS)
    try:
        await asset_broker.prefetch_hotset()
    except Exception as exc:
        logger.warning(f"[endfield] asset hot set prefetch unavailable error_type={type(exc).__name__}")


@on_ready
async def _warmup_asset_hotset(_bot=None) -> None:
    """Re-warm the disk with images queries used in the last 24 h (only stale or missing ones)."""
    global _asset_hotset_task
    if _asset_hotset_task is None or _asset_hotset_task.done():
        _asset_hotset_task = asyncio.create_task(_prefetch_asset_hotset())


async def _refresh_due_ownership_snapshots() -> None:
    try:
        cipher = CredentialCipher.from_settings()
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


async def _refresh_due_usage_snapshots() -> None:
    try:
        cipher = CredentialCipher.from_settings()
        await usage_stats_service.refresh_due(cipher)
    except Exception as exc:
        logger.warning(
            "[endfield-usage] scheduled refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )


timer.add_job(
    _refresh_due_usage_snapshots,
    "interval",
    minutes=10,
    id="endfield_usage_refresh",
    replace_existing=True,
    max_instances=1,
)


@listen(Cleanup)
async def _close_ownership_startup_task() -> None:
    await announcement_runtime.close()
    startup_tasks = [
        task for task in (_ownership_startup_task, _asset_hotset_task, *_background_tasks)
        if task is not None and not task.done()
    ]
    for task in startup_tasks:
        task.cancel()
    await asyncio.gather(*startup_tasks, return_exceptions=True)
    await close_challenge_locale()
    await asyncio.gather(*(
        cache.close() for cache in (
            _CARD_CACHE, _LOADOUT_CACHE, _CALENDAR_CACHE,
            _CHALLENGE_DATA_CACHE, _CHALLENGE_RENDER_CACHE, service._weapon_relations, service._ake_views,
            _ACCOUNT_PAGE_CACHE, _CARD_DETAIL_CACHE,
        )
    ))
    await official_client.close()
