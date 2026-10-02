"""One cold-start notice per command, sent before a slow public download.

The shared asset downloaders do not speak on their own. A command arms this
scope after arguments, permissions, and account selection are done; later
checks in that same task reuse the scope so a table notice is not followed
by a second asset notice. Sign-in, daily, and other local commands never arm
the scope, so they stay quiet even when they download icons.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from otae_bot.infrastructure.http.client import cached_public_resource

from .catalog.commands import parse_equipment_attribute_filters
from .providers.akedata import (
    AKEDATA_DATA_BASE,
    AKEDATA_HEADERS,
    fetch_akedata_manifest,
    i18n_loaded_path,
    i18n_path_from_manifest,
    i18n_process_warm,
)
from .providers.warfarin import API_CACHE_NAMESPACE, WarfarinClient


COLD_START_NOTICE = "终末地资料正在首次加载，可能需要十几秒，加载完会继续发送结果。"

REMOTE_ASSET_NAMESPACE = "endfield-assets"
ACCOUNT_UI_ASSET_NAMESPACE = "endfield-account-ui-assets"
_ASSET_NAMESPACES = {REMOTE_ASSET_NAMESPACE, ACCOUNT_UI_ASSET_NAMESPACE}

_FZ_CATALOG_TITLES = {"operator": "干员", "weapon": "武器", "equipment": "装备"}
_FZ_TITLE_PREFIXES = {"operator": "干员/", "weapon": "武器/", "equipment": "装备/"}


@dataclass
class _Notice:
    matcher: Any
    sent: bool = False


_notice: ContextVar[_Notice | None] = ContextVar("endfield_cold_start_notice", default=None)


@contextmanager
def cold_start_command(matcher) -> Iterator[None]:
    """Arm the once-per-command notice for the current task."""
    token = _notice.set(_Notice(matcher))
    try:
        yield
    finally:
        _notice.reset(token)


async def send_cold_start_notice() -> bool:
    state = _notice.get()
    send = getattr(getattr(state, "matcher", None), "send", None)
    if state is None or state.sent or not callable(send):
        return False
    state.sent = True
    await send(COLD_START_NOTICE)
    return True


async def ake_public_tables_cold() -> bool:
    """True when default AKE public data still has to be loaded.

    The process-warm flag is enough on its own: the first successful
    ``I18nTextTable_CN`` read in this process is the slow part, even when
    disk already holds the file. After that, only a missing memory-and-disk
    copy of the current manifest path counts, and a version-path mismatch
    counts as missing.
    """
    if not i18n_process_warm():
        return True
    try:
        manifest = await fetch_akedata_manifest()
    except Exception:
        return False
    current = i18n_path_from_manifest(manifest)
    if not current or current != i18n_loaded_path():
        return True
    return not await cached_public_resource(
        f"{AKEDATA_DATA_BASE}{current}",
        namespace="akedata",
        response_kind="json",
        headers=AKEDATA_HEADERS,
    )


async def notice_default_ake_public() -> None:
    state = _notice.get()
    if state is None or state.sent:
        return
    if await ake_public_tables_cold():
        await send_cold_start_notice()


async def notice_before_public_data(*, source: str, scope: str, query: str) -> None:
    """Notice before catalog collection. Explicit FZ/Warfarin ignore the AKE flag."""
    state = _notice.get()
    if state is None or state.sent:
        return
    if source in {"fz", "warfarin"}:
        if await explicit_wiki_responses_cold(source, scope, query):
            await send_cold_start_notice()
        return
    if scope == "archive_entry":
        return
    await notice_default_ake_public()


async def explicit_wiki_responses_cold(source: str, scope: str, query: str) -> bool:
    requests = list(_explicit_requests(source, scope, query))
    if not requests:
        return False
    headers = dict(WarfarinClient().headers)
    for url, params in requests:
        if not await cached_public_resource(
            url,
            namespace=API_CACHE_NAMESPACE,
            response_kind="json",
            params=params,
            headers=headers,
        ):
            return True
    return False


def _explicit_requests(source: str, scope: str, query: str) -> Iterator[tuple[str, dict[str, Any] | None]]:
    text = str(query or "").strip()
    kinds = ("operator", "weapon", "equipment") if scope in {"", "all"} else (scope,)
    for kind in kinds:
        if source == "fz":
            yield from _fz_requests(kind, text)
        elif source == "warfarin":
            yield from _warfarin_requests(kind, text)


def _fz_requests(kind: str, query: str) -> Iterator[tuple[str, dict[str, Any]]]:
    title = _FZ_CATALOG_TITLES.get(kind)
    prefix = _FZ_TITLE_PREFIXES.get(kind)
    if title is None or prefix is None or not query or query == "__all__" or query.startswith(prefix):
        return
    if kind == "equipment" and parse_equipment_attribute_filters(query):
        return
    yield (
        f"{WarfarinClient.FZ_BASE_URL}/articles/by-title",
        {"ns": 0, "title": title, "withRevision": 1},
    )


def _warfarin_requests(kind: str, query: str) -> Iterator[tuple[str, dict[str, Any] | None]]:
    if kind not in {"operator", "weapon"} or not query:
        return
    prefix = _FZ_TITLE_PREFIXES[kind]
    stripped = query[len(prefix):] if query.startswith(prefix) else query
    if not stripped:
        return
    leaf = "operators" if kind == "operator" else "weapons"
    yield (f"{WarfarinClient.BASE_URL}/cn/search", {"q": stripped})
    yield (f"{WarfarinClient.BASE_URL}/cn/{leaf}", None)


async def note_remote_assets(urls: Iterable[Any], *, namespace: str) -> None:
    """Before one batch of remote images. No-op unless this command armed a notice."""
    state = _notice.get()
    if state is None or state.sent or namespace not in _ASSET_NAMESPACES:
        return
    if await remote_assets_cold(urls, namespace=namespace):
        await send_cold_start_notice()


async def remote_assets_cold(urls: Iterable[Any], *, namespace: str) -> bool:
    pending = _remote_urls(urls)
    if not pending:
        return False
    for url in pending:
        if not await cached_public_resource(
            url,
            namespace=namespace,
            response_kind="bytes",
            asset=True,
        ):
            return True
    return False


def _remote_urls(urls: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    remote: list[str] = []
    for url in urls:
        text = str(url or "").strip()
        if not text.startswith(("http://", "https://")) or text in seen:
            continue
        seen.add(text)
        remote.append(text)
    return tuple(remote)
