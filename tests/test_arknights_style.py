"""Optional monthly attendance display uses real supplied data only."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
from dataclasses import replace
from pathlib import Path

from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "arknights_style_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/arknights")]
sys.modules[PACKAGE] = package
importlib.import_module("otae_bot.config.paths")
_previous_cwd = Path.cwd()
with tempfile.TemporaryDirectory() as scratch:
    try:
        os.chdir(scratch)
        models = importlib.import_module(f"{PACKAGE}.models")
        cards = importlib.import_module(f"{PACKAGE}.rendering.cards")
    finally:
        os.chdir(_previous_cwd)


def _role(**extra):
    return types.SimpleNamespace(nickname="合成博士", uid="****1234", server_label="官服",
                                 status="already", status_label="今日已签到", message="",
                                 rewards=(), **extra)


def test_monthly_count_is_shown_only_when_known_and_valid():
    for count in (0, 8, 31):
        html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(_role(monthly_count=count),)))
        assert f'<b>{count} 天</b>' in html
        assert '<div class="attendance-meta">' in html
    for extra in ({}, {"monthly_count": None}, {"monthly_count": -1}, {"monthly_count": 32},
                  {"monthly_count": True}, {"monthly_count": '<img src="bad">'}):
        html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(_role(**extra),)))
        assert '<div class="attendance-meta">' not in html
        assert '<img' not in html


def test_milestone_state_priority_and_missing_data():
    milestone = models.AttendanceMilestoneView
    role = models.AttendanceRoleView(
        nickname="合成博士", uid="****1234", monthly_count=8,
        milestones=(milestone(day=4, name="合成玉", count=100, done=True, available=True),
                    milestone(day=9, name="合成玉", count=200, done=False, available=False)),
    )
    html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,)))
    assert 'class="milestone done"' in html
    assert 'class="milestone available"' not in html
    assert "累计第 9 天" in html
    assert "距离下次奖励还需 1 次签到" in html
    assert "本月 9 日" not in html
    missing = models.AttendanceRoleView(nickname="合成博士", uid="****1234")
    html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(missing,)))
    assert '<div class="milestones">' not in html


def test_available_and_completed_milestones_do_not_show_fake_distance():
    milestone = models.AttendanceMilestoneView
    for done, available, expected in ((False, True, "有合成玉奖励可领取"),
                                      (True, False, "本月合成玉奖励已全部领取")):
        role = models.AttendanceRoleView(nickname="合成博士", uid="****1234", monthly_count=8,
            milestones=(milestone(day=9, name="合成玉", count=200, done=done, available=available),))
        html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,)))
        assert expected in html
        assert "距离下次" not in html


def test_missing_monthly_count_and_untrusted_milestone_name():
    role = models.AttendanceRoleView(nickname="合成博士", uid="****1234", milestones=(
        models.AttendanceMilestoneView(day=9, name='<img src="https://bad.invalid/x">',
                                       count=0, done=False, available=False),))
    html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,)))
    assert "距离下次" not in html
    assert "× 0" not in html
    assert "<img" not in html
    assert "https://" not in html
    assert "&lt;img" in html


def test_client_milestone_contract_reaches_the_rendered_card():
    client = importlib.import_module(f"{PACKAGE}.client")
    result = client.AttendanceResult(status="already", message="今日已签到", monthly_count=8,
        milestones=(client.AttendanceMilestone(day=9, name="合成玉", count=200,
                                               done=False, available=False),))
    role = types.SimpleNamespace(nickname="合成博士", uid="10001234", masked_uid="****1234",
                                 channel_name="官服")
    view_role = models.build_attendance_view(role, result)
    html = cards.render_attendance_card_html(models.AttendanceCardView(roles=(view_role,)))
    assert "累计第 9 天" in html
    assert "合成玉 × 200" in html
    assert "8 天" in html


def test_daily_track_preserves_server_slots_including_non_prefix_progress():
    client = importlib.import_module(f"{PACKAGE}.client")
    payload = {"data": {"calendar": [
        {"type": "first", "done": True},
        {"type": "daily", "done": False},
        {"type": "daily", "done": True},
        {"type": "activity", "done": True},
        {"type": "daily", "done": False},
    ]}}
    progress = client.daily_checkin_progress(payload)
    assert progress == (False, True, False)
    result = client.AttendanceResult(
        status="already", message="今日已签到", monthly_count=1, daily_progress=progress,
        milestones=(client.AttendanceMilestone(2, "合成玉", 80, True, False),
                    client.AttendanceMilestone(3, "合成玉", 100, False, False)),
    )
    role = models.build_attendance_view(types.SimpleNamespace(nickname="博士", masked_uid="****1234"), result)
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,))))
    cells = document.find_class("milestone-cell")
    assert len(cells) == 3
    assert ["done" in cell.classes for cell in cells] == [False, True, False]
    summary = document.find_class("milestone-summary")[0].text_content()
    assert "80/ 180" in summary
    assert "还需签到 2 天" in document.text_content()


def test_track_keeps_claimed_available_and_pending_states_distinct():
    milestone = models.AttendanceMilestoneView
    role = models.AttendanceRoleView(
        nickname="博士", uid="****1234", monthly_count=1, daily_progress=(True, False, False),
        milestones=(milestone(1, "合成玉", 80, True, True),
                    milestone(2, "合成玉", 100, False, True),
                    milestone(3, "合成玉", 120, False, False)),
    )
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,))))
    nodes = document.find_class("milestone-node")
    assert sum("done" in node.classes for node in nodes) == 1
    assert sum("available" in node.classes for node in nodes) == 1
    assert "可领取" in document.find_class("milestone-next")[0].text_content()
    assert "还需签到" not in document.text_content()
    completed = replace(role, daily_progress=(True, True, True), monthly_count=3,
                        milestones=tuple(replace(item, done=True) for item in role.milestones))
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(completed,))))
    assert "已全部领取" in document.text_content()
    assert "300/ 300" in document.find_class("milestone-summary")[0].text_content()
    failed = replace(role, status="failed")
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(failed,))))
    assert not document.find_class("attendance-milestones")
    assert not document.find_class("milestones")


def test_invalid_or_missing_daily_calendar_never_invents_a_full_track():
    client = importlib.import_module(f"{PACKAGE}.client")
    for calendar in (None, [], [None], [{"type": "first", "done": True}],
                     [{"type": "daily", "done": 1}], [{"type": "daily", "done": True}] * 32):
        assert client.daily_checkin_progress({"data": {"calendar": calendar}}) == ()
    for progress in (None, [], "true", [1], [True] * 32):
        assert models.daily_progress_value(progress) == ()
    role = models.AttendanceRoleView(nickname="博士", uid="****1234", monthly_count=8,
                                    milestones=(models.AttendanceMilestoneView(9, "合成玉", 100),))
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,))))
    assert not document.find_class("milestone-track")
    assert "累计第 9 天" in document.text_content()


def test_approved_icons_are_bundled_and_rendered_by_exact_reward_name():
    import base64
    import hashlib
    import json
    icons = importlib.import_module(f"{PACKAGE}.rendering.icons")
    manifest = json.loads((icons.ASSET_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["items"]) == 12
    for item in manifest["items"]:
        content = (icons.ASSET_DIR / item["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        assert icons.REWARD_ICON_IDS[item["name"]] == item["resource_id"]
        assert icons.reward_icon_url(item["name"]) == "data:image/png;base64," + base64.b64encode(content).decode("ascii")
    assert icons.reward_icon_url("../4003") == ""
    assert icons.reward_icon_url("https://example.com/4003.png") == ""
    role = models.AttendanceRoleView(nickname="测试", uid="****0001", status="success",
        rewards=tuple(models.AttendanceRewardView(name, 1) for name in icons.REWARD_ICON_IDS),
        daily_progress=(True, False),
        milestones=(models.AttendanceMilestoneView(2, "合成玉", 80),))
    document = lxml_html.fromstring(cards.render_attendance_card_html(models.AttendanceCardView(roles=(role,))))
    assert len(document.find_class("attendance-rewards")[0].findall('.//img')) == 12
    assert document.find_class("milestone-node")[0].find('img').get('alt') == "合成玉"
