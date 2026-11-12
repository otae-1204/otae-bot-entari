from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arclet.letoderea import ExitState, Subscriber
from satori import ChannelType

from otae_bot.accounts import runtime
from otae_bot.pipeline import feature_gate
from otae_bot.compat.entari import ChainMsg
from otae_bot.delivery import FAILED, NO_ACCOUNT, SENT, SUPPRESSED, Target, deliver, is_not_in_group
from otae_bot.groups.features import GroupFeatureStore, GroupScope


def account(self_id, groups=(), *, platform="qq", send=None):
    listings = []

    async def pages():
        listings.append(self_id)
        for group in bot.groups:
            yield SimpleNamespace(id=group)

    bot = SimpleNamespace(
        self_id=self_id, platform=platform, groups=list(groups), listings=listings,
        protocol=SimpleNamespace(
            guild_list=pages,
            send_message=AsyncMock(return_value=[SimpleNamespace(id=f"m-{self_id}")]) if send is None else send,
            send_private_message=AsyncMock(return_value=[SimpleNamespace(id=f"p-{self_id}")]),
        ),
    )
    return bot


class TargetTests(unittest.TestCase):
    def test_constructors_keep_ids_as_strings(self):
        group = Target.group("100", channel_id="101", self_id="9", platform="qq")
        self.assertEqual(group, Target("qq", "9", "100", "101", ""))
        self.assertFalse(group.is_private)
        private = Target.private("200", self_id="9")
        self.assertEqual((private.user_id, private.guild_id, private.is_private), ("200", "", True))
        self.assertEqual(Target.group(None).guild_id, "")
        with self.assertRaises(AttributeError):
            group.guild_id = "x"  # frozen

    def test_from_event(self):
        bot = SimpleNamespace(self_id="9", platform="qq")
        user = SimpleNamespace(id="200")
        in_group = SimpleNamespace(guild=SimpleNamespace(id="100"), user=user,
                                   channel=SimpleNamespace(id="101", type=ChannelType.TEXT))
        self.assertEqual(Target.from_event(bot, in_group), Target("qq", "9", "100", "101", ""))
        channel_only = SimpleNamespace(guild=None, user=user, channel=SimpleNamespace(id="101", type=ChannelType.TEXT))
        self.assertEqual(Target.from_event(bot, channel_only).guild_id, "101")
        direct = SimpleNamespace(guild=None, user=user, channel=SimpleNamespace(id="private:200", type=ChannelType.DIRECT))
        self.assertEqual(Target.from_event(bot, direct), Target("qq", "9", "", "", "200"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime.clear_account()
        self.addCleanup(runtime.clear_account)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = GroupFeatureStore(Path(directory.name) / "switches.json")
        self.enterContext(patch.object(feature_gate, "feature_store", self.store))
        feature_gate._suppressed_logged.clear()

    async def online(self, *bots):
        for bot in bots:
            runtime.set_account(bot)
        await runtime.refresh_guilds()

    # Account resolution

    async def test_explicit_account_is_used_even_if_not_listed_in_the_group(self):
        default, other = account("1", ["100"]), account("2")
        await self.online(other, default)
        result = await deliver(Target.group("100"), "hi", account=other)
        self.assertEqual((result.status, result.account), (SENT, other))
        other.protocol.send_message.assert_awaited_once_with("100", ChainMsg.text("hi"))
        self.assertEqual(result.messages[0].id, "m-2")

    async def test_group_prefers_the_named_account_when_it_is_in_the_group(self):
        default, named = account("1", ["100"]), account("2", ["100"])
        await self.online(named, default)
        self.assertIs((await deliver(Target.group("100", self_id="2"), "x")).account, named)
        # Unknown/offline self_id or another platform falls back to the default member.
        self.assertIs((await deliver(Target.group("100", self_id="3"), "x")).account, default)
        self.assertIs((await deliver(Target.group("100", self_id="2", platform="other"), "x")).account, default)

    async def test_group_skips_accounts_not_in_it(self):
        default, member = account("1", ["200"]), account("2", ["100"])
        await self.online(member, default)
        # The named account is online but not in the group: use one that is.
        result = await deliver(Target.group("100", self_id="1"), "x")
        self.assertIs(result.account, member)
        default.protocol.send_message.assert_not_awaited()

    async def test_stale_listing_is_refreshed_before_giving_up(self):
        bot = account("1", ["200"])
        await self.online(bot)
        bot.groups.append("100")  # Invited since the last listing.
        runtime._guilds_at["1"] -= runtime.GUILD_RETRY_REFRESH_SECONDS + 1
        result = await deliver(Target.group("100"), "x")
        self.assertEqual((result.status, result.account), (SENT, bot))
        self.assertEqual(len(bot.listings), 2)

    async def test_no_account_in_the_group(self):
        bot = account("1", ["200"])
        await self.online(bot)
        result = await deliver(Target.group("100"), "x")
        self.assertEqual((result.status, result.account, result.messages), (NO_ACCOUNT, None, []))
        bot.protocol.send_message.assert_not_awaited()
        runtime.clear_account()
        self.assertEqual((await deliver(Target.private("200"), "x")).status, NO_ACCOUNT)

    async def test_private_uses_named_online_account_else_default(self):
        default, named = account("1"), account("2")
        await self.online(named, default)
        result = await deliver(Target.private("200", self_id="2"), "x")
        self.assertIs(result.account, named)
        named.protocol.send_private_message.assert_awaited_once_with("200", ChainMsg.text("x"))
        self.assertIs((await deliver(Target.private("200"), "x")).account, default)
        self.assertIs((await deliver(Target.private("200", self_id="3"), "x")).account, default)

    async def test_channel_id_is_the_send_destination_when_known(self):
        bot = account("1")
        await deliver(Target.group("100", channel_id="101"), "x", account=bot)
        bot.protocol.send_message.assert_awaited_once_with("101", ChainMsg.text("x"))

    # Per-group feature switch

    async def test_switched_off_group_is_suppressed_for_that_plugin_only(self):
        bot = account("9")
        self.store.set_enabled(GroupScope("qq", "9", "100"), "hyw", False)
        with patch.object(feature_gate.logger, "warning") as warning:
            for _ in range(2):
                result = await deliver(Target.group("100"), "x", account=bot, plugin="hyw")
                self.assertEqual((result.status, result.account), (SUPPRESSED, bot))
        warning.assert_called_once()  # Once per interval.
        bot.protocol.send_message.assert_not_awaited()
        self.assertEqual((await deliver(Target.group("101"), "x", account=bot, plugin="hyw")).status, SENT)
        self.assertEqual((await deliver(Target.group("100"), "x", account=bot, plugin="endfield")).status, SENT)
        self.assertEqual((await deliver(Target.private("100"), "x", account=bot, plugin="hyw")).status, SENT)

    async def test_plugin_defaults_to_the_running_subscriber(self):
        bot = account("9")
        self.store.set_enabled(GroupScope("qq", "9", "100"), "hyw", False)

        async def notify():
            return (await asyncio.create_task(deliver(Target.group("100"), "x", account=bot))).status

        notify.__module__ = "plugins.hyw.handlers"
        self.assertEqual(await Subscriber(notify).handle({}), SUPPRESSED)

    # Failures

    async def test_not_in_group_failure_forgets_the_group_and_relists(self):
        bot = account("1", ["100"], send=AsyncMock(side_effect=RuntimeError("getGroupImageUploadInfo msgInfo undefined")))
        await self.online(bot)
        with patch.object(runtime, "schedule_guild_refresh") as relist:
            result = await deliver(Target.group("100"), "x")
        self.assertEqual((result.status, result.account), (FAILED, bot))
        self.assertTrue(result.not_in_group)
        self.assertNotIn("100", runtime.known_guilds(bot))
        relist.assert_called_once_with(bot)
        with self.assertRaises(RuntimeError):
            result.raise_for_failure()

    async def test_other_failures_keep_the_listing(self):
        bot = account("1", ["100"], send=AsyncMock(side_effect=RuntimeError("timeout")))
        await self.online(bot)
        with patch.object(runtime, "schedule_guild_refresh") as relist:
            result = await deliver(Target.group("100"), "x")
        self.assertEqual(result.status, FAILED)
        self.assertFalse(result.not_in_group)
        self.assertIn("100", runtime.known_guilds(bot))
        relist.assert_not_called()

    def test_not_in_group_classification(self):
        for text in ("msgInfo undefined", "账号不在该群", "bot is not a member", "not in this group"):
            self.assertTrue(is_not_in_group(RuntimeError(text)), text)
        for text in ("timeout", "rate limited"):
            self.assertFalse(is_not_in_group(RuntimeError(text)), text)

    async def test_stop_signal_and_cancellation_propagate(self):
        for exc in (ExitState.stop, asyncio.CancelledError()):
            bot = account("1", send=AsyncMock(side_effect=exc))
            with self.assertRaises(type(exc)):
                await deliver(Target.group("100"), "x", account=bot)


if __name__ == "__main__":
    unittest.main()
