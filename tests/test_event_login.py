"""Events whose ``login`` is partial (Satori spec: non-login events carry only sn, user and
platform) reach the account registered from READY on the same connection, never another one.

Driven through the real app (``create_app()``) and satori-python's real ``App.post`` with
payloads shaped like what Iridium and LLBot's native Satori server send.
"""

from __future__ import annotations

import unittest

from satori import EventType
from satori.model import Event, Login, LoginStatus, User

from otae_bot.accounts.event_login import matching_login, resolved_login

SCENARIOS = r"""
from arclet.entari import Account, MessageCreatedEvent, listen
from satori import EventType
from satori.client.config import WebsocketsInfo
from satori.client.network.websocket import WsNetwork
from satori.model import Ready

from otae_bot.accounts.event_login import EventLoginEntari

assert isinstance(app, EventLoginEntari), type(app)
# Accounts the app registers from READY get the recording protocol (only HTTP is faked).
app.default_api_cls = Protocol

received = []


@listen(MessageCreatedEvent)
async def record(event: MessageCreatedEvent, account: Account):
    received.append((account.platform, account.self_id, event.channel.id))


def connection(port):
    return WsNetwork(app, WebsocketsInfo(host='127.0.0.1', port=port, token='test-token'))


def full_login(sn, platform, user, adapter, features=('message.create', 'upload.create')):
    return {'sn': sn, 'platform': platform, 'user': {'id': user, 'name': user}, 'status': 1,
            'adapter': adapter, 'features': list(features)}


async def ready(conn, *logins):
    # What WsNetwork does with READY: one login-added per login, through App.post.
    for login in Ready.parse({'logins': list(logins), 'proxy_urls': []}).logins:
        await app.post(Event(EventType.LOGIN_ADDED, datetime.now(), login), conn)


_sn = count(1)


def message(platform, user, login_sn, content, channel='c1'):
    # A non-login event as the spec has it: login with sn, user and platform only.
    return {'sn': next(_sn) * 1024, 'type': 'message-created', 'timestamp': 1794534681000,
            'login': {'sn': login_sn, 'platform': platform, 'user': {'id': user, 'name': user}},
            'channel': {'id': channel, 'type': 0, 'name': 'session'}, 'guild': {'id': 'g1', 'name': 'folder'},
            'user': {'id': 'someone', 'name': 'someone'},
            'message': {'id': 'm' + str(next(_ids)), 'content': content}}


async def post(conn, raw):
    before = len(received)
    await app.post(Event.parse(raw), conn)
    for _ in range(5):
        await asyncio.sleep(0)
    return received[before:]


def account(platform, user):
    found = [a for a in app.accounts.values() if a.platform == platform and a.self_id == user]
    assert len(found) == 1, (platform, user, list(app.accounts))
    return found[0]


async def scenario():
    ir, llbot = connection(7810), connection(5500)
    await ready(ir, full_login(1, 'app-a', 'bot', 'iridium'), full_login(2, 'app-a', 'helper', 'iridium'))
    await ready(llbot, full_login(1, 'llonebot', 'bot', 'llonebot', features=('message.create',)))
    assert len(app.accounts) == 3, list(app.accounts)

    # A partial login reaches its account, and a plugin command answers on that account.
    assert await post(ir, message('app-a', 'bot', 1, '/ping', channel='c1')) == [('app-a', 'bot', 'c1')]
    ir_bot = account('app-a', 'bot')
    sent = [(channel, content) for channel, content in ir_bot.protocol.sent]
    assert len(sent) == 1 and sent[0][0] == 'c1' and '尚未添加任何服务器' in sent[0][1], sent
    # The account keeps READY's login: features (outbound media's upload decision) intact.
    assert ir_bot.self_info.adapter == 'iridium' and 'upload.create' in ir_bot.self_info.features

    # Two accounts on one connection: each login goes to its own account.
    assert await post(ir, message('app-a', 'helper', 2, 'hi', channel='c2')) == [('app-a', 'helper', 'c2')]
    # Same platform and user under another sn, unambiguous on the connection: still that account.
    assert await post(ir, message('app-a', 'helper', 9, 'hi', channel='c3')) == [('app-a', 'helper', 'c3')]

    # Same user id on two connections and platforms: never delivered across.
    assert await post(llbot, message('llonebot', 'bot', 1, 'hi', channel='q1')) == [('llonebot', 'bot', 'q1')]
    assert await post(llbot, message('app-a', 'bot', 1, 'hi', channel='x1')) == []
    assert await post(ir, message('llonebot', 'bot', 1, 'hi', channel='x2')) == []
    assert await post(ir, message('app-a', 'nobody', 1, 'hi', channel='x3')) == []
    assert len(account('llonebot', 'bot').protocol.sent) == 0

    # login-removed with a partial login removes the right account; later events are dropped.
    removed = {'sn': next(_sn) * 1024, 'type': 'login-removed', 'timestamp': 1794534681000,
               'login': {'sn': 2, 'platform': 'app-a', 'user': {'id': 'helper'}, 'status': 0}}
    await app.post(Event.parse(removed), ir)
    assert sorted((a.platform, a.self_id) for a in app.accounts.values()) == [('app-a', 'bot'), ('llonebot', 'bot')]
    assert sorted((a.platform, a.self_id) for a in ir.accounts.values()) == [('app-a', 'bot')]
    assert await post(ir, message('app-a', 'helper', 2, 'hi', channel='c4')) == []
    assert await post(ir, message('app-a', 'bot', 1, 'hi', channel='c5')) == [('app-a', 'bot', 'c5')]


run(scenario())
print('ALL-OK')
"""


def _login(sn: int, platform: str, user: str, adapter: str = "satori", features=()) -> Login:
    return Login(sn=sn, platform=platform, user=User(user), adapter=adapter, features=list(features))


class MatchingTests(unittest.TestCase):
    def test_partial_login_matches_by_platform_user_and_sn(self):
        registered = [_login(1, "a", "bot", "iridium"), _login(2, "a", "bot", "iridium"), _login(1, "b", "bot", "x")]
        self.assertIs(matching_login(_login(2, "a", "bot"), registered), registered[1])
        self.assertIs(matching_login(_login(1, "b", "bot"), registered), registered[2])

    def test_without_the_sn_only_an_unambiguous_user_matches(self):
        registered = [_login(1, "a", "bot", "iridium"), _login(2, "a", "bot", "iridium"), _login(3, "a", "other", "iridium")]
        self.assertIsNone(matching_login(_login(9, "a", "bot"), registered))
        self.assertIs(matching_login(_login(9, "a", "other"), registered), registered[2])
        self.assertIsNone(matching_login(_login(1, "c", "bot"), registered))

    def test_events_take_the_accounts_login_and_login_events_only_its_identity(self):
        registered = [_login(1, "a", "bot", "iridium", features=("upload.create",))]
        message = Event(EventType.MESSAGE_CREATED, None, _login(1, "a", "bot"))
        self.assertEqual(resolved_login(message, registered), registered[0])
        removed = Event(EventType.LOGIN_REMOVED, None, _login(1, "a", "bot"))
        removed.login.status = LoginStatus.OFFLINE
        login = resolved_login(removed, registered)
        self.assertEqual((login.adapter, login.sn, login.status, login.features),
                         ("iridium", 1, LoginStatus.OFFLINE, ["upload.create"]))
        added = Event(EventType.LOGIN_ADDED, None, _login(1, "a", "bot"))
        self.assertIsNone(resolved_login(added, registered))


class RealAppTests(unittest.TestCase):
    def test_partial_event_logins_reach_their_account_through_the_real_app(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(SCENARIOS)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
