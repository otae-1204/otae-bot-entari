"""统一 JSON 持久化存储 — 替代分散的 openJson/saveJson/Plugin_Data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from functools import wraps


class JsonStore:
    """基于文件的 JSON 键值存储，自动初始化 + 线程安全写入."""

    def __init__(self, file_path: str | Path) -> None:
        self._path = Path(file_path)
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save()

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, indent=4, ensure_ascii=False), encoding="utf-8"
        )

    # ── dict-like access ──
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        self.delete(key)

    def all(self) -> Dict[str, Any]:
        return dict(self._data)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    def values(self):
        return self._data.values()

    # ── 嵌套访问 ──
    def deep_get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return default
        return node

    def deep_set(self, value: Any, *keys: str) -> None:
        if not keys:
            return
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        self._save()


def auto_save(method):
    """装饰器：方法执行后自动保存."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self._save()
        return result
    return wrapper
