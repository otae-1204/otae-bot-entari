"""Apply group switches to native Entari commands, listeners and deliveries."""

from __future__ import annotations

import time

from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.entari.plugin.service import plugin_service
from arclet.letoderea import EVENT, STOP, Contexts, Propagator, Subscriber
from loguru import logger

from otae_bot.adapters.framework_hooks import attach_to_every_subscriber, running_subscriber

from otae_bot.group_features import (
    PROTECTED_PLUGINS,
    feature_store,
    group_scope,
    plugin_key,
    scope_from_event,
)


class GroupFeatureGate(Propagator):
    def __init__(self, plugin: str):
        self.plugin = plugin

    def compose(self):
        async def check(ctx: Contexts):
            session = ctx.get(ITEM_SESSION)
            account = session.account if session else ctx.get(ITEM_ACCOUNT)
            event = session.event if session else ctx.get(EVENT)
            if not feature_store.is_enabled(scope_from_event(account, event), self.plugin):
                # Stop only this subscriber, leaving other plugins available.
                return STOP

        # Run before Alconna parsing (including generated command help).
        yield check, True, -100


def subscriber_plugin(subscriber: Subscriber | None) -> str:
    """The ``plugins.<name>`` package a subscriber belongs to ('' if none).

    Entari only accepts handlers defined in the plugin's own modules, and the
    bot's wrappers (Matcher, on_event, timers) copy the handler's ``__module__``.
    """
    return plugin_key(getattr(getattr(subscriber, "callable_target", None), "__module__", "") or "")


def _gate_for(subscriber: Subscriber) -> GroupFeatureGate | None:
    name = subscriber_plugin(subscriber)
    if not name or name in PROTECTED_PLUGINS:
        return None
    return GroupFeatureGate(name)


def install_group_feature_gates():
    """Gate every plugin subscriber, including later and hot-reloaded ones."""
    return attach_to_every_subscriber(GroupFeatureGate, _gate_for)


def loaded_group_plugins() -> list[str]:
    return sorted({
        plugin_key(plugin.path)
        for plugin in plugin_service.plugins.values()
        if plugin.path.startswith("plugins.") and plugin.path.count(".") == 1
    }, key=str.casefold)


# A suppressed delivery returns an empty result to its caller, which cannot
# tell "sent" from "switched off". Say it in the log instead of losing the
# message silently, but keep it to one line per scope per interval.
SUPPRESSED_LOG_INTERVAL_SECONDS = 300
_suppressed_logged: dict[tuple[str, str], float] = {}


def allow_group_delivery(dest, account) -> bool:
    if dest.private:
        return True
    name = subscriber_plugin(running_subscriber())
    # Entari's subscriber context is inherited by background tasks, including
    # the shared scheduler's create_task/to_thread callbacks.
    scope = group_scope(account, dest.parent_id or dest.id)
    if feature_store.is_enabled(scope, name):
        return True
    _log_suppressed(scope, name)
    return False


def _log_suppressed(scope, plugin: str) -> None:
    """Report a switched-off delivery once per interval instead of silently."""
    key = (scope.key if scope is not None else "", plugin)
    now = time.monotonic()
    if now - _suppressed_logged.get(key, float("-inf")) < SUPPRESSED_LOG_INTERVAL_SECONDS:
        return
    _suppressed_logged[key] = now
    group = scope.group_id if scope is not None else "?"
    logger.warning(
        f"[feature-gate] dropped a delivery to group {group}: "
        f"{plugin or 'this plugin'} is switched off for that chat"
    )
