"""Optional game version label; version lookup must not block account statistics."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ...providers.akedata import (
    AKEDATA_TIMEOUT_SECONDS,
    fetch_akedata_manifest,
    game_version_label,
)

logger = logging.getLogger(__name__)
# The manifest URL changes every minute, so lookups are often cold. Never cut one
# short of its own HTTP timeout; this bound only stops a stalled request.
LOOKUP_TIMEOUT_SECONDS = AKEDATA_TIMEOUT_SECONDS + 10


async def fetch_exploration_version() -> str:
    try:
        async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
            manifest = await fetch_akedata_manifest()
        label = game_version_label(str(manifest.get("latest") or ""))
        return label if re.fullmatch(r"[0-9]+\.[0-9]+", label) else ""
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, TimeoutError) as exc:
        logger.warning("Exploration game version unavailable: %s", type(exc).__name__)
        return ""
