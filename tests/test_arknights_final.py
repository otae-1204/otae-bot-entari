"""Final presentation boundary checks, using synthetic roles only."""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "arknights_final_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/arknights")]
sys.modules[PACKAGE] = package

# Keep shared asset paths rooted in the test workspace for other plugin tests.
importlib.import_module("otae_bot.config.paths")

# Do not load a workspace .env even when this file is run in isolation.
_previous_cwd = Path.cwd()
with tempfile.TemporaryDirectory() as scratch:
    try:
        os.chdir(scratch)
        models = importlib.import_module(f"{PACKAGE}.models")
        cards = importlib.import_module(f"{PACKAGE}.rendering.cards")
    finally:
        os.chdir(_previous_cwd)


def test_unknown_status_has_consistent_failure_count_and_label():
    role = models.AttendanceRoleView(nickname="博士", uid="****1234", status="unexpected")
    view = models.AttendanceCardView(roles=(role,))
    assert view.counts() == {"success": 0, "already": 0, "failed": 1, "total": 1}
    assert role.status_label == "签到失败"
    assert 'class="ak-row status-failed"' in cards.render_attendance_card_html(view)


def test_missing_channel_does_not_mislabel_bilibili_as_official():
    role = SimpleNamespace(nickname="博士", masked_uid="****1234", uid="10001234",
                           channel_name="", server_label="渠道 2")
    view = models.build_attendance_view(role, SimpleNamespace(status="already", message="", rewards=()))
    assert view.server_label == "渠道 2"
    assert models.AttendanceRoleView(nickname="博士", uid="****1234").server_label == "未知渠道"


def test_excessive_roster_falls_back_before_rendering_partial_result():
    roles = tuple(models.AttendanceRoleView(nickname=f"博士{i}", uid=f"****{i:04}")
                  for i in range(cards.MAX_CARD_ROWS + 1))
    view = models.AttendanceCardView(roles=roles)
    with mock.patch.object(cards, "screenshot_web_element", new_callable=mock.AsyncMock) as screenshot:
        try:
            asyncio.run(cards.draw_attendance_card(view))
        except ValueError:
            pass
        else:
            raise AssertionError("An incomplete image must never be delivered")
        screenshot.assert_not_awaited()
    assert f"博士{cards.MAX_CARD_ROWS}" in models.format_attendance_report(view)


def test_offline_preview_never_opens_dotenv():
    # Audit the subprocess rather than assuming imports are free of side effects.
    script = ROOT / "scripts/render_arknights_attendance_preview.py"
    harness = """import os, runpy, sys
script, output = sys.argv[1:]
def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.basename(os.fsdecode(args[0])) == '.env':
            raise AssertionError('Offline preview attempted to read .env')
sys.addaudithook(guard)
sys.argv = [script, '--html-only', '--output-dir', output]
runpy.run_path(script, run_name='__main__')
"""
    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / ".env").write_text("SYNTHETIC_PREVIEW_SENTINEL=1", encoding="utf-8")
        result = subprocess.run([sys.executable, "-c", harness, str(script), str(Path(scratch) / "out")],
                                cwd=scratch, capture_output=True, text=True, encoding="utf-8", timeout=30,
                                check=False)
        assert result.returncode == 0, result.stderr
        assert (Path(scratch) / "out" / "validation.json").is_file()


def test_ambiguous_candidates_never_invent_account_numbers():
    store_module = importlib.import_module(f"{PACKAGE}.store")
    crypto = importlib.import_module(f"{PACKAGE}.crypto")
    commands = importlib.import_module(f"{PACKAGE}.commands")
    with tempfile.TemporaryDirectory() as scratch:
        store = store_module.ArknightsStore(Path(scratch) / "synthetic.db")
        try:
            store.bind_roles("qa", "synthetic-token", [
                store_module.RoleCandidate("10000001", "1", "其他角色"),
                store_module.RoleCandidate("10001234", "1", "相同后缀甲"),
                store_module.RoleCandidate("20001234", "2", "相同后缀乙"),
            ], crypto.ArknightsCipher(bytes(range(32))))
            resolution = store.resolve("qa", "1234")
            assert resolution.reason == store_module.AMBIGUOUS
            text = commands.format_selector_failure(resolution)
            assert "1. 相同后缀甲" not in text
            assert "2. 相同后缀乙" not in text
            assert "- 相同后缀甲" in text
            assert "/ak 账号" in text
            assert store.resolve("qa", "1").role.nickname == "其他角色"
            assert "10001234" not in text
        finally:
            store.close()


def test_report_timestamp_uses_shanghai_time_independent_of_host_timezone():
    assert models.format_local_timestamp(0) == "1970-01-01 08:00"
    # UTC 16:00 crosses the Shanghai day boundary.
    assert models.format_local_timestamp(16 * 3600) == "1970-01-02 00:00"
