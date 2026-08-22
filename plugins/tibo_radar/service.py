"""Collection, normalization and read-only query service for Tibo Radar."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from loguru import logger

from .client import CollectionResult, SourceCollection, TiboRadarClient
from .models import (
    EVENT_CONFIRMED,
    EVENT_EXPECTED_WINDOW,
    EVENT_OFFICIAL_ANNOUNCEMENT,
    EVENT_REJECTED,
    EVENT_SUSPECTED,
    EVENT_UNCONFIRMED,
    RELEVANCE_DIRECT,
    RELEVANCE_INDIRECT,
    RadarSnapshot,
    ResetEvent,
    SourceState,
    TiboPost,
)
from .store import TiboStore


@dataclass(slots=True)
class RadarStatus:
    label: str
    detail: str
    event: ResetEvent | None = None
    active: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_time(event: ResetEvent) -> datetime:
    return event.effective_at or event.announced_at or datetime.min.replace(tzinfo=timezone.utc)


def _post_time(post: TiboPost) -> datetime:
    return post.source_time or post.last_seen_at or datetime.min.replace(tzinfo=timezone.utc)


def _format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分钟")
    return "".join(parts)


class TiboRadarService:
    def __init__(self, store: TiboStore, client: TiboRadarClient):
        self.store = store
        self.client = client
        self._refresh_lock = asyncio.Lock()
        self._refresh_count = 0

    async def refresh(self) -> bool:
        """Fetch and persist sources.  Return True when at least one source succeeded."""

        if self._refresh_lock.locked():
            logger.info("[tibo_radar] refresh skipped: previous refresh still running")
            return False
        async with self._refresh_lock:
            result = await self.client.collect()
            success = False
            grouped: dict[str, list[SourceCollection]] = {}
            for source in result.sources:
                grouped.setdefault(source.source_name, []).append(source)
                if source.state.last_success_at is not None:
                    success = True
                for post in source.posts:
                    self.store.upsert_post(post)
                for event in source.events:
                    self.store.upsert_event(event)
            for source_name, collections in grouped.items():
                self.store.upsert_source_state(self._merge_source_states(source_name, [item.state for item in collections]))
            self._refresh_count += 1
            logger.info(
                "[tibo_radar] refresh finished success={} posts={} events={} cycle={}",
                success,
                sum(len(source.posts) for source in result.sources),
                sum(len(source.events) for source in result.sources),
                self._refresh_count,
            )
            return success

    @staticmethod
    def _merge_source_states(source_name: str, states: Iterable[SourceState]) -> SourceState:
        states = list(states)
        successful = [state for state in states if state.last_success_at is not None]
        failed = [state for state in states if state.last_error]
        latest_success = max((state.last_success_at for state in successful if state.last_success_at), default=None)
        latest_attempt = max((state.last_attempt_at for state in states if state.last_attempt_at), default=None)
        upstream = max((state.upstream_updated_at for state in states if state.upstream_updated_at), default=None)
        fingerprints = ",".join(sorted({state.fingerprint for state in states if state.fingerprint}))
        return SourceState(
            source_name=source_name,
            last_success_at=latest_success,
            last_attempt_at=latest_attempt,
            upstream_updated_at=upstream,
            fingerprint=fingerprints,
            stale=bool(failed) or any(state.stale for state in states),
            last_error="; ".join(dict.fromkeys(state.last_error for state in failed if state.last_error)),
            consecutive_failures=0 if successful else max((state.consecutive_failures for state in states), default=1),
        )

    def snapshot(self, *, post_limit: int = 6, event_limit: int = 20) -> RadarSnapshot:
        return RadarSnapshot(
            posts=self.store.posts(limit=post_limit),
            events=self.store.events(limit=event_limit),
            source_states=self.store.source_states(),
            collected_at=_now(),
        )

    def latest_posts(self, limit: int = 6) -> list[TiboPost]:
        return self.store.posts(relevant_only=True, limit=limit)

    def history(self, limit: int = 6) -> list[ResetEvent]:
        return self.store.events(limit=limit, include_rejected=True)

    def latest_confirmed(self) -> ResetEvent | None:
        return self.store.latest_confirmed()

    def status(self, *, now: datetime | None = None) -> RadarStatus:
        now = now or _now()
        events = sorted(self.store.events(limit=1000), key=_event_time, reverse=True)
        latest_rejected = next((event for event in events if event.status == EVENT_REJECTED), None)
        for event in events:
            announced = event.announced_at or event.effective_at
            age = now - announced if announced else timedelta(days=9999)
            if event.status == EVENT_EXPECTED_WINDOW:
                if event.window_end and event.window_end >= now:
                    label = "官方预告窗口进行中"
                    detail = event.window_label or "预计窗口尚未结束"
                    return RadarStatus(label, detail, event, active=True)
                if age <= timedelta(days=2):
                    return RadarStatus("预告窗口已过，尚未核验完成", "不能把预告时间当成已完成事实", event, active=False)
            elif event.status == EVENT_OFFICIAL_ANNOUNCEMENT and age <= timedelta(days=2):
                return RadarStatus("官方重置预告", "等待完成证据", event, active=True)
            elif event.status == EVENT_SUSPECTED and age <= timedelta(days=2):
                return RadarStatus("疑似重置信号", "来源尚未给出可核验的完成确认", event, active=True)
        if latest_rejected and (latest_rejected.announced_at and now - latest_rejected.announced_at <= timedelta(days=2)):
            return RadarStatus("预告未被核验", "上游已将该候选标记为 rejected", latest_rejected, active=False)
        return RadarStatus("暂无进行中的重置信号", "最近一次已确认重置请查看 /tibo 最近", None, active=False)

    def stats(self) -> dict:
        return self.store.reset_stats()

    def source_health_lines(self) -> list[str]:
        now = _now()
        lines: list[str] = []
        for state in self.store.source_states():
            if state.last_success_at is None:
                freshness = "从未成功"
            else:
                freshness = f"成功于 {_format_duration(now - state.last_success_at)} 前"
            suffix = "（陈旧/部分失败）" if state.stale or state.last_error else ""
            lines.append(f"{state.source_name}: {freshness}{suffix}")
        return lines or ["暂无采集记录"]

    @staticmethod
    def relevance_label(value: str) -> str:
        return {
            RELEVANCE_DIRECT: "直接相关",
            RELEVANCE_INDIRECT: "间接相关",
        }.get(value, "无重置信号")

    @staticmethod
    def event_label(value: str) -> str:
        return {
            EVENT_CONFIRMED: "已确认完成",
            EVENT_EXPECTED_WINDOW: "预计时间窗口",
            EVENT_OFFICIAL_ANNOUNCEMENT: "官方重置预告",
            EVENT_SUSPECTED: "疑似发生",
            EVENT_REJECTED: "预告未兑现/已否定",
            EVENT_UNCONFIRMED: "未确认",
        }.get(value, "未确认")

    @staticmethod
    def duration_since(event: ResetEvent | None, *, now: datetime | None = None) -> str:
        if event is None:
            return "暂无记录"
        at = event.effective_at or event.announced_at
        return _format_duration((now or _now()) - at) if at else "时间未知"

