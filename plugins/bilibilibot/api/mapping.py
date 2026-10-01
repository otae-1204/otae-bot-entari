from __future__ import annotations

import re
from typing import Any

from otae_bot.endfield_notifications.classification import classify

from ..dynamic_filter import ENDFIELD_OFFICIAL_UID, dynamic_lottery_reason
from ..models import BiliCard, KIND_DYNAMIC, KIND_VIDEO


BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")


def bvid_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    match = BV_RE.search(url or "")
    if match:
        return match.group(0)
    query = parse_qs(urlparse(url or "").query)
    candidate = query.get("bvid", [""])[0]
    return candidate if BV_RE.fullmatch(candidate) else ""


def bvid_from_card(card: BiliCard) -> str:
    for value in (card.item_id, card.url, card.description):
        bvid = bvid_from_url(value or "")
        if bvid:
            return bvid
    return ""


def dynamic_item_to_card(item: dict[str, Any], uid: str) -> BiliCard:
    """Map one dynamic feed entry to a card (public form of the old private helper)."""
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    desc = dynamic.get("desc") or {}
    major = dynamic.get("major") or {}
    opus = major.get("opus") or {}
    additional = dynamic.get("additional") or {}
    title = "发布了新动态"
    cover = ""
    url = f"https://t.bilibili.com/{item.get('id_str')}"

    if major.get("type") == "MAJOR_TYPE_OPUS":
        title = str(opus.get("title") or title)
        images = opus.get("pics") or []
        cover = str(images[0].get("url") or "") if images else ""
        url = str(opus.get("jump_url") or url)
    elif major.get("type") == "MAJOR_TYPE_DRAW":
        images = (major.get("draw") or {}).get("items") or []
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

    summary = opus.get("summary") or {}
    text = str(desc.get("text") or summary.get("text") or "")
    if not text:
        text = "".join(
            str(node.get("text") or node.get("orig_text") or "")
            for node in (desc.get("rich_text_nodes") or summary.get("rich_text_nodes") or [])
        )
    if text and title == "发布了新动态":
        lines = [re.sub(r"#[^#]+#", "", line).strip() for line in text.splitlines()]
        title = next((line[:40] for line in lines if line), text.splitlines()[0][:40])
    if url.startswith("//"):
        url = "https:" + url
    lottery_reason = dynamic_lottery_reason(item)
    published_at = int(author.get("pub_ts") or 0)
    classification = (
        classify(title, text, published_at, lottery=bool(lottery_reason))
        if uid == ENDFIELD_OFFICIAL_UID else None
    )
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
        item_id=(
            str(item.get("id_str") or "")
            if uid == ENDFIELD_OFFICIAL_UID
            else bvid_from_url(url) or str(item.get("id_str") or "")
        ),
        published_at=published_at,
        lottery_reason=lottery_reason,
        content_category=classification.category if classification else "",
        content_tags=classification.tags if classification else (),
    )


def video_item_to_card(item: dict[str, Any], uid: str) -> BiliCard:
    bvid = str(item.get("bvid") or "")
    return BiliCard(
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


def video_view_to_card(payload: dict[str, Any], bvid: str) -> BiliCard:
    owner = payload.get("owner") or {}
    return BiliCard(
        KIND_VIDEO,
        title=str(payload.get("title") or ""),
        author=str(owner.get("name") or ""),
        description=str(payload.get("desc") or ""),
        cover_url=str(payload.get("pic") or ""),
        avatar_url=str(owner.get("face") or ""),
        url=f"https://www.bilibili.com/video/{bvid}",
        badge="VIDEO",
        uid=str(owner.get("mid") or ""),
        item_id=bvid,
        published_at=int(payload.get("pubdate") or 0),
    )
