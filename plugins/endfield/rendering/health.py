"""Per-render asset health; no URL or account data is logged or persisted."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterable, Iterator


@dataclass(slots=True)
class RenderHealth:
    missing: set[str] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return not self.missing


_health: ContextVar[RenderHealth | None] = ContextVar(
    "endfield_render_health", default=None
)


@contextmanager
def track_render_health() -> Iterator[RenderHealth]:
    health = RenderHealth()
    token = _health.set(health)
    try:
        yield health
    finally:
        _health.reset(token)


def record_assets(requested: Iterable[str], resolved: Iterable[str]) -> None:
    health = _health.get()
    if health is not None:
        ready = set(resolved)
        health.missing.update(url for url in requested if url and url not in ready)
        health.missing.difference_update(ready)


def record_fallback_success(candidates: Iterable[str]) -> None:
    health = _health.get()
    if health is not None:
        health.missing.difference_update(candidates)
