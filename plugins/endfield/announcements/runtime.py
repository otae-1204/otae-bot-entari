"""Entari-facing commands and delivery; registration stays in endfield.handlers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, replace

from loguru import logger

from otae_bot.adapters.entari import (
    ChainMsg,
    SendDest,
    event_user_id,
    get_channel_id,
    get_group_id,
    is_group,
    make_image,
)
from otae_bot.group_features import feature_store, group_scope, scope_from_event
from otae_bot.group_permissions import can_manage
from otae_bot.infrastructure.cache import AsyncTTLCache

from .commands import HELP, parse
from .models import KINDS, Delivery, Destination, Subscription, digest, local_time
from .service import AnnouncementService, DeliveryDeferred
from .source import OfficialAnnouncementSource
from .store import AnnouncementStore


class AnnouncementRuntime:
    def __init__(self, store=None, source=None, *, clock=time.time, renderer=None):
        self.store = store or AnnouncementStore()
        self.clock = clock
        self.accounts = {}
        self.tasks: set[asyncio.Task] = set()
        self.renderer = renderer
        self.images = AsyncTTLCache(
            ttl_seconds=60, max_bytes=32 * 1024 * 1024, max_entries=16, sizeof=len
        )
        self.service = AnnouncementService(
            self.store, source or OfficialAnnouncementSource(), self.send, clock=clock
        )

    def remember(self, bot):
        platform = str(getattr(bot, "platform", "") or "")
        account = str(getattr(bot, "self_id", "") or "")
        if platform and account:
            self.accounts[(platform, account)] = bot

    def ready(self, bot):
        self.remember(bot)
        task = asyncio.create_task(self.tick())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def tick(self):
        try:
            await self.service.tick()
        except Exception as exc:  # noqa: BLE001 - a scheduled cycle must not kill other jobs
            logger.error(
                "[endfield-announcements] cycle failed error_type={}",
                type(exc).__name__,
            )

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.accounts.clear()
        await self.images.close()

    def _delivery_account(self, destination: Destination):
        bot = self.accounts.get((destination.platform, destination.account_id))
        connected = getattr(bot, "connected", None)
        if bot is None or (connected is not None and not connected.is_set()):
            raise DeliveryDeferred()
        if not destination.private and not feature_store.is_enabled(
            group_scope(bot, destination.target_id), "endfield"
        ):
            raise DeliveryDeferred()
        return bot

    async def send(self, destination: Destination, jobs: list[Delivery]):
        self._delivery_account(destination)
        if any(job.subscription_key != destination.key for job in jobs):
            raise ValueError("Announcement jobs do not belong to this destination")
        subscription = await asyncio.to_thread(self.store.subscription, destination.key)
        bulletin = await asyncio.to_thread(self.store.digest, jobs, int(self.clock()))
        if bulletin is None or not bulletin.cards:
            raise DeliveryDeferred()
        if self.renderer is None:
            from .rendering import draw_digest

            renderer = draw_digest
        else:
            renderer = self.renderer
        key = digest(
            [
                tuple(asdict(card) for card in bulletin.cards),
                bulletin.generated_at // 60,
            ]
        )
        png = await self.images.get_or_create(key, lambda: renderer(bulletin))
        # Rendering can take seconds; respect unsubscriptions, revised lead
        # times, expiry, account disconnects and group switches before sending.
        pending = await asyncio.to_thread(
            self.store.pending_jobs, jobs, int(self.clock())
        )
        current_subscription = await asyncio.to_thread(
            self.store.subscription, destination.key
        )
        if len(pending) != len(jobs) or current_subscription != subscription:
            raise DeliveryDeferred()
        bot = self._delivery_account(destination)
        result = await ChainMsg([make_image(raw=png)]).send(
            SendDest(
                id=destination.target_id
                if destination.private
                else destination.channel_id,
                parent_id="" if destination.private else destination.target_id,
                private=destination.private,
                self_id=destination.account_id,
            ),
            bot,
        )
        if not result:
            raise RuntimeError("Delivery was not acknowledged")

    async def command(self, event, args: tuple[str, ...], bot) -> str:
        command = parse(args)
        if command.error:
            return command.error
        if command.action == "help":
            return HELP
        if (
            bot is None
            or not getattr(bot, "self_id", None)
            or not getattr(bot, "platform", None)
        ):
            return "当前机器人账号不可用，请稍后重试。"
        private = not is_group(event)
        target = str(event_user_id(event)) if private else str(get_group_id(event))
        if not target:
            return "无法识别当前会话，已取消操作。"
        destination = Destination(
            str(bot.platform),
            str(bot.self_id),
            target,
            str(get_channel_id(event) or target),
            private,
        )
        self.remember(bot)
        if (
            command.action in {"subscribe", "unsubscribe", "lead", "started"}
            and not private
            and not await can_manage(bot, event, scope_from_event(bot, event))
        ):
            return "仅群主、群管理员或 SUPERUSER 可以修改本群公告订阅。"
        sub = await asyncio.to_thread(self.store.subscription, destination.key)
        if command.action == "unsubscribe":
            removed = await asyncio.to_thread(self.store.unsubscribe, destination.key)
            return (
                "已取消公告订阅，待发提醒已清除。"
                if removed
                else "当前会话尚未订阅公告。"
            )
        if command.action == "subscribe":
            saved = (
                replace(sub, destination=destination, kinds=command.kinds)
                if sub
                else Subscription(
                    destination,
                    command.kinds,
                    created_at=int(self.clock()),
                )
            )
            await asyncio.to_thread(self.store.subscribe, saved)
            self.service.next_refresh = 0
            return (
                "已订阅："
                + "、".join(KINDS[kind] for kind in saved.kinds)
                + "。\n首次成功采集建立基线；未来提醒生效。使用 /ef 公告 状态 查看设置。"
            )
        if command.action == "started":
            if sub is None:
                return "请先使用 /ef 公告 订阅 开启当前会话的提醒。"
            await asyncio.to_thread(
                self.store.subscribe, replace(sub, notify_started=command.enabled)
            )
            await asyncio.to_thread(self.store.plan, int(self.clock()))
            return "到点开始推送已" + ("开启" if command.enabled else "关闭") + "。"
        if command.action == "lead":
            if sub is None:
                return "请先使用 /ef 公告 订阅 开启当前会话的提醒。"
            await asyncio.to_thread(
                self.store.subscribe, replace(sub, **{command.setting: command.minutes})
            )
            await asyncio.to_thread(self.store.plan, int(self.clock()))
            return "提醒设置已保存：" + (
                f"提前 {command.minutes} 分钟。"
                if command.minutes
                else "已关闭此类提前提醒。"
            )
        if command.action == "list":
            await self.service.tick(read_only=True)
            articles = await asyncio.to_thread(self.store.articles)
            if not articles:
                return "尚未取得公告，请稍后重试。"
            lines = ["终末地官网最近公告（北京时间）："]
            for article in articles[:5]:
                lines.extend(
                    [
                        f"{local_time(article.published_at)}  {article.title}",
                        article.url,
                    ]
                )
            health = await asyncio.to_thread(self.store.metadata)
            if health.get("last_error"):
                lines.append("本轮采集失败，以上为已保存的公告。")
            return "\n".join(lines)
        health = await asyncio.to_thread(self.store.metadata)
        lines = ["终末地公告订阅状态："]
        if sub is None:
            lines.append("当前会话未订阅。")
        else:
            lines.append("类型：" + "、".join(KINDS[kind] for kind in sub.kinds))
            lines.append("到点开始推送：" + ("开启" if sub.notify_started else "关闭"))
            for label, minutes in (
                ("开始", sub.start_minutes),
                ("结束", sub.end_minutes),
                ("维护", sub.maintenance_minutes),
            ):
                lines.append(
                    f"{label}提醒：" + (f"提前 {minutes} 分钟" if minutes else "关闭")
                )
            lines.append(
                "基线：" + ("已建立" if sub.initialized else "等待首次成功采集")
            )
            count = await asyncio.to_thread(self.store.pending_count, destination.key)
            lines.append(f"待发提醒：{count} 条")
        success = int(health.get("last_success", 0))
        lines.append(
            "最近采集："
            + (local_time(success) + "（北京时间）" if success else "尚未成功")
        )
        if health.get("last_error"):
            lines.append(
                "采集暂时失败，保留上次数据；数据超过 30 分钟未更新时暂停推送。"
            )
        return "\n".join(lines)
