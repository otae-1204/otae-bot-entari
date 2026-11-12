"""Platform protocol code may only live in otae_bot/boundary/, and imports follow the layering.

Existing violations are pinned in tests/fixtures/boundary_guard_baseline.json;
see tests/support/boundary_guard.py for the rules and the --report/--write commands.
"""

from __future__ import annotations

import ast
from collections import Counter
import unittest

from tests.support import boundary_guard as guard

REGENERATE = "python -m tests.support.boundary_guard --write"


def _scan_source(source: str, rel: str = "x.py") -> list[str]:
    return sorted(v.rule for v in guard._Scanner(rel, source).run(ast.parse(source)))


LAYERING = {name for code, name in guard.RULES.items() if code.startswith("L")}


def _layering(rel: str, source: str) -> list[str]:
    return [rule for rule in _scan_source(source, rel) if rule in LAYERING]


class BoundaryGuardTests(unittest.TestCase):
    def test_no_new_violations_and_baseline_is_current(self):
        violations = guard.scan()
        new, gone = guard.compare(violations, guard.load_baseline())
        self.assertFalse(new, "new violations (protocol code belongs in otae_bot/boundary/; imports follow "
                         "docs/architecture_conventions.md §2):\n"
                         + "\n".join(f"  {v.rule} {v.file}:{v.line}  {v.snippet}" for v in new))
        self.assertFalse(gone, f"violations fixed but still in the baseline; run `{REGENERATE}`:\n"
                         + "\n".join(f"  {r} {f}  {s}" for (r, f, s) in sorted(gone)))

    def test_compare_counts_duplicates(self):
        one = guard.Violation("onebot-action", "a.py", 1, 'x("get_msg")')
        two = one._replace(line=9)
        new, gone = guard.compare([one, two], Counter({one.key: 1}))
        self.assertEqual((new, gone), ([two], Counter()))
        new, gone = guard.compare([], Counter({one.key: 2}))
        self.assertEqual((new, gone), ([], Counter({one.key: 2})))

    def test_rules_flag_protocol_code(self):
        source = '''
"""Docstrings mentioning get_msg, internal/onebot11 or ONEBOT_HTTP_URL are fine."""
from otae_bot.adapters.onebot import call_onebot_action
from arclet.letoderea.exceptions import _ExitException
async def f(bot, event, plugin, scope):
    await bot.internal(action="get_msg", message_id=int(event.user.id))
    url = ONEBOT_HTTP_URL
    plugin._scope.propagators.append(1)
    if scope.platform.casefold() in {"qq", "onebot"}:
        return event.__class__.__name__ == "FriendRequestEvent"
'''
        self.assertEqual(_scan_source(source), sorted([
            "onebot-http", "framework-private", "internal-call", "onebot-action", "numeric-id",
            "onebot-http", "framework-private", "platform-name", "event-class-name"]))

    def test_rules_ignore_lookalikes(self):
        source = '''
import sys
URL = "https://api.bilibili.com/x/internal/gaia-gateway"
def g(cmd, self, store):
    cmd("set_default_peek")
    summary = {"upload_dir": 1}
    self._data = store._data
    return sys.platform == "win32", int(self.count)
'''
        self.assertEqual(_scan_source(source), [])

    def test_plugins_do_not_import_other_plugins(self):
        source = "from plugins.b.store import x\nfrom plugins import b\nfrom ..b import y\nimport plugins.b.draw\n"
        self.assertEqual(_layering("plugins/a/handlers.py", source), ["plugin-cross-import"] * 4)
        own = "from plugins.a.store import x\nfrom . import store\nfrom .sub import y\nfrom ..a import z\n"
        self.assertEqual(_layering("plugins/a/sub/view.py", own), [])

    def test_only_the_boundary_and_application_import_the_boundary(self):
        source = ("from otae_bot.boundary import SERVER_PLUGIN\nfrom otae_bot import boundary\n"
                  "from satori.adapters.onebot11 import message\nimport entari_plugin_server\n")
        self.assertEqual(_layering("plugins/a/handlers.py", source), ["boundary-import"] * 4)
        self.assertEqual(_layering("otae_bot/delivery/send.py", source), ["boundary-import"] * 4)
        self.assertEqual(_layering("otae_bot/application.py", source), [])
        self.assertEqual(_layering("otae_bot/boundary/onebot11.py", source), [])
        self.assertEqual(_layering("plugins/a/handlers.py", "from satori import Event\nimport satori.element\n"), [])

    def test_infrastructure_and_config_import_nothing_above_them(self):
        source = ("from arclet.entari import Session\nimport arclet.letoderea\nfrom satori import Event\n"
                  "from plugins.a import x\nfrom otae_bot.pipeline.message_log import redact\n"
                  "from otae_bot import paths\n")
        found = _layering("otae_bot/infrastructure/http/client.py", source)
        self.assertEqual(found.count("infrastructure-upward-import"), 6)
        self.assertEqual(_layering("otae_bot/config/settings.py", source).count("infrastructure-upward-import"), 6)
        allowed = ("from otae_bot.infrastructure.http import client\nfrom otae_bot.config.settings import Config\n"
                   "from . import storage\nfrom ..cache import x\nimport satori_like\nimport httpx\n")
        self.assertEqual(_layering("otae_bot/infrastructure/rendering/pages.py", allowed), [])
        self.assertEqual(_layering("otae_bot/delivery/send.py", "from arclet.entari import Session\n"), [])
        # Only otae_bot/config may bind to Entari's config API, and only to that.
        config_api = "from arclet.entari.config import EntariConfig\nimport arclet.entari.config.model\n"
        self.assertEqual(_layering("otae_bot/config/settings.py", config_api), [])
        self.assertEqual(_layering("otae_bot/infrastructure/http/client.py", config_api).count(
            "infrastructure-upward-import"), 2)

    def test_core_does_not_import_plugins(self):
        source = "from plugins.a import x\nimport plugins\nfrom plugins import a\n"
        self.assertEqual(_layering("otae_bot/groups/features.py", source), ["core-imports-plugins"] * 3)
        self.assertEqual(_layering("otae_bot/boundary/onebot11.py", source), ["core-imports-plugins"] * 3)


if __name__ == "__main__":
    unittest.main()
