"""Read the same public CMS endpoints as the mainland official website."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

from lxml import html

from otae_bot.infrastructure.http.client import fetch_json

from .models import BEIJING, ActivityWindow, Announcement, digest

API_ROOT = "https://web-news.hypergryph.com/api/bulletin"
NEWS_ROOT = "https://endfield.hypergryph.com/news"
NAMESPACE = "endfield-announcements"
POLL_SECONDS = 600
LOOKBACK_SECONDS = 45 * 86400
MAX_PAGES = 3
PAGE_SIZE = 20
MAX_ARTICLES = 80
DATE = re.compile(
    r"(?<!\d)(20\d{2})\s*[/年.\-]\s*(\d{1,2})\s*[/月.\-]\s*(\d{1,2})日?\s*(\d{1,2})[:：](\d{2})(?!\d)"
)
TIME_LABEL = re.compile(
    r"(?:活动|开放|寻访|申领|维护|签到|限时|开启|结束|截止).*?(?:时间|日期)|(?:时间|日期).*?(?:活动|维护)"
)


class AnnouncementSourceError(ValueError):
    pass


def _kind(text: str) -> str:
    if any(word in text for word in ("维护", "版本更新说明", "更新预告")):
        return "maintenance"
    if "签到" in text:
        return "signin"
    if any(word in text for word in ("寻访", "申领", "卡池")):
        return "banner"
    return "activity" if "活动" in text else "notice"


def body_lines(raw: str) -> list[str]:
    root = html.fragment_fromstring(raw, create_parent="div")
    for element in root.xpath(".//script|.//style"):
        element.drop_tree()
    # Keep paragraph boundaries: an unrelated reward date must not be paired
    # with an event start from another section.
    blocks = root.xpath(
        ".//*[self::p or self::h1 or self::h2 or self::h3 or self::h4 or self::li or self::td][not(.//p)]"
    )
    return [
        text
        for node in (blocks or [root])
        if (text := " ".join(node.text_content().split()))
    ]


def parse_windows(cid: str, title: str, lines: list[str]) -> tuple[ActivityWindow, ...]:
    windows: list[ActivityWindow] = []
    heading = title
    time_heading = ""
    labels: dict[str, int] = {}
    for line in lines:
        matches = list(DATE.finditer(line))
        if not matches:
            if line.startswith(("▼", "■")) or (
                re.match(r"^\d+[.、]", line) and "「" in line
            ):
                clean = re.sub(r"^[▼■/\s\d.、]+", "", line)
                if "「" in clean or _kind(clean) == "maintenance":
                    heading = clean
                time_heading = clean if TIME_LABEL.search(clean) else ""
            else:
                time_heading = (
                    line if TIME_LABEL.search(line) and len(line) < 60 else ""
                )
            continue
        context = time_heading
        time_heading = ""
        if not (TIME_LABEL.search(line) or context) or len(matches) > 2:
            continue
        # The source is zh-cn. An explicitly different timezone is not assumed
        # to be the mainland server clock.
        zones = re.findall(r"UTC\s*([+-]\d{1,2}(?::\d{2})?)", line, re.IGNORECASE)
        if any(zone not in {"+8", "+08", "+8:00", "+08:00"} for zone in zones):
            continue
        try:
            stamps = [
                int(
                    datetime(
                        *(int(v) for v in match.groups()), tzinfo=BEIJING
                    ).timestamp()
                )
                for match in matches
            ]
        except ValueError:
            continue
        start, end = stamps[0], stamps[1] if len(stamps) == 2 else 0
        if end and end <= start:
            continue
        if end and not re.search(
            r"[-—–~～至]", line[matches[0].end() : matches[1].start()]
        ):
            continue
        if len(stamps) == 1:
            prefix = line[: matches[0].start()]
            # "版本更新后 - <explicit end>" has no reliable opening time.
            if re.search(r"(?:后|起)\s*[-—–~～至]", prefix) or re.search(
                r"结束|截止", prefix + context
            ):
                start, end = 0, start
        label = heading or title
        kind = _kind(label)
        if "维护" in context or "维护时间" in line:
            kind = "maintenance"
        if kind == "notice":
            kind = "activity"
        # The version overview and a dedicated bulletin often describe the
        # same named activity. Keep that activity's identity across articles.
        names = re.findall(r"[「『]([^」』]+)[」』]", label)
        identity = names or (["maintenance"] if kind == "maintenance" else [cid, label])
        signature = digest([identity, kind])[:24]
        index = labels.get(signature, 0)
        labels[signature] = index + 1
        windows.append(ActivityWindow(f"{signature}:{index}", label, kind, start, end))
    return tuple(windows)


def parse_article(payload: dict, expected_id: str = "") -> Announcement:
    data = payload.get("data")
    if payload.get("code") != 0 or not isinstance(data, dict):
        raise AnnouncementSourceError("官网公告详情格式异常")
    cid = str(data.get("cid") or "")
    title = str(data.get("title") or "").strip()
    body = data.get("data")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid) or (
        expected_id and cid != expected_id
    ):
        raise AnnouncementSourceError("官网公告 ID 不匹配")
    if not title or not isinstance(body, str) or not body.strip():
        raise AnnouncementSourceError("官网公告缺少标题或正文")
    published = data.get("displayTime")
    if (
        isinstance(published, bool)
        or not isinstance(published, (int, float))
        or not 946684800 <= published < 4102444800
    ):
        raise AnnouncementSourceError("官网公告发布时间无效")
    lines = body_lines(body)
    windows = parse_windows(cid, title, lines)
    headings = [line for line in lines if line.startswith(("▼", "■"))]
    kind_set = {
        _kind(title),
        *(window.kind for window in windows),
        *(_kind(line) for line in headings),
    }
    if len(kind_set) > 1:
        kind_set.discard("notice")
    kinds = tuple(sorted(kind_set))
    summary = " ".join(body_lines(str(data.get("brief") or ""))) or " ".join(lines[:3])
    # Include image/link changes too, but exclude cover, sticky and frontend
    # presentation fields which do not change the announcement's substance.
    return Announcement(
        cid,
        title,
        int(published),
        f"{NEWS_ROOT}/{cid}",
        summary[:240],
        kinds,
        windows,
        digest([title, int(published), body, summary]),
    )


class OfficialAnnouncementSource:
    def __init__(self, fetcher=fetch_json):
        self.fetcher = fetcher

    async def _get(self, url: str, **params) -> dict:
        result = await self.fetcher(
            url,
            params={"lang": "zh-cn", "code": "endfield_web", **params},
            namespace=NAMESPACE,
            ttl_seconds=POLL_SECONDS,
            timeout_seconds=15,
            max_bytes=2 * 1024 * 1024,
        )
        if not isinstance(result, dict):
            raise AnnouncementSourceError("官网公告接口未返回对象")
        return result

    async def fetch(
        self, watch_ids: tuple[str, ...] = (), *, now: int | None = None
    ) -> list[Announcement]:
        now = int(time.time()) if now is None else now
        ids = dict.fromkeys(watch_ids)
        for page in range(1, MAX_PAGES + 1):
            payload = await self._get(API_ROOT, page=page, pageSize=PAGE_SIZE)
            data = payload.get("data")
            rows = data.get("list") if isinstance(data, dict) else None
            if (
                payload.get("code") != 0
                or not isinstance(rows, list)
                or (page == 1 and not rows)
            ):
                raise AnnouncementSourceError("官网公告列表为空或格式已变化")
            oldest = now
            for row in rows:
                if not isinstance(row, dict) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,64}", str(row.get("cid", ""))
                ):
                    raise AnnouncementSourceError("官网公告列表条目无效")
                published = row.get("displayTime")
                if isinstance(published, bool) or not isinstance(
                    published, (int, float)
                ):
                    raise AnnouncementSourceError("官网公告列表缺少发布时间")
                if not row.get("sticky"):
                    oldest = min(oldest, int(published))
                if published >= now - LOOKBACK_SECONDS or row.get("sticky"):
                    ids[str(row["cid"])] = None
            if len(rows) < PAGE_SIZE or oldest < now - LOOKBACK_SECONDS:
                break
        else:
            raise AnnouncementSourceError("公告超过单次采集范围，暂缓更新以免遗漏")
        if len(ids) > MAX_ARTICLES:
            raise AnnouncementSourceError("待核对公告数量超过上限")
        semaphore = asyncio.Semaphore(3)

        async def detail(cid: str) -> Announcement:
            async with semaphore:
                return parse_article(await self._get(f"{API_ROOT}/{cid}"), cid)

        # Apply only a complete snapshot. A failed detail must not silently
        # initialize a subscriber's baseline or erase its saved schedule.
        return list(await asyncio.gather(*(detail(cid) for cid in ids)))
