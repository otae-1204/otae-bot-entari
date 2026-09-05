"""Boundaries and runtime contracts that can regress when modules are moved."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from otae_bot import application, lifecycle
from otae_bot.plugin_registry import discover_plugins


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_shared_code_does_not_import_feature_plugins(self):
        for path in (ROOT / "otae_bot").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name == "plugins" or name.startswith("plugins."), (path, name))

    def test_business_models_and_view_builders_do_not_register_events(self):
        paths = list((ROOT / "plugins/endfield").rglob("models.py"))
        paths.extend((ROOT / "plugins/endfield/catalog/views").glob("*.py"))
        paths.append(ROOT / "plugins/endfield/account/challenge/parsing.py")
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(
                        module.startswith(("arclet.entari", "otae_bot.adapters"))
                        or "rendering" in module.split("."),
                        (path, module),
                    )

    def test_plugin_entrypoints_only_import_handlers(self):
        for name in discover_plugins(ROOT / "plugins"):
            path = ROOT / name.replace(".", "/") / "__init__.py"
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                self.assertIsInstance(node, (ast.Expr, ast.ImportFrom), path)
                if isinstance(node, ast.ImportFrom):
                    self.assertEqual((node.level, node.module), (1, "handlers"), path)
                else:
                    self.assertIsInstance(node.value, ast.Constant, path)

    def test_shared_compatibility_imports_preserve_singletons(self):
        aliases = {
            "configs.config": "otae_bot.config.settings",
            "configs.path_config": "otae_bot.config.paths",
            "utils.http_client": "otae_bot.infrastructure.http.client",
            "utils.async_cache": "otae_bot.infrastructure.cache",
            "utils.entari_native": "otae_bot.adapters.entari",
            "utils.runtime": "otae_bot.adapters.runtime",
            "utils.image_executor": "otae_bot.infrastructure.rendering.executor",
            "utils.image_utils": "otae_bot.infrastructure.rendering.browser",
            "utils.json_store": "otae_bot.infrastructure.storage.json_store",
        }
        for old, new in aliases.items():
            with self.subTest(old=old):
                self.assertIs(importlib.import_module(old), importlib.import_module(new))
        from utils.image_utils import Cv2BuildImage, PILBuildImage
        from otae_bot.infrastructure.rendering.opencv import Cv2BuildImage as Cv2
        from otae_bot.infrastructure.rendering.pillow import PILBuildImage as Pillow

        self.assertIs(Cv2BuildImage, Cv2)
        self.assertIs(PILBuildImage, Pillow)

    def test_resource_paths_are_independent_of_feature_depth(self):
        from plugins.endfield import paths
        from plugins.endfield.catalog import aliases
        from plugins.endfield.calendar import akedata
        from plugins.endfield.account import draw
        from plugins.endfield.account.challenge import draw as challenge
        from plugins.endfield.rendering import cards

        plugin_root = ROOT / "plugins/endfield"
        self.assertEqual(paths.PROJECT_ROOT, ROOT)
        self.assertEqual(aliases.ALIAS_DATA_PATH, plugin_root / "alias_data.json")
        self.assertEqual(akedata.CALENDAR_DIR, plugin_root / "assets/calendar")
        self.assertEqual(draw.UI_ASSET_ROOT, plugin_root / "assets/ui")
        self.assertEqual(challenge.POTENTIAL_ICON_DIR, plugin_root / "assets/ui")
        self.assertEqual(cards.ASSET_DIR, ROOT / "assets/image/endfield")

    def test_discovery_ignores_resources_and_nested_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("zeta", "alpha", "resource_only", "alpha/nested"):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ("zeta", "alpha", "alpha/nested"):
                (root / name / "__init__.py").touch()
            self.assertEqual(discover_plugins(root), ("plugins.alpha", "plugins.zeta"))

    def test_run_lock_blocks_a_second_process_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = lifecycle.acquire_run_lock(Path(directory))
            self.assertIsNotNone(lock)
            self.addCleanup(lock.close)
            child = (
                "from pathlib import Path; import sys; "
                "from otae_bot.lifecycle import acquire_run_lock; "
                "lock = acquire_run_lock(Path(sys.argv[1])); "
                "raise SystemExit(2 if lock is None else 0)"
            )
            result = subprocess.run([sys.executable, "-c", child, directory], cwd=ROOT, timeout=20)
            self.assertEqual(result.returncode, 2)
            lock.close()
            result = subprocess.run([sys.executable, "-c", child, directory], cwd=ROOT, timeout=20)
            self.assertEqual(result.returncode, 0)

    def test_main_releases_lock_when_application_creation_fails(self):
        lock = Mock()
        with patch.object(application, "acquire_run_lock", return_value=lock):
            with patch.object(application, "create_app", side_effect=RuntimeError("startup failed")):
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    application.main()
        lock.close.assert_called_once_with()

    def test_network_configuration_preserves_defaults_and_multiple_clients(self):
        with patch.object(application, "WS", side_effect=lambda **values: values):
            self.assertEqual(application.build_networks([]), [
                {"host": "localhost", "port": 5500, "path": "", "token": None},
            ])
            networks = application.build_networks([
                {"host": "one", "port": "5501", "token": "test-token"},
                {"host": "two", "port": 5502, "path": "/satori", "token": ""},
            ])
        self.assertEqual(networks[0], {"host": "one", "port": 5501, "path": "", "token": "test-token"})
        self.assertEqual(networks[1], {"host": "two", "port": 5502, "path": "/satori", "token": None})

    def test_scheduler_uses_the_scope_registering_imported_service_methods(self):
        from otae_bot.adapters import entari

        plugin = SimpleNamespace(module=SimpleNamespace(__name__="plugins.demo.handlers"))
        token = entari.current_plugin.set(plugin)
        try:
            self.assertEqual(entari._plugin_module("plugins.demo.service"), "plugins.demo.handlers")
        finally:
            entari.current_plugin.reset(token)

    def test_all_plugins_load_in_real_entari_without_connecting(self):
        script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from otae_bot.application import create_app
from otae_bot.plugin_registry import discover_plugins
from otae_bot.adapters.entari import timer
from arclet.entari.plugin.service import plugin_service
from arclet.alconna import command_manager

create_app()
expected = set(discover_plugins())
missing = sorted(expected - plugin_service.plugins.keys())
assert not missing, missing
jobs = sorted(timer._jobs)
for name in ('bili_live_check', 'bili_video_check', 'bili_dynamic_check',
             'endfield_ownership_refresh', 'endfield_ownership_catalog_refresh',
             'tibo_radar_refresh'):
    assert name in jobs, (name, jobs)
for job in timer._jobs.values():
    assert job.subscriber is not None
print('CONTRACT ' + json.dumps({'plugins': sorted(expected), 'jobs': jobs}, ensure_ascii=False))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "assets", root / "assets")
            # Discover top-level names in the sandbox; imports resolve to source.
            for name in discover_plugins(ROOT / "plugins"):
                package = root / name.replace(".", "/")
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
            result = subprocess.run(
                [sys.executable, "-c", script, str(ROOT)], cwd=root,
                capture_output=True, text=True, timeout=45,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        contract = next(line.removeprefix("CONTRACT ") for line in result.stdout.splitlines() if line.startswith("CONTRACT "))
        self.assertEqual(len(json.loads(contract)["plugins"]), 13)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_stop_before_shared_resources_close(self):
        order = []

        async def stop_jobs():
            order.append("jobs")

        async def close_resource():
            self.assertEqual(order[0], "jobs")
            order.append("resource")

        with (
            patch.object(lifecycle, "close_scheduled_jobs", AsyncMock(side_effect=stop_jobs)),
            patch.object(lifecycle, "close_http_client", AsyncMock(side_effect=close_resource)),
            patch.object(lifecycle, "close_image_executor", AsyncMock(side_effect=close_resource)),
            patch.object(lifecycle, "close_browser", AsyncMock(side_effect=close_resource)),
        ):
            await lifecycle.close_shared_resources()
        self.assertEqual(order, ["jobs", "resource", "resource", "resource"])
