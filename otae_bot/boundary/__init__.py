"""Protocol boundary: the only package that speaks a platform protocol.

Whatever is connected outside, the rest of the bot sees only Satori. Accounts listed in
``ONEBOT_CLIENTS`` are served in-process by entari-plugin-server in direct-adapter mode:
API calls go straight from Entari to the adapter, never over a network.

The plugin still starts its uvicorn service (``satori.server.Server`` requires it), and
satori's HTTP API does not check the token. On POSIX it therefore listens on a unix
socket in a 0700 directory instead of a TCP port; nothing in the bot connects to it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

SERVER_PLUGIN = "entari_plugin_server"
ONEBOT11_ADAPTER = "otae_bot.boundary.onebot11:OneBotBoundaryAdapter"


def server_plugin_config(onebot_clients: list[dict], runtime_dir: Path = Path(".runtime")) -> dict:
    """entari-plugin-server config serving each OneBot 11 endpoint through the boundary adapter."""
    adapters = [
        {
            "$path": ONEBOT11_ADAPTER,
            "endpoint": str(client["endpoint"]),
            "access_token": str(client.get("token") or "") or None,
        }
        for client in onebot_clients
    ]
    config: dict = {
        "direct_adapter": True,
        "adapters": adapters,
        "host": "127.0.0.1",
        # Fresh per process: nothing outside needs it, and it guards the event socket.
        "token": secrets.token_urlsafe(24),
        "options": {"access_log": False},
    }
    if os.name != "nt":
        socket_dir = runtime_dir / "satori-server"
        socket_dir.mkdir(parents=True, exist_ok=True)
        socket_dir.chmod(0o700)
        config["options"]["uds"] = str(socket_dir / "satori.sock")
    return config
