"""终末地 /ef 影拓、回响等挑战查询的实现（取数、缓存、分页与合并转发）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime

from arclet.entari import Event
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.infrastructure.http.asset_policy import asset_render_budget
from otae_bot.messaging.inbound import is_group

from ... import caches
from ...caches import _CHALLENGE_DATA_CACHE, _CHALLENGE_RENDER_CACHE, CARD_RENDER_VERSION, ChallengeCacheKey
from ...catalog.commands import ParsedEndfieldCommand
from ...cold_start import cold_start_command, notice_default_ake_public
from ...rendering.health import track_render_health
from ...replies import _finish_pngs, _IncompletePages, _one_page, _send_forward_pngs
from ...state import account_store, official_client
from ...usage.handlers import _challenge_mention_targets, _handle_usage_stats, _piggyback_usage_snapshot
from ...usage.service import (
    FAMILY_MONUMENT as USAGE_FAMILY_MONUMENT,
    FAMILY_WAR_ECHO as USAGE_FAMILY_WAR_ECHO,
)
from ..client import ACCOUNT_PROVIDER_SKPORT, decode_account_credential, is_asia_role
from ..crypto import CredentialCipher
from ..i18n import server_label
from ..store import EndfieldRole
from .draw import (
    ChallengeIdentity,
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
from .i18n import ChallengeLocale, get_challenge_locale


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
