"""Serializable announcement data; all stored times are UTC Unix seconds."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

BEIJING = timezone(timedelta(hours=8))
KINDS = {
    "activity": "活动",
    "maintenance": "维护",
    "banner": "卡池",
    "signin": "签到",
    "notice": "其他公告",
}


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def local_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, BEIJING).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class ActivityWindow:
    key: str
    title: str
    kind: str
    start_at: int = 0
    end_at: int = 0
    summary: str = ""
    image_url: str = ""
    start_hint: str = ""
    end_hint: str = ""
    schedule_label: str = ""


@dataclass(frozen=True)
class Announcement:
    cid: str
    title: str
    published_at: int
    url: str
    summary: str
    kinds: tuple[str, ...]
    windows: tuple[ActivityWindow, ...]
    fingerprint: str
    image_url: str = ""

    def dumps(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def loads(cls, value: str) -> Announcement:
        data = json.loads(value)
        data["kinds"] = tuple(data["kinds"])
        data["windows"] = tuple(ActivityWindow(**item) for item in data["windows"])
        return cls(**data)


@dataclass(frozen=True)
class Destination:
    platform: str
    account_id: str
    target_id: str
    channel_id: str
    private: bool = False

    @property
    def key(self) -> str:
        return json.dumps(
            [self.platform, self.account_id, self.private, self.target_id],
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Subscription:
    destination: Destination
    kinds: tuple[str, ...] = tuple(KINDS)
    start_minutes: int = 60
    end_minutes: int = 1440
    maintenance_minutes: int = 60
    created_at: int = 0
    initialized: bool = False


@dataclass(frozen=True)
class Delivery:
    key: str
    subscription_key: str
    article_id: str
    text: str
    due_at: int
    expires_at: int
    attempts: int = 0
    window_key: str = ""
    phase: str = "news"


@dataclass(frozen=True)
class ActivityCard:
    key: str
    title: str
    kind: str
    summary: str
    image_url: str
    start_at: int
    end_at: int
    start_hint: str
    end_hint: str
    published_at: int
    url: str
    phases: tuple[str, ...]
    image_caption: str = "活动配图"


@dataclass(frozen=True)
class AnnouncementDigest:
    cards: tuple[ActivityCard, ...]
    generated_at: int


def message(
    article: Announcement,
    *,
    updated: bool = False,
    window: ActivityWindow | None = None,
    phase: str = "",
    target: int = 0,
) -> str:
    label = "公告更新" if updated else "新公告"
    if window:
        label = {"start": "即将开始", "end": "即将结束", "maintenance": "维护提醒"}[
            phase
        ]
    lines = [f"【终末地·{label}】", article.title]
    if window:
        if window.title != article.title:
            lines.append(window.title)
        lines.append(
            f"{'结束' if phase == 'end' else '开始'}时间：{local_time(target)}（北京时间）"
        )
    elif article.summary:
        lines.append(article.summary[:240])
    lines.extend(
        [f"公告发布：{local_time(article.published_at)}（北京时间）", article.url]
    )
    return "\n".join(lines)
