"""Drive plugins through the real Entari event path in a sandboxed subprocess.

``run_scenarios(body)`` starts a fresh interpreter in a temporary working
directory (copied ``assets/``, empty ``data/``), builds the application with
``create_app()`` (every plugin, the feature gate and the usage recorder) and
runs ``body`` after the prelude below. Scenarios publish real Satori events
with ``say(...)`` through ``Entari.handle_event``; the only fake is the HTTP
call of the Satori protocol (``Protocol.call_api``), which records what the bot
sends. The script must end by printing ``ALL-OK``.

Helpers available to ``body``:

- ``app``: the Entari application; ``plugin(name)``: a loaded plugin module.
- ``make_account(self_id='test-bot')``: a real ``Account`` with the recording protocol
  (call it outside coroutines); ``default_account`` is the one ``say`` uses.
- ``await say(content, user=..., group=..., private=..., account=...)``:
  publish one ``message-created`` event and return the contents (Satori XML
  strings) sent back while it was handled. ``content`` is a string (Satori
  XML) or a list of elements.
- ``account.protocol.calls``: every faked API call as ``(action, params)``;
  ``account.protocol.answers[action]`` sets a result (value or callable).
- ``run(coro)``: run a coroutine on Entari's event loop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from otae_bot.plugin_registry import discover_plugins

ROOT = Path(__file__).resolve().parents[2]

PRELUDE = r'''
import asyncio, sys
from datetime import datetime, timezone
from itertools import count

sys.path.insert(0, sys.argv[1])

from otae_bot.application import create_app
app = create_app()

from arclet.entari.config import EntariConfig
from arclet.entari.plugin.service import plugin_service
from arclet.entari.session import EntariProtocol
from satori import Api, Channel, ChannelType, Event, Guild, Login, Member, MessageObject, User
from satori.client.account import Account, ApiInfo

# Entari 0.19 runs built-in niche plugins only when the app starts; run the command one here.
plugin_service.plugins['$.commands'].exec_apply()
EntariConfig.instance.basic.prefix = ['/']

_ids = count()


def plugin(name):
    return plugin_service.plugins[name].module


class Protocol(EntariProtocol):
    """The real EntariProtocol (APIRequest/SendRequest hooks included); only HTTP is faked."""

    def __init__(self, account):
        super().__init__(account)
        self.calls = []
        self.sent = []
        self.answers = {
            Api.USER_CHANNEL_CREATE.value: lambda params: {'id': 'private:' + params['user_id'], 'type': 1},
        }

    async def call_api(self, action, params=None, multipart=False, method='POST'):
        name = getattr(action, 'value', action)
        params = dict(params or {})
        self.calls.append((name, params))
        if name == Api.MESSAGE_CREATE.value:
            self.sent.append((params['channel_id'], params['content']))
            return [{'id': 'sent-' + str(next(_ids)), 'content': params['content']}]
        if name not in self.answers:
            raise AssertionError('unexpected API call ' + name)
        answer = self.answers[name]
        return answer(params) if callable(answer) else answer


loop = asyncio.get_event_loop()


def run(coro):
    return loop.run_until_complete(coro)


def make_account(self_id='test-bot'):
    async def build():  # aiohttp wants a running loop for the protocol's client session
        return Account(Login(user=User(self_id), platform='qq'), ApiInfo(port=5500, token='test-token'), [], Protocol)
    return run(build())


default_account = make_account()


def incoming(content, *, user='sender', group='100', private=False, account=None, member=None):
    account = account or default_account
    message_id = 'incoming-' + str(next(_ids))
    if isinstance(content, str):
        message = MessageObject(message_id, content)
    else:
        message = MessageObject.from_elements(message_id, list(content))
    if private:
        channel, guild = Channel('private:' + user, ChannelType.DIRECT), None
    else:
        channel, guild = Channel(group, ChannelType.TEXT), Guild(group)
    origin = Event('message-created', datetime.now(timezone.utc), Login(user=User(account.self_id), platform='qq'),
                   channel=channel, guild=guild, user=User(user), member=member, message=message)
    return account, origin


async def say(content, **kwargs):
    account, origin = incoming(content, **kwargs)
    before = len(account.protocol.sent)
    await app.handle_event(account, origin)
    for _ in range(3):  # let tasks started by the handlers settle
        await asyncio.sleep(0)
    return [sent for _, sent in account.protocol.sent[before:]]
'''


def run_scenarios(body: str, *, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run ``PRELUDE + body`` in a sandbox; the caller checks ``returncode`` and ``ALL-OK``."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "assets", root / "assets")
        for name in discover_plugins(ROOT / "plugins"):
            package = root / name.replace(".", "/")
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").touch()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-c", PRELUDE + body, str(ROOT)],
            cwd=root, capture_output=True, text=True, timeout=timeout, env=env,
        )
