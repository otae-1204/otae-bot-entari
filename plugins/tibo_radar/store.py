"""SQLite persistence for Tibo posts, reset events and source health."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import (
    EVENT_CONFIRMED,
    ResetEvent,
    SourceState,
    TiboSubscription,
    TiboPost,
    iso_or_empty,
    parse_datetime,
)


DATA_DIR = Path("data") / "tibo_radar"
DB_PATH = DATA_DIR / "tibo_radar.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None) -> str:
    return iso_or_empty(value)


def _dt(value: object) -> datetime | None:
    return parse_datetime(value)


def _json(value: Iterable[str] | dict) -> str:
    return json.dumps(list(value) if not isinstance(value, dict) else value, ensure_ascii=False, separators=(",", ":"))


def _tuple(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in loaded if item)


def _union(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*(str(item) for item in left if item), *(str(item) for item in right if item)]))


def _timezone(name: str):
    """Use stdlib zoneinfo, with the project's existing pytz fallback."""

    try:
        return ZoneInfo(name)
    except Exception:
        try:
            import pytz

            return pytz.timezone(name)
        except Exception:
            return timezone.utc


class TiboStore:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def close(self) -> None:
        connection = self.conn
        self.conn = None
        if connection is not None:
            connection.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tibo_posts (
                post_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                url TEXT NOT NULL,
                source_time TEXT NOT NULL DEFAULT '',
                translation TEXT NOT NULL DEFAULT '',
                analysis TEXT NOT NULL DEFAULT '',
                relevance TEXT NOT NULL DEFAULT 'none',
                phrases TEXT NOT NULL DEFAULT '',
                page_updated_at TEXT NOT NULL DEFAULT '',
                content_fingerprint TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                source_names TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tibo_posts_time ON tibo_posts(source_time DESC);
            CREATE INDEX IF NOT EXISTS idx_tibo_posts_relevance ON tibo_posts(relevance, source_time DESC);

            CREATE TABLE IF NOT EXISTS tibo_events (
                event_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                localized_summary TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                announced_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                window_start TEXT NOT NULL DEFAULT '',
                window_end TEXT NOT NULL DEFAULT '',
                window_label TEXT NOT NULL DEFAULT '',
                window_timezone TEXT NOT NULL DEFAULT '',
                preview INTEGER NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_label TEXT NOT NULL DEFAULT '',
                reset_kind TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                event_group TEXT NOT NULL DEFAULT '',
                audience TEXT NOT NULL DEFAULT '[]',
                reason_tags TEXT NOT NULL DEFAULT '[]',
                announcement_state TEXT NOT NULL DEFAULT '',
                observation_result TEXT NOT NULL DEFAULT '',
                reset_verification_status TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unconfirmed',
                content_fingerprint TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                source_names TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tibo_events_time ON tibo_events(announced_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tibo_events_status ON tibo_events(status, announced_at DESC);

            CREATE TABLE IF NOT EXISTS tibo_source_state (
                source_name TEXT PRIMARY KEY,
                last_success_at TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT NOT NULL DEFAULT '',
                upstream_updated_at TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                stale INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tibo_subscriptions (
                group_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_notified_at TEXT NOT NULL DEFAULT '',
                last_notified_post_id TEXT NOT NULL DEFAULT '',
                subscribed_at TEXT NOT NULL,
                baseline_pending INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tibo_subscriptions_enabled
                ON tibo_subscriptions(enabled, group_id);
            """
        )
        subscription_columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(tibo_subscriptions)").fetchall()
        }
        if "baseline_pending" not in subscription_columns:
            self.conn.execute(
                "ALTER TABLE tibo_subscriptions ADD COLUMN baseline_pending INTEGER NOT NULL DEFAULT 0"
            )
        for name, definition in {
            "delivery_failures": "INTEGER NOT NULL DEFAULT 0",
            "retry_after": "TEXT NOT NULL DEFAULT ''",
            "last_delivery_error": "TEXT NOT NULL DEFAULT ''",
            "last_delivery_mode": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in subscription_columns:
                self.conn.execute(f"ALTER TABLE tibo_subscriptions ADD COLUMN {name} {definition}")
        self.conn.commit()

    def upsert_post(self, post: TiboPost) -> bool:
        """Persist a post and return ``True`` only when it is first seen."""

        now = _now()
        existing = self.conn.execute("SELECT * FROM tibo_posts WHERE post_id = ?", (post.post_id,)).fetchone()
        if existing is None:
            first_seen = post.first_seen_at or post.last_seen_at or now
            source_names = post.source_names
            values = self._post_values(post, first_seen, post.last_seen_at or now, source_names, now)
            self.conn.execute(
                """
                INSERT INTO tibo_posts(
                    post_id,text,url,source_time,translation,analysis,relevance,phrases,
                    page_updated_at,content_fingerprint,first_seen_at,last_seen_at,source_names,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            self.conn.commit()
            return True
        else:
            current_sources = _tuple(existing["source_names"])
            incoming_sources = _union(current_sources, post.source_names)
            from_codexradar = "codexradar" in post.source_names
            # CodexRadar owns the user-facing Chinese interpretation.  A
            # fallback JSON source can only fill missing fields for an already
            # enriched CodexRadar row.
            text = post.text if from_codexradar or not existing["text"] else str(existing["text"])
            url = post.url if from_codexradar or not existing["url"] else str(existing["url"])
            source_time = _ts(post.source_time) or str(existing["source_time"])
            translation = post.translation if from_codexradar or not existing["translation"] else str(existing["translation"])
            analysis = post.analysis if from_codexradar or not existing["analysis"] else str(existing["analysis"])
            relevance = post.relevance if from_codexradar else str(existing["relevance"])
            phrases = post.phrases if from_codexradar or not existing["phrases"] else str(existing["phrases"])
            page_updated_at = _ts(post.page_updated_at) or str(existing["page_updated_at"])
            fingerprint = post.content_fingerprint if from_codexradar or not existing["content_fingerprint"] else str(existing["content_fingerprint"])
            last_seen = post.last_seen_at or now
            self.conn.execute(
                """
                UPDATE tibo_posts SET text=?,url=?,source_time=?,translation=?,analysis=?,relevance=?,phrases=?,
                    page_updated_at=?,content_fingerprint=?,last_seen_at=?,source_names=?,updated_at=?
                WHERE post_id=?
                """,
                (text, url, source_time, translation, analysis, relevance, phrases, page_updated_at, fingerprint, _ts(last_seen), _json(incoming_sources), _ts(now), post.post_id),
            )
        self.conn.commit()
        return False

    @staticmethod
    def _post_values(post: TiboPost, first_seen: datetime, last_seen: datetime, source_names: Iterable[str], now: datetime) -> tuple:
        return (
            post.post_id,
            post.text,
            post.url,
            _ts(post.source_time),
            post.translation,
            post.analysis,
            post.relevance,
            post.phrases,
            _ts(post.page_updated_at),
            post.content_fingerprint,
            _ts(first_seen),
            _ts(last_seen),
            _json(source_names),
            _ts(now),
        )

    def upsert_event(self, event: ResetEvent) -> None:
        now = _now()
        existing = self.conn.execute("SELECT * FROM tibo_events WHERE event_id = ?", (event.event_id,)).fetchone()
        if existing is None:
            first_seen = event.first_seen_at or event.last_seen_at or now
            self.conn.execute(
                """
                INSERT INTO tibo_events(
                    event_id,summary,localized_summary,url,announced_at,effective_at,window_start,window_end,
                    window_label,window_timezone,preview,scope,confidence,source,source_label,reset_kind,
                    event_type,event_group,audience,reason_tags,announcement_state,observation_result,
                    reset_verification_status,status,content_fingerprint,metadata,first_seen_at,last_seen_at,
                    source_names,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                self._event_values(event, first_seen, event.last_seen_at or now, event.source_names, now),
            )
        else:
            sources = _union(_tuple(existing["source_names"]), event.source_names)
            self.conn.execute(
                """
                UPDATE tibo_events SET summary=?,localized_summary=?,url=?,announced_at=?,effective_at=?,
                    window_start=?,window_end=?,window_label=?,window_timezone=?,preview=?,scope=?,confidence=?,
                    source=?,source_label=?,reset_kind=?,event_type=?,event_group=?,audience=?,reason_tags=?,
                    announcement_state=?,observation_result=?,reset_verification_status=?,status=?,
                    content_fingerprint=?,metadata=?,last_seen_at=?,source_names=?,updated_at=?
                WHERE event_id=?
                """,
                self._event_update_values(event, existing, sources, now),
            )
        self.conn.commit()

    @staticmethod
    def _event_values(event: ResetEvent, first_seen: datetime, last_seen: datetime, source_names: Iterable[str], now: datetime) -> tuple:
        return (
            event.event_id, event.summary, event.localized_summary, event.url, _ts(event.announced_at), _ts(event.effective_at),
            _ts(event.window_start), _ts(event.window_end), event.window_label, event.window_timezone, int(event.preview),
            event.scope, event.confidence, event.source, event.source_label, event.reset_kind, event.event_type, event.group,
            _json(event.audience), _json(event.reason_tags), event.announcement_state, event.observation_result,
            event.reset_verification_status, event.status, event.content_fingerprint, _json(dict(event.metadata)),
            _ts(first_seen), _ts(last_seen), _json(source_names), _ts(now),
        )

    @staticmethod
    def _event_update_values(event: ResetEvent, existing: sqlite3.Row, sources: Iterable[str], now: datetime) -> tuple:
        def prefer(incoming: str, key: str) -> str:
            return incoming or str(existing[key] or "")

        return (
            prefer(event.summary, "summary"), prefer(event.localized_summary, "localized_summary"), prefer(event.url, "url"),
            _ts(event.announced_at) or str(existing["announced_at"]), _ts(event.effective_at) or str(existing["effective_at"]),
            _ts(event.window_start) or str(existing["window_start"]), _ts(event.window_end) or str(existing["window_end"]),
            prefer(event.window_label, "window_label"), prefer(event.window_timezone, "window_timezone"), int(event.preview),
            prefer(event.scope, "scope"), prefer(event.confidence, "confidence"), prefer(event.source, "source"),
            prefer(event.source_label, "source_label"), prefer(event.reset_kind, "reset_kind"), prefer(event.event_type, "event_type"),
            prefer(event.group, "event_group"), _json(event.audience or _tuple(existing["audience"])),
            _json(event.reason_tags or _tuple(existing["reason_tags"])), prefer(event.announcement_state, "announcement_state"),
            prefer(event.observation_result, "observation_result"), prefer(event.reset_verification_status, "reset_verification_status"),
            event.status or str(existing["status"]), event.content_fingerprint or str(existing["content_fingerprint"]),
            _json(dict(event.metadata) if event.metadata else json.loads(str(existing["metadata"] or "{}"))),
            _ts(event.last_seen_at or now), _json(sources), _ts(now), event.event_id,
        )

    def upsert_source_state(self, state: SourceState) -> None:
        now = _now()
        existing = self.conn.execute("SELECT * FROM tibo_source_state WHERE source_name = ?", (state.source_name,)).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO tibo_source_state(source_name,last_success_at,last_attempt_at,upstream_updated_at,
                    fingerprint,stale,last_error,consecutive_failures,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (state.source_name, _ts(state.last_success_at), _ts(state.last_attempt_at), _ts(state.upstream_updated_at), state.fingerprint, int(state.stale), state.last_error, state.consecutive_failures, _ts(now)),
            )
        else:
            success = state.last_success_at is not None
            last_success = _ts(state.last_success_at) if success else str(existing["last_success_at"] or "")
            failures = 0 if success else int(existing["consecutive_failures"] or 0) + max(1, state.consecutive_failures)
            self.conn.execute(
                """
                UPDATE tibo_source_state SET last_success_at=?,last_attempt_at=?,upstream_updated_at=?,fingerprint=?,
                    stale=?,last_error=?,consecutive_failures=?,updated_at=? WHERE source_name=?
                """,
                (last_success, _ts(state.last_attempt_at) or str(existing["last_attempt_at"] or ""), _ts(state.upstream_updated_at) or str(existing["upstream_updated_at"] or ""), state.fingerprint or str(existing["fingerprint"] or ""), int(state.stale), state.last_error, failures, _ts(now), state.source_name),
            )
        self.conn.commit()

    def posts(self, *, relevant_only: bool = False, limit: int = 6) -> list[TiboPost]:
        limit = max(1, min(int(limit), 100))
        where = "WHERE relevance != 'none'" if relevant_only else ""
        cursor_expr = "COALESCE(NULLIF(source_time,''),NULLIF(last_seen_at,''),NULLIF(first_seen_at,''))"
        rows = self.conn.execute(
            f"SELECT * FROM tibo_posts {where} ORDER BY {cursor_expr} DESC, post_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._post_from_row(row) for row in rows]

    def latest_post_cursor(self) -> tuple[str, str]:
        """Return the newest persisted post cursor, or now for an empty store."""

        cursor_expr = "COALESCE(NULLIF(first_seen_at,''),NULLIF(last_seen_at,''),NULLIF(source_time,''))"
        row = self.conn.execute(
            f"SELECT post_id, {cursor_expr} AS cursor_at FROM tibo_posts "
            "ORDER BY cursor_at DESC, post_id DESC LIMIT 1"
        ).fetchone()
        if row and row["cursor_at"]:
            return str(row["cursor_at"]), str(row["post_id"])
        return _ts(_now()), ""

    def posts_after(self, cursor_at: str = "", cursor_post_id: str = "", *, limit: int = 100) -> list[TiboPost]:
        """Read posts strictly after a subscription cursor, oldest first."""

        limit = max(1, min(int(limit), 500))
        cursor_expr = "COALESCE(NULLIF(first_seen_at,''),NULLIF(last_seen_at,''),NULLIF(source_time,''))"
        if cursor_at:
            where = f"WHERE ({cursor_expr} > ? OR ({cursor_expr} = ? AND post_id > ?))"
            params: tuple[object, ...] = (cursor_at, cursor_at, cursor_post_id, limit)
        else:
            where = "WHERE 1=1"
            params = (limit,)
        rows = self.conn.execute(
            f"SELECT * FROM tibo_posts {where} ORDER BY {cursor_expr} ASC, post_id ASC LIMIT ?",
            params,
        ).fetchall()
        return [self._post_from_row(row) for row in rows]

    def subscribe(self, group_id: str | int, channel_id: str | int = "") -> tuple[bool, TiboSubscription]:
        """Enable a group subscription and start at the current newest post."""

        gid = str(group_id).strip()
        if not gid:
            raise ValueError("group_id is required")
        target = str(channel_id or gid).strip()
        now = _now()
        cursor_at, cursor_post_id = self.latest_post_cursor()
        baseline_pending = not bool(cursor_post_id)
        existing = self.conn.execute(
            "SELECT enabled FROM tibo_subscriptions WHERE group_id = ?", (gid,)
        ).fetchone()
        if existing and existing["enabled"]:
            self.conn.execute(
                "UPDATE tibo_subscriptions SET channel_id=?, updated_at=? WHERE group_id=?",
                (target, _ts(now), gid),
            )
            self.conn.commit()
            subscription = self.subscription(gid)
            if subscription is None:  # pragma: no cover - guarded by the row above
                raise RuntimeError("failed to load Tibo subscription")
            return True, subscription
        self.conn.execute(
            """
            INSERT INTO tibo_subscriptions(
                group_id,channel_id,enabled,last_notified_at,last_notified_post_id,subscribed_at,baseline_pending,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                enabled=1,
                last_notified_at=excluded.last_notified_at,
                last_notified_post_id=excluded.last_notified_post_id,
                subscribed_at=excluded.subscribed_at,
                baseline_pending=excluded.baseline_pending,
                delivery_failures=0,retry_after='',last_delivery_error='',last_delivery_mode='',
                updated_at=excluded.updated_at
            """,
            (gid, target, 1, cursor_at, cursor_post_id, _ts(now), int(baseline_pending), _ts(now)),
        )
        self.conn.commit()
        subscription = self.subscription(gid)
        if subscription is None:  # pragma: no cover - guarded by the INSERT above
            raise RuntimeError("failed to persist Tibo subscription")
        return bool(existing and existing["enabled"]), subscription

    def unsubscribe(self, group_id: str | int) -> bool:
        gid = str(group_id).strip()
        if not gid:
            return False
        updated = self.conn.execute(
            "UPDATE tibo_subscriptions SET enabled=0, updated_at=? WHERE group_id=? AND enabled=1",
            (_ts(_now()), gid),
        )
        self.conn.commit()
        return updated.rowcount > 0

    def subscription(self, group_id: str | int) -> TiboSubscription | None:
        row = self.conn.execute(
            "SELECT * FROM tibo_subscriptions WHERE group_id = ?", (str(group_id).strip(),)
        ).fetchone()
        return self._subscription_from_row(row) if row else None

    def subscriptions(self) -> list[TiboSubscription]:
        rows = self.conn.execute(
            "SELECT * FROM tibo_subscriptions WHERE enabled=1 ORDER BY group_id"
        ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def mark_subscription_delivered(self, group_id: str | int, cursor_at: str, post_id: str, *, mode: str = "image") -> None:
        self.conn.execute(
            """
            UPDATE tibo_subscriptions
            SET last_notified_at=?, last_notified_post_id=?, baseline_pending=0, updated_at=?,
                delivery_failures=0,retry_after='',last_delivery_error='',last_delivery_mode=?
            WHERE group_id=? AND enabled=1
                AND (last_notified_at < ? OR (last_notified_at = ? AND last_notified_post_id <= ?))
            """,
            (str(cursor_at or ""), str(post_id or ""), _ts(_now()), mode, str(group_id).strip(),
             str(cursor_at or ""), str(cursor_at or ""), str(post_id or "")),
        )
        self.conn.commit()

    def mark_subscription_failed(self, group_id: str | int, error_type: str, *, now: datetime | None = None) -> None:
        """Keep the delivery cursor and persist backoff, including across restarts."""
        current = self.subscription(group_id)
        if current is None or not current.enabled:
            return
        now = now or _now()
        failures = current.delivery_failures + 1
        delay = min(3600, 60 * (2 ** min(failures - 1, 6)))
        self.conn.execute(
            "UPDATE tibo_subscriptions SET delivery_failures=?,retry_after=?,last_delivery_error=?,updated_at=? "
            "WHERE group_id=? AND enabled=1",
            (failures, _ts(now + timedelta(seconds=delay)), str(error_type)[:80], _ts(now), current.group_id),
        )
        self.conn.commit()

    def mark_subscription_initialized(self, group_id: str | int) -> None:
        """Discard the initial snapshot without sending it to a new subscriber."""

        cursor_at, post_id = self.latest_post_cursor()
        self.conn.execute(
            """
            UPDATE tibo_subscriptions
            SET last_notified_at=?, last_notified_post_id=?, baseline_pending=0, updated_at=?
            WHERE group_id=? AND enabled=1
            """,
            (cursor_at, post_id, _ts(_now()), str(group_id).strip()),
        )
        self.conn.commit()

    def events(self, *, limit: int = 20, include_rejected: bool = True) -> list[ResetEvent]:
        limit = max(1, min(int(limit), 100))
        where = "" if include_rejected else "WHERE status != 'rejected'"
        rows = self.conn.execute(f"SELECT * FROM tibo_events {where} ORDER BY COALESCE(effective_at,announced_at) DESC LIMIT ?", (limit,)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def latest_confirmed(self) -> ResetEvent | None:
        row = self.conn.execute(
            "SELECT * FROM tibo_events WHERE status = ? ORDER BY COALESCE(effective_at,announced_at) DESC LIMIT 1",
            (EVENT_CONFIRMED,),
        ).fetchone()
        return self._event_from_row(row) if row else None

    def source_states(self) -> list[SourceState]:
        rows = self.conn.execute("SELECT * FROM tibo_source_state ORDER BY source_name").fetchall()
        return [
            SourceState(
                source_name=str(row["source_name"]), last_success_at=_dt(row["last_success_at"]), last_attempt_at=_dt(row["last_attempt_at"]),
                upstream_updated_at=_dt(row["upstream_updated_at"]), fingerprint=str(row["fingerprint"] or ""), stale=bool(row["stale"]),
                last_error=str(row["last_error"] or ""), consecutive_failures=int(row["consecutive_failures"] or 0),
            )
            for row in rows
        ]

    def reset_stats(self) -> dict:
        events = self.events(limit=1000, include_rejected=False)
        confirmed = [event for event in events if event.status == EVENT_CONFIRMED]
        confirmed.sort(key=lambda event: event.effective_at or event.announced_at or datetime.min.replace(tzinfo=timezone.utc))
        times = [event.effective_at or event.announced_at for event in confirmed]
        times = [value for value in times if value]
        intervals_hours = [round((right - left).total_seconds() / 3600, 2) for left, right in zip(times, times[1:])]
        bjt = _timezone("Asia/Shanghai")
        hour_counts: dict[int, int] = {}
        for value in times:
            hour = value.astimezone(bjt).hour
            hour_counts[hour] = hour_counts.get(hour, 0) + 1
        return {
            "sample_count": len(times),
            "intervals_hours": intervals_hours[-5:],
            "beijing_hour_counts": dict(sorted(hour_counts.items())),
            "latest_interval_hours": intervals_hours[-1] if intervals_hours else None,
        }

    @staticmethod
    def _post_from_row(row: sqlite3.Row) -> TiboPost:
        return TiboPost(
            post_id=str(row["post_id"]), text=str(row["text"] or ""), url=str(row["url"] or ""), source_time=_dt(row["source_time"]),
            translation=str(row["translation"] or ""), analysis=str(row["analysis"] or ""), relevance=str(row["relevance"] or "none"),
            phrases=str(row["phrases"] or ""), page_updated_at=_dt(row["page_updated_at"]), content_fingerprint=str(row["content_fingerprint"] or ""),
            first_seen_at=_dt(row["first_seen_at"]), last_seen_at=_dt(row["last_seen_at"]), source_names=_tuple(row["source_names"]),
        )

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> TiboSubscription:
        return TiboSubscription(
            group_id=str(row["group_id"]),
            channel_id=str(row["channel_id"] or ""),
            enabled=bool(row["enabled"]),
            last_notified_at=_dt(row["last_notified_at"]),
            last_notified_post_id=str(row["last_notified_post_id"] or ""),
            subscribed_at=_dt(row["subscribed_at"]),
            baseline_pending=bool(row["baseline_pending"]),
            delivery_failures=int(row["delivery_failures"]),
            retry_after=_dt(row["retry_after"]),
            last_delivery_error=str(row["last_delivery_error"] or ""),
            last_delivery_mode=str(row["last_delivery_mode"] or ""),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ResetEvent:
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except ValueError:
            metadata = {}
        return ResetEvent(
            event_id=str(row["event_id"]), summary=str(row["summary"] or ""), localized_summary=str(row["localized_summary"] or ""), url=str(row["url"] or ""),
            announced_at=_dt(row["announced_at"]), effective_at=_dt(row["effective_at"]), window_start=_dt(row["window_start"]), window_end=_dt(row["window_end"]),
            window_label=str(row["window_label"] or ""), window_timezone=str(row["window_timezone"] or ""), preview=bool(row["preview"]), scope=str(row["scope"] or ""),
            confidence=str(row["confidence"] or ""), source=str(row["source"] or ""), source_label=str(row["source_label"] or ""), reset_kind=str(row["reset_kind"] or ""),
            event_type=str(row["event_type"] or ""), group=str(row["event_group"] or ""), audience=_tuple(row["audience"]), reason_tags=_tuple(row["reason_tags"]),
            announcement_state=str(row["announcement_state"] or ""), observation_result=str(row["observation_result"] or ""), reset_verification_status=str(row["reset_verification_status"] or ""),
            status=str(row["status"] or "unconfirmed"), content_fingerprint=str(row["content_fingerprint"] or ""), first_seen_at=_dt(row["first_seen_at"]), last_seen_at=_dt(row["last_seen_at"]),
            source_names=_tuple(row["source_names"]), metadata=metadata,
        )
