"""Replay a saved official schedule through subscription, planning and image delivery.

Produces previews before/after a simulated peer receipt. No real bot is connected.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from preview_endfield_announcements import ROOT, models, rendering, source

runtime_module = importlib.import_module(
    "_endfield_announcement_preview.announcements.runtime"
)
store_module = importlib.import_module(
    "_endfield_announcement_preview.announcements.store"
)


async def scenario(output, articles, at, *, peer_sent):
    name = "opening-after-dedup" if peer_sent else "opening-activities"
    with tempfile.TemporaryDirectory(prefix=name + "-", dir=output) as scratch:
        folder = Path(scratch).resolve()
        assert folder.is_relative_to(output)
        store = store_module.AnnouncementStore(folder / "announcements.db")
        destination = models.Destination(
            "preview", "preview-bot", "preview-group", "preview-group"
        )
        store.subscribe(
            models.Subscription(
                destination,
                start_minutes=0,
                end_minutes=0,
                maintenance_minutes=0,
                created_at=at - 600,
            )
        )
        clock = [at - 600]
        provider = SimpleNamespace(fetch=AsyncMock(return_value=articles))
        bot = SimpleNamespace(
            platform="preview",
            self_id="preview-bot",
            protocol=SimpleNamespace(
                send_message=AsyncMock(return_value=[SimpleNamespace(id="preview-ack")])
            ),
        )
        rendered = []

        async def render(bulletin):
            png = await rendering.draw_digest(bulletin)
            (output / f"{name}.png").write_bytes(png)
            rendered.append(bulletin)
            return png

        runtime = runtime_module.AnnouncementRuntime(
            store, provider, clock=lambda: clock[0], renderer=render
        )
        runtime.remember(bot)
        removed = ""
        try:
            with patch.object(
                runtime_module.feature_store, "is_enabled", return_value=True
            ):
                await runtime.service.tick()
                assert bot.protocol.send_message.await_count == 0
                jobs = store.due(at, None)
                bulletin = store.digest(jobs, at)
                assert len(bulletin.cards) >= 2, (
                    "Choose a timestamp with at least two simultaneous activities"
                )
                assert all(
                    card.phases == ("started",) and card.start_at == at
                    for card in bulletin.cards
                )
                if peer_sent:
                    candidate = next(
                        (card for card in bulletin.cards if "山团团" in card.title),
                        bulletin.cards[0],
                    )
                    event = candidate.notification_events[0]
                    assert event is not None
                    claim = runtime.receipts.reserve(
                        destination.key, "bilibili", [event], at
                    )
                    runtime.receipts.finish(claim, at)
                    removed = candidate.title
                clock[0] = at
                await runtime.service.tick()
                assert bot.protocol.send_message.await_count == 1 and len(rendered) == 1
                assert len(rendered[0].cards) == len(bulletin.cards) - int(peer_sent)
                assert not store.due(at, None)
                await runtime.close()
                # Recreate the runtime to verify persisted queue/receipt behavior.
                clock[0] += 1
                runtime = runtime_module.AnnouncementRuntime(
                    store, provider, clock=lambda: clock[0], renderer=render
                )
                runtime.remember(bot)
                await runtime.service.tick()
                assert bot.protocol.send_message.await_count == 1 and len(rendered) == 1
        finally:
            await runtime.close()
        return {
            "scenario": name,
            "planned_activities": len(bulletin.cards),
            "delivered_activities": len(rendered[0].cards),
            "image_messages": bot.protocol.send_message.await_count,
            "simulated_peer_receipt": removed,
            "restart_duplicate_messages": 0,
            "activities": [card.title for card in rendered[0].cards],
        }


async def replay(args):
    from otae_bot.infrastructure.http.client import close_http_client
    from otae_bot.infrastructure.rendering.browser import close_browser

    at = datetime.fromisoformat(args.at)
    if at.tzinfo is None:
        at = at.replace(tzinfo=models.BEIJING)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    articles = [
        source.parse_article(json.loads(path.read_text(encoding="utf-8")))
        for path in args.file
    ]
    try:
        results = [
            await scenario(output, articles, int(at.timestamp()), peer_sent=flag)
            for flag in (False, True)
        ]
        report = {
            "mode": "official schedule replay, simulated group receiver",
            "at": at.isoformat(),
            "note": "The peer receipt is simulated; it does not claim the official Bilibili account posted an opening notice at this timestamp.",
            "results": results,
        }
        (output / "business-preview.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Previews: {output}")
    finally:
        await close_browser()
        await close_http_client()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        action="append",
        type=Path,
        required=True,
        help="Saved official CMS detail JSON; repeat for multiple articles",
    )
    parser.add_argument(
        "--at",
        required=True,
        help="Exact opening time in ISO format (Beijing by default)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/endfield/announcements/previews/pr-2026-10-01",
    )
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
