"""终末地个人账号查询的实现：账号列表与详情、基建、养成、资源流水、探索、日常，以及 card/detail 的短时复用。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, replace
from time import perf_counter

from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.infrastructure.http.asset_policy import asset_render_budget
from otae_bot.messaging.prompts import prompt_silently

from .. import caches
from ..caches import _ACCOUNT_PAGE_CACHE, _CARD_DETAIL_CACHE, CARD_RENDER_VERSION
from ..catalog.commands import parse_candidate_selection, ParsedEndfieldCommand
from ..catalog.models import DailyAccountView, DailyDashboardView
from ..cold_start import cold_start_command, notice_default_ake_public
from ..gacha.service import format_timestamp, ROLE_TASKS, TaskAlreadyRunning
from ..rendering.cards import draw_daily_dashboard_card
from ..rendering.health import track_render_health
from ..replies import _finish_png, _finish_pngs, _IncompletePages
from ..state import account_store, official_client, ownership_stats_service
from .base.draw import draw_account_base_card
from .base.service import build_account_base_view
from .challenge.i18n import fetch_challenge_locale
from .client import (
    account_credential_fingerprint,
    ACCOUNT_PROVIDER_SKPORT,
    CURRENCY_TYPES,
    decode_account_credential,
    EndfieldAPIError,
    is_asia_role,
)
from .crypto import CredentialCipher, CredentialKeyError
from .currency.draw import draw_currency_log_cards
from .currency.service import (
    aggregate_currency_logs,
    date_bounds as currency_date_bounds,
    earliest_currency_log_date,
    format_all_history_period_label,
    format_currency_log_report,
    resolve_query_dates,
    split_report,
)
from .detail.draw import draw_account_detail_cards
from .detail.names import fetch_account_detail_name_map
from .detail.service import build_account_detail_view, build_daily_account_view
from .exploration.draw import draw_exploration_cards
from .exploration.service import build_exploration_view
from .exploration.version import fetch_exploration_version
from .i18n import server_label
from .investment.draw import draw_account_investment_cards
from .investment.service import build_account_investment_view, fetch_account_investment_catalog
from .prefetch import prefetch_detail_assets
from .store import EndfieldRole


async def _handle_accounts(
    session, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            await session.send("未找到指定账号，发送 /ef 账号 可查看有效编号。")
            return
        return await _render_account_detail(session, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_detail(session, roles[0], cipher, group=group)

    answer = await prompt_silently(
        session, _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        await session.send("已取消账号查询。")
        return
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        await session.send(f"序号输入有误，请输入 1–{len(roles)}。")
        return
    return await _render_account_detail(session, role, cipher, group=group)


_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coroutine) -> None:
    """Run work the reply does not wait for; cancelled on Cleanup."""
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _persist_detail_quietly(role: EndfieldRole, detail: dict) -> None:
    try:
        await ownership_stats_service.persist_detail(role, detail)
    except Exception:
        # Opportunistic writes must not affect the account-detail response;
        # refresh batches report aggregate failures without logging identities.
        pass


async def _fetch_card_detail(token: str, role: EndfieldRole) -> tuple[dict, bool]:
    """card/detail through the 60 s cache; True when no new request was made.

    Concurrent queries for one role share a single request. The role lock is
    taken only when the API is actually called, so a hit never blocks on it.
    """
    key = (account_credential_fingerprint(token), str(role.role_id), str(role.server_id))

    async def request() -> dict:
        async with ROLE_TASKS.claim(role):
            return await official_client.card_detail(token, role)

    return await _CARD_DETAIL_CACHE.get_or_create_with_status(key, request)


async def _forget_card_detail(role: EndfieldRole) -> int:
    target = (str(role.role_id), str(role.server_id))
    return await _CARD_DETAIL_CACHE.clear(lambda key: key[1:] == target)


async def _card_detail_with_snapshot(token: str, role: EndfieldRole) -> dict:
    detail, reused = await _fetch_card_detail(token, role)
    if reused:
        # 同一份 detail 的快照与预取在第一次取到时已经做过。
        return detail
    # 持有率快照在后台写，不挡视图构建；素材 URL 此刻已知，先在后台把图预取到磁盘，
    # 与名称表加载、视图构建重叠。
    _spawn_background(_persist_detail_quietly(role, detail))
    prefetch_detail_assets(detail, source="card_detail")
    return detail


async def _render_account_detail(
    session, role: EndfieldRole, cipher: CredentialCipher, *, group: bool
) -> None:
    with cold_start_command(session):
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

        data_started = perf_counter()
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
        pages = await _render_account_pages(
            "detail", role, group, view, lambda: draw_account_detail_cards(view),
            data_seconds=perf_counter() - data_started,
        )
        return await _finish_pngs(session, pages)


async def _handle_account_investment(
    session,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            await session.send("未找到指定账号，发送 /ef 账号 可查看有效编号。")
            return
        return await _render_account_investment(session, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_investment(session, roles[0], cipher, group=group)

    answer = await prompt_silently(
        session, _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        await session.send("已取消账号养成统计查询。")
        return
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        await session.send(f"序号输入有误，请输入 1–{len(roles)}。")
        return
    return await _render_account_investment(session, role, cipher, group=group)


async def _render_account_investment(
    session,
    role: EndfieldRole,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        await session.send("养成统计功能目前仅支持国服角色，亚服暂未接入。")
        return

    with cold_start_command(session):
        await notice_default_ake_public()
        async def load_name_map():
            try:
                return await fetch_account_detail_name_map()
            except Exception as exc:
                logger.warning(f"[endfield] investment AKE name map unavailable: {exc}")
                return None

        data_started = perf_counter()
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
        pages = await _render_account_pages(
            "investment", role, group, view, lambda: draw_account_investment_cards(view),
            data_seconds=perf_counter() - data_started,
        )
        return await _finish_pngs(session, pages)


async def _handle_account_currency(
    session,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            await session.send("未找到指定账号，发送 /ef 账号 可查看有效编号。")
            return
        return await _render_account_currency(session, role, command, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_currency(session, roles[0], command, cipher, group=group)

    answer = await prompt_silently(
        session, _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        await session.send("已取消资源流水查询。")
        return
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        await session.send(f"序号输入有误，请输入 1–{len(roles)}。")
        return
    return await _render_account_currency(session, role, command, cipher, group=group)


async def _render_account_currency(
    session,
    role: EndfieldRole,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    token = account_store.decrypt_token(role, cipher)
    provider, _raw_token = decode_account_credential(token)
    if provider == ACCOUNT_PROVIDER_SKPORT or is_asia_role(role):
        await session.send("资源流水查询目前仅支持国服角色，亚服暂未接入。")
        return

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
        await session.send(str(exc))
        return

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
        return await _finish_pngs(session, cards)
    except ExitState:
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
            await session.send(chunk)
        await session.send(chunks[-1])
        return


async def _handle_account_base(
    session,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    roles = account_store.list_roles(qq_user_id)
    if not roles:
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return
    if command.account_selector:
        role = account_store.resolve_role(qq_user_id, command.account_selector)
        if role is None:
            await session.send("未找到指定账号，发送 /ef 账号 可查看有效编号。")
            return
        return await _render_account_base(session, role, cipher, group=group)
    if len(roles) == 1:
        return await _render_account_base(session, roles[0], cipher, group=group)

    answer = await prompt_silently(
        session, _format_accounts(roles, reveal_uid=not group, detail_hint=True), timeout=60
    )
    if answer is None:
        return
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        await session.send("已取消账号查询。")
        return
    selection = parse_candidate_selection(text, len(roles))
    role = roles[selection] if selection is not None else account_store.resolve_role(qq_user_id, text)
    if role is None:
        await session.send(f"序号输入有误，请输入 1–{len(roles)}。")
        return
    return await _render_account_base(session, role, cipher, group=group)


async def _render_account_base(
    session,
    role: EndfieldRole,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    with cold_start_command(session):
        token = account_store.decrypt_token(role, cipher)
        await notice_default_ake_public()

        async def load_name_map():
            try:
                return await fetch_account_detail_name_map()
            except Exception as exc:
                logger.warning(f"[endfield] account AKE name map unavailable: {exc}")
                return None

        data_started = perf_counter()
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
        data_seconds = perf_counter() - data_started

        async def render():
            return (await draw_account_base_card(view),)

        return await _finish_pngs(
            session,
            await _render_account_pages("base", role, group, view, render, data_seconds=data_seconds),
        )


async def _handle_exploration(
    session, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    # Resolve only the caller's roles. An empty selector uses their primary account.
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        if command.account_selector:
            await session.send("未找到对应账号，请发送 /ef 账号 查看编号，再使用 /ef 探索 <编号>。")
            return
        await session.send("尚未绑定终末地账号，请先私聊发送 /ef 绑定。")
        return
    with cold_start_command(session):
        token = account_store.decrypt_token(role, cipher)
        # A cold version lookup can take a while; overlap it with the account fetch.
        data_started = perf_counter()
        detail, game_version = await asyncio.gather(
            _card_detail_with_snapshot(token, role), fetch_exploration_version()
        )
        view = build_exploration_view(
            detail,
            uid=role.masked_uid if group else role.role_id,
            nickname=role.nickname,
            server_name=role.server_name or role.server_id,
            version=game_version,
        )
        data_seconds = perf_counter() - data_started

        async def render():
            return await draw_exploration_cards(view)

        return await _finish_pngs(
            session,
            await _render_account_pages(
                "exploration", role, group, view, render, data_seconds=data_seconds
            ),
        )


async def _render_account_pages(
    kind: str, role, group: bool, view, render, *, data_seconds: float = 0.0
) -> tuple[bytes, ...]:
    """Cache only rendering after current data and authorization were resolved.

    ``data_seconds`` is the caller's API/table time, logged next to the asset
    and drawing time of this render.
    """
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
        str(caches._ASSET_GENERATION),
        kind,
        str(role.role_id),
        str(role.server_id),
        "group" if group else "private",
        digest,
    )

    asset_seconds = 0.0
    network = (0, 0, 0)

    async def create_pages():
        nonlocal asset_seconds, network
        # 整张卡共用一个素材预算；到点未取到的素材留空，页面照常出图（不进缓存）。
        with track_render_health() as health, asset_render_budget():
            try:
                pages = tuple(await render())
            finally:
                asset_seconds = health.asset_seconds
                network = (health.network, health.revalidated, health.refill)
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
    seconds = perf_counter() - started
    logger.info(
        f"[endfield] account-render kind={kind} reuse={hit} data={data_seconds:.3f} "
        f"assets={asset_seconds:.3f} draw={max(0.0, seconds - asset_seconds):.3f} "
        f"seconds={seconds:.3f} pages={len(pages)} "
        f"net={network[0]} net_304={network[1]} refill={network[2]}"
    )
    return pages


async def _handle_daily(
    session,
    qq_user_id: str,
    command: ParsedEndfieldCommand,
    cipher: CredentialCipher,
    *,
    group: bool,
) -> None:
    """日常仪表盘：逐账号读取森空岛 card/detail，展示理智/活跃度/每周事务/通行证。"""
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        await session.send("未找到对应的终末地账号，请先私聊发送 /ef 绑定 进行添加。")
        return

    async def load(role: EndfieldRole) -> DailyAccountView:
        try:
            token = account_store.decrypt_token(role, cipher)
            # card_detail 返回已解包的 data.detail；endfield_card_detail 是完整响应，
            # 仅供奖章/档案等自行解包的视图使用。与账号查询共用 60 秒缓存，没取新数据就不再预取。
            detail, reused = await _fetch_card_detail(token, role)
            if not reused:
                prefetch_detail_assets(detail, source="daily")
            return build_daily_account_view(
                detail,
                nickname=role.nickname, uid=role.masked_uid,
                server_name=server_label(role.server_name or role.server_id),
            )
        except TaskAlreadyRunning:
            return DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message="当前任务正在处理中")
        except EndfieldAPIError as exc:
            return DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message=str(exc))
        except CredentialKeyError as exc:
            return DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message=str(exc))
        except Exception as exc:
            logger.error(
                f"[endfield-account] daily dashboard failed: stored_role={role.id} error_type={type(exc).__name__}"
            )
            return DailyAccountView(
                role.nickname, role.masked_uid, role.server_name, status="failed", message="数据获取失败，请稍后再试")

    # 各账号并行取（每个角色仍各自 claim）；gather 保持绑定顺序。
    accounts = list(await asyncio.gather(*(load(role) for role in roles)))
    png = await draw_daily_dashboard_card(
        DailyDashboardView(accounts, format_timestamp(int(__import__("time").time())))
    )
    return await _finish_png(session, png)


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
