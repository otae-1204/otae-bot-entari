"""终末地 /ef 持有率统计与刷新的实现。"""

from __future__ import annotations

from arclet.entari import Event, MessageChain
from loguru import logger

from otae_bot.accounts.ready import get_bot
from otae_bot.messaging.inbound import event_user_id, get_group_id, is_group

from ..account.crypto import CredentialCipher, CredentialKeyError
from ..catalog.commands import ParsedEndfieldCommand
from ..cold_start import cold_start_command
from ..replies import _finish_png, _finish_pngs, _is_endfield_superuser
from ..state import account_store, ownership_stats_service
from .service import (
    collect_group_member_ids,
    GroupMemberListError,
    is_group_manager,
    OwnershipRefreshResult,
    OwnershipStatsRendererUnavailable,
    render_ownership_stats,
)


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
