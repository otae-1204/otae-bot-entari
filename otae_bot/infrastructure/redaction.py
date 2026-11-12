"""Mask phone numbers, SMS codes and tokens in free text.

Pure string helpers shared by the ``[message]`` log redaction
(:mod:`otae_bot.pipeline.message_log`) and the in-memory alert buffer
(:mod:`otae_bot.infrastructure.recent_alerts`):

- always: mainland mobile numbers become ``138****1234``;
- ``sensitive=True``: 4–8 digit codes become ``******`` and token-like strings
  of 16 or more characters are hidden too.
"""

from __future__ import annotations

import re

PHONE = re.compile(r"(?<![0-9])(1[3-9][0-9])[0-9]{4}([0-9]{4})(?![0-9])")
# Not the visible tail of an already masked phone number, so redacting twice changes nothing.
CODE = re.compile(r"(?<![0-9A-Za-z*])[0-9]{4,8}(?![0-9A-Za-z])")
TOKEN = re.compile(r"[A-Za-z0-9+/=_.%-]{16,}")


def redact(text: str, *, sensitive: bool = False) -> str:
    if sensitive:
        text = TOKEN.sub("<已隐藏>", text)
        text = CODE.sub("******", text)
    return PHONE.sub(r"\1****\2", text)
