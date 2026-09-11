from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "endfield_daily_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[PACKAGE] = package

crypto = _load(f"{PACKAGE}.account.crypto", "plugins/endfield/account/crypto.py")
store_module = _load(f"{PACKAGE}.account.store", "plugins/endfield/account/store.py")
xhh_module = _load(f"{PACKAGE}.gacha.xhh", "plugins/endfield/gacha/xhh.py")
client_module = _load(f"{PACKAGE}.account.client", "plugins/endfield/account/client.py")
currency_module = _load(f"{PACKAGE}.account.currency.service", "plugins/endfield/account/currency/service.py")
gacha_module = _load(f"{PACKAGE}.gacha.service", "plugins/endfield/gacha/service.py")
models_module = _load(f"{PACKAGE}.catalog.models", "plugins/endfield/catalog/models.py")
aliases_module = _load(f"{PACKAGE}.catalog.aliases", "plugins/endfield/catalog/aliases.py")
sources_module = _load(f"{PACKAGE}.providers.registry", "plugins/endfield/providers/registry.py")
commands_module = _load(f"{PACKAGE}.catalog.commands", "plugins/endfield/catalog/commands.py")
account_detail_models_module = _load(
    f"{PACKAGE}.account.detail.models", "plugins/endfield/account/detail/models.py"
)
account_detail_module = _load(
    f"{PACKAGE}.account.detail.service", "plugins/endfield/account/detail/service.py"
)


class DailyCommandParseTests(unittest.TestCase):
    def test_daily_defaults_to_all_accounts(self):
        for text in ("日常", "每日", "仪表盘", "实时数据", "dashboard", "DAILY"):
            parsed = commands_module.parse_command(text)
            self.assertEqual(parsed.action, "daily", text)
            self.assertEqual(parsed.account_selector, "全部", text)

    def test_daily_accepts_account_selector(self):
        parsed = commands_module.parse_command("日常 2")
        self.assertEqual((parsed.action, parsed.account_selector), ("daily", "2"))
        parsed = commands_module.parse_command("daily UID1234")
        self.assertEqual((parsed.action, parsed.account_selector), ("daily", "UID1234"))

    def test_daily_help_mentions_command(self):
        self.assertIn("/ef 日常", commands_module.format_help())


class EndfieldActionDispatchTests(unittest.TestCase):
    """Guard the dispatch gate in ``_handle_command`` against silent dead branches.

    ``/ef 日常`` regressed because ``_handle_daily`` was implemented but ``"daily"``
    was never added to the dispatcher's action set, so the parser produced a valid
    command that fell through to ``format_unknown()``.  Any action the parser can
    emit must either be handled by the dispatcher or be an explicit fallback.
    """

    # Actions the dispatcher resolves without a dedicated branch: they are matched
    # by the early help/source/calendar/dev/alias/quick_calc/loadout checks, by the
    # query/search tail, or handled as an error before dispatch.
    FALLBACK_ACTIONS = {
        "invalid", "help", "source", "calendar", "dev", "alias",
        "quick_calc", "query", "search", "loadout",
    }

    @staticmethod
    def _emitted_actions() -> set[str]:
        tree = ast.parse((ROOT / "plugins/endfield/catalog/commands.py").read_text(encoding="utf-8"))
        actions: set[str] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "ParsedEndfieldCommand":
                continue
            for keyword in node.keywords:
                if keyword.arg != "action":
                    continue
                if isinstance(keyword.value, ast.Constant):
                    actions.add(keyword.value.value)
                elif isinstance(keyword.value, ast.IfExp):
                    for branch in (keyword.value.body, keyword.value.orelse):
                        if isinstance(branch, ast.Constant):
                            actions.add(branch.value)
            if node.args and isinstance(node.args[0], ast.Constant):
                actions.add(node.args[0].value)
        return actions

    @staticmethod
    def _dispatched_actions() -> set[str]:
        tree = ast.parse((ROOT / "plugins/endfield/handlers.py").read_text(encoding="utf-8"))
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_command"
        )
        actions: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    if isinstance(op, (ast.In, ast.Eq, ast.NotIn, ast.NotEq)):
                        actions.add(comparator.value)
                elif isinstance(comparator, ast.Set):
                    for element in comparator.elts:
                        if isinstance(element, ast.Constant):
                            actions.add(element.value)
        return actions

    def test_every_parsed_action_is_dispatchable(self):
        unreachable = sorted(
            self._emitted_actions() - self._dispatched_actions() - self.FALLBACK_ACTIONS
        )
        self.assertEqual(unreachable, [], f"parsed but never dispatched: {unreachable}")

    def test_daily_action_is_dispatched(self):
        self.assertIn("daily", self._dispatched_actions())


class DailyAccountViewTests(unittest.TestCase):
    def _build(self, detail, *, nickname="储备昵称", server_name=""):
        return account_detail_module.build_daily_account_view(
            detail, nickname=nickname, uid="****1234", server_name=server_name,
        )

    def test_extracts_metrics_from_full_payload(self):
        detail = {
            "currentTs": 1_000_000,
            "base": {"name": "管理员", "level": 60, "avatarUrl": "https://example.com/a.png"},
            "dungeon": {"curStamina": "46", "maxStamina": 360, "maxTs": 1_013_200},
            "dailyMission": {"dailyActivation": 100, "maxDailyActivation": 100},
            "weeklyMission": {"score": 10, "total": 10},
            "bpSystem": {"curLevel": 45, "maxLevel": 60},
        }
        view = self._build(detail)
        self.assertEqual(view.nickname, "管理员")
        self.assertEqual(view.account_level, 60)
        self.assertEqual(view.avatar_url, "https://example.com/a.png")
        self.assertEqual((view.stamina_current, view.stamina_max), (46, 360))
        self.assertEqual(view.stamina_recover_text, "3 小时 40 分回满")
        self.assertEqual((view.daily_current, view.daily_max), (100, 100))
        self.assertEqual((view.weekly_current, view.weekly_max), (10, 10))
        self.assertEqual((view.bp_level, view.bp_max), (45, 60))
        self.assertEqual(view.status, "ok")

    def test_full_stamina_shows_recovered(self):
        detail = {
            "currentTs": 2_000_000,
            "dungeon": {"curStamina": 360, "maxStamina": 360, "maxTs": 1_000_000},
        }
        view = self._build(detail)
        self.assertEqual(view.stamina_recover_text, "已回满")

    def test_missing_modules_stay_none(self):
        view = self._build({"base": {"name": ""}})
        self.assertEqual(view.nickname, "储备昵称")
        self.assertIsNone(view.account_level)
        self.assertIsNone(view.stamina_current)
        self.assertIsNone(view.stamina_max)
        self.assertEqual(view.stamina_recover_text, "")
        self.assertIsNone(view.daily_current)
        self.assertIsNone(view.daily_max)
        self.assertIsNone(view.weekly_current)
        self.assertIsNone(view.bp_level)
        self.assertEqual(view.status, "ok")


if __name__ == "__main__":
    unittest.main()
