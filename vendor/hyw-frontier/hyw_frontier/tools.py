"""Only project-defined tools. No dynamic imports, arbitrary commands or Pi tools."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
from threading import Lock
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker

from .ddgs import DDGSClient
from .jina import JinaClient, JinaError
from .parallel import ParallelClient, SEARCH_MODE
from .pageshot import PageshotStore
from .reverse_image import ENGINES

SEARCH_PROVIDER = "ddgs"
SEARCH_PROVIDERS = ("jina", "parallel", "ddgs")
# Temporarily disabled; keep the schema and implementation available for restoration.
DISABLED_TOOLS = frozenset({"fill_thinking", "set_reasoning"})
TOOL_FILE = Path(__file__).with_name("tools.json")
_REGISTRY = json.loads(TOOL_FILE.read_text(encoding="utf-8"))

def _provider_definition(value, provider):
    """Strip registry metadata and omit unsupported parameters/instructions."""
    if isinstance(value, list):
        return [_provider_definition(item, provider) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _provider_definition(item, provider) for key, item in value.items()
            if key != "search_providers"
            and not (isinstance(item, dict) and provider is not None
                     and provider not in item.get("search_providers", SEARCH_PROVIDERS))}


def tool_definitions(search_provider: str | None = None) -> list[dict]:
    if search_provider is not None and search_provider not in SEARCH_PROVIDERS:
        raise ValueError("Unsupported search provider")
    return [_provider_definition(tool, search_provider) for tool in _REGISTRY
            if tool["name"] not in DISABLED_TOOLS
            and (search_provider is None or search_provider in tool.get("search_providers", SEARCH_PROVIDERS))]


class ToolRuntime:
    def __init__(self, jina: JinaClient, search: ParallelClient | JinaClient | DDGSClient | None = None, *,
                 search_mode: str = SEARCH_MODE, search_provider: str = SEARCH_PROVIDER,
                 send: Callable[[str], None] | None = None):
        if search_provider not in SEARCH_PROVIDERS:
            raise ValueError("Unsupported search provider")
        if send is not None and not callable(send):
            raise TypeError("send must be callable or None")
        self.send = send
        self.set_reasoning: Callable[[str], dict] | None = None
        self.crop_user_image: Callable[[str, list[int]], tuple[dict, list[dict]]] | None = None
        self.reverse_image_search: Callable[[dict], dict] | None = None
        self.jina = jina
        self.pageshots = PageshotStore(jina)
        self.search_provider = search_provider
        self.definitions = tool_definitions(search_provider)
        self._validators = {tool["name"]: Draft202012Validator(tool["parameters"], format_checker=FormatChecker())
                            for tool in self.definitions}

        if search is not None:
            self.search = search
        elif search_provider == "ddgs":
            self.search = DDGSClient()
        else:
            self.search = jina if search_provider == "jina" else ParallelClient(jina.home, mode=search_mode)
        self.images = self.search if search_provider == "ddgs" else jina
        self.search_mode = self.search.mode if search_provider == "parallel" else None
        # Like search caches, this state belongs to one user task, not the conversation.
        self._intro_lock = Lock()
        self._intro_sent = False
        self._reader_intro_sent = False

    def cost_items(self):
        items = self.jina.costs.snapshot()
        if self.search is not self.jina:
            items += self.search.costs.snapshot()
        return items

    def close(self):
        self.send = None  # Do not retain the caller's callback/event loop through a traceback.
        self.crop_user_image = None
        self.reverse_image_search = None
        self.set_reasoning = None
        self.pageshots.close()
        try:
            self.jina.close()
        finally:
            if self.search is not self.jina:
                self.search.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _batch(items: list[dict], work: Callable, identity: str, on_item: Callable | None = None) -> dict:
        def run(indexed):
            index, item = indexed
            started = time.monotonic()
            if on_item:
                on_item({"type": "query_start", "query_index": index, "query": item.get(identity, ""), "arguments": item})
            result = {"ok": False, identity: item.get(identity, ""), "code": "tool_error"}
            try:
                result = {"ok": True, **work(item)}
            except JinaError as exc:
                result = {"ok": False, identity: item.get(identity, ""), "code": exc.code, "error": str(exc)}
            finally:
                if on_item:
                    on_item({"type": "query_end", "query_index": index, "query": item.get(identity, ""),
                             "duration_ms": round((time.monotonic() - started) * 1000, 2),
                             "ok": result["ok"], "cached": result.get("cached"), "code": result.get("code"),
                             "result_count": len(result.get("results", result.get("matches", [])))})
            return result
        with ThreadPoolExecutor(max_workers=min(5, len(items))) as pool:
            results = list(pool.map(run, enumerate(items)))
        successes = sum(row["ok"] for row in results)
        return {
            "ok": successes > 0, "partial": 0 < successes < len(results) or any(row.get("partial") for row in results), "results": results,
        }

    def execute(self, call: dict, *, on_query: Callable | None = None, _has_companion: bool = False,
                _has_reader: bool = False) -> dict:
        started = time.monotonic()
        name, args = call.get("name", ""), call.get("arguments")
        attachments = []
        try:
            if name not in self._validators:
                result = {"ok": False, "code": "unknown_tool", "error": "工具未注册或当前搜索服务不支持；当前可用：" + "、".join(self._validators) + "；不可用工具不会执行网络请求"}
            else:
                invalid = next(self._validators[name].iter_errors(args), None)
                if invalid:
                    path = ".".join(map(str, invalid.absolute_path)) or "arguments"
                    # No raw invalid values in errors; they could contain secrets or excessive text.
                    result = {"ok": False, "code": "invalid_arguments", "error": f"{path} 不符合 {invalid.validator} 约束，请按工具参数 schema 修正"}
                elif name == "set_reasoning":
                    result = (self.set_reasoning(args["level"]) if self.set_reasoning is not None else
                              {"ok": False, "code": "reasoning_unavailable", "error": "当前任务未启用动态思考，保持模型原有设置"})
                elif name == "fill_thinking":
                    # Arguments already live in the assistant tool call; do not echo or retain another copy.
                    result = {"ok": True}
                elif name == "crop_user_image":
                    if self.crop_user_image is None:
                        result = {"ok": False, "code": "no_user_images", "error": "当前没有可裁剪的用户原图"}
                    else:
                        result, attachments = self.crop_user_image(args["source_id"], args["bbox"])
                elif name == "reverse_image_search":
                    if self.reverse_image_search is None:
                        result = {"ok": False, "code": "image_search_unavailable", "error": "当前任务未初始化以图搜图"}
                    else:
                        notify = (lambda event: on_query({**event, "id": call.get("id", ""), "name": name,
                                                          "provider": "+".join(key for key, _ in ENGINES), "search_mode": None})) if on_query else None
                        result = self._batch([args], self.reverse_image_search,
                                             "source_id" if "source_id" in args else "url", notify)
                elif name == "send_process_intro":
                    with self._intro_lock:
                        if self._intro_sent and (not _has_reader or self._reader_intro_sent):
                            result = {"ok": False, "code": "intro_already_sent",
                                      "error": "过程介绍已发送；仅在尚未告知读取、整页截图或以图搜图计划时，可与 jina_read_url、jina_pageshot 或 reverse_image_search 同轮补充一次"}
                        elif not _has_companion:
                            result = {"ok": False, "code": "companion_required",
                                      "error": "过程介绍须与有效的 " + "、".join(
                                          tool for tool in ("web_search", "search_images", "jina_read_url", "jina_pageshot", "reverse_image_search")
                                          if tool in self._validators) + " 调用同轮发送；Reader 仍须符合使用条件"}
                        else:
                            # Reserve before delivery: a failed callback may already have sent the message.
                            # Never retry automatically or report delivery as successful when it raised.
                            supplemental = self._intro_sent
                            self._intro_sent = True
                            if _has_reader:
                                self._reader_intro_sent = True
                            text = args["text"].strip()
                            try:
                                if self.send is not None:
                                    self.send(text)
                            except Exception:
                                self._reader_intro_sent = True
                                result = {"ok": False, "code": "intro_delivery_failed",
                                          "error": "过程介绍发送回调失败，无法确认是否送达；不要重复发送，继续完成回答"}
                            else:
                                result = {"ok": True, "text": text, "supplemental": supplemental}
                elif name in ("web_search", "search_images"):
                    provider = ("ddgs" if self.search_provider == "ddgs" else "jina") if name == "search_images" else self.search_provider
                    mode = None if name == "search_images" else self.search_mode
                    notify = (lambda event: on_query({**event, "id": call.get("id", ""), "name": name,
                                                      "provider": provider, "search_mode": mode})) if on_query else None
                    result = (self._batch([args], self.images.search_images, "query", notify) if name == "search_images"
                              else self._batch(args["searches"], self.search.search, "query", notify))
                elif name == "jina_read_url":
                    notify = (lambda event: on_query({**event, "id": call.get("id", ""), "name": name,
                                                      "provider": "jina", "search_mode": None})) if on_query else None
                    result = self._batch([args], self.jina.read_url, "url", notify)
                elif name == "jina_pageshot":
                    notify = (lambda event: on_query({**event, 'id': call.get('id', ''), 'name': name})) if on_query else None
                    result, attachments = self.pageshots.run(args, on_event=notify)
                else:
                    result = {"ok": False, "code": "unknown_tool", "error": "工具未实现"}
        except Exception:
            result = {"ok": False, "code": "tool_error", "error": "工具执行异常，原始异常已隐藏以避免泄露密钥；不要把本次结果作为证据"}
        return {
            "role": "toolResult", "toolCallId": call.get("id", ""), "toolName": name,
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}, *attachments],
            "isError": not result["ok"], "timestamp": int(time.time() * 1000),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }

    def execute_many(self, calls: list[dict], on_result: Callable | None = None,
                     on_query: Callable | None = None) -> list[dict]:
        # Notify as each call settles, but replay to the model in original call order.
        if not calls:
            return []
        results = [None] * len(calls)
        companions = {
            call["name"] for call in calls
            if call.get("name") in self._validators and call["name"] in ("web_search", "search_images", "jina_read_url", "jina_pageshot", "reverse_image_search")
            and self._validators[call["name"]].is_valid(call.get("arguments"))
        }
        # Apply task settings serially before introductions/network work, never from worker threads.
        for index, call in enumerate(calls):
            if call.get("name") in ("fill_thinking", "set_reasoning"):
                results[index] = self.execute(call)
                if on_result:
                    on_result(results[index])
        # Deliver introductions before starting network work, even if the model listed them last.
        pending = []
        for index, call in enumerate(calls):
            if results[index] is not None:
                continue
            if call.get("name") == "send_process_intro":
                result = self.execute(call, _has_companion=bool(companions),
                                      _has_reader=bool({"jina_read_url", "jina_pageshot", "reverse_image_search"} & companions))
                results[index] = result
                if on_result:
                    on_result(result)
            else:
                pending.append((index, call))
        if not pending:
            return results
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
            futures = {pool.submit(self.execute, call, on_query=on_query): index
                       for index, call in pending}
            for future in as_completed(futures):
                result = future.result()
                results[futures[future]] = result
                if on_result:
                    on_result(result)
        return results
