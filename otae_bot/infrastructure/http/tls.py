"""Process-wide TLS contexts for httpx clients.

With ``verify=True`` httpx builds a new SSL context for every client or
transport and reads the whole certifi CA bundle synchronously (~1s on Windows),
which freezes the event loop. Pass ``verify=shared_ssl_context()`` instead.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import threading

import httpx

_contexts: dict[tuple[str | None, str | None], ssl.SSLContext] = {}
_lock = threading.Lock()


def _key(trust_env: bool) -> tuple[str | None, str | None]:
    # Mirrors httpx: trust_env only changes the context through these variables.
    if not trust_env:
        return (None, None)
    return (os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))


def shared_ssl_context(*, trust_env: bool = True) -> ssl.SSLContext:
    key = _key(trust_env)
    context = _contexts.get(key)
    if context is not None:
        return context
    with _lock:
        context = _contexts.get(key)
        if context is None:
            context = httpx.create_ssl_context(verify=True, trust_env=trust_env)
            _contexts[key] = context
        return context


async def ashared_ssl_context(*, trust_env: bool = True) -> ssl.SSLContext:
    context = _contexts.get(_key(trust_env))
    if context is not None:
        return context
    return await asyncio.to_thread(shared_ssl_context, trust_env=trust_env)


def prewarm_shared_ssl_context() -> None:
    """Build the default context in the background so the first request is cheap."""
    threading.Thread(
        target=shared_ssl_context, name="ssl-context-prewarm", daemon=True
    ).start()
