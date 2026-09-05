"""Deterministic discovery of top-level Entari plugin packages."""

from pathlib import Path


def discover_plugins(directory: Path = Path("plugins")) -> tuple[str, ...]:
    return tuple(
        f"plugins.{path.name}"
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "__init__.py").exists()
    )
