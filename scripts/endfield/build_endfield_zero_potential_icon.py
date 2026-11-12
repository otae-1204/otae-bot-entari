#!/usr/bin/env python3
"""Build the Endfield zero-potential display state from the first progress icon.

The game does not store this state as a standalone texture. Its
SimplePotentialStar prefab uses PotentialCell nodes: completed cells are white,
the next cell is yellow, and inactive cells use a translucent gray.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


INACTIVE_CELL = (92, 92, 92, 81)
ACTIVE_CELL = (255, 210, 0)


def _cross(
    origin: tuple[int, int], first: tuple[int, int], second: tuple[int, int]
) -> int:
    return (first[0] - origin[0]) * (second[1] - origin[1]) - (
        first[1] - origin[1]
    ) * (second[0] - origin[0])


def _convex_hull(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(set(points))
    if len(ordered) <= 1:
        return ordered
    lower: list[tuple[int, int]] = []
    for point in ordered:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[int, int]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def build_zero_potential_icon(source: Path, output: Path) -> None:
    image = Image.open(source).convert("RGBA")
    result = image.copy()
    source_pixels = image.load()
    result_pixels = result.load()
    completed_cell_points: list[tuple[int, int]] = []

    for y_position in range(image.height):
        for x_position in range(image.width):
            red, green, blue, alpha = source_pixels[x_position, y_position]
            if not alpha:
                continue
            is_completed_cell = (
                min(red, green, blue) > 115
                and max(red, green, blue) - min(red, green, blue) < 35
            )
            is_next_cell = (
                red > 100
                and green > 75
                and blue < 110
                and red > blue * 1.45
                and green > blue * 1.2
            )
            if is_completed_cell:
                completed_cell_points.append((x_position, y_position))
            if is_completed_cell or is_next_cell:
                inactive_alpha = round(alpha * INACTIVE_CELL[3] / 255)
                result_pixels[x_position, y_position] = (
                    *INACTIVE_CELL[:3],
                    inactive_alpha,
                )

    hull = _convex_hull(completed_cell_points)
    if not hull:
        raise ValueError(f"No completed potential cell found in {source}")

    scale = 4
    active_mask = Image.new("L", (image.width * scale, image.height * scale), 0)
    ImageDraw.Draw(active_mask).polygon(
        [(x_position * scale, y_position * scale) for x_position, y_position in hull],
        fill=255,
    )
    active_mask = active_mask.resize(image.size, Image.Resampling.LANCZOS)
    active_mask = active_mask.filter(ImageFilter.GaussianBlur(0.35))

    shadow_mask = active_mask.filter(ImageFilter.GaussianBlur(2.0)).point(
        lambda value: round(value * 0.28)
    )
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_mask)
    result = Image.alpha_composite(result, shadow)

    active = Image.new("RGBA", image.size, (*ACTIVE_CELL, 0))
    active.putalpha(active_mask)
    result = Image.alpha_composite(result, active)

    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output, optimize=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build_zero_potential_icon(args.source, args.output)
    print(f"Wrote zero-potential icon: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
