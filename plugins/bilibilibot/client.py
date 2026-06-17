from __future__ import annotations

import re
import time
import json
import uuid
import html
import asyncio
import email.utils
import xml.etree.ElementTree as ET
from random import randint
from dataclasses import dataclass
from hashlib import md5
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from loguru import logger

from .models import BiliCard, KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, TargetInfo


class BiliAPIError(Exception):
    pass


class BiliRiskControlError(BiliAPIError):
    def __init__(self, label: str):
        super().__init__(
            f"B站风控校验失败，已尝试临时 Cookie；可配置 BILI_SESSDATA/BILI_BUVID3 提高稳定性 ({label})"
        )


@dataclass(slots=True)
class ParsedLink:
    kind: str
    value: str
    url: str


BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
LIVE_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d+)")
DEFAULT_RSSHUB_BASE_URLS = [
    "https://rss.materium.io",
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.moeyy.cn",
    "https://rsshub.ktachibana.party",
]


class BiliClient:
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    USER_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"
    VIDEO_LIST_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
    VIDEO_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
    LIVE_ROOM_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
    LIVE_USER_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"
    DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    RISK_COOKIE_URL = "https://www.bilibili.com/1/dynamic"
    RISK_GATEWAY_URL = "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi"

    _mixin_key_table = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52,
    ]

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
    ):
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "application/json, text/plain, */*",
        }
        self.dm_img_params = {
            key: value
            for key, value in {
                "dm_img_list": dm_img_list,
                "dm_img_str": dm_img_str,
                "dm_cover_img_str": dm_cover_img_str,
            }.items()
            if value
        }
        self.rsshub_base_urls = self._merge_rsshub_base_urls(rsshub_base_urls)
        self.cookies = httpx.Cookies()
        self._login_cookies = httpx.Cookies()
        if sessdata:
            self._login_cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        if buvid3:
            self._login_cookies.set("buvid3", buvid3, domain=".bilibili.com")
        self.cookies.update(self._login_cookies)
        self.img_key = ""
        self.sub_key = ""
        self._wbi_updated_at = 0
        self._risk_cookie_updated_at = 0

    async def refresh_wbi_keys(self) -> None:
        data = await self._get_json(self.NAV_URL)
        img = data.get("data", {}).get("wbi_img", {})
        img_url = img.get("img_url", "")
        sub_url = img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BiliAPIError("Bilibili nav response did not include WBI keys")
        self.img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        self.sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._wbi_updated_at = int(time.time())

    async def ensure_wbi_keys(self) -> None:
        if self.img_key and self.sub_key and int(time.time()) - self._wbi_updated_at < 3600:
            return
        await self.refresh_wbi_keys()

    async def refresh_risk_cookies(self) -> None:
        headers = {
            **self.headers,
            "Host": "space.bilibili.com",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        temp_cookies = httpx.Cookies(
            {"_uuid": f"{str(uuid.uuid4()).upper()}{randint(0, 99999):05d}infoc"}
        )
        async with httpx.AsyncClient(headers=headers, timeout=self.timeout, follow_redirects=True, trust_env=False) as client:
            response = await client.get(self.RISK_COOKIE_URL, cookies=temp_cookies)
            response.raise_for_status()
            temp_cookies.update(response.cookies)
            spm_match = re.search(r'<meta name="spm_prefix" content="([^"]+?)">', response.text)
            spm_prefix = spm_match.group(1) if spm_match else "333.999"
            payload = {
                "3064": 1,
                "39c8": f"{spm_prefix}.fp.risk",
                "3c43": {"adca": "Linux"},
            }
            try:
                gateway = await client.post(
                    self.RISK_GATEWAY_URL,
                    cookies=temp_cookies,
                    json={"payload": json.dumps(payload, separators=(",", ":"))},
                )
                gateway.raise_for_status()
                temp_cookies.update(gateway.cookies)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.debug("[bilibilibot] ExClimbWuzhi gateway returned 404; continuing with page cookies")
                else:
                    raise
        self.cookies.clear()
        self.cookies.update(temp_cookies)
        self.cookies.update(self._login_cookies)
        self._risk_cookie_updated_at = int(time.time())

    async def ensure_risk_cookies(self) -> None:
        if self.cookies and int(time.time()) - self._risk_cookie_updated_at < 6 * 3600:
            return
        await self.refresh_risk_cookies()

    async def user_info(self, uid: str) -> dict[str, Any]:
        await self.ensure_wbi_keys()
        data = await self._get_json_with_risk_retry(
            self.USER_INFO_URL,
            f"user {uid}",
            params=self._wbi_sign({"mid": uid}),
            sign_params={"mid": uid},
        )
        return data.get("data") or {}

    async def resolve_live_target(self, value: str) -> TargetInfo:
        if not value.isdigit():
            raise BiliAPIError(f"invalid live uid/room id: {value}")
        live_by_room = await self._live_room(value)
        if live_by_room and str(live_by_room.get("uid") or ""):
            uid = str(live_by_room["uid"])
            room_id = str(live_by_room.get("room_id") or value)
            profile = await self._live_user(uid)
            name = profile.get("info", {}).get("uname") or live_by_room.get("uname") or uid
            return TargetInfo(
                KIND_LIVE,
                uid,
                name=str(name),
                room_id=room_id,
                avatar_url=str(profile.get("info", {}).get("face") or ""),
                is_live=int(live_by_room.get("live_status") or 0) == 1,
                last_title=str(live_by_room.get("title") or ""),
                last_cover=str(live_by_room.get("user_cover") or live_by_room.get("cover") or ""),
            )

        profile = await self._live_user(value)
        room_id = str(profile.get("room_id") or "")
        if not room_id or room_id == "0":
            raise BiliAPIError(f"user {value} has no live room")
        live = await self._live_room(room_id)
        return TargetInfo(
            KIND_LIVE,
            value,
            name=str(profile.get("info", {}).get("uname") or value),
            room_id=room_id,
            avatar_url=str(profile.get("info", {}).get("face") or ""),
            is_live=int(live.get("live_status") or 0) == 1,
            last_title=str(live.get("title") or ""),
            last_cover=str(live.get("user_cover") or live.get("cover") or ""),
        )

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

    async def live_card(self, target: TargetInfo) -> BiliCard:
        live = await self._live_room(target.room_id or target.uid)
        is_live = int(live.get("live_status") or 0) == 1
        return BiliCard(
            "live_on" if is_live else "live_off",
            title=str(live.get("title") or target.last_title or "直播间"),
            author=target.name,
            subtitle="正在直播" if is_live else "当前未开播",
            cover_url=str(live.get("user_cover") or live.get("cover") or target.last_cover or ""),
            avatar_url=target.avatar_url,
            url=f"https://live.bilibili.com/{target.room_id or live.get('room_id') or target.uid}",
            badge="LIVE" if is_live else "ENDED",
            uid=target.uid,
            room_id=str(target.room_id or live.get("room_id") or ""),
            published_at=int(time.time()),
        )

    async def latest_live_state(self, target: TargetInfo) -> TargetInfo:
        card = await self.live_card(target)
        return TargetInfo(
            KIND_LIVE,
            target.uid,
            name=target.name,
            room_id=card.room_id,
            avatar_url=target.avatar_url,
            is_live=card.badge == "LIVE",
            last_title=card.title,
            last_cover=card.cover_url,
        )

    async def latest_video(self, uid: str) -> BiliCard:
        await self.ensure_wbi_keys()
        raw_params = {
            "mid": uid,
            "ps": 1,
            "tid": 0,
            "pn": 1,
            "order": "pubdate",
            "jsonp": "jsonp",
            **self.dm_img_params,
        }
        try:
            data = await self._get_json_with_risk_retry(
                self.VIDEO_LIST_URL,
                f"video list {uid}",
                params=self._wbi_sign(raw_params),
                sign_params=raw_params,
            )
        except BiliAPIError as primary_error:
            return await self._fallback_latest_video(uid, primary_error)
        vlist = (((data.get("data") or {}).get("list") or {}).get("vlist") or [])
        if not vlist:
            return BiliCard(KIND_VIDEO, "暂无视频", uid=uid)
        item = vlist[0]
        bvid = str(item.get("bvid") or "")
        card = BiliCard(
            KIND_VIDEO,
            title=str(item.get("title") or ""),
            description=str(item.get("description") or item.get("desc") or ""),
            cover_url=str(item.get("pic") or ""),
            url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            badge="VIDEO",
            uid=uid,
            item_id=bvid,
            published_at=int(item.get("created") or 0),
        )
        return await self._enrich_video_card(card)

    async def video_by_bvid(self, bvid: str) -> BiliCard:
        data = self._require_ok(await self._get_json(self.VIDEO_VIEW_URL, params={"bvid": bvid}), f"video {bvid}")
        item = data.get("data") or {}
        owner = item.get("owner") or {}
        return BiliCard(
            KIND_VIDEO,
            title=str(item.get("title") or ""),
            author=str(owner.get("name") or ""),
            description=str(item.get("desc") or ""),
            cover_url=str(item.get("pic") or ""),
            avatar_url=str(owner.get("face") or ""),
            url=f"https://www.bilibili.com/video/{bvid}",
            badge="VIDEO",
            uid=str(owner.get("mid") or ""),
            item_id=bvid,
            published_at=int(item.get("pubdate") or 0),
        )

    async def _enrich_video_card(self, card: BiliCard) -> BiliCard:
        bvid = card.item_id or self._bvid_from_url(card.url)
        if not bvid:
            return card
        try:
            detail = await self.video_by_bvid(bvid)
        except Exception as exc:
            logger.debug(f"[bilibilibot] video detail fallback for {bvid}: {exc}")
            return card
        card.title = card.title or detail.title
        card.author = card.author or detail.author
        card.description = card.description or detail.description
        card.cover_url = card.cover_url or detail.cover_url
        card.avatar_url = card.avatar_url or detail.avatar_url
        card.url = card.url or detail.url
        card.uid = card.uid or detail.uid
        card.item_id = card.item_id or detail.item_id
        card.published_at = card.published_at or detail.published_at
        return card

    async def _fallback_latest_video(self, uid: str, primary_error: Exception) -> BiliCard:
        video_rss_error: Exception | None = None
        try:
            return await self._rsshub_latest_video(uid, primary_error)
        except Exception as exc:
            video_rss_error = exc
            logger.debug(f"[bilibilibot] video RSSHub fallback failed for {uid}: {exc}")

        try:
            return await self._dynamic_latest_video(uid)
        except Exception as dynamic_error:
            raise BiliAPIError(
                "video sources failed: "
                f"primary={self._compact_error(primary_error)}; "
                f"video_rss={self._compact_error(video_rss_error)}; "
                f"dynamic={self._compact_error(dynamic_error)}; "
                "可配置 BILI_SESSDATA/BILI_BUVID3 或 BILI_DM_IMG_* 提高稳定性"
            ) from dynamic_error

    async def _dynamic_latest_video(self, uid: str) -> BiliCard:
        items = await self.dynamic_items(uid)
        cards = [self._dynamic_card_from_item(item, uid) for item in items[:10]]
        video_cards = [card for card in cards if self._bvid_from_card(card)]
        if not video_cards:
            raise BiliAPIError(f"dynamic fallback found no video item for {uid}")
        dynamic_card = max(video_cards, key=lambda item: item.published_at)
        bvid = self._bvid_from_card(dynamic_card)
        card = BiliCard(
            KIND_VIDEO,
            title=dynamic_card.title,
            author=dynamic_card.author,
            description=dynamic_card.description,
            cover_url=dynamic_card.cover_url,
            avatar_url=dynamic_card.avatar_url,
            url=f"https://www.bilibili.com/video/{bvid}",
            badge="VIDEO",
            uid=uid,
            item_id=bvid,
            published_at=dynamic_card.published_at,
        )
        return await self._enrich_video_card(card)

    def _bvid_from_card(self, card: BiliCard) -> str:
        for value in (card.item_id, card.url, card.description):
            bvid = self._bvid_from_url(value or "")
            if bvid:
                return bvid
        return ""

    def _compact_error(self, exc: Exception | None) -> str:
        if exc is None:
            return ""
        text = " ".join(str(exc).split())
        return text if len(text) <= 220 else text[:217] + "..."

    async def latest_dynamic(self, uid: str) -> BiliCard:
        items = await self.dynamic_items(uid)
        if not items:
            return BiliCard(KIND_DYNAMIC, "暂无动态", uid=uid)
        return self._dynamic_card_from_item(items[0], uid)

    async def dynamic_items(self, uid: str) -> list[dict[str, Any]]:
        try:
            data = await self._get_json_with_risk_retry(
                self.DYNAMIC_URL,
                f"dynamic {uid}",
                params={"host_mid": uid, "timezone_offset": -480},
            )
            return list((data.get("data") or {}).get("items") or [])
        except BiliAPIError as primary_error:
            return [self._rss_card_to_dynamic_item(await self._rsshub_latest_dynamic(uid, primary_error), uid)]

    def _dynamic_card_from_item(self, item: dict[str, Any], uid: str) -> BiliCard:
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        dynamic = modules.get("module_dynamic") or {}
        desc = dynamic.get("desc") or {}
        major = dynamic.get("major") or {}
        additional = dynamic.get("additional") or {}
        title = "发布了新动态"
        cover = ""
        url = f"https://t.bilibili.com/{item.get('id_str')}"

        if major.get("type") == "MAJOR_TYPE_DRAW":
            images = ((major.get("draw") or {}).get("items") or [])
            cover = str(images[0].get("src") or "") if images else ""
        elif major.get("type") == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            title = str(article.get("title") or title)
            covers = article.get("covers") or []
            cover = str(covers[0]) if covers else ""
            url = str(article.get("jump_url") or url)
        elif major.get("type") == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            title = str(archive.get("title") or title)
            cover = str(archive.get("cover") or "")
            url = str(archive.get("jump_url") or archive.get("url") or url)
        elif additional.get("type") == "ADDITIONAL_TYPE_UGC":
            ugc = additional.get("ugc") or {}
            title = str(ugc.get("title") or title)
            cover = str(ugc.get("cover") or "")
            url = str(ugc.get("jump_url") or url)

        text = str(desc.get("text") or "")
        if text and title == "发布了新动态":
            title = text.splitlines()[0][:40] or title
        return BiliCard(
            KIND_DYNAMIC,
            title=title,
            author=str(author.get("name") or ""),
            description=text,
            cover_url=cover,
            avatar_url=str(author.get("face") or ""),
            url=url,
            badge="DYNAMIC",
            uid=uid,
            item_id=self._bvid_from_url(url) or str(item.get("id_str") or ""),
            published_at=int(author.get("pub_ts") or 0),
        )

    async def parse_link(self, text: str) -> ParsedLink | None:
        for raw in URL_RE.findall(text):
            url = raw.rstrip("),，。]")
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            if "b23.tv" in host:
                url = await self.expand_short_url(url)
                parsed = urlparse(url)
                host = parsed.netloc.lower()
            if "live.bilibili.com" in host:
                match = LIVE_RE.search(url)
                if match:
                    return ParsedLink(KIND_LIVE, match.group(1), url)
            if "bilibili.com" in host:
                bvid = self._bvid_from_url(url)
                if bvid:
                    return ParsedLink(KIND_VIDEO, bvid, url)
        bvid_match = BV_RE.search(text)
        if bvid_match:
            bvid = bvid_match.group(0)
            return ParsedLink(KIND_VIDEO, bvid, f"https://www.bilibili.com/video/{bvid}")
        return None

    async def expand_short_url(self, url: str) -> str:
        async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                return resp.headers["location"]
            if resp.next_request is not None:
                return str(resp.next_request.url)
            return str(resp.url)

    def _bvid_from_url(self, url: str) -> str:
        match = BV_RE.search(url)
        if match:
            return match.group(0)
        qs = parse_qs(urlparse(url).query)
        bvid = qs.get("bvid", [""])[0]
        return bvid if BV_RE.fullmatch(bvid) else ""

    async def card_for_link(self, parsed: ParsedLink) -> BiliCard:
        if parsed.kind == KIND_VIDEO:
            return await self.video_by_bvid(parsed.value)
        if parsed.kind == KIND_LIVE:
            target = await self.resolve_live_target(parsed.value)
            return await self.live_card(target)
        raise BiliAPIError(f"unsupported link type: {parsed.kind}")

    async def _live_room(self, room_id: str) -> dict[str, Any]:
        data = await self._get_json(self.LIVE_ROOM_URL, params={"room_id": room_id})
        if data.get("code") == 1:
            return {}
        return self._require_ok(data, f"live room {room_id}").get("data") or {}

    async def _live_user(self, uid: str) -> dict[str, Any]:
        data = self._require_ok(await self._get_json(self.LIVE_USER_URL, params={"uid": uid}), f"live user {uid}")
        return data.get("data") or {}

    async def _get_json(self, url: str, *, params: dict[str, Any] | None = None, cookies: httpx.Cookies | None = None) -> dict[str, Any]:
        try:
            request_cookies = cookies if cookies is not None else self.cookies
            async with httpx.AsyncClient(headers=self.headers, cookies=request_cookies, timeout=self.timeout, follow_redirects=True, trust_env=False) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as exc:
            raise BiliAPIError(f"Bilibili request timeout: {url}") from exc
        except httpx.HTTPStatusError as exc:
            raise BiliAPIError(f"Bilibili HTTP {exc.response.status_code}: {url}") from exc
        except ValueError as exc:
            raise BiliAPIError(f"Bilibili response is not JSON: {url}") from exc
        except httpx.HTTPError as exc:
            raise BiliAPIError(f"Bilibili request failed: {exc}") from exc

    async def _get_json_with_risk_retry(
        self,
        url: str,
        label: str,
        *,
        params: dict[str, Any] | None = None,
        sign_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

    async def _safe_user_info(self, uid: str) -> dict[str, Any]:
        try:
            return await self.user_info(uid)
        except BiliAPIError as exc:
            logger.debug(f"[bilibilibot] user info fallback for {uid}: {exc}")
            return {}

    async def _rsshub_latest_video(self, uid: str, primary_error: Exception) -> BiliCard:
        try:
            item = await self._rsshub_first_item(f"/bilibili/user/video/{uid}")
        except Exception as rss_error:
            raise BiliAPIError(
                f"{primary_error}; RSSHub fallback failed: {rss_error}; "
                "可配置 BILI_SESSDATA/BILI_BUVID3 或 BILI_DM_IMG_* 提高稳定性"
            ) from rss_error
        card = BiliCard(
            KIND_VIDEO,
            title=item["title"] or "Bilibili video",
            author=item["author"],
            description=item["description"],
            cover_url=item["cover_url"],
            url=item["link"],
            badge="VIDEO",
            uid=uid,
            item_id=self._bvid_from_url(item["link"]) or item["link"].rstrip("/").rsplit("/", 1)[-1],
            published_at=item["published_at"],
        )
        return await self._enrich_video_card(card)

    async def _rsshub_latest_dynamic(self, uid: str, primary_error: Exception) -> BiliCard:
        try:
            item = await self._rsshub_first_item(f"/bilibili/user/dynamic/{uid}")
        except Exception as rss_error:
            raise BiliAPIError(
                f"{primary_error}; RSSHub fallback failed: {rss_error}; "
                "可配置 BILI_SESSDATA/BILI_BUVID3 或 BILI_DM_IMG_* 提高稳定性"
            ) from rss_error
        return BiliCard(
            KIND_DYNAMIC,
            title=item["title"] or "Bilibili dynamic",
            author=item["author"],
            description=item["description"],
            cover_url=item["cover_url"],
            url=item["link"],
            badge="DYNAMIC",
            uid=uid,
            item_id=item["link"].rstrip("/").rsplit("/", 1)[-1],
            published_at=item["published_at"],
        )

    async def _rsshub_first_item(self, route: str) -> dict[str, Any]:
        if not self.rsshub_base_urls:
            raise BiliAPIError("all RSSHub instances unavailable: no RSSHub base urls configured")

        tasks = [
            asyncio.create_task(self._rsshub_fetch_first_item(base_url, route))
            for base_url in self.rsshub_base_urls
        ]
        errors: list[str] = []
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        result = task.result()
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                    for rest in pending:
                        rest.cancel()
                    logger.debug(f"[bilibilibot] RSSHub fallback succeeded via {result['base_url']}{route}")
                    return result["item"]
            compact = "; ".join(errors[:3])
            if len(errors) > 3:
                compact += f"; ... and {len(errors) - 3} more"
            raise BiliAPIError(f"all RSSHub instances unavailable: {compact}")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _rsshub_fetch_first_item(self, base_url: str, route: str) -> dict[str, Any]:
        url = base_url.rstrip("/") + route
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=min(self.timeout, 8), follow_redirects=True, trust_env=False) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            return {"base_url": base_url.rstrip("/"), "item": self._parse_rss_first_item(resp.text)}
        except Exception as exc:
            logger.debug(f"[bilibilibot] RSSHub fallback failed for {url}: {exc}")
            raise BiliAPIError(f"{base_url.rstrip('/')}: {exc}") from exc

    def _parse_rss_first_item(self, xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        channel_title = self._clean_rsshub_author_title(self._first_text(root, ".//channel/title"))
        item = root.find(".//item")
        if item is not None:
            description_html = self._first_text(item, "description") or self._first_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
            return {
                "title": self._first_text(item, "title"),
                "link": self._first_text(item, "link"),
                "description": self._html_to_text(description_html),
                "cover_url": self._extract_image_url(description_html),
                "published_at": self._parse_rss_time(self._first_text(item, "pubDate")),
                "author": channel_title,
            }

        atom_ns = "{http://www.w3.org/2005/Atom}"
        entry = root.find(f".//{atom_ns}entry")
        if entry is not None:
            description_html = self._first_text(entry, f"{atom_ns}summary") or self._first_text(entry, f"{atom_ns}content")
            link_el = entry.find(f"{atom_ns}link")
            return {
                "title": self._first_text(entry, f"{atom_ns}title"),
                "link": (link_el.get("href") if link_el is not None else ""),
                "description": self._html_to_text(description_html),
                "cover_url": self._extract_image_url(description_html),
                "published_at": self._parse_rss_time(self._first_text(entry, f"{atom_ns}updated") or self._first_text(entry, f"{atom_ns}published")),
                "author": self._clean_rsshub_author_title(self._first_text(root, f"{atom_ns}title")),
            }
        raise BiliAPIError("RSSHub feed contains no item")

    def _rss_card_to_dynamic_item(self, card: BiliCard, uid: str) -> dict[str, Any]:
        return {
            "id_str": card.item_id or card.url.rstrip("/").rsplit("/", 1)[-1],
            "modules": {
                "module_author": {
                    "name": card.author,
                    "face": card.avatar_url,
                    "pub_ts": card.published_at,
                },
                "module_dynamic": {
                    "desc": {"text": card.description or card.title},
                    "major": {
                        "type": "MAJOR_TYPE_DRAW",
                        "draw": {"items": [{"src": card.cover_url}]} if card.cover_url else {"items": []},
                    },
                    "additional": None,
                },
            },
        }

    def _first_text(self, element: ET.Element, path: str) -> str:
        found = element.find(path)
        return (found.text or "").strip() if found is not None and found.text else ""

    def _html_to_text(self, value: str) -> str:
        value = html.unescape(value or "")
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", "", value)
        return " ".join(value.split())

    def _extract_image_url(self, value: str) -> str:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', value or "", flags=re.I)
        return html.unescape(match.group(1)) if match else ""

    def _parse_rss_time(self, value: str) -> int:
        if not value:
            return 0
        try:
            return int(email.utils.parsedate_to_datetime(value).timestamp())
        except Exception:
            try:
                return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                return 0

    def _clean_rsshub_author_title(self, value: str) -> str:
        text = " ".join(str(value or "").split())
        suffixes = [
            " 的 Bilibili 投稿视频",
            " 的 bilibili 投稿视频",
            " 的 B站投稿视频",
            " 的 Bilibili 视频",
            " 的 bilibili 视频",
            " 的 B站视频",
            " 的 Bilibili 动态",
            " 的 bilibili 动态",
            " 的 B站动态",
        ]
        for suffix in suffixes:
            if text.endswith(suffix):
                return text[: -len(suffix)].strip()
        return text

    def _merge_rsshub_base_urls(self, configured: list[str] | None) -> list[str]:
        result: list[str] = []
        for item in [*(configured or []), *DEFAULT_RSSHUB_BASE_URLS]:
            normalized = item.strip().rstrip("/")
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def _require_ok(self, data: dict[str, Any], label: str) -> dict[str, Any]:
        code = data.get("code", 0)
        if code == -352:
            raise BiliRiskControlError(label)
        if code != 0:
            raise BiliAPIError(f"Bilibili API error for {label}: {code} {data.get('message') or data.get('msg') or ''}")
        return data

    def _wbi_sign(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.img_key or not self.sub_key:
            return dict(params)
        mixin = "".join((self.img_key + self.sub_key)[i] for i in self._mixin_key_table)[:32]
        signed = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
        signed["wts"] = int(time.time())
        query = urlencode(dict(sorted(signed.items())))
        signed["w_rid"] = md5((query + mixin).encode()).hexdigest()
        return signed
