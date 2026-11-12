"""scripts/migrate_platform.py, exercised through the real stores so key formats cannot drift."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from otae_bot.command_usage.store import CallRecord, Scope, UsageStore
from otae_bot.endfield_notifications.classification import Event
from otae_bot.endfield_notifications.receipts import ReceiptStore
from otae_bot.group_features import GroupFeatureStore, GroupScope
from plugins.grok_bot.conversations import ConversationScope, SessionStore
from plugins.tibo_radar.store import TiboStore

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("migrate_platform_for_test", ROOT / "scripts/migrate_platform.py")
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrate  # dataclasses look their module up while decorating
_spec.loader.exec_module(migrate)

# Import the announcement store without registering the Endfield plugin.
PACKAGE = "endfield_announcements_for_test"
_package = ModuleType(PACKAGE)
_package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules.setdefault(PACKAGE, _package)
models = importlib.import_module(f"{PACKAGE}.announcements.models")
announcement_store = importlib.import_module(f"{PACKAGE}.announcements.store")

OLD, NEW = "llonebot", "qq-client"
BOT, OTHER_BOT = "10001", "20002"
NOW = int(time.time())


def data_paths(data: Path) -> dict[str, Path]:
    return {store.path: data / store.path for store in migrate.STORES}


def article(cid: str, start: int):
    return models.Announcement(
        cid, "测试活动", NOW - 100, f"https://example.invalid/{cid}", "摘要", ("activity",),
        (models.ActivityWindow(f"{cid}:event", "测试活动", "activity", start, start + 86400),), "v1",
    )


def build(data: Path, platform: str = OLD) -> None:
    """Write one record per store as `platform`, using each store's own code."""
    paths = data_paths(data)

    switches = GroupFeatureStore(paths["group_manager/switches.json"])
    switches.set_enabled(GroupScope(platform, BOT, "g1"), "hyw", False)
    switches.set_enabled(GroupScope(platform, BOT, "g1"), "grok_bot", True)
    switches.set_enabled(GroupScope(platform, BOT, "u1", private=True), "grok_bot", True)
    switches.set_enabled(GroupScope(platform, OTHER_BOT, "g2"), "steamInfo", False)

    usage = UsageStore(paths["command_usage/usage.sqlite3"])
    usage.insert_many([
        CallRecord(time.time() - 60, platform, "g1", "u1", "hyw", "q", "", 10, True, name="群名片", nickname="昵称"),
        CallRecord(time.time() - 30, platform, "g1", "u2", "hyw", "q", "", 10, True, name="二号", nickname="二号昵称"),
        CallRecord(time.time() - 10, "other", "g9", "u9", "hyw", "q", "", 10, True, name="别的", nickname="别的"),
    ])
    usage.close()

    sessions = SessionStore(paths["grok_bot/sessions.json"])
    sessions.put(ConversationScope(platform, BOT, "group", "g1", "g1").key, str(uuid4()), str(uuid4()))
    sessions.put(ConversationScope(platform, OTHER_BOT, "private", "u1").key, str(uuid4()), str(uuid4()))

    tibo = TiboStore(paths["tibo_radar/tibo_radar.db"])
    tibo.subscribe("g1", account_key=f"{platform}:{BOT}")
    tibo.subscribe("g2", account_key=f"{platform}:{OTHER_BOT}")
    tibo.close()

    store = announcement_store.AnnouncementStore(paths["endfield/announcements.db"])
    store.subscribe(models.Subscription(models.Destination(platform, BOT, "g1", "g1"), created_at=NOW - 1000))
    store.apply_snapshot([article("a1", NOW + 7200)], NOW)  # initialises the subscription
    store.apply_snapshot([article("a1", NOW + 7200), article("a2", NOW + 9000)], NOW)  # queues news for a2
    store.plan(NOW)  # queues reminders
    with store.connect() as conn:
        first = conn.execute("SELECT key FROM outbox ORDER BY key LIMIT 1").fetchone()[0]
    store.sent(first)

    receipts = ReceiptStore(paths["endfield/notification_receipts.db"])
    claim = receipts.reserve(models.Destination(platform, BOT, "g1", "g1").key, "announcement",
                             [Event("测试活动", "activity", "start", NOW, start_at=NOW + 7200)], NOW)
    receipts.finish(claim, NOW)


def run(data: Path, *args: str) -> int:
    return migrate.main(["--data-dir", str(data), "--runtime-dir", str(data.parent / ".runtime"), *args])


def snapshot(data: Path) -> dict:
    """Logical content of every store, order-independent."""
    result = {}
    for store in migrate.STORES:
        path = data / store.path
        if not path.exists():
            continue
        if not store.sqlite:
            result[store.path] = json.loads(path.read_text(encoding="utf-8"))
            continue
        conn = sqlite3.connect(path)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        result[store.path] = {
            table: sorted(map(repr, conn.execute(f"SELECT * FROM {table}").fetchall())) for table in tables
        }
        conn.close()
    return result


def outbox(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute("SELECT key, subscription_key, state FROM outbox").fetchall())
    finally:
        conn.close()


@pytest.fixture
def data(tmp_path):
    data = tmp_path / "data"
    build(data)
    return data


def test_dry_run_changes_nothing(data, capsys):
    before = snapshot(data)
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok") == 0
    assert snapshot(data) == before
    assert not (data / "_migration_backup").exists()
    out = capsys.readouterr().out
    assert "预演" in out and BOT not in out and "g1" not in out


def test_apply_renames_every_store_and_lookups_hit(data):
    paths = data_paths(data)
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply") == 0

    switches = GroupFeatureStore(paths["group_manager/switches.json"])
    assert not switches.is_enabled(GroupScope(NEW, BOT, "g1"), "hyw")
    assert switches.is_enabled(GroupScope(NEW, BOT, "g1"), "grok_bot")
    assert switches.is_enabled(GroupScope(NEW, BOT, "u1", private=True), "grok_bot")
    assert not switches.is_enabled(GroupScope(NEW, OTHER_BOT, "g2"), "steamInfo")
    assert OLD not in paths["group_manager/switches.json"].read_text(encoding="utf-8")

    usage = UsageStore(paths["command_usage/usage.sqlite3"])
    group = usage.report(Scope(NEW, "g1"), 7, datetime.now())
    assert group.total == 2 and {row.name for row in group.top_users} == {"群名片", "二号"}
    overall = usage.report(Scope(), 7, datetime.now())
    assert overall.total == 3 and "昵称" in {row.name for row in overall.top_users}
    usage.close()
    conn = sqlite3.connect(paths["command_usage/usage.sqlite3"])
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE platform='other'").fetchone()[0] == 1
    conn.close()

    bindings = SessionStore(paths["grok_bot/sessions.json"]).snapshot()["bindings"]
    assert ConversationScope(NEW, BOT, "group", "g1", "g1").key in bindings
    assert ConversationScope(NEW, OTHER_BOT, "private", "u1").key in bindings
    assert len(bindings) == 2

    tibo = TiboStore(paths["tibo_radar/tibo_radar.db"])
    assert tibo.subscription("g1").account_key == f"{NEW}:{BOT}"
    assert tibo.subscription("g2").account_key == f"{NEW}:{OTHER_BOT}"
    tibo.close()

    store = announcement_store.AnnouncementStore(paths["endfield/announcements.db"])
    new_dest = models.Destination(NEW, BOT, "g1", "g1")
    assert store.subscription(new_dest.key).destination == new_dest
    assert store.subscription(models.Destination(OLD, BOT, "g1", "g1").key) is None
    jobs = outbox(paths["endfield/announcements.db"])
    assert jobs and all(job[1] == new_dest.key for job in jobs) and any(job[2] == "sent" for job in jobs)
    # Re-planning under the new name must find the same rows, not queue duplicates.
    store.apply_snapshot([article("a1", NOW + 7200), article("a2", NOW + 9000)], NOW)
    store.plan(NOW)
    assert outbox(paths["endfield/announcements.db"]) == jobs

    receipts = ReceiptStore(paths["endfield/notification_receipts.db"])
    claim = receipts.reserve(new_dest.key, "announcement", [Event("测试活动", "activity", "start", NOW, start_at=NOW + 7200)], NOW)
    assert claim.duplicates == (0,)


def test_apply_backs_up_and_is_idempotent(data, capsys):
    before = snapshot(data)
    backup = data.parent / "backup"
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply", "--backup-dir", str(backup)) == 0
    for store in migrate.STORES:
        assert (backup / store.path).is_file()
    copy = backup.parent / "backup-as-data"
    backup.rename(copy)
    assert snapshot(copy) == before

    after = snapshot(data)
    capsys.readouterr()
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply") == 0
    assert snapshot(data) == after
    assert "没有需要改动的存储" in capsys.readouterr().out
    assert not (data / "_migration_backup").exists()


def test_round_trip_restores_original(data):
    before = snapshot(data)
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply") == 0
    assert snapshot(data) != before
    assert run(data, "--from", NEW, "--to", OLD, "--include-grok", "--apply") == 0
    assert snapshot(data) == before


def test_grok_is_skipped_unless_requested(data, capsys):
    path = data / "grok_bot/sessions.json"
    before = path.read_text(encoding="utf-8")
    assert run(data, "--from", OLD, "--to", NEW, "--apply") == 0
    assert path.read_text(encoding="utf-8") == before
    assert "--include-grok" in capsys.readouterr().out


def test_self_id_filter(data, capsys):
    paths = data_paths(data)
    usage_before = snapshot(data)["command_usage/usage.sqlite3"]
    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply", "--self-id", BOT) == 0
    assert "--include-unscoped" in capsys.readouterr().out
    assert snapshot(data)["command_usage/usage.sqlite3"] == usage_before

    switches = GroupFeatureStore(paths["group_manager/switches.json"])
    assert not switches.is_enabled(GroupScope(NEW, BOT, "g1"), "hyw")
    assert not switches.is_enabled(GroupScope(OLD, OTHER_BOT, "g2"), "steamInfo")
    assert switches.is_enabled(GroupScope(NEW, OTHER_BOT, "g2"), "steamInfo")
    bindings = SessionStore(paths["grok_bot/sessions.json"]).snapshot()["bindings"]
    assert ConversationScope(NEW, BOT, "group", "g1", "g1").key in bindings
    assert ConversationScope(OLD, OTHER_BOT, "private", "u1").key in bindings
    tibo = TiboStore(paths["tibo_radar/tibo_radar.db"])
    assert tibo.subscription("g2").account_key == f"{OLD}:{OTHER_BOT}"
    tibo.close()

    assert run(data, "--from", OLD, "--to", NEW, "--apply", "--self-id", BOT, "--include-unscoped") == 0
    conn = sqlite3.connect(paths["command_usage/usage.sqlite3"])
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE platform=?", (OLD,)).fetchone()[0] == 0
    conn.close()


def test_conflicts_merge_with_store_semantics(data, capsys):
    paths = data_paths(data)
    # The new adapter already ran and wrote its own records.
    switches = GroupFeatureStore(paths["group_manager/switches.json"])
    switches.set_enabled(GroupScope(NEW, BOT, "g1"), "steamInfo", False)
    switches.set_enabled(GroupScope(NEW, BOT, "g1"), "hyw", True)  # explicit enable vs. old disable
    usage = UsageStore(paths["command_usage/usage.sqlite3"])
    usage.insert_many([
        CallRecord(time.time() + 100, NEW, "g1", "u1", "hyw", "q", "", 5, True, name="新名片", nickname="新昵称"),
        CallRecord(time.time() - 3600, NEW, "g1", "u2", "hyw", "q", "", 5, True, name="旧二号", nickname="旧二号"),
    ])
    usage.close()
    sessions = SessionStore(paths["grok_bot/sessions.json"])
    new_key = ConversationScope(NEW, BOT, "group", "g1", "g1").key
    kept_agent = str(uuid4())
    sessions.put(new_key, kept_agent, str(uuid4()))
    store = announcement_store.AnnouncementStore(paths["endfield/announcements.db"])
    new_dest = models.Destination(NEW, BOT, "g1", "g1")
    store.subscribe(models.Subscription(new_dest, created_at=NOW - 500))

    assert run(data, "--from", OLD, "--to", NEW, "--include-grok", "--apply") == 0
    out = capsys.readouterr().out
    assert "合并的作用域 1" in out and "丢弃的源绑定 1" in out and "丢弃的源订阅 1" in out

    switches = GroupFeatureStore(paths["group_manager/switches.json"])
    scope = GroupScope(NEW, BOT, "g1")
    assert not switches.is_enabled(scope, "steamInfo") and not switches.is_enabled(scope, "hyw")
    assert switches.is_enabled(scope, "grok_bot")
    saved = json.loads(paths["group_manager/switches.json"].read_text(encoding="utf-8"))
    assert "hyw" not in saved["enabled"][scope.key]

    conn = sqlite3.connect(paths["command_usage/usage.sqlite3"])
    names = dict(conn.execute("SELECT user_id, name FROM names WHERE platform=?", (NEW,)))
    users = dict(conn.execute("SELECT user_id, name FROM users WHERE platform=?", (NEW,)))
    assert names == {"u1": "新名片", "u2": "二号"} and users["u1"] == "新昵称" and users["u2"] == "二号昵称"
    assert conn.execute("SELECT COUNT(*) FROM names WHERE platform=?", (OLD,)).fetchone()[0] == 0
    conn.close()

    bindings = SessionStore(paths["grok_bot/sessions.json"]).snapshot()["bindings"]
    assert bindings[new_key]["agent_id"] == kept_agent

    assert store.subscription(new_dest.key).created_at == NOW - 500
    assert store.subscription(models.Destination(OLD, BOT, "g1", "g1").key) is None
    assert all(job[1] == new_dest.key for job in outbox(paths["endfield/announcements.db"]))


def test_missing_and_malformed_stores(tmp_path, capsys):
    data = tmp_path / "data"
    build(data)
    (data / "endfield/notification_receipts.db").unlink()
    switches = data / "group_manager/switches.json"
    switches.write_text('{"version": 9, "disabled": {}}', encoding="utf-8")
    assert run(data, "--from", OLD, "--to", NEW, "--apply") == 1
    out = capsys.readouterr().out
    assert "文件不存在" in out and "[错误]" in out
    assert switches.read_text(encoding="utf-8") == '{"version": 9, "disabled": {}}'
    tibo = TiboStore(data / "tibo_radar/tibo_radar.db")
    assert tibo.subscription("g1").account_key == f"{NEW}:{BOT}"
    tibo.close()


def test_refuses_while_bot_holds_run_lock(data, capsys):
    from otae_bot.lifecycle import acquire_run_lock

    before = snapshot(data)
    lock = acquire_run_lock(data.parent / ".runtime")
    assert lock is not None
    try:
        assert run(data, "--from", OLD, "--to", NEW, "--apply") == 1
    finally:
        lock.close()
    assert "运行锁" in capsys.readouterr().err
    assert snapshot(data) == before
    assert run(data, "--from", OLD, "--to", NEW, "--apply") == 0


def test_rejects_same_or_missing_names(data):
    with pytest.raises(SystemExit):
        run(data, "--from", OLD, "--to", OLD)
    with pytest.raises(SystemExit):
        run(data, "--to", NEW)
