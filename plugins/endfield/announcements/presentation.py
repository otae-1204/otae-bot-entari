"""Build one activity digest from the receipts due for a destination."""

from __future__ import annotations

from dataclasses import replace

from .models import ActivityCard, Announcement, AnnouncementDigest, Delivery
from .notification_rules import event_for


def build_digest(
    articles: list[Announcement],
    jobs: list[Delivery],
    kinds: tuple[str, ...],
    now: int,
    withdrawn: dict[str, int] | None = None,
) -> AnnouncementDigest:
    by_id = {article.cid: article for article in articles}
    withdrawn = withdrawn or {}
    latest_windows = {}
    for article in sorted(articles, key=lambda item: (item.published_at, item.cid)):
        for window in article.windows:
            if article.published_at > withdrawn.get(window.key, -1):
                latest_windows[window.key] = article, window
    cards: dict[str, ActivityCard] = {}
    for job in jobs:
        article = by_id.get(job.article_id)
        if article is None:
            continue
        windows = [
            window
            for window in article.windows
            if window.kind in kinds
            and window.key in latest_windows
            and (not job.window_key or window.key == job.window_key)
        ]
        # A withdrawn scheduled window must never become a generic notice.
        if job.window_key and not windows:
            continue
        for window in windows or [None]:
            details = article
            if window:
                candidate, candidate_window = latest_windows[window.key]
                if candidate.published_at >= article.published_at:
                    details, window = candidate, candidate_window
            key = window.key if window else f"article:{article.cid}"
            card = ActivityCard(
                key=key,
                title=(
                    window.title
                    + (f" · {window.schedule_label}" if window.schedule_label else "")
                )
                if window
                else article.title,
                kind=window.kind
                if window
                else next((kind for kind in article.kinds if kind in kinds), "notice"),
                summary=(window.summary or "活动内容详见官方公告。")
                if window
                else article.summary,
                image_url=(window.image_url or details.image_url)
                if window
                else article.image_url,
                start_at=window.start_at if window else 0,
                end_at=window.end_at if window else 0,
                start_hint=window.start_hint if window else "",
                end_hint=window.end_hint if window else "",
                published_at=details.published_at,
                url=details.url,
                phases=(job.phase,),
                image_caption="版本公告配图"
                if window
                and len(details.windows) > 1
                and window.image_url == details.image_url
                else "活动配图",
                notification_events=(
                    event_for(
                        article if job.phase in {"news", "updated"} else details,
                        window,
                        job.phase,
                    ),
                ),
            )
            previous = cards.get(key)
            if previous:
                phases = tuple(sorted(set(previous.phases + card.phases)))
                events = dict(zip(previous.phases, previous.notification_events))
                events.update(zip(card.phases, card.notification_events))
                card = replace(
                    card if card.published_at >= previous.published_at else previous,
                    phases=phases,
                    notification_events=tuple(events.get(phase) for phase in phases),
                )
            cards[key] = card
    return AnnouncementDigest(
        tuple(
            sorted(
                cards.values(),
                key=lambda card: (
                    card.start_at or card.end_at or card.published_at,
                    card.key,
                ),
            )
        ),
        now,
    )


def remaining(card: ActivityCard, now: int) -> tuple[str, str]:
    """Never interpret an unknown end time as zero days remaining."""
    if card.end_at and now >= card.end_at:
        return "活动状态", "已结束"
    if card.start_at and now < card.start_at:
        label, seconds = "距开始", card.start_at - now
    elif card.end_at:
        label, seconds = "距结束", card.end_at - now
    else:
        return "剩余时间", "截止时间待定"
    minutes = (seconds + 59) // 60
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        text = f"{days} 天" + (f" {hours} 小时" if hours else "")
    elif hours:
        text = f"{hours} 小时" + (f" {minutes} 分" if minutes else "")
    else:
        text = f"{minutes} 分钟"
    return label, text


def activity_state(card: ActivityCard, now: int) -> str:
    if card.end_at and now >= card.end_at:
        return "已结束"
    if card.start_at and now < card.start_at:
        return "即将开始"
    if card.start_at and now >= card.start_at:
        return "维护中" if card.kind == "maintenance" else "进行中"
    return "时间待定" if not card.end_at else "结束时间已公布"
