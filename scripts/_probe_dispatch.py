import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from arclet.entari import MessageChain, Session
from arclet.entari.command.provider import _remove_config_prefix
from arclet.entari.config import EntariConfig
from arclet.entari.const import ITEM_ACCOUNT
from arclet.entari.event.command import CommandExecute
from arclet.letoderea import post
from satori import ChannelType

from otae_bot.application import create_app

create_app()
EntariConfig.instance.basic.prefix = ["/"]


def session():
    s = object.__new__(Session)
    s.account = SimpleNamespace(
        platform="qq",
        self_id="test-bot",
        guild_member_get=AsyncMock(return_value=SimpleNamespace(roles=[])),
        internal=AsyncMock(return_value={}),
    )
    s.event = SimpleNamespace(
        user=SimpleNamespace(id="test-user"),
        member=SimpleNamespace(roles=[]),
        guild=SimpleNamespace(id="test-group"),
        channel=SimpleNamespace(id="test-group", type=ChannelType.TEXT),
        content="",
    )
    s.reply = None
    s.send = AsyncMock(return_value=[])
    return s


async def probe(text):
    s = session()
    try:
        await post(
            CommandExecute(_remove_config_prefix(MessageChain(text)), s),
            inherit_ctx={ITEM_ACCOUNT: s.account},
        )
    except Exception as error:  # noqa: BLE001
        print(f"{text!r:30} EXC {type(error).__name__}: {str(error)[:90]}")
        return
    calls = s.send.await_args_list
    first = str(calls[0].args[0])[:110].replace("\n", " ") if calls else ""
    print(f"{text!r:30} sends={len(calls)} first={first}")


async def main():
    for text in sys.argv[1:]:
        await probe(text)


asyncio.get_event_loop().run_until_complete(main())
