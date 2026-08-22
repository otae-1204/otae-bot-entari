"""Public-source adapters for the Tibo radar.

The parser functions are deliberately pure apart from the HTTP wrapper.  This
makes a changing upstream page testable with small HTML/JSON fixtures and lets
the service keep the last good source independently when one source is down.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from lxml import html as lxml_html
from loguru import logger

from utils.http_client import fetch_bytes, fetch_json

from .models import (
    EVENT_CONFIRMED,
    EVENT_EXPECTED_WINDOW,
    EVENT_OFFICIAL_ANNOUNCEMENT,
    EVENT_REJECTED,
    EVENT_SUSPECTED,
    EVENT_UNCONFIRMED,
    RELEVANCE_DIRECT,
    RELEVANCE_INDIRECT,
    RELEVANCE_NONE,
    SOURCE_CODEX_RESET,
    SOURCE_CODEXRADAR,
    ResetEvent,
    SourceState,
    TiboPost,
    parse_datetime,
)


TIBO_HANDLE = "thsottiaux"
TIBO_PROFILE_URL = "https://x.com/thsottiaux"
# CodexRadar exposes the profile image used in its Tibo radar rail.
TIBO_AVATAR_URL = "https://codexradar.com/assets/tibo-x-avatar.jpg"
CODEXRADAR_HOME = "https://codexradar.com/"
CODEX_RESET_FEED = "https://codex-reset.com/api/feed?locale=zh"
CODEX_RESET_TIMELINE = "https://codex-reset.com/api/timeline?locale=zh"
_POST_URL_RE = re.compile(r"https?://(?:www\.)?x\.com/thsottiaux/status/(\d+)", re.I)
_RESET_WORD_RE = re.compile(r"\breset(?:s|ting|ed)?\b", re.I)


class RadarSourceError(RuntimeError):
    """A source could not be fetched or failed its identity/shape checks."""


class RadarSourceParseError(RadarSourceError):
    """A response was available but is not a supported source shape."""


@dataclass(slots=True)
class SourceCollection:
    source_name: str
    posts: list[TiboPost]
    events: list[ResetEvent]
    state: SourceState


@dataclass(slots=True)
class CollectionResult:
    sources: list[SourceCollection]

    @property
    def posts(self) -> list[TiboPost]:
        return [post for source in self.sources for post in source.posts]

    @property
    def events(self) -> list[ResetEvent]:
        return [event for source in self.sources for event in source.events]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(parts: Iterable[Any]) -> str:
    payload = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _post_id_from_url(url: str) -> str:
    match = _POST_URL_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _valid_post_url(url: str, post_id: str) -> bool:
    return bool(_POST_URL_RE.fullmatch(str(url or "").strip())) and _post_id_from_url(url) == str(post_id)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", "").split()).strip()


def _clean_multiline(value: Any) -> str:
    return "\n".join(line.strip() for line in str(value or "").replace("\r", "").split("\n")).strip()


def _node_text_without_label(node: Any) -> str:
    if node is None:
        return ""
    pieces = node.xpath(".//text()[not(ancestor::b)]")
    return _clean_multiline("".join(pieces))


def _find_first(node: Any, class_name: str) -> Any | None:
    result = node.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), $class)]",
        **{"class": f" {class_name} "},
    )
    return result[0] if result else None


def parse_codexradar_html(content: str | bytes, *, collected_at: datetime | None = None) -> SourceCollection:
    """Parse the public CodexRadar Tibo rail.

    Missing required nodes raises instead of returning an empty result: an
    HTML redesign must never silently replace a good local cache with nothing.
    """

    tree = lxml_html.fromstring(content)
    roots = tree.xpath(
        "//section[contains(concat(' ', normalize-space(@class), ' '), ' desktop-tibo-radar ')]"
    )
    if not roots:
        raise RadarSourceParseError("CodexRadar Tibo section not found")
    root = roots[0]
    post_nodes = root.xpath(".//li[@data-tibo-post-id]")
    if not post_nodes:
        raise RadarSourceParseError("CodexRadar Tibo post list is empty")

    collected_at = collected_at or _now()
    updated_at = parse_datetime(root.get("data-tibo-posts-updated-at"))
    posts: list[TiboPost] = []
    fingerprints: list[Any] = []
    for node in post_nodes:
        post_id = str(node.get("data-tibo-post-id") or "").strip()
        link = node.xpath(".//div[contains(@class,'reset-tibo-post-head')]//a/@href")
        url = str(link[0]).strip() if link else ""
        if not post_id or not _valid_post_url(url, post_id):
            raise RadarSourceParseError(f"invalid Tibo post identity: {post_id!r}")
        time_node = node.xpath(".//div[contains(@class,'reset-tibo-post-head')]//time/@datetime")
        source_time = parse_datetime(time_node[0] if time_node else None)
        original = _node_text_without_label(_find_first(node, "reset-tibo-post-original"))
        translation = _node_text_without_label(_find_first(node, "reset-tibo-post-translation"))
        analysis = _node_text_without_label(_find_first(node, "reset-tibo-post-analysis"))
        phrases = _node_text_without_label(_find_first(node, "reset-tibo-post-phrases"))
        if not original:
            raise RadarSourceParseError(f"CodexRadar post {post_id} has no original text")
        raw_relevance = str(node.get("data-reset-relevance") or "none").strip().lower()
        relevance = raw_relevance if raw_relevance in {RELEVANCE_DIRECT, RELEVANCE_INDIRECT, RELEVANCE_NONE} else RELEVANCE_NONE
        fingerprint = _fingerprint([post_id, original, translation, analysis, phrases, raw_relevance, source_time.isoformat() if source_time else ""])
        posts.append(
            TiboPost(
                post_id=post_id,
                text=original,
                url=url,
                source_time=source_time,
                translation=translation,
                analysis=analysis,
                relevance=relevance,
                phrases=phrases,
                page_updated_at=updated_at,
                content_fingerprint=fingerprint,
                last_seen_at=collected_at,
                source_names=(SOURCE_CODEXRADAR,),
            )
        )
        fingerprints.append([post_id, fingerprint])

    root_fingerprint = str(root.get("data-tibo-posts-fingerprint") or "").strip()
    state = SourceState(
        source_name=SOURCE_CODEXRADAR,
        last_success_at=collected_at,
        last_attempt_at=collected_at,
        upstream_updated_at=updated_at,
        fingerprint=root_fingerprint or _fingerprint(fingerprints),
        stale=False,
    )
    return SourceCollection(SOURCE_CODEXRADAR, posts, [], state)


def _localized_value(payload: Mapping[str, Any], key: str, fallback: str = "") -> str:
    value = payload.get(key)
    if value is None:
        return fallback
    return str(value).strip()


def _classify_feed_post(item: Mapping[str, Any]) -> str:
    lane = _localized_value(item, "tibo_lane").lower()
    text = _localized_value(item, "text")
    if lane == "reset_announcement" and bool(item.get("explicit_reset_claim")):
        return RELEVANCE_DIRECT
    if lane == "reset_related" and _RESET_WORD_RE.search(text):
        return RELEVANCE_INDIRECT
    return RELEVANCE_NONE


def _feed_post(item: Mapping[str, Any], collected_at: datetime) -> TiboPost | None:
    post_id = _localized_value(item, "id")
    url = _localized_value(item, "url")
    if not post_id or not _valid_post_url(url, post_id):
        return None
    text = _clean_multiline(item.get("text"))
    if not text:
        return None
    translation = _clean_multiline(item.get("localized_text"))
    relevance = _classify_feed_post(item)
    source_time = parse_datetime(item.get("at") or item.get("created_at"))
    fingerprint = _fingerprint([post_id, text, translation, relevance, source_time.isoformat() if source_time else ""])
    return TiboPost(
        post_id=post_id,
        text=text,
        url=url,
        source_time=source_time,
        translation=translation,
        relevance=relevance,
        content_fingerprint=fingerprint,
        last_seen_at=collected_at,
        source_names=(SOURCE_CODEX_RESET,),
    )


def _event_status(item: Mapping[str, Any]) -> str:
    verification = _localized_value(item, "reset_verification_status").lower()
    if verification == "confirmed":
        return EVENT_CONFIRMED
    if verification == "rejected":
        return EVENT_REJECTED
    if bool(item.get("preview")) and isinstance(item.get("official_window"), Mapping):
        return EVENT_EXPECTED_WINDOW
    source = _localized_value(item, "source").lower()
    confidence = _localized_value(item, "confidence").lower()
    group = _localized_value(item, "group").lower()
    event_type = _localized_value(item, "type").lower()
    summary = _localized_value(item, "summary")
    if source == "archive" and confidence == "high" and not bool(item.get("preview")):
        if event_type != "credits" and not (group == "credits" and "reset" not in summary.lower()):
            return EVENT_CONFIRMED
    announcement_state = _localized_value(item, "announcement_state").lower()
    if announcement_state in {"announced", "hinted"}:
        return EVENT_OFFICIAL_ANNOUNCEMENT
    if verification in {"pending", ""} and (group == "reset" or event_type == "reset"):
        return EVENT_SUSPECTED
    return EVENT_UNCONFIRMED


def _event_from_payload(item: Mapping[str, Any], collected_at: datetime) -> ResetEvent | None:
    event_id = _localized_value(item, "id")
    url = _localized_value(item, "url")
    if not event_id or not _valid_post_url(url, event_id):
        return None
    window = item.get("official_window")
    if not isinstance(window, Mapping):
        window = {}
    status = _event_status(item)
    summary = _clean_multiline(item.get("summary"))
    localized_summary = _clean_multiline(item.get("localized_summary"))
    announced_at = parse_datetime(item.get("announced_at"))
    effective_at = parse_datetime(item.get("effective_at"))
    window_start = parse_datetime(window.get("start_at"))
    window_end = parse_datetime(window.get("end_at"))
    fingerprint = _fingerprint(
        [
            event_id,
            summary,
            localized_summary,
            announced_at.isoformat() if announced_at else "",
            effective_at.isoformat() if effective_at else "",
            window_start.isoformat() if window_start else "",
            window_end.isoformat() if window_end else "",
            status,
            item.get("reset_verification_status"),
        ]
    )
    return ResetEvent(
        event_id=event_id,
        summary=summary,
        localized_summary=localized_summary,
        url=url,
        announced_at=announced_at,
        effective_at=effective_at,
        window_start=window_start,
        window_end=window_end,
        window_label=_localized_value(window, "label"),
        window_timezone=_localized_value(window, "time_zone"),
        preview=bool(item.get("preview")),
        scope=_localized_value(item, "scope"),
        confidence=_localized_value(item, "confidence"),
        source=_localized_value(item, "source"),
        source_label=_localized_value(item, "source_label"),
        reset_kind=_localized_value(item, "reset_kind"),
        event_type=_localized_value(item, "type"),
        group=_localized_value(item, "group"),
        audience=tuple(str(value) for value in (item.get("audience") or []) if value),
        reason_tags=tuple(str(value) for value in (item.get("reason_tags") or []) if value),
        announcement_state=_localized_value(item, "announcement_state"),
        observation_result=_localized_value(item, "observation_result"),
        reset_verification_status=_localized_value(item, "reset_verification_status"),
        status=status,
        content_fingerprint=fingerprint,
        first_seen_at=None,
        last_seen_at=collected_at,
        source_names=(SOURCE_CODEX_RESET,),
        metadata={
            "is_reply": bool(item.get("is_reply")),
            "replying_to": _localized_value(item, "replying_to"),
            "observation_sources": item.get("observation_sources") or [],
        },
    )


def parse_codex_reset_feed(payload: Mapping[str, Any], *, collected_at: datetime | None = None) -> SourceCollection:
    collected_at = collected_at or _now()
    profile = payload.get("profile")
    if not isinstance(profile, Mapping) or _localized_value(profile, "handle").lower() != TIBO_HANDLE:
        raise RadarSourceParseError("codex-reset feed is not the Tibo timeline")
    tweets = payload.get("tweets")
    if not isinstance(tweets, list):
        raise RadarSourceParseError("codex-reset feed has no tweets list")
    posts = [post for item in tweets if isinstance(item, Mapping) if (post := _feed_post(item, collected_at)) is not None]
    if not posts:
        raise RadarSourceParseError("codex-reset feed contains no canonical Tibo posts")
    fingerprints = [[post.post_id, post.content_fingerprint] for post in posts]
    fetched_at = parse_datetime(payload.get("fetched_at"))
    state = SourceState(
        source_name=SOURCE_CODEX_RESET,
        last_success_at=collected_at,
        last_attempt_at=collected_at,
        upstream_updated_at=fetched_at,
        fingerprint=_fingerprint(fingerprints),
        stale=bool(payload.get("stale")),
    )
    return SourceCollection(SOURCE_CODEX_RESET, posts, [], state)


def parse_codex_reset_timeline(payload: Mapping[str, Any], *, collected_at: datetime | None = None) -> SourceCollection:
    collected_at = collected_at or _now()
    events_payload = payload.get("events")
    if not isinstance(events_payload, list):
        raise RadarSourceParseError("codex-reset timeline has no events list")
    events = [event for item in events_payload if isinstance(item, Mapping) if (event := _event_from_payload(item, collected_at)) is not None]
    if not events:
        raise RadarSourceParseError("codex-reset timeline contains no canonical events")
    updated_at = parse_datetime(payload.get("updated_at"))
    state = SourceState(
        source_name=SOURCE_CODEX_RESET,
        last_success_at=collected_at,
        last_attempt_at=collected_at,
        upstream_updated_at=updated_at,
        fingerprint=_fingerprint([[event.event_id, event.content_fingerprint] for event in events]),
        stale=False,
    )
    return SourceCollection(SOURCE_CODEX_RESET, [], events, state)


class TiboRadarClient:
    def __init__(
        self,
        *,
        codexradar_url: str = CODEXRADAR_HOME,
        feed_url: str = CODEX_RESET_FEED,
        timeline_url: str = CODEX_RESET_TIMELINE,
        timeout_seconds: float = 15.0,
        ttl_seconds: float = 600.0,
        codexradar_enabled: bool = True,
    ) -> None:
        self.codexradar_url = codexradar_url
        self.feed_url = feed_url
        self.timeline_url = timeline_url
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.codexradar_enabled = bool(codexradar_enabled)

    async def collect(self) -> CollectionResult:
        now = _now()
        jobs: list[tuple[str, Any]] = []
        if self.codexradar_enabled:
            jobs.append((SOURCE_CODEXRADAR, self._fetch_codexradar()))
        jobs.extend(
            [
                ("feed", self._fetch_feed()),
                ("timeline", self._fetch_timeline()),
            ]
        )
        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        collections: list[SourceCollection] = []
        for (name, _), result in zip(jobs, results):
            if isinstance(result, BaseException):
                source_name = SOURCE_CODEXRADAR if name == SOURCE_CODEXRADAR else SOURCE_CODEX_RESET
                logger.warning(f"[tibo_radar] source {source_name} failed: {result}")
                collections.append(
                    SourceCollection(
                        source_name,
                        [],
                        [],
                        SourceState(
                            source_name=source_name,
                            last_attempt_at=now,
                            last_error=str(result),
                            consecutive_failures=1,
                        ),
                    )
                )
            elif isinstance(result, SourceCollection):
                collections.append(result)
        return CollectionResult(collections)

    async def _fetch_codexradar(self) -> SourceCollection:
        resource = await fetch_bytes(
            self.codexradar_url,
            namespace="tibo-radar/codexradar",
            headers={"User-Agent": "otae-bot-entari-tibo-radar/1.0"},
            timeout_seconds=self.timeout_seconds,
            ttl_seconds=self.ttl_seconds,
            max_bytes=6 * 1024 * 1024,
        )
        return parse_codexradar_html(resource.content)

    async def _fetch_feed(self) -> SourceCollection:
        payload = await fetch_json(
            self.feed_url,
            namespace="tibo-radar/codex-reset-feed",
            headers={"User-Agent": "otae-bot-entari-tibo-radar/1.0"},
            timeout_seconds=self.timeout_seconds,
            ttl_seconds=self.ttl_seconds,
            max_bytes=6 * 1024 * 1024,
        )
        if not isinstance(payload, Mapping):
            raise RadarSourceParseError("codex-reset feed is not an object")
        return parse_codex_reset_feed(payload)

    async def _fetch_timeline(self) -> SourceCollection:
        payload = await fetch_json(
            self.timeline_url,
            namespace="tibo-radar/codex-reset-timeline",
            headers={"User-Agent": "otae-bot-entari-tibo-radar/1.0"},
            timeout_seconds=self.timeout_seconds,
            ttl_seconds=self.ttl_seconds,
            max_bytes=6 * 1024 * 1024,
        )
        if not isinstance(payload, Mapping):
            raise RadarSourceParseError("codex-reset timeline is not an object")
        return parse_codex_reset_timeline(payload)
