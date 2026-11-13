"""Accounts on several platforms side by side (QQ through LLBot, and the Ir panel).

Real Satori ``Account`` objects with the real ``EntariProtocol``; only the HTTP
call is faked (``tests/support/satori_accounts.py``).
"""

from __future__ import annotations

import unittest

from arclet.entari import MessageChain, Text

from otae_bot.accounts import runtime
from otae_bot.delivery import FAILED, NO_ACCOUNT, SENT, Target, deliver
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



GROUP = "977295722"


class MultiPlatformDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """deliver() with a QQ account and the Ir panel online; Ir logged in last (the default)."""

    async def asyncSetUp(self):
        patcher = pin_event_loop()
        patcher.start()
        self.addCleanup(patcher.stop)
        runtime.clear_account()
        self.addCleanup(runtime.clear_account)
        self.qq = make_account("3898438488", platform="llonebot", guilds=[GROUP])
        # Ir: native Satori, a non-numeric id, "guilds" that are folders.
        self.ir = make_account("ir-7f3a", platform="ir_web", guilds=["folder-root", GROUP + "-folder"])
        for bot in (self.qq, self.ir):
            runtime.set_account(bot)
        await runtime.refresh_guilds()
        self.assertIs(runtime.get_account(), self.ir)

    def message(self):
        return MessageChain([Text("hi")])

    async def test_a_group_delivery_goes_through_the_account_listing_the_group(self):
        result = await deliver(Target.group(GROUP), self.message())
        self.assertEqual((result.status, result.account), (SENT, self.qq))
        self.assertEqual(self.qq.protocol.sent, [(GROUP, "hi")])
        self.assertEqual(self.ir.protocol.sent, [])

    async def test_a_group_no_account_lists_is_not_handed_to_the_default_account(self):
        result = await deliver(Target.group("100"), self.message())
        self.assertEqual(result.status, NO_ACCOUNT)
        self.assertEqual(self.ir.protocol.sent + self.qq.protocol.sent, [])

    async def test_a_private_target_on_a_known_platform_never_uses_another_platform(self):
        result = await deliver(Target.private("2461673400", platform="llonebot"), self.message())
        self.assertIs(result.account, self.qq)
        self.assertEqual(self.qq.protocol.sent, [("private:2461673400", "hi")])
        # The named account went offline: another account on that platform, never Ir.
        runtime.clear_account(self.qq)
        result = await deliver(Target.private("2461673400", self_id="3898438488", platform="llonebot"), self.message())
        self.assertEqual(result.status, NO_ACCOUNT)
        self.assertEqual(self.ir.protocol.sent, [])

    async def test_a_private_target_on_an_unknown_platform_tries_the_next_account(self):
        self.ir.protocol.reject = RuntimeError("user not found")
        result = await deliver(Target.private("2461673400"), self.message())
        self.assertEqual((result.status, result.account), (SENT, self.qq))
        self.qq.protocol.reject = RuntimeError("send failed")
        result = await deliver(Target.private("2461673400"), self.message())
        self.assertEqual((result.status, result.account), (FAILED, self.qq))  # The last failure is reported.


if __name__ == "__main__":
    unittest.main()
