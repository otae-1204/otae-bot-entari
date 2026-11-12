"""Mask phone numbers, SMS codes and tokens in Entari's ``[message]`` log.

Entari's built-in ``record_message`` plugin logs every received message verbatim
(``arclet/entari/core.py``: ``log.message.info(f"{scene} {nick}({user_id}) -> {content!r}")``).
Credential dialogs ask for a phone number, an SMS code or a token in private
chat, so the ``[message]`` logger is patched before any sink sees the line:

- every message: mainland mobile numbers become ``138****1234``;
- messages from a user inside :func:`sensitive_input`: 4–8 digit codes become
  ``******`` and token-like strings of 16 or more characters are hidden too.

Other text, the scene, nickname and user id are logged unchanged.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

PHONE = re.compile(r"(?<![0-9])(1[3-9][0-9])[0-9]{4}([0-9]{4})(?![0-9])")
# Not the visible tail of an already masked phone number, so redacting twice changes nothing.
CODE = re.compile(r"(?<![0-9A-Za-z*])[0-9]{4,8}(?![0-9A-Za-z])")
TOKEN = re.compile(r"[A-Za-z0-9+/=_.%-]{16,}")
# The nickname may contain parentheses; the user id is the group right before " -> ".
LINE = re.compile(r"(?P<head>.*?\((?P<user>[^()]*)\) -> )(?P<content>.*)", re.S)

_sensitive: Counter[str] = Counter()
_installed = None


@contextmanager
def sensitive_input(user_id: str) -> Iterator[None]:
    """Also hide codes and tokens in this user's messages until the dialog ends."""
    user_id = str(user_id)
    _sensitive[user_id] += 1
    try:
        yield
    finally:
        _sensitive[user_id] -= 1
        if _sensitive[user_id] <= 0:
            del _sensitive[user_id]


def is_sensitive(user_id: str) -> bool:
    return str(user_id) in _sensitive


def redact(text: str, *, sensitive: bool = False) -> str:
    if sensitive:
        text = TOKEN.sub("<已隐藏>", text)
        text = CODE.sub("******", text)
    return PHONE.sub(r"\1****\2", text)


def redact_record(record) -> None:
    message = record["message"]
    line = LINE.fullmatch(message)
    if line is None:
        record["message"] = redact(message)
    else:
        record["message"] = line["head"] + redact(line["content"], sensitive=is_sensitive(line["user"]))


def install_message_log_redaction() -> None:
    """Patch Entari's ``[message]`` logger; repeated calls keep a single patch."""
    global _installed
    from arclet.entari.logger import log

    current = log.loggers.get("[message]")
    if current is None or current is _installed:
        return
    _installed = log.loggers["[message]"] = current.patch(redact_record)
