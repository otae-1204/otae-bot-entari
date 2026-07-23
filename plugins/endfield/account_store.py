from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .account_crypto import CredentialCipher, EncryptedCredential


DATA_DIR = Path("data") / "endfield"
DB_PATH = DATA_DIR / "endfield.db"


@dataclass(frozen=True, slots=True)
class RoleCandidate:
    binding_uid: str
    role_id: str
    server_id: str
    nickname: str
    server_name: str = ""


@dataclass(frozen=True, slots=True)
class EndfieldRole:
    id: int
    credential_id: int
    qq_user_id: str
    binding_uid: str
    role_id: str
    server_id: str
    nickname: str
    server_name: str
    is_primary: bool

    @property
    def masked_uid(self) -> str:
        suffix = self.role_id[-4:] if self.role_id else "----"
        return f"****{suffix}"


@dataclass(frozen=True, slots=True)
class GachaRecord:
    role_id: str
    server_id: str
    pool_id: str
    pool_name: str
    pool_type: str
    seq_id: str
    gacha_ts: int
    item_id: str
    item_name: str
    rarity: int
    item_type: str
    weapon_type: str = ""
    is_new: bool = False
    is_free: bool = False


@dataclass(frozen=True, slots=True)
class SyncState:
    role_id: str
    server_id: str
    stream_key: str
    newest_seq_id: str = ""
    page_cursor: str = ""
    last_sync_at: int = 0
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class XhhGachaPool:
    pool_id: str
    pool_name: str
    pool_type: str
    item_type: str
    total_count: int
    current_count: int = 0
    free_count: int = 0
    latest_ts: int = 0
    is_current: bool = False
    sort_order: int = -1


@dataclass(frozen=True, slots=True)
class XhhSixStar:
    pool_id: str
    unique_key: str
    item_name: str
    item_type: str
    gacha_ts: int
    interval: int
    pool_position: int = 0
    item_id: str = ""
    miss_up: bool = False
    is_free: bool = False


@dataclass(frozen=True, slots=True)
class XhhGachaImport:
    source_uid: str
    nickname: str
    total_count: int
    imported_at: int
    pools: tuple[XhhGachaPool, ...]
    six_stars: tuple[XhhSixStar, ...]


class EndfieldStore:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_user_id TEXT NOT NULL,
                    token_fingerprint TEXT NOT NULL,
                    token_nonce BLOB NOT NULL,
                    token_ciphertext BLOB NOT NULL,
                    token_tag BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(qq_user_id, token_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    credential_id INTEGER NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
                    qq_user_id TEXT NOT NULL,
                    binding_uid TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    server_name TEXT NOT NULL DEFAULT '',
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(qq_user_id, role_id, server_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_endfield_primary_role
                    ON roles(qq_user_id) WHERE is_primary = 1;
                CREATE INDEX IF NOT EXISTS idx_endfield_roles_user ON roles(qq_user_id, id);
                CREATE TABLE IF NOT EXISTS gacha_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    pool_name TEXT NOT NULL DEFAULT '',
                    pool_type TEXT NOT NULL DEFAULT '',
                    seq_id TEXT NOT NULL,
                    gacha_ts INTEGER NOT NULL DEFAULT 0,
                    item_id TEXT NOT NULL DEFAULT '',
                    item_name TEXT NOT NULL DEFAULT '',
                    rarity INTEGER NOT NULL DEFAULT 0,
                    item_type TEXT NOT NULL,
                    weapon_type TEXT NOT NULL DEFAULT '',
                    is_new INTEGER NOT NULL DEFAULT 0,
                    is_free INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    UNIQUE(role_id, server_id, pool_id, seq_id)
                );
                CREATE INDEX IF NOT EXISTS idx_endfield_gacha_history
                    ON gacha_records(role_id, server_id, gacha_ts DESC, id DESC);
                CREATE TABLE IF NOT EXISTS gacha_pool_totals (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    total_count INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(role_id, server_id, pool_id)
                );
                CREATE TABLE IF NOT EXISTS xhh_gacha_imports (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    source_uid TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    imported_at INTEGER NOT NULL,
                    PRIMARY KEY(role_id, server_id)
                );
                CREATE TABLE IF NOT EXISTS xhh_gacha_pools (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    pool_name TEXT NOT NULL DEFAULT '',
                    pool_type TEXT NOT NULL DEFAULT '',
                    item_type TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    current_count INTEGER NOT NULL DEFAULT 0,
                    free_count INTEGER NOT NULL DEFAULT 0,
                    latest_ts INTEGER NOT NULL DEFAULT 0,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT -1,
                    PRIMARY KEY(role_id, server_id, pool_id)
                );
                CREATE TABLE IF NOT EXISTS xhh_gacha_six_stars (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    unique_key TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    gacha_ts INTEGER NOT NULL DEFAULT 0,
                    interval INTEGER NOT NULL DEFAULT 0,
                    pool_position INTEGER NOT NULL DEFAULT 0,
                    item_id TEXT NOT NULL DEFAULT '',
                    miss_up INTEGER NOT NULL DEFAULT 0,
                    is_free INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(role_id, server_id, pool_id, unique_key)
                );
                CREATE TABLE IF NOT EXISTS sync_states (
                    role_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    stream_key TEXT NOT NULL,
                    newest_seq_id TEXT NOT NULL DEFAULT '',
                    page_cursor TEXT NOT NULL DEFAULT '',
                    last_sync_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(role_id, server_id, stream_key)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(gacha_records)").fetchall()
            }
            if "is_free" not in columns:
                self.conn.execute(
                    "ALTER TABLE gacha_records ADD COLUMN is_free INTEGER NOT NULL DEFAULT 0"
                )
                self.conn.execute("DELETE FROM sync_states")
            xhh_pool_columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(xhh_gacha_pools)").fetchall()
            }
            if "sort_order" not in xhh_pool_columns:
                self.conn.execute(
                    "ALTER TABLE xhh_gacha_pools ADD COLUMN sort_order INTEGER NOT NULL DEFAULT -1"
                )
                rows = self.conn.execute(
                    """
                    SELECT rowid AS storage_rowid, role_id, server_id
                    FROM xhh_gacha_pools
                    ORDER BY role_id, server_id, rowid
                    """
                ).fetchall()
                counters: dict[tuple[str, str], int] = {}
                for row in rows:
                    identity = (str(row["role_id"]), str(row["server_id"]))
                    sort_order = counters.get(identity, 0)
                    self.conn.execute(
                        "UPDATE xhh_gacha_pools SET sort_order = ? WHERE rowid = ?",
                        (sort_order, int(row["storage_rowid"])),
                    )
                    counters[identity] = sort_order + 1
            xhh_six_columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(xhh_gacha_six_stars)").fetchall()
            }
            if "is_free" not in xhh_six_columns:
                self.conn.execute(
                    "ALTER TABLE xhh_gacha_six_stars ADD COLUMN is_free INTEGER NOT NULL DEFAULT 0"
                )
            self.conn.commit()

    def bind_roles(
        self,
        qq_user_id: str,
        account_token: str,
        roles: Sequence[RoleCandidate],
        cipher: CredentialCipher,
    ) -> list[EndfieldRole]:
        if not roles:
            return []
        encrypted = cipher.encrypt(account_token)
        fingerprint = hashlib.sha256(account_token.encode("utf-8")).hexdigest()[:24]
        now = int(time.time())
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    """
                    INSERT INTO credentials(
                        qq_user_id, token_fingerprint, token_nonce, token_ciphertext, token_tag, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qq_user_id, token_fingerprint) DO UPDATE SET
                        token_nonce = excluded.token_nonce,
                        token_ciphertext = excluded.token_ciphertext,
                        token_tag = excluded.token_tag,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(qq_user_id), fingerprint, encrypted.nonce, encrypted.ciphertext, encrypted.tag, now, now,
                    ),
                )
                credential_id = int(
                    self.conn.execute(
                        "SELECT id FROM credentials WHERE qq_user_id = ? AND token_fingerprint = ?",
                        (str(qq_user_id), fingerprint),
                    ).fetchone()["id"]
                )
                has_primary = bool(
                    self.conn.execute(
                        "SELECT 1 FROM roles WHERE qq_user_id = ? AND is_primary = 1", (str(qq_user_id),)
                    ).fetchone()
                )
                for candidate in roles:
                    make_primary = 0 if has_primary else 1
                    self.conn.execute(
                        """
                        INSERT INTO roles(
                            credential_id, qq_user_id, binding_uid, role_id, server_id,
                            nickname, server_name, is_primary, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(qq_user_id, role_id, server_id) DO UPDATE SET
                            credential_id = excluded.credential_id,
                            binding_uid = excluded.binding_uid,
                            nickname = excluded.nickname,
                            server_name = excluded.server_name,
                            is_primary = CASE WHEN roles.is_primary = 1 OR excluded.is_primary = 1 THEN 1 ELSE 0 END,
                            updated_at = excluded.updated_at
                        """,
                        (
                            credential_id, str(qq_user_id), candidate.binding_uid, candidate.role_id,
                            candidate.server_id, candidate.nickname, candidate.server_name,
                            make_primary, now, now,
                        ),
                    )
                    if make_primary:
                        has_primary = True
                self.conn.execute(
                    """
                    DELETE FROM credentials
                    WHERE qq_user_id = ?
                      AND NOT EXISTS (SELECT 1 FROM roles WHERE roles.credential_id = credentials.id)
                    """,
                    (str(qq_user_id),),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.list_roles(qq_user_id)

    def list_roles(self, qq_user_id: str) -> list[EndfieldRole]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM roles WHERE qq_user_id = ? ORDER BY id ASC",
                (str(qq_user_id),),
            ).fetchall()
        return [self._role(row) for row in rows]

    def get_role(self, role_id: int) -> EndfieldRole | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM roles WHERE id = ?", (int(role_id),)).fetchone()
        return self._role(row) if row else None

    def resolve_role(self, qq_user_id: str, selector: str = "") -> EndfieldRole | None:
        roles = self.list_roles(qq_user_id)
        if not roles:
            return None
        value = str(selector or "").strip()
        if not value or value.casefold() in {"主账号", "主账户", "primary", "main"}:
            return next((role for role in roles if role.is_primary), roles[0])
        if len(value) >= 4:
            suffix_matches = [role for role in roles if role.role_id.endswith(value[-4:])]
            if len(suffix_matches) == 1:
                return suffix_matches[0]
            if len(value) == 4:
                return None
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(roles):
                return roles[index]
        lowered = value.casefold()
        exact = [role for role in roles if role.nickname.casefold() == lowered]
        if len(exact) == 1:
            return exact[0]
        suffix = value[-4:]
        matches = [role for role in roles if role.role_id.endswith(suffix)]
        return matches[0] if len(matches) == 1 else None

    def resolve_roles(self, qq_user_id: str, selector: str = "全部") -> list[EndfieldRole]:
        if str(selector or "").strip().casefold() in {"", "全部", "all"}:
            return self.list_roles(qq_user_id)
        role = self.resolve_role(qq_user_id, selector)
        return [role] if role else []

    def set_primary(self, qq_user_id: str, selector: str) -> EndfieldRole | None:
        role = self.resolve_role(qq_user_id, selector)
        if role is None:
            return None
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("UPDATE roles SET is_primary = 0 WHERE qq_user_id = ?", (str(qq_user_id),))
                self.conn.execute("UPDATE roles SET is_primary = 1 WHERE id = ?", (role.id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_role(role.id)

    def unbind(self, qq_user_id: str, selector: str) -> EndfieldRole | None:
        role = self.resolve_role(qq_user_id, selector)
        if role is None:
            return None
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM roles WHERE id = ?", (role.id,))
                remaining = self.conn.execute(
                    "SELECT id FROM roles WHERE credential_id = ? LIMIT 1", (role.credential_id,)
                ).fetchone()
                if not remaining:
                    self.conn.execute("DELETE FROM credentials WHERE id = ?", (role.credential_id,))
                if role.is_primary:
                    replacement = self.conn.execute(
                        "SELECT id FROM roles WHERE qq_user_id = ? ORDER BY id LIMIT 1", (str(qq_user_id),)
                    ).fetchone()
                    if replacement:
                        self.conn.execute("UPDATE roles SET is_primary = 1 WHERE id = ?", (replacement["id"],))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return role

    def decrypt_token(self, role: EndfieldRole, cipher: CredentialCipher) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT token_nonce, token_ciphertext, token_tag FROM credentials WHERE id = ?",
                (role.credential_id,),
            ).fetchone()
        if row is None:
            raise LookupError("账号凭据不存在")
        return cipher.decrypt(EncryptedCredential(row["token_nonce"], row["token_ciphertext"], row["token_tag"]))

    def insert_gacha_records(self, records: Iterable[GachaRecord]) -> int:
        now = int(time.time())
        inserted = 0
        with self._lock:
            for record in records:
                existed = self.conn.execute(
                    """
                    SELECT 1 FROM gacha_records
                    WHERE role_id = ? AND server_id = ? AND pool_id = ? AND seq_id = ?
                    """,
                    (record.role_id, record.server_id, record.pool_id, record.seq_id),
                ).fetchone()
                self.conn.execute(
                    """
                    INSERT INTO gacha_records(
                        role_id, server_id, pool_id, pool_name, pool_type, seq_id, gacha_ts,
                        item_id, item_name, rarity, item_type, weapon_type, is_new, is_free, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(role_id, server_id, pool_id, seq_id) DO UPDATE SET
                        pool_name = excluded.pool_name,
                        pool_type = excluded.pool_type,
                        gacha_ts = excluded.gacha_ts,
                        item_id = excluded.item_id,
                        item_name = excluded.item_name,
                        rarity = excluded.rarity,
                        item_type = excluded.item_type,
                        weapon_type = excluded.weapon_type,
                        is_new = excluded.is_new,
                        is_free = excluded.is_free
                    """,
                    (
                        record.role_id, record.server_id, record.pool_id, record.pool_name,
                        record.pool_type, record.seq_id, int(record.gacha_ts), record.item_id,
                        record.item_name, int(record.rarity), record.item_type, record.weapon_type,
                        1 if record.is_new else 0, 1 if record.is_free else 0, now,
                    ),
                )
                inserted += 0 if existed else 1
            self.conn.commit()
        return inserted

    def get_sync_state(self, role: EndfieldRole, stream_key: str) -> SyncState:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sync_states WHERE role_id = ? AND server_id = ? AND stream_key = ?",
                (role.role_id, role.server_id, stream_key),
            ).fetchone()
        if row is None:
            return SyncState(role.role_id, role.server_id, stream_key)
        return SyncState(**dict(row))

    def set_gacha_pool_total(self, role: EndfieldRole, pool_id: str, total_count: int) -> None:
        total = max(0, int(total_count))
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO gacha_pool_totals(role_id, server_id, pool_id, total_count, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(role_id, server_id, pool_id) DO UPDATE SET
                    total_count = excluded.total_count,
                    updated_at = excluded.updated_at
                """,
                (role.role_id, role.server_id, str(pool_id), total, int(time.time())),
            )
            self.conn.commit()

    def list_gacha_pool_totals(self, role: EndfieldRole) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT pool_id, total_count FROM gacha_pool_totals WHERE role_id = ? AND server_id = ?",
                (role.role_id, role.server_id),
            ).fetchall()
        return {str(row["pool_id"]): max(0, int(row["total_count"])) for row in rows}

    def replace_xhh_gacha_import(self, role: EndfieldRole, imported: XhhGachaImport) -> None:
        if str(imported.source_uid).strip() != role.role_id:
            raise ValueError("小黑盒终末地 UID 与绑定角色不一致")
        imported_at = int(imported.imported_at or time.time())
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                identity = (role.role_id, role.server_id)
                self.conn.execute(
                    """
                    INSERT INTO xhh_gacha_imports(
                        role_id, server_id, source_uid, nickname, total_count, imported_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(role_id, server_id) DO UPDATE SET
                        source_uid = excluded.source_uid,
                        nickname = excluded.nickname,
                        total_count = excluded.total_count,
                        imported_at = excluded.imported_at
                    """,
                    (*identity, imported.source_uid, imported.nickname, max(0, int(imported.total_count)), imported_at),
                )
                self.conn.execute(
                    "DELETE FROM xhh_gacha_pools WHERE role_id = ? AND server_id = ?", identity
                )
                self.conn.execute(
                    "DELETE FROM xhh_gacha_six_stars WHERE role_id = ? AND server_id = ?", identity
                )
                self.conn.executemany(
                    """
                    INSERT INTO xhh_gacha_pools(
                        role_id, server_id, pool_id, pool_name, pool_type, item_type,
                        total_count, current_count, free_count, latest_ts, is_current, sort_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            *identity, item.pool_id, item.pool_name, item.pool_type, item.item_type,
                            max(0, int(item.total_count)), max(0, int(item.current_count)),
                            max(0, int(item.free_count)), max(0, int(item.latest_ts)),
                            1 if item.is_current else 0, int(item.sort_order),
                        )
                        for item in imported.pools
                    ],
                )
                self.conn.executemany(
                    """
                    INSERT INTO xhh_gacha_six_stars(
                        role_id, server_id, pool_id, unique_key, item_name, item_type,
                        gacha_ts, interval, pool_position, item_id, miss_up, is_free
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            *identity, item.pool_id, item.unique_key, item.item_name, item.item_type,
                            max(0, int(item.gacha_ts)), max(0, int(item.interval)),
                            max(0, int(item.pool_position)), item.item_id, 1 if item.miss_up else 0,
                            1 if item.is_free else 0,
                        )
                        for item in imported.six_stars
                    ],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_xhh_gacha_import(self, role: EndfieldRole) -> XhhGachaImport | None:
        identity = (role.role_id, role.server_id)
        with self._lock:
            header = self.conn.execute(
                "SELECT * FROM xhh_gacha_imports WHERE role_id = ? AND server_id = ?", identity
            ).fetchone()
            if header is None:
                return None
            pool_rows = self.conn.execute(
                """
                SELECT * FROM xhh_gacha_pools
                WHERE role_id = ? AND server_id = ?
                ORDER BY CASE WHEN sort_order >= 0 THEN 0 ELSE 1 END,
                         sort_order ASC, latest_ts DESC, pool_name
                """,
                identity,
            ).fetchall()
            six_rows = self.conn.execute(
                "SELECT * FROM xhh_gacha_six_stars WHERE role_id = ? AND server_id = ? ORDER BY gacha_ts DESC, rowid DESC",
                identity,
            ).fetchall()
        pools = tuple(
            XhhGachaPool(
                pool_id=str(row["pool_id"]), pool_name=str(row["pool_name"]),
                pool_type=str(row["pool_type"]), item_type=str(row["item_type"]),
                total_count=max(0, int(row["total_count"])),
                current_count=max(0, int(row["current_count"])),
                free_count=max(0, int(row["free_count"])), latest_ts=max(0, int(row["latest_ts"])),
                is_current=bool(row["is_current"]),
                sort_order=int(row["sort_order"]),
            )
            for row in pool_rows
        )
        six_stars = tuple(
            XhhSixStar(
                pool_id=str(row["pool_id"]), unique_key=str(row["unique_key"]),
                item_name=str(row["item_name"]), item_type=str(row["item_type"]),
                gacha_ts=max(0, int(row["gacha_ts"])), interval=max(0, int(row["interval"])),
                pool_position=max(0, int(row["pool_position"])), item_id=str(row["item_id"]),
                miss_up=bool(row["miss_up"]),
                is_free=bool(row["is_free"]),
            )
            for row in six_rows
        )
        return XhhGachaImport(
            source_uid=str(header["source_uid"]), nickname=str(header["nickname"]),
            total_count=max(0, int(header["total_count"])), imported_at=int(header["imported_at"]),
            pools=pools, six_stars=six_stars,
        )

    def save_sync_state(
        self,
        role: EndfieldRole,
        stream_key: str,
        *,
        newest_seq_id: str = "",
        page_cursor: str = "",
        error: str = "",
        synced_at: int | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sync_states(
                    role_id, server_id, stream_key, newest_seq_id, page_cursor, last_sync_at, last_error
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_id, server_id, stream_key) DO UPDATE SET
                    newest_seq_id = excluded.newest_seq_id,
                    page_cursor = excluded.page_cursor,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error
                """,
                (
                    role.role_id, role.server_id, stream_key, newest_seq_id, page_cursor,
                    int(synced_at or time.time()), str(error or "")[:240],
                ),
            )
            self.conn.commit()

    def list_sync_states(self, role: EndfieldRole) -> list[SyncState]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM sync_states WHERE role_id = ? AND server_id = ? ORDER BY stream_key",
                (role.role_id, role.server_id),
            ).fetchall()
        return [SyncState(**dict(row)) for row in rows]

    def list_gacha_records(
        self,
        role: EndfieldRole,
        *,
        page: int = 1,
        page_size: int = 20,
        pool_filter: str = "",
        limit: int | None = None,
    ) -> list[GachaRecord]:
        sql = "SELECT * FROM gacha_records WHERE role_id = ? AND server_id = ? AND item_id <> ''"
        params: list[object] = [role.role_id, role.server_id]
        if pool_filter:
            sql += " AND (pool_name LIKE ? OR pool_id = ? OR pool_type LIKE ?)"
            like = f"%{pool_filter}%"
            params.extend([like, pool_filter, like])
        sql += " ORDER BY gacha_ts DESC, id DESC"
        size = int(limit or page_size)
        sql += " LIMIT ? OFFSET ?"
        params.extend([size, 0 if limit else (max(1, page) - 1) * page_size])
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [self._gacha_record(row) for row in rows]

    def count_gacha_records(self, role: EndfieldRole, pool_filter: str = "") -> int:
        sql = "SELECT COUNT(*) AS count FROM gacha_records WHERE role_id = ? AND server_id = ? AND item_id <> ''"
        params: list[object] = [role.role_id, role.server_id]
        if pool_filter:
            sql += " AND (pool_name LIKE ? OR pool_id = ? OR pool_type LIKE ?)"
            like = f"%{pool_filter}%"
            params.extend([like, pool_filter, like])
        with self._lock:
            return int(self.conn.execute(sql, tuple(params)).fetchone()["count"])

    @staticmethod
    def _role(row: sqlite3.Row) -> EndfieldRole:
        return EndfieldRole(
            id=int(row["id"]), credential_id=int(row["credential_id"]), qq_user_id=str(row["qq_user_id"]),
            binding_uid=str(row["binding_uid"]), role_id=str(row["role_id"]), server_id=str(row["server_id"]),
            nickname=str(row["nickname"]), server_name=str(row["server_name"]), is_primary=bool(row["is_primary"]),
        )

    @staticmethod
    def _gacha_record(row: sqlite3.Row) -> GachaRecord:
        return GachaRecord(
            role_id=str(row["role_id"]), server_id=str(row["server_id"]), pool_id=str(row["pool_id"]),
            pool_name=str(row["pool_name"]), pool_type=str(row["pool_type"]), seq_id=str(row["seq_id"]),
            gacha_ts=int(row["gacha_ts"]), item_id=str(row["item_id"]), item_name=str(row["item_name"]),
            rarity=int(row["rarity"]), item_type=str(row["item_type"]), weapon_type=str(row["weapon_type"]),
            is_new=bool(row["is_new"]), is_free=bool(row["is_free"]),
        )
