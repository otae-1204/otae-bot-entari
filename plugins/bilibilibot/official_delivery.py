"""Cross-source receipts for the official account's automatic notifications."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from otae_bot.endfield_notifications.classification import classify
from otae_bot.endfield_notifications.receipts import ReceiptStore

from .models import BiliCard
from .notifier import SenderUnavailable


class OfficialDelivery:
    def __init__(self, receipts: ReceiptStore | None = None, *, clock=time.time):
        self.receipts = receipts or ReceiptStore()
        self.clock = clock

    async def deliver(
        self,
        card: BiliCard,
        destination: str,
        send: Callable[[BiliCard], Awaitable[None]],
    ) -> None:
        """`send` must raise unless the adapter acknowledges the selected image."""
        result = classify(card.title, card.description, card.published_at)
        claim = await asyncio.to_thread(
            self.receipts.reserve,
            destination,
            "bilibili",
            [unit.event for unit in result.units],
            int(self.clock()),
        )
        if claim.busy:
            raise SenderUnavailable("same Endfield event is being delivered")
        try:
            if not claim.accepted:
                return
            selected = card
            if len(claim.accepted) != len(result.units):
                kept = [result.units[index] for index in claim.accepted]
                selected = replace(
                    card,
                    title=kept[0].title
                    if len(kept) == 1
                    else f"终末地活动动态（{len(kept)}项）",
                    description="\n\n".join(unit.text for unit in kept),
                    # A composite cover can itself contain suppressed events.
                    cover_url="",
                )
            await asyncio.wait_for(send(selected), timeout=120)
            await asyncio.to_thread(self.receipts.finish, claim, int(self.clock()))
        finally:
            # Sent rows survive release; failures/cancellation free pending claims.
            await asyncio.shield(asyncio.to_thread(self.receipts.release, claim))
