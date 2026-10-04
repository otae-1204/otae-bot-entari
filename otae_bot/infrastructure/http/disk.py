"""Bounded, disposable public caches. Never accept account API requests.

Only hashed request keys and whitelisted public bytes/validators are stored.
Images and version-addressed AKE tables have separate SQLite files and budgets.
The image-named storage types remain shared for backwards compatibility. SQLite
transactions provide atomic replacement; content hashes detect damaged bodies.

Fetches never wait for the disk: ``put_later``/``refresh_later`` queue rows for
one background writer thread per database, which applies them in batched
transactions; queued rows are visible to ``get``/``metadata`` immediately.
``put`` stays synchronous for scripts and tests. A ``size`` column with an
``(accessed_at, size)`` index turns the quota check into an index-only
aggregate and eviction into an index walk, so neither reads stored bodies.
Expired rows are kept; callers may serve them when revalidation fails.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import sqlite3
import time
import weakref
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from threading import Condition, RLock, Thread, current_thread
from urllib.parse import urlsplit

from loguru import logger


# PRAGMA user_version：0 = 最初的 10 列；2 = 增加 size 列与 (accessed_at, size) 索引。
SCHEMA_VERSION = 2
# 旧库回填 size 时每个事务的行数；批与批之间释放写连接的锁，读用单独的连接，不受影响。
BACKFILL_ROWS = 64
# 后台写线程每个事务最多写入的新行数。
WRITE_BATCH_ROWS = 32
# 排队待写正文的总上限；超出时丢弃新写入（只是缓存，下次再取）。
QUEUE_LIMIT_BYTES = 64 * 1024 * 1024
# 只有读取时，访问时间先攒着，攒够这么多条再唤醒写线程。
TOUCH_FLUSH_ROWS = 256
# 写线程空闲多久后退出，有新写入时再启动。
WRITER_IDLE_SECONDS = 2.0
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


@dataclass(frozen=True, slots=True)
class _Queued:
    namespace: str
    value: DiskImage | DiskImageMeta
    generation: tuple[int, int]


def _refreshed(image: DiskImage, meta: DiskImageMeta) -> DiskImage:
    return replace(
        image,
        etag=meta.etag,
        modified=meta.modified,
        validated_at=meta.validated_at,
        max_age=meta.max_age,
    )


_COLUMNS = (
    "key, namespace, content, digest, content_type, etag, modified, "
    "validated_at, max_age, accessed_at, size"
)
_live_caches: weakref.WeakSet[PublicImageDiskCache] = weakref.WeakSet()


class PublicImageDiskCache:
    def __init__(self, path: Path, budget: int, max_entries: int = 4096):
        self.path = path
        self.budget = budget
        self.max_entries = max(1, max_entries)
        # Reads use their own WAL connection, so writes and the schema backfill
        # never block them. Lock order: _read_lock, then _lock (write connection,
        # schema), then _queue (pending rows, epochs, writer handle). _queue is
        # never held during SQLite I/O.
        self._read_lock = RLock()
        self._lock = RLock()
        self._queue = Condition(RLock())
        self._epochs: dict[str, int] = {}
        self._global_epoch = 0
        self._connection = None
        self._reader = None
        self._schema_ready = False
        self._backfill_from: int | None = None
        self._writes: dict[str, _Queued] = {}
        self._refreshes: dict[str, _Queued] = {}
        self._touches: dict[str, float] = {}
        self._queued_bytes = 0
        self._queue_full_logged = False
        self._writer: Thread | None = None
        self._stopping = False

    def generation(self, namespace):
        with self._queue:
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
        """Cheap part of the schema upgrade; the size backfill runs in the writer."""
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
        with self._queue:
            self._wake()

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

    def close(self, timeout: float | None = 10.0):
        """Flush queued rows (bounded by ``timeout``), stop the writer and disconnect.

        An unfinished size backfill is abandoned and resumes on the next connection.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._queue:
            self._stopping = True
            self._queue.notify_all()
        try:
            self.flush(timeout)
            with self._queue:
                writer = self._writer
            if writer is not None and writer is not current_thread():
                writer.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        finally:
            with self._queue:
                self._stopping = False
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

    def _current(self, queued: dict[str, _Queued], key) -> _Queued | None:
        """A queued row unless a clear made it stale. Caller holds ``_queue``."""
        item = queued.get(key)
        if item is None or item.generation != self.generation(item.namespace):
            return None
        return item

    def metadata(self, key) -> DiskImageMeta | None:
        """Return freshness metadata for one key without reading the body."""
        if self.budget <= 0:
            return None
        with self._queue:
            queued = self._current(self._writes, key)
            refresh = self._current(self._refreshes, key)
        if queued is not None:
            value = queued.value
            return DiskImageMeta(
                value.validated_at, value.max_age, value.etag, value.modified
            )
        if not self.path.exists():
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
        if row is None:
            return None
        return DiskImageMeta(*row) if refresh is None else refresh.value

    def get(self, key, max_bytes):
        """Return the row whatever its age; freshness is the caller's decision."""
        if self.budget <= 0:
            return None
        with self._queue:
            queued = self._current(self._writes, key)
            refresh = self._current(self._refreshes, key)
        if queued is not None:
            return queued.value if len(queued.value.content) <= max_bytes else None
        if not self.path.exists():
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
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            with self._lock:
                connection = self._connect()
                try:
                    with connection:
                        connection.execute(
                            "DELETE FROM public_images_v1 WHERE key=? AND digest=?",
                            (key, row[1]),
                        )
                finally:
                    if connection.in_transaction:
                        connection.rollback()
            return None
        with self._queue:
            # LRU order only; written with the next batch instead of on the read path.
            self._touches[key] = time.time()
            if len(self._touches) >= TOUCH_FLUSH_ROWS:
                self._wake()
        image = DiskImage(row[0], *row[2:])
        return image if refresh is None else _refreshed(image, refresh.value)

    def put(self, key, namespace, value, generation):
        """Synchronous write (scripts and tests); fetches use ``put_later``."""
        if self.budget <= 0 or len(value.content) > self.budget:
            return
        with self._lock:
            with self._queue:
                if generation != self.generation(namespace):
                    return
                self._forget(key)
            connection = self._connect()
            try:
                with connection:
                    self._store(connection, [(key, namespace, value)])
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def put_later(self, key, namespace, value: DiskImage, generation) -> bool:
        """Queue a row for the background writer. Never blocks on SQLite."""
        size = len(value.content)
        if self.budget <= 0 or size > self.budget:
            return False
        with self._queue:
            if generation != self.generation(namespace):
                return False
            self._forget(key)
            if self._queued_bytes + size > QUEUE_LIMIT_BYTES:
                if not self._queue_full_logged:
                    self._queue_full_logged = True
                    logger.warning(
                        f"[http-disk] {self.path.name} write queue full "
                        f"({self._queued_bytes} bytes); dropping new rows until it drains"
                    )
                return False
            self._writes[key] = _Queued(namespace, value, generation)
            self._queued_bytes += size
            self._wake()
            return True

    def refresh_later(self, key, namespace, meta: DiskImageMeta, generation) -> bool:
        """Queue a validator/freshness update (HTTP 304). The body is not rewritten."""
        if self.budget <= 0:
            return False
        with self._queue:
            if generation != self.generation(namespace):
                return False
            queued = self._current(self._writes, key)
            if queued is not None:
                self._writes[key] = _Queued(
                    namespace, _refreshed(queued.value, meta), generation
                )
            else:
                self._refreshes[key] = _Queued(namespace, meta, generation)
            self._wake()
            return True

    def _forget(self, key):
        """Drop queued rows for ``key``. Caller holds ``_queue``."""
        previous = self._writes.pop(key, None)
        if previous is not None:
            self._queued_bytes -= len(previous.value.content)
        self._refreshes.pop(key, None)

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until queued rows are written (or dropped after a logged error)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._queue:
                if not self._has_work():
                    return True
                self._wake()  # touches alone do not wake an idle writer
                inline = self._writer is None
                if not inline:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._queue.wait(remaining)
                    continue
            # No thread can start (interpreter shutdown): write from this thread.
            self._write_batch()

    def _has_work(self):
        if self._writes or self._refreshes or self._touches:
            return True
        return self._backfill_from is not None and not self._stopping

    def _wake(self):
        """Start the writer when needed. Caller holds ``_queue``."""
        _live_caches.add(self)
        if self._writer is None:
            writer = Thread(
                target=self._write_loop, name=f"disk-cache:{self.path.name}", daemon=True
            )
            try:
                writer.start()
            except RuntimeError:  # interpreter shutting down; flush() writes inline
                return
            self._writer = writer
        self._queue.notify_all()

    def _write_loop(self):
        me = current_thread()
        while True:
            with self._queue:
                idle_until = time.monotonic() + WRITER_IDLE_SECONDS
                while not self._has_work():
                    remaining = idle_until - time.monotonic()
                    if self._stopping or remaining <= 0:
                        if self._writer is me:
                            self._writer = None
                        self._queue.notify_all()
                        return
                    self._queue.wait(remaining)
            self._write_batch()

    def _write_batch(self):
        with self._queue:
            writes = list(islice(self._writes.items(), WRITE_BATCH_ROWS))
            refreshes = list(self._refreshes.items())
            touches = list(self._touches.items())
        try:
            self._migrate()
            self._apply(writes, refreshes, touches)
        except Exception as exc:  # noqa: BLE001 - a cache write must never break fetches
            logger.warning(
                f"[http-disk] {self.path.name} write failed, {len(writes)} rows dropped: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            with self._queue:
                for key, item in writes:
                    if self._writes.get(key) is item:
                        del self._writes[key]
                        self._queued_bytes -= len(item.value.content)
                for key, item in refreshes:
                    if self._refreshes.get(key) is item:
                        del self._refreshes[key]
                for key, stamp in touches:
                    if self._touches.get(key) == stamp:
                        del self._touches[key]
                if not self._writes:
                    self._queue_full_logged = False
                self._queue.notify_all()

    def _apply(self, writes, refreshes, touches):
        if not (writes or refreshes or touches):
            return
        with self._lock:
            connection = self._connect()
            with self._queue:
                # Checked under _lock: clear() bumps epochs and deletes atomically.
                writes = [
                    (key, item.namespace, item.value)
                    for key, item in writes
                    if item.generation == self.generation(item.namespace)
                ]
                refreshes = [
                    (key, item.value)
                    for key, item in refreshes
                    if item.generation == self.generation(item.namespace)
                ]
            try:
                with connection:
                    if writes:
                        self._store(connection, writes)
                    if refreshes:
                        now = time.time()
                        connection.executemany(
                            "UPDATE public_images_v1 SET validated_at=?, max_age=?, "
                            "etag=?, modified=?, accessed_at=? WHERE key=?",
                            [
                                (
                                    meta.validated_at,
                                    meta.max_age,
                                    meta.etag[:4096],
                                    meta.modified[:128],
                                    now,
                                    key,
                                )
                                for key, meta in refreshes
                            ],
                        )
                    if touches:
                        connection.executemany(
                            "UPDATE public_images_v1 SET accessed_at=? WHERE key=?",
                            [(stamp, key) for key, stamp in touches],
                        )
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
            with self._queue:
                if prefix is None:
                    self._global_epoch += 1
                else:
                    for namespace in list(self._epochs):
                        if namespace.startswith(prefix):
                            self._epochs[namespace] += 1
                dropped = 0
                for key, item in list(self._writes.items()):
                    if prefix is None or item.namespace.startswith(prefix):
                        self._forget(key)
                        dropped += 1
                for key, item in list(self._refreshes.items()):
                    if prefix is None or item.namespace.startswith(prefix):
                        del self._refreshes[key]
                self._queue.notify_all()
            if not self.path.exists():
                return dropped
            connection = self._connect()
            try:
                with connection:
                    if prefix is None:
                        return dropped + connection.execute(
                            "DELETE FROM public_images_v1"
                        ).rowcount
                    return dropped + connection.execute(
                        "DELETE FROM public_images_v1 WHERE substr(namespace, 1, ?)=?",
                        (len(prefix), prefix),
                    ).rowcount
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def register(self, namespace):
        with self._queue:
            self._epochs.setdefault(namespace, 0)
            return self.generation(namespace)


def _flush_at_exit():
    for cache in list(_live_caches):
        try:
            cache.flush(timeout=5.0)
        except Exception:  # noqa: BLE001 - best effort while the process exits
            pass


atexit.register(_flush_at_exit)


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
