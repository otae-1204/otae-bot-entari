"""Keyless DDGS text/image search with task-scoped single-flight caching."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
from threading import BoundedSemaphore, Lock

from .cleanup import clear_exception_frames
from .costs import ToolCosts
from .jina import JinaError, public_url

# DDGS uses country-language regions rather than the other providers' country codes.
REGIONS = {
    "ar": "ar-es", "au": "au-en", "at": "at-de", "be": "be-fr", "br": "br-pt",
    "ca": "ca-en", "cl": "cl-es", "cn": "cn-zh", "dk": "dk-da", "fi": "fi-fi",
    "fr": "fr-fr", "de": "de-de", "gr": "gr-el", "hk": "hk-tzh", "in": "in-en",
    "id": "id-id", "it": "it-it", "jp": "jp-jp", "my": "my-en", "mx": "mx-es",
    "nl": "nl-nl", "nz": "nz-en", "no": "no-no", "ph": "ph-en", "pl": "pl-pl",
    "pt": "pt-pt", "ru": "ru-ru", "sa": "sa-ar", "za": "za-en", "kr": "kr-kr",
    "es": "es-es", "se": "se-sv", "ch": "ch-de", "tw": "tw-tzh", "tr": "tr-tr",
    "gb": "uk-en", "us": "us-en",
}
SEARCH_CONFIG = {"library": "ddgs", "requires_api_key": False, "backend": "auto",
                 "region": "us-en", "safesearch": "moderate", "max_results": 10,
                 "timeout_seconds": 10, "max_concurrent_searches": 3,
                 "time_filter": "timelimit", "image_provider": "ddgs"}


class DDGSClient:
    def __init__(self):
        self._cache: dict[str, Future] = {}
        self._lock = Lock()
        self._network_slots = BoundedSemaphore(SEARCH_CONFIG["max_concurrent_searches"])
        self._closed = False
        self.costs = ToolCosts()

    def close(self):
        # Tool executors are joined by the owner before close.
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

    def search(self, item: dict) -> dict:
        return self._search(item, images=False)

    def search_images(self, item: dict) -> dict:
        return self._search(item, images=True)

    def _search(self, item: dict, *, images: bool) -> dict:
        query = " ".join(item["query"].split())
        if not query:
            raise JinaError("invalid_query", "查询不能为空或仅包含空白")
        if "after_date" in item:
            raise JinaError("invalid_arguments", "DDGS 不支持 after_date，请使用 timelimit: d/w/m/y 或省略时间筛选")
        if "location" in item and item["location"] not in REGIONS:
            raise JinaError("invalid_arguments", "DDGS location 必须是工具定义中的地区代码")
        if "timelimit" in item and item["timelimit"] not in ("d", "w", "m", "y"):
            raise JinaError("invalid_arguments", "DDGS timelimit 仅支持 d/w/m/y")
        params = {"query": query, "region": REGIONS[item["location"]] if "location" in item else SEARCH_CONFIG["region"],
                  "safesearch": SEARCH_CONFIG["safesearch"], "backend": SEARCH_CONFIG["backend"],
                  "max_results": SEARCH_CONFIG["max_results"], "timelimit": item.get("timelimit")}
        identity = json.dumps({"images": images, **params}, sort_keys=True)
        with self._lock:
            if self._closed:
                raise JinaError("client_closed", "搜索客户端已关闭")
            cached = identity in self._cache
            future = self._cache.setdefault(identity, Future())
        if not cached:
            try:
                future.set_result(self._fetch(params, images=images))
            except Exception as exc:
                future.set_exception(exc)
        result = deepcopy(future.result())
        result["cached"] = cached
        return result

    def _fetch(self, params: dict, *, images: bool) -> dict:
        # Lazy import keeps the search SDK off the offline rendering path. Each query
        # owns its SDK object: engines mutate cookies and must not be shared across queries.
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        try:
            with self._network_slots:
                with DDGS(timeout=SEARCH_CONFIG["timeout_seconds"]) as client:
                    payload = (client.images if images else client.text)(**params)
        except TimeoutException:
            raise JinaError("network_error", "DDGS 搜索超时；可稍后再试或换用其他来源") from None
        except RatelimitException:
            raise JinaError("http_429", "DDGS 搜索被限流；请稍后再试，不要立即重复检索") from None
        except DDGSException as exc:
            # The library raises for both empty results and upstream failures. Only its
            # explicit no-results sentinel is a successful empty search.
            if str(exc) == "No results found.":
                payload = []
            else:
                raise JinaError("upstream_error", "DDGS 搜索服务暂不可用；请稍后再试或换用其他来源") from None
        finally:
            self.costs.record("ddgs", "search_images" if images else "web_search", None, free=True)
        if not isinstance(payload, list):
            raise JinaError("invalid_response", "DDGS 未返回结果列表")
        rows = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                url = public_url(str(row.get("url" if images else "href", "")))
                image_url = public_url(str(row.get("image", ""))) if images else None
            except JinaError:
                continue
            rows.append({"rank": len(rows) + 1, "title": str(row.get("title") or url),
                         "url": url, "snippet": str(row.get("body") or ""), "snippet_truncated": False,
                         "evidence_type": "image_search" if images else "search_excerpt",
                         **({"image_url": image_url} if images else {})})
        return {"query": params["query"], "provider": "ddgs", "mode": None,
                "backend": params["backend"], "region": params["region"], "timelimit": params["timelimit"],
                "max_results": params["max_results"], "results": rows, "results_truncated": False,
                "empty": not rows, "untrusted_content": True}
