"""Regression guards for the MCSM subcommand router.

``/mcsm 重启``, ``强制结束``, ``强杀``, ``命令`` and the ``全部`` flag were silently
unreachable: the Chinese aliases had been corrupted from UTF-8 into GBK mojibake
(``'閲嶅惎'`` instead of ``'重启'``), so every Chinese form fell through to the
``Unknown command`` tail.  The English forms kept working, which is why the
regression stayed invisible for Chinese-speaking users.

These tests assert the router's literals are readable Chinese and that the
mojibake forms are gone, so an encoding-corrupting edit cannot come back silently.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCSM_HANDLERS = ROOT / "plugins/mcsm/handlers.py"
HANDLER_SOURCES = tuple(
    sorted(
        path
        for base in ("plugins", "otae_bot", "utils", "scripts")
        for path in (ROOT / base).rglob("*.py")
        if "__pycache__" not in path.parts
    )
)


def _mojibake_runs(text: str) -> list[tuple[str, str]]:
    """Return (corrupted, recovered) pairs for UTF-8-read-as-GBK text."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        value = match.group()
        try:
            recovered = value.encode("gbk").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if recovered != value and any("\u4e00" <= char <= "\u9fff" for char in recovered):
            found.append((value, recovered))
    return found


def _subcommand_tuples() -> dict[int, tuple[str, ...]]:
    """Collect the literal tuples compared against ``subcmd`` in handle_mcsm."""
    tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
    )
    literals: dict[int, tuple[str, ...]] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "subcmd"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Tuple):
                values = tuple(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
                if values:
                    literals[comparator.lineno] = values
    return literals


def _show_all_flags() -> tuple[str, ...]:
    """Collect the flag literals accepted by the `list` subcommand's show_all check.

    The source reads ``show_all = any(p in ("-a", "--all", "all", "全部") for p in parts[1:])``,
    so the comparison is against the loop variable ``p``; anchor on the assignment instead.
    """
    tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
    )
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "show_all" not in targets:
            continue
        for inner in ast.walk(node.value):
            if not isinstance(inner, ast.Compare):
                continue
            for comparator in inner.comparators:
                if isinstance(comparator, ast.Tuple):
                    values = tuple(
                        element.value
                        for element in comparator.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    )
                    if values:
                        return values
    return ()


class McsmMojibakeTests(unittest.TestCase):
    def test_no_mojibake_anywhere_in_shipped_python(self):
        offenders: list[str] = []
        for path in HANDLER_SOURCES:
            for corrupted, recovered in _mojibake_runs(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(f"{path.relative_to(ROOT)}: {corrupted!r} should be {recovered!r}")
        self.assertEqual(offenders, [], "mojibake reintroduced:\n" + "\n".join(offenders))

    def test_chinese_aliases_are_readable_and_reachable(self):
        literals = _subcommand_tuples()
        flat = {value for values in literals.values() for value in values}

        # The previously broken aliases must now match real user input.
        for alias in ("重启", "强制结束", "强杀", "命令"):
            self.assertIn(alias, flat, f"{alias!r} is missing from the subcommand router")

        # And the corrupted forms must be gone entirely.
        for corrupted in ("閲嶅惎", "寮哄埗缁撴潫", "寮烘潃", "鍛戒护", "鍏ㄩ儴"):
            self.assertNotIn(corrupted, flat, f"mojibake {corrupted!r} still routed on")

    def test_list_all_flag_is_readable(self):
        """`/mcsm list -a` accepts the Chinese `全部` flag alongside -a/--all/all."""
        flags = _show_all_flags()
        self.assertIn("全部", flags, "'全部' show-all flag is missing")
        for corrupted in ("鍏ㄩ儴",):
            self.assertNotIn(corrupted, flags, f"mojibake {corrupted!r} still accepted")
        for flag in ("-a", "--all", "all"):
            self.assertIn(flag, flags, f"English flag {flag!r} was dropped")

    def test_english_aliases_still_present(self):
        flat = {value for values in _subcommand_tuples().values() for value in values}
        for alias in ("list", "start", "stop", "restart", "kill", "cmd", "log", "deploy"):
            self.assertIn(alias, flat, f"English alias {alias!r} was dropped")

    def test_help_subcommand_is_reachable(self):
        """``help`` must be tested by a real branch, not sit after a return."""
        source = MCSM_HANDLERS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
        )
        tests = [ast.unparse(node.test) for node in ast.walk(func) if isinstance(node, ast.If)]
        self.assertTrue(
            any("help" in test for test in tests),
            "handle_mcsm has no `help` branch; the help tip is unreachable",
        )

    def test_no_unreachable_statements_in_handler(self):
        """The orphaned help tip used to sit after a return inside `if not subcmd`."""
        tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body[:-1]):
                if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    following = body[index + 1]
                    self.fail(
                        "unreachable statement at "
                        f"{MCSM_HANDLERS.name}:{following.lineno}: {ast.unparse(following)[:80]}"
                    )


class EftHelperRegistrationTests(unittest.TestCase):
    """The abandoned eft_helper package must not be advertised as switchable."""

    def test_empty_plugin_is_not_registered_as_group_feature(self):
        from otae_bot.group_features import PLUGIN_NAMES

        init = ROOT / "plugins/eft_helper/__init__.py"
        self.assertEqual(init.read_bytes().strip(), b"", "eft_helper gained an entrypoint; re-check this test")
        self.assertNotIn(
            "eft_helper",
            PLUGIN_NAMES,
            "eft_helper registers no commands but is still offered as a group switch",
        )


if __name__ == "__main__":
    unittest.main()
