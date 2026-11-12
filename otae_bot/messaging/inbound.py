"""Read who sent an event, where it came from and what it says."""

from __future__ import annotations

from typing import Any

from arclet.entari import MessageChain, Text
from satori import ChannelType


def get_group_id(event: Any) -> str:
    guild = getattr(event, "guild", None)
    if guild and getattr(guild, "id", None):
        return str(guild.id)
    channel = getattr(event, "channel", None)
    if channel and getattr(channel, "type", None) is not ChannelType.DIRECT:
        return str(channel.id)
    return ""


def get_channel_id(event: Any) -> str:
    channel = getattr(event, "channel", None)
    return str(getattr(channel, "id", "") or "")


def is_group(event: Any) -> bool:
    return bool(get_group_id(event))


def event_user_id(event: Any) -> str:
    user = getattr(event, "user", None)
    return str(getattr(user, "id", "") or "")


def event_chain(event: Any) -> MessageChain:
    content = getattr(event, "content", None)
    if content is not None:
        return MessageChain(content)
    message = getattr(event, "message", None)
    if message is not None:
        return MessageChain(getattr(message, "message", message))
    return MessageChain()


def event_plain_text(event: Any) -> str:
    text_parts: list[str] = []
    for seg in event_chain(event):
        if isinstance(seg, Text):
            text_parts.append(str(getattr(seg, "text", "") or ""))
    if text_parts:
        return "".join(text_parts)
    return str(getattr(event, "content", "") or "")


def get_rest(rest_match: Any) -> str:
    """The free-text ``rest`` argument of a command (a ``Match``) as one stripped string."""
    if not getattr(rest_match, "available", False):
        return ""
    val = getattr(rest_match, "result", "")
    if isinstance(val, tuple):
        return " ".join(str(item) for item in val).strip()
    return str(val).strip() if val is not None else ""
