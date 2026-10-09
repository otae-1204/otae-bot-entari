"""Independent encrypted SQLite storage for Arknights Skland bindings.

The database lives in ``data/arknights/arknights.db`` and never touches the
Endfield database or its schema.  Account ownership is isolated per QQ user id:
every read and write is scoped by ``qq_user_id``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .crypto import ArknightsCipher, EncryptedCredential
from .paths import DB_PATH

DEFAULT_DB_PATH = DB_PATH

ALL_SELECTORS = frozenset({"", "全部", "所有", "all"})
PRIMARY_SELECTORS = frozenset({"主账号", "主账户", "primary", "main"})

# Resolution verdicts; callers must refuse every value except ``resolved``/``all``.
RESOLVED = "resolved"
ALL = "all"
NO_BINDING = "no_binding"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"
BULK_NOT_ALLOWED = "bulk_not_allowed"


@dataclass(frozen=True, slots=True)
class RoleCandidate:
    """One ``gameId``-scoped Arknights character discovered on bind."""

    uid: str
    game_id: str
    nickname: str
    channel_name: str = ""


@dataclass(frozen=True, slots=True)
class ArknightsRole:
    id: int
    credential_id: int
    qq_user_id: str
    uid: str
    game_id: str
    channel_name: str
    nickname: str
    is_primary: bool

    @property
    def masked_uid(self) -> str:
        suffix = self.uid[-4:] if self.uid else "----"
        return f"****{suffix}"

    @property
    def server_label(self) -> str:
        return self.channel_name or f"渠道 {self.game_id}"

    @property
    def key(self) -> tuple[str, str]:
        return (self.uid, self.game_id)


@dataclass(frozen=True, slots=True)
class SelectorResolution:
    """Outcome of resolving a user-typed account selector.

    ``role`` is only set for ``RESOLVED``.  ``candidates`` carries every role a
    fuzzy-but-exact prefix matched so ambiguous input can be reported instead of
    silently picking one account.
    """

    role: ArknightsRole | None = None
    reason: str = RESOLVED
    candidates: tuple[ArknightsRole, ...] = ()
    selector: str = ""

    @property
    def resolved(self) -> bool:
        return self.reason == RESOLVED and self.role is not None


class ArknightsStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ setup

    def _init_schema(self) -> None:
        with self._lock:
            assert self.conn is not None
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
                    uid TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(qq_user_id, uid, game_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_arknights_primary_role
                    ON roles(qq_user_id) WHERE is_primary = 1;
                CREATE INDEX IF NOT EXISTS idx_arknights_roles_user
                    ON roles(qq_user_id, id);
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    # ------------------------------------------------------------------ writes

    def bind_roles(
        self,
        qq_user_id: str,
        account_token: str,
        roles: Sequence[RoleCandidate],
        cipher: ArknightsCipher,
    ) -> list[ArknightsRole]:
        """Store one account token and upsert its characters for this QQ user.

        Re-binding the same character updates it in place instead of creating a
        duplicate row; a second login account becomes a second credential.
        """
        normalized: dict[tuple[str, str], RoleCandidate] = {}
        for role in roles:
            uid = str(role.uid).strip()
            game_id = str(role.game_id).strip()
            if not uid or not game_id:
                continue
            normalized.setdefault((uid, game_id), RoleCandidate(
                uid=uid,
                game_id=game_id,
                nickname=str(role.nickname or "").strip(),
                channel_name=str(role.channel_name or "").strip(),
            ))
        if not normalized:
            return self.list_roles(qq_user_id)

        token = str(account_token or "").strip()
        if not token:
            raise ValueError("账号 Token 不能为空")

        encrypted = cipher.encrypt(token)
        fingerprint = _token_fingerprint(token)
        now = int(time.time())
        with self._lock:
            assert self.conn is not None
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    """
                    INSERT INTO credentials(
                        qq_user_id, token_fingerprint, token_nonce, token_ciphertext,
                        token_tag, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qq_user_id, token_fingerprint) DO UPDATE SET
                        token_nonce = excluded.token_nonce,
                        token_ciphertext = excluded.token_ciphertext,
                        token_tag = excluded.token_tag,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(qq_user_id),
                        fingerprint,
                        encrypted.nonce,
                        encrypted.ciphertext,
                        encrypted.tag,
                        now,
                        now,
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
                        "SELECT 1 FROM roles WHERE qq_user_id = ? AND is_primary = 1",
                        (str(qq_user_id),),
                    ).fetchone()
                )
                for candidate in normalized.values():
                    make_primary = 0 if has_primary else 1
                    self.conn.execute(
                        """
                        INSERT INTO roles(
                            credential_id, qq_user_id, uid, game_id, channel_name,
                            nickname, is_primary, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(qq_user_id, uid, game_id) DO UPDATE SET
                            credential_id = excluded.credential_id,
                            channel_name = excluded.channel_name,
                            nickname = excluded.nickname,
                            is_primary = CASE
                                WHEN roles.is_primary = 1 OR excluded.is_primary = 1 THEN 1
                                ELSE 0
                            END,
                            updated_at = excluded.updated_at
                        """,
                        (
                            credential_id,
                            str(qq_user_id),
                            candidate.uid,
                            candidate.game_id,
                            candidate.channel_name,
                            candidate.nickname,
                            make_primary,
                            now,
                            now,
                        ),
                    )
                    if make_primary:
                        has_primary = True
                self._delete_orphan_credentials(str(qq_user_id))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.list_roles(qq_user_id)

    def set_primary(self, qq_user_id: str, selector: str) -> SelectorResolution:
        resolution = self.resolve(qq_user_id, selector)
        if not resolution.resolved or resolution.role is None:
            return resolution
        with self._lock:
            assert self.conn is not None
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    "UPDATE roles SET is_primary = 0 WHERE qq_user_id = ?",
                    (str(qq_user_id),),
                )
                self.conn.execute(
                    "UPDATE roles SET is_primary = 1 WHERE id = ?",
                    (resolution.role.id,),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return SelectorResolution(
            self.get_role(resolution.role.id), RESOLVED, resolution.candidates, resolution.selector
        )

    def unbind(self, qq_user_id: str, selector: str) -> SelectorResolution:
        """Delete exactly one resolved role; never delete several accounts."""
        resolution = self.resolve(qq_user_id, selector)
        if not resolution.resolved or resolution.role is None:
            return resolution
        role = resolution.role
        with self._lock:
            assert self.conn is not None
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM roles WHERE id = ?", (role.id,))
                self._delete_orphan_credentials(str(qq_user_id))
                remaining = self.conn.execute(
                    "SELECT id FROM roles WHERE qq_user_id = ? ORDER BY id LIMIT 1",
                    (str(qq_user_id),),
                ).fetchone()
                if role.is_primary and remaining is not None:
                    self.conn.execute(
                        "UPDATE roles SET is_primary = 1 WHERE id = ?",
                        (int(remaining["id"]),),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return resolution

    def _delete_orphan_credentials(self, qq_user_id: str) -> None:
        assert self.conn is not None
        self.conn.execute(
            """
            DELETE FROM credentials
            WHERE qq_user_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM roles WHERE roles.credential_id = credentials.id
              )
            """,
            (str(qq_user_id),),
        )

    # ------------------------------------------------------------------- reads

    def list_roles(self, qq_user_id: str) -> list[ArknightsRole]:
        with self._lock:
            assert self.conn is not None
            rows = self.conn.execute(
                "SELECT * FROM roles WHERE qq_user_id = ? ORDER BY id ASC",
                (str(qq_user_id),),
            ).fetchall()
        return [self._role(row) for row in rows]

    def list_bound_users(self) -> list[str]:
        with self._lock:
            assert self.conn is not None
            rows = self.conn.execute(
                "SELECT DISTINCT qq_user_id FROM roles ORDER BY qq_user_id ASC"
            ).fetchall()
        return [str(row["qq_user_id"]) for row in rows]

    def get_role(self, role_id: int) -> ArknightsRole | None:
        with self._lock:
            assert self.conn is not None
            row = self.conn.execute("SELECT * FROM roles WHERE id = ?", (int(role_id),)).fetchone()
        return self._role(row) if row else None

    def credential_count(self) -> int:
        with self._lock:
            assert self.conn is not None
            return int(self.conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0])

    def resolve(self, qq_user_id: str, selector: str = "") -> SelectorResolution:
        """Resolve one selector to exactly one role, or explain the failure.

        Matching is exact by design: full UID, then UID suffix (at least four
        characters), then the listed index (fewer than four digits), then an
        exact nickname.  Nothing is guessed and nothing fuzzy is accepted, so a
        selector can never sign in or delete an unintended account.
        """
        roles = self.list_roles(qq_user_id)
        value = str(selector or "").strip()
        if not roles:
            return SelectorResolution(None, NO_BINDING, (), value)
        folded = value.casefold()
        if not value or folded in PRIMARY_SELECTORS:
            primary = next((role for role in roles if role.is_primary), roles[0])
            return SelectorResolution(primary, RESOLVED, (primary,), value)
        if folded in ALL_SELECTORS:
            return SelectorResolution(None, BULK_NOT_ALLOWED, tuple(roles), value)

        exact_uid = [role for role in roles if role.uid == value]
        if exact_uid:
            return _single_or_ambiguous(exact_uid, value)

        if len(value) >= 4:
            suffixed = [role for role in roles if role.uid.endswith(value)]
            if suffixed:
                return _single_or_ambiguous(suffixed, value)

        # Four or more digits are meant as a UID suffix.  One that matched no
        # suffix above must not fall back to an index: "0001" is not role #1.
        if value.isdecimal() and len(value) < 4:
            index = int(value) - 1
            if 0 <= index < len(roles):
                role = roles[index]
                return SelectorResolution(role, RESOLVED, (role,), value)

        named = [role for role in roles if role.nickname.casefold() == folded]
        if named:
            return _single_or_ambiguous(named, value)

        return SelectorResolution(None, NOT_FOUND, (), value)

    def resolve_roles(
        self, qq_user_id: str, selector: str = ""
    ) -> tuple[tuple[ArknightsRole, ...], SelectorResolution]:
        """Resolve an attendance selector: empty/全部 expands to every role."""
        roles = self.list_roles(qq_user_id)
        value = str(selector or "").strip()
        if not roles:
            return (), SelectorResolution(None, NO_BINDING, (), value)
        if not value or value.casefold() in ALL_SELECTORS:
            return tuple(roles), SelectorResolution(None, ALL, tuple(roles), value)
        resolution = self.resolve(qq_user_id, value)
        if resolution.role is None:
            return (), resolution
        return (resolution.role,), resolution

    def decrypt_token(self, role: ArknightsRole, cipher: ArknightsCipher) -> str:
        """Decrypt the credential behind one role.

        The read is scoped by owner as well as by credential id: a credential is
        never handed out for a role that does not belong to the same QQ user,
        even if a caller passes a role object from somewhere else.
        """
        with self._lock:
            assert self.conn is not None
            row = self.conn.execute(
                """
                SELECT token_nonce, token_ciphertext, token_tag
                FROM credentials
                WHERE id = ? AND qq_user_id = ?
                """,
                (role.credential_id, role.qq_user_id),
            ).fetchone()
        if row is None:
            raise LookupError("账号凭据不存在或不属于当前用户，请重新绑定。")
        return cipher.decrypt(
            EncryptedCredential(row["token_nonce"], row["token_ciphertext"], row["token_tag"])
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _role(row: sqlite3.Row) -> ArknightsRole:
        return ArknightsRole(
            id=int(row["id"]),
            credential_id=int(row["credential_id"]),
            qq_user_id=str(row["qq_user_id"]),
            uid=str(row["uid"]),
            game_id=str(row["game_id"]),
            channel_name=str(row["channel_name"]),
            nickname=str(row["nickname"]),
            is_primary=bool(row["is_primary"]),
        )


def _single_or_ambiguous(matches: Iterable[ArknightsRole], selector: str) -> SelectorResolution:
    found = tuple(matches)
    if len(found) == 1:
        return SelectorResolution(found[0], RESOLVED, found, selector)
    return SelectorResolution(None, AMBIGUOUS, found, selector)


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:24]
