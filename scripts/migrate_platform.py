"""One-time rename of the account platform string in the bot's persisted data.

Accounts used to be reported by LLBot's native Satori as platform ``llonebot``;
through the in-process OneBot boundary they are ``qq-client``. Every store that
keys data by platform must be renamed, or switches, statistics and
subscriptions silently stop matching.

Run it from the bot project root with the bot STOPPED:

    python -X utf8 scripts/migrate_platform.py --from llonebot --to qq-client            # dry run
    python -X utf8 scripts/migrate_platform.py --from llonebot --to qq-client --apply    # write
    python -X utf8 scripts/migrate_platform.py --from qq-client --to llonebot --apply    # rollback

Output is counts only. Exit codes: 0 success, 1 refused or at least one store failed.
Standard library only, so it never imports (and starts) bot code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID


class StoreError(Exception):
    """The store is not in the format this tool knows; it is left untouched."""


@dataclass(frozen=True)
class Rename:
    source: str
    target: str
    self_ids: frozenset[str] | None = None

    def wants(self, platform: object, self_id: object) -> bool:
        return platform == self.source and (self.self_ids is None or str(self_id) in self.self_ids)


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    changes: int = 0

    def add(self, label: str, n: int = 1, *, change: bool = True) -> None:
        self.counts[label] = self.counts.get(label, 0) + n
        if change:
            self.changes += n


# -- JSON stores ----------------------------------------------------------------


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise StoreError("无法读取或解析 JSON") from None


def write_json(path: Path, data: Any) -> None:
    # Same serialisation and atomic replace as the stores' own writers.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _string_list(raw: str, lengths: tuple[int, ...]) -> list[str]:
    try:
        parts = json.loads(raw)
    except ValueError:
        raise StoreError("键不是 JSON") from None
    if not isinstance(parts, list) or len(parts) not in lengths or not all(isinstance(p, str) for p in parts):
        raise StoreError("键结构不符合预期")
    return parts


def migrate_switches(data: Any, rename: Rename, report: Report) -> Any:
    """otae_bot/group_features.py GroupFeatureStore (versions 1 and 2)."""
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        raise StoreError("版本不是 1 或 2")
    sections = ["disabled", "enabled"] if data["version"] == 2 else ["disabled"]
    for name in sections:
        mapping = data.get(name)
        if not isinstance(mapping, dict) or any(
            not isinstance(items, list) or not all(isinstance(i, str) for i in items) for items in mapping.values()
        ):
            raise StoreError(f"{name} 段结构不符合预期")

    renames: dict[str, str] = {}
    for key in {key for name in sections for key in data[name]}:
        parts = _string_list(key, (3, 4))
        if len(parts) == 4 and parts[0] != "private":
            raise StoreError("私聊键结构不符合预期")
        offset = len(parts) - 3  # GroupScope.key: ["private", platform, self_id, peer] or [platform, self_id, group]
        if parts[offset] != rename.source:
            continue
        if not rename.wants(parts[offset], parts[offset + 1]):
            report.add("按 --self-id 跳过的键", change=False)
            continue
        parts[offset] = rename.target
        renames[key] = json.dumps(parts, ensure_ascii=False)
    if not renames:
        return data

    existing = {key for name in sections for key in data[name]}
    merged = {new for new in renames.values() if new in existing}
    result = dict(data)
    for name in sections:
        updated: dict[str, list[str]] = {}
        for key, items in data[name].items():
            key = renames.get(key, key)
            updated[key] = sorted(set(updated[key]) | set(items)) if key in updated else items
        result[name] = updated
    if "enabled" in result:
        # is_enabled() lets an explicit disable win; keep the store's invariant
        # that a plugin is never listed in both for one scope.
        for key in merged:
            disabled = set(result["disabled"].get(key, ()))
            if key in result["enabled"]:
                kept = [p for p in result["enabled"][key] if p not in disabled]
                if kept:
                    result["enabled"][key] = kept
                else:
                    del result["enabled"][key]
    report.add("重命名的作用域键", len(renames) - len(merged))
    report.add("与已有目标键合并的作用域", len(merged))
    return result


def migrate_grok(data: Any, rename: Rename, report: Report) -> Any:
    """plugins/grok_bot/conversations.py SessionStore."""
    try:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError
        UUID(data["installation"])
        bindings = data["bindings"]
        if not isinstance(bindings, dict):
            raise TypeError
        for entry in bindings.values():
            UUID(entry["nonce"])
            if entry["agent_id"] is not None:
                UUID(entry["agent_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise StoreError("结构不符合 SessionStore 格式") from None

    updated: dict[str, Any] = {}
    renamed = dropped = 0
    sources = []
    for key in bindings:
        parts = _string_list(key, (5,))  # ConversationScope.key
        if parts[0] != rename.source:
            continue
        if not rename.wants(parts[0], parts[1]):
            report.add("按 --self-id 跳过的绑定", change=False)
            continue
        sources.append(key)
    if not sources:
        return data
    new_keys = {}
    for key in sources:
        parts = json.loads(key)
        parts[0] = rename.target
        new_keys[key] = json.dumps(parts, ensure_ascii=False)
    for key, entry in bindings.items():
        new = new_keys.get(key)
        if new is None:
            updated[key] = entry
        elif new in bindings:
            # The target conversation already has its own Bot; keep it.
            dropped += 1
        else:
            updated[new] = entry
            renamed += 1
    report.add("重命名的绑定", renamed)
    report.add("目标已有绑定而丢弃的源绑定", dropped)
    return {**data, "bindings": updated}


# -- SQLite stores --------------------------------------------------------------


def _require(conn: sqlite3.Connection, table: str, columns: set[str]) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not present:
        raise StoreError(f"缺少表 {table}")
    if missing := columns - present:
        raise StoreError(f"表 {table} 缺少列 {', '.join(sorted(missing))}")


def migrate_usage(conn: sqlite3.Connection, rename: Rename, report: Report) -> None:
    """otae_bot/command_usage/store.py UsageStore; it records no bot self id."""
    _require(conn, "calls", {"platform"})
    _require(conn, "names", {"platform", "group_id", "user_id", "name", "updated"})
    _require(conn, "users", {"platform", "user_id", "name", "updated"})
    src, dst = rename.source, rename.target
    report.add("calls 行", conn.execute("UPDATE calls SET platform=? WHERE platform=?", (dst, src)).rowcount)

    for table, keys in (("names", ("group_id", "user_id")), ("users", ("user_id",))):
        match = " AND ".join(f"{k}=?" for k in keys)
        renamed = merged = 0
        rows = conn.execute(f"SELECT {', '.join(keys)}, name, updated FROM {table} WHERE platform=?", (src,)).fetchall()
        for row in rows:
            ident, name, updated = row[:-2], row[-2], row[-1]
            current = conn.execute(f"SELECT updated FROM {table} WHERE platform=? AND {match}", (dst, *ident)).fetchone()
            if current is None:
                conn.execute(f"UPDATE {table} SET platform=? WHERE platform=? AND {match}", (dst, src, *ident))
                renamed += 1
                continue
            # insert_many's upsert rule: the incoming row wins when not older.
            if updated >= current[0]:
                conn.execute(f"UPDATE {table} SET name=?, updated=? WHERE platform=? AND {match}",
                             (name, updated, dst, *ident))
            conn.execute(f"DELETE FROM {table} WHERE platform=? AND {match}", (src, *ident))
            merged += 1
        report.add(f"{table} 行", renamed)
        report.add(f"{table} 与已有目标行合并（保留较新）", merged)


def migrate_tibo(conn: sqlite3.Connection, rename: Rename, report: Report) -> None:
    """plugins/tibo_radar/store.py; account_key is "<platform>:<self_id>"."""
    _require(conn, "tibo_subscriptions", {"group_id", "account_key"})
    prefix = rename.source + ":"
    rows = conn.execute(
        "SELECT rowid, account_key FROM tibo_subscriptions WHERE substr(account_key, 1, ?) = ?",
        (len(prefix), prefix),
    ).fetchall()
    for rowid, key in rows:
        self_id = key[len(prefix):]
        if not rename.wants(rename.source, self_id):
            report.add("按 --self-id 跳过的订阅", change=False)
            continue
        conn.execute("UPDATE tibo_subscriptions SET account_key=? WHERE rowid=?", (f"{rename.target}:{self_id}", rowid))
        report.add("订阅行")


def _destination_key(parts: list) -> str:
    # Destination.key / destination_key(): compact separators, ASCII escapes.
    return json.dumps(parts, separators=(",", ":"))


def _destination(raw: str) -> list:
    try:
        parts = json.loads(raw)
    except ValueError:
        raise StoreError("目标键不是 JSON") from None
    if (not isinstance(parts, list) or len(parts) != 4 or not isinstance(parts[2], bool)
            or not all(isinstance(parts[i], str) for i in (0, 1, 3))):
        raise StoreError("目标键结构不符合预期")
    return parts


def _digest(value: object) -> str:
    # plugins/endfield/announcements/models.py digest().
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _outbox_key(row: sqlite3.Row, articles: dict[str, dict], old_dest: str, new_dest: str) -> str | None:
    """Re-derive an outbox key for the new destination, so plan() matches sent rows."""
    article = articles.get(row["article_id"])
    if article is None:
        return None
    if row["reminder"]:
        tails = [[w.get("key"), row["phase"], w.get(when)]
                 for w in article.get("windows", ()) if w.get("key") == row["window_key"]
                 for when in ("start_at", "end_at")]
    else:
        tails = [[article.get("cid"), "news", article.get("fingerprint")]]
    for tail in tails:
        if _digest([old_dest, *tail]) == row["key"]:
            return _digest([new_dest, *tail])
    return None


def migrate_announcements(conn: sqlite3.Connection, rename: Rename, report: Report) -> None:
    """plugins/endfield/announcements/store.py AnnouncementStore."""
    _require(conn, "subscriptions", {"key", "payload"})
    _require(conn, "outbox", {"key", "subscription_key", "article_id", "reminder", "window_key", "phase"})
    _require(conn, "articles", {"cid", "payload"})
    conn.row_factory = sqlite3.Row
    plans = []
    for row in conn.execute("SELECT key, payload FROM subscriptions").fetchall():
        parts = _destination(row["key"])
        try:
            payload = json.loads(row["payload"])
            dest = payload["destination"]
            derived = [dest["platform"], dest["account_id"], dest["private"], dest["target_id"]]
        except (ValueError, TypeError, KeyError):
            raise StoreError("订阅内容结构不符合预期") from None
        if _destination_key(derived) != row["key"]:
            raise StoreError("订阅键与内容中的目标不一致")
        if parts[0] != rename.source:
            continue
        if not rename.wants(parts[0], parts[1]):
            report.add("按 --self-id 跳过的订阅", change=False)
            continue
        plans.append((row["key"], parts, payload))
    if not plans:
        return

    articles = {}
    for (raw,) in conn.execute("SELECT payload FROM articles"):
        try:
            article = json.loads(raw)
            articles[article["cid"]] = article
        except (ValueError, TypeError, KeyError):
            raise StoreError("公告内容结构不符合预期") from None
    for old_key, parts, payload in plans:
        new_key = _destination_key([rename.target, *parts[1:]])
        if conn.execute("SELECT 1 FROM subscriptions WHERE key=?", (new_key,)).fetchone():
            report.add("目标已有订阅而丢弃的源订阅")
        else:
            payload["destination"]["platform"] = rename.target
            conn.execute("INSERT INTO subscriptions VALUES (?,?)", (new_key, json.dumps(payload, ensure_ascii=False)))
            report.add("订阅行")
        for job in conn.execute("SELECT * FROM outbox WHERE subscription_key=?", (old_key,)).fetchall():
            job_key = _outbox_key(job, articles, old_key, new_key)
            if job_key is None:
                job_key = job["key"]  # Article gone or changed: plan() will not regenerate it.
                report.add("保留原键的投递队列行", change=False)
            if job_key != job["key"] and conn.execute("SELECT 1 FROM outbox WHERE key=?", (job_key,)).fetchone():
                conn.execute("DELETE FROM outbox WHERE key=?", (job["key"],))
                report.add("目标已有而丢弃的投递队列行")
                continue
            conn.execute("UPDATE outbox SET key=?, subscription_key=? WHERE key=?", (job_key, new_key, job["key"]))
            report.add("投递队列行")
        conn.execute("DELETE FROM subscriptions WHERE key=?", (old_key,))
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise StoreError("迁移后外键检查失败")


def migrate_receipts(conn: sqlite3.Connection, rename: Rename, report: Report) -> None:
    """otae_bot/endfield_notifications/receipts.py ReceiptStore."""
    _require(conn, "delivery_receipts", {"id", "destination"})
    for rowid, raw in conn.execute("SELECT id, destination FROM delivery_receipts").fetchall():
        parts = _destination(raw)
        if parts[0] != rename.source:
            continue
        if not rename.wants(parts[0], parts[1]):
            report.add("按 --self-id 跳过的回执", change=False)
            continue
        parts[0] = rename.target
        conn.execute("UPDATE delivery_receipts SET destination=? WHERE id=?", (_destination_key(parts), rowid))
        report.add("回执行")


# -- Store plumbing -------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    name: str
    path: str
    migrate: Callable
    sqlite: bool
    scoped: bool = True  # records a bot self id, so --self-id can filter it


STORES = (
    Store("群功能开关", "group_manager/switches.json", migrate_switches, sqlite=False),
    Store("指令统计", "command_usage/usage.sqlite3", migrate_usage, sqlite=True, scoped=False),
    Store("Grok 会话绑定", "grok_bot/sessions.json", migrate_grok, sqlite=False),
    Store("Tibo 雷达订阅", "tibo_radar/tibo_radar.db", migrate_tibo, sqlite=True),
    Store("终末地公告订阅", "endfield/announcements.db", migrate_announcements, sqlite=True),
    Store("终末地通知回执", "endfield/notification_receipts.db", migrate_receipts, sqlite=True),
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def plan(store: Store, path: Path, rename: Rename) -> Report:
    """Run the migration without touching the file (SQLite: on an in-memory copy)."""
    report = Report()
    if not store.sqlite:
        store.migrate(read_json(path), rename, report)
        return report
    source, memory = _connect(path), _connect(Path(":memory:"))
    try:
        source.backup(memory)
        memory.execute("PRAGMA foreign_keys=ON")
        memory.execute("BEGIN")
        store.migrate(memory, rename, report)
    except sqlite3.DatabaseError as error:
        raise StoreError(f"SQLite 错误：{type(error).__name__}") from None
    finally:
        source.close()
        memory.close()
    return report


def apply(store: Store, path: Path, rename: Rename) -> Report:
    report = Report()
    if not store.sqlite:
        data = read_json(path)
        result = store.migrate(data, rename, report)
        if report.changes:
            write_json(path, result)
        return report
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            store.migrate(conn, rename, report)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError as error:
        raise StoreError(f"SQLite 错误：{type(error).__name__}") from None
    finally:
        conn.close()
    return report


def backup(store: Store, path: Path, backup_dir: Path) -> None:
    target = backup_dir / store.path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not store.sqlite:
        shutil.copy2(path, target)
        return
    # Fold the WAL in first; the backup API then yields one consistent file.
    conn, copy = _connect(path), sqlite3.connect(target)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.backup(copy)
    finally:
        copy.close()
        conn.close()


def acquire_lock(runtime_dir: Path) -> BinaryIO | None:
    """Take the bot's own run lock (otae_bot/lifecycle.py), so it cannot start meanwhile."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handle = (runtime_dir / "bot.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def describe(report: Report) -> str:
    return "，".join(f"{label} {n}" for label, n in report.counts.items()) or "无需改动"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="把持久化数据中的平台名从 --from 改为 --to（默认只预演）。")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--from", dest="source", required=True, help="原平台名，例如 llonebot")
    parser.add_argument("--to", dest="target", required=True, help="新平台名，例如 qq-client")
    parser.add_argument("--self-id", action="append", help="只迁移这些机器人账号（可重复）")
    parser.add_argument("--include-unscoped", action="store_true",
                        help="配合 --self-id：仍迁移不记录账号的存储（指令统计）")
    parser.add_argument("--include-grok", action="store_true",
                        help="同时迁移 Grok 会话绑定（会使已绑定会话的云端标记失配，见 README）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只预演")
    parser.add_argument("--backup-dir", type=Path, help="默认 <data-dir>/_migration_backup/<时间戳>/")
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"), help="机器人运行锁所在目录")
    args = parser.parse_args(argv)
    if not args.source or not args.target or args.source == args.target:
        parser.error("--from 与 --to 必须非空且不同")

    rename = Rename(args.source, args.target, frozenset(args.self_id) if args.self_id else None)
    mode = "执行" if args.apply else "预演"
    print(f"[{mode}] 平台名 {rename.source} -> {rename.target}，数据目录 {args.data_dir}")
    if rename.self_ids is not None:
        print(f"仅限 {len(rename.self_ids)} 个机器人账号")

    lock = None
    if args.apply:
        lock = acquire_lock(args.runtime_dir)
        if lock is None:
            print(f"[拒绝] 机器人运行锁被占用（{args.runtime_dir / 'bot.lock'}），请先停止机器人。", file=sys.stderr)
            return 1
    try:
        return run(args, rename)
    finally:
        if lock is not None:
            lock.close()


def run(args: argparse.Namespace, rename: Rename) -> int:
    failed = False
    pending: list[tuple[Store, Path]] = []
    for store in STORES:
        path = args.data_dir / store.path
        label = f"{store.name}（{store.path}）"
        if not path.is_file():
            print(f"- {label}：文件不存在，跳过")
            continue
        if not store.scoped and rename.self_ids is not None and not args.include_unscoped:
            print(f"- {label}：不记录机器人账号，指定了 --self-id 时默认跳过（加 --include-unscoped 一并迁移）")
            continue
        if store.migrate is migrate_grok and not args.include_grok:
            print(f"- {label}：默认跳过。会话标记由键的哈希写入云端 Bot，改键后已绑定会话会报绑定不一致；"
                  "确需迁移请加 --include-grok")
            continue
        try:
            report = plan(store, path, rename)
        except StoreError as error:
            print(f"- {label}：[错误] {error}，未改动该存储")
            failed = True
            continue
        print(f"- {label}：{describe(report)}")
        if report.changes:
            pending.append((store, path))

    if not args.apply:
        print("预演结束，未写入任何文件。确认无误后加 --apply 执行。")
        return 1 if failed else 0
    if not pending:
        print("没有需要改动的存储。")
        return 1 if failed else 0

    backup_dir = args.backup_dir or args.data_dir / "_migration_backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for store, path in pending:
            backup(store, path, backup_dir)
    except (OSError, sqlite3.DatabaseError) as error:
        print(f"[错误] 备份失败（{type(error).__name__}），未改动任何存储。", file=sys.stderr)
        return 1
    print(f"已备份 {len(pending)} 个文件到 {backup_dir}")

    for store, path in pending:
        try:
            report = apply(store, path, rename)
        except (StoreError, OSError) as error:
            print(f"- {store.name}：[错误] 写入失败（{error}），该存储保持原样")
            failed = True
            continue
        print(f"- {store.name}：已写入，{describe(report)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
