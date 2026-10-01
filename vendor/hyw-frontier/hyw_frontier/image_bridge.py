"""Private, per-install image-host settings; never serialize the key into tool results."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from threading import Lock
from urllib.parse import urlsplit

from .jina import JinaError, public_url

_SAVE_LOCK = Lock()


def validate_bridge(data: dict) -> dict:
    try:
        url = public_url(data["url"].strip().rstrip("/"))
        key = data["api_key"]
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.path or parts.query or not isinstance(key, str)
                or not 1 <= len(key) <= 512 or any(ord(c) <= 32 or ord(c) >= 127 for c in key)):
            raise ValueError
        return {"url": url, "api_key": key}
    except (KeyError, TypeError, AttributeError, ValueError, JinaError):
        raise JinaError("invalid_image_bridge_config", "图床需要公开 HTTPS 根地址和有效上传密钥") from None


def load_bridge(home: Path) -> dict:
    try:
        return validate_bridge(json.loads((home / "image-bridge.json").read_text()))
    except FileNotFoundError:
        raise JinaError("image_bridge_not_configured", "请先在设置中配置图片图床地址和上传密钥") from None
    except (OSError, ValueError):
        raise JinaError("invalid_image_bridge_config", "无法读取本机图床配置，请在设置中重新保存") from None


def bridge_status(home: Path, *, include_url=False) -> dict:
    try:
        config = load_bridge(home)
        return {"configured": True, **({"url": config["url"]} if include_url else {})}
    except JinaError:
        return {"configured": False}


def save_bridge(home: Path, data: dict) -> dict:
    if not isinstance(data.get("url"), str) or not isinstance(data.get("api_key", ""), str):
        raise JinaError("invalid_image_bridge_config", "图床地址和上传密钥必须为文本")
    with _SAVE_LOCK:
        if not data.get("api_key"):
            previous = load_bridge(home)
            if data.get("url", "").strip().rstrip("/") != previous["url"]:
                raise JinaError("image_bridge_key_required", "修改图床地址时请同时填写该图床的上传密钥")
            data = {**data, "api_key": previous["api_key"]}
        config = validate_bridge(data)
        home.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".image-bridge-", dir=home)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(config, output)
            os.replace(name, home / "image-bridge.json")
        finally:
            Path(name).unlink(missing_ok=True)
    return {"configured": True, "url": config["url"]}
