from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from arclet.letoderea.exceptions import ExitState

import plugins.endfield.handlers as endfield
from plugins.endfield.catalog import commands as commands_module


class EndfieldCalendarCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_finish_stop_is_propagated_without_failure_reply(self):
        matcher = mock.AsyncMock()
        matcher.finish.side_effect = ExitState.stop
        command = commands_module.parse_command("日历")

        with mock.patch.object(
            endfield,
            "_render_current_version_calendar",
            new=mock.AsyncMock(return_value=b"\x89PNG\r\n\x1a\n"),
        ):
            with self.assertRaises(ExitState):
                await endfield._handle_command(matcher, None, command)

        matcher.finish.assert_awaited_once()


class EndfieldCurrencyLogCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_currency_finish_stop_is_propagated_without_fallback_error(self):
        matcher = mock.AsyncMock()
        matcher.finish.side_effect = ExitState.stop
        role = mock.Mock(masked_uid="****1234", nickname="测试角色", role_id="role")
        command = commands_module.ParsedEndfieldCommand("currency_log")
        empty_logs = {1: (), 2: (), 3: ()}

        with (
            mock.patch.object(endfield.account_store, "decrypt_token", return_value="token"),
            mock.patch.object(endfield, "decode_account_credential", return_value=(endfield.ACCOUNT_PROVIDER_CN, "raw")),
            mock.patch.object(endfield, "is_asia_role", return_value=False),
            mock.patch.object(endfield, "resolve_query_dates", return_value=(date(2026, 8, 1), date(2026, 8, 12))),
            mock.patch.object(endfield.official_client, "currency_logs", new=mock.AsyncMock(return_value=empty_logs)),
            mock.patch.object(endfield.account_store, "upsert_currency_logs", return_value=0),
            mock.patch.object(endfield.account_store, "list_currency_logs", return_value=empty_logs),
            mock.patch.object(endfield, "draw_currency_log_cards", new=mock.AsyncMock(return_value=(b"png",))),
        ):
            with self.assertRaises(ExitState):
                await endfield._render_account_currency(
                    matcher,
                    role,
                    command,
                    mock.Mock(),
                    group=True,
                )

        matcher.finish.assert_awaited_once()
