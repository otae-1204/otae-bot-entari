"""终末地 /ef 抽卡、抽卡记录与小黑盒导入的实现，及抽卡分析图的合并转发投递。"""

from __future__ import annotations

import asyncio
import re
from time import perf_counter

from arclet.entari import MessageChain, Session, Text
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.messaging.prompts import prompt

from ..account.crypto import CredentialCipher
from ..account.i18n import server_label
from ..catalog.commands import ParsedEndfieldCommand
from ..catalog.models import GachaHistoryItemView, GachaHistoryView
from ..config import endfield_settings, EndfieldSettings
from ..rendering.cards import draw_gacha_analysis_cards, draw_gacha_history_card
from ..replies import _finish_png, _finish_pngs, _png_image, _send_forward_pngs
from ..state import account_store, gacha_asset_cache, official_client
from .assets import apply_gacha_metadata
from .service import EndfieldGachaService, filter_xhh_import_six_stars, format_timestamp, ROLE_TASKS
from .xhh import XhhLoginSession


# 抽卡分析：超过 N 张图改为一条合并转发（每节点 1 张）；plugins.endfield.gacha_forward_above: 0 关闭转发。
GACHA_FORWARD_ABOVE_DEFAULT = EndfieldSettings().gacha_forward_above
GACHA_FALLBACK_BATCH = 3                # 合并转发不可用时，每条消息最多几张图
FORWARD_MAX_NODES = 50                  # 单条合并转发的节点上限，超出拆成多条
_GACHA_FORWARD_SENDER_NAME = "终末地抽卡分析"


def _gacha_forward_above() -> int:
    """抽卡分析超过多少张图改为合并转发；0 = 关闭合并转发（回滚开关）。"""
    return max(0, endfield_settings().gacha_forward_above)


async def _finish_gacha_pngs(session, event, bot, pngs: tuple[bytes, ...]) -> None:
    """抽卡分析投递：≤ 阈值与其他命令一样一条消息发出；超过阈值发 QQ 合并转发（每节点 1 张）。

    回退链：Satori ``<message forward>`` → 逐张普通图片消息 → 每条 ≤3 张分批发送（逐张发送也失败时）。
    抽卡没有分页命令，所以最后一级是分批而不是文字提示（影拓历史仍按原规则：>2 页转发，失败回文字）。
    """
    threshold = _gacha_forward_above()
    if not threshold or len(pngs) <= threshold:
        return await _finish_pngs(session, pngs)
    started = perf_counter()
    sent = 0
    vias: list[str] = []
    try:
        for start in range(0, len(pngs), FORWARD_MAX_NODES):
            chunk = pngs[start:start + FORWARD_MAX_NODES]
            vias.append(await _send_forward_pngs(
                session, bot, chunk, sender_name=_GACHA_FORWARD_SENDER_NAME, log_tag="endfield-gacha",
            ))
            sent += len(chunk)
    except ExitState:
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
        return
    remaining = pngs[sent:]
    batches = [remaining[index:index + GACHA_FALLBACK_BATCH] for index in range(0, len(remaining), GACHA_FALLBACK_BATCH)]
    notice = (
        f"抽卡分析长图共 {len(pngs)} 页，当前环境无法合并转发，已分 {len(batches)} 条消息发送。" if not sent
        else f"抽卡分析长图共 {len(pngs)} 页，第 {sent + 1}–{len(pngs)} 页转发异常，已分 {len(batches)} 条消息补发。"
    )
    for index, batch in enumerate(batches):
        prefix = [Text(notice)] if index == 0 else []
        await session.send(MessageChain([*prefix, *(_png_image(png) for png in batch)]))
    logger.info(
        f"[endfield-gacha] deliver mode=batches pages={len(pngs)} forwarded={sent} batches={len(batches)} "
        f"elapsed={perf_counter() - started:.2f}s"
    )


async def _handle_gacha(
    session, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool,
    event=None, bot=None,
) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
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
        await session.send(f"{role.nickname} 抽卡记录{mode}同步完毕：新增收录 {result.inserted} 条{failed}{suffix}。")
        return
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
    return await _finish_gacha_pngs(session, event, bot, pngs)


async def _handle_gacha_history(session, qq_user_id: str, command: ParsedEndfieldCommand, *, group: bool) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    total = account_store.count_gacha_records(role, command.pool_filter)
    total_pages = max(1, (total + 19) // 20)
    if command.page > total_pages and total:
        await session.send(f"页码超出有效范围，当前记录共 {total_pages} 页。")
        return
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
    return await _finish_png(session, await draw_gacha_history_card(view))


async def _handle_xhh_import(session, qq_user_id: str, command: ParsedEndfieldCommand) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    phone = await _prompt_text(session, "请输入小黑盒账号绑定的 11 位手机号，如需放弃请回复“取消”。", timeout=90)
    if phone is None:
        await session.send("导入流程已取消或等待超时。")
        return
    if not re.fullmatch(r"1\d{10}", phone):
        await session.send("手机号格式不符合规范，导入已取消。")
        return

    xhh_session: XhhLoginSession | None = None
    try:
        async with ROLE_TASKS.claim(role):
            xhh_session = await XhhLoginSession.start(phone)
            code = await _prompt_text(
                session, "小黑盒短信验证码已发送，请输入验证码，如需放弃请回复“取消”。", timeout=120
            )
            if code is None:
                await session.send("导入流程已取消或等待超时。")
                return
            if not re.fullmatch(r"\d{4,8}", code):
                await session.send("验证码格式输入有误，导入已取消。")
                return
            imported = await xhh_session.login_and_fetch(code)
            if imported.source_uid != role.role_id:
                await session.send(
                    f"小黑盒绑定的终末地 UID 与当前所选账号不一致，请切换对应账号后再试。当前账号 UID：{role.masked_uid}。"
                )
                return
            candidate_names = [item.item_name for item in imported.six_stars]
            xhh_metadata = await gacha_asset_cache.prepare_names(candidate_names)
            unresolved_names = {
                item.item_name
                for item in imported.six_stars
                if "".join(item.item_name.split()).casefold() not in xhh_metadata
            }
            if unresolved_names:
                await session.send(
                    "FZ Wiki 星级数据暂未覆盖本次小黑盒记录，为避免星级误判已终止导入，请稍后再试。"
                )
                return
            imported = filter_xhh_import_six_stars(imported, xhh_metadata)
            account_store.replace_xhh_gacha_import(role, imported)
    finally:
        if xhh_session is not None:
            await xhh_session.close()

    await session.send(
        f"{role.nickname} 小黑盒历史抽卡统计导入成功：涵盖 {len(imported.pools)} 个卡池，"
        f"累计 {imported.total_count} 抽，共包含 {len(imported.six_stars)} 条六星记录。\n"
        "发送 /ef 抽卡 即可查阅合并统计后的分析卡片；逐抽明细页仍以官方接口记录为准。"
    )


async def _prompt_text(session: Session, message: str, *, timeout: int) -> str | None:
    answer = await prompt(session, message, timeout=timeout)
    if answer is None:
        return None
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        return None
    return text
