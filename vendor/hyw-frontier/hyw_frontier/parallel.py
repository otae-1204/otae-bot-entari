"""Parallel v1 Search with a user-selected mode; Reader is a separate Jina client."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Callable

from .cleanup import clear_exception_frames
from .costs import ToolCosts
from .jina import JinaError, JsonTransport, public_url, request_json

SEARCH_ENDPOINT = "https://api.parallel.ai/v1/search"
SEARCH_MODE = "turbo"
SEARCH_MODES = ("turbo", "fast", "basic", "advanced")


class ParallelError(JinaError):
    """Uses the web tools' existing safe error contract."""


def load_parallel_key(home: Path) -> str:
    key = os.environ.get("PARALLEL_API_KEY", "").strip()
    if key:
        return key
    try:
        data = json.loads((home / "parallel.json").read_text(encoding="utf-8"))
        key = data.get("api_key", "")
    except FileNotFoundError:
        key = ""
    except (ValueError, OSError, AttributeError) as exc:
        raise ParallelError("invalid_config", "Parallel 配置无效，请检查独立目录的 parallel.json") from exc
    if not isinstance(key, str) or not key.strip():
        raise ParallelError("missing_key", "未配置 Parallel：设置 PARALLEL_API_KEY 或在独立目录 parallel.json 配置 api_key")
    return key.strip()


def parallel_request_json(endpoint: str, body: dict, headers: dict) -> dict:
    return request_json(endpoint, body, headers, provider="Parallel")


class ParallelClient:
    def __init__(self, home: Path, transport: Callable | None = None, *, mode: str = SEARCH_MODE):
        if mode not in SEARCH_MODES:
            raise ValueError("Unsupported Parallel search mode")
        self.mode = mode
        self.home = home
        self._transport = JsonTransport("Parallel") if transport is None else None
        self.transport = self._transport if transport is None else transport
        self._cache: dict[str, Future] = {}
        self._lock = Lock()
        self._network_slots = BoundedSemaphore(6)
        self._closed = False
        self.costs = ToolCosts()

    def close(self):
        with self._lock:
            self._closed = True
            futures = tuple(self._cache.values())
            self._cache.clear()
        for future in futures:
            if future.done() and not future.cancelled():
                error = future.exception()
                if error is not None:
                    clear_exception_frames(error)
        self.costs.clear()
        if self._transport is not None:
            self._transport.close()

    def search(self, item: dict) -> dict:
        query = " ".join(item["query"].split())
        if not query:
            raise ParallelError("invalid_query", "查询不能为空或仅包含空白")
        body = {"search_queries": [query], "mode": self.mode}
        advanced_settings = {}
        if "location" in item:
            advanced_settings["location"] = item["location"]
        if "after_date" in item:
            advanced_settings["source_policy"] = {"after_date": item["after_date"]}
        if advanced_settings:
            body["advanced_settings"] = advanced_settings
        identity = json.dumps(body, sort_keys=True)
        with self._lock:
            if self._closed:
                raise ParallelError("client_closed", "搜索客户端已关闭")
            cached = identity in self._cache
            future = self._cache.setdefault(identity, Future())
        if not cached:
            try:
                headers = {"Content-Type": "application/json", "Accept": "application/json",
                           "x-api-key": load_parallel_key(self.home)}
                payload = None
                try:
                    with self._network_slots:
                        payload = self.transport(SEARCH_ENDPOINT, body, headers)
                finally:
                    self.costs.record("parallel", "web_search", payload, mode=self.mode)
                if not isinstance(payload.get("results"), list):
                    raise ParallelError("invalid_response", "Parallel Search 未返回结果列表")
                rows = []
                for row in payload["results"]:
                    if not isinstance(row, dict):
                        continue
                    try:
                        url = public_url(str(row.get("url", "")))
                    except JinaError:
                        continue
                    excerpts = row.get("excerpts")
                    if not isinstance(excerpts, list) or not all(isinstance(part, str) for part in excerpts):
                        raise ParallelError("invalid_response", "Parallel Search 未返回有效 excerpts")
                    snippet = "\n\n".join(excerpts)
                    rows.append({"rank": len(rows) + 1, "title": str(row.get("title") or url),
                                 "url": url, "snippet": snippet, "snippet_truncated": False,
                                 "publish_date": str(row.get("publish_date") or ""),
                                 "evidence_type": "search_excerpt"})
                future.set_result({"query": query, "provider": "parallel", "mode": self.mode,
                                   "results": rows, "untrusted_content": True})
            except Exception as exc:
                future.set_exception(exc)
        result = deepcopy(future.result())
        # Keep the provider's default result scope; never crop returned excerpts.
        result["results_truncated"] = False
        result["empty"] = not result["results"]
        result["cached"] = cached
        return result
