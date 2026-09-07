"""Apply group switches to native Entari commands, listeners and deliveries."""

from __future__ import annotations

from arclet.entari.const import ITEM_ACCOUNT, ITEM_SESSION
from arclet.entari.event.plugin import PluginLoadedSuccess
from arclet.entari.plugin.service import plugin_service
from arclet.letoderea import EVENT, STOP, Contexts, Propagator
from arclet.letoderea.subscriber import current_subscriber

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


def install_group_feature_gates() -> None:
    """Cover all handler submodules, including future registrations in a scope."""
    for plugin in tuple(plugin_service.plugins.values()):
        name = plugin_key(plugin.path)
        if not name or name in PROTECTED_PLUGINS:
            continue
        scope = plugin._scope
        if any(isinstance(item, GroupFeatureGate) for item in scope.propagators):
            continue
        gate = GroupFeatureGate(name)
        scope.propagators.insert(0, gate)
        for subscriber, _ in tuple(scope.subscribers.values()):
            subscriber.propagate(gate)


async def on_plugin_loaded(event: PluginLoadedSuccess):
    # Also covers Entari hot reload, when a plugin's scopes are recreated.
    install_group_feature_gates()


def loaded_group_plugins() -> list[str]:
    return sorted({
        plugin_key(plugin.path)
        for plugin in plugin_service.plugins.values()
        if plugin.path.startswith("plugins.") and plugin.path.count(".") == 1
    }, key=str.casefold)


def allow_group_delivery(dest, account) -> bool:
    if dest.private:
        return True
    subscriber = current_subscriber.get(None)
    name = plugin_key(getattr(getattr(subscriber, "callable_target", None), "__module__", ""))
    # Entari's subscriber context is inherited by background tasks, including
    # the shared scheduler's create_task/to_thread callbacks.
    return feature_store.is_enabled(group_scope(account, dest.parent_id or dest.id), name)
