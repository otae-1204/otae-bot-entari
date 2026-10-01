"""Replay official samples through polling, filtering, delivery and PNG rendering.

Uses isolated databases and an in-memory receiver, never a connected bot.
Public cover/avatar URLs are loaded by the production renderer.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("_bilibili_official_preview")
package.__path__ = [str(ROOT / "plugins/bilibilibot")]
sys.modules[package.__name__] = package


def module(name):
    return importlib.import_module(f"{package.__name__}.{name}")


async def replay(output: Path, samples: Path, captured: Path | None = None):
    from otae_bot.endfield_notifications.receipts import ReceiptStore, destination_key
    from otae_bot.infrastructure.http.client import close_http_client

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    names = ("livestream", "activity", "lottery_start", "lottery_result")
    items = {
        name: json.loads((samples / f"{name}.json").read_text(encoding="utf-8"))["item"]
        for name in names
    }
    if captured:
        aliases = dict(zip(names, ("poDSIOq", "aiWOiYa", "hOq2yiT", "gdFkQiA")))
        items = {
            name: json.loads(
                (captured / f"{alias}.opus-detail.json").read_text(encoding="utf-8")
            )["data"]["item"]
            for name, alias in aliases.items()
        }
    cards = {
        name: module("api.mapping").dynamic_item_to_card(item, "1265652806")
        for name, item in items.items()
    }
    now = max(card.published_at for card in cards.values()) + 1
    classifications = [
        {
            "sample": name,
            "id": card.item_id,
            "category": card.content_category,
            "tags": card.content_tags,
            "filtered": module("dynamic_filter").suppress_dynamic(card),
        }
        for name, card in cards.items()
    ]
    assert [item["sample"] for item in classifications if not item["filtered"]] == [
        "livestream",
        "activity",
    ]
    received = []
    by_id = {card.item_id: name for name, card in cards.items()}
    try:
        with tempfile.TemporaryDirectory(prefix="business-", dir=output) as scratch:
            path = Path(scratch).resolve()
            assert path.is_relative_to(output)
            store = module("store").BiliStore(path / "bilibili.db", path / "missing.db")
            receipts = ReceiptStore(path / "receipts.db")
            delivery = module("official_delivery").OfficialDelivery(
                receipts, clock=lambda: now
            )
            api = SimpleNamespace(
                dynamic_items=AsyncMock(
                    return_value=list(reversed(list(items.values())))
                )
            )
            await store.open()
            try:
                await store.upsert_target(
                    module("models").TargetInfo("dynamic", "1265652806")
                )
                for group in ("preview-a", "preview-b"):
                    await store.add_subscription(
                        "dynamic", "1265652806", "group", group
                    )
                await (
                    module("poller")
                    .Poller(api, store, clock=lambda: now, intervals={"dynamic": 0})
                    .tick_dynamic()
                )
                assert await store.outbox_count() == 4
            finally:
                await store.close()
            # Restart before delivery: only eligible rows should survive.
            store = module("store").BiliStore(path / "bilibili.db", path / "missing.db")
            await store.open()
            try:

                async def send(row, png):
                    card = row.card()

                    async def receiver(selected):
                        image = (
                            png
                            if selected is card
                            else await module("draw").draw_bili_card(selected)
                        )
                        name = by_id[selected.item_id]
                        (output / f"{name}.png").write_bytes(image)
                        received.append(
                            {
                                "group": row.subscriber_id,
                                "sample": name,
                                "png": f"{name}.png",
                            }
                        )

                    await delivery.deliver(
                        card,
                        destination_key("preview", "preview-bot", row.subscriber_id),
                        receiver,
                    )

                notifier = module("notifier").Notifier(
                    store,
                    render=module("draw").draw_bili_card,
                    send=send,
                    clock=lambda: now,
                )
                while await notifier._dispatch_once():
                    pass
                assert len(received) == 4 and await store.outbox_count() == 0
                await (
                    module("poller")
                    .Poller(api, store, clock=lambda: now, intervals={"dynamic": 0})
                    .tick_dynamic()
                )
                assert await store.outbox_count() == 0
            finally:
                await store.close()
        report = {
            "mode": "official sample replay, simulated recipients",
            "sample_source": "captured official responses"
            if captured
            else "committed excerpts",
            "source_sample_count": 4,
            "eligible_sample_count": 2,
            "filtered_sample_count": 2,
            "recipient_count": 2,
            "delivered_count": len(received),
            "pending_after_repoll": 0,
            "restart_recovery": "passed",
            "classification": classifications,
            "deliveries": received,
        }
        (output / "business-preview.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Previews: {output}")
    finally:
        await close_http_client()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/bilibilibot/previews/pr-2026-10-01"
    )
    parser.add_argument(
        "--samples",
        type=Path,
        default=ROOT / "tests/fixtures/bilibili/endfield_official",
    )
    parser.add_argument(
        "--captured",
        type=Path,
        help="Directory containing the four saved official detail responses",
    )
    args = parser.parse_args()
    asyncio.run(replay(args.output, args.samples, args.captured))


if __name__ == "__main__":
    main()
