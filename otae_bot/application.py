"""Build and run the Entari application."""

from __future__ import annotations

from arclet.entari import Cleanup, Entari, Startup, listen, load_plugin
from arclet.entari.event.plugin import PluginUnloaded
from loguru import logger

from otae_bot.command_usage import close_command_usage, install_command_usage_recorders
from otae_bot.config.settings import loaded_config, server_declaration
from otae_bot.hypergryph.attendance_registry import on_plugin_unloaded
from otae_bot.infrastructure.http.tls import prewarm_shared_ssl_context
from otae_bot.infrastructure.loop_watchdog import (
    close_loop_watchdog,
    start_loop_watchdog,
)
from otae_bot.infrastructure.recent_alerts import install_recent_alerts
from otae_bot.lifecycle import acquire_run_lock, close_shared_resources
from otae_bot.pipeline.command_input import install_command_input_normalizers
from otae_bot.pipeline.feature_gate import install_group_feature_gates
from otae_bot.pipeline.message_log import install_message_log_redaction
from otae_bot.pipeline.quote_fallback import install_quote_fetch_fallback
from otae_bot.plugin_registry import discover_plugins


def create_app() -> Entari:
    # Connections, log level and prefix come from entari.yml (see bot.py, which loads it
    # before anything else); without a loaded configuration the app has no connection.
    config = loaded_config()
    app = Entari.from_config(config) if config is not None else Entari()
    server = server_declaration()
    if server is not None:
        # Loaded here, before Entari would load `server` from the file with its own
        # defaults (a TCP listener): the boundary forces loopback/unix socket and a fresh token.
        from otae_bot.boundary import SERVER_PLUGIN, server_plugin_config

        loaded = load_plugin(SERVER_PLUGIN, server_plugin_config(server))
        if not loaded:
            # Without it the configured accounts never come online; fail loudly.
            raise RuntimeError(f"plugins.server is declared but {SERVER_PLUGIN} failed to load: {loaded.error!r}")
    if config is not None and not config.basic.network and server is None:
        logger.warning("[config] entari.yml declares no connection (basic.network / plugins.server)")
    install_message_log_redaction()
    install_recent_alerts()
    install_quote_fetch_fallback()
    install_command_input_normalizers()
    # Before any plugin loads: both attach to every plugin subscriber registered
    # from now on, so hot-reloaded plugins are covered without reinstalling.
    install_group_feature_gates()
    install_command_usage_recorders()
    listen(Startup)(start_loop_watchdog)
    listen(Cleanup)(close_loop_watchdog)
    listen(Cleanup)(close_shared_resources)
    listen(Cleanup)(close_command_usage)
    # A game plugin that is unloaded for good must stop advertising its
    # attendance capability to the other game's /ak 签到 or /ef 签到.
    listen(PluginUnloaded)(on_plugin_unloaded)
    for name in discover_plugins():
        load_plugin(name)
    return app


def main() -> int:
    run_lock = acquire_run_lock()
    if run_lock is None:
        print("[ERROR] Another bot-entari instance is already running.")
        print("[ERROR] Run scripts\\stop.bat first if you need to restart it.")
        return 2
    prewarm_shared_ssl_context()
    try:
        create_app().run()
    finally:
        run_lock.close()
    return 0
