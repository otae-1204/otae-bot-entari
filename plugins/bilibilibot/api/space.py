"""Space APIs: user info, video list, video detail and the dynamic feed.

Every entry point takes the shared absolute deadline (a `loop.time()` value), so
the video fallback chain can never outlive one target's budget. Overridable
steps are dispatched through the caller object, which keeps the historical
`BiliClient` monkeypatch points working.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

from ..models import BiliCard, KIND_DYNAMIC, KIND_VIDEO
from .mapping import (
    bvid_from_card,
    bvid_from_url,
    dynamic_item_to_card,
    video_item_to_card,
    video_view_to_card,
)
from .session import BiliAPIError


USER_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"
VIDEO_LIST_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
VIDEO_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

VIDEO_ROUTE = "/bilibili/user/video/{uid}"
DYNAMIC_ROUTE = "/bilibili/user/dynamic/{uid}"
DYNAMIC_ITEM_LIMIT = 10
COMPACT_ERROR_LIMIT = 220
# Spelled out in every source-failure summary so the user knows the knobs.
FALLBACK_HINT = "可配置 BILI_SESSDATA/BILI_BUVID3 或 BILI_DM_IMG_* 提高稳定性"


def compact_error(exc: Exception | None) -> str:
    if exc is None:
        return ""
    text = " ".join(str(exc).split())
    return (
        text
        if len(text) <= COMPACT_ERROR_LIMIT
        else text[: COMPACT_ERROR_LIMIT - 3] + "..."
    )


async def within(
    label: str, deadline: float | None, awaitable_factory: Callable[[], Awaitable[Any]]
):
    """Run one step under the chain's absolute deadline (a loop.time() value)."""
    if deadline is None:
        return await awaitable_factory()
    try:
        async with asyncio.timeout_at(deadline):
            return await awaitable_factory()
    except TimeoutError as exc:
        raise BiliAPIError(f"Bilibili deadline exceeded: {label}") from exc


async def user_info(
    api: Any, uid: str, *, deadline: float | None = None
) -> dict[str, Any]:
    await api.ensure_wbi_keys()
    label = f"user {uid}"
    data = await api._within(
        label,
        deadline,
        lambda: api._get_json_with_risk_retry(
            USER_INFO_URL,
            label,
            params=api.sign({"mid": uid}),
            sign_params={"mid": uid},
        ),
    )
    return data.get("data") or {}


async def safe_user_info(
    api: Any, uid: str, *, deadline: float | None = None
) -> dict[str, Any]:
    """Name and avatar are decoration: never fail a subscription over them."""
    try:
        return await user_info(api, uid, deadline=deadline)
    except BiliAPIError as exc:
        logger.debug(f"[bilibilibot] user info fallback for {uid}: {exc}")
        return {}


async def latest_video(
    api: Any, uid: str, *, deadline: float | None = None
) -> BiliCard:
    await api.ensure_wbi_keys()
    raw_params = {
        "mid": uid,
        "ps": 1,
        "tid": 0,
        "pn": 1,
        "order": "pubdate",
        "jsonp": "jsonp",
        **api.dm_img_params,
    }
    label = f"video list {uid}"
    try:
        data = await api._within(
            label,
            deadline,
            lambda: api._get_json_with_risk_retry(
                VIDEO_LIST_URL,
                label,
                params=api.sign(raw_params),
                sign_params=raw_params,
            ),
        )
    except BiliAPIError as primary_error:
        return await api._fallback_latest_video(uid, primary_error, deadline=deadline)
    vlist = ((data.get("data") or {}).get("list") or {}).get("vlist") or []
    if not vlist:
        return BiliCard(KIND_VIDEO, "暂无视频", uid=uid)
    return await api._enrich_video_card(
        video_item_to_card(vlist[0], uid), deadline=deadline
    )


async def video_by_bvid(
    api: Any, bvid: str, *, deadline: float | None = None
) -> BiliCard:
    data = api._require_ok(
        await api._within(
            f"video {bvid}",
            deadline,
            lambda: api._get_json(VIDEO_VIEW_URL, params={"bvid": bvid}),
        ),
        f"video {bvid}",
    )
    return video_view_to_card(data.get("data") or {}, bvid)


async def enrich_video_card(
    api: Any, card: BiliCard, *, deadline: float | None = None
) -> BiliCard:
    """Fill the gaps of a list/RSS card from the video detail endpoint."""
    bvid = card.item_id or bvid_from_url(card.url)
    if not bvid:
        return card
    try:
        detail = await api.video_by_bvid(bvid, deadline=deadline)
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


async def fallback_latest_video(
    api: Any, uid: str, primary_error: Exception, *, deadline: float | None = None
) -> BiliCard:
    """Primary -> video RSSHub -> video inside the dynamic feed.

    Every step is bounded by the shared absolute deadline, so the whole chain
    can never outlive one target's budget.
    """
    video_rss_error: Exception | None = None
    try:
        return await api._within(
            f"video rsshub {uid}",
            deadline,
            lambda: api._rsshub_latest_video(uid, primary_error, deadline=deadline),
        )
    except Exception as exc:
        video_rss_error = exc
        logger.debug(f"[bilibilibot] video RSSHub fallback failed for {uid}: {exc}")

    try:
        return await api._within(
            f"video dynamic fallback {uid}",
            deadline,
            lambda: api._dynamic_latest_video(uid, deadline=deadline),
        )
    except Exception as dynamic_error:
        raise BiliAPIError(
            "video sources failed: "
            f"primary={compact_error(primary_error)}; "
            f"video_rss={compact_error(video_rss_error)}; "
            f"dynamic={compact_error(dynamic_error)}; "
            f"{FALLBACK_HINT}"
        ) from dynamic_error


async def dynamic_latest_video(
    api: Any, uid: str, *, deadline: float | None = None
) -> BiliCard:
    """Newest video posted as a dynamic, used when both other sources are down."""
    items = await api.dynamic_items(uid, deadline=deadline)
    cards = [
        dynamic_item_to_card(item, uid) for item in items[:DYNAMIC_ITEM_LIMIT]
    ]
    video_cards = [card for card in cards if bvid_from_card(card)]
    if not video_cards:
        raise BiliAPIError(f"dynamic fallback found no video item for {uid}")
    dynamic_card = max(video_cards, key=lambda item: item.published_at)
    bvid = bvid_from_card(dynamic_card)
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
    return await api._enrich_video_card(card, deadline=deadline)


async def latest_dynamic(
    api: Any, uid: str, *, deadline: float | None = None
) -> BiliCard:
    items = await api.dynamic_items(uid, deadline=deadline)
    if not items:
        return BiliCard(KIND_DYNAMIC, "暂无动态", uid=uid)
    return dynamic_item_to_card(items[0], uid)


async def dynamic_items(
    api: Any, uid: str, *, deadline: float | None = None
) -> list[dict[str, Any]]:
    label = f"dynamic {uid}"
    try:
        data = await api._within(
            label,
            deadline,
            lambda: api._get_json_with_risk_retry(
                DYNAMIC_URL,
                label,
                params={"host_mid": uid, "timezone_offset": -480},
            ),
        )
        return list((data.get("data") or {}).get("items") or [])
    except BiliAPIError as primary_error:
        # Bound to a fresh name: the except target is unbound when the block ends,
        # which would break the deferred lambda below.
        reason = primary_error
        card = await api._within(
            f"dynamic rsshub {uid}",
            deadline,
            lambda: api._rsshub_latest_dynamic(uid, reason, deadline=deadline),
        )
        return [api._rss_card_to_dynamic_item(card, uid)]


async def rsshub_latest_video(
    api: Any, uid: str, primary_error: Exception, *, deadline: float | None = None
) -> BiliCard:
    try:
        item = await api.rsshub_first_item(
            VIDEO_ROUTE.format(uid=uid), deadline=deadline
        )
    except Exception as rss_error:
        raise BiliAPIError(
            f"{primary_error}; RSSHub fallback failed: {rss_error}; {FALLBACK_HINT}"
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
        item_id=bvid_from_url(item["link"])
        or item["link"].rstrip("/").rsplit("/", 1)[-1],
        published_at=item["published_at"],
    )
    return await api._enrich_video_card(card, deadline=deadline)


async def rsshub_latest_dynamic(
    api: Any, uid: str, primary_error: Exception, *, deadline: float | None = None
) -> BiliCard:
    try:
        item = await api.rsshub_first_item(
            DYNAMIC_ROUTE.format(uid=uid), deadline=deadline
        )
    except Exception as rss_error:
        raise BiliAPIError(
            f"{primary_error}; RSSHub fallback failed: {rss_error}; {FALLBACK_HINT}"
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


def rss_card_to_dynamic_item(card: BiliCard, uid: str) -> dict[str, Any]:
    """Present an RSS card in the dynamic-feed shape so one mapper serves both."""
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
                    "draw": {"items": [{"src": card.cover_url}]}
                    if card.cover_url
                    else {"items": []},
                },
                "additional": None,
            },
        },
    }


__all__ = [
    "DYNAMIC_ITEM_LIMIT",
    "compact_error",
    "dynamic_items",
    "dynamic_latest_video",
    "dynamic_item_to_card",
    "enrich_video_card",
    "fallback_latest_video",
    "latest_dynamic",
    "latest_video",
    "rss_card_to_dynamic_item",
    "rsshub_latest_dynamic",
    "rsshub_latest_video",
    "safe_user_info",
    "user_info",
    "video_by_bvid",
    "within",
]
