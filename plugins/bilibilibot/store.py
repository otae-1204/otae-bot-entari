from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from loguru import logger

from .models import KIND_DYNAMIC, KIND_LIVE, KIND_VIDEO, SUPPORTED_KINDS, Subscription, TargetInfo


DATA_DIR = Path("data") / "bilibilibot"
DB_PATH = DATA_DIR / "bilibili.db"
LEGACY_DB_PATH = Path(__file__).resolve().parent / "bilibili_2.db"


class BiliStore:
    def __init__(self, db_path: str | Path = DB_PATH, legacy_db_path: str | Path = LEGACY_DB_PATH):
        self.db_path = Path(db_path)
        self.legacy_db_path = Path(legacy_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()
        self.migrate_legacy_if_needed()

    def close(self) -> None:
        connection = self.conn
        self.conn = None
        if connection is not None:
            connection.close()

    def init_schema(self) -> None:
        cur = self.conn.cursor()
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
                UNIQUE(kind, uid)
            )
            """
        )
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_target ON subscriptions(target_kind, target_uid)")
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def migrate_legacy_if_needed(self) -> None:
        if self.get_meta("legacy_migrated") == "1" or not self.legacy_db_path.exists():
            return
        try:
            legacy = sqlite3.connect(self.legacy_db_path)
            legacy.row_factory = sqlite3.Row
            now = int(time.time())
            for row in legacy.execute("SELECT up_uid, up_name, latest_update FROM up"):
                self.upsert_target(TargetInfo(KIND_VIDEO, str(row["up_uid"]), str(row["up_name"]), latest_ts=int(row["latest_update"] or 0)), commit=False)
            for row in legacy.execute("SELECT liver_uid, liver_name, is_live, live_room FROM liver"):
                self.upsert_target(
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
                self.upsert_target(TargetInfo(KIND_DYNAMIC, str(row["uid"]), str(row["u_name"]), latest_ts=int(row["latest_timestamp"] or 0)), commit=False)

            relation_map = [
                (KIND_VIDEO, "up_follower", "up_uid"),
                (KIND_LIVE, "liver_follower", "liver_uid"),
                (KIND_DYNAMIC, "dynamic_follower", "uid"),
            ]
            for kind, table, id_col in relation_map:
                for row in legacy.execute(f"SELECT {id_col}, user_id, group_id FROM {table}"):
                    target_uid = str(row[id_col])
                    if row["user_id"] is not None:
                        self.add_subscription(kind, target_uid, "user", str(row["user_id"]), commit=False)
                    if row["group_id"] is not None:
                        self.add_subscription(kind, target_uid, "group", str(row["group_id"]), commit=False)
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('legacy_migrated', '1') ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
            self.conn.commit()
            legacy.close()
            logger.info("[bilibilibot] migrated legacy sqlite data to data/bilibilibot/bilibili.db")
        except Exception as exc:
            self.conn.rollback()
            logger.exception(f"[bilibilibot] legacy migration failed: {exc}")

    def upsert_target(self, target: TargetInfo, commit: bool = True) -> None:
        if target.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported target kind: {target.kind}")
        self.conn.execute(
            """
            INSERT INTO targets(kind, uid, room_id, name, avatar_url, latest_id, latest_ts, is_live, last_title, last_cover, last_desc, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                updated_at = excluded.updated_at
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
            ),
        )
        if commit:
            self.conn.commit()

    def get_target(self, kind: str, uid: str) -> TargetInfo | None:
        row = self.conn.execute("SELECT * FROM targets WHERE kind = ? AND uid = ?", (kind, uid)).fetchone()
        return self._row_to_target(row) if row else None

    def list_targets(self, kind: str | None = None) -> list[TargetInfo]:
        if kind:
            rows = self.conn.execute("SELECT * FROM targets WHERE kind = ? ORDER BY name, uid", (kind,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM targets ORDER BY kind, name, uid").fetchall()
        return [self._row_to_target(row) for row in rows]

    def list_active_targets(self, kind: str | None = None) -> list[TargetInfo]:
        sql = """
            SELECT DISTINCT t.* FROM targets t
            JOIN subscriptions s ON s.target_kind = t.kind AND s.target_uid = t.uid
        """
        params: tuple[str, ...] = ()
        if kind:
            sql += " WHERE t.kind = ?"
            params = (kind,)
        sql += " ORDER BY t.kind, t.uid"
        return [self._row_to_target(row) for row in self.conn.execute(sql, params).fetchall()]

    def add_subscription(self, kind: str, uid: str, subscriber_type: str, subscriber_id: str, commit: bool = True) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions(target_kind, target_uid, subscriber_type, subscriber_id, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (kind, uid, subscriber_type, subscriber_id, int(time.time())),
        )
        if commit:
            self.conn.commit()
        return cur.rowcount > 0

    def remove_subscription(self, kind: str, uid: str, subscriber_type: str, subscriber_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM subscriptions WHERE target_kind = ? AND target_uid = ? AND subscriber_type = ? AND subscriber_id = ?",
            (kind, uid, subscriber_type, subscriber_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def subscriptions_for_target(self, kind: str, uid: str) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT target_kind, target_uid, subscriber_type, subscriber_id FROM subscriptions WHERE target_kind = ? AND target_uid = ?",
            (kind, uid),
        ).fetchall()
        return [Subscription(**dict(row)) for row in rows]

    def subscriptions_for_subscriber(self, subscriber_type: str, subscriber_id: str, kind: str | None = None) -> list[tuple[Subscription, TargetInfo | None]]:
        sql = "SELECT * FROM subscriptions WHERE subscriber_type = ? AND subscriber_id = ?"
        params: tuple[str, ...] = (subscriber_type, subscriber_id)
        if kind:
            sql += " AND target_kind = ?"
            params = (subscriber_type, subscriber_id, kind)
        sql += " ORDER BY target_kind, target_uid"
        result = []
        for row in self.conn.execute(sql, params).fetchall():
            sub = Subscription(row["target_kind"], row["target_uid"], row["subscriber_type"], row["subscriber_id"])
            result.append((sub, self.get_target(sub.target_kind, sub.target_uid)))
        return result

    def mark_seen(self, kind: str, uid: str, item_id: str, published_at: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items(kind, uid, item_id, published_at) VALUES(?, ?, ?, ?)",
            (kind, uid, item_id, int(published_at or 0)),
        )
        self.conn.commit()

    def has_seen(self, kind: str, uid: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE kind = ? AND uid = ? AND item_id = ?",
            (kind, uid, item_id),
        ).fetchone()
        return row is not None

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
        )
