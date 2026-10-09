"""Stable Arknights plugin resource locations.

The Arknights plugin is intentionally independent from ``plugins.endfield``:
importing any ``plugins.endfield`` module would execute that package's
entrypoint and register its commands.  Only the shared ``otae_bot.*``
infrastructure is reused.
"""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path("data") / "arknights"
DB_PATH = DATA_DIR / "arknights.db"
