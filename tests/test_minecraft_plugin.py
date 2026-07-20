from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dns.resolver import LifetimeTimeout


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MinecraftPluginTests(unittest.TestCase):
    def test_saved_identifiers_are_case_insensitive(self):
        data_source = _load_module(
            "minecraft_casefold_data_source_for_test",
            "plugins/minecraft_plugin/data_source.py",
        )
        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                self.assertTrue(manager.add_group_server("10001", "short-address", "nfwc2"))
                self.assertTrue(manager.update_server_nickname("10001", "nfwc2", "survival"))

                self.assertEqual(
                    manager.get_server_by_identifier("10001", "NFWC2")["address"],
                    "nfwc2",
                )
                self.assertEqual(
                    manager.get_server_by_nickname("10001", "SURVIVAL")["address"],
                    "nfwc2",
                )
            finally:
                os.chdir(cwd)

    def test_dns_timeout_returns_error_without_traceback(self):
        ping_module = _load_module(
            "minecraft_dns_timeout_ping_for_test",
            "plugins/minecraft_plugin/ping.py",
        )
        timeout = LifetimeTimeout(timeout=3, errors=[])

        with patch.object(ping_module, "_lookup_and_status", side_effect=timeout):
            result = asyncio.run(ping_module.ping("NFWC2", "java"))

        self.assertEqual(result["status"], "error")
        self.assertIn("DNS", result["data"])

    def test_ping_command_resolves_saved_identifier_first(self):
        source = (ROOT / "plugins/minecraft_plugin/__init__.py").read_text(encoding="utf-8")
        start = source.index("# 优先按已保存的地址、名称或昵称查找")
        end = source.index("r = await ping(command_args", start)
        lookup_source = source[start:end]

        self.assertIn("get_server_by_identifier(group_id, command_args)", lookup_source)
        self.assertNotIn("get_server_by_nickname(group_id, command_args)", lookup_source)


if __name__ == "__main__":
    unittest.main()
