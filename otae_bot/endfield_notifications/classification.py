"""Conservative, deterministic classification of official Endfield content."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

LABELS = {
    "activity": "游戏活动",
    "banner": "卡池与武器申领",
    "version": "版本更新与维护",
    "research": "研发情报",
    "community": "社区活动与福利",
    "governance": "账号治理",
    "preview": "版本前瞻与直播",
    "operator": "干员介绍与演示",
    "music": "音乐与MV",
    "collaboration": "品牌联名与线下活动",
    "greeting": "节庆祝福",
    "lottery_result": "开奖结算",
    "other": "其他资讯",
}
ZONE = timezone(timedelta(hours=8))
DATE = re.compile(
    r"(20\d{2})[/年.\-](\d{1,2})[/月.\-](\d{1,2})日?\s*(\d{1,2})[:：](\d{2})"
)
QUOTED = re.compile(r"[「『《]([^」』》]+)[」』》]")
UPDATE = re.compile(r"改期|延期|时间调整|调整说明|补充说明|更正|勘误|追加奖励|取消活动")
SOCIAL_LOTTERY = re.compile(
    r"抽[奖獎]|[开開中兑兌][奖獎]|(?:转发|轉發|评论|評論)(?:有奖|有獎|抽|赢|贏)|"
    r"(?:抽取|抽出|随机选出|随机挑选)[^。！？]{0,60}(?:位|名)[^。！？]{0,30}(?:送出|赠送|获得|玩家|管理员)|"
    r"(?:抽取|抽出)[^。！？\n]{0,80}(?:周边|京东卡|iPad|手机|手柄)|"
    r"(?:bilibili\.com|b23\.tv)/[^\s]*lottery",
    re.IGNORECASE,
)
EVENT_KINDS = {"activity", "banner", "signin", "maintenance"}


def normalize(text: str) -> str:
    """Keep letters/digits, drop formatting and invisible characters."""
    return "".join(
        c.lower() for c in unicodedata.normalize("NFKC", text) if c.isalnum()
    )


def core_text(text: str) -> str:
    # Official Bilibili posts append unrelated current-version advertising.
    return re.split(r"\n\s*[—─_-]{4,}\s*\n", text, maxsplit=1)[0].strip()


def headline(title: str, text: str) -> str:
    fallback = title.strip()
    if QUOTED.search(fallback) or re.search(
        r"研发|封禁|封停|违规|更新|预下载|前瞻|干员|演示|单曲|音乐|联名|高校|征集|开奖|中奖",
        fallback,
    ):
        return fallback
    for line in core_text(text).splitlines():
        line = re.sub(r"#[^#]+#", "", line).strip()
        if not line or line.startswith(("亲爱的", "管理员：", "互动抽奖")):
            continue
        if QUOTED.search(line) and len(line) < 120:
            return line
        if fallback not in {"互动抽奖", "发布了新动态", ""}:
            break
    return title.strip()


def version_of(text: str) -> str:
    found = re.search(r"[「『]([^」』]+)[」』](?:核心章节)?版本", text)
    return normalize(found[1]) if found else ""


def event_kind(title: str) -> str:
    if "维护" in title:
        return "maintenance"
    if any(word in title for word in ("寻访", "申领", "卡池")):
        return "banner"
    if "签到" in title:
        return "signin"
    return "activity"


@dataclass(frozen=True)
class Event:
    name: str
    kind: str
    phase: str
    source_at: int
    start_at: int = 0
    end_at: int = 0
    version: str = ""
    fingerprint: str = ""


@dataclass(frozen=True)
class Unit:
    title: str
    text: str
    event: Event | None


@dataclass(frozen=True)
class Classification:
    category: str
    tags: tuple[str, ...]
    units: tuple[Unit, ...]


def window_event(
    title: str,
    text: str,
    kind: str,
    phase: str,
    source_at: int,
    start_at: int = 0,
    end_at: int = 0,
    version: str = "",
) -> Event | None:
    names = QUOTED.findall(title)
    if kind == "maintenance":
        name = "maintenance"
    elif names:
        name = ":".join(sorted({normalize(name) for name in names}))
    else:
        # Generic headings like "活动开启" must never hide an unrelated post.
        return None
    if "兑换" in title:
        kind = "exchange"
    return Event(
        name,
        kind,
        phase,
        int(source_at),
        int(start_at),
        int(end_at),
        version,
        sha256(normalize(text).encode()).hexdigest(),
    )


def _schedule(text: str) -> tuple[int, int]:
    """Only read a labelled schedule, never a publication or giveaway date."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"(?:活动|开放|维护)(?:开始|开启|结束)?时间", line):
            continue
        value = line + " " + " ".join(lines[index + 1 : index + 3])
        zones = re.findall(r"UTC\s*([+-]\d{1,2}(?::\d{2})?)", value, re.IGNORECASE)
        if any(zone not in {"+8", "+08", "+8:00", "+08:00"} for zone in zones):
            return 0, 0
        matches = list(DATE.finditer(value))
        if not 1 <= len(matches) <= 2:
            continue
        try:
            stamps = [
                int(datetime(*(int(v) for v in m.groups()), tzinfo=ZONE).timestamp())
                for m in matches
            ]
        except ValueError:
            return 0, 0
        if len(stamps) == 2:
            return (stamps[0], stamps[1]) if stamps[1] > stamps[0] else (0, 0)
        prefix = value[: matches[0].start()]
        return (
            (0, stamps[0])
            if re.search(r"结束|截止|后\s*[-—至]", prefix)
            else (stamps[0], 0)
        )
    return 0, 0


def _phase(title: str, text: str, published_at: int, start_at: int) -> str:
    if UPDATE.search(title + "\n" + text[:160]):
        return "update"
    if re.search(r"攻略|参与指南|玩法教学|玩法介绍|机制详解|玩法演示", title):
        return "guide"
    if re.search(r"活动回顾|活动成果|活动总结", title):
        return "recap"
    if re.search(r"即将结束|即将截止|结束提醒", title):
        return "ending"
    if re.search(r"已开启|现已开启|正式开启|今日开启", title) and (
        not start_at or start_at <= published_at
    ):
        return "started"
    return "announcement"


def classify(
    title: str, text: str, published_at: int, *, lottery: bool = False
) -> Classification:
    title = headline(title, text)
    body = core_text(text)
    lead = title + "\n" + body[:180]
    tags = set()
    if lottery or SOCIAL_LOTTERY.search(text):
        tags.add("lottery")
    if re.search(r"开奖|中奖|兑奖|中獎|開獎", lead):
        category = "lottery_result"
    elif re.search(r"封禁|封停|违规处理", title):
        category = "governance"
    elif re.search(r"研发通讯|开发者日志|开发进展", title):
        category = "research"
    elif re.search(r"版本更新说明|预下载|停机维护|更新预告", title):
        category = "version"
    elif re.search(r"前瞻|特别节目|直播预告|直播回放", lead):
        category = "preview"
    elif re.search(r"联名|合作宣传|主题店|线下活动", lead):
        category = "collaboration"
    elif re.search(
        r"专属单曲|音乐专辑|音乐发布|MV|OST|EP(?:现已|发布)", lead, re.IGNORECASE
    ):
        category = "music"
    elif re.search(r"干员演示|干员介绍|干员档案", lead):
        category = "operator"
    elif re.search(r"创作征集|创作者|高校认证|社区活动", lead):
        category = "community"
    elif re.search(r"寻访|申领|卡池", title):
        category = "banner"
    elif re.search(r"活动|签到", title):
        category = "activity"
    elif re.search(r"中秋|新年|节日|清光盈袖|祝福", lead):
        category = "greeting"
    else:
        category = "other"
    for word, tag in (
        ("签到", "signin"),
        ("作战演练", "trial"),
        ("预下载", "predownload"),
        ("维护", "maintenance"),
    ):
        if word in body:
            tags.add(tag)
    version = version_of(title + "\n" + text)
    if category not in {"activity", "banner"}:
        # Composite version/research articles carry more than activity dates.
        # Deduplicate them only by their complete headline, never by one child.
        event = None
        if (
            category in {"version", "research", "governance"}
            and len(normalize(title)) >= 8
        ):
            event = Event(
                normalize(title),
                "document:" + category,
                _phase(title, body, published_at, 0),
                published_at,
                version=version,
                fingerprint=sha256(normalize(body).encode()).hexdigest(),
            )
        return Classification(
            category, tuple(sorted(tags)), (Unit(title, body, event),)
        )
    headings = list(
        re.finditer(
            r"(?m)^[▼■○/\s\d.、]*([「『][^\n]+(?:活动|签到|寻访|申领)[^\n]*)$", body
        )
    )
    # Split only unambiguous section boundaries. Shared schedules stay together.
    sections = [(title, body)]
    splittable = False
    if len(headings) > 1 and all(
        _schedule(
            body[
                h.start() : headings[i + 1].start()
                if i + 1 < len(headings)
                else len(body)
            ]
        )[0]
        for i, h in enumerate(headings)
    ):
        splittable = True
        sections = [
            (
                h[1].strip(),
                body[
                    h.start() : headings[i + 1].start()
                    if i + 1 < len(headings)
                    else len(body)
                ].strip(),
            )
            for i, h in enumerate(headings)
        ]
        tags.add("multiple_activities")
    units = []
    for heading, section in sections:
        start, end = _schedule(section)
        units.append(
            Unit(
                heading,
                section,
                window_event(
                    heading,
                    section,
                    event_kind(heading),
                    _phase(heading, section, published_at, start),
                    published_at,
                    start,
                    end,
                    version,
                )
                if len(headings) <= 1 or splittable
                else None,
            )
        )
    return Classification(category, tuple(sorted(tags)), tuple(units))
