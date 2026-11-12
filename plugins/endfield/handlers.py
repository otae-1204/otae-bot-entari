from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import perf_counter

import aiohttp
from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Cleanup, Event, MessageChain, Session, command, listen
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
from otae_bot.accounts.ready import on_ready
from otae_bot.messaging import media
from otae_bot.messaging.inbound import event_user_id, is_group
from otae_bot.messaging.prompts import prompt_silently
from otae_bot.scheduling.jobs import timer
from otae_bot.pipeline.message_log import sensitive_input
from otae_bot.command_usage import describe_command
from otae_bot.infrastructure.rendering.help_runtime import cached_help_image
from otae_bot.infrastructure.http.assets import broker as asset_broker

from .providers.warfarin import WarfarinAPIError
from .announcements.runtime import AnnouncementRuntime
from .account.challenge.i18n import close_challenge_locale
from .account.client import EndfieldAPIError
from .account.crypto import CredentialCipher, CredentialKeyError
from .account.store import EndfieldRole
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
from .rendering.cards import draw_attendance_card
from .account.investment.service import InvestmentDataUnavailable
from .account.challenge.draw import ChallengeAmbiguousError, ChallengeResolutionError
from .account.challenge.i18n import start_challenge_locale_warmup
from .account.challenge.parsing import (
    _difficulty_label as _challenge_difficulty_label,
    _monument_difficulty_label as _challenge_monument_difficulty_label,
)
from .stages.service import StageVariantNotFound
from .stages.fz import StageDataIncomplete
from .gacha.service import TaskAlreadyRunning
from .gacha.xhh import XhhAPIError
from .catalog.models import AttendanceCardView
from .catalog.service import format_status_quick_calc
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
    official_client,
    ownership_stats_service,
    service,
    usage_stats_service,
)
from .caches import (
    _ACCOUNT_PAGE_CACHE,
    _CALENDAR_CACHE,
    _CARD_CACHE,
    _CARD_DETAIL_CACHE,
    _CHALLENGE_DATA_CACHE,
    _CHALLENGE_RENDER_CACHE,
    _LOADOUT_CACHE,
)
from .replies import _finish_png, _finish_pngs
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
from .account.challenge.handlers import _handle_challenge
from .ownership.handlers import _handle_ownership_stats
from .medals.handlers import _handle_medal, _handle_medal_missing
from .archives.handlers import _handle_archive, _handle_archive_progress


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
        await session.send(MessageChain([media.image_file(image)]))
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
