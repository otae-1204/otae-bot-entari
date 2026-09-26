"""Command facade for bilibilibot.

This module only serves the `/bili` command path: subscribing, unsubscribing,
refreshing and listing, plus the chat link preview. Polling lives in
`poller.py`, delivery in `notifier.py` and detection in `detect.py`.
"""

from __future__ import annotations

import tempfile
from typing import Iterable

from loguru import logger
from otae_bot.adapters.entari import make_image
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from .api import BiliAPIError, BiliApi
from .draw import draw_bili_card
from .models import BiliCard, KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, SUPPORTED_KINDS, TargetInfo
from .refs import parse_target_ref
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
    def __init__(self, store: BiliStore, client: BiliApi):
        self.store = store
        self.client = client
        self._last_resolve_error = ""

    # --- follow / unfollow / refresh --------------------------------------

    async def follow(
        self, kind_arg: str, values: list[str], subscriber_type: str, subscriber_id: str
    ) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        kinds = expand_kinds(kind_arg)
        for raw in values:
            resolved = await self._resolve_value(raw, kinds)
            if resolved is None:
                failed.append(self._last_resolve_error)
                continue
            uid, live_target, by_room = resolved
            for kind in kinds:
                try:
                    target = (
                        live_target
                        if (kind == KIND_LIVE and live_target is not None)
                        else await self.resolve_target_by_uid(kind, uid)
                    )
                    self._assert_uid_matches(kind, uid, target)
                    await self.store.upsert_target(target)
                    added = await self.store.add_subscription(kind, target.uid, subscriber_type, subscriber_id)
                    label = self._describe(kind, target, by_room=by_room)
                    ok.append(label + ("已订阅" if added else "已存在"))
                except Exception as exc:
                    logger.warning(f"[bilibilibot] follow {kind} {raw} failed: {exc}")
                    failed.append(f"{self._kind_name(kind)} {raw}: {exc}")
        return ok, failed

    async def unfollow(
        self, kind_arg: str, values: list[str], subscriber_type: str, subscriber_id: str
    ) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        kinds = expand_kinds(kind_arg)
        for raw in values:
            resolved = await self._resolve_value(raw, kinds, lookup_only=True)
            if resolved is None:
                failed.append(self._last_resolve_error)
                continue
            uid, live_target, _by_room = resolved
            for kind in kinds:
                removed = await self.store.remove_subscription(kind, uid, subscriber_type, subscriber_id)
                target = (
                    live_target
                    if (kind == KIND_LIVE and live_target is not None)
                    else await self.store.get_target(kind, uid)
                )
                label = self._describe(kind, target, uid=uid) if target else f"{self._kind_name(kind)} {uid}"
                (ok if removed else failed).append(label + ("已取关" if removed else "未订阅"))
        return ok, failed

    async def refresh(self, kind_arg: str, values: list[str]) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        failed: list[str] = []
        kinds = expand_kinds(kind_arg)
        for raw in values:
            resolved = await self._resolve_value(raw, kinds)
            if resolved is None:
                failed.append(self._last_resolve_error)
                continue
            uid, live_target, by_room = resolved
            for kind in kinds:
                try:
                    target = (
                        live_target
                        if (kind == KIND_LIVE and live_target is not None)
                        else await self.resolve_target_by_uid(kind, uid)
                    )
                    self._assert_uid_matches(kind, uid, target)
                    await self.store.upsert_target(target)
                    ok.append(self._describe(kind, target, by_room=by_room) + "已刷新")
                except Exception as exc:
                    logger.warning(f"[bilibilibot] refresh {kind} {raw} failed: {exc}")
                    failed.append(f"{self._kind_name(kind)} {raw}: {exc}")
        return ok, failed

    # --- target resolution -------------------------------------------------

    async def resolve_target_by_uid(self, kind: str, uid: str) -> TargetInfo:
        """Resolve one kind from a confirmed UID; no value is ever guessed twice."""
        if kind == KIND_LIVE:
            return await self.client.resolve_live_by_uid(uid)
        if kind == KIND_VIDEO:
            return await self.client.resolve_video_target(uid)
        if kind == KIND_DYNAMIC:
            return await self.client.resolve_dynamic_target(uid)
        raise ValueError(f"unsupported kind: {kind}")

    async def _resolve_value(self, raw: str, kinds: list[str], *, lookup_only: bool = False):
        """Turn one user argument into (uid, live_target, by_room) or None on failure.

        A single value is parsed once, so every kind subscribes to the same
        person. `room:` and live links are only meaningful for live (and all).
        """
        try:
            ref = parse_target_ref(raw)
        except ValueError as exc:
            self._last_resolve_error = str(exc)
            return None

        if ref.by == "uid":
            return ref.value, None, False

        if KIND_LIVE not in kinds:
            self._last_resolve_error = f"直播间号只能用于 live 或 all: {raw}"
            return None

        live_target = await self._live_target_by_room(ref.value, lookup_only=lookup_only)
        if live_target is None:
            return None
        return live_target.uid, live_target, True

    async def _live_target_by_room(self, room_id: str, *, lookup_only: bool) -> TargetInfo | None:
        target = await self.store.get_live_target_by_room(room_id)
        if target is not None:
            return target
        try:
            # Short room ids are not stored verbatim, so ask the API for the long id.
            resolved = await self.client.resolve_live_by_room(room_id)
        except Exception as exc:
            logger.warning(f"[bilibilibot] live room {room_id} resolve failed: {exc}")
            self._last_resolve_error = f"{self._kind_name(KIND_LIVE)} {room_id}: {exc}"
            return None
        stored = await self.store.get_live_target_by_room(resolved.room_id)
        if stored is not None:
            return stored
        if lookup_only:
            self._last_resolve_error = f"{self._kind_name(KIND_LIVE)} {resolved.room_id} 未订阅"
            return None
        return resolved

    def _assert_uid_matches(self, kind: str, uid: str, target: TargetInfo) -> None:
        if str(target.uid) != str(uid):
            logger.error(
                f"[bilibilibot] {kind} resolved UID {target.uid} for {uid}; refusing to subscribe"
            )
            raise BiliAPIError(f"解析出的 UID {target.uid} 与期望的 {uid} 不一致")

    def _describe(self, kind: str, target: TargetInfo | None, *, uid: str = "", by_room: bool = False) -> str:
        if target is None:
            return f"{self._kind_name(kind)} {uid}"
        details = [f"UID {target.uid}"]
        if kind == KIND_LIVE and target.room_id:
            details.append(f"直播间 {target.room_id}")
        if by_room and kind == KIND_LIVE:
            details.append("按直播间号解析")
        return f"{self._kind_name(kind)} {target.name or target.uid}（{'，'.join(details)}）"

    def _kind_name(self, kind: str) -> str:
        return {KIND_LIVE: "直播", KIND_VIDEO: "视频", KIND_DYNAMIC: "动态"}.get(kind, kind)

    # --- listing and preview ----------------------------------------------

    async def list_subscriptions(
        self, subscriber_type: str, subscriber_id: str, kind_arg: str | None = None
    ) -> list[str]:
        kinds = expand_kinds(kind_arg) if kind_arg else [KIND_LIVE, KIND_VIDEO, KIND_DYNAMIC]
        lines: list[str] = []
        for kind in kinds:
            rows = await self.store.subscriptions_for_subscriber(subscriber_type, subscriber_id, kind)
            lines.append(f"{self._kind_name(kind)}:")
            if not rows:
                lines.append("  (空)")
                continue
            for sub, target in rows:
                name = target.name if target else sub.target_uid
                extra = f" room {target.room_id}" if target and target.room_id else ""
                lines.append(f"  - {name} ({sub.target_uid}{extra})")
        return lines

    async def preview_link(self, text: str) -> BiliCard | None:
        """Render one chat link into a card; None when the link is not understood."""
        parsed = await self.client.parse_link(text)
        if parsed is None:
            return None
        return await self.client.card_for_link(parsed)

    async def card_to_segment(self, card: BiliCard):
        png = await draw_bili_card(card)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            f.flush()
            schedule_temp_file_cleanup(f.name)
            return make_image(path=f.name)


def kind_names(kinds: Iterable[str]) -> list[str]:
    service = {KIND_LIVE: "直播", KIND_VIDEO: "视频", KIND_DYNAMIC: "动态"}
    return [service.get(kind, kind) for kind in kinds]
