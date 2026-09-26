from __future__ import annotations

import time
from hashlib import md5
from typing import Any
from urllib.parse import urlencode


# Bilibili's public mixin-key permutation.
MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def mixin_key(img_key: str, sub_key: str) -> str:
    return "".join((img_key + sub_key)[index] for index in MIXIN_KEY_TABLE)[:32]


def sign_params(
    params: dict[str, Any], img_key: str, sub_key: str, *, now: float | None = None
) -> dict[str, Any]:
    """Sign query parameters; without keys the parameters are returned unchanged."""
    if not img_key or not sub_key:
        return dict(params)
    signed = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in params.items()
    }
    signed["wts"] = int(time.time() if now is None else now)
    query = urlencode(dict(sorted(signed.items())))
    signed["w_rid"] = md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed
