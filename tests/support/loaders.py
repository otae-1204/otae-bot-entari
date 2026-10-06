"""Load plugin modules from source under synthetic package names.

Tests use these instead of a plain import when they need a fresh copy of a
module (its own module-level state) without importing the plugin's
``__init__`` and the Entari wiring it pulls in.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_bili_subpackage(package: str, name: str):
    """Load the directory package `plugins/bilibilibot/<name>/` as `<package>.<name>`.

    `_load_module` builds a spec for a single file; a package also needs its
    submodule search location, otherwise the relative imports inside
    `__init__.py` (`from ..models import ...`, `from .session import ...`)
    cannot resolve.
    """
    target = f"{package}.{name}"
    if target in sys.modules:
        return sys.modules[target]
    directory = ROOT / "plugins/bilibilibot" / name
    spec = importlib.util.spec_from_file_location(
        target,
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[target] = module
    spec.loader.exec_module(module)
    return module


def _bili_root_package(module) -> str:
    """The synthetic root package a loaded bilibilibot module belongs to.

    `api` is a real package, so its own `__package__` points one level deeper
    than the root every loader-made name starts with.
    """
    return module.__name__.split(".", 1)[0]


def _load_bili_new_module(module_name: str):
    pkg_name = f"bilibilibot_new_for_test_{module_name}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/bilibilibot")]
    sys.modules[pkg_name] = pkg
    models = _load_module(f"{pkg_name}.models", "plugins/bilibilibot/models.py")
    sys.modules[f"{pkg_name}.models"] = models
    if module_name == "models":
        return models
    if module_name == "api":
        return _load_bili_subpackage(pkg_name, "api")
    return _load_module(f"{pkg_name}.{module_name}", f"plugins/bilibilibot/{module_name}.py")


def _load_in_package(package: str, name: str):
    """Load one more module inside the synthetic package the loader created."""
    key = f"{package}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    return _load_module(key, f"plugins/bilibilibot/{name}.py")
