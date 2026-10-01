"""Shared SQLite claims: classify first, acknowledge only after a successful send."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .classification import Event

WINDOW_SECONDS = 86400
LEASE_SECONDS = 300
RETENTION_SECONDS = 90 * WINDOW_SECONDS


def destination_key(
    platform: str, account: str, target: str, private: bool = False
) -> str:
    if not all((platform, account, target)):
        raise ValueError("A receipt requires platform, bot account and destination")
    return json.dumps(
        [str(platform), str(account), bool(private), str(target)], separators=(",", ":")
    )


def equivalent(a: Event, b: Event) -> bool:
    if (a.name, a.kind, a.phase) != (b.name, b.kind, b.phase):
        return False
    if (
        not a.source_at
        or not b.source_at
        or abs(a.source_at - b.source_at) >= WINDOW_SECONDS
    ):
        return False
    if a.version and b.version and a.version != b.version:
        return False
    if a.start_at and b.start_at and a.start_at != b.start_at:
        return False
    if a.end_at and b.end_at and a.end_at != b.end_at:
        return False
    if a.phase == "update":
        return bool(a.fingerprint and a.fingerprint == b.fingerprint)
    return bool(
        a.kind.startswith("document:")
        or a.start_at
        or b.start_at
        or (a.version and a.version == b.version)
        or (a.fingerprint and a.fingerprint == b.fingerprint)
    )


@dataclass(frozen=True)
class Claim:
    token: str
    accepted: tuple[int, ...]
    duplicates: tuple[int, ...]
    busy: bool = False


class ReceiptStore:
    def __init__(self, path: str | Path = "data/endfield/notification_receipts.db"):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS delivery_receipts (
            id INTEGER PRIMARY KEY, destination TEXT NOT NULL, source TEXT NOT NULL,
            event_key TEXT NOT NULL, payload TEXT NOT NULL, token TEXT NOT NULL,
            state TEXT NOT NULL, lease_until INTEGER NOT NULL, touched_at INTEGER NOT NULL
        )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS receipts_match ON delivery_receipts(destination,event_key)"
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def reserve(
        self, destination: str, source: str, events: list[Event | None], now: int
    ) -> Claim:
        token = uuid4().hex
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM delivery_receipts WHERE touched_at<? OR (state='pending' AND lease_until<=?)",
                    (now - RETENTION_SECONDS, now),
                )
                accepted, duplicates = [], []
                for index, event in enumerate(events):
                    if event is None:
                        accepted.append(index)
                        continue
                    key = json.dumps(
                        [event.name, event.kind, event.phase], ensure_ascii=False
                    )
                    rows = conn.execute(
                        "SELECT * FROM delivery_receipts WHERE destination=? AND event_key=?",
                        (destination, key),
                    ).fetchall()
                    matches = [
                        row
                        for row in rows
                        if row["token"] != token
                        and equivalent(event, Event(**json.loads(row["payload"])))
                    ]
                    if any(row["state"] == "sent" for row in matches):
                        duplicates.append(index)
                    elif matches:
                        # Release this batch's claims; wait for the peer ACK.
                        conn.execute(
                            "DELETE FROM delivery_receipts WHERE token=?", (token,)
                        )
                        return Claim(token, (), tuple(duplicates), True)
                    else:
                        accepted.append(index)
                        conn.execute(
                            "INSERT INTO delivery_receipts(destination,source,event_key,payload,token,state,lease_until,touched_at) VALUES (?,?,?,?,?,'pending',?,?)",
                            (
                                destination,
                                source,
                                key,
                                json.dumps(asdict(event), ensure_ascii=False),
                                token,
                                now + LEASE_SECONDS,
                                now,
                            ),
                        )
                return Claim(token, tuple(accepted), tuple(duplicates))
        finally:
            conn.close()

    def finish(self, claim: Claim, now: int) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE delivery_receipts SET state='sent',touched_at=? WHERE token=? AND state='pending'",
                    (now, claim.token),
                )
        finally:
            conn.close()

    def release(self, claim: Claim) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM delivery_receipts WHERE token=? AND state='pending'",
                    (claim.token,),
                )
        finally:
            conn.close()
