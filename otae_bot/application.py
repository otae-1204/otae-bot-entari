"""Build and run the Entari application."""

from __future__ import annotations

from arclet.entari import Cleanup, Entari, WS, listen, load_plugin

from otae_bot.config.settings import SATORI_CLIENTS
from otae_bot.lifecycle import acquire_run_lock, close_shared_resources
from otae_bot.plugin_registry import discover_plugins


def build_network(client: dict | None = None) -> WS:
    client = client or {}
    return WS(
        host=str(client.get("host", "localhost")),
        port=int(client.get("port", 5500)),
        path=str(client.get("path", "")),
        token=str(client.get("token", "")) or None,
    )


def build_networks(clients: list[dict] | None = None) -> list[WS]:
    """Build one connection per configured LLOneBot endpoint."""
    if clients is None:
        clients = SATORI_CLIENTS
    return [build_network(client) for client in (clients or [{}])]


def create_app() -> Entari:
    app = Entari(*build_networks())
    listen(Cleanup)(close_shared_resources)
    for name in discover_plugins():
        load_plugin(name)
    return app


def main() -> int:
    run_lock = acquire_run_lock()
    if run_lock is None:
        print("[ERROR] Another bot-entari instance is already running.")
        print("[ERROR] Run scripts\\stop.bat first if you need to restart it.")
        return 2
    try:
        create_app().run()
    finally:
        run_lock.close()
    return 0
