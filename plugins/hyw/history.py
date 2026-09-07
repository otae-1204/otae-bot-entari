"""Bounded, expiring reply history, isolated by bot, channel and sender."""

from __future__ import annotations

import json
from collections import OrderedDict
from copy import deepcopy
from time import monotonic

Scope = tuple[str, str, str, str, str]


class HistoryStore:
    def __init__(self, *, ttl: float = 3600, max_entries: int = 128, max_bytes: int = 4_000_000):
        self.ttl, self.max_entries, self.max_bytes = ttl, max_entries, max_bytes
        self._entries: OrderedDict[tuple[Scope, str], tuple[float, list[dict], int]] = OrderedDict()
        self._bytes = 0

    def _remove(self, key):
        self._bytes -= self._entries.pop(key)[2]

    def _prune(self):
        now = monotonic()
        for key, (expires, _, _) in list(self._entries.items()):
            if expires <= now:
                self._remove(key)

    def get(self, scope: Scope, message_id: str) -> list[dict]:
        self._prune()
        entry = self._entries.get((scope, message_id))
        return deepcopy(entry[1]) if entry else []

    def put(self, scope: Scope, message_id: str, history: list[dict]):
        self._prune()
        # Continuations keep text and final answers, never base64 images or tool transcripts.
        clean = []
        for message in history[-12:]:
            content = message["content"]
            if isinstance(content, list):
                content = "\n".join(
                    part["text"] if part["type"] == "text" else "[上一轮图片，需再次分析时请重新发送]"
                    for part in content
                )
            entry = {"role": message["role"], "content": str(content)[:6000]}
            if message["role"] == "assistant" and message.get("_sources"):
                entry["_sources"] = deepcopy(message["_sources"])
            clean.append(entry)
        size = len(json.dumps(clean, ensure_ascii=False).encode())
        if size > self.max_bytes:
            return
        key = (scope, message_id)
        if key in self._entries:
            self._remove(key)
        self._entries[key] = (monotonic() + self.ttl, clean, size)
        self._bytes += size
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            self._remove(next(iter(self._entries)))

    def clear(self, scope: Scope):
        for key in list(self._entries):
            if key[0] == scope:
                self._remove(key)
