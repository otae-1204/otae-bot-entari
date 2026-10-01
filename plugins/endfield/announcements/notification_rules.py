"""Adapt parsed website windows to the shared event vocabulary."""

from __future__ import annotations

from dataclasses import replace

from otae_bot.endfield_notifications.classification import (
    classify,
    version_of,
    window_event,
)

from .models import ActivityWindow, Announcement, AnnouncementDigest


def event_for(article: Announcement, window: ActivityWindow | None, phase: str):
    text = article.content_text or article.summary
    classified = classify(article.title, text, article.published_at)
    if phase in {"news", "updated"}:
        event = classified.units[0].event
        # Whole version/research/governance notices have a document identity.
        if classified.category not in {"activity", "banner"} or window is None:
            return (
                replace(event, phase="update")
                if event and phase == "updated"
                else event
            )
        content_phase = (
            "update" if phase == "updated" else event.phase if event else "announcement"
        )
        source_at = article.published_at
    else:
        if window is None:
            return None
        content_phase = {
            "start": "upcoming",
            "started": "started",
            "end": "ending",
            "maintenance": "maintenance",
        }[phase]
        source_at = window.end_at if phase == "end" else window.start_at
    return window_event(
        window.title + (f" · {window.schedule_label}" if window.schedule_label else ""),
        text,
        window.kind,
        content_phase,
        source_at,
        window.start_at,
        window.end_at,
        version_of(article.title + "\n" + text),
    )


def events_for(bulletin: AnnouncementDigest):
    return [
        card.notification_events[index]
        if index < len(card.notification_events)
        else None
        for card in bulletin.cards
        for index, _ in enumerate(card.phases)
    ]


def retain_events(bulletin: AnnouncementDigest, accepted: tuple[int, ...]):
    """Remove only acknowledged phases, then remove cards with no phases left."""
    accepted_set = set(accepted)
    cards = []
    offset = 0
    for card in bulletin.cards:
        indices = [i for i in range(len(card.phases)) if offset + i in accepted_set]
        if indices:
            cards.append(
                replace(
                    card,
                    phases=tuple(card.phases[i] for i in indices),
                    notification_events=tuple(
                        card.notification_events[i]
                        if i < len(card.notification_events)
                        else None
                        for i in indices
                    ),
                )
            )
        offset += len(card.phases)
    return replace(bulletin, cards=tuple(cards))
