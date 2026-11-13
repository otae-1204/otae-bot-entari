"""终末地 /ef 使用统计（影拓等玩法的群内/个人统计）的实现，及挑战查询顺带记录的快照。"""

from __future__ import annotations

import asyncio

from arclet.entari import At, Event
from loguru import logger

from otae_bot.messaging.inbound import event_chain, event_user_id, get_group_id, is_group

from ..account.challenge.i18n import get_challenge_locale
from ..account.crypto import CredentialCipher, CredentialKeyError
from ..catalog.commands import ParsedEndfieldCommand
from ..cold_start import cold_start_command
from ..ownership.service import collect_group_member_ids, GroupMemberListError, is_group_manager
from ..replies import _finish_pngs, _is_endfield_superuser
from ..state import account_store, usage_stats_service
from .draw import draw_usage_history, draw_usage_overview, draw_usage_stage
from .models import FAMILY_LABELS as USAGE_FAMILY_LABELS, UsageStageReport
from .service import (
    FAMILY_MONUMENT as USAGE_FAMILY_MONUMENT,
    FAMILY_WAR_ECHO as USAGE_FAMILY_WAR_ECHO,
    UsageRefreshResult as UsageStatsRefreshResult,
    UsageResolutionError,
    UsageUnavailableError,
)


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
        # The account that got the command is in this group; not whichever logged in last.
        active_bot = session.account

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
