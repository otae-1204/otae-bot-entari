"""Transactional snapshots and outbox. Connections never cross worker threads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .models import Announcement, Delivery, Destination, Subscription, digest, message

RETENTION_SECONDS = 90 * 86400
CATCHUP_SECONDS = 2 * 3600


def _subscription(raw: str) -> Subscription:
    data = json.loads(raw)
    data["destination"] = Destination(**data["destination"])
    data["kinds"] = tuple(data["kinds"])
    return Subscription(**data)


class AnnouncementStore:
    def __init__(self, path: str | Path = Path("data/endfield/announcements.db")):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS articles (
                    cid TEXT PRIMARY KEY, payload TEXT NOT NULL, refreshed_at INTEGER NOT NULL,
                    retire_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS withdrawn_windows (
                    key TEXT PRIMARY KEY, published_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    key TEXT PRIMARY KEY, subscription_key TEXT NOT NULL REFERENCES subscriptions(key) ON DELETE CASCADE,
                    article_id TEXT NOT NULL, text TEXT NOT NULL, due_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_at INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
                    reminder INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state,next_at,due_at);
            """)
            # Serialize read-modify-write transactions with command updates;
            # SQLite's default deferred BEGIN starts too late for that.
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _save_subscription(conn, sub: Subscription):
        conn.execute(
            "INSERT OR REPLACE INTO subscriptions VALUES (?,?)",
            (
                sub.destination.key,
                json.dumps(asdict(sub), ensure_ascii=False),
            ),
        )

    def subscribe(self, sub: Subscription) -> Subscription:
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT payload FROM subscriptions WHERE key=?", (sub.destination.key,)
            ).fetchone()
            if previous:
                old = _subscription(previous[0])
                sub = replace(
                    sub, created_at=old.created_at, initialized=old.initialized
                )
                # UPDATE rather than REPLACE: preserve sent receipts and avoid
                # the FK cascade on the existing subscription row.
                conn.execute(
                    "UPDATE subscriptions SET payload=? WHERE key=?",
                    (
                        json.dumps(asdict(sub), ensure_ascii=False),
                        sub.destination.key,
                    ),
                )
                conn.execute(
                    "UPDATE outbox SET state='cancelled' WHERE subscription_key=? AND state='pending' AND reminder=1",
                    (sub.destination.key,),
                )
                # Replanning a lead time must not discard unrelated news that
                # is waiting for delivery. Cancel it only when its type is no
                # longer selected; enabling another type does not backfill it.
                for row in conn.execute(
                    """SELECT outbox.key,articles.payload FROM outbox
                    JOIN articles ON articles.cid=outbox.article_id
                    WHERE subscription_key=? AND state='pending' AND reminder=0""",
                    (sub.destination.key,),
                ).fetchall():
                    if not set(sub.kinds).intersection(
                        Announcement.loads(row["payload"]).kinds
                    ):
                        conn.execute(
                            "UPDATE outbox SET state='cancelled' WHERE key=?",
                            (row["key"],),
                        )
            else:
                self._save_subscription(conn, sub)
        return sub

    def unsubscribe(self, key: str) -> bool:
        with self.connect() as conn:
            return bool(
                conn.execute("DELETE FROM subscriptions WHERE key=?", (key,)).rowcount
            )

    def subscriptions(self) -> list[Subscription]:
        with self.connect() as conn:
            return [
                _subscription(row[0])
                for row in conn.execute(
                    "SELECT payload FROM subscriptions ORDER BY key"
                )
            ]

    def subscription(self, key: str) -> Subscription | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM subscriptions WHERE key=?", (key,)
            ).fetchone()
            return _subscription(row[0]) if row else None

    def articles(self) -> list[Announcement]:
        with self.connect() as conn:
            return sorted(
                (
                    Announcement.loads(row[0])
                    for row in conn.execute("SELECT payload FROM articles")
                ),
                key=lambda article: (article.published_at, article.cid),
                reverse=True,
            )

    def watch_ids(self, now: int) -> tuple[str, ...]:
        return tuple(
            article.cid
            for article in self.articles()
            if any(
                max(window.start_at, window.end_at) >= now for window in article.windows
            )
        )

    def metadata(self) -> dict[str, str]:
        with self.connect() as conn:
            return dict(conn.execute("SELECT key,value FROM metadata"))

    def set_error(self, error: str):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('last_error',?)",
                (error[:100],),
            )

    @staticmethod
    def _queue(conn, job: Delivery, *, reminder: bool = False):
        conn.execute(
            """
            INSERT INTO outbox(key,subscription_key,article_id,text,due_at,expires_at,reminder)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                text=excluded.text, due_at=excluded.due_at, expires_at=excluded.expires_at,
                article_id=excluded.article_id, state='pending'
            WHERE outbox.state IN ('pending','cancelled')
        """,
            (
                job.key,
                job.subscription_key,
                job.article_id,
                job.text,
                job.due_at,
                job.expires_at,
                int(reminder),
            ),
        )

    def apply_snapshot(self, articles: list[Announcement], now: int):
        with self.connect() as conn:
            subs = [
                _subscription(row[0])
                for row in conn.execute("SELECT payload FROM subscriptions")
            ]
            for article in articles:
                row = conn.execute(
                    "SELECT payload FROM articles WHERE cid=?", (article.cid,)
                ).fetchone()
                old = Announcement.loads(row[0]) if row else None
                changed = old is not None and old.fingerprint != article.fingerprint
                if changed:
                    conn.execute(
                        "UPDATE outbox SET state='cancelled' WHERE article_id=? AND state='pending'",
                        (article.cid,),
                    )
                    removed = {w.key for w in old.windows} - {
                        w.key for w in article.windows
                    }
                    for key in removed:
                        conn.execute(
                            """INSERT INTO withdrawn_windows VALUES (?,?,?) ON CONFLICT(key)
                            DO UPDATE SET published_at=MAX(published_at,excluded.published_at),updated_at=excluded.updated_at""",
                            (key, article.published_at, now),
                        )
                for window in article.windows:
                    conn.execute(
                        "DELETE FROM withdrawn_windows WHERE key=? AND published_at<=?",
                        (window.key, article.published_at),
                    )
                if old is None or changed:
                    for sub in subs:
                        if not sub.initialized or not set(sub.kinds).intersection(
                            article.kinds
                        ):
                            continue
                        if not changed and (
                            article.published_at < sub.created_at
                            or article.published_at < now - 2 * 86400
                        ):
                            continue
                        key = digest(
                            [
                                sub.destination.key,
                                article.cid,
                                "news",
                                article.fingerprint,
                            ]
                        )
                        self._queue(
                            conn,
                            Delivery(
                                key,
                                sub.destination.key,
                                article.cid,
                                message(article, updated=changed),
                                now,
                                now + CATCHUP_SECONDS,
                            ),
                        )
                retire_at = (
                    max(
                        [
                            article.published_at,
                            *(max(w.start_at, w.end_at) for w in article.windows),
                        ]
                    )
                    + RETENTION_SECONDS
                )
                conn.execute(
                    "INSERT OR REPLACE INTO articles VALUES (?,?,?,?)",
                    (article.cid, article.dumps(), now, retire_at),
                )
            for sub in subs:
                if not sub.initialized:
                    conn.execute(
                        "UPDATE subscriptions SET payload=? WHERE key=?",
                        (
                            json.dumps(
                                asdict(replace(sub, initialized=True)),
                                ensure_ascii=False,
                            ),
                            sub.destination.key,
                        ),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('last_success',?)", (str(now),)
            )
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('last_error','')")

    def plan(self, now: int):
        with self.connect() as conn:
            subs = [
                _subscription(row[0])
                for row in conn.execute("SELECT payload FROM subscriptions")
            ]
            withdrawn = dict(
                conn.execute("SELECT key,published_at FROM withdrawn_windows")
            )
            selected = {}
            for row in conn.execute("SELECT payload FROM articles").fetchall():
                article = Announcement.loads(row[0])
                for window in article.windows:
                    if article.published_at <= withdrawn.get(window.key, -1):
                        continue
                    previous = selected.get(window.key)
                    if previous is None or (article.published_at, article.cid) > (
                        previous[0].published_at,
                        previous[0].cid,
                    ):
                        selected[window.key] = (article, window)
            # Reconcile pending schedules, preserving sent receipts and retry
            # backoff. A revised newer article must supersede an old overview.
            conn.execute(
                "UPDATE outbox SET state='cancelled' WHERE reminder=1 AND state='pending'"
            )
            for article, window in selected.values():
                for sub in subs:
                    if not sub.initialized:
                        continue
                    if window.kind not in sub.kinds:
                        continue
                    phases = (
                        [("maintenance", window.start_at, sub.maintenance_minutes)]
                        if window.kind == "maintenance"
                        else [
                            ("start", window.start_at, sub.start_minutes),
                            ("end", window.end_at, sub.end_minutes),
                        ]
                    )
                    for phase, target, minutes in phases:
                        if not target or not minutes or target <= now:
                            continue
                        due = target - minutes * 60
                        expires = min(target, due + CATCHUP_SECONDS)
                        if expires <= now:
                            continue
                        key = digest([sub.destination.key, window.key, phase, target])
                        self._queue(
                            conn,
                            Delivery(
                                key,
                                sub.destination.key,
                                article.cid,
                                message(
                                    article, window=window, phase=phase, target=target
                                ),
                                due,
                                expires,
                            ),
                            reminder=True,
                        )
            conn.execute(
                "UPDATE outbox SET state='expired' WHERE state='pending' AND expires_at<=?",
                (now,),
            )
            conn.execute(
                "DELETE FROM outbox WHERE expires_at<?", (now - RETENTION_SECONDS,)
            )
            conn.execute("DELETE FROM articles WHERE retire_at<?", (now,))
            conn.execute(
                "DELETE FROM withdrawn_windows WHERE updated_at<?",
                (now - RETENTION_SECONDS,),
            )

    def due(self, now: int, per_subscription: int = 3) -> list[Delivery]:
        with self.connect() as conn:
            # One broken destination cannot occupy the entire global batch.
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY subscription_key ORDER BY due_at,key) AS position
                    FROM outbox WHERE state='pending' AND due_at<=? AND next_at<=? AND expires_at>?
                ) WHERE position<=? ORDER BY position,due_at,key LIMIT 48
            """,
                (now, now, now, per_subscription),
            ).fetchall()
            return [
                Delivery(
                    *(
                        row[key]
                        for key in (
                            "key",
                            "subscription_key",
                            "article_id",
                            "text",
                            "due_at",
                            "expires_at",
                            "attempts",
                        )
                    )
                )
                for row in rows
            ]

    def is_pending(self, key: str, now: int) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM outbox WHERE key=? AND state='pending' AND expires_at>?",
                    (key, now),
                ).fetchone()
                is not None
            )

    def sent(self, key: str):
        with self.connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='sent' WHERE key=? AND state='pending'", (key,)
            )

    def failed(self, key: str, now: int, attempts: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE outbox SET attempts=attempts+1,next_at=? WHERE key=? AND state='pending'",
                (
                    now + min(1800, 60 * 2 ** min(attempts, 5)),
                    key,
                ),
            )

    def pending_count(self, key: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE subscription_key=? AND state='pending'",
                (key,),
            ).fetchone()[0]
