"""统一 JSON 持久化存储 — 替代分散的 openJson/saveJson/Plugin_Data."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import threading
from typing import Any, Dict, Optional
from functools import wraps


class JsonStore:
    """基于文件的 JSON 键值存储，自动初始化 + 线程安全写入.

    内存里的 ``_data`` 只在调用方线程（通常是事件循环）上修改；协程里用
    ``set_async`` / ``save_async``：在调用方线程序列化出一致的快照，再到工作线程
    写盘。每次保存带递增版本号，旧快照不会覆盖已经写下的新快照。
    """

    def __init__(self, file_path: str | Path) -> None:
        self._path = Path(file_path)
        self._data: Dict[str, Any] = {}
        self._versions = itertools.count(1)  # next() is atomic: callers may be on any thread
        self._written_version = 0
        self._write_lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save()

    def _dump(self) -> str:
        return json.dumps(self._data, indent=4, ensure_ascii=False)

    def _write(self, version: int, text: str) -> None:
        with self._write_lock:
            if version <= self._written_version:
                return  # a newer snapshot is already on disk
            self._path.write_text(text, encoding="utf-8")
            self._written_version = version

    def _save(self) -> None:
        self._write(next(self._versions), self._dump())

    async def save_async(self) -> None:
        """Snapshot ``_data`` on the calling thread, write the file in a worker thread."""
        version, text = next(self._versions), self._dump()
        await asyncio.to_thread(self._write, version, text)

    # ── dict-like access ──
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    async def set_async(self, key: str, value: Any) -> None:
        self._data[key] = value
        await self.save_async()

    def setdefault(self, key: str, default: Any) -> Any:
        """Insert ``default`` in memory only; persist with ``save_async()``."""
        return self._data.setdefault(key, default)

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
