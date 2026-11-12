"""终末地 /ef 档案（统计、收集进度）的实现。"""

from __future__ import annotations

from loguru import logger

from ..account.client import EndfieldAPIError
from ..account.crypto import CredentialCipher, CredentialKeyError
from ..account.i18n import server_label
from ..catalog.commands import ParsedEndfieldCommand
from ..catalog.refresh import SnapshotPersistenceError
from ..cold_start import cold_start_command
from ..gacha.service import ROLE_TASKS
from ..rendering.cards import draw_archive_progress_card, draw_archive_stats_card
from ..replies import _finish_pngs
from ..state import account_store, archive_store, collection_refresher, official_client, service


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
