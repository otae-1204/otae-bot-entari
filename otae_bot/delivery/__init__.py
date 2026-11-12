"""Proactive (non-reply) message delivery: ``deliver(Target, message)``."""

from otae_bot.delivery.send import (
    FAILED,
    NO_ACCOUNT,
    SENT,
    SUPPRESSED,
    DeliveryResult,
    deliver,
    is_not_in_group,
    resolve_account,
)
from otae_bot.delivery.target import Target

__all__ = [
    "FAILED",
    "NO_ACCOUNT",
    "SENT",
    "SUPPRESSED",
    "DeliveryResult",
    "Target",
    "deliver",
    "is_not_in_group",
    "resolve_account",
]
