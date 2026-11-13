"""Real Satori ``Account`` objects for in-process tests; only the HTTP call is faked.

``make_account(self_id, platform=..., guilds=...)`` must be called inside a
running event loop (aiohttp). The protocol is the real ``EntariProtocol``:
``guild.list`` answers one page with ``guilds`` (``None``: the call fails, as
on an endpoint without it), ``message.create`` records the send in ``sent``
as ``(channel_id, content)`` or raises ``reject`` when set, and
``user.channel.create`` opens ``private:<user id>``.
"""

from __future__ import annotations

from arclet.entari.session import EntariProtocol
from satori import Api, ChannelType, Login, User
from satori.client.account import Account, ApiInfo


class RecordingProtocol(EntariProtocol):
    def __init__(self, account):
        super().__init__(account)
        self.calls: list[tuple[str, dict]] = []
        self.guilds: list[str] | None = []
        self.reject: BaseException | None = None
        self.answer_empty = False

    async def call_api(self, action, params=None, multipart=False, method="POST"):
        name = str(getattr(action, "value", action))
        params = dict(params or {})
        self.calls.append((name, params))
        if name == Api.GUILD_LIST.value:
            if self.guilds is None:
                raise RuntimeError("guild.list unavailable")
            return {"data": [{"id": guild} for guild in self.guilds], "next": None}
        if name == Api.USER_CHANNEL_CREATE.value:
            return {"id": "private:" + params["user_id"], "type": ChannelType.DIRECT.value}
        if name == Api.MESSAGE_CREATE.value:
            if self.reject is not None:
                raise self.reject
            if self.answer_empty:
                return []
            return [{"id": f"sent-{len(self.calls)}", "content": params["content"]}]
        raise AssertionError(f"unexpected API call {name}")

    # Properties, not methods: EntariProtocol wraps every method as an API call.
    @property
    def sent(self) -> list[tuple[str, str]]:
        return [(params["channel_id"], params["content"])
                for action, params in self.calls if action == Api.MESSAGE_CREATE.value]

    @property
    def listings(self) -> int:
        return sum(1 for action, _ in self.calls if action == Api.GUILD_LIST.value)


def make_account(self_id: str, *, platform: str = "llonebot", guilds: list[str] | None = (),
                 features: tuple[str, ...] = ("message.create",)) -> Account:
    login = Login(user=User(self_id), platform=platform, features=list(features))
    account = Account(login, ApiInfo(port=5500, token="t"), [], RecordingProtocol)
    account.protocol.guilds = None if guilds is None else list(guilds)
    return account


def pin_event_loop():
    """Entari pins Letoderea's tasks to its own loop at import; return a patcher for the running one.

    Start it inside the test's loop (``IsolatedAsyncioTestCase.asyncSetUp`` or an
    ``asyncio.run`` scenario) and stop it afterwards.
    """
    import asyncio
    from unittest import mock

    from arclet.letoderea.utils import _EventSystem

    return mock.patch.object(_EventSystem, "loop", asyncio.get_running_loop())
