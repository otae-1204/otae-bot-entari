"""Opaque image references resolved only against supplied in-memory assets."""
import hashlib
import re


def display_url(identity: str, raw: bytes) -> str:
    digest = hashlib.sha256(identity.encode() + b'\0' + raw).hexdigest()
    return 'hyw-media://image/' + digest


def is_display_url(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r'hyw-media://image/[0-9a-f]{64}', value) is not None
