"""Accounts on several platforms side by side (QQ through LLBot, and the Ir panel).

Real Satori ``Account`` objects with the real ``EntariProtocol``; only the HTTP
call is faked (``tests/support/satori_accounts.py``).
"""

from __future__ import annotations

import unittest

from otae_bot.accounts import runtime
from tests.support.satori_accounts import make_account, pin_event_loop


class MultiPlatformRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = pin_event_loop()
        patcher.start()
        self.addCleanup(patcher.stop)
        runtime.clear_account()
        self.addCleanup(runtime.clear_account)

    async def test_the_same_user_id_on_two_platforms_is_two_accounts(self):
        qq = make_account("10001", platform="llonebot", guilds=["977295722"])
        ir = make_account("10001", platform="ir_web", guilds=["folder-root"])
        runtime.set_account(qq)
        runtime.set_account(ir)
        self.assertEqual(runtime.get_bots(), [ir, qq])  # Neither overwrote the other.
        await runtime.refresh_guilds()
        self.assertEqual(runtime.known_guilds(qq), frozenset({"977295722"}))
        self.assertEqual(runtime.known_guilds(ir), frozenset({"folder-root"}))
        self.assertEqual(runtime.get_bots_for_guild("977295722"), [qq])
        runtime.forget_guild(ir, "folder-root")
        self.assertEqual(runtime.known_guilds(qq), frozenset({"977295722"}))
        runtime.clear_account(ir)  # Ir went offline: the QQ account stays, with its listing.
        self.assertEqual(runtime.get_bots(), [qq])
        self.assertEqual(runtime.get_bots_for_guild("977295722"), [qq])
        self.assertEqual(runtime.account_key(qq), ("llonebot", "10001"))


if __name__ == "__main__":
    unittest.main()
