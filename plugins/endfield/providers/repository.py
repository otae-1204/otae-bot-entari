"""One version-pinned, public-only TableCfg reader shared by End consumers.

Snapshots are query scoped: they never retain account payloads or build another
unbounded process-wide copy of the HTTP table cache.
"""

from __future__ import annotations

import asyncio
import re
import httpx
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .akedata import _get, fetch_akedata_manifest


class AkeDataIncomplete(ValueError):
    """A public source cannot supply a complete, coherent view."""


def localize(value: Any, texts: dict) -> Any:
    """Copy only a selected row; never localize entire unrelated tables."""
    if isinstance(value, dict):
        if "id" in value and "text" in value:
            return str(texts.get(str(value["id"])) or value.get("text") or "")
        return {key: localize(item, texts) for key, item in value.items()}
    if isinstance(value, list):
        return [localize(item, texts) for item in value]
    return value


@dataclass(slots=True)
class AkeSnapshot:
    version: str
    table_path: str
    shared_revision: str = ""
    _tables: dict[str, dict] = field(default_factory=dict, repr=False)

    @property
    def revision(self) -> str:
        return f"{self.version}|{self.shared_revision}"

    async def table(self, name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ValueError("Invalid AKE table name")
        if name not in self._tables:
            try:
                data = await _get(
                    f"/{self.table_path}/{name}.json", max_bytes=64 * 1024 * 1024
                )
            except httpx.HTTPError as exc:
                raise AkeDataIncomplete(
                    f"AKE {name} request failed ({type(exc).__name__})"
                ) from exc
            if not isinstance(data, dict) or not data:
                raise AkeDataIncomplete(f"AKE {name} is empty or malformed")
            self._tables[name] = data
        return self._tables[name]

    async def tables(self, *names: str) -> tuple[dict, ...]:
        return tuple(await asyncio.gather(*(self.table(name) for name in names)))

    async def localized(self, name: str, key: str | None = None) -> dict:
        table, texts = await self.tables(name, "I18nTextTable_CN")
        if key is None:
            return localize(table, texts)
        if key not in table:
            raise AkeDataIncomplete(f"AKE {name} is missing {key}")
        return localize(table[key], texts)


_snapshot: ContextVar[AkeSnapshot | None] = ContextVar(
    "endfield_ake_snapshot", default=None
)


async def snapshot(revision: str = "") -> AkeSnapshot:
    current = _snapshot.get()
    if current is not None and (not revision or current.revision == revision):
        return current
    try:
        manifest = await fetch_akedata_manifest()
    except httpx.HTTPError as exc:
        raise AkeDataIncomplete(
            f"AKE manifest request failed ({type(exc).__name__})"
        ) from exc
    version = revision.split("|", 1)[0] if revision else str(manifest["latest"])
    entry = next(
        (v for v in manifest.get("versions", []) if v.get("id") == version), {}
    )
    path = str(entry.get("tableCfgPath") or "").strip("/")
    if not re.fullmatch(r"public/[0-9.]+/[0-9-]+/TableCfg", path):
        raise AkeDataIncomplete("AKE manifest has no valid versioned table path")
    shared = (
        revision.split("|", 1)[1]
        if "|" in revision
        else str(manifest.get("sharedRevision") or "")
    )
    return AkeSnapshot(version, path, shared)


@asynccontextmanager
async def query_snapshot(revision: str = ""):
    current = await snapshot(revision)
    token = _snapshot.set(current)
    try:
        yield current
    finally:
        _snapshot.reset(token)
