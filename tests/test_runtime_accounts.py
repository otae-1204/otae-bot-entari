from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from otae_bot.adapters import entari, runtime


def account(self_id, groups=(), *, fail=None):
    calls = []

    async def pages():
        calls.append(self_id)
        if fail is not None:
            raise fail
        for group in groups:
            yield SimpleNamespace(id=group)

    bot = SimpleNamespace(self_id=self_id, platform="qq", protocol=SimpleNamespace(guild_list=pages))
    bot.listings = calls
    return bot


class RuntimeAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime.clear_account()
        self.addCleanup(runtime.clear_account)

    def test_every_online_account_is_kept_and_the_latest_login_is_the_default(self):
        first, second = account("1194397508"), account("3898438488")
        runtime.set_account(first)
        runtime.set_account(second)
        self.assertIs(runtime.get_account(), second)
        self.assertIs(entari.get_bot(), second)
        self.assertEqual(entari.get_bots(), [second, first])
        runtime.set_account(first)  # Reconnected: default again.
        self.assertEqual(runtime.get_bots(), [first, second])
        runtime.clear_account("1194397508")
        # Another account is still online, so there is still a default.
        self.assertIs(entari.get_bot(), second)
        self.assertTrue(runtime.is_online())
        runtime.clear_account("1194397508")
        runtime.clear_account("3898438488")
        self.assertIsNone(runtime.get_account())
        with self.assertRaises(RuntimeError):
            entari.get_bot()

    async def test_groups_come_from_guild_list_and_pick_an_account_in_the_group(self):
        first, second = account("1194397508", ["100", "200"]), account("3898438488", ["977295722", "200"])
        runtime.set_account(second)
        runtime.set_account(first)
        self.assertIsNone(entari.get_bot_for_guild("977295722"))  # Not listed yet.
        await runtime.refresh_guilds()
        self.assertIs(entari.get_bot_for_guild("977295722"), second)
        self.assertIs(entari.get_bot_for_guild("200"), first)  # Default first when both are in it.
        self.assertEqual(runtime.get_bots_for_guild("200"), [first, second])
        self.assertIsNone(entari.get_bot_for_guild("300"))
        runtime.forget_guild(first, "200")
        self.assertIs(entari.get_bot_for_guild("200"), second)
        runtime.clear_account("3898438488")
        self.assertIsNone(entari.get_bot_for_guild("977295722"))

    async def test_fresh_listings_are_reused_and_concurrent_refreshes_list_once(self):
        bot = account("1194397508", ["100"])
        runtime.set_account(bot)
        await asyncio.gather(*(runtime.refresh_guilds(max_age=60) for _ in range(3)))
        self.assertEqual(bot.listings, ["1194397508"])
        await runtime.refresh_guilds(max_age=60)
        self.assertEqual(len(bot.listings), 1)
        await runtime.refresh_guilds()  # max_age=0 lists again.
        self.assertEqual(len(bot.listings), 2)

    async def test_a_failed_listing_keeps_the_previous_one_and_is_not_retried_at_once(self):
        bot = account("1194397508", ["100"])
        runtime.set_account(bot)
        await runtime.refresh_guilds()
        bot.protocol.guild_list = account("x", fail=RuntimeError("guild.list unsupported")).protocol.guild_list
        with mock.patch.object(runtime.logger, "warning") as warning:
            await runtime.refresh_guilds()
            await runtime.refresh_guilds(max_age=60)
        warning.assert_called_once()
        self.assertIn("1194397508", warning.call_args.args[0])
        self.assertEqual(runtime.known_guilds(bot), frozenset({"100"}))
        unknown = account("3898438488", fail=RuntimeError("down"))
        runtime.set_account(unknown)
        await runtime.refresh_guilds(unknown)
        self.assertIsNone(runtime.known_guilds(unknown))

    async def test_login_lists_groups_in_the_background(self):
        captured = []
        with mock.patch.object(entari, "listen", lambda _event: lambda func: captured.append(func) or func):
            entari.on_ready(lambda: None)
        bot = account("3898438488", ["977295722"])
        await captured[0](SimpleNamespace(status=entari.LoginStatus.ONLINE, account=bot))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertIs(entari.get_bot_for_guild("977295722"), bot)
        await captured[0](SimpleNamespace(status=entari.LoginStatus.OFFLINE, account=bot))
        self.assertEqual(entari.get_bots(), [])


if __name__ == "__main__":
    unittest.main()
