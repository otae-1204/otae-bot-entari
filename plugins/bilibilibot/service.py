from __future__ import annotations

import re
import time
import tempfile
from typing import Iterable

from arclet.entari import Account as Bot
from loguru import logger
from utils.entari_native import make_image, SendDest, ChainMsg, get_bot, account_adapter_name

from utils.temp_files import schedule_temp_file_cleanup

from .client import BiliAPIError, BiliClient
from .draw import draw_bili_card
from .models import BiliCard, KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, SUPPORTED_KINDS, TargetInfo
from .store import BiliStore


KIND_ALIASES = {
    "all": "all",
    "live": KIND_LIVE,
    "liver": KIND_LIVE,
    "直播": KIND_LIVE,
    "video": KIND_VIDEO,
    "up": KIND_VIDEO,
    "视频": KIND_VIDEO,
    "dynamic": KIND_DYNAMIC,
    "动态": KIND_DYNAMIC,
}


def expand_kinds(raw: str) -> list[str]:
    kind = KIND_ALIASES.get(raw.lower(), KIND_ALIASES.get(raw))
    if kind == "all":
        return [KIND_LIVE, KIND_VIDEO, KIND_DYNAMIC]
    if kind in SUPPORTED_KINDS:
        return [kind]
    raise ValueError(f"unsupported kind: {raw}")


class BiliService:
    def __init__(self, store: BiliStore, client: BiliClient):
        self.store = store
        self.client = client
        self._video_failure_log_cache: dict[str, tuple[str, int]] = {}

    async def follow(self, kind_arg: str, values: list[str], subscriber_type: str, subscriber_id: str) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        kinds = expand_kinds(kind_arg)
        for value in values:
            for kind in kinds:
                try:
                    target = await self.resolve_target(kind, value)
                    self.store.upsert_target(target)
                    added = self.store.add_subscription(kind, target.uid, subscriber_type, subscriber_id)
                    label = f"{self._kind_name(kind)} {target.name or target.uid}"
                    ok.append(label + (" 已订阅" if added else " 已存在"))
                except Exception as exc:
                    logger.warning(f"[bilibilibot] follow {kind} {value} failed: {exc}")
                    failed.append(f"{self._kind_name(kind)} {value}: {exc}")
        return ok, failed

    async def unfollow(self, kind_arg: str, values: list[str], subscriber_type: str, subscriber_id: str) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        for value in values:
            for kind in expand_kinds(kind_arg):
                target = self.store.get_target(kind, value)
                uid = target.uid if target else value
                removed = self.store.remove_subscription(kind, uid, subscriber_type, subscriber_id)
                (ok if removed else failed).append(f"{self._kind_name(kind)} {uid}" + (" 已取关" if removed else " 未订阅"))
        return ok, failed

    async def refresh(self, kind_arg: str, values: list[str]) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        for value in values:
            for kind in expand_kinds(kind_arg):
                try:
                    target = await self.resolve_target(kind, value)
                    self.store.upsert_target(target)
                    ok.append(f"{self._kind_name(kind)} {target.name or target.uid} 已刷新")
                except Exception as exc:
                    logger.warning(f"[bilibilibot] refresh {kind} {value} failed: {exc}")
                    failed.append(f"{self._kind_name(kind)} {value}: {exc}")
        return ok, failed

    async def resolve_target(self, kind: str, value: str) -> TargetInfo:
        value = value.strip()
        if kind == KIND_LIVE:
            return await self.client.resolve_live_target(value)
        if kind == KIND_VIDEO:
            return await self.client.resolve_video_target(value)
        if kind == KIND_DYNAMIC:
            return await self.client.resolve_dynamic_target(value)
        raise ValueError(f"unsupported kind: {kind}")

    def list_subscriptions(self, subscriber_type: str, subscriber_id: str, kind_arg: str | None = None) -> list[str]:
        kinds = expand_kinds(kind_arg) if kind_arg else [KIND_LIVE, KIND_VIDEO, KIND_DYNAMIC]
        lines: list[str] = []
        for kind in kinds:
            rows = self.store.subscriptions_for_subscriber(subscriber_type, subscriber_id, kind)
            lines.append(f"{self._kind_name(kind)}:")
            if not rows:
                lines.append("  (空)")
                continue
            for sub, target in rows:
                name = target.name if target else sub.target_uid
                extra = f" room {target.room_id}" if target and target.room_id else ""
                lines.append(f"  - {name} ({sub.target_uid}{extra})")
        return lines

    async def check_all(self) -> None:
        await self.check_live()
        await self.check_video()
        await self.check_dynamic()

    async def check_live(self) -> None:
        for target in self.store.list_active_targets(KIND_LIVE):
            try:
                latest = await self.client.latest_live_state(target)
                if latest.is_live != target.is_live:
                    card = BiliCard(
                        "live_on" if latest.is_live else "live_off",
                        latest.last_title or target.last_title or "直播状态变化",
                        author=target.name,
                        subtitle="正在直播" if latest.is_live else "直播已结束",
                        cover_url=latest.last_cover or target.last_cover,
                        avatar_url=target.avatar_url,
                        url=f"https://live.bilibili.com/{latest.room_id or target.room_id}",
                        badge="LIVE" if latest.is_live else "ENDED",
                        uid=target.uid,
                        room_id=latest.room_id or target.room_id,
                    )
                    await self.broadcast(target.kind, target.uid, card)
                self.store.upsert_target(latest)
            except Exception as exc:
                logger.warning(f"[bilibilibot] live check failed for {target.uid}: {exc}")

    async def check_video(self) -> None:
        for target in self.store.list_active_targets(KIND_VIDEO):
            try:
                card = await self.client.latest_video(target.uid)
                next_name = self._refined_name(target.name, target.uid, card.author)
                next_avatar = target.avatar_url or card.avatar_url
                if card.item_id and card.item_id != target.latest_id and not self.store.has_seen(KIND_VIDEO, target.uid, card.item_id):
                    card.author = card.author or next_name or target.uid
                    card.avatar_url = card.avatar_url or next_avatar
                    await self.broadcast(KIND_VIDEO, target.uid, card)
                    self.store.mark_seen(KIND_VIDEO, target.uid, card.item_id, card.published_at)
                self.store.upsert_target(
                    TargetInfo(
                        KIND_VIDEO,
                        target.uid,
                        name=next_name,
                        avatar_url=next_avatar,
                        latest_id=card.item_id or target.latest_id,
                        latest_ts=card.published_at or target.latest_ts,
                        last_title=card.title or target.last_title,
                        last_cover=card.cover_url or target.last_cover,
                        last_desc=card.description or target.last_desc,
                    )
                )
            except Exception as exc:
                self._log_video_check_failure(target.uid, exc)

    async def check_dynamic(self) -> None:
        for target in self.store.list_active_targets(KIND_DYNAMIC):
            try:
                items = await self.client.dynamic_items(target.uid)
                cards = [self.client._dynamic_card_from_item(item, target.uid) for item in items[:5]]
                cards.sort(key=lambda item: item.published_at)
                newest_ts = target.latest_ts
                newest_id = target.latest_id
                next_name = target.name
                next_avatar = target.avatar_url
                for card in cards:
                    next_name = self._refined_name(next_name, target.uid, card.author)
                    next_avatar = next_avatar or card.avatar_url
                    if not card.item_id or card.published_at <= target.latest_ts or self.store.has_seen(KIND_DYNAMIC, target.uid, card.item_id):
                        continue
                    newest_ts = max(newest_ts, card.published_at)
                    newest_id = card.item_id
                    if self._should_skip_video_dynamic(target.uid, card):
                        self.store.mark_seen(KIND_DYNAMIC, target.uid, card.item_id, card.published_at)
                        continue
                    card.author = card.author or next_name or target.uid
                    card.avatar_url = card.avatar_url or next_avatar
                    await self.broadcast(KIND_DYNAMIC, target.uid, card)
                    self.store.mark_seen(KIND_DYNAMIC, target.uid, card.item_id, card.published_at)
                if cards:
                    latest = max(cards, key=lambda item: item.published_at)
                    self.store.upsert_target(
                        TargetInfo(
                            KIND_DYNAMIC,
                            target.uid,
                            name=next_name,
                            avatar_url=next_avatar,
                            latest_id=newest_id or latest.item_id or target.latest_id,
                            latest_ts=newest_ts or latest.published_at or target.latest_ts,
                            last_title=latest.title or target.last_title,
                            last_cover=latest.cover_url or target.last_cover,
                            last_desc=latest.description or target.last_desc,
                        )
                    )
            except Exception as exc:
                logger.warning(f"[bilibilibot] dynamic check failed for {target.uid}: {exc}")

    async def broadcast(self, kind: str, uid: str, card: BiliCard) -> None:
        bot = get_bot()
        image = await self.card_to_segment(card)
        for sub in self.store.subscriptions_for_target(kind, uid):
            target = self._target(bot, sub.subscriber_type, sub.subscriber_id)
            try:
                await ChainMsg([image]).send(target, bot)
            except Exception as exc:
                logger.warning(f"[bilibilibot] send to {sub.subscriber_type}:{sub.subscriber_id} failed: {exc}")

    async def card_to_segment(self, card: BiliCard):
        png = await draw_bili_card(card)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            f.flush()
            schedule_temp_file_cleanup(f.name)
            return make_image(path=f.name)

    def _target(self, bot: Bot, subscriber_type: str, subscriber_id: str) -> SendDest:
        adapter_name = account_adapter_name(bot)
        if subscriber_type == "group":
            return SendDest(subscriber_id, subscriber_id, True, False, "", adapter_name)
        return SendDest(subscriber_id, "", False, True, "", adapter_name)

    def _kind_name(self, kind: str) -> str:
        return {KIND_LIVE: "直播", KIND_VIDEO: "视频", KIND_DYNAMIC: "动态"}.get(kind, kind)

    def _refined_name(self, current: str, uid: str, candidate: str) -> str:
        candidate = (candidate or "").strip()
        current = (current or "").strip()
        if candidate and (not current or current == uid):
            return candidate
        return current

    def _should_skip_video_dynamic(self, uid: str, card: BiliCard) -> bool:
        if not self.store.subscriptions_for_target(KIND_VIDEO, uid):
            return False
        return bool(self._bvid_from_card(card))

    def _bvid_from_card(self, card: BiliCard) -> str:
        for value in (card.item_id, card.url, card.description):
            match = re.search(r"\bBV[0-9A-Za-z]{10}\b", value or "")
            if match:
                return match.group(0)
        return ""

    def _log_video_check_failure(self, uid: str, exc: Exception) -> None:
        message = str(exc)
        now = int(time.time())
        cached = self._video_failure_log_cache.get(uid)
        self._video_failure_log_cache[uid] = (message, now)
        if cached and cached[0] == message and now - cached[1] < 1800:
            logger.debug(f"[bilibilibot] video check failed for {uid}: {message}")
            return
        logger.warning(f"[bilibilibot] video check failed for {uid}: {message}")
