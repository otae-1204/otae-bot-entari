"""card/detail 60 s 进程内复用、社区换签排队指标、签到 POST 与日历 GET 串行。"""

from __future__ import annotations

import asyncio
import re
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from plugins.endfield import caches, handlers
from plugins.endfield.account import client as client_module
from plugins.endfield.account.client import (
    ACCOUNT_PROVIDER_SKPORT,
    EndfieldAPIError,
    EndfieldOfficialClient,
    account_credential_fingerprint,
    encode_account_credential,
)
from plugins.endfield.catalog.commands import parse_command
from plugins.endfield.gacha.service import ROLE_TASKS, TaskAlreadyRunning

ROLE_A = SimpleNamespace(role_id="r1", server_id="1")
ROLE_B = SimpleNamespace(role_id="r2", server_id="1")


def _detail(name: str = "管理员") -> dict:
    return {"base": {"name": name}}


def _logged_text(*loggers: Mock) -> str:
    return "\n".join(repr(call) for log in loggers for call in log.mock_calls)


def _client(**kwargs) -> tuple[EndfieldOfficialClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    return EndfieldOfficialClient(http, **kwargs), http


def _context(token: str = "cred") -> client_module._SklandContext:
    return client_module._SklandContext(token, token, 1000, 1000, float("inf"))


class _CacheTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Module-level cache, and tests share tokens and role ids: start empty.
        await handlers._CARD_DETAIL_CACHE.clear()
        persist = patch.object(handlers.ownership_stats_service, "persist_detail", AsyncMock())
        prefetch = patch.object(handlers, "prefetch_detail_assets")
        self.persist = persist.start()
        self.prefetch = prefetch.start()
        self.addCleanup(persist.stop)
        self.addCleanup(prefetch.stop)

    async def asyncTearDown(self):
        await handlers._CARD_DETAIL_CACHE.clear()

    async def drain_background(self):
        await asyncio.gather(*list(handlers._background_tasks), return_exceptions=True)


class CardDetailReuseTests(_CacheTestCase):
    async def test_four_queries_in_a_row_make_one_request(self):
        """账号 → 基建 → 养成 → 探索：第二至四次不再请求、各 ≤ 50 ms。"""
        detail = _detail()

        async def slow(_token, _role):
            await asyncio.sleep(0.2)
            return detail

        fetch = AsyncMock(side_effect=slow)
        before = await handlers._CARD_DETAIL_CACHE.stats()  # counters are process-wide
        with patch.object(handlers.official_client, "card_detail", fetch):
            elapsed = []
            for _ in range(4):
                started = time.perf_counter()
                self.assertIs(await handlers._card_detail_with_snapshot("token", ROLE_A), detail)
                elapsed.append(time.perf_counter() - started)
        await self.drain_background()
        self.assertEqual(fetch.await_count, 1)
        self.assertGreaterEqual(elapsed[0], 0.19)
        self.assertTrue(all(seconds < 0.05 for seconds in elapsed[1:]), elapsed)
        # The snapshot write and asset prefetch run once per fetched detail.
        self.persist.assert_awaited_once_with(ROLE_A, detail)
        self.prefetch.assert_called_once_with(detail, source="card_detail")
        after = await handlers._CARD_DETAIL_CACHE.stats()
        self.assertEqual(
            (after.entries, after.misses - before.misses, after.direct_hits - before.direct_hits),
            (1, 1, 3),
        )

    async def test_key_holds_a_credential_fingerprint_not_the_token(self):
        fetch = AsyncMock(side_effect=lambda token, _role: _detail(token))
        with patch.object(handlers.official_client, "card_detail", fetch):
            first = await handlers._card_detail_with_snapshot("token-a", ROLE_A)
            second = await handlers._card_detail_with_snapshot("token-b", ROLE_A)
            again = await handlers._card_detail_with_snapshot("token-a", ROLE_A)
        self.assertEqual(fetch.await_count, 2)  # a different credential never reuses
        self.assertEqual(second, _detail("token-b"))
        self.assertIs(again, first)
        keys = set(handlers._CARD_DETAIL_CACHE._entries)
        self.assertEqual(
            keys,
            {
                (account_credential_fingerprint("token-a"), "r1", "1"),
                (account_credential_fingerprint("token-b"), "r1", "1"),
            },
        )
        self.assertNotIn("token-a", repr(keys))

    def test_fingerprint_shape_and_normalisation(self):
        fingerprint = account_credential_fingerprint("tok")
        self.assertRegex(fingerprint, r"^[0-9a-f]{24}$")
        self.assertNotIn("tok", fingerprint)
        # A stored JSON credential and the bare token it wraps are the same account.
        self.assertEqual(account_credential_fingerprint(encode_account_credential("tok")), fingerprint)
        self.assertNotEqual(
            account_credential_fingerprint(encode_account_credential("tok", ACCOUNT_PROVIDER_SKPORT)),
            fingerprint,
        )
        with self.assertRaises(ValueError):
            account_credential_fingerprint("")

    async def test_roles_do_not_share_entries(self):
        fetch = AsyncMock(side_effect=lambda _token, role: _detail(role.role_id))
        with patch.object(handlers.official_client, "card_detail", fetch):
            first = await handlers._card_detail_with_snapshot("token", ROLE_A)
            second = await handlers._card_detail_with_snapshot("token", ROLE_B)
            self.assertEqual(await handlers._card_detail_with_snapshot("token", ROLE_A), first)
        self.assertEqual((first, second), (_detail("r1"), _detail("r2")))
        self.assertEqual(fetch.await_count, 2)

    async def test_entries_expire_after_sixty_seconds(self):
        now = [1000.0]
        fetch = AsyncMock(return_value=_detail())
        with (
            patch.object(handlers._CARD_DETAIL_CACHE, "_clock", lambda: now[0]),
            patch.object(handlers.official_client, "card_detail", fetch),
        ):
            await handlers._card_detail_with_snapshot("token", ROLE_A)
            now[0] += 59
            await handlers._card_detail_with_snapshot("token", ROLE_A)
            self.assertEqual(fetch.await_count, 1)
            now[0] += 2
            await handlers._card_detail_with_snapshot("token", ROLE_A)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(caches.CARD_DETAIL_CACHE_TTL_SECONDS, 60)

    async def test_concurrent_queries_share_one_request(self):
        release = asyncio.Event()

        async def blocked(_token, _role):
            await release.wait()
            return _detail()

        fetch = AsyncMock(side_effect=blocked)
        with patch.object(handlers.official_client, "card_detail", fetch):
            tasks = [
                asyncio.create_task(handlers._card_detail_with_snapshot("token", ROLE_A))
                for _ in range(2)
            ]
            await asyncio.sleep(0.01)
            release.set()
            first, second = await asyncio.gather(*tasks)
        await self.drain_background()
        self.assertIs(first, second)
        self.assertEqual(fetch.await_count, 1)
        self.persist.assert_awaited_once()  # only the request owner writes the snapshot
        self.prefetch.assert_called_once()

    async def test_a_hit_does_not_wait_for_the_role_lock(self):
        fetch = AsyncMock(return_value=_detail())
        with patch.object(handlers.official_client, "card_detail", fetch):
            await handlers._card_detail_with_snapshot("token", ROLE_A)
            async with ROLE_TASKS.claim(ROLE_A):  # e.g. a gacha sync is running
                detail = await asyncio.wait_for(
                    handlers._card_detail_with_snapshot("token", ROLE_A), 0.5
                )
        self.assertEqual(detail, _detail())
        self.assertEqual(fetch.await_count, 1)

    async def test_failures_reach_every_waiter_and_are_not_cached(self):
        fetch = AsyncMock(return_value=_detail())
        with patch.object(handlers.official_client, "card_detail", fetch):
            async with ROLE_TASKS.claim(ROLE_A):
                results = await asyncio.gather(
                    handlers._card_detail_with_snapshot("token", ROLE_A),
                    handlers._card_detail_with_snapshot("token", ROLE_A),
                    return_exceptions=True,
                )
            self.assertTrue(all(isinstance(item, TaskAlreadyRunning) for item in results), results)
            fetch.assert_not_awaited()
            self.assertEqual(await handlers._card_detail_with_snapshot("token", ROLE_A), _detail())
        self.assertEqual(fetch.await_count, 1)

        await handlers._CARD_DETAIL_CACHE.clear()
        error = EndfieldAPIError("查询终末地档案", "10000", "接口繁忙")

        async def failing_once(_token, _role):
            await asyncio.sleep(0.01)
            if fetch.await_count == 1:
                raise error
            return _detail("retry")

        fetch = AsyncMock(side_effect=failing_once)
        with patch.object(handlers.official_client, "card_detail", fetch):
            results = await asyncio.gather(
                handlers._card_detail_with_snapshot("token", ROLE_A),
                handlers._card_detail_with_snapshot("token", ROLE_A),
                return_exceptions=True,
            )
            self.assertEqual(results, [error, error])
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(
                await handlers._card_detail_with_snapshot("token", ROLE_A), _detail("retry")
            )
        self.assertEqual(fetch.await_count, 2)


class CardDetailCleanupTests(_CacheTestCase):
    async def fill(self, *roles):
        with patch.object(handlers.official_client, "card_detail", AsyncMock(return_value=_detail())):
            for role in roles:
                await handlers._card_detail_with_snapshot("token", role)

    async def test_every_dev_clear_scope_drops_personal_data(self):
        for scope in ("all", "icon", "operator", "weapon", "equipment", "stage"):
            with self.subTest(scope=scope):
                await self.fill(ROLE_A, ROLE_B)
                with (
                    patch.object(caches, "clear_http_cache", AsyncMock(return_value=0)),
                    patch.object(caches.service, "clear_query_caches", AsyncMock(return_value=0)),
                    patch.object(caches, "asset_broker", Mock()),
                ):
                    removed = await caches._clear_endfield_caches(scope)
                self.assertGreaterEqual(removed, 2)
                self.assertEqual((await caches._CARD_DETAIL_CACHE.stats()).entries, 0)

    async def test_unbind_forgets_only_that_role(self):
        await self.fill(ROLE_A, ROLE_B)
        store = Mock()
        store.unbind.return_value = ROLE_A
        session = AsyncMock()
        with (
            patch.object(handlers, "account_store", store),
            patch.object(handlers, "is_group", return_value=False),
            patch.object(handlers, "event_user_id", return_value="caller"),
        ):
            await handlers._handle_personal_command(session, SimpleNamespace(), parse_command("解绑 1"))
        store.unbind.assert_called_once_with("caller", "1")
        fetch = AsyncMock(return_value=_detail("new"))
        with patch.object(handlers.official_client, "card_detail", fetch):
            await handlers._card_detail_with_snapshot("token", ROLE_B)
            self.assertEqual(fetch.await_count, 0)  # the other role is still reused
            self.assertEqual(await handlers._card_detail_with_snapshot("token", ROLE_A), _detail("new"))
        self.assertEqual(fetch.await_count, 1)

    async def test_cleanup_empties_the_cache(self):
        await self.fill(ROLE_A)
        await self.drain_background()
        with (
            patch.object(handlers, "close_challenge_locale", AsyncMock()),
            patch.object(handlers.official_client, "close", AsyncMock()),
        ):
            await handlers._close_ownership_startup_task()
        self.assertEqual((await handlers._CARD_DETAIL_CACHE.stats()).entries, 0)

    async def test_status_shows_personal_data_and_community_queue_without_identities(self):
        await self.fill(ROLE_A)
        diagnostics = {
            "pools": {
                "assets": {"bytes": 0, "budget_bytes": 64 << 20},
                "i18n": {"bytes": 0, "budget_bytes": 64 << 20},
            },
            "namespaces": {},
        }
        metrics = {
            "cache_hits": 7, "singleflight_reuses": 0, "exchange_attempts": 3,
            "exchange_succeeded": 2, "exchange_failed": 1, "circuit_rejections": 0,
            "lock_waits": 2, "lock_wait_ms": 2500, "interval_waits": 1,
            "interval_wait_ms": 1900, "exchange_ms": 1236,
        }
        with (
            patch.object(caches, "get_http_cache_diagnostics", AsyncMock(return_value=diagnostics)),
            patch.object(caches.official_client, "community_context_metrics", return_value=metrics),
        ):
            lines = await caches._cache_status_lines()
        self.assertTrue(lines[6].startswith("账号数据: 1 项 / "), lines[6])
        self.assertIn(
            "社区换签: 命中 7 / 换签 3（失败 1，熔断拒绝 0）/ 等待锁 2 次 共 2.5s / "
            "等待间隔 1 次 共 1.9s / 换签耗时 1.2s",
            lines,
        )
        self.assertTrue(lines[-2].startswith("浏览器: "))
        self.assertTrue(lines[-1].startswith("缓存策略"))
        fingerprint = account_credential_fingerprint("token")
        self.assertFalse(any(fingerprint[:8] in line or "r1" in line for line in lines))


class CardDetailPrivacyTests(_CacheTestCase):
    async def test_no_log_line_carries_the_token_or_its_fingerprint(self):
        raw = "secret-token-xyz"
        token = encode_account_credential(raw)
        client, http = _client(community_exchange_interval_seconds=0)
        client._create_skland_context = AsyncMock(return_value=_context())
        client._signed_skland_request = AsyncMock(
            return_value={"code": 0, "data": {"detail": _detail()}}
        )
        diagnostics = {
            "pools": {
                "assets": {"bytes": 0, "budget_bytes": 64 << 20},
                "i18n": {"bytes": 0, "budget_bytes": 64 << 20},
            },
            "namespaces": {},
        }
        with (
            patch.object(handlers, "official_client", client),
            patch.object(caches, "official_client", client),
            patch.object(handlers, "logger") as handler_log,
            patch.object(client_module, "logger") as client_log,
            patch.object(caches, "get_http_cache_diagnostics", AsyncMock(return_value=diagnostics)),
        ):
            for _ in range(2):
                await handlers._card_detail_with_snapshot(token, ROLE_A)
            lines = await caches._cache_status_lines()
        await http.aclose()
        client._create_skland_context.assert_awaited_once()  # a real exchange ran
        logged = _logged_text(handler_log, client_log)
        self.assertIn("exchange result=ok", logged)
        fingerprint = account_credential_fingerprint(token)
        for secret in (raw, raw[:8], fingerprint, fingerprint[:8]):
            self.assertNotIn(secret, logged)
            self.assertFalse(any(secret in line for line in lines))
        self.assertIn("社区换签: 命中 0 / 换签 1（失败 0，熔断拒绝 0）", "\n".join(lines))


class DailyDashboardReuseTests(_CacheTestCase):
    async def test_daily_reuses_details_and_retries_failed_accounts(self):
        roles = [
            SimpleNamespace(
                id=index, role_id=f"role-{index}", server_id="1", nickname=f"账号{index}",
                masked_uid=f"****000{index}", server_name="China",
            )
            for index in range(3)
        ]
        store = Mock()
        store.resolve_roles.return_value = roles
        store.decrypt_token.side_effect = lambda role, _cipher: f"token-{role.id}"

        async def card_detail(_token, role):
            await asyncio.sleep(0.05)
            if role.id == 1:
                raise EndfieldAPIError("card_detail", "10000", "接口繁忙")
            return _detail(f"管理员{role.id}")

        fetch = AsyncMock(side_effect=card_detail)
        draw = AsyncMock(return_value=b"png")
        with (
            patch.object(handlers, "account_store", store),
            patch.object(handlers.official_client, "card_detail", fetch),
            patch.object(handlers, "draw_daily_dashboard_card", draw),
            patch.object(handlers, "_finish_png", AsyncMock()),
        ):
            for _ in range(2):
                await handlers._handle_daily(
                    AsyncMock(), "caller", parse_command("日常"), Mock(), group=False
                )
                accounts = draw.await_args.args[0].accounts
                self.assertEqual([account.status for account in accounts], ["ok", "failed", "ok"])
        # Two successes are reused; the failed account is asked again (3 + 1, not 6).
        self.assertEqual(fetch.await_count, 4)
        self.assertEqual(self.prefetch.call_count, 2)
        self.assertEqual({call.kwargs["source"] for call in self.prefetch.call_args_list}, {"daily"})

    async def test_daily_after_account_detail_reuses_the_same_request(self):
        role = SimpleNamespace(
            id=0, role_id="role-0", server_id="1", nickname="账号0", masked_uid="****0000", server_name="China"
        )
        store = Mock()
        store.resolve_roles.return_value = [role]
        store.decrypt_token.return_value = "token"
        fetch = AsyncMock(return_value=_detail())
        with (
            patch.object(handlers, "account_store", store),
            patch.object(handlers.official_client, "card_detail", fetch),
            patch.object(handlers, "draw_daily_dashboard_card", AsyncMock(return_value=b"png")),
            patch.object(handlers, "_finish_png", AsyncMock()),
        ):
            await handlers._card_detail_with_snapshot("token", role)
            await handlers._handle_daily(AsyncMock(), "caller", parse_command("日常"), Mock(), group=False)
        self.assertEqual(fetch.await_count, 1)
        self.prefetch.assert_called_once_with(_detail(), source="card_detail")


class CommunityExchangeMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_interval_and_lock_waits_are_counted_separately(self):
        client, http = _client(community_exchange_interval_seconds=0.05)
        client._create_skland_context = AsyncMock(side_effect=lambda token, *_args: _context(token))
        with patch.object(client_module, "logger") as log:
            await client._skland_context("first-token")
            metrics = client.community_context_metrics()
            self.assertEqual((metrics["lock_waits"], metrics["interval_waits"]), (0, 0))

            await client._skland_context("second-token")  # right after: waits for the interval
            metrics = client.community_context_metrics()
            self.assertEqual((metrics["lock_waits"], metrics["interval_waits"]), (0, 1))
            self.assertGreaterEqual(metrics["interval_wait_ms"], 40)
            exchange_ms = metrics["exchange_ms"]

            # Held longer than the interval, so only the lock is waited for.
            await client._skland_exchange_lock.acquire()
            third = asyncio.create_task(client._skland_context("third-token"))
            await asyncio.sleep(0.08)
            client._skland_exchange_lock.release()
            await third
            metrics = client.community_context_metrics()
            self.assertEqual((metrics["lock_waits"], metrics["interval_waits"]), (1, 1))
            self.assertGreaterEqual(metrics["lock_wait_ms"], 70)
            self.assertGreaterEqual(metrics["exchange_ms"], exchange_ms)

            # A context hit does not exchange or wait.
            await client._skland_context("third-token")
            self.assertEqual(client.community_context_metrics()["cache_hits"], 1)
        await http.aclose()

        lines = [call.args[0] for call in log.info.call_args_list]
        self.assertEqual(len(lines), 3)
        pattern = re.compile(
            r"^\[endfield-community\] exchange result=ok lock_wait=\d+\.\d{3} "
            r"interval_wait=\d+\.\d{3} exchange=\d+\.\d{3} foreground_waiters=\d+$"
        )
        self.assertTrue(all(pattern.match(line) for line in lines), lines)
        self.assertRegex(lines[1], r"interval_wait=0\.0[2-9]\d")
        self.assertRegex(lines[2], r"lock_wait=0\.(0[7-9]|[1-9])")
        logged = _logged_text(log)
        for token in ("first-token", "second-token", "third-token"):
            self.assertNotIn(token, logged)
            self.assertNotIn(account_credential_fingerprint(token)[:8], logged)
        log.warning.assert_not_called()

    async def test_background_waiting_for_foreground_counts_as_lock_wait(self):
        client, http = _client(community_exchange_interval_seconds=0)
        client._create_skland_context = AsyncMock(side_effect=lambda token, *_args: _context(token))
        await client._skland_exchange_lock.acquire()
        foreground = asyncio.create_task(client._skland_context("foreground-token"))
        await asyncio.sleep(0)
        background = asyncio.create_task(client._skland_context("background-token", background=True))
        await asyncio.sleep(0.02)
        client._skland_exchange_lock.release()
        await asyncio.gather(foreground, background)
        await http.aclose()
        metrics = client.community_context_metrics()
        self.assertEqual(metrics["lock_waits"], 2)
        self.assertGreaterEqual(metrics["lock_wait_ms"], 30)

    async def test_long_queue_and_failures_are_logged(self):
        client, http = _client(community_exchange_interval_seconds=0.05)
        client._create_skland_context = AsyncMock(
            side_effect=[_context(), EndfieldAPIError("获取社区凭据", "10001", "busy")]
        )
        with (
            patch.object(client_module, "SKLAND_EXCHANGE_QUEUE_WARN_SECONDS", 0.01),
            patch.object(client_module, "logger") as log,
        ):
            await client._skland_context("first-token")
            with self.assertRaises(EndfieldAPIError):
                await client._skland_context("second-token")
        await http.aclose()
        self.assertIn("exchange result=ok", log.info.call_args.args[0])
        self.assertIn("exchange result=failed", log.warning.call_args.args[0])
        metrics = client.community_context_metrics()
        self.assertEqual((metrics["exchange_failed"], metrics["interval_waits"]), (1, 1))


class AttendanceSerialTests(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_get_starts_after_the_post_returns(self):
        client, http = _client()
        client._skland_context = AsyncMock(return_value=_context())
        events: list[tuple[str, str, float]] = []

        async def signed(_context, method, path, **_kwargs):
            events.append((method, "start", time.perf_counter()))
            if method == "POST":
                await asyncio.sleep(0.05)
                events.append((method, "done", time.perf_counter()))
                return {"code": 0, "data": {"awardIds": [{"id": "a1"}]}}
            return {
                "code": 0,
                "data": {
                    "calendar": [{"done": True}, {"done": False}],
                    "resourceInfoMap": {"a1": {"name": "折金票", "count": 1}},
                },
            }

        client._signed_skland_request = AsyncMock(side_effect=signed)
        role = SimpleNamespace(role_id="r1", server_id="1")
        with patch.object(client_module, "logger") as log:
            result = await client.attendance("secret-token-xyz", role)
        await http.aclose()
        self.assertEqual(
            [call.args[1:3] for call in client._signed_skland_request.await_args_list],
            [("POST", "/web/v1/game/endfield/attendance"), ("GET", "/web/v1/game/endfield/attendance")],
        )
        self.assertEqual([event[:2] for event in events], [("POST", "start"), ("POST", "done"), ("GET", "start")])
        self.assertGreaterEqual(events[2][2], events[1][2])
        self.assertEqual((result.status, result.monthly_count, result.calendar_days), ("success", 1, 2))
        self.assertEqual([reward.name for reward in result.rewards], ["折金票"])  # name came from the GET
        line = log.info.call_args.args[0]
        self.assertRegex(
            line,
            r"^\[endfield-community\] attendance status=success post=0\.0[4-9]\d calendar=\d+\.\d{3} calendar_ok=true$",
        )
        self.assertNotIn("secret-token-xyz", _logged_text(log))

    async def test_calendar_failure_is_logged_as_not_ok(self):
        client, http = _client()
        client._skland_context = AsyncMock(return_value=_context())
        client._signed_skland_request = AsyncMock(
            side_effect=[
                EndfieldAPIError("社区请求", "10001", "已签到"),
                EndfieldAPIError("社区请求", "500", "busy"),
            ]
        )
        with patch.object(client_module, "logger") as log:
            result = await client.attendance("token", SimpleNamespace(role_id="r1", server_id="1"))
        await http.aclose()
        self.assertEqual((result.status, result.monthly_count), ("already", None))
        self.assertIn("status=already", log.info.call_args.args[0])
        self.assertIn("calendar_ok=false", log.info.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
