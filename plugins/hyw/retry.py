"""Transient upstream failures: truncated exponential backoff with jitter.

Both credential modes hit the same upstream shapes worth retrying:

* ``429`` (and ``5xx``) from the model endpoint — Vertex answers a bare
  ``RESOURCE_EXHAUSTED`` under contention, and relays rate limit too;
* transport hiccups — read timeouts (slow reasoning models) and streams dropped by
  a gateway or proxy.

The schedule is truncated exponential backoff with *equal jitter* (half the window
fixed, half random) so a throttled client never retries instantly, plus an overall
sleep budget: one question may issue up to ``max_turns`` model calls inside the
handler's own timeout, so retries must not be able to consume it.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0
DEFAULT_BUDGET = 20.0

# Statuses worth retrying: rate limiting plus gateway/server hiccups.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Transport classifications (see network_errors.classify_error) that clear on their
# own. Deliberately excludes dns / tls_certificate / tls_handshake / proxy /
# url_protocol / request_protocol / response_encoding: those are configuration or
# trust failures that a retry cannot fix, and retrying only delays the diagnosis.
RETRYABLE_CODES = frozenset({"timeout", "connection_interrupted"})


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts to make and how long to wait between them."""

    attempts: int = DEFAULT_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    budget: float = DEFAULT_BUDGET

    @property
    def enabled(self) -> bool:
        return self.attempts > 1 and self.budget > 0

    def new_budget(self) -> RetryBudget:
        return RetryBudget(limit=self.budget)


@dataclass
class RetryBudget:
    """Sleep budget shared by every model call of one question.

    ``ask`` issues up to ``max_turns`` model calls, and the handler wraps the whole
    question in its own timeout. A per-call budget would therefore let a single
    question sleep ``max_turns`` times over and blow that timeout, so the budget is
    created once per question and shared by all of its calls.
    """

    limit: float = DEFAULT_BUDGET
    spent: float = 0.0

    def take(self, wait: float) -> bool:
        """Reserve ``wait`` seconds when the remaining budget allows it."""
        if self.spent + wait > self.limit:
            return False
        self.spent += wait
        return True


async def sleep(seconds: float) -> None:
    """Indirection so tests can observe the schedule without waiting."""
    await asyncio.sleep(seconds)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delta-seconds or HTTP-date.

    Returns None when the header is absent or unparseable. Callers clamp the result,
    because upstreams may send an implausibly large value.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def delay_for(attempt: int, policy: RetryPolicy, retry_after: float | None = None) -> float:
    """Delay before the next attempt, where ``attempt`` is 1-based.

    An upstream ``Retry-After`` wins over the computed window (still clamped): it is
    the only signal that says when the limit actually lifts.
    """
    if retry_after is not None and retry_after > 0:
        return min(retry_after, policy.max_delay)
    window = min(policy.base_delay * (2 ** max(0, attempt - 1)), policy.max_delay)
    # Equal jitter: keep a floor so a throttled client backs off instead of hammering.
    return window / 2 + random.uniform(0, window / 2)
