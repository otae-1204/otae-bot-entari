"""Tibo reset radar commands and lifecycle integration."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from arclet.entari import Account as Bot, Cleanup, Event, listen
from loguru import logger

from configs.config import Config as GlobalConfig
from utils.entari_native import (
    ArgVal,
    ChainMsg,
    SendDest,
    Text,
    account_adapter_name,
    cmd as _cmd,
    get_channel_id,
    get_group_id,
    get_rest,
    get_bot,
    make_image,
    on_ready,
    timer,
    event_user_id,
)
from utils.temp_files import schedule_temp_file_cleanup

from .client import TiboRadarClient
from .draw_scope import AMBER, CYAN, GREEN, CardSection, event_sections, render_card
from .draw_xfeed import render_xfeed
from .models import EVENT_CONFIRMED, ResetEvent, TiboPost
from .service import TiboRadarService
from .store import TiboStore


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


REFRESH_MINUTES = _env_int("TIBO_RADAR_REFRESH_MINUTES", 10, 2, 1440)
TIMEOUT_SECONDS = _env_int("TIBO_RADAR_TIMEOUT_SECONDS", 15, 3, 60)
CODEXRADAR_ENABLED = _env_bool("TIBO_RADAR_CODEXRADAR_ENABLED", True)
SUBSCRIPTION_BATCH = _env_int("TIBO_RADAR_SUBSCRIPTION_BATCH", 20, 1, 100)

store = TiboStore()
client = TiboRadarClient(
    codexradar_url=os.getenv("TIBO_RADAR_CODEXRADAR_URL", "https://codexradar.com/"),
    feed_url=os.getenv("TIBO_RADAR_FEED_URL", "https://codex-reset.com/api/feed?locale=zh"),
    timeline_url=os.getenv("TIBO_RADAR_TIMELINE_URL", "https://codex-reset.com/api/timeline?locale=zh"),
    timeout_seconds=TIMEOUT_SECONDS,
    ttl_seconds=REFRESH_MINUTES * 60,
    codexradar_enabled=CODEXRADAR_ENABLED,
)
service = TiboRadarService(store, client)


def _image_segment(png: bytes):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(png)
        handle.flush()
        schedule_temp_file_cleanup(handle.name, delay_seconds=60)
        return make_image(path=handle.name)


def _post_links(posts: Iterable[TiboPost]) -> list[str]:
    return [f"[{post.post_id}] {post.url}" for post in posts if post.url]


def _event_links(events: Iterable[ResetEvent]) -> list[str]:
    return [f"[{event.event_id}] {event.url}" for event in events if event.url]


def _source_links() -> list[str]:
    return ["数据源: https://codexradar.com/", "数据源: https://codex-reset.com/tibo"]


def _chunk(items: list, size: int = 3) -> list[list]:
    return [items[index : index + size] for index in range(0, len(items), size)]


_GROUP_ADMIN_ROLE_TOKENS = {
    "admin",
    "administrator",
    "owner",
    "manager",
    "群主",
    "管理员",
}


def _is_superuser(user_id: str) -> bool:
    configured = GlobalConfig.SUPERUSERS
    if isinstance(configured, str):
        configured = [configured]
    return str(user_id) in {str(value) for value in configured}


def _member_has_group_admin_role(member: object) -> bool:
    """Accept Satori roles and the common adapter-level admin flags."""

    for attribute in ("is_owner", "is_admin", "is_administrator", "owner", "admin"):
        value = getattr(member, attribute, False)
        if value is True or (
            isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}
        ):
            return True

    roles = getattr(member, "roles", None) or []
    if isinstance(roles, (str, bytes, dict)):
        roles = [roles]
    for role in roles:
        if isinstance(role, dict):
            values = role.values()
        else:
            values = (
                getattr(role, "id", ""),
                getattr(role, "name", ""),
            )
        for value in values:
            lowered = str(value or "").strip().lower()
            if lowered and (
                lowered in _GROUP_ADMIN_ROLE_TOKENS
                or any(token in lowered for token in _GROUP_ADMIN_ROLE_TOKENS)
            ):
                return True
    return False


async def _is_subscription_manager(bot: Bot, event: Event, group_id: str, user_id: str) -> bool:
    """Check SuperUser or the sender's current group owner/admin role."""

    if _is_superuser(user_id):
        return True
    member = getattr(event, "member", None)
    if member is not None and _member_has_group_admin_role(member):
        return True
    getter = getattr(bot, "guild_member_get", None)
    if callable(getter) and group_id:
        try:
            member = await getter(guild_id=group_id, user_id=str(user_id))
        except Exception as exc:
            logger.debug("[tibo_radar] failed to fetch group member role: {}", exc)
        else:
            if member is not None and _member_has_group_admin_role(member):
                return True
    return False


def _subscription_permission_text() -> str:
    return "仅群主、群管理员或 SUPERUSER 可管理 Tibo 新帖订阅。"


def _post_cursor(post: TiboPost) -> tuple[str, str]:
    value = post.first_seen_at or post.last_seen_at or post.source_time
    if value is None:
        return "", post.post_id
    return value.astimezone(timezone.utc).isoformat(), post.post_id


async def _notify_subscriptions(bot: Bot) -> None:
    """Deliver posts after each group's persisted cursor in X-style cards."""

    subscriptions = store.subscriptions()
    if not subscriptions:
        return
    adapter = account_adapter_name(bot)
    for subscription in subscriptions:
        if subscription.baseline_pending:
            store.mark_subscription_initialized(subscription.group_id)
            logger.info("[tibo_radar] subscription baseline initialized group={}", subscription.group_id)
            continue
        cursor_at = subscription.last_notified_at.astimezone(timezone.utc).isoformat() if subscription.last_notified_at else ""
        pending = store.posts_after(
            cursor_at,
            subscription.last_notified_post_id,
            limit=SUBSCRIPTION_BATCH,
        )
        if not pending:
            continue
        pages = _chunk(pending)
        for page_index, page_posts in enumerate(pages, 1):
            try:
                png = await render_xfeed(
                    page_posts,
                    service.relevance_label,
                    title="Tibo 新帖订阅",
                    subtitle=f"群订阅 · 新发现 {len(page_posts)} 条 · 每 {REFRESH_MINUTES} 分钟检测",
                    page=f"{page_index}/{len(pages)}" if len(pages) > 1 else "",
                )
                links = list(dict.fromkeys([*_post_links(page_posts), *_source_links()]))
                segments = [_image_segment(png)]
                if links:
                    segments.append(Text("源帖链接：\n" + "\n".join(links)))
                target = SendDest(
                    subscription.channel_id or subscription.group_id,
                    subscription.group_id,
                    True,
                    False,
                    "",
                    adapter,
                )
                await ChainMsg(segments).send(target, bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[tibo_radar] subscription delivery failed group={}", subscription.group_id)
                break

            cursor_at, post_id = _post_cursor(page_posts[-1])
            store.mark_subscription_delivered(subscription.group_id, cursor_at, post_id)


def _format_pt_time() -> str:
    value = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local = value.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        try:
            import pytz

            local = value.astimezone(pytz.timezone("America/Los_Angeles"))
        except Exception:
            local = value
    phase = "睡觉" if local.hour < 7 or local.hour >= 23 else "上班" if local.hour < 18 else "可能在线"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')} · {phase}（粗粒度公开推测）"


def _ellipsize(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _feed_section_lines(posts: Iterable[TiboPost]) -> list[str]:
    """三条动态，每条固定输出 原文 / 翻译 / 解读 三要素。"""
    lines: list[str] = []
    for post in posts:
        when = post.source_time.astimezone().strftime("%m-%d %H:%M") if post.source_time else "未知"
        lines.append(f"{service.relevance_label(post.relevance)} · {when} · {post.post_id}")
        lines.append(f"原文：{_ellipsize(post.text)}")
        if post.translation:
            lines.append(f"翻译：{_ellipsize(post.translation)}")
        if post.analysis:
            lines.append(f"解读：{_ellipsize(post.analysis)}")
    return lines


def _overview_sections() -> tuple[list[CardSection], list[str]]:
    status = service.status()
    latest = service.latest_confirmed()
    stats = service.stats()
    posts = service.latest_posts(3)
    source_lines = service.source_health_lines()
    status_lines = [status.label, status.detail]
    if status.event:
        status_lines.append(f"证据：{status.event.event_id} · {status.event.url}")
    recent_lines = ["暂无可核验的已完成重置记录"]
    recent_links: list[str] = []
    if latest:
        at = latest.effective_at or latest.announced_at
        recent_lines = [
            f"时间：{at.astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if at else '未知'}",
            f"距今：{service.duration_since(latest)}",
            f"证据：{latest.localized_summary or latest.summary}",
            f"确认：{latest.source_label or latest.source or '公开核验'}",
        ]
        recent_links = _event_links([latest])
    stat_lines = [f"已确认样本：{stats['sample_count']} 条"]
    if stats.get("latest_interval_hours") is not None:
        stat_lines.append(f"最近间隔：{stats['latest_interval_hours']} 小时")
    if stats.get("beijing_hour_counts"):
        hours = "、".join(f"{hour:02d}:00({count})" for hour, count in stats["beijing_hour_counts"].items())
        stat_lines.append(f"北京时间小时分布：{hours}")
    sections = [
        CardSection("当前雷达状态", status_lines, (GREEN if status.active else CYAN)),
        CardSection("最近一次已确认重置", recent_lines, GREEN),
        CardSection("Tibo 所在 PT 时区", [_format_pt_time(), "仅用于公开活动时段的粗粒度展示，不代表实时在线"], AMBER),
        CardSection("历史统计", stat_lines, CYAN),
        CardSection("最新相关动态", _feed_section_lines(posts), AMBER),
        CardSection("来源健康", source_lines, CYAN),
    ]
    return sections, [*recent_links, *_post_links(posts), *_source_links()]


async def _finish_card(title: str, subtitle: str, sections: list[CardSection], links: list[str], *, page: str = "", finish: bool = True):
    png = await render_card(title, subtitle, sections, page=page)
    segments = [_image_segment(png)]
    deduped = list(dict.fromkeys(link for link in links if link))
    if deduped:
        segments.append(Text("来源链接：\n" + "\n".join(deduped)))
    if finish:
        await tibo_cmd.finish(ChainMsg(segments))
    else:
        await tibo_cmd.send(ChainMsg(segments))


def _parse_count(parts: list[str], default: int, maximum: int = 20) -> tuple[int | None, str | None]:
    if len(parts) <= 1:
        return default, None
    try:
        value = int(parts[1])
    except ValueError:
        return None, "数量必须是整数"
    if not 1 <= value <= maximum:
        return None, f"数量必须在 1–{maximum} 之间"
    return value, None


def _help_text() -> str:
    return (
        "Tibo 雷达用法：\n"
        "/tibo 或 /雷达 —— 雷达总览\n"
        "/tibo 动态 [数量] —— 最近 X 动态（原文+翻译+解读），默认 6 条合一图，超过 6 条自动分页\n"
        "/tibo 状态 —— 当前预告、窗口或疑似信号\n"
        "/tibo 最近 —— 最近一次已核验完成的重置\n"
        "/tibo 历史 [数量] —— 重置事件历史，默认 6 条\n"
        "/tibo 订阅 —— 本群订阅 Tibo 新帖（仅群主/管理员/SUPERUSER）\n"
        "/tibo 取消订阅 —— 停止本群的新帖推送（仅群主/管理员/SUPERUSER）\n"
        "/tibo 订阅状态 —— 查看本群订阅状态\n"
        "/tibo 帮助 —— 显示本帮助"
    )


tibo_cmd = _cmd("tibo", aliases={"雷达"}, priority=5, block=True)


@tibo_cmd.handle()
async def handle_tibo(rest: ArgVal[str], event: Event, bot: Bot):
    parts = [part for part in get_rest(rest).split() if part]
    action = parts[0].lower() if parts else "总览"
    action = {
        "overview": "总览",
        "最新": "动态",
        "reset": "状态",
        "重置": "状态",
        "last": "最近",
        "history": "历史",
        "help": "帮助",
        "subscribe": "订阅",
        "sub": "订阅",
        "unsubscribe": "取消订阅",
        "unsub": "取消订阅",
        "退订": "取消订阅",
        "subscription": "订阅状态",
        "substatus": "订阅状态",
        "subscriptions": "订阅状态",
    }.get(action, action)
    if action == "订阅" and len(parts) > 1 and parts[1].lower() in {"状态", "status"}:
        action = "订阅状态"
    if action in {"帮助", "?"}:
        await tibo_cmd.finish(_help_text())
        return
    if action == "订阅":
        group_id = get_group_id(event)
        if not group_id:
            await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
            return
        if not await _is_subscription_manager(bot, event, group_id, event_user_id(event)):
            await tibo_cmd.finish(_subscription_permission_text())
            return
        already_enabled, _subscription = store.subscribe(group_id, get_channel_id(event) or group_id)
        if already_enabled:
            await tibo_cmd.finish("本群已经订阅 Tibo 新帖；新帖子会按采集周期推送。")
        else:
            await tibo_cmd.finish("已订阅本群的 Tibo 新帖；从下一条新帖开始推送，附 X 风格卡片和源帖链接。")
        return
    if action == "取消订阅":
        group_id = get_group_id(event)
        if not group_id:
            await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
            return
        if not await _is_subscription_manager(bot, event, group_id, event_user_id(event)):
            await tibo_cmd.finish(_subscription_permission_text())
            return
        if store.unsubscribe(group_id):
            await tibo_cmd.finish("已停止本群的 Tibo 新帖推送。")
        else:
            await tibo_cmd.finish("本群当前没有启用 Tibo 新帖订阅。")
        return
    if action == "订阅状态":
        group_id = get_group_id(event)
        if not group_id:
            await tibo_cmd.finish("Tibo 新帖订阅只支持在群内使用。")
            return
        subscription = store.subscription(group_id)
        if subscription and subscription.enabled:
            cursor = subscription.last_notified_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if subscription.last_notified_at else "尚未推送"
            await tibo_cmd.finish(f"本群已订阅 Tibo 新帖。\n最近推送游标：{cursor}")
        else:
            await tibo_cmd.finish("本群未启用 Tibo 新帖订阅。使用 /tibo 订阅 开启。")
        return
    if action in {"总览", "概览"}:
        sections, links = _overview_sections()
        await _finish_card("Tibo 雷达总览", f"本地缓存查询 · 最近采集周期 {REFRESH_MINUTES} 分钟", sections, links)
        return
    if action == "动态":
        count, error = _parse_count(parts, 6)
        if error:
            await tibo_cmd.finish(error + "。使用 /tibo 帮助查看用法")
            return
        posts = service.latest_posts(count or 6)
        if not posts:
            await _finish_card("Tibo 最新动态", "本地历史中暂无直接或间接相关内容", [CardSection("动态", ["暂无记录；无关动态不会显示在此列表中"], CYAN)], _source_links())
            return
        # Keep the default six-post query in one message.  Larger explicit
        # requests still paginate at six posts per card to stay within the
        # renderer's height limit.
        pages = _chunk(posts, size=6)
        for page_index, page_posts in enumerate(pages, 1):
            png = await render_xfeed(
                page_posts,
                service.relevance_label,
                title="Tibo 最新 X 动态",
                subtitle=f"最近 {len(page_posts)} 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
                page=f"{page_index}/{len(pages)}" if len(pages) > 1 else "",
            )
            segments = [_image_segment(png)]
            deduped = list(dict.fromkeys(link for link in [*_post_links(page_posts), *_source_links()] if link))
            if deduped:
                segments.append(Text("来源链接：\n" + "\n".join(deduped)))
            if page_index == len(pages):
                await tibo_cmd.finish(ChainMsg(segments))
            else:
                await tibo_cmd.send(ChainMsg(segments))
        return
    if action == "状态":
        current = service.status()
        links = _event_links([current.event] if current.event else []) + _source_links()
        lines = [current.label, current.detail]
        if current.event:
            lines.extend([f"事件：{current.event.event_id}", f"证据原文：{current.event.localized_summary or current.event.summary}"])
        await _finish_card("Tibo 重置状态", "只区分预告、窗口、疑似和已确认，不把预测写成事实", [CardSection("当前状态", lines, GREEN if current.active else AMBER)], links)
        return
    if action == "最近":
        latest = service.latest_confirmed()
        if latest is None:
            await _finish_card("最近一次重置", "本地历史中暂无严格核验完成记录", [CardSection("状态", ["暂无已确认完成的全局重置"], CYAN)], _source_links())
        else:
            at = latest.effective_at or latest.announced_at
            lines = [
                f"完成/确认时间：{at.astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if at else '未知'}",
                f"距今：{service.duration_since(latest)}",
                f"确认状态：{service.event_label(latest.status)}",
                f"证据：{latest.localized_summary or latest.summary}",
            ]
            await _finish_card("最近一次已确认重置", "只显示可核验完成事件", event_sections([latest], service.event_label), _event_links([latest]) + _source_links())
        return
    if action == "历史":
        count, error = _parse_count(parts, 6)
        if error:
            await tibo_cmd.finish(error + "。使用 /tibo 帮助查看用法")
            return
        events = service.history(count or 6)
        if not events:
            await _finish_card("Tibo 重置历史", "本地缓存查询", [CardSection("历史", ["暂无记录"], CYAN)], _source_links())
            return
        pages = _chunk(events)
        for page_index, page_events in enumerate(pages, 1):
            await _finish_card("Tibo 重置历史", "预告、窗口、疑似与完成状态分开标注", event_sections(page_events, service.event_label), [*_event_links(page_events), *_source_links()], page=f"{page_index}/{len(pages)}", finish=page_index == len(pages))
        return
    await tibo_cmd.finish("未知子命令。\n\n" + _help_text())


_startup_started = False
_startup_task: asyncio.Task | None = None


async def _refresh_and_notify(bot: Bot | None = None):
    if bot is None:
        try:
            bot = get_bot()
        except Exception:
            bot = None
    success = await service.refresh()
    if bot is not None and success:
        await _notify_subscriptions(bot)
    return success


@on_ready
async def _warmup(_bot: Bot | None = None):
    global _startup_started, _startup_task
    if _startup_started:
        return
    _startup_started = True
    _startup_task = asyncio.create_task(_refresh_and_notify(_bot))


timer.add_job(
    _refresh_and_notify,
    "interval",
    minutes=REFRESH_MINUTES,
    id="tibo_radar_refresh",
    replace_existing=True,
    max_instances=1,
)


@listen(Cleanup)
async def _close_tibo_radar():
    if _startup_task is not None and not _startup_task.done():
        _startup_task.cancel()
        await asyncio.gather(_startup_task, return_exceptions=True)
    store.close()
