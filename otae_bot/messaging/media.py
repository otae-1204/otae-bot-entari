"""Media elements built from bytes — the only way plugins put images and files in a message.

An element made here carries its bytes inline (``data:`` URL). The outbound
pipeline (``otae_bot.pipeline.outbound_media``) uploads them through the sending
account's ``upload.create`` when it has one, so plugins never write temporary
files for sending and no local path ever leaves the bot.
"""

from __future__ import annotations

import mimetypes
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from arclet.entari import File, Image

_ASSET_CACHE_BYTES = 32 * 1024 * 1024
_assets: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()
_assets_lock = Lock()


def png(data: bytes) -> Image:
    """An image element for PNG bytes."""
    return Image.of(raw=data, mime="image/png")


def image(data: bytes, mime: str | None = None) -> Image:
    """An image element for ``data``; the type is detected from the bytes unless given."""
    if mime is None:
        try:
            return Image.of(raw=data)
        except ValueError:  # not recognisable; the platform sniffs the bytes anyway
            mime = "image/png"
    return Image.of(raw=data, mime=mime)


def file(data: bytes, name: str, mime: str | None = None) -> File:
    """A file element; ``name`` is what the recipient sees."""
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return File.of(raw=data, mime=mime, name=name)


def read_asset(path: str | Path) -> bytes:
    """The bytes of a file shipped with or rendered by the bot, cached while it is unchanged."""
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _assets_lock:
        if (data := _assets.get(key)) is not None:
            _assets.move_to_end(key)
            return data
    data = path.read_bytes()
    with _assets_lock:
        _assets[key] = data
        total = sum(len(value) for value in _assets.values())
        while total > _ASSET_CACHE_BYTES and len(_assets) > 1:
            _, dropped = _assets.popitem(last=False)
            total -= len(dropped)
    return data


def image_file(path: str | Path) -> Image:
    """An image element for a file on disk (help cards, shipped artwork), read as bytes."""
    path = Path(path)
    return image(read_asset(path), mimetypes.guess_type(path.name)[0])
