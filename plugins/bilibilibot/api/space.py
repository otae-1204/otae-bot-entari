"""Space APIs: user info, video list, video detail and the dynamic feed.

Every entry point takes the shared absolute deadline (a `loop.time()` value), so
the video fallback chain can never outlive one target's budget. Overridable
steps are dispatched through the caller object, which keeps the historical
`BiliClient` monkeypatch points working.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from loguru import logger

from ..models import BiliCard, KIND_DYNAMIC, KIND_VIDEO
from ..official import ENDFIELD_OFFICIAL_UID
from .mapping import (
    bvid_from_card,
    bvid_from_url,
    dynamic_item_to_card,
    video_item_to_card,
    video_view_to_card,
)
from .session import BiliAPIError, DynamicDetailUnavailable, DynamicEnrichDeferred


USER_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"
VIDEO_LIST_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
VIDEO_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
DYNAMIC_DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
# Business codes that mean the dynamic can never be shown: deleted, hidden, or
# an id that does not exist. Retrying these as transient failures would defer
# the whole window forever and block the UP's newer posts behind it.
# 4101105 is deliberately absent: public API docs describe it as a retryable
# "please refresh" error, so it goes through the repeated-failure release path
# instead of being cached as permanently invisible.
DYNAMIC_UNAVAILABLE_CODES = frozenset({-404, 4101131})

VIDEO_ROUTE = "/bilibili/user/video/{uid}"
DYNAMIC_ROUTE = "/bilibili/user/dynamic/{uid}"
DYNAMIC_ITEM_LIMIT = 10
DYNAMIC_DETAIL_LIMIT = 5
# One poll round may spend at most this long on detail lookups; a window that
# cannot finish inside it defers as a whole (see DynamicEnrichDeferred). Kept
# well under the 30 s per-target timeout so the deferral decision is ours.
DYNAMIC_ENRICH_BUDGET_SECONDS = 10.0
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
    """Newest video posted as a dynamic, used when both other sources are down.

    Reads the raw feed only: enrichment calls must never decide which item the
    subscription baseline sees.
    """
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
    """Subscription baseline: the newest raw feed item, never an enriched view.

    No detail call happens here. If a transient failure could drop the newest
    shell from this lookup, the baseline would fall back to an older post and
    everything in between would be backfilled as new.
    """
    items = await api.dynamic_items(uid, deadline=deadline)
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return BiliCard(KIND_DYNAMIC, "暂无动态", uid=uid)
    # 置顶或旧视频动态可能排在列表首位；订阅基线按发布时间取最新。
    return dynamic_item_to_card(max(candidates, key=_dynamic_pub_ts), uid)


async def dynamic_items(
    api: Any, uid: str, *, deadline: float | None = None
) -> list[dict[str, Any]]:
    """One raw feed page: enrichment is the poller's job, never this call's.

    Keeping the feed raw is what protects the subscription baseline: baseline
    lookups (`latest_dynamic`, `dynamic_latest_video`) must see every item even
    when its detail is temporarily unavailable.
    """
    label = f"dynamic {uid}"
    try:
        data = await api._within(
            label,
            deadline,
            lambda: api._get_json_with_risk_retry(
                DYNAMIC_URL,
                label,
                params={
                    "host_mid": uid,
                    "timezone_offset": -480,
                    **({"features": "itemOpusStyle"} if uid == ENDFIELD_OFFICIAL_UID else {}),
                },
            ),
        )
        items = list((data.get("data") or {}).get("items") or [])
        if uid == ENDFIELD_OFFICIAL_UID:
            for item in items:
                validate_official_dynamic(item)
        return items
    except BiliAPIError as primary_error:
        if uid == ENDFIELD_OFFICIAL_UID:
            # RSS 只做发现：拿到 ID 后回查 detail，重建带正文与转发的原始结构。
            try:
                return await api._within(
                    "endfield dynamic fallback",
                    deadline,
                    lambda: official_rss_dynamics(api, deadline=deadline),
                )
            except Exception as fallback_error:
                raise BiliAPIError(
                    "终末地官号动态采集失败，保留进度等待重试："
                    f"{compact_error(primary_error)}; {compact_error(fallback_error)}"
                ) from fallback_error
        # Bound to a fresh name: the except target is unbound when the block ends,
        # which would break the deferred lambda below.
        reason = primary_error
        card = await api._within(
            f"dynamic rsshub {uid}",
            deadline,
            lambda: api._rsshub_latest_dynamic(uid, reason, deadline=deadline),
        )
        return [api._rss_card_to_dynamic_item(card, uid)]


def _dynamic_pub_ts(item: dict[str, Any]) -> int:
    author = (item.get("modules") or {}).get("module_author") or {}
    try:
        return int(author.get("pub_ts") or 0)
    except (TypeError, ValueError):
        return 0


def _carry_feed_pub_ts(shell: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Keep the feed's publish time when the detail omits or backdates it.

    Detection skips any card not newer than the stored cursor. A detail item
    without ``pub_ts`` (or with an older one) would make the filled shell
    silently disappear, so the feed timestamp wins whenever it is newer.

    Returns a copy of the affected path: ``detail`` may be the dict stored in
    the 300 s cache, and enriching one feed item must not rewrite it for every
    other caller.
    """
    feed_ts = _dynamic_pub_ts(shell)
    if not feed_ts or _dynamic_pub_ts(detail) >= feed_ts:
        return detail
    modules = detail.get("modules")
    if not isinstance(modules, dict):
        return detail
    author = modules.get("module_author")
    author = dict(author) if isinstance(author, dict) else {}
    author["pub_ts"] = feed_ts
    return {**detail, "modules": {**modules, "module_author": author}}


async def enrich_empty_dynamic_items(
    api: Any,
    items: list[dict[str, Any]],
    *,
    deadline: float | None = None,
    after_ts: int = 0,
    limit: int = DYNAMIC_DETAIL_LIMIT,
    release_ids: set[str] | None = None,
    skip_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Replace feed shells with the detail item before detection.

    Only the items handed in are considered: the poller passes exactly the
    window it is about to detect, so deferred items can never be hidden by
    re-selecting the page afterwards. Only empty, enrichable items newer than
    the poll baseline are queried, at most ``limit`` of them, inside one 10 s
    budget.

    If any eligible item would be left unprocessed because of the limit or the
    budget, or a lookup fails transiently (timeout, risk control), the whole
    round raises `DynamicEnrichDeferred`: pushing a partially filled window
    would advance the cursor past the skipped shell and it would never be
    retried. The exception carries how many items this round *newly* resolved,
    so the poller can tell a slowly converging window (retry at once) from one
    that is stuck (back off). Cache hits are excluded from that count: they
    were resolved by an earlier round and must not keep a stuck window on the
    normal cadence forever.

    A shell whose detail is reported unavailable keeps its placeholder; that
    result is cached and has nothing to retry. ``release_ids`` names shells the
    poller has already seen fail repeatedly: those persistent transient
    failures release their placeholder instead of deferring forever, and count
    as progress for the round. Only listed ids qualify, so a shell that first
    fails in a release round still gets its own retry rounds instead of losing
    its content immediately. ``skip_ids`` are the shells released in an earlier
    round: they stay placeholders and are not queried again.
    """
    loop = asyncio.get_running_loop()
    budget_deadline = loop.time() + DYNAMIC_ENRICH_BUDGET_SECONDS
    if deadline is not None:
        budget_deadline = min(budget_deadline, deadline)
    eligible = [
        index
        for index, item in enumerate(items)
        if _dynamic_enrichable(item, after_ts)
    ]
    if len(eligible) > limit:
        raise DynamicEnrichDeferred(
            f"本轮有 {len(eligible)} 条动态需要回查详情，超过单轮上限 {limit}"
        )
    release_ids = release_ids or set()
    skip_ids = skip_ids or set()
    resolved_ids: set[str] = set()
    released_ids: set[str] = set()
    for index in eligible:
        item_id = str(items[index].get("id_str") or "")
        # A shell released in an earlier round is already a placeholder; asking
        # again only burns the budget and repeats the warning.
        if item_id in skip_ids:
            continue
        if loop.time() >= budget_deadline - 0.25:
            raise DynamicEnrichDeferred(
                f"动态详情回查预算用完，{item_id} 留待下一轮",
                progress=len(resolved_ids),
                resolved_ids=resolved_ids,
                released_ids=released_ids,
            )
        # A cache hit was resolved by an earlier round; only newly advanced
        # items may reset the poller's no-progress deferral counter.
        was_cached = api.has_cached_dynamic_detail(item_id)
        try:
            detail = await api.dynamic_detail(item_id, deadline=budget_deadline)
        except DynamicDetailUnavailable:
            # Deleted/invisible: keep the placeholder shell, nothing to retry.
            if not was_cached:
                resolved_ids.add(item_id)
            continue
        except BiliAPIError as exc:
            if item_id in release_ids:
                # Releasing one shell lets the window converge instead of
                # blocking every newer post behind a permanently unreadable
                # item. It counts as progress and is skipped from now on.
                resolved_ids.add(item_id)
                released_ids.add(item_id)
                logger.warning(
                    f"[bilibilibot] dynamic {item_id} detail still unavailable "
                    f"after repeated rounds, releasing its shell to keep newer "
                    f"posts moving: {compact_error(exc)}"
                )
                continue
            raise DynamicEnrichDeferred(
                f"动态 {item_id} 详情临时不可用，本轮整体延后: {compact_error(exc)}",
                progress=len(resolved_ids),
                resolved_ids=resolved_ids,
                item_id=item_id,
                released_ids=released_ids,
            ) from exc
        items[index] = _carry_feed_pub_ts(items[index], detail)
        if not was_cached:
            resolved_ids.add(item_id)
    return items


def _dynamic_enrichable(item: Any, after_ts: int) -> bool:
    """An item this round may look up: newer than the baseline and empty."""
    if not isinstance(item, dict) or _dynamic_pub_ts(item) <= after_ts:
        return False
    # An item without an id cannot be queried; it also never renders as a card
    # worth pushing, so leave it alone instead of deferring forever.
    return bool(str(item.get("id_str") or "")) and _dynamic_needs_detail(item)


# 详情回查也补不出可渲染内容的类型：直播卡片归直播订阅管，common/music/PGC
# 分享卡片即使回查也只会得到通用占位。命中这些类型的空壳直接放行，不占预算。
UNRENDERABLE_DYNAMIC_TYPES = frozenset({"DYNAMIC_TYPE_LIVE", "DYNAMIC_TYPE_LIVE_RCMD"})
UNRENDERABLE_MAJOR_TYPES = frozenset(
    {
        "MAJOR_TYPE_LIVE",
        "MAJOR_TYPE_LIVE_RCMD",
        "MAJOR_TYPE_COMMON",
        "MAJOR_TYPE_MUSIC",
        "MAJOR_TYPE_PGC",
    }
)
UNRENDERABLE_ADDITIONAL_TYPES = frozenset({"ADDITIONAL_TYPE_LIVE"})


def _dynamic_needs_detail(item: dict[str, Any]) -> bool:
    """Whether this feed item may be an empty shell the detail endpoint can fill.

    Live cards, live recommendations and common/music/PGC shares render no
    useful content anywhere, so asking the detail endpoint only burns the
    request budget. Normal empty DRAW/WORD items still need the lookup.
    """
    if not isinstance(item, dict):
        return False
    if item.get("type") in UNRENDERABLE_DYNAMIC_TYPES:
        return False
    modules = item.get("modules") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    if major.get("type") in UNRENDERABLE_MAJOR_TYPES:
        return False
    additional = dynamic.get("additional") or {}
    if additional.get("type") in UNRENDERABLE_ADDITIONAL_TYPES:
        return False
    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if not isinstance(orig, dict) or not orig:
            return False
        if not _dynamic_has_content(orig):
            return False
    return not _dynamic_has_content(item)


def _dynamic_has_content(item: dict[str, Any]) -> bool:
    """Whether mapping this item would produce anything beyond the generic shell."""
    if not isinstance(item, dict):
        return False
    author = (item.get("modules") or {}).get("module_author") or {}
    card = dynamic_item_to_card(item, str(author.get("mid") or ""))
    description = card.description.strip()
    if description in {"-", "--", "—"}:
        description = ""
    return bool(
        card.cover_url.strip()
        or description
        or card.title not in {"", "发布了新动态", "Bilibili"}
    )


async def dynamic_detail(
    api: Any, item_id: str, *, deadline: float | None = None
) -> dict[str, Any]:
    try:
        data = await api._within(
            f"dynamic detail {item_id}",
            deadline,
            lambda: api._get_json_with_risk_retry(
                DYNAMIC_DETAIL_URL,
                f"dynamic detail {item_id}",
                params={"id": item_id, "features": "itemOpusStyle"},
            ),
        )
    except BiliAPIError as exc:
        if exc.code in DYNAMIC_UNAVAILABLE_CODES:
            # A business code for "deleted/hidden/id does not exist" can never
            # recover; classify it with the empty-item answer so the miss cache
            # stops the retries instead of deferring the whole window forever.
            raise DynamicDetailUnavailable(
                f"动态 {item_id} 不可见: {compact_error(exc)}"
            ) from exc
        raise
    item = (data.get("data") or {}).get("item") or {}
    if str(item.get("id_str") or "") != item_id or not _dynamic_has_content(item):
        raise DynamicDetailUnavailable(f"动态 {item_id} 不可见")
    return item


async def official_dynamic_detail(
    api: Any, item_id: str, *, deadline: float | None = None
) -> dict[str, Any]:
    data = await api._within(
        f"dynamic detail {item_id}",
        deadline,
        lambda: api._get_json_with_risk_retry(
            DYNAMIC_DETAIL_URL,
            f"dynamic detail {item_id}",
            params={"id": item_id, "features": "itemOpusStyle"},
        ),
    )
    item = (data.get("data") or {}).get("item") or {}
    author = (item.get("modules") or {}).get("module_author") or {}
    if str(item.get("id_str")) != item_id or str(author.get("mid")) != ENDFIELD_OFFICIAL_UID:
        raise BiliAPIError("官号动态详情的 ID 或发布者不匹配")
    validate_official_dynamic(item)
    return item


def validate_official_dynamic(item: dict[str, Any]) -> None:
    """拒绝静默丢掉 Opus 正文的旧版图文结构。"""
    while isinstance(item, dict):
        dynamic = (item.get("modules") or {}).get("module_dynamic")
        if not isinstance(dynamic, dict):
            raise BiliAPIError("官号动态缺少正文结构")
        major = dynamic.get("major") or {}
        if major.get("type") == "MAJOR_TYPE_DRAW" and dynamic.get("desc") is None:
            raise BiliAPIError("官号图文缺少 Opus 正文，暂不判定为普通动态")
        if major.get("type") == "MAJOR_TYPE_OPUS" and not isinstance(
            (major.get("opus") or {}).get("summary"), dict
        ):
            raise BiliAPIError("官号 Opus 缺少正文摘要")
        if item.get("type") == "DYNAMIC_TYPE_FORWARD" and not item.get("orig"):
            raise BiliAPIError("官号转发动态缺少原动态")
        item = item.get("orig")


async def official_rss_dynamics(api: Any, *, deadline: float | None = None) -> list[dict[str, Any]]:
    """space 接口不可用时，用 RSS 的 ID 列表回查官方 detail。"""
    hints = await api.rsshub_items(
        DYNAMIC_ROUTE.format(uid=ENDFIELD_OFFICIAL_UID), deadline=deadline
    )
    ids: list[str] = []
    for hint in hints[:20]:
        parsed = urlparse(str(hint.get("link") or ""))
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "t.bilibili.com" and len(parts) == 1:
            item_id = parts[0]
        elif (
            parsed.hostname in {"www.bilibili.com", "bilibili.com"}
            and len(parts) == 2
            and parts[0] == "opus"
        ):
            item_id = parts[1]
        else:
            raise BiliAPIError("RSS 未提供可验证的动态链接")
        if not item_id.isascii() or not item_id.isdigit():
            raise BiliAPIError("RSS 动态 ID 无效")
        if item_id not in ids:
            ids.append(item_id)
    if not ids:
        raise BiliAPIError("RSS 未提供动态")
    semaphore = asyncio.Semaphore(3)

    async def fetch(item_id: str) -> dict[str, Any]:
        async with semaphore:
            return await api.official_dynamic_detail(item_id, deadline=deadline)

    # 部分失败不能推进游标，否则未知的那条会被永久跳过。
    tasks = [asyncio.create_task(fetch(item_id)) for item_id in ids]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    "DYNAMIC_DETAIL_LIMIT",
    "DYNAMIC_ITEM_LIMIT",
    "compact_error",
    "dynamic_detail",
    "dynamic_items",
    "dynamic_latest_video",
    "dynamic_item_to_card",
    "enrich_empty_dynamic_items",
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
