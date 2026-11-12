"""Protocol boundary: the only package that speaks a platform protocol.

Whatever is connected outside, the rest of the bot sees only Satori. Accounts declared under
``plugins: server:`` in entari.yml are served in-process by entari-plugin-server in direct-adapter mode:
API calls go straight from Entari to the adapter, never over a network.

The plugin still starts its uvicorn service (``satori.server.Server`` requires it), and
satori's HTTP API does not check the token. On POSIX it therefore listens on a unix
socket in a 0700 directory instead of a TCP port; nothing in the bot connects to it.
"""

from __future__ import annotations

import copy
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SERVER_PLUGIN = "entari_plugin_server"
ONEBOT11_ADAPTER = "otae_bot.boundary.onebot11:OneBotBoundaryAdapter"


def server_plugin_config(declared: Mapping[str, Any], runtime_dir: Path = Path(".runtime")) -> dict:
    """entari-plugin-server config for the ``plugins: server:`` section of entari.yml.

    ``declared`` lists the OneBot 11 endpoints as ``adapters`` entries (``endpoint``,
    ``access_token``); entries without ``$path`` get the boundary adapter. Whatever
    the file says, the server runs in direct-adapter mode on loopback with a token
    fresh per process, and on POSIX on a unix socket instead of a TCP port.
    """
    config: dict = copy.deepcopy(dict(declared))
    adapters = []
    for entry in config.get("adapters") or []:
        adapter = dict(entry)
        adapter.setdefault("$path", ONEBOT11_ADAPTER)
        adapter["access_token"] = str(adapter.get("access_token") or "") or None
        adapters.append(adapter)
    options = dict(config.get("options") or {})
    options["access_log"] = False
    config.update(
        adapters=adapters,
        direct_adapter=True,
        host="127.0.0.1",
        # Fresh per process: nothing outside needs it, and it guards the event socket.
        token=secrets.token_urlsafe(24),
        options=options,
    )
    if os.name != "nt":
        socket_dir = runtime_dir / "satori-server"
        socket_dir.mkdir(parents=True, exist_ok=True)
        socket_dir.chmod(0o700)
        options["uds"] = str(socket_dir / "satori.sock")
    return config
