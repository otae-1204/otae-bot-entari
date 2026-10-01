"""Render the same merged PNG sent by announcement subscriptions, without sending.

Examples:
    python scripts/preview_endfield_announcements.py
    python scripts/preview_endfield_announcements.py --file bulletin.json --at 2026-10-01T11:00 --phase start
    python scripts/preview_endfield_announcements.py --file bulletin.json --at 2026-10-01T12:00 --phase started
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Load only business modules; a preview must not register or start the bot.
package = ModuleType("_endfield_announcement_preview")
package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules[package.__name__] = package
models = importlib.import_module(f"{package.__name__}.announcements.models")
source = importlib.import_module(f"{package.__name__}.announcements.source")
presentation = importlib.import_module(f"{package.__name__}.announcements.presentation")
rendering = importlib.import_module(f"{package.__name__}.announcements.rendering")


async def render(args):
    from otae_bot.infrastructure.http.client import close_http_client
    from otae_bot.infrastructure.rendering.browser import close_browser

    at = datetime.fromisoformat(args.at) if args.at else datetime.now(models.BEIJING)
    if at.tzinfo is None:
        at = at.replace(tzinfo=models.BEIJING)
    now = int(at.timestamp())
    try:
        if args.file:
            articles = [
                source.parse_article(json.loads(path.read_text(encoding="utf-8")))
                for path in args.file
            ]
        else:
            articles = await source.OfficialAnnouncementSource().fetch(now=now)
        jobs = []
        for article in articles:
            if args.phase == "news":
                jobs.append(
                    models.Delivery(
                        article.cid, "preview", article.cid, "", now, now + 7200
                    )
                )
                continue
            for window in article.windows:
                if args.phase == "started":
                    if (
                        window.kind in {"activity", "banner", "signin"}
                        and not window.schedule_label
                        and window.start_at == now
                        and (not window.end_at or window.end_at > now)
                    ):
                        jobs.append(
                            models.Delivery(
                                window.key,
                                "preview",
                                article.cid,
                                "",
                                now,
                                min(now + 7200, window.end_at or now + 7200),
                                window_key=window.key,
                                phase="started",
                            )
                        )
                    continue
                target = window.end_at if args.phase == "end" else window.start_at
                lead = 1440 if args.phase == "end" else 60
                phase = "maintenance" if window.kind == "maintenance" else args.phase
                if target and target - lead * 60 <= now < target:
                    jobs.append(
                        models.Delivery(
                            window.key,
                            "preview",
                            article.cid,
                            "",
                            now,
                            target,
                            window_key=window.key,
                            phase=phase,
                        )
                    )
        digest = presentation.build_digest(articles, jobs, tuple(models.KINDS), now)
        if args.limit:
            digest = models.AnnouncementDigest(digest.cards[: args.limit], now)
        if not digest.cards:
            raise SystemExit(
                "当前预览时间没有符合条件的活动；可调整 --at 或使用 --phase news。"
            )
        png = await rendering.draw_digest(digest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(png)
        print(
            f"Rendered {len(digest.cards)} activities at {models.local_time(now)} -> {args.output.resolve()} ({len(png)} bytes)"
        )
    finally:
        await close_browser()
        await close_http_client()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        action="append",
        type=Path,
        help="Official CMS detail JSON; repeat for multiple announcements",
    )
    parser.add_argument(
        "--at", help="Preview timestamp in ISO format; defaults to now in Beijing"
    )
    parser.add_argument(
        "--phase", choices=("news", "start", "started", "end"), default="news"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit preview entries only; 0 keeps all"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/endfield/announcements/previews/activity-digest.png",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or a positive integer")
    asyncio.run(render(args))


if __name__ == "__main__":
    main()
