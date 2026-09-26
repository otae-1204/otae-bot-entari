"""SQLite persistence for bilibilibot.

Everything runs on one dedicated worker thread: sqlite connections are bound to
the thread that created them, and serializing every statement there also keeps
the event loop free of synchronous disk I/O (refactor plan, section 5.3).

The public surface is async. The `_sync_*` methods hold the real
implementations and are callable directly from tests that already run inside the
executor thread.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from loguru import logger

from .models import (
    BiliEvent,
    KIND_DYNAMIC,
    KIND_LIVE,
    KIND_VIDEO,
    OutboxRow,
    SeenItem,
    SUPPORTED_KINDS,
    Subscription,
    TargetInfo,
)


DATA_DIR = Path("data") / "bilibilibot"
DB_PATH = DATA_DIR / "bilibili.db"
LEGACY_DB_PATH = Path(__file__).resolve().parent / "bilibili_2.db"


class BiliStore:
    def __init__(self, db_path: str | Path = DB_PATH, legacy_db_path: str | Path = LEGACY_DB_PATH):
        self.db_path = Path(db_path)
        self.legacy_db_path = Path(legacy_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # One thread for every statement: sqlite is used from exactly one place.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bili-sqlite")
        self.conn: sqlite3.Connection | None = None
        self._closed = False

    # --- lifecycle --------------------------------------------------------

    async def open(self) -> None:
        """Create the connection, the schema and the legacy migration."""
        await self._run(self._open_sync)

    async def close(self) -> None:
        """Idempotent: closing twice is a no-op."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._run(self._close_sync)
        finally:
            self._executor.shutdown(wait=False)

    def _open_sync(self) -> None:
        if self.conn is not None:
            return
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the writer; NORMAL is safe under WAL.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self.conn = conn
        self._init_schema_sync()
        self._migrate_legacy_if_needed_sync()

    def _close_sync(self) -> None:
        connection = self.conn
        self.conn = None
        if connection is not None:
            connection.close()

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    def _connection(self) -> sqlite3.Connection:
        if self.conn is None:
            # Defensive: a caller that skipped open() still gets a working store.
            self._open_sync()
        return self.conn

    # --- schema -----------------------------------------------------------

    def _init_schema_sync(self) -> None:
        conn = self._connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                uid TEXT NOT NULL,
                room_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                latest_id TEXT NOT NULL DEFAULT '',
                latest_ts INTEGER NOT NULL DEFAULT 0,
                is_live INTEGER NOT NULL DEFAULT 0,
                last_title TEXT NOT NULL DEFAULT '',
                last_cover TEXT NOT NULL DEFAULT '',
                last_desc TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0,
                live_started_at INTEGER NOT NULL DEFAULT 0,
                live_last_seen_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(kind, uid)
            )
            """
        )
        # Existing subscription databases gain timing fields without losing data.
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(targets)")}
        for column in ("live_started_at", "live_last_seen_at"):
            if column not in columns:
                cur.execute(f"ALTER TABLE targets ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_kind TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                subscriber_type TEXT NOT NULL,
                subscriber_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(target_kind, target_uid, subscriber_type, subscriber_id)
            )
            """
        )
        cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                kind TEXT NOT NULL,
                uid TEXT NOT NULL,
                item_id TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                PRIMARY KEY(kind, uid, item_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                uid TEXT NOT NULL,
                card_type TEXT NOT NULL,
                subscriber_type TEXT NOT NULL,
                subscriber_id TEXT NOT NULL,
                card_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(event_key, subscriber_type, subscriber_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_target ON subscriptions(target_kind, target_uid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_outbox_recipient ON outbox(subscriber_type, subscriber_id, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(next_attempt_at)")
        conn.commit()

    # --- meta -------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        return await self._run(self._get_meta_sync, key)

    async def set_meta(self, key: str, value: str) -> None:
        await self._run(self._set_meta_sync, key, value)

    def _get_meta_sync(self, key: str) -> str | None:
        row = self._connection().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _set_meta_sync(self, key: str, value: str) -> None:
        conn = self._connection()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()

    async def migrate_legacy_if_needed(self) -> None:
        await self._run(self._migrate_legacy_if_needed_sync)

    def _migrate_legacy_if_needed_sync(self) -> None:
        conn = self._connection()
        if self._get_meta_sync("legacy_migrated") == "1" or not self.legacy_db_path.exists():
            return
        try:
            legacy = sqlite3.connect(self.legacy_db_path)
            legacy.row_factory = sqlite3.Row
            for row in legacy.execute("SELECT up_uid, up_name, latest_update FROM up"):
                self._upsert_target_sync(
                    TargetInfo(KIND_VIDEO, str(row["up_uid"]), str(row["up_name"]), latest_ts=int(row["latest_update"] or 0)),
                    commit=False,
                )
            for row in legacy.execute("SELECT liver_uid, liver_name, is_live, live_room FROM liver"):
                self._upsert_target_sync(
                    TargetInfo(
                        KIND_LIVE,
                        str(row["liver_uid"]),
                        str(row["liver_name"]),
                        room_id=str(row["live_room"] or ""),
                        is_live=bool(row["is_live"]),
                    ),
                    commit=False,
                )
            for row in legacy.execute("SELECT uid, u_name, latest_timestamp FROM dynamic"):
                self._upsert_target_sync(
                    TargetInfo(KIND_DYNAMIC, str(row["uid"]), str(row["u_name"]), latest_ts=int(row["latest_timestamp"] or 0)),
                    commit=False,
                )

            relation_map = [
                (KIND_VIDEO, "up_follower", "up_uid"),
                (KIND_LIVE, "liver_follower", "liver_uid"),
                (KIND_DYNAMIC, "dynamic_follower", "uid"),
            ]
            for kind, table, id_col in relation_map:
                for row in legacy.execute(f"SELECT {id_col}, user_id, group_id FROM {table}"):
                    target_uid = str(row[id_col])
                    if row["user_id"] is not None:
                        self._add_subscription_sync(kind, target_uid, "user", str(row["user_id"]), commit=False)
                    if row["group_id"] is not None:
                        self._add_subscription_sync(kind, target_uid, "group", str(row["group_id"]), commit=False)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('legacy_migrated', '1') ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
            conn.commit()
            legacy.close()
            logger.info("[bilibilibot] migrated legacy sqlite data to data/bilibilibot/bilibili.db")
        except Exception as exc:
            conn.rollback()
            logger.exception(f"[bilibilibot] legacy migration failed: {exc}")

    # --- targets ----------------------------------------------------------

    async def upsert_target(self, target: TargetInfo, commit: bool = True) -> None:
        await self._run(self._upsert_target_sync, target, commit)

    async def get_target(self, kind: str, uid: str) -> TargetInfo | None:
        return await self._run(self._get_target_sync, kind, uid)

    async def get_live_target_by_room(self, room_id: str) -> TargetInfo | None:
        return await self._run(self._get_live_target_by_room_sync, room_id)

    async def list_targets(self, kind: str | None = None) -> list[TargetInfo]:
        return await self._run(self._list_targets_sync, kind)

    async def list_active_targets(self, kind: str | None = None) -> list[TargetInfo]:
        return await self._run(self._list_active_targets_sync, kind)

    def _upsert_target_sync(self, target: TargetInfo, commit: bool = True) -> None:
        if target.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported target kind: {target.kind}")
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO targets(kind, uid, room_id, name, avatar_url, latest_id, latest_ts, is_live, last_title, last_cover, last_desc, updated_at, live_started_at, live_last_seen_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, uid) DO UPDATE SET
                room_id = excluded.room_id,
                name = excluded.name,
                avatar_url = excluded.avatar_url,
                latest_id = excluded.latest_id,
                latest_ts = excluded.latest_ts,
                is_live = excluded.is_live,
                last_title = excluded.last_title,
                last_cover = excluded.last_cover,
                last_desc = excluded.last_desc,
                updated_at = excluded.updated_at,
                live_started_at = excluded.live_started_at,
                live_last_seen_at = excluded.live_last_seen_at
            """,
            (
                target.kind,
                target.uid,
                target.room_id,
                target.name,
                target.avatar_url,
                target.latest_id,
                int(target.latest_ts or 0),
                1 if target.is_live else 0,
                target.last_title,
                target.last_cover,
                target.last_desc,
                int(time.time()),
                int(target.live_started_at or 0),
                int(target.live_last_seen_at or 0),
            ),
        )
        if commit:
            conn.commit()

    def _get_target_sync(self, kind: str, uid: str) -> TargetInfo | None:
        row = self._connection().execute(
            "SELECT * FROM targets WHERE kind = ? AND uid = ?", (kind, uid)
        ).fetchone()
        return self._row_to_target(row) if row else None

    def _get_live_target_by_room_sync(self, room_id: str) -> TargetInfo | None:
        """Find the live target owning a long room number (short ids must be expanded first)."""
        row = self._connection().execute(
            "SELECT * FROM targets WHERE kind = ? AND room_id = ?",
            (KIND_LIVE, str(room_id)),
        ).fetchone()
        return self._row_to_target(row) if row else None

    def _list_targets_sync(self, kind: str | None = None) -> list[TargetInfo]:
        conn = self._connection()
        if kind:
            rows = conn.execute("SELECT * FROM targets WHERE kind = ? ORDER BY name, uid", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM targets ORDER BY kind, name, uid").fetchall()
        return [self._row_to_target(row) for row in rows]

    def _list_active_targets_sync(self, kind: str | None = None) -> list[TargetInfo]:
        sql = """
            SELECT DISTINCT t.* FROM targets t
            JOIN subscriptions s ON s.target_kind = t.kind AND s.target_uid = t.uid
        """
        params: tuple[str, ...] = ()
        if kind:
            sql += " WHERE t.kind = ?"
            params = (kind,)
        sql += " ORDER BY t.kind, t.uid"
        return [self._row_to_target(row) for row in self._connection().execute(sql, params).fetchall()]

    # --- subscriptions ----------------------------------------------------

    async def add_subscription(
        self, kind: str, uid: str, subscriber_type: str, subscriber_id: str, commit: bool = True
    ) -> bool:
        return await self._run(self._add_subscription_sync, kind, uid, subscriber_type, subscriber_id, commit)

    async def remove_subscription(self, kind: str, uid: str, subscriber_type: str, subscriber_id: str) -> bool:
        return await self._run(self._remove_subscription_sync, kind, uid, subscriber_type, subscriber_id)

    async def subscriptions_for_target(self, kind: str, uid: str) -> list[Subscription]:
        return await self._run(self._subscriptions_for_target_sync, kind, uid)

    async def subscriptions_for_subscriber(
        self, subscriber_type: str, subscriber_id: str, kind: str | None = None
    ) -> list[tuple[Subscription, TargetInfo | None]]:
        return await self._run(self._subscriptions_for_subscriber_sync, subscriber_type, subscriber_id, kind)

    def _add_subscription_sync(
        self, kind: str, uid: str, subscriber_type: str, subscriber_id: str, commit: bool = True
    ) -> bool:
        conn = self._connection()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions(target_kind, target_uid, subscriber_type, subscriber_id, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (kind, uid, subscriber_type, subscriber_id, int(time.time())),
        )
        if commit:
            conn.commit()
        return cur.rowcount > 0

    def _remove_subscription_sync(self, kind: str, uid: str, subscriber_type: str, subscriber_id: str) -> bool:
        conn = self._connection()
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE target_kind = ? AND target_uid = ? AND subscriber_type = ? AND subscriber_id = ?",
            (kind, uid, subscriber_type, subscriber_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def _subscriptions_for_target_sync(self, kind: str, uid: str) -> list[Subscription]:
        rows = self._connection().execute(
            "SELECT target_kind, target_uid, subscriber_type, subscriber_id FROM subscriptions WHERE target_kind = ? AND target_uid = ?",
            (kind, uid),
        ).fetchall()
        return [Subscription(**dict(row)) for row in rows]

    def _subscriptions_for_subscriber_sync(
        self, subscriber_type: str, subscriber_id: str, kind: str | None = None
    ) -> list[tuple[Subscription, TargetInfo | None]]:
        # One LEFT JOIN instead of a get_target() call per row.
        sql = """
            SELECT s.target_kind, s.target_uid, s.subscriber_type, s.subscriber_id, t.*
            FROM subscriptions s
            LEFT JOIN targets t ON t.kind = s.target_kind AND t.uid = s.target_uid
            WHERE s.subscriber_type = ? AND s.subscriber_id = ?
        """
        params: tuple[str, ...] = (subscriber_type, subscriber_id)
        if kind:
            sql += " AND s.target_kind = ?"
            params = (subscriber_type, subscriber_id, kind)
        sql += " ORDER BY s.target_kind, s.target_uid"
        result: list[tuple[Subscription, TargetInfo | None]] = []
        for row in self._connection().execute(sql, params).fetchall():
            sub = Subscription(row["target_kind"], row["target_uid"], row["subscriber_type"], row["subscriber_id"])
            # The joined target's "id" is NULL when the subscription outlived it.
            target = self._row_to_target(row) if row["id"] is not None else None
            result.append((sub, target))
        return result

    # --- seen items -------------------------------------------------------

    async def mark_seen(self, kind: str, uid: str, item_id: str, published_at: int) -> None:
        await self._run(self._mark_seen_sync, kind, uid, item_id, published_at)

    async def has_seen(self, kind: str, uid: str, item_id: str) -> bool:
        return await self._run(self._has_seen_sync, kind, uid, item_id)

    async def seen_ids(self, kind: str, uid: str, item_ids: Iterable[str]) -> set[str]:
        return await self._run(self._seen_ids_sync, kind, uid, list(item_ids))

    def _mark_seen_sync(self, kind: str, uid: str, item_id: str, published_at: int) -> None:
        conn = self._connection()
        conn.execute(
            "INSERT OR IGNORE INTO seen_items(kind, uid, item_id, published_at) VALUES(?, ?, ?, ?)",
            (kind, uid, item_id, int(published_at or 0)),
        )
        conn.commit()

    def _has_seen_sync(self, kind: str, uid: str, item_id: str) -> bool:
        row = self._connection().execute(
            "SELECT 1 FROM seen_items WHERE kind = ? AND uid = ? AND item_id = ?",
            (kind, uid, item_id),
        ).fetchone()
        return row is not None

    def _seen_ids_sync(self, kind: str, uid: str, item_ids: Iterable[str]) -> set[str]:
        wanted = [str(item_id) for item_id in item_ids]
        if not wanted:
            return set()
        marks = ",".join("?" * len(wanted))
        rows = self._connection().execute(
            f"SELECT item_id FROM seen_items WHERE kind = ? AND uid = ? AND item_id IN ({marks})",
            (kind, uid, *wanted),
        ).fetchall()
        return {str(row["item_id"]) for row in rows}

    # --- outbox -----------------------------------------------------------
    # Delivery is "at least once": state changes and their pending notifications
    # commit together, so a crash either loses both or replays the notification.

    async def apply_poll_result(
        self,
        targets: Iterable[TargetInfo],
        seen: Iterable[SeenItem],
        events: Iterable[BiliEvent],
        now: int,
        *,
        expand: Callable[[BiliEvent], Iterable[tuple[str, str]]],
        event_key: Callable[[BiliEvent], str],
        commit: bool = True,
    ) -> int:
        """Write new state, seen items and outbox rows in one transaction.

        `expand` returns the (subscriber_type, subscriber_id) pairs an event goes
        to; `event_key` names the event for dedup and render caching. The poller
        resolves both before calling in, so the write stays one transaction.
        """
        return await self._run(
            self._apply_poll_result_sync,
            list(targets),
            list(seen),
            list(events),
            now,
            expand,
            event_key,
            commit,
        )

    async def due_outbox(self, now: int, limit: int = 50) -> list[OutboxRow]:
        return await self._run(self._due_outbox_sync, now, limit)

    async def outbox_done(self, row_id: int, commit: bool = True) -> None:
        await self._run(self._outbox_done_sync, row_id, commit)

    async def outbox_retry(
        self, row_id: int, *, attempts: int, next_attempt_at: int, error: str, commit: bool = True
    ) -> None:
        await self._run(self._outbox_retry_sync, row_id, attempts, next_attempt_at, error, commit)

    async def outbox_drop(self, row_id: int, commit: bool = True) -> None:
        await self._run(self._outbox_done_sync, row_id, commit)

    async def outbox_expire(self, now: int, max_age: dict[str, int], commit: bool = True) -> int:
        return await self._run(self._outbox_expire_sync, now, dict(max_age), commit)

    async def outbox_count(self) -> int:
        return await self._run(self._outbox_count_sync)

    async def outbox_rows(
        self, *, subscriber_type: str | None = None, subscriber_id: str | None = None
    ) -> list[OutboxRow]:
        return await self._run(self._outbox_rows_sync, subscriber_type, subscriber_id)

    def _apply_poll_result_sync(
        self,
        targets: list[TargetInfo],
        seen: list[SeenItem],
        events: list[BiliEvent],
        now: int,
        expand: Callable[[BiliEvent], Iterable[tuple[str, str]]],
        event_key: Callable[[BiliEvent], str],
        commit: bool = True,
    ) -> int:
        conn = self._connection()
        try:
            for target in targets:
                self._upsert_target_sync(target, commit=False)
            for item in seen:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_items(kind, uid, item_id, published_at) VALUES(?, ?, ?, ?)",
                    (item.kind, item.uid, item.item_id, int(item.published_at or 0)),
                )
            rows = 0
            for event in events:
                key = event_key(event)
                payload = json.dumps(asdict(event.card), ensure_ascii=False)
                for subscriber_type, subscriber_id in expand(event):
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO outbox(
                            event_key, kind, uid, card_type, subscriber_type, subscriber_id,
                            card_json, created_at, attempts, next_attempt_at, last_error
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '')
                        """,
                        (
                            key,
                            event.kind,
                            event.uid,
                            event.card.card_type,
                            subscriber_type,
                            subscriber_id,
                            payload,
                            int(now),
                            int(now),
                        ),
                    )
                    rows += cur.rowcount
            if commit:
                conn.commit()
            return rows
        except Exception:
            # Nothing partial may survive: the next round re-detects the change.
            conn.rollback()
            raise

    def _due_outbox_sync(self, now: int, limit: int = 50) -> list[OutboxRow]:
        """Due rows, oldest per recipient only, so one recipient stays ordered."""
        rows = self._connection().execute(
            """
            SELECT o.* FROM outbox o
            WHERE o.next_attempt_at <= ?
              AND o.id = (
                  SELECT MIN(i.id) FROM outbox i
                  WHERE i.subscriber_type = o.subscriber_type
                    AND i.subscriber_id = o.subscriber_id
              )
            ORDER BY o.id
            LIMIT ?
            """,
            (int(now), int(limit)),
        ).fetchall()
        return [OutboxRow(**dict(row)) for row in rows]

    def _outbox_done_sync(self, row_id: int, commit: bool = True) -> None:
        conn = self._connection()
        conn.execute("DELETE FROM outbox WHERE id = ?", (int(row_id),))
        if commit:
            conn.commit()

    def _outbox_retry_sync(
        self, row_id: int, attempts: int, next_attempt_at: int, error: str, commit: bool = True
    ) -> None:
        conn = self._connection()
        conn.execute(
            "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (int(attempts), int(next_attempt_at), str(error), int(row_id)),
        )
        if commit:
            conn.commit()

    def _outbox_expire_sync(self, now: int, max_age: dict[str, int], commit: bool = True) -> int:
        """Drop notifications that would mislead by arriving too late."""
        conn = self._connection()
        removed = 0
        for card_type, age in max_age.items():
            cur = conn.execute(
                "DELETE FROM outbox WHERE card_type = ? AND created_at <= ?",
                (card_type, int(now) - int(age)),
            )
            removed += cur.rowcount
        if commit:
            conn.commit()
        return removed

    def _outbox_count_sync(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) AS total FROM outbox").fetchone()
        return int(row["total"] or 0)

    def _outbox_rows_sync(
        self, subscriber_type: str | None = None, subscriber_id: str | None = None
    ) -> list[OutboxRow]:
        sql = "SELECT * FROM outbox"
        params: tuple[str, ...] = ()
        if subscriber_type is not None:
            sql += " WHERE subscriber_type = ?"
            params = (subscriber_type,)
            if subscriber_id is not None:
                sql += " AND subscriber_id = ?"
                params = (subscriber_type, subscriber_id)
        sql += " ORDER BY id"
        return [OutboxRow(**dict(row)) for row in self._connection().execute(sql, params).fetchall()]

    def _row_to_target(self, row: sqlite3.Row) -> TargetInfo:
        return TargetInfo(
            kind=str(row["kind"]),
            uid=str(row["uid"]),
            room_id=str(row["room_id"] or ""),
            name=str(row["name"] or ""),
            avatar_url=str(row["avatar_url"] or ""),
            latest_id=str(row["latest_id"] or ""),
            latest_ts=int(row["latest_ts"] or 0),
            is_live=bool(row["is_live"]),
            last_title=str(row["last_title"] or ""),
            last_cover=str(row["last_cover"] or ""),
            last_desc=str(row["last_desc"] or ""),
            live_started_at=int(row["live_started_at"] or 0),
            live_last_seen_at=int(row["live_last_seen_at"] or 0),
        )
