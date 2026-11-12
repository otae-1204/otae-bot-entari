"""Count every matched command across all plugins without touching their handlers.

``CommandUsageRecorder`` is installed on each plugin scope the same way
``feature_gate.GroupFeatureGate`` is. It runs after Entari's Alconna supplier
(priority 70) has put ``alc_result`` into the context, so only commands that actually
matched are counted; plain message listeners (link previews, private dialogs) have no
``alc_result`` and are skipped. Group-disabled plugins are stopped earlier by the gate.

Handlers usually end with ``session.stop()``, which raises ``STOP`` and makes letoderea
skip post-handler propagators. The recorder therefore registers a callback on the
subscriber's exit stack, which letoderea always closes: a normal return or ``STOP``
counts as success, any other exception as failure.

Arguments are never stored. A plugin may register its sub-command words with
:func:`describe_command`; the first argument is kept only when it is one of them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.entari.plugin.service import plugin_service
from arclet.letoderea import EVENT, STACK, Contexts, Propagator
from loguru import logger

from otae_bot.group_features import plugin_key

from .store import CallRecord, UsageStore

#: Runs after AlconnaSuppiler (70) has parsed the command.
HOOK_PRIORITY = 90
FLUSH_SECONDS = 5.0
FLUSH_BATCH = 200


@dataclass(frozen=True)
class CommandDescription:
    display: str
    subcommands: Mapping[str, str] = field(default_factory=dict)
    default_sub: str = ""


_descriptions: dict[str, CommandDescription] = {}


def describe_command(
    names: Iterable[str],
    *,
    display: str,
    subcommands: Mapping[str, str] | None = None,
    default_sub: str = "",
) -> None:
    """Register how a command shows up in the stats.

    ``names`` are the Alconna command names (any of them); ``display`` is the short
    form shown on cards, e.g. ``/ef``. ``subcommands`` maps every accepted first word
    (aliases included) to its canonical label; ``default_sub`` labels calls whose first
    word is not registered, e.g. a free-text lookup. Matching ignores case.
    """
    description = CommandDescription(
        display,
        {str(word).casefold(): label for word, label in (subcommands or {}).items()},
        default_sub,
    )
    for name in names:
        _descriptions[str(name).lstrip("/").casefold()] = description


def _first_word(arparma) -> str:
    args = getattr(arparma, "all_matched_args", {}) or {}
    for key in ("rest", "content"):
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            return str(value).split(maxsplit=1)[0] if str(value).strip() else ""
    return ""


def resolve_command(alc_result) -> tuple[str, str]:
    """``(display command, sub-command label)`` for a parsed command result."""
    command = getattr(alc_result, "source", None)
    name = str(getattr(command, "name", "") or "").lstrip("/")
    description = _descriptions.get(name.casefold())
    if description is None:
        return f"/{name}", ""
    word = _first_word(getattr(alc_result, "result", None)).casefold()
    if not word:
        return description.display, ""
    return description.display, description.subcommands.get(word, description.default_sub)


def _first(*values) -> str:
    return next((str(value)[:32] for value in values if value), "")


def _group_card(event) -> str:
    """群名片，没有就退回 QQ 昵称。"""
    member, user = getattr(event, "member", None), getattr(event, "user", None)
    return _first(getattr(member, "nick", None), getattr(user, "name", None), getattr(user, "nick", None))


def _nickname(event) -> str:
    """QQ 昵称（Satori 的 ``user.name``）。"""
    user = getattr(event, "user", None)
    return _first(getattr(user, "name", None), getattr(user, "nick", None))


def _group_id(event) -> str:
    from otae_bot.adapters.entari import get_group_id

    return get_group_id(event)


class UsageRecorder:
    """Buffers records in memory and writes them to SQLite off the event loop."""

    def __init__(self, store: UsageStore | None = None):
        self.store = store or UsageStore()
        self._pending: list[CallRecord] = []
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0

    def add(self, record: CallRecord) -> None:
        self._pending.append(record)
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._flush_soon(), name="command-usage-flush")
            except RuntimeError:  # no running loop (tests calling add directly)
                pass

    async def _flush_soon(self) -> None:
        while self._pending:
            if len(self._pending) < FLUSH_BATCH:
                await asyncio.sleep(FLUSH_SECONDS)
            await self.flush()

    async def flush(self) -> None:
        batch, self._pending = self._pending, []
        if not batch:
            return
        try:
            await asyncio.to_thread(self._write, batch)
        except Exception as exc:  # noqa: BLE001 - statistics must never break commands
            logger.warning(f"[usage] failed to write {len(batch)} command records: {type(exc).__name__}: {exc}")

    def _write(self, batch: list[CallRecord]) -> None:
        self.store.insert_many(batch)
        now = time.time()
        if now - self._last_prune > 86400:
            self._last_prune = now
            self.store.prune(datetime.fromtimestamp(now))

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.flush()
        self.store.close()


usage_recorder = UsageRecorder()


class CommandUsageRecorder(Propagator):
    def __init__(self, plugin: str):
        self.plugin = plugin

    def compose(self):
        async def record(ctx: Contexts):
            try:
                alc_result = ctx.get("alc_result")
                stack = ctx.get(STACK)
                if alc_result is None or stack is None:
                    return
                session = ctx.get(ITEM_SESSION)
                account = session.account if session else ctx.get(ITEM_ACCOUNT)
                event = session.event if session else ctx.get(EVENT)
                command, sub = resolve_command(alc_result)
                platform = str(getattr(account, "platform", "") or "")
                user = getattr(event, "user", None)
                user_id = str(getattr(user, "id", "") or "")
                group_id = _group_id(event)
                name, nickname = _group_card(event), _nickname(event)
                plugin = self.plugin
                started, wall = time.perf_counter(), time.time()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[usage] skipped a command record: {type(exc).__name__}: {exc}")
                return

            async def on_exit(exc_type, exc, tb):
                if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                    return False
                usage_recorder.add(
                    CallRecord(
                        ts=wall,
                        platform=platform,
                        group_id=group_id,
                        user_id=user_id,
                        plugin=plugin,
                        command=command,
                        sub=sub,
                        elapsed_ms=round((time.perf_counter() - started) * 1000),
                        ok=exc_type is None,
                        name=name,
                        nickname=nickname,
                    )
                )
                return False

            stack.push_async_exit(on_exit)
            return

        yield record, True, HOOK_PRIORITY


def install_command_usage_recorders() -> None:
    """Cover every plugin scope; safe to call again after a hot reload."""
    for plugin in tuple(plugin_service.plugins.values()):
        name = plugin_key(plugin.path)
        if not name:
            continue
        scope = plugin._scope
        if any(isinstance(item, CommandUsageRecorder) for item in scope.propagators):
            continue
        hook = CommandUsageRecorder(name)
        scope.propagators.append(hook)
        for slot in tuple(scope.subscribers):
            slot.subscriber.propagate(hook)


async def close_command_usage() -> None:
    await usage_recorder.close()


__all__ = [
    "HOOK_PRIORITY",
    "CommandUsageRecorder",
    "UsageRecorder",
    "close_command_usage",
    "describe_command",
    "install_command_usage_recorders",
    "resolve_command",
    "usage_recorder",
]
