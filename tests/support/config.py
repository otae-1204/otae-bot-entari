"""Load a throwaway entari.yml (and .env) through the real loader, then restore everything."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from arclet.entari.config import EntariConfig

from otae_bot.config import settings


@contextlib.contextmanager
def loaded_config_file(yml: str, env: str | None = None) -> Iterator[tuple[EntariConfig, Path]]:
    """``load_config`` on ``yml`` in a temporary working directory; yields (config, directory)."""
    missing = object()
    previous = getattr(EntariConfig, "instance", missing)
    with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory), \
            patch.dict(os.environ), settings.override({}):
        root = Path(directory)
        (root / "entari.yml").write_text(yml, encoding="utf-8")
        if env is not None:
            (root / ".env").write_text(env, encoding="utf-8")
        try:
            yield settings.load_config(), root
        finally:
            if previous is missing:
                # Entari only creates its instance once per process; leave a blank one.
                (root / ".env").unlink(missing_ok=True)
                EntariConfig.load(root / "absent.yml")
            else:
                EntariConfig.instance = previous
