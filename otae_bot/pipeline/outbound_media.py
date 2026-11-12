"""Media leave the bot as uploads or inline data, never as local paths.

Plugins build media elements from bytes (``Image.of(raw=…)`` →
``data:…;base64,…``) or from a public http(s) URL. Right before Entari sends a
message, :func:`prepare` walks it — elements nested in ``<message forward>``
and ``<quote>`` included — and for every ``img`` / ``audio`` / ``video`` /
``file`` whose ``src`` is a ``data:`` (or, defensively, a ``file:``) URL:

- if the sending account declares ``upload.create`` in its login ``features``,
  the bytes go up in one standard ``upload.create`` request (identical payloads
  once; the target ``channel_id`` as an extra form field, which some endpoints
  want and the others ignore) and ``src`` becomes the returned URL — possibly
  an ``internal:`` URL, valid for that same account;
- otherwise, or when the upload fails (one warning), the media stay inline:
  ``data:`` as is, ``file:`` read and turned into ``data:``.

The message is never dropped and the send is never retried; other attributes
(title, width, height, …) are kept, and the caller's elements are not mutated.

Hook: Entari posts ``SendRequest`` before every ``message.create`` it makes
(``Session.send``, ``protocol.send_message`` / ``send_private_message`` and so
``otae_bot.delivery.deliver``) and sends the ``MessageChain`` a subscriber
returns. Entari's own publisher for that event looks the channel up
(``channel.get``) for every send without a session, once any subscriber exists;
this module subscribes through a second publisher of the same event whose
context needs nothing, so no send costs an extra API call.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import dataclasses
import mimetypes
from collections.abc import Iterator, MutableSequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse
from urllib.request import url2pathname

from arclet.entari import Audio, File, Image, MessageChain, Video
from arclet.entari.event.api import SendRequest
from arclet.letoderea import Contexts, ExitState, define
from loguru import logger
from satori import Api

UPLOAD_FEATURE = "upload.create"
UPLOAD_TIMEOUT = 30.0
MEDIA = (Image, Audio, Video, File)


@dataclass(frozen=True)
class _Payload:
    data: bytes
    mime: str
    filename: str | None = None


@dataclass
class _Slot:
    """One media element of the (copied) message and where it sits."""

    parent: MutableSequence[Any]
    index: int
    element: Any
    payload: _Payload


def _local(src: Any) -> bool:
    return isinstance(src, str) and src[:5].lower() in ("data:", "file:")


def _children(element: Any) -> list[Any]:
    return list(getattr(element, "children", None) or [])


def _walk(parent: MutableSequence[Any]) -> Iterator[tuple[MutableSequence[Any], int, Any]]:
    for index, element in enumerate(parent):
        yield parent, index, element
        children = getattr(element, "children", None)
        if children:
            yield from _walk(children)


def _decode(src: str) -> _Payload:
    """Bytes of a ``data:`` or ``file:`` URL; raises ValueError/OSError if unreadable."""
    if src[:5].lower() == "data:":
        header, comma, body = src[5:].partition(",")
        if not comma:
            raise ValueError("data URL without a comma")
        params = header.split(";")
        mime = params[0].strip() or "text/plain"
        if any(p.strip().lower() == "base64" for p in params[1:]):
            try:
                data = base64.b64decode(body, validate=False)
            except binascii.Error as exc:
                raise ValueError(f"bad base64 in data URL: {exc}") from None
        else:
            data = unquote_to_bytes(body)
        return _Payload(data, mime)
    parsed = urlparse(src)
    path = Path(url2pathname(parsed.path))
    data = path.read_bytes()
    return _Payload(data, mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.name)


def _attrs(element: Any) -> dict[str, Any]:
    """The element's attributes, as Satori's ``unpack`` reads them."""
    attrs = dict(element.raw().attrs)
    if not attrs:  # built by the constructor, never serialised
        attrs = {
            f.name: getattr(element, f.name)
            for f in dataclasses.fields(element)
            if not f.name.startswith("_") and getattr(element, f.name, None) is not None
        }
    # ``unpack`` parses booleans from their text form.
    return {key: ("true" if value else "false") if isinstance(value, bool) else value for key, value in attrs.items()}


def _with_src(element: Any, src: str, payload: _Payload) -> Any:
    attrs = _attrs(element)
    attrs["src"] = src
    if isinstance(element, File) and not attrs.get("title") and payload.filename:
        attrs["title"] = payload.filename  # a file needs its name once the path is gone
    replaced = type(element).unpack(attrs)
    if children := _children(element):
        replaced(*children)
    return replaced


def _data_url(payload: _Payload) -> str:
    return f"data:{payload.mime};base64,{base64.b64encode(payload.data).decode('ascii')}"


def _filename(slot: _Slot, number: int) -> str:
    title = getattr(slot.element, "title", None)
    if isinstance(slot.element, File) and title:
        return str(title)
    if slot.payload.filename:
        return slot.payload.filename
    extension = mimetypes.guess_extension(slot.payload.mime) or ".bin"
    return f"{slot.element.tag}-{number}{extension}"


def supports_upload(account: Any) -> bool:
    info = getattr(account, "self_info", None)
    return UPLOAD_FEATURE in (getattr(info, "features", None) or ())


async def _upload(account: Any, channel_id: str, slots: list[_Slot]) -> dict[_Payload, str]:
    """One ``upload.create`` for every distinct payload; {} (and one warning) on failure."""
    keys: dict[_Payload, str] = {}
    fields: dict[str, Any] = {"channel_id": channel_id}
    for slot in slots:
        if slot.payload in keys:
            continue
        key = f"file{len(keys)}"
        keys[slot.payload] = key
        fields[key] = {
            "value": slot.payload.data,
            "filename": _filename(slot, len(keys) - 1),
            "content_type": slot.payload.mime,
        }
    try:
        result = await asyncio.wait_for(
            account.protocol.call_api(Api.UPLOAD_CREATE, fields, multipart=True), UPLOAD_TIMEOUT
        )
        if not isinstance(result, dict):
            raise TypeError(f"upload.create returned {type(result).__name__}, not an object")
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - the message still goes out, inline
        # Bodies are not logged: bridges may echo the whole payload or credentials.
        logger.warning(
            f"[outbound-media] upload.create failed, sending {len(keys)} media inline "
            f"channel={channel_id} error_type={type(exc).__name__}"
        )
        return {}
    urls = {payload: result.get(key) for payload, key in keys.items()}
    missing = [payload for payload, url in urls.items() if not (isinstance(url, str) and url)]
    if missing:
        logger.warning(
            f"[outbound-media] upload.create returned no URL for {len(missing)} of {len(keys)} media, "
            f"sending those inline channel={channel_id}"
        )
    return {payload: url for payload, url in urls.items() if isinstance(url, str) and url}


async def prepare(account: Any, channel_id: str, message: MessageChain) -> MessageChain | None:
    """The message to send instead of ``message``, or None if nothing changes."""
    upload = supports_upload(account)
    if not any(
        isinstance(el, MEDIA) and _local(el.src) and (upload or el.src[:5].lower() == "file:")
        for _, _, el in _walk(message)
    ):
        return None  # nothing to upload, nothing to read from disk: inline data stays as is
    prepared = copy.deepcopy(message)  # the caller may resend or cache its elements
    slots: list[_Slot] = []
    for parent, index, element in list(_walk(prepared)):
        if not (isinstance(element, MEDIA) and _local(element.src)):
            continue
        if not upload and element.src[:5].lower() == "data:":
            continue
        try:
            payload = await asyncio.to_thread(_decode, element.src) if element.src[:5].lower() == "file:" \
                else _decode(element.src)
        except (OSError, ValueError) as exc:
            logger.warning(
                f"[outbound-media] {element.tag} src unreadable, left as is: {type(exc).__name__}: {str(exc)[:120]}"
            )
            continue
        slots.append(_Slot(parent, index, element, payload))
    if not slots:
        return None
    urls = await _upload(account, channel_id, slots) if upload else {}
    changed = False
    for slot in slots:
        url = urls.get(slot.payload)
        if url is None and slot.element.src[:5].lower() == "data:":
            continue  # stays inline as it is
        slot.parent[slot.index] = _with_src(slot.element, url or _data_url(slot.payload), slot.payload)
        changed = True
    return prepared if changed else None


async def rewrite_outbound_media(event: SendRequest) -> MessageChain | None:
    try:
        return await prepare(event.account, event.channel, event.message)
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - never lose the message over its media
        logger.warning(f"[outbound-media] left the message unchanged: {type(exc).__name__}: {str(exc)[:120]}")
        return None


async def _no_lookup(event: SendRequest, context: Contexts) -> None:
    """Context for :func:`rewrite_outbound_media`: the event alone, no ``channel.get``."""
    return None


# A validator makes letoderea check this publisher on every post instead of
# caching the publishers of SendRequest the first time one is sent.
publisher = define(SendRequest, _no_lookup, lambda _event: True, name="otae.pipeline/outbound-media")

_installed = False


def install_outbound_media() -> None:
    """Subscribe once, in the root scope (plugin reloads keep it)."""
    global _installed
    if _installed:
        return
    from arclet.letoderea import use

    use(publisher, rewrite_outbound_media, priority=0)
    _installed = True
