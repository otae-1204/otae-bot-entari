"""Render deterministic offline fixtures in a selected worktree; no remote I/O."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch


async def render():
    import test_endfield_visual as fixture
    from benchmark_endfield_queries import signature
    from otae_bot.infrastructure.rendering.browser import close_browser
    from otae_bot.infrastructure.rendering.executor import close_image_executor
    from loguru import logger

    logger.remove()
    result = {}
    try:
        with patch.object(
            fixture.draw, "fetch_many_resilient", AsyncMock(return_value=({}, {}))
        ):
            for kind in ("operator", "weapon"):
                for density in ("dense", "sparse"):
                    view = getattr(fixture, f"_{kind}_sample")(dense=density == "dense")
                    png = await getattr(fixture.draw, f"draw_{kind}_card")(view)
                    result[f"{kind}_{density}"] = {
                        "image": signature(png),
                        "visual_signature": fixture._visual_signature(png),
                    }
    finally:
        await close_browser()
        await close_image_executor()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    sys.path[:0] = [str(repo), str(repo / "tests")]
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="endfield-visual-check-") as directory:
        os.chdir(directory)
        try:
            result = asyncio.run(render())
        finally:
            os.chdir(previous)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
