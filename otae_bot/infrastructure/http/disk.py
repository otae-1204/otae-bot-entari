"""Bounded, disposable public caches. Never accept account API requests.

Only hashed request keys and whitelisted public bytes/validators are stored.
Images and version-addressed AKE tables have separate SQLite files and budgets.
The image-named storage types remain shared for backwards compatibility. SQLite
transactions provide atomic replacement; content hashes detect damaged bodies.
All callers run I/O in a worker thread. No stale-on-error fallback is provided.

A ``size`` column with an ``(accessed_at, size)`` index turns the quota check
into an index-only aggregate and eviction into an index walk, so neither reads
stored bodies.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, Thread, current_thread
from urllib.parse import urlsplit

from loguru import logger


# PRAGMA user_version：0 = 最初的 10 列；2 = 增加 size 列与 (accessed_at, size) 索引。
SCHEMA_VERSION = 2
# 旧库回填 size 时每个事务的行数；批与批之间释放写连接的锁，读用单独的连接，不受影响。
BACKFILL_ROWS = 64
EVICT_PAGE_ROWS = 64


def public_image_request(url, namespace, headers, params, kind):
    if kind != "bytes" or not namespace.startswith("endfield-") or params:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.username or parts.password:
        return False
    prefixes = {
        "data.akedata.wiki": "/public/images/",
        "static.warfarin.wiki": "/v4/",
        "assets.fz.wiki": "/",
        "bbs.hycdn.cn": "/image/",
    }
    prefix = prefixes.get(parts.hostname)
    if prefix is None or not parts.path.startswith(prefix):
        return False
    allowed = {"accept", "user-agent", "referer", "origin", "cache-control"}
    return all(key.lower() in allowed for key in (headers or {}))


def public_table_request(url, namespace, headers, params, kind):
    """Only version-addressed public AKE tables, never manifests or account APIs."""
    parts = urlsplit(url)
    if (
        kind != "json"
        or namespace != "akedata"
        or params
        or parts.query
        or parts.scheme != "https"
        or parts.hostname != "data.akedata.wiki"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        return False
    if not re.fullmatch(
        r"/public/[0-9.]+/[0-9-]+/TableCfg/[A-Za-z0-9_]+\.json", parts.path
    ):
        return False
    allowed = {
        "accept",
        "accept-language",
        "user-agent",
        "referer",
        "origin",
        "cache-control",
        "pragma",
    }
    return all(key.lower() in allowed for key in (headers or {}))


@dataclass(frozen=True)
class DiskImage:
    content: bytes
    content_type: str
    etag: str
    modified: str
    validated_at: float
    max_age: float


@dataclass(frozen=True)
class DiskImageMeta:
    """Cache-row metadata. Never includes the stored body."""

    validated_at: float
    max_age: float
    etag: str
    modified: str


_COLUMNS = (
    "key, namespace, content, digest, content_type, etag, modified, "
    "validated_at, max_age, accessed_at, size"
)


class PublicImageDiskCache:
    def __init__(self, path: Path, budget: int, max_entries: int = 4096):
        self.path = path
        self.budget = budget
        self.max_entries = max(1, max_entries)
        # Reads use their own WAL connection, so writes and the schema backfill
        # never block them. Lock order: _read_lock, then _lock (write connection,
        # schema, epochs).
        self._read_lock = RLock()
        self._lock = RLock()
        self._epochs: dict[str, int] = {}
        self._global_epoch = 0
        self._connection = None
        self._reader = None
        self._schema_ready = False
        self._backfill_from: int | None = None
        self._backfiller: Thread | None = None
        self._stopping = False

    def generation(self, namespace):
        with self._lock:
            return self._global_epoch, self._epochs.get(namespace, 0)

    def _connect(self):
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.execute("PRAGMA auto_vacuum=FULL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=128")
        connection.execute("PRAGMA journal_size_limit=4194304")
        connection.execute("""CREATE TABLE IF NOT EXISTS public_images_v1 (
            key TEXT PRIMARY KEY, namespace TEXT NOT NULL, content BLOB NOT NULL,
            digest TEXT NOT NULL, content_type TEXT NOT NULL, etag TEXT NOT NULL,
            modified TEXT NOT NULL, validated_at REAL NOT NULL,
            max_age REAL NOT NULL, accessed_at REAL NOT NULL, size INTEGER)""")
        self._connection = connection
        self._prepare_schema(connection)
        return connection

    def _read_connection(self):
        """Read-only use; the caller holds ``_read_lock``."""
        if self._reader is None:
            with self._lock:
                self._connect()  # creates or upgrades the schema first
            self._reader = sqlite3.connect(self.path, check_same_thread=False)
        return self._reader

    def _prepare_schema(self, connection):
        """Cheap part of the schema upgrade; the size backfill runs in a thread."""
        if connection.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            self._schema_ready = True
            return
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(public_images_v1)")
        }
        if "size" not in columns:
            # Only rewrites the schema text; existing rows read NULL until backfilled.
            connection.execute("ALTER TABLE public_images_v1 ADD COLUMN size INTEGER")
        if connection.execute("SELECT 1 FROM public_images_v1 LIMIT 1").fetchone() is None:
            self._finish_schema(connection)
            return
        self._schema_ready = False
        self._backfill_from = 0
        self._backfiller = Thread(
            target=self._migrate, name=f"disk-cache-backfill:{self.path.name}", daemon=True
        )
        self._backfiller.start()

    def _finish_schema(self, connection):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS public_images_v1_lru "
            "ON public_images_v1(accessed_at, size)"
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._schema_ready = True
        self._backfill_from = None

    def _migrate(self):
        """Backfill ``size`` for rows written before schema 2, one batch per transaction."""
        started = time.perf_counter()
        rows = 0
        while True:
            with self._lock:
                if self._backfill_from is None or self._stopping:
                    return  # close() stops it; the next connection resumes
                try:
                    batch = self._backfill_batch(self._connect())
                except (sqlite3.Error, OSError) as exc:
                    # Quota checks fall back to length(content): slow, but correct.
                    self._backfill_from = None
                    logger.warning(
                        f"[http-disk] {self.path.name} size backfill failed; "
                        f"keeping the slow quota check: {type(exc).__name__}: {exc}"
                    )
                    return
            if not batch:
                logger.info(
                    f"[http-disk] {self.path.name} upgraded to schema {SCHEMA_VERSION}: "
                    f"size backfilled for {rows} rows in {time.perf_counter() - started:.2f}s"
                )
                return
            rows += batch
            time.sleep(0.001)  # RLock is not fair: let a waiting put()/clear() in

    def _backfill_batch(self, connection) -> int:
        """Fill ``size`` for the next rowid range; 0 means done and the schema is final."""
        try:
            with connection:
                # Rowids come from the b-tree keys; only the UPDATE reads the rows.
                ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT rowid FROM public_images_v1 WHERE rowid>? "
                        "ORDER BY rowid LIMIT ?",
                        (self._backfill_from, BACKFILL_ROWS),
                    )
                ]
                if ids:
                    connection.execute(
                        "UPDATE public_images_v1 SET size=length(content) "
                        "WHERE rowid BETWEEN ? AND ? AND size IS NULL",
                        (ids[0], ids[-1]),
                    )
        finally:
            if connection.in_transaction:
                connection.rollback()
        if not ids:
            self._finish_schema(connection)
            return 0
        self._backfill_from = ids[-1]
        return len(ids)

    def close(self):
        """Disconnect. An unfinished size backfill resumes on the next connection."""
        self._stopping = True
        try:
            backfiller = self._backfiller
            if backfiller is not None and backfiller is not current_thread():
                backfiller.join()
        finally:
            self._stopping = False
            self._backfiller = None
        with self._read_lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._schema_ready = False
            self._backfill_from = None

    def metadata(self, key) -> DiskImageMeta | None:
        """Return freshness metadata for one key without reading the body."""
        if self.budget <= 0 or not self.path.exists():
            return None
        with self._read_lock:
            row = (
                self._read_connection()
                .execute(
                    "SELECT validated_at, max_age, etag, modified "
                    "FROM public_images_v1 WHERE key=?",
                    (key,),
                )
                .fetchone()
            )
        return None if row is None else DiskImageMeta(*row)

    def get(self, key, max_bytes):
        if self.budget <= 0 or not self.path.exists():
            return None
        with self._read_lock:
            row = (
                self._read_connection()
                .execute(
                    "SELECT content, digest, content_type, etag, modified, validated_at, max_age "
                    "FROM public_images_v1 WHERE key=? AND length(content)<=?",
                    (key, max_bytes),
                )
                .fetchone()
            )
        if row is None:
            return None
        corrupt = hashlib.sha256(row[0]).hexdigest() != row[1]
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    if corrupt:
                        connection.execute(
                            "DELETE FROM public_images_v1 WHERE key=? AND digest=?",
                            (key, row[1]),
                        )
                    else:
                        connection.execute(
                            "UPDATE public_images_v1 SET accessed_at=? WHERE key=?",
                            (time.time(), key),
                        )
            finally:
                if connection.in_transaction:
                    connection.rollback()
        return None if corrupt else DiskImage(row[0], *row[2:])

    def put(self, key, namespace, value, generation):
        if self.budget <= 0 or len(value.content) > self.budget:
            return
        with self._lock:
            if generation != self.generation(namespace):
                return
            connection = self._connect()
            try:
                with connection:
                    self._store(connection, [(key, namespace, value)])
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def _store(self, connection, rows):
        """Insert rows, evicting least recently used ones only when over budget."""
        size_sql = "size" if self._schema_ready else "coalesce(size, length(content))"
        count, total = connection.execute(
            f"SELECT count(*), total({size_sql}) FROM public_images_v1"
        ).fetchone()
        total = int(total)
        for key, namespace, value in rows:
            size = len(value.content)
            if size > self.budget:
                continue
            old = connection.execute(
                f"SELECT {size_sql} FROM public_images_v1 WHERE key=?", (key,)
            ).fetchone()
            if old is not None:
                connection.execute("DELETE FROM public_images_v1 WHERE key=?", (key,))
                count -= 1
                total -= old[0] or 0
            if total + size > self.budget or count >= self.max_entries:
                count, total = self._evict(connection, count, total, size, size_sql)
            connection.execute(
                f"INSERT INTO public_images_v1 ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    namespace,
                    value.content,
                    hashlib.sha256(value.content).hexdigest(),
                    value.content_type,
                    value.etag[:4096],
                    value.modified[:128],
                    value.validated_at,
                    value.max_age,
                    time.time(),
                    size,
                ),
            )
            count += 1
            total += size

    def _evict(self, connection, count, total, incoming, size_sql):
        def over():
            return total + incoming > self.budget or count >= self.max_entries

        while count > 0 and over():
            # Covered by the (accessed_at, size) index once the schema is ready.
            victims = connection.execute(
                f"SELECT rowid, {size_sql} FROM public_images_v1 "
                "ORDER BY accessed_at LIMIT ?",
                (EVICT_PAGE_ROWS,),
            ).fetchall()
            if not victims:
                break
            for rowid, size in victims:
                if not over():
                    break
                connection.execute(
                    "DELETE FROM public_images_v1 WHERE rowid=?", (rowid,)
                )
                count -= 1
                total -= size or 0
        return count, total

    def clear(self, prefix=None):
        with self._lock:
            if prefix is None:
                self._global_epoch += 1
            else:
                for namespace in list(self._epochs):
                    if namespace.startswith(prefix):
                        self._epochs[namespace] += 1
            if not self.path.exists():
                return 0
            connection = self._connect()
            try:
                with connection:
                    if prefix is None:
                        return connection.execute(
                            "DELETE FROM public_images_v1"
                        ).rowcount
                    return connection.execute(
                        "DELETE FROM public_images_v1 WHERE substr(namespace, 1, ?)=?",
                        (len(prefix), prefix),
                    ).rowcount
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def register(self, namespace):
        with self._lock:
            self._epochs.setdefault(namespace, 0)
            return self.generation(namespace)


public_images = PublicImageDiskCache(
    Path(
        os.environ.get(
            "OTAE_PUBLIC_IMAGE_CACHE_PATH", "data/cache/public-images-v1.sqlite3"
        )
    ),
    max(0, int(os.environ.get("OTAE_PUBLIC_IMAGE_CACHE_MIB", "256"))) * 1024 * 1024,
)

# Separate disposable database and budget; the on-disk record format is shared.
public_tables = PublicImageDiskCache(
    Path(
        os.environ.get(
            "OTAE_PUBLIC_TABLE_CACHE_PATH", "data/cache/akedata-tables-v1.sqlite3"
        )
    ),
    max(0, int(os.environ.get("OTAE_PUBLIC_TABLE_CACHE_MIB", "256"))) * 1024 * 1024,
    max_entries=512,
)
