"""终末地 /ef 奖章（蚀刻章统计、缺失查询）的实现。"""

from __future__ import annotations

from loguru import logger

from ..account.client import EndfieldAPIError
from ..account.crypto import CredentialCipher, CredentialKeyError
from ..account.i18n import server_label
from ..catalog.commands import ParsedEndfieldCommand
from ..catalog.refresh import SnapshotPersistenceError
from ..cold_start import cold_start_command
from ..gacha.service import ROLE_TASKS
from ..providers.warfarin import WarfarinAPIError
from ..rendering.cards import draw_medal_missing_card, draw_medal_stats_card
from ..replies import _finish_pngs
from ..state import account_store, collection_refresher, medal_store, official_client, service


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
