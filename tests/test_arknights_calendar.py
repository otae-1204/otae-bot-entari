"""Official cumulative sign-in (合成玉 milestones) and monthly total.

Everything here is synthetic: ``httpx.MockTransport`` for the protocol, fixed
UTC+8 timestamps for the month arithmetic, and fabricated UIDs.  No account,
credential or network call is involved.

The rules under test come from the official sign-in page bundles kept in
``output/arknights-protocol/tools/``:

* ``dist-BZImVwlH.js`` — ``calendar.filter(e => e.type === "daily")`` then
  ``.map((e, i) => ({...e, day: i + 1}))``, so a cumulative day is the 1-based
  position **among daily slots only**.
* ``signIn-CDDzUwtl.js`` — ``checkInCount`` = ``calendar.filter(e => e.done).length``
  and ``isTodayChecked`` = ``!calendar.find(e => e.available)``.

The sample reward numbers below are synthetic data used to prove the parser
follows the payload; they are not a reward promise.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "arknights_calendar_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/arknights")]
    sys.modules[PACKAGE] = package

crypto = _load(f"{PACKAGE}.crypto", "plugins/arknights/crypto.py")
store_module = _load(f"{PACKAGE}.store", "plugins/arknights/store.py")
client_module = _load(f"{PACKAGE}.client", "plugins/arknights/client.py")
models_module = _load(f"{PACKAGE}.models", "plugins/arknights/models.py")


SHANGHAI = timezone(timedelta(hours=8))
ORIGINIUM_ID = "4003"
OTHER_ID = "2002"
INFO = {
    ORIGINIUM_ID: {"id": ORIGINIUM_ID, "name": "合成玉", "type": "DIAMOND_SHD"},
    OTHER_ID: {"id": OTHER_ID, "name": "龙门币", "type": "GOLD"},
}


def sh_ts(year: int, month: int, day: int, hour: int = 12) -> int:
    """A UTC+8 wall-clock instant expressed as an epoch second."""
    return int(datetime(year, month, day, hour, tzinfo=SHANGHAI).timestamp())


def today_shanghai_ts() -> int:
    now = datetime.now(SHANGHAI)
    return sh_ts(now.year, now.month, now.day, 12)


def slot(resource_id: str, *, kind: str = "daily", count=100, available=False, done=False):
    return {
        "resourceId": resource_id,
        "type": kind,
        "count": count,
        "available": available,
        "done": done,
    }


def daily_originium(*, count=100, available=False, done=False):
    return slot(ORIGINIUM_ID, count=count, available=available, done=done)


def status_payload(*, records=None, calendar=None, info=None, current_ts=None) -> dict:
    data: dict = {}
    if records is not None:
        data["records"] = records
    if calendar is not None:
        data["calendar"] = calendar
    if info is not None:
        data["resourceInfoMap"] = info
    if current_ts is not None:
        data["currentTs"] = str(current_ts)
    return {"code": 0, "data": data}


def progress_calendar(*done_flags, count=100):
    """Daily 合成玉 slots; the first unclaimed slot is the claimable one.

    Mirrors the official rule that only today's slot can be ``available``, which
    is what makes ``isTodayChecked`` false before signing in.
    """
    flags = list(done_flags)
    try:
        claimable = flags.index(False)
    except ValueError:
        claimable = -1
    return [
        daily_originium(count=count, done=flag, available=index == claimable)
        for index, flag in enumerate(flags)
    ]


# ------------------------------------------------------------ milestone parse


class MilestoneParseTests(unittest.TestCase):
    def test_no_calendar_or_no_originium_produces_no_nodes(self):
        cases = {
            "payload_none": None,
            "payload_empty": {},
            "no_data": {"code": 0},
            "calendar_empty": status_payload(calendar=[], info=INFO),
            "calendar_string": status_payload(calendar="nope", info=INFO),
            "no_daily_entries": status_payload(
                calendar=[slot(ORIGINIUM_ID, kind="first")], info=INFO
            ),
            "other_reward_only": status_payload(calendar=[slot(OTHER_ID)], info=INFO),
            "missing_resource_map": status_payload(calendar=[daily_originium()]),
            "resource_map_pointing_elsewhere": status_payload(
                calendar=[daily_originium()], info={ORIGINIUM_ID: {"name": "龙门币"}}
            ),
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                self.assertEqual(client_module.parse_attendance_milestones(payload), ())

    def test_unknown_calendar_entry_refuses_to_number(self):
        # A non-mapping entry could have been a daily slot, so numbering after
        # it would be wrong: produce nothing rather than guess.
        payload = status_payload(
            calendar=[daily_originium(count=100), "junk", daily_originium(count=200)],
            info=INFO,
        )
        self.assertEqual(client_module.parse_attendance_milestones(payload), ())

    def test_daily_numbering_ignores_first_and_activity(self):
        payload = status_payload(
            calendar=[
                slot(ORIGINIUM_ID, kind="first", count=500, done=True),
                daily_originium(count=100, done=True),
                slot(OTHER_ID, count=5, done=True),
                slot("9999", kind="activity", count=1),
                daily_originium(count=200, available=True),
            ],
            info=INFO,
        )
        nodes = client_module.parse_attendance_milestones(payload)
        # first/activity never enter the ladder, so days 1 and 3 are the first
        # and third daily slots -- not their raw calendar positions (2 and 5).
        self.assertEqual([node.day for node in nodes], [1, 3])
        self.assertEqual([node.count for node in nodes], [100, 200])
        self.assertEqual([node.name for node in nodes], ["合成玉", "合成玉"])
        self.assertEqual(
            [(node.done, node.available) for node in nodes], [(True, False), (False, True)]
        )

    def test_skipped_daily_slots_keep_their_position(self):
        payload = status_payload(
            calendar=[
                daily_originium(count=100, done=True),  # day 1 -> kept
                daily_originium(count=None),            # day 2 -> unusable count
                daily_originium(count="500"),           # day 3 -> not an integer field
                slot(OTHER_ID, count=5),                # day 4 -> other reward
                daily_originium(count=300, done=True),  # day 5 -> kept
            ],
            info=INFO,
        )
        nodes = client_module.parse_attendance_milestones(payload)
        self.assertEqual([(node.day, node.count) for node in nodes], [(1, 100), (5, 300)])

    def test_non_positive_or_non_boolean_fields_are_dropped(self):
        for bad in (
            {"count": 0},
            {"count": -5},
            {"count": True},
            {"count": 100, "done": 1},
            {"count": 100, "available": "true"},
            {"count": 100, "done": None},
        ):
            entry = slot(ORIGINIUM_ID, count=100)
            entry.update(bad)
            with self.subTest(bad=bad):
                payload = status_payload(calendar=[entry], info=INFO)
                self.assertEqual(client_module.parse_attendance_milestones(payload), ())

    def test_counts_come_from_the_payload_not_a_table(self):
        # The same ladder positions with different server counts must follow the
        # server, which a hardcoded 5/10/15/25/30 table could not do.
        low = status_payload(calendar=[daily_originium(count=111)], info=INFO)
        high = status_payload(calendar=[daily_originium(count=222)], info=INFO)
        self.assertEqual(
            [node.count for node in client_module.parse_attendance_milestones(low)], [111]
        )
        self.assertEqual(
            [node.count for node in client_module.parse_attendance_milestones(high)], [222]
        )

        ladder = status_payload(
            calendar=[daily_originium(count=100 + index) for index in range(30)], info=INFO
        )
        nodes = client_module.parse_attendance_milestones(ladder)
        self.assertEqual([node.day for node in nodes], list(range(1, 31)))
        self.assertEqual([node.count for node in nodes], [100 + index for index in range(30)])

    def test_names_use_resource_info_map(self):
        payload = status_payload(
            calendar=[daily_originium(count=100)],
            info={ORIGINIUM_ID: {"name": {"zh-CN": "合成玉", "en": "Orundum"}}},
        )
        nodes = client_module.parse_attendance_milestones(payload)
        self.assertEqual([node.name for node in nodes], ["合成玉"])


# --------------------------------------------------------------- month total


class MonthlyCheckinCountTests(unittest.TestCase):
    def test_daily_calendar_done_wins_over_records(self):
        payload = status_payload(
            current_ts=sh_ts(2026, 3, 20),
            calendar=[
                slot(ORIGINIUM_ID, kind="first", count=500, done=True),
                daily_originium(count=100, done=True),
                slot(OTHER_ID, count=5, done=True),
                daily_originium(count=200, available=True),
            ],
            info=INFO,
            records=[
                {"ts": str(sh_ts(2026, 3, 3))},
                {"ts": str(sh_ts(2026, 3, 4))},
                {"ts": str(sh_ts(2026, 3, 5))},
            ],
        )
        # Only the two done daily slots count; the first bonus and the history
        # records (which may include first/activity grants) must not override.
        self.assertEqual(client_module.monthly_checkin_count(payload), 2)

    def test_records_are_the_fallback_when_the_calendar_is_unusable(self):
        base = {
            "current_ts": sh_ts(2026, 3, 20),
            "records": [
                {"ts": str(sh_ts(2026, 3, 5))},
                {"ts": str(sh_ts(2026, 3, 5, 23))},
                {"ts": str(sh_ts(2026, 3, 7))},
                {"ts": str(sh_ts(2026, 2, 28))},
            ],
        }
        for calendar in (
            None,                                    # no calendar
            [],                                      # empty calendar
            [slot(ORIGINIUM_ID, kind="first")],      # no daily entry at all
            [daily_originium(), "junk"],             # unreliable numbering
        ):
            with self.subTest(calendar=calendar):
                payload = status_payload(calendar=calendar, **base)
                self.assertEqual(client_module.monthly_checkin_count(payload), 2)

    def test_valid_calendar_without_done_slots_is_a_real_zero(self):
        payload = status_payload(
            current_ts=sh_ts(2026, 3, 20),
            calendar=[daily_originium(count=100, available=True)],
            info=INFO,
            records=[{"ts": str(sh_ts(2026, 3, 5))}],
        )
        self.assertEqual(client_module.monthly_checkin_count(payload), 0)

    def test_month_boundary_is_utc_plus_8_not_utc(self):
        march_last = sh_ts(2026, 3, 31, 23)
        april_first = sh_ts(2026, 4, 1, 0)
        self.assertNotEqual(
            client_module.shanghai_month(datetime.fromtimestamp(march_last, tz=timezone.utc)),
            client_module.shanghai_month(datetime.fromtimestamp(april_first, tz=timezone.utc)),
        )
        payload = status_payload(
            current_ts=march_last,
            records=[{"ts": str(march_last)}, {"ts": str(april_first)}],
        )
        self.assertEqual(client_module.monthly_checkin_count(payload), 1)

    def test_uses_the_local_month_when_current_ts_is_missing(self):
        payload = status_payload(
            records=[{"ts": str(sh_ts(2026, 4, 10))}, {"ts": str(sh_ts(2026, 3, 31))}]
        )
        now = datetime(2026, 4, 20, 6, tzinfo=timezone.utc)
        self.assertEqual(client_module.monthly_checkin_count(payload, now=now), 1)

    def test_empty_records_is_a_real_zero_and_malformed_is_none(self):
        self.assertEqual(
            client_module.monthly_checkin_count(
                status_payload(current_ts=sh_ts(2026, 3, 20), records=[])
            ),
            0,
        )
        self.assertIsNone(client_module.monthly_checkin_count(None))
        self.assertIsNone(client_module.monthly_checkin_count({}))
        self.assertIsNone(client_module.monthly_checkin_count({"data": None}))
        self.assertIsNone(client_module.monthly_checkin_count({"code": 0, "data": {}}))
        for malformed in ([{"ts": None}, "junk", {}], [{"ts": "abc"}], [{"ts": -5}], ["junk"]):
            with self.subTest(malformed=malformed):
                self.assertIsNone(
                    client_module.monthly_checkin_count(
                        status_payload(current_ts=sh_ts(2026, 3, 20), records=malformed)
                    )
                )


# ---------------------------------------------------------- has-signed-today


class AttendanceHasTodayTests(unittest.TestCase):
    def test_available_daily_slot_means_not_signed_yet(self):
        payload = status_payload(
            calendar=[daily_originium(done=True), daily_originium(available=True)], info=INFO
        )
        self.assertIs(client_module.attendance_has_today(payload), False)

    def test_no_available_daily_slot_means_signed(self):
        payload = status_payload(
            calendar=[daily_originium(done=True), daily_originium()], info=INFO
        )
        self.assertIs(client_module.attendance_has_today(payload), True)

    def test_empty_or_absent_daily_never_implies_signed(self):
        # An empty calendar must not read as "nothing left to claim".
        self.assertIs(
            client_module.attendance_has_today(status_payload(calendar=[], records=[])), False
        )
        only_first = status_payload(calendar=[slot(ORIGINIUM_ID, kind="first")])
        self.assertIsNone(client_module.attendance_has_today(only_first))
        untrusted = status_payload(calendar=[daily_originium(), "junk"])
        self.assertIsNone(client_module.attendance_has_today(untrusted))

    def test_records_fallback_still_applies(self):
        today = today_shanghai_ts()
        self.assertIs(
            client_module.attendance_has_today(status_payload(records=[{"ts": str(today)}])),
            True,
        )
        self.assertIs(client_module.attendance_has_today(status_payload(records=[])), False)
        self.assertIsNone(client_module.attendance_has_today({}))

    def test_untrustworthy_calendar_falls_back_to_records(self):
        today = today_shanghai_ts()
        payload = status_payload(
            calendar=[daily_originium(available=True), "junk"],
            records=[{"ts": str(today)}],
        )
        self.assertIs(client_module.attendance_has_today(payload), True)

    def test_hastoday_flag_is_still_honoured(self):
        self.assertIs(client_module.attendance_has_today({"data": {"hasToday": True}}), True)
        self.assertIs(client_module.attendance_has_today({"data": {"hasToday": False}}), False)


# --------------------------------------------------------------------- flow


AWARDS_DATA = {
    "ts": "1693823939",
    "awards": [
        {"resource": {"id": ORIGINIUM_ID, "name": "合成玉", "type": "DIAMOND_SHD"}, "count": 500},
        {"resource": {"id": OTHER_ID, "name": "初级作战记录", "type": "CARD_EXP"}, "count": 3},
    ],
}


class MockSkland:
    """MockTransport handler that records the attendance request order.

    ``statuses`` holds one entry per successive attendance GET; an entry may be
    a payload dict or an ``httpx.Response`` (for failures).  The final entry is
    reused if more reads happen than expected.
    """

    def __init__(self, statuses, *, post=None):
        self.statuses = list(statuses)
        self.post = post if post is not None else {"code": 0, "data": AWARDS_DATA}
        self.order: list[tuple[str, str]] = []
        self.status_reads = 0
        self.posts = 0

    async def __call__(self, request: httpx.Request):
        self.order.append((request.method, request.url.path))
        if request.url.host == "as.hypergryph.com":
            return httpx.Response(200, json={"status": 0, "data": {"code": "oauth-code"}})
        if request.url.path.endswith("/user/auth/generate_cred_by_code"):
            return httpx.Response(
                200, json={"code": 0, "data": {"cred": "cred", "token": "sign-token"}}
            )
        if request.url.path == client_module.ATTENDANCE_PATH:
            if request.method == "GET":
                response = self.statuses[min(self.status_reads, len(self.statuses) - 1)]
                self.status_reads += 1
                if isinstance(response, httpx.Response):
                    return response
                return httpx.Response(200, json=response)
            self.posts += 1
            if isinstance(self.post, httpx.Response):
                return self.post
            return httpx.Response(200, json=self.post)
        raise AssertionError(str(request.url))


ROLE = SimpleNamespace(uid="10001234", game_id="1")


class AttendanceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, handler: MockSkland):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        try:
            return await client.attendance("account-token", ROLE)
        finally:
            await http.aclose()

    async def test_already_today_reuses_the_first_read(self):
        # Every daily slot is claimed -> official isTodayChecked is true.
        handler = MockSkland(
            [status_payload(calendar=[daily_originium(done=True)], info=INFO)]
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "already")
        self.assertEqual(result.monthly_count, 1)
        self.assertEqual(result.daily_progress, (True,))
        self.assertEqual([(m.day, m.done) for m in result.milestones], [(1, True)])
        self.assertEqual(handler.status_reads, 1)
        self.assertEqual(handler.posts, 0)

    async def test_success_re_reads_status_after_posting(self):
        today = today_shanghai_ts()
        handler = MockSkland(
            [
                # A claimable slot means "not signed yet" -> POST.
                status_payload(
                    calendar=progress_calendar(True, False, False),
                    info=INFO,
                    current_ts=today,
                ),
                # After the POST the second slot is claimed too.
                status_payload(
                    calendar=progress_calendar(True, True, False),
                    info=INFO,
                    current_ts=today,
                ),
            ]
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "success")
        self.assertEqual(
            [(item.name, item.count) for item in result.rewards],
            [("合成玉", 500), ("初级作战记录", 3)],
        )
        # The refreshed calendar is used, not the stale pre-POST one.
        self.assertEqual(result.monthly_count, 2)
        self.assertEqual(result.daily_progress, (True, True, False))
        self.assertEqual(
            [(m.day, m.done) for m in result.milestones],
            [(1, True), (2, True), (3, False)],
        )
        self.assertEqual(handler.posts, 1)
        post_index = handler.order.index(("POST", client_module.ATTENDANCE_PATH))
        reads = [
            index
            for index, (method, path) in enumerate(handler.order)
            if method == "GET" and path == client_module.ATTENDANCE_PATH
        ]
        self.assertEqual(len(reads), 2)
        self.assertLess(post_index, reads[1])

    async def test_post_read_failure_keeps_success_and_drops_stale_progress(self):
        today = today_shanghai_ts()
        handler = MockSkland(
            [
                # Pre-POST progress that must NOT be reused after the failure.
                status_payload(
                    calendar=progress_calendar(True, False), info=INFO, current_ts=today
                ),
                httpx.Response(503, text="busy"),
            ]
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.rewards), 2)
        self.assertIsNone(result.monthly_count)
        self.assertEqual(result.milestones, ())
        self.assertEqual(result.daily_progress, ())

    async def test_status_probe_failure_still_signs_in(self):
        handler = MockSkland(
            [httpx.Response(503, text="busy"), httpx.Response(503, text="busy")]
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "success")
        self.assertIsNone(result.monthly_count)
        self.assertEqual(result.milestones, ())
        self.assertEqual(handler.posts, 1)

    async def test_duplicate_post_is_already_and_backfills_progress(self):
        today = today_shanghai_ts()
        handler = MockSkland(
            [
                status_payload(
                    calendar=progress_calendar(True, False), info=INFO, current_ts=today
                ),
                status_payload(
                    calendar=progress_calendar(True, True), info=INFO, current_ts=today
                ),
            ],
            post=httpx.Response(403, json={"code": 10001, "message": "今天已经签到过了"}),
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "already")
        self.assertEqual(result.monthly_count, 2)
        self.assertEqual(result.daily_progress, (True, True))
        self.assertEqual([(m.day, m.done) for m in result.milestones], [(1, True), (2, True)])
        self.assertEqual(handler.posts, 1)
        self.assertEqual(handler.status_reads, 2)
        self.assertNotIn("已经签到过了", result.message)

    async def test_duplicate_post_with_failing_backfill_is_still_already(self):
        today = today_shanghai_ts()
        handler = MockSkland(
            [
                status_payload(
                    calendar=progress_calendar(True, False), info=INFO, current_ts=today
                ),
                httpx.Response(500, text="boom"),
            ],
            post=httpx.Response(403, json={"code": 10001}),
        )
        result = await self._run(handler)

        self.assertEqual(result.status, "already")
        self.assertIsNone(result.monthly_count)
        self.assertEqual(result.milestones, ())

    async def test_credential_failure_is_not_already_and_has_no_progress(self):
        handler = MockSkland(
            [httpx.Response(200, json={"code": 401, "message": "登录失效"})],
            post=httpx.Response(200, json={"code": 401, "message": "登录失效"}),
        )
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await self._run(handler)
        self.assertFalse(caught.exception.already_signed)
        # One rejected POST per context: the original and the single refreshed one.
        self.assertEqual(handler.posts, 2)
        self.assertIn("请重新私聊使用 /ak 绑定", str(caught.exception))


# -------------------------------------------------------------------- views


class MilestoneViewTests(unittest.TestCase):
    def _role(self):
        return store_module.ArknightsRole(
            id=1,
            credential_id=1,
            qq_user_id="7",
            uid="10001234",
            game_id="1",
            channel_name="官服",
            nickname="甲",
            is_primary=True,
        )

    def _report(self, view) -> str:
        card = models_module.AttendanceCardView(roles=(view,), generated_at="")
        return models_module.format_attendance_report(card)

    def test_result_carries_progress_into_the_view(self):
        result = client_module.AttendanceResult(
            "success",
            "签到成功",
            (client_module.AttendanceReward("合成玉", 500),),
            12,
            (
                client_module.AttendanceMilestone(7, "合成玉", 200, True, True),
                client_module.AttendanceMilestone(14, "合成玉", 500, False, True),
                client_module.AttendanceMilestone(30, "合成玉", 1000, False, False),
            ),
        )
        view = models_module.build_attendance_view(self._role(), result)
        self.assertEqual(view.monthly_count, 12)
        self.assertEqual([milestone.day for milestone in view.milestones], [7, 14, 30])
        text = self._report(view)
        self.assertIn("本月累签 12 天", text)
        self.assertIn("累计第 7 天：合成玉 × 200（已领取）", text)
        self.assertIn("累计第 14 天：合成玉 × 500（可领取）", text)
        self.assertIn("累计第 30 天：合成玉 × 1000（未达成）", text)

    def test_done_wins_over_available(self):
        view = models_module.AttendanceMilestoneView(
            day=5, name="合成玉", count=100, done=True, available=True
        )
        self.assertEqual(view.state_label, "已领取")

    def test_missing_progress_stays_hidden(self):
        result = client_module.AttendanceResult("success", "签到成功", ())
        self.assertIsNone(result.monthly_count)
        self.assertEqual(result.milestones, ())
        view = models_module.build_attendance_view(self._role(), result)
        self.assertIsNone(view.monthly_count)
        self.assertEqual(view.milestones, ())
        text = self._report(view)
        self.assertNotIn("本月累签", text)
        self.assertNotIn("累计第", text)

    def test_results_without_the_new_fields_still_build(self):
        class LegacyResult:
            status = "already"
            message = "今日已签到"
            rewards = ()

        view = models_module.build_attendance_view(self._role(), LegacyResult())
        self.assertEqual(view.status, "already")
        self.assertIsNone(view.monthly_count)
        self.assertEqual(view.milestones, ())

    def test_normalizers_reject_garbage(self):
        normalize = models_module.monthly_count_value
        for rejected in (None, True, False, -1, "abc", [], {}):
            with self.subTest(rejected=rejected):
                self.assertIsNone(normalize(rejected))
        self.assertEqual(normalize(0), 0)
        self.assertIsNone(normalize("4"))

        normalized = models_module.milestone_views(
            [
                SimpleNamespace(day=0, name="合成玉", count=100),
                SimpleNamespace(day=1, name="", count=100),
                SimpleNamespace(day=2, name="合成玉", count=0),
                SimpleNamespace(day=3, name="合成玉", count=100, done=False, available=True),
            ]
        )
        self.assertEqual(
            normalized,
            (models_module.AttendanceMilestoneView(3, "合成玉", 100, False, True),),
        )

    def test_failed_rows_never_show_progress(self):
        view = models_module.AttendanceRoleView(
            nickname="甲",
            uid="****1234",
            status="failed",
            message="网络请求失败",
            monthly_count=9,
            milestones=(
                models_module.AttendanceMilestoneView(7, "合成玉", 200, False, True),
            ),
        )
        text = self._report(view)
        self.assertIn("签到失败", text)
        self.assertNotIn("本月累签", text)
        self.assertNotIn("累计第", text)

    def test_already_row_shows_progress(self):
        view = models_module.AttendanceRoleView(
            nickname="甲",
            uid="****1234",
            status="already",
            monthly_count=3,
            milestones=(
                models_module.AttendanceMilestoneView(3, "合成玉", 100, True, False),
            ),
        )
        text = self._report(view)
        self.assertIn("今日已签到，无需重复签到", text)
        self.assertIn("本月累签 3 天", text)
        self.assertIn("累计第 3 天：合成玉 × 100（已领取）", text)


class RawCalendarViewTests(unittest.TestCase):
    """``attendance_calendar_days`` is the raw entry view, not a day number."""

    def test_raw_order_is_kept_and_names_resolved(self):
        payload = status_payload(
            calendar=[
                slot(ORIGINIUM_ID, kind="first", count=500),
                daily_originium(count=100),
                slot(OTHER_ID, count=5),
            ],
            info=INFO,
        )
        days = client_module.attendance_calendar_days(payload)
        self.assertEqual([day.index for day in days], [0, 1, 2])
        self.assertEqual([day.name for day in days], ["合成玉", "合成玉", "龙门币"])
        self.assertEqual([day.reward_type for day in days], ["DIAMOND_SHD", "DIAMOND_SHD", "GOLD"])

    def test_missing_calendar_is_empty(self):
        self.assertEqual(client_module.attendance_calendar_days(None), ())
        self.assertEqual(client_module.attendance_calendar_days({"data": {}}), ())
        self.assertEqual(client_module.attendance_calendar_days(status_payload(calendar="x")), ())


class ProgressProvenanceTests(unittest.TestCase):
    """Progress must come from the official calendar, never a local table."""

    def test_no_progress_without_server_data(self):
        self.assertIsNone(client_module.monthly_checkin_count(None))
        self.assertIsNone(client_module.monthly_checkin_count({"code": 0, "data": {}}))
        self.assertEqual(client_module.parse_attendance_milestones(None), ())
        self.assertEqual(client_module.parse_attendance_milestones({"data": {}}), ())

    def test_first_and_activity_are_never_progress(self):
        payload = status_payload(
            calendar=[
                slot(ORIGINIUM_ID, kind="first", count=500, done=True),
                slot(ORIGINIUM_ID, kind="activity", count=50, done=True),
            ],
            info=INFO,
        )
        self.assertEqual(client_module.parse_attendance_milestones(payload), ())
        self.assertIsNone(client_module.monthly_checkin_count(payload))


if __name__ == "__main__":
    unittest.main()


class FinalProtocolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_malformed_time_and_monthly_values_are_nonfatal(self):
        for value in (10**100, float("inf"), float("nan"), -10**100):
            payload = {"data": {"currentTs": value, "records": [{"ts": value}]}}
            self.assertFalse(client_module.attendance_has_today(payload))
            self.assertIsNone(client_module.monthly_checkin_count(payload))
        for value in (32, 1.5, 4.0, "4", float("inf"), float("nan"), True):
            self.assertIsNone(models_module.monthly_count_value(value))
        self.assertEqual(models_module.monthly_count_value(31), 31)

    def test_malformed_daily_done_uses_records_or_unknown(self):
        for malformed in ({"type": "daily"}, {"type": "daily", "done": "false"}):
            self.assertIsNone(client_module.monthly_checkin_count({"data": {"calendar": [malformed]}}))
            payload = {"data": {"calendar": [malformed], "currentTs": sh_ts(2026, 10, 8),
                                "records": [{"ts": sh_ts(2026, 10, 7)}]}}
            self.assertEqual(client_module.monthly_checkin_count(payload), 1)

    async def test_successful_post_survives_malformed_status_timestamps(self):
        service = MockSkland([{"code": 0, "data": {"records": [], "hasToday": False}},
                             {"code": 0, "data": {"currentTs": 10**100,
                                                    "records": [{"ts": 10**100}]}}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as http:
            client = client_module.ArknightsClient(http)
            result = await client.attendance("synthetic", SimpleNamespace(uid="1234", game_id="1"))
        self.assertEqual(result.status, "success")
        self.assertIsNone(result.monthly_count)
        self.assertEqual(service.posts, 1)

    async def test_bilibili_binding_uses_game_id_not_channel_master_id(self):
        import json
        bindings = {"data": {"list": [{"appCode": "arknights", "bindingList": [
            {"uid": "20005678", "channelMasterId": "2", "gameId": 1, "channelName": "B服"},
            {"uid": "20009999", "channelMasterId": "2", "channelName": "B服"},
        ]}]}}
        roles = client_module.parse_arknights_bindings(bindings)
        self.assertEqual([role.game_id for role in roles], ["1", "1"])
        captured = []
        service = MockSkland([{"code": 0, "data": {"records": [], "hasToday": False}}])
        async def transport(request):
            captured.append(request)
            return await service(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            result = await client_module.ArknightsClient(http).attendance("synthetic", roles[0])
        self.assertEqual(result.status, "success")
        for request in captured:
            if request.url.path != client_module.ATTENDANCE_PATH:
                continue
            if request.method == "GET":
                self.assertEqual(request.url.params["gameId"], "1")
            else:
                self.assertEqual(json.loads(request.content)["gameId"], "1")
