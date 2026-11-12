"""Platform protocol code may only live in otae_bot/boundary/ (report-only for now).

Existing violations are pinned in tests/fixtures/boundary_guard_baseline.json;
see tests/support/boundary_guard.py for the rules and the --report/--write commands.
"""

from __future__ import annotations

import ast
from collections import Counter
import unittest

from tests.support import boundary_guard as guard

REGENERATE = "python -m tests.support.boundary_guard --write"


def _scan_source(source: str) -> list[str]:
    return sorted(v.rule for v in guard._Scanner("x.py", source).run(ast.parse(source)))


class BoundaryGuardTests(unittest.TestCase):
    def test_no_new_violations_and_baseline_is_current(self):
        violations = guard.scan()
        new, gone = guard.compare(violations, guard.load_baseline())
        self.assertFalse(new, "new boundary violations (move the code into otae_bot/boundary/):\n"
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


if __name__ == "__main__":
    unittest.main()
