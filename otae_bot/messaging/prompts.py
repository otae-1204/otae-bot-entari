"""Ask the user of a session for the next message (multi-step dialogs)."""

from __future__ import annotations

import asyncio
from time import perf_counter

import aiohttp
from arclet.entari import MessageChain, MessageCreatedEvent, Session
from arclet.letoderea import defer, step_out
from loguru import logger
from satori import ChannelType


async def prompt(session: Session, message: str, timeout: int = 60):
    """``session.prompt`` (sends 等待超时 on timeout), retried once if sending dropped early."""
    started = perf_counter()
    try:
        return await session.prompt(message, timeout=timeout)
    except aiohttp.ClientConnectionError:
        if perf_counter() - started >= 5:
            raise
        logger.warning("[entari] prompt message connection interrupted; retrying once")
        await asyncio.sleep(0.25)
        return await session.prompt(message, timeout=timeout)


async def prompt_silently(session: Session, message: str, timeout: int = 60):
    """Send ``message`` and wait for a reply that quotes it; ``None`` on timeout, without a notice."""
    sent_messages = await session.send(message)
    message_ids = {
        str(item.id)
        for item in sent_messages or ()
        if getattr(item, "id", None) is not None
    }

    async def waiter(content: MessageChain, candidate_session: Session):
        quote_id = str(getattr(getattr(candidate_session.event, "quote", None), "id", "") or "")
        if not quote_id or quote_id not in message_ids:
            return None
        if session.event.user and session.event.user.id == candidate_session.event.user.id:
            if candidate_session.event.channel.type == ChannelType.DIRECT:
                return content
            if session.event.channel and candidate_session.event.channel.id == session.event.channel.id:
                return content

    waiter.__annotations__ = {"content": MessageChain, "candidate_session": Session}
    step = step_out(MessageCreatedEvent, waiter, block=False, priority=15)
    defer(step.dispose)
    return await step.wait(timeout=timeout)
