"""Merged-forward messages as Satori ``<message forward>`` elements."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from arclet.entari import Author, Message, MessageChain, Session, Text


def make_forward(
    nodes: Iterable[Any],
    *,
    name: str | None = None,
    uin: str | int | None = None,
) -> Message:
    """Build a Satori merged-forward element (``<message forward>``).

    Each node becomes one child ``<message>``; a node may be a single element, a
    string, or a sequence of elements.  ``name``/``uin`` are emitted as an
    ``<author>`` inside every node.  Callers pass one author for the whole
    forward; per-node senders are not relied on.  This is the only way plugins
    send a merged forward; there is no protocol-specific fallback.
    """
    children: list[Message] = []
    for node in nodes:
        if isinstance(node, str):
            content: list[Any] = [Text(node)]
        elif isinstance(node, (list, tuple, MessageChain)):
            content = [Text(item) if isinstance(item, str) else item for item in node]
        else:
            content = [node]
        if uin:
            content.insert(0, Author(str(uin), name or None))
        children.append(Message(content=content))
    return Message(forward=True, content=children)


async def send_forward(
    session: Session,
    nodes: Iterable[Any],
    *,
    name: str | None = None,
    uin: str | int | None = None,
):
    """Send one merged-forward message through ``session``."""
    return await session.send(MessageChain([make_forward(nodes, name=name, uin=uin)]))
