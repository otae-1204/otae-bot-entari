"""Run the random Wiki command and save its actual reply images locally."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arclet.entari import Image

from otae_bot.infrastructure.http.client import close_http_client
from otae_bot.infrastructure.rendering.browser import close_browser
from otae_bot.infrastructure.rendering.executor import (
    close_image_executor,
)
from plugins.endfield import handlers
from plugins.endfield.catalog.commands import parse_command
from plugins.endfield.catalog.random_wiki import RandomWikiService


class PreviewMatcher:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.messages: list[str] = []
        self.images: list[str] = []

    async def send(self, message) -> None:
        self.messages.append(str(message))

    async def finish(self, message=None) -> None:
        if isinstance(message, str) or message is None:
            if message:
                self.messages.append(message)
            return
        for element in message:
            if isinstance(element, Image):
                uri = urlparse(element.src)
                if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
                    raise ValueError("Preview expects a local rendered image")
                destination = self.directory / f"reply-{len(self.images) + 1:02}.png"
                shutil.copyfile(Path(url2pathname(uri.path)), destination)
                self.images.append(str(destination))
            else:
                self.messages.append(str(element))


class PreviewSelection(RandomWikiService):
    def __init__(self, seed: int | None) -> None:
        super().__init__(
            handlers.service,
            handlers.stage_service,
            lambda: handlers.archive_store.load_current_view(),
            rng=random.Random(seed) if seed is not None else None,
        )
        self.attempts: list[dict] = []

    async def candidates(self):
        async for candidate in super().candidates():
            self.attempts.append(asdict(candidate))
            yield candidate


async def run(args: argparse.Namespace) -> int:
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    matcher = PreviewMatcher(directory)
    selection = PreviewSelection(args.seed)
    original_service = handlers.random_wiki_service
    handlers.random_wiki_service = selection
    try:
        # No user identity, bot connection, or QQ destination is supplied.
        await handlers._handle_command(matcher, object(), parse_command("随机"))
        report = {
            "command": "/zmd 随机",
            "created_at": datetime.now().astimezone().isoformat(),
            "seed": args.seed,
            "data_mode": "public providers and existing public caches",
            "sent_to_qq": False,
            "attempts": selection.attempts,
            "messages": matcher.messages,
            "images": matcher.images,
            "ok": bool(matcher.images),
        }
        (directory / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if matcher.images else 1
    finally:
        handlers.random_wiki_service = original_service
        await close_http_client()
        await close_browser()
        await close_image_executor()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", type=int, help="Optional seed for repeatable selection"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT
        / "data/endfield/random-wiki/previews"
        / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S"),
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
