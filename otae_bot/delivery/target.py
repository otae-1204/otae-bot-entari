"""Where a proactive message goes, as opaque Satori ids."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from satori import ChannelType


def _text(value: Any) -> str:
    return str(value or "")


@dataclass(frozen=True)
class Target:
    """A chat to deliver to. Empty strings mean unknown or not applicable.

    ``self_id``/``platform`` name the account that should send, when known; the
    other ids are the Satori guild, channel and user ids, never converted.
    """

    platform: str = ""
    self_id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    user_id: str = ""

    def __post_init__(self):
        for name in ("platform", "self_id", "guild_id", "channel_id", "user_id"):
            object.__setattr__(self, name, _text(getattr(self, name)))

    @classmethod
    def group(cls, group_id: Any, *, channel_id: Any = "", self_id: Any = "", platform: Any = "") -> "Target":
        return cls(platform, self_id, group_id, channel_id)

    @classmethod
    def private(cls, user_id: Any, *, self_id: Any = "", platform: Any = "") -> "Target":
        return cls(platform, self_id, user_id=user_id)

    @classmethod
    def from_event(cls, account: Any, event: Any) -> "Target":
        """The chat an event came from, sent to by the account that received it."""
        platform = getattr(account, "platform", "")
        self_id = getattr(account, "self_id", "")
        user_id = getattr(getattr(event, "user", None), "id", "")
        channel = getattr(event, "channel", None)
        channel_id = _text(getattr(channel, "id", ""))
        guild_id = _text(getattr(getattr(event, "guild", None), "id", ""))
        if not guild_id and channel is not None and getattr(channel, "type", None) is not ChannelType.DIRECT:
            guild_id = channel_id
        if guild_id:
            return cls.group(guild_id, channel_id=channel_id, self_id=self_id, platform=platform)
        return cls.private(user_id, self_id=self_id, platform=platform)

    @property
    def is_private(self) -> bool:
        return not self.guild_id
