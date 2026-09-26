"""Bilibili API layer: transport, live, space, RSSHub and link parsing.

`BiliApi` is the facade the rest of the plugin talks to; each module below owns
one concern and only `session` touches the network directly.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..models import (
    BiliCard,
    KIND_DYNAMIC,
    KIND_LIVE,
    KIND_VIDEO,
    LiveObservation,
    TargetInfo,
)
from . import links as links_api
from . import live as live_api
from . import rsshub as rsshub_api
from . import space as space_api
from .links import ParsedLink
from .session import BiliAPIError, BiliRiskControlError, BiliSession


class BiliApi:
    """Composition root for the Bilibili APIs.

    Owns the long-lived `BiliSession` plus the RSSHub instance list and the
    `BILI_DM_IMG_*` signing parameters, and delegates each capability to its
    module. The `_*` methods are the stable override points used by tests and by
    the modules themselves.
    """

    NAV_URL = BiliSession.NAV_URL
    USER_INFO_URL = space_api.USER_INFO_URL
    VIDEO_LIST_URL = space_api.VIDEO_LIST_URL
    VIDEO_VIEW_URL = space_api.VIDEO_VIEW_URL
    LIVE_ROOM_URL = live_api.ROOM_INFO_URL
    LIVE_USER_URL = live_api.LIVE_USER_URL
    DYNAMIC_URL = space_api.DYNAMIC_URL
    RISK_COOKIE_URL = BiliSession.RISK_COOKIE_URL
    RISK_GATEWAY_URL = BiliSession.RISK_GATEWAY_URL

    def __init__(
        self,
        *,
        timeout: float = 15,
        sessdata: str = "",
        buvid3: str = "",
        dm_img_list: str = "",
        dm_img_str: str = "",
        dm_cover_img_str: str = "",
        rsshub_base_urls: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 0.2,
    ):
        self.timeout = timeout
        self._transport = transport
        # Only non-empty values are signed; empty ones are simply absent.
        self.dm_img_params = {
            key: value
            for key, value in {
                "dm_img_list": dm_img_list,
                "dm_img_str": dm_img_str,
                "dm_cover_img_str": dm_cover_img_str,
            }.items()
            if value
        }
        self.rsshub_base_urls = rsshub_api.merge_base_urls(rsshub_base_urls)
        # Transport, cookies, WBI keys and risk-control live in the session.
        self.session = BiliSession(
            timeout=timeout,
            sessdata=sessdata,
            buvid3=buvid3,
            transport=transport,
            min_interval=min_interval,
        )

    # --- session passthrough -------------------------------------------------

    async def aclose(self) -> None:
        await self.session.aclose()

    @property
    def cookies(self):
        return self.session.cookies

    @property
    def img_key(self) -> str:
        return self.session.img_key

    @img_key.setter
    def img_key(self, value: str) -> None:
        self.session.img_key = value

    @property
    def sub_key(self) -> str:
        return self.session.sub_key

    @sub_key.setter
    def sub_key(self, value: str) -> None:
        self.session.sub_key = value

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        return self.session._http_client

    @property
    def _wbi_updated_at(self) -> int:
        return self.session._wbi_updated_at

    @_wbi_updated_at.setter
    def _wbi_updated_at(self, value: int) -> None:
        self.session._wbi_updated_at = value

    async def _http(self) -> httpx.AsyncClient:
        return await self.session._http()

    async def refresh_wbi_keys(self) -> None:
        await self.session.refresh_wbi_keys()

    async def ensure_wbi_keys(self) -> None:
        await self.session.ensure_wbi_keys()

    async def refresh_risk_cookies(self) -> None:
        await self.session.refresh_risk_cookies()

    async def ensure_risk_cookies(self) -> None:
        await self.session.ensure_risk_cookies()

    def _require_ok(self, data: dict[str, Any], label: str) -> dict[str, Any]:
        return self.session.require_ok(data, label)

    def sign(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.session.sign(params)

    def _wbi_sign(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.session.sign(params)

    async def _get_json(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.session.fetch_json(url, params=params)

    async def _get_json_with_risk_retry(
        self,
        url: str,
        label: str,
        *,
        params: dict[str, Any] | None = None,
        sign_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One retry after a risk-control refresh; signed requests are re-signed."""
        data = await self._get_json(url, params=params)
        if data.get("code") != -352:
            return self._require_ok(data, label)

        await self.refresh_risk_cookies()
        if sign_params is not None:
            await self.refresh_wbi_keys()
            params = self._wbi_sign(dict(sign_params))
        retry = await self._get_json(url, params=params)
        if retry.get("code") == -352:
            raise BiliRiskControlError(label)
        return self._require_ok(retry, label)

    # --- live ---------------------------------------------------------------

    async def batch_live_status(self, uids: list[str]) -> dict[str, LiveObservation]:
        return await live_api.batch_live_status(self.session, uids)

    async def live_observation(self, target: TargetInfo) -> LiveObservation | None:
        """Single-room fallback for one target; None means the room was not observed."""
        room = await live_api.room_info(self.session, target.room_id or target.uid)
        if not room:
            return None
        return live_api.live_observation_from_room(
            room, target.uid, now=int(time.time())
        )

    async def _live_room(self, room_id: str) -> dict[str, Any]:
        return await live_api.room_info(self.session, room_id)

    async def _live_user(self, uid: str) -> dict[str, Any]:
        return await live_api.live_user(self.session, uid)

    @staticmethod
    def _live_start_timestamp(live: dict[str, Any]) -> int:
        # Bilibili's live_time is a Beijing wall-clock string, independent of the
        # bot host's timezone; api.live owns the parsing rule.
        return live_api.live_start_timestamp(live)

    async def resolve_live_by_uid(self, uid: str) -> TargetInfo:
        """Resolve a live target from an explicit UID, never from a room number.

        Bilibili UIDs and live room numbers share one numeric space, so guessing
        which one a bare number means subscribes to the wrong person.
        """
        if not uid.isdigit():
            raise BiliAPIError(f"invalid uid: {uid}")
        profile = await self._live_user(uid)
        room_id = str(profile.get("room_id") or "")
        if not room_id or room_id == "0":
            raise BiliAPIError(f"UID {uid} 没有直播间")
        live = await self._live_room(room_id)
        return TargetInfo(
            KIND_LIVE,
            uid,
            name=str(profile.get("info", {}).get("uname") or uid),
            room_id=room_id,
            avatar_url=str(profile.get("info", {}).get("face") or ""),
            is_live=int(live.get("live_status") or 0) == 1,
            last_title=str(live.get("title") or ""),
            last_cover=str(live.get("user_cover") or live.get("cover") or ""),
            live_started_at=live_api.live_start_timestamp(live),
            live_last_seen_at=int(time.time())
            if int(live.get("live_status") or 0) == 1
            else 0,
        )

    async def resolve_live_by_room(self, room_id: str) -> TargetInfo:
        """Resolve a live target from an explicit room number (short ids work).

        The returned TargetInfo carries the room owner's UID; the room_id is the
        long room number.
        """
        if not room_id.isdigit():
            raise BiliAPIError(f"invalid room id: {room_id}")
        live_by_room = await self._live_room(room_id)
        uid = str(live_by_room.get("uid") or "")
        if not uid:
            raise BiliAPIError(f"直播间 {room_id} 不存在")
        profile = await self._live_user(uid)
        name = profile.get("info", {}).get("uname") or live_by_room.get("uname") or uid
        return TargetInfo(
            KIND_LIVE,
            uid,
            name=str(name),
            room_id=str(live_by_room.get("room_id") or room_id),
            avatar_url=str(profile.get("info", {}).get("face") or ""),
            is_live=int(live_by_room.get("live_status") or 0) == 1,
            last_title=str(live_by_room.get("title") or ""),
            last_cover=str(
                live_by_room.get("user_cover") or live_by_room.get("cover") or ""
            ),
            live_started_at=live_api.live_start_timestamp(live_by_room),
            live_last_seen_at=int(time.time())
            if int(live_by_room.get("live_status") or 0) == 1
            else 0,
        )

    async def live_card(self, target: TargetInfo) -> BiliCard:
        live = await self._live_room(target.room_id or target.uid)
        is_live = int(live.get("live_status") or 0) == 1
        # A room preview knows current status, not whether a stream just ended.
        # Only the subscription transition in BiliService emits live_off.
        return BiliCard(
            "live_on" if is_live else "live_idle",
            title=str(live.get("title") or target.last_title or "直播间"),
            author=target.name,
            subtitle="正在直播" if is_live else "当前未开播",
            cover_url=str(
                live.get("user_cover") or live.get("cover") or target.last_cover or ""
            ),
            avatar_url=target.avatar_url,
            url=f"https://live.bilibili.com/{target.room_id or live.get('room_id') or target.uid}",
            badge="LIVE" if is_live else "OFFLINE",
            uid=target.uid,
            room_id=str(target.room_id or live.get("room_id") or ""),
            published_at=int(time.time()),
            live_started_at=live_api.live_start_timestamp(live),
        )

    async def latest_live_state(self, target: TargetInfo) -> TargetInfo:
        card = await self.live_card(target)
        return TargetInfo(
            KIND_LIVE,
            target.uid,
            name=target.name,
            room_id=card.room_id,
            avatar_url=target.avatar_url,
            # Derive the state from card_type so display-only badge copy can never
            # flip the subscription transition that drives notifications and timing.
            is_live=card.card_type == "live_on",
            last_title=card.title,
            last_cover=card.cover_url,
            live_started_at=card.live_started_at,
            live_last_seen_at=card.published_at if card.card_type == "live_on" else 0,
        )

    # --- space --------------------------------------------------------------

    async def user_info(
        self, uid: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return await space_api.user_info(self, uid, deadline=deadline)

    async def _safe_user_info(
        self, uid: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return await space_api.safe_user_info(self, uid, deadline=deadline)

    async def resolve_video_target(self, uid: str) -> TargetInfo:
        if not uid.isdigit():
            raise BiliAPIError(f"invalid uid: {uid}")
        user = await self._safe_user_info(uid)
        latest = await self.latest_video(uid)
        return TargetInfo(
            KIND_VIDEO,
            uid,
            name=str(user.get("name") or latest.author or uid),
            avatar_url=str(user.get("face") or latest.avatar_url or ""),
            latest_id=latest.item_id,
            latest_ts=latest.published_at,
            last_title=latest.title,
            last_cover=latest.cover_url,
            last_desc=latest.description,
        )

    async def resolve_dynamic_target(self, uid: str) -> TargetInfo:
        if not uid.isdigit():
            raise BiliAPIError(f"invalid uid: {uid}")
        user = await self._safe_user_info(uid)
        latest = await self.latest_dynamic(uid)
        return TargetInfo(
            KIND_DYNAMIC,
            uid,
            name=str(user.get("name") or latest.author or uid),
            avatar_url=str(user.get("face") or ""),
            latest_id=latest.item_id,
            latest_ts=latest.published_at,
            last_title=latest.title,
            last_cover=latest.cover_url,
            last_desc=latest.description,
        )

    async def latest_video(
        self, uid: str, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.latest_video(self, uid, deadline=deadline)

    async def video_by_bvid(
        self, bvid: str, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.video_by_bvid(self, bvid, deadline=deadline)

    async def _enrich_video_card(
        self, card: BiliCard, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.enrich_video_card(self, card, deadline=deadline)

    async def _fallback_latest_video(
        self, uid: str, primary_error: Exception, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.fallback_latest_video(
            self, uid, primary_error, deadline=deadline
        )

    async def _dynamic_latest_video(
        self, uid: str, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.dynamic_latest_video(self, uid, deadline=deadline)

    async def latest_dynamic(
        self, uid: str, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.latest_dynamic(self, uid, deadline=deadline)

    async def dynamic_items(
        self, uid: str, *, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return await space_api.dynamic_items(self, uid, deadline=deadline)

    def _bvid_from_card(self, card: BiliCard) -> str:
        return space_api.bvid_from_card(card)

    def _bvid_from_url(self, url: str) -> str:
        return space_api.bvid_from_url(url)

    def _compact_error(self, exc: Exception | None) -> str:
        return space_api.compact_error(exc)

    async def _within(self, label: str, deadline: float | None, awaitable_factory):
        """Run one step under the chain's absolute deadline (a loop.time() value)."""
        return await space_api.within(label, deadline, awaitable_factory)

    # --- RSSHub -------------------------------------------------------------

    async def rsshub_first_item(
        self, route: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return await self._rsshub_first_item(route, deadline=deadline)

    async def _rsshub_first_item(
        self, route: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """Race every configured instance; the first success cancels the rest."""
        return await rsshub_api.first_item(
            self._rsshub_fetch_first_item,
            self.rsshub_base_urls,
            route,
            deadline=deadline,
        )

    async def _rsshub_fetch_first_item(
        self, base_url: str, route: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return await rsshub_api.fetch_first_item(
            self.session, base_url, route, timeout=self.timeout, deadline=deadline
        )

    async def _rsshub_latest_video(
        self, uid: str, primary_error: Exception, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.rsshub_latest_video(
            self, uid, primary_error, deadline=deadline
        )

    async def _rsshub_latest_dynamic(
        self, uid: str, primary_error: Exception, *, deadline: float | None = None
    ) -> BiliCard:
        return await space_api.rsshub_latest_dynamic(
            self, uid, primary_error, deadline=deadline
        )

    def _rss_card_to_dynamic_item(self, card: BiliCard, uid: str) -> dict[str, Any]:
        return space_api.rss_card_to_dynamic_item(card, uid)

    def _parse_rss_first_item(self, xml_text: str) -> dict[str, Any]:
        return rsshub_api.parse_first_item(xml_text)

    def _clean_rsshub_author_title(self, value: str) -> str:
        return rsshub_api.clean_author_title(value)

    def _merge_rsshub_base_urls(self, configured: list[str] | None) -> list[str]:
        return rsshub_api.merge_base_urls(configured)

    # --- links --------------------------------------------------------------

    async def parse_link(self, text: str) -> ParsedLink | None:
        return await links_api.parse_link(self, text)

    async def expand_short_url(self, url: str) -> str:
        return await links_api.expand_short_url(self, url)

    async def card_for_link(self, parsed: ParsedLink) -> BiliCard:
        if parsed.kind == KIND_VIDEO:
            return await self.video_by_bvid(parsed.value)
        if parsed.kind == KIND_LIVE:
            # A live link always carries a room number, never a UID.
            target = await self.resolve_live_by_room(parsed.value)
            return await self.live_card(target)
        raise BiliAPIError(f"unsupported link type: {parsed.kind}")


__all__ = [
    "BiliAPIError",
    "BiliApi",
    "BiliRiskControlError",
    "BiliSession",
    "ParsedLink",
]
