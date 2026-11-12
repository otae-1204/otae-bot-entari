"""Keep the latest WARNING-and-above log lines in memory for the superuser /status card.

Only the first line of each message is kept (no tracebacks), trimmed and passed through
the same phone-number masking as the message log. Nothing is written to disk.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from otae_bot.infrastructure.redaction import redact

CAPACITY = 50
MAX_LENGTH = 160


@dataclass(frozen=True)
class Alert:
    at: datetime
    level: str
    source: str
    message: str


_alerts: deque[Alert] = deque(maxlen=CAPACITY)
_sink_id: int | None = None


def _sink(message) -> None:
    record = message.record
    text = str(record["message"]).strip().splitlines()[0] if str(record["message"]).strip() else ""
    _alerts.append(
        Alert(
            at=record["time"].replace(tzinfo=None),
            level=record["level"].name,
            source=str(record["name"] or ""),
            message=redact(text)[:MAX_LENGTH],
        )
    )


def install_recent_alerts() -> None:
    """Attach the sink once; repeated calls keep a single sink."""
    global _sink_id
    if _sink_id is None:
        _sink_id = logger.add(_sink, level="WARNING", format="{message}", catch=True)


def recent_alerts(limit: int = 5) -> list[Alert]:
    """Newest first."""
    return list(reversed(_alerts))[:limit]


__all__ = ["Alert", "install_recent_alerts", "recent_alerts"]
