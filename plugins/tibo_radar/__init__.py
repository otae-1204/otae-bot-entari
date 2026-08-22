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

from utils.entari_native import ArgVal, ChainMsg, Text, cmd as _cmd, get_rest, make_image, on_ready, timer
from utils.temp_files import schedule_temp_file_cleanup

from .client import TiboRadarClient
from .draw_scope import AMBER, CYAN, GREEN, CardSection, event_sections, render_card, render_scope
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
        "/tibo 动态 [数量] —— 最近 X 动态（原文+翻译+解读），默认 6 条，每 3 条一图\n"
        "/tibo 状态 —— 当前预告、窗口或疑似信号\n"
        "/tibo 最近 —— 最近一次已核验完成的重置\n"
        "/tibo 历史 [数量] —— 重置事件历史，默认 6 条\n"
        "/tibo 帮助 —— 显示本帮助"
    )


tibo_cmd = _cmd("tibo", aliases={"雷达"}, priority=5, block=True)


@tibo_cmd.handle()
async def handle_tibo(rest: ArgVal[str]):
    parts = [part for part in get_rest(rest).split() if part]
    action = parts[0].lower() if parts else "总览"
    action = {"overview": "总览", "最新": "动态", "reset": "状态", "重置": "状态", "last": "最近", "history": "历史", "help": "帮助"}.get(action, action)
    if action in {"帮助", "?"}:
        await tibo_cmd.finish(_help_text())
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
        pages = _chunk(posts)
        for page_index, page_posts in enumerate(pages, 1):
            png = await render_scope(
                page_posts,
                service.relevance_label,
                title="Tibo 最新 X 动态",
                subtitle=f"最近 {len(page_posts)} 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
                page=f"{page_index}/{len(pages)}",
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


@on_ready
async def _warmup(_bot: Bot | None = None):
    global _startup_started, _startup_task
    if _startup_started:
        return
    _startup_started = True
    _startup_task = asyncio.create_task(service.refresh())


timer.add_job(
    service.refresh,
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
