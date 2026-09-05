"""Bounded, disposable public-image cache. Never accepts account API requests.

Only hashed request keys and public image bytes/validators are stored. SQLite
transactions provide atomic replacement; content hashes detect damaged bodies.
All callers run I/O in a worker thread. No stale-on-error fallback is provided.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit


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


@dataclass(frozen=True)
class DiskImage:
    content: bytes
    content_type: str
    etag: str
    modified: str
    validated_at: float
    max_age: float


class PublicImageDiskCache:
    def __init__(self, path: Path, budget: int, max_entries: int = 4096):
        self.path = path
        self.budget = budget
        self.max_entries = max(1, max_entries)
        self._lock = RLock()
        self._epochs: dict[str, int] = {}
        self._global_epoch = 0
        self._connection = None

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
            max_age REAL NOT NULL, accessed_at REAL NOT NULL)""")
        self._connection = connection
        return connection

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def get(self, key, max_bytes):
        if self.budget <= 0 or not self.path.exists():
            return None
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT content, digest, content_type, etag, modified, validated_at, max_age "
                    "FROM public_images_v1 WHERE key=? AND length(content)<=?",
                    (key, max_bytes),
                ).fetchone()
                if row is None:
                    return None
                if hashlib.sha256(row[0]).hexdigest() != row[1]:
                    connection.execute(
                        "DELETE FROM public_images_v1 WHERE key=?", (key,)
                    )
                    connection.commit()
                    return None
                connection.execute(
                    "UPDATE public_images_v1 SET accessed_at=? WHERE key=?",
                    (time.time(), key),
                )
                connection.commit()
                return DiskImage(row[0], *row[2:])
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def put(self, key, namespace, value, generation):
        if self.budget <= 0 or len(value.content) > self.budget:
            return
        with self._lock:
            if generation != self.generation(namespace):
                return
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "DELETE FROM public_images_v1 WHERE key=?", (key,)
                    )
                    total = connection.execute(
                        "SELECT coalesce(sum(length(content)), 0) FROM public_images_v1"
                    ).fetchone()[0]
                    old_rows = connection.execute(
                        "SELECT key, length(content) FROM public_images_v1 ORDER BY accessed_at"
                    ).fetchall()
                    count = len(old_rows)
                    for old_key, size in old_rows:
                        if (
                            total + len(value.content) <= self.budget
                            and count < self.max_entries
                        ):
                            break
                        connection.execute(
                            "DELETE FROM public_images_v1 WHERE key=?", (old_key,)
                        )
                        total -= size
                        count -= 1
                    connection.execute(
                        "INSERT INTO public_images_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        ),
                    )
            finally:
                if connection.in_transaction:
                    connection.rollback()

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
