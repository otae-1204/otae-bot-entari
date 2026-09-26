"""Choose which pre-rendered help image to send.

Every help topic ships one primary PNG (``assets/image/help/<name>.png``). A topic that
lists several artworks also gets variants under ``assets/image/help/variants/<name>/``;
each send picks one of them at random so the same help page can show different art.
"""

from __future__ import annotations

import random
from pathlib import Path

VARIANT_DIR_NAME = "variants"


def variant_dir(primary: Path) -> Path:
    return primary.parent / VARIANT_DIR_NAME / primary.stem


def help_image_choices(primary: Path) -> list[Path]:
    """The primary image first, then its variants; empty when the primary is missing."""
    if not primary.is_file():
        return []
    return [primary, *sorted(variant_dir(primary).glob("*.png"))]


def pick_help_image(primary: Path, rng: random.Random | None = None) -> Path | None:
    choices = help_image_choices(primary)
    if not choices:
        return None
    return (rng or random).choice(choices)
