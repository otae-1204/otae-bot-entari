"""Per-role in-flight guard so one character is never signed in twice.

Two concurrent ``/ak 签到`` commands may target the same character.  Only the
first claim proceeds; the second is reported as an independent failed result
instead of firing a duplicate attendance request.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class TaskAlreadyRunning(RuntimeError):
    """Raised when the same game role already has an attendance request running."""


class RoleTaskRegistry:
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._active: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def claim(self, role):
        key = (str(getattr(role, "uid", "")), str(getattr(role, "game_id", "")))
        async with self._guard:
            if key in self._active:
                raise TaskAlreadyRunning("该角色正在签到，请稍后再试")
            self._active.add(key)
        try:
            yield
        finally:
            async with self._guard:
                self._active.discard(key)

    def active_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._active)


ROLE_TASKS = RoleTaskRegistry()
