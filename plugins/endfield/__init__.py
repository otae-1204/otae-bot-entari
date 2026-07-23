from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Event
from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from nepattern import AnyString

from configs.config import Config
from utils.async_cache import AsyncTTLCache, CacheStats
from utils.entari_native import ArgVal, ChainMsg, event_user_id, is_group, make_image, on_alconna, prompt
from utils.http_client import clear_http_cache, get_http_cache_stats
from utils.temp_files import schedule_temp_file_cleanup

from .client import WarfarinAPIError, WarfarinClient
from .account_client import AttendanceResult, EndfieldAPIError, EndfieldOfficialClient
from .account_crypto import CredentialCipher, CredentialKeyError
from .account_store import EndfieldRole, EndfieldStore, RoleCandidate
from .aliases import add_alias, alias_targets
from .commands import (
    EndfieldCandidate,
    CANDIDATE_SCORE_THRESHOLD,
    ParsedEndfieldCommand,
    ParsedLoadoutSpec,
    ROOT_ALIASES,
    choose_candidate,
    dev_visible_for_user,
    format_candidates,
    format_error,
    format_help,
    format_not_found,
    format_source,
    format_unknown,
    normalize_alias_kind,
    parse_command,
    parse_loadout_spec,
    parse_shortcut_command,
    score_candidate,
    score_entity_candidate,
)
from .draw import (
    draw_equipment_card,
    draw_equipment_catalog_card,
    draw_loadout_card,
    draw_operator_card,
    draw_operator_catalog_card,
    draw_weapon_card,
    draw_weapon_catalog_card,
    draw_attendance_card,
    draw_gacha_analysis_cards,
    draw_gacha_history_card,
)
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
from .sources import source_label, source_order


client = WarfarinClient()
service = EndfieldService(client)
gacha_asset_cache = EndfieldGachaAssetCache(service)
account_store = EndfieldStore()
official_client = EndfieldOfficialClient()
ENDFIELD_HELP_IMAGE_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "image" / "help" / "endfield.png"
)
CARD_CACHE_TTL_SECONDS = 600.0
CARD_CACHE_MAX_BYTES = 48 * 1024 * 1024
CARD_RENDER_VERSION = "endfield-card-v26"
CardCacheKey = tuple[str, str, str, str]
_CARD_CACHE: AsyncTTLCache[CardCacheKey, bytes] = AsyncTTLCache(
    ttl_seconds=CARD_CACHE_TTL_SECONDS,
    max_bytes=CARD_CACHE_MAX_BYTES,
    max_entries=64,
    sizeof=len,
)

Resolver = Callable[..., Awaitable[list[EndfieldCandidate]]]
Renderer = Callable[[str, str], Awaitable[bytes | None]]


CONTENT_RESOLVERS: dict[str, Resolver] = {
    "operator": lambda query: _resolve_candidates_from_sources("operator", query),
    "weapon": lambda query: _resolve_candidates_from_sources("weapon", query),
    "equipment": lambda query: _resolve_candidates_from_sources("equipment", query),
}

CONTENT_RENDERERS: dict[str, Renderer] = {
    "operator": lambda key, source: _render_operator(key, source),
    "operator_catalog": lambda key, source: _render_operator_catalog(key, source),
    "weapon": lambda key, source: _render_weapon(key, source),
    "weapon_catalog": lambda key, source: _render_weapon_catalog(key, source),
    "equipment": lambda key, source: _render_equipment(key, source),
    "equipment_catalog": lambda key, source: _render_equipment_catalog(key, source),
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
async def handle_endfield(event: Event, rest: ArgVal):
    await _handle_command(endfield_cmd, event, parse_command(_rest(rest)))


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


async def _handle_command(matcher, event: Event, command: ParsedEndfieldCommand) -> None:
    if command.error:
        return await matcher.finish(format_error(command.error))
    if command.action == "help":
        return await _finish_endfield_help(matcher)
    if command.action == "source":
        return await matcher.finish(format_source())
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
    if command.action in {"bind", "accounts", "primary", "unbind", "attendance", "gacha", "gacha_history", "gacha_sync", "gacha_import"}:
        return await _handle_personal_command(matcher, event, command)
    if command.action == "loadout":
        return await _handle_loadout(matcher, command)
    if command.action not in {"query", "search"}:
        return await matcher.finish(format_unknown())
    if not command.query:
        if command.action == "query" and command.scope in {"operator", "weapon", "equipment"}:
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
            return await matcher.finish(format_candidates(ambiguous))
        if selected is None:
            return await matcher.finish(format_not_found(command.scope, command.query))

        render_started = perf_counter()
        png = await _render_candidate(selected, command.source)
        render_seconds = perf_counter() - render_started
        if png is None:
            return await matcher.finish(format_not_found(selected.kind, command.query))
        logger.info(
            f"[endfield] perf action=query scope={command.scope} kind={selected.kind} "
            f"candidate={candidate_seconds:.3f}s render={render_seconds:.3f}s "
            f"total_before_send={perf_counter() - started:.3f}s"
        )
        try:
            return await _finish_png(matcher, png)
        except _ExitException:
            raise
        except Exception as exc:
            logger.exception(f"[endfield] send failed for {selected.kind} {command.query}: {exc}")
            return await matcher.finish("图片发送失败，请稍后重试")
    except _ExitException:
        raise
    except WarfarinAPIError as exc:
        logger.warning(f"[endfield] data API failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("数据源暂时不可用")
    except Exception as exc:
        logger.exception(f"[endfield] card failed for {command.scope} {command.query}: {exc}")
        return await matcher.finish("图片生成失败")


async def _handle_personal_command(matcher, event: Event, command: ParsedEndfieldCommand) -> None:
    private_only = {"bind", "accounts", "primary", "unbind", "gacha_import"}
    if command.action in private_only and is_group(event):
        return await matcher.finish("该命令涉及账号凭据或手机号，仅支持私聊使用。")
    qq_user_id = str(event_user_id(event))

    try:
        if command.action == "bind":
            cipher = CredentialCipher.from_env()
            return await _handle_binding(matcher, qq_user_id, cipher)
        if command.action == "accounts":
            return await matcher.finish(_format_accounts(account_store.list_roles(qq_user_id), reveal_uid=True))
        if command.action == "primary":
            role = account_store.set_primary(qq_user_id, command.account_selector)
            return await matcher.finish(
                f"已将 {role.nickname}（{role.role_id}）设为主账号。" if role else "未找到对应账号，请使用 /zmd 账号 查看编号。"
            )
        if command.action == "unbind":
            role = account_store.unbind(qq_user_id, command.account_selector)
            return await matcher.finish(
                f"已解绑 {role.nickname}（{role.role_id}）。" if role else "未找到对应账号，请使用 /zmd 账号 查看编号。"
            )
        if command.action == "attendance":
            cipher = CredentialCipher.from_env()
            return await _handle_attendance(matcher, qq_user_id, command, cipher, group=is_group(event))
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
    except XhhAPIError as exc:
        return await matcher.finish(str(exc))
    except _ExitException:
        raise
    except Exception as exc:
        logger.error(f"[endfield-account] action failed: action={command.action} error_type={type(exc).__name__}")
        return await matcher.finish("终末地账号功能暂时不可用，请稍后重试。")


async def _handle_binding(matcher, qq_user_id: str, cipher: CredentialCipher) -> None:
    method = await _prompt_text(
        "请选择绑定方式：\n1. Token 绑定\n2. 手机号验证码绑定\n回复 1 或 2；回复“取消”退出。",
        timeout=90,
    )
    if method is None:
        return await matcher.finish("绑定已取消或等待超时。")
    normalized = method.casefold()
    if normalized in {"1", "token", "t"}:
        await matcher.send(
            "请在浏览器登录森空岛后打开：\nhttps://web-api.skland.com/account/info/hg\n"
            "复制响应中 data.content 的完整内容并发送。不要在群聊或其他平台公开该内容。"
        )
        account_token = await _prompt_text("请发送 data.content；回复“取消”退出。", timeout=150)
        if account_token is None:
            return await matcher.finish("绑定已取消或等待超时。")
    elif normalized in {"2", "短信", "手机", "sms"}:
        phone = await _prompt_text("请输入用于鹰角账号登录的手机号；回复“取消”退出。", timeout=90)
        if phone is None:
            return await matcher.finish("绑定已取消或等待超时。")
        if not re.fullmatch(r"1\d{10}", phone):
            return await matcher.finish("手机号格式不正确，绑定已取消。")
        await official_client.send_phone_code(phone)
        code = await _prompt_text("验证码已发送，请输入短信验证码；回复“取消”退出。", timeout=120)
        if code is None:
            return await matcher.finish("绑定已取消或等待超时。")
        if not re.fullmatch(r"\d{4,8}", code):
            return await matcher.finish("验证码格式不正确，绑定已取消。")
        account_token = await official_client.token_by_phone_code(phone, code)
    else:
        return await matcher.finish("未识别绑定方式，绑定已取消。")

    roles = await official_client.discover_roles(account_token)
    if not roles:
        return await matcher.finish("该鹰角账号下未找到终末地角色。")
    selected = await _select_binding_roles(roles)
    if selected is None:
        return await matcher.finish("绑定已取消或等待超时。")
    account_store.bind_roles(qq_user_id, account_token, selected, cipher)
    return await matcher.finish(
        "绑定完成。\n" + "\n".join(
            f"- {role.nickname} · {role.server_name or role.server_id} · UID {role.role_id}" for role in selected
        )
    )


async def _select_binding_roles(roles: list[RoleCandidate]) -> list[RoleCandidate] | None:
    if len(roles) == 1:
        return roles
    lines = ["检测到多个终末地角色，请回复编号、逗号分隔的多个编号，或“全部”："]
    lines.extend(
        f"{index}. {role.nickname} · {role.server_name or role.server_id} · UID {role.role_id}"
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


async def _handle_attendance(
    matcher, qq_user_id: str, command: ParsedEndfieldCommand, cipher: CredentialCipher, *, group: bool
) -> None:
    roles = account_store.resolve_roles(qq_user_id, command.account_selector)
    if not roles:
        return await matcher.finish("未找到对应账号，请先私聊使用 /zmd 绑定。")
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
        return await matcher.finish("未找到对应账号，请先私聊使用 /zmd 绑定。")
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
    keepsake_metadata = await gacha_asset_cache.prepare_keepsakes(pool_rules)
    analysis = gacha_service.analysis(
        role, metadata, pool_rules, xhh_metadata, keepsake_metadata,
    )
    pngs = await draw_gacha_analysis_cards(analysis, uid=role.masked_uid)
    return await _finish_pngs(matcher, pngs)


async def _handle_gacha_history(matcher, qq_user_id: str, command: ParsedEndfieldCommand, *, group: bool) -> None:
    role = account_store.resolve_role(qq_user_id, command.account_selector)
    if role is None:
        return await matcher.finish("未找到对应账号，请先私聊使用 /zmd 绑定。")
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
        server_name=role.server_name, page=command.page, total_pages=total_pages, total=total,
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
        return await matcher.finish("未找到对应账号，请先私聊使用 /zmd 绑定。")
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
        "发送 /zmd 抽卡 查看补齐后的分析卡；逐抽历史页仍只展示官方明细。"
    )


def _attendance_view(role: EndfieldRole, result: AttendanceResult) -> AttendanceRoleView:
    return AttendanceRoleView(
        nickname=role.nickname,
        uid=role.masked_uid,
        server_name=role.server_name,
        status=result.status,
        message=result.message,
        rewards=[AttendanceRewardView(item.name, item.count) for item in result.rewards],
    )


def _format_accounts(roles: list[EndfieldRole], *, reveal_uid: bool) -> str:
    if not roles:
        return "尚未绑定终末地账号。使用 /zmd 绑定 开始绑定。"
    lines = ["已绑定的终末地账号："]
    for index, role in enumerate(roles, 1):
        marker = " [主账号]" if role.is_primary else ""
        uid = role.role_id if reveal_uid else role.masked_uid
        lines.append(f"{index}. {role.nickname}{marker} · {role.server_name or role.server_id} · UID {uid}")
    lines.append("可使用 /zmd 主账号 <编号> 或 /zmd 解绑 <编号> 管理。")
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
            candidates.append(
                EndfieldCandidate(
                    kind="operator",
                    key=title,
                    display_name=name,
                    score=score or 70,
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
    if _looks_like_operator_slug(query) and not alias_targets("operator", query):
        candidates.append(
            EndfieldCandidate(
                kind="operator",
                key=query,
                display_name=query,
                score=94,
                source="warfarin",
                reason="slug",
            )
        )

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") != "operators" or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_entity_candidate("operator", query, name, slug)
        candidates.append(
            EndfieldCandidate(
                kind="operator",
                key=slug,
                display_name=name,
                score=score or 70,
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
    if _looks_like_operator_slug(query) and not alias_targets("weapon", query):
        candidates.append(
            EndfieldCandidate(
                kind="weapon",
                key=query,
                display_name=query,
                score=94,
                source="warfarin",
                reason="slug",
            )
        )

    search_data = await client.search(query)
    for item in search_data.get("results") or []:
        if str(item.get("type") or "") not in {"weapons", "weapon"} or not item.get("slug"):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or slug).strip()
        score = score_entity_candidate("weapon", query, name, slug)
        candidates.append(
            EndfieldCandidate(
                kind="weapon",
                key=slug,
                display_name=name,
                score=score or 70,
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


async def _render_candidate(candidate: EndfieldCandidate, requested_source: str = "") -> bytes | None:
    renderer = CONTENT_RENDERERS.get(candidate.kind)
    if renderer is None:
        return None
    cache_source = requested_source or "auto"
    cache_key = (CARD_RENDER_VERSION, candidate.kind, cache_source, candidate.key)

    async def render() -> bytes:
        output = await renderer(candidate.key, requested_source)
        if output is None:
            raise _CardNotFound
        return output

    try:
        output, cache_hit = await _CARD_CACHE.get_or_create_with_status(cache_key, render)
    except _CardNotFound:
        return None
    logger.info(
        f"[endfield] card-cache kind={candidate.kind} source={cache_source} "
        f"hit={str(cache_hit).lower()} bytes={len(output)}"
    )
    return output


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


async def _finish_png(matcher, png: bytes) -> None:
    return await _finish_pngs(matcher, (png,))


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
            return "用法：/ef dev refresh <all|干员|武器|装备> [关键词]"
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
                return "用法：/ef dev cache clear <all|operator|weapon|equipment|icon>"
            removed = await _clear_endfield_caches(scope)
            return f"已清理 {scope} 缓存，共 {removed} 项。"
        return "\n".join(await _cache_status_lines())
    return "dev 命令：status | resolve | refresh | cache"


def _handle_alias_command(command: ParsedEndfieldCommand) -> str:
    usage = "用法：/zmd 别名 添加 <干员|武器|装备> <正式名称> <新别名>"
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
    elif scope in {"operator", "weapon", "equipment"}:
        cache_kinds = {scope, "equipment_catalog"} if scope == "equipment" else {scope}
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


def _looks_like_operator_slug(query: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", query, flags=re.I) is not None


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
