"""Aspect-ratio helpers shared by the help artwork chooser.

Each help page ships one pre-rendered PNG (`assets/image/help/<name>.png`) that acts as
the offline fallback. The artwork actually sent is picked by
`otae_bot.infrastructure.rendering.help_cards.ratio_candidates` and rendered on demand
by `otae_bot.infrastructure.rendering.help_runtime`. The helpers here are the geometry
behind that choice: how much of an artwork survives `object-fit: cover` into a page box,
which is what decides whether the sharp standee window still shows the subject.
"""

from __future__ import annotations

from functools import lru_cache

#: Legacy directory for pre-rendered per-artwork variants; only used to clean them up.
VARIANT_DIR_NAME = "variants"


@lru_cache(maxsize=64)
def image_ratio(path: str) -> float:
    """Width / height of an image, read from the header only."""
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    return width / height if height else 1.0


def cover_visibility(source_ratio: float, target_ratio: float) -> float:
    """Fraction of an image left visible by `object-fit: cover` in a target box."""
    if source_ratio <= 0 or target_ratio <= 0:
        return 0.0
    return min(source_ratio, target_ratio) / max(source_ratio, target_ratio)
