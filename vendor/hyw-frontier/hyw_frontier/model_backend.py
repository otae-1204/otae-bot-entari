"""Task-owned Pydantic AI model transport; Python owns tools, history and cancellation.

SDK imports happen only on the model path, never in the offline rendering worker.
No Agent, router, telemetry instrumentation, background price updater or native tools.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack, asynccontextmanager
import email.utils
import json
import logging
import os
import random
from datetime import datetime, timezone
from queue import Empty, SimpleQueue
from threading import Event
import time

from .credentials import ModelConnection
from .errors import FrontierError
from .model_limits import resolve_output_limit
from .tools import SEARCH_PROVIDERS, tool_definitions


RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_LOG = logging.getLogger("hyw")


def _env_number(name: str, default, cast):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        return default


def http_status(error: BaseException) -> int | None:
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        for attr in ("status_code", "code"):
            value = getattr(current, attr, None)
            if type(value) is int and 100 <= value <= 599:
                return value
        response = getattr(current, "response", None)
        value = getattr(response, "status_code", None)
        if type(value) is int and 100 <= value <= 599:
            return value
        current = current.__cause__ or current.__context__
    return None


def retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:
        return None
    if not value:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def transient_failure(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True
    names = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return any(name in {"TimeoutException", "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout",
                        "RemoteProtocolError", "ReadError", "WriteError", "ConnectError"} for name in names)


def retry_delay(attempt: int, *, base: float, limit: float, retry_after: float | None) -> float:
    if retry_after is not None and retry_after > 0:
        return min(retry_after, limit)
    window = min(base * (2 ** max(0, attempt - 1)), limit)
    return window / 2 + random.uniform(0, window / 2)


def safe_model_error(error: Exception) -> FrontierError:
    status = http_status(error)
    code = f"http_{status}" if status else "model_error"
    message = {401: "模型认证失败，请检查 API Key 或服务账号凭据。", 402: "模型账户额度不足。",
               403: "模型接口拒绝访问。", 404: "模型或 API 端点不存在；未回退其他模型或协议。",
               408: "模型请求超时，请稍后重试。",
               429: "模型请求过于频繁或额度不足，请稍后重试。"}.get(
                   status, "模型请求失败；原始异常已隐藏以保护凭据。")
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        code, message = "timeout", "模型请求超时，请稍后重试。"
    return FrontierError(message, diagnostics={"code": code, "http_status": status,
                                               "retryable": status in RETRYABLE_STATUSES or transient_failure(error)})


def _content(blocks):
    from pydantic_ai.messages import BinaryContent
    if isinstance(blocks, str):
        return blocks
    content = []
    for block in blocks:
        if block.get("type") == "text":
            content.append(block["text"])
        elif block.get("type") == "image":
            if block.get("mimeType") not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                raise FrontierError("无效的历史图片类型。")
            content.append(BinaryContent(base64.b64decode(block["data"], validate=True), media_type=block["mimeType"]))
        else:
            raise FrontierError("不支持的消息内容类型。")
    return content


def model_messages(context: dict):
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart
    prompt = context.get("systemPrompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise FrontierError("系统提示词不能为空。")
    # Accept only an exact subset of one project-owned provider registry.
    # Provider-specific parameters intentionally differ.
    registries = [{t["name"]: t for t in tool_definitions(provider)}
                  for provider in (None, *SEARCH_PROVIDERS)]
    definitions = context.get("tools", [])
    if (len({t.get("name") for t in definitions}) != len(definitions)
            or not any(all(registry.get(t.get("name")) == t for t in definitions)
                       for registry in registries)):
        raise FrontierError("工具注册表不匹配，拒绝模型请求。")
    messages, pending = [], {}
    for message in context["messages"]:
        role = message.get("role")
        if role == "toolResult":
            call_id = message.get("toolCallId")
            if call_id not in pending or pending.pop(call_id) != message.get("toolName"):
                raise FrontierError("历史工具结果缺少匹配的调用。")
            part = ToolReturnPart(message["toolName"], _content(message["content"]), call_id,
                                  outcome="failed" if message.get("isError") else "success")
            if messages and isinstance(messages[-1], ModelRequest):
                messages[-1].parts = [*messages[-1].parts, part]
            else:
                messages.append(ModelRequest([part], instructions=prompt))
            continue
        if pending:
            raise FrontierError("历史工具调用缺少结果。")
        if role == "user":
            messages.append(ModelRequest([UserPromptPart(_content(message["content"]))], instructions=prompt))
        elif role == "assistant":
            parts = []
            provider = message.get("provider")
            for block in message["content"]:
                kind = block.get("type")
                identity = {"id": block.get("itemId"), "provider_name": provider}
                if kind == "text":
                    parts.append(TextPart(block["text"], **identity))
                elif kind == "thinking":
                    raw = block.get("rawThinking", False)
                    parts.append(ThinkingPart("" if raw else block["thinking"], **identity,
                                              signature=block.get("thinkingSignature"),
                                              provider_details={"raw_content": [block["thinking"]]} if raw else None))
                elif kind == "toolCall":
                    call_id = block.get("id")
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        raise FrontierError("历史工具调用 ID 无效或重复。")
                    pending[call_id] = block["name"]
                    parts.append(ToolCallPart(block["name"], block["arguments"], call_id, **identity))
                else:
                    raise FrontierError("不支持的助手消息内容类型。")
            messages.append(ModelResponse(parts, model_name=message.get("model"), provider_name=provider,
                                          provider_response_id=message.get("providerResponseId")))
        else:
            raise FrontierError("历史仅允许用户、助手和配对工具结果。")
    if pending or not messages:
        raise FrontierError("上下文为空或仍有未配对的工具调用。")
    return messages


def model_identity(model):
    """Read public Model metadata without inspecting credentials or changing settings."""
    from pydantic_ai.models import Model
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    if not isinstance(model, Model):
        raise TypeError("model 必须是模型 ID 字符串或 Pydantic AI Model 实例，不是类本身。")
    api = ("responses" if isinstance(model, OpenAIResponsesModel) else "chat" if isinstance(model, OpenAIChatModel)
           else "google" if model.system in ("google", "google-cloud", "google-vertex", "google-gla") else "native")
    return model.system, model.model_name, api


def with_reasoning(settings, api, effort):
    """Apply only request-local effort; preserve unrelated settings on borrowed models."""
    settings = dict(settings or {})
    if api in ("responses", "chat"):
        settings["openai_reasoning_effort"] = "none" if effort == "off" else effort
    elif api == "google" and effort in ("low", "medium", "high"):
        thinking = dict(settings.get("google_thinking_config") or {})
        thinking.pop("thinking_budget", None)  # Gemini 3 uses levels, not a simultaneous token budget.
        thinking.setdefault("include_thoughts", True)
        thinking["thinking_level"] = effort.upper()
        settings["google_thinking_config"] = thinking
    else:
        raise FrontierError("该模型接口不支持请求的思考等级。")
    return settings


def default_model_settings(api, timeout, reasoning=None, *, max_output_tokens=None):
    """Defaults only for models constructed by hyw, never for caller-supplied instances."""
    settings = {"timeout": timeout}
    if max_output_tokens is not None:
        settings["max_tokens"] = max_output_tokens
    if api in ("responses", "chat"):
        settings["openai_store"] = False
        if api == "responses":
            settings["openai_send_reasoning_ids"] = True
    return with_reasoning(settings, api, reasoning) if reasoning is not None else settings


def _response(message, provider: str, model: str, api: str) -> dict:
    from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart
    if message.finish_reason not in ("stop", "length", "tool_call"):
        raise FrontierError("模型未正常完成，拒绝将部分响应作为完整答案。", diagnostics={"code": "incomplete_response"})
    blocks = []
    for part in message.parts:
        identity = {"itemId": part.id} if part.id else {}
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.content, **identity})
        elif isinstance(part, ThinkingPart):
            raw = (part.provider_details or {}).get("raw_content")
            blocks.append({"type": "thinking", "thinking": "\n".join(raw) if raw else part.content,
                           **identity, **({"rawThinking": True} if raw else {}),
                           **({"thinkingSignature": part.signature} if part.signature else {})})
        elif isinstance(part, ToolCallPart):
            try:
                arguments = part.args_as_dict()
            except (ValueError, TypeError):
                raise FrontierError("模型返回无效工具参数 JSON；未执行工具。") from None
            blocks.append({"type": "toolCall", "id": part.tool_call_id, "name": part.tool_name,
                           "arguments": arguments, **identity})
        else:
            raise FrontierError("收到未注册的模型原生工具或输出类型，任务已停止。")
    usage = message.usage
    normalized_usage = {"input": max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens),
                        "output": usage.output_tokens, "cacheRead": usage.cache_read_tokens,
                        "cacheWrite": usage.cache_write_tokens,
                        "reasoning": usage.details.get("reasoning_tokens", usage.details.get("thoughts_tokens", 0)),
                        "totalTokens": usage.input_tokens + usage.output_tokens}
    if usage.cost is not None:
        normalized_usage["cost"] = {"total": float(usage.cost)}  # Catalog estimate, not an invoice.
    return {"role": "assistant", "content": blocks, "provider": message.provider_name or provider,
            "model": message.model_name or model, "api": api, "providerResponseId": message.provider_response_id,
            "usage": normalized_usage, "timestamp": int(time.time() * 1000),
            "stopReason": "length" if message.finish_reason == "length" else
                          "toolUse" if any(b["type"] == "toolCall" for b in blocks) else "stop"}


class ModelSession:
    """Task adapter for owned or borrowed models; borrowed clients stay on their owner loop."""

    def __init__(self, connection: ModelConnection | None, timeout: float, cancel_event=None,
                 *, model=None, loop=None, model_factory=None):
        self.connection = connection
        self.timeout = timeout
        self.cancel_event = cancel_event
        self.loop = loop
        self.runner = asyncio.Runner() if loop is None else None
        self.resources = AsyncExitStack()
        self.model = model
        self.model_factory = model_factory
        self.preserve_settings = model is not None or model_factory is not None
        self.provider, self.model_name, self.api = (model_identity(model) if model is not None
                                                  else (connection.provider, None, connection.api) if connection else (None, None, None))
        self.client = None
        self.closed = False
        # One budget for every model call in this question. Attempts include the first try.
        self._retry_attempts = max(1, _env_number("HYW_RETRY_ATTEMPTS", 3, int))
        self._retry_base = max(0.0, _env_number("HYW_RETRY_BASE_DELAY", 0.5, float))
        self._retry_max_delay = max(0.0, _env_number("HYW_RETRY_MAX_DELAY", 8.0, float))
        self._retry_budget = max(0.0, _env_number("HYW_RETRY_BUDGET", 20.0, float))
        self._retry_spent = 0.0

    async def _model(self, name, reasoning=None, *, resolve_capacity=True):
        if self.model is not None:
            if name != self.model_name:
                raise FrontierError("同一任务不能切换模型。")
            return self.model
        if self.model_factory is not None:
            self.model = await self.resources.enter_async_context(self.model_factory(name))
            self.provider, self.model_name, self.api = model_identity(self.model)
            if name != self.model_name:
                raise FrontierError("模型工厂返回的实例与请求模型不匹配。")
            return self.model
        config = self.connection
        settings = default_model_settings(config.api, self.timeout, reasoning)
        try:
            if config.api in ("responses", "chat"):
                from openai import AsyncOpenAI
                from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
                from pydantic_ai.providers.deepseek import DeepSeekProvider
                from pydantic_ai.providers.openai import OpenAIProvider
                self.client = await self.resources.enter_async_context(AsyncOpenAI(
                    api_key=config.api_key, base_url=config.base_url, max_retries=0, timeout=self.timeout))
                provider = (DeepSeekProvider(openai_client=self.client) if config.provider == "deepseek"
                            else OpenAIProvider(openai_client=self.client))
                model_class = OpenAIResponsesModel if config.api == "responses" else OpenAIChatModel
                self.model = model_class(name, provider=provider, settings=settings)
            elif config.api == "anthropic":
                from anthropic import AsyncAnthropic
                from pydantic_ai.models.anthropic import AnthropicModel
                from pydantic_ai.providers.anthropic import AnthropicProvider
                self.client = await self.resources.enter_async_context(AsyncAnthropic(
                    api_key=config.api_key, base_url=config.base_url, max_retries=0, timeout=self.timeout))
                self.model = AnthropicModel(name, provider=AnthropicProvider(anthropic_client=self.client), settings=settings)
            elif config.api == "google":
                from google.genai.types import HttpRetryOptions
                from pydantic_ai.models.google import GoogleModel
                from pydantic_ai.providers.google import GoogleProvider
                if config.service_account is not None:
                    from google.oauth2.service_account import Credentials
                    from pydantic_ai.providers.google_cloud import GoogleCloudProvider
                    try:
                        credentials = Credentials.from_service_account_info(
                            config.service_account, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                    except (ValueError, TypeError):
                        raise FrontierError("Google 服务账号私钥或凭据格式无效。") from None
                    provider = GoogleCloudProvider(credentials=credentials,
                                                   project=config.service_account["project_id"],
                                                   location=config.location,
                                                   retry_options=HttpRetryOptions(attempts=1))
                else:
                    provider = GoogleProvider(api_key=config.api_key, base_url=config.base_url,
                                              retry_options=HttpRetryOptions(attempts=1))
                await self.resources.enter_async_context(provider)
                self.client = provider.client
                self.resources.callback(self.client.close)
                self.resources.push_async_callback(self.client.aio.aclose)
                self.model = GoogleModel(name, provider=provider, settings=settings)
            if resolve_capacity:
                limit = await resolve_output_limit(config, self.client, name)
                self.model.settings["max_tokens"] = limit
            self.model_name = name
            return self.model
        except ImportError:
            raise FrontierError(f"缺少 {config.api} 接入依赖，请安装 hyw-frontier[{config.api}] 可选依赖。") from None

    async def _request(self, request, on_event):
        from pydantic_ai.messages import ModelResponse, PartStartEvent, PartDeltaEvent, TextPart, ThinkingPart, ToolCallPart, TextPartDelta, ThinkingPartDelta, ToolCallPartDelta
        from pydantic_ai.models import ModelRequestParameters
        from pydantic_ai.tools import ToolDefinition
        model = await self._model(request["model"], request.get("reasoning"))
        messages = model_messages(request["context"])
        parameters = ModelRequestParameters(function_tools=[ToolDefinition(
            name=t["name"], description=t["description"], parameters_json_schema=t["parameters"], strict=False)
            for t in request["context"]["tools"]], allow_text_output=True)
        # Never mutate a borrowed/shared Model; explicit dynamic effort is request-local.
        settings = (dict(model.settings) if model.settings else None) if self.preserve_settings else default_model_settings(
            self.api, self.timeout, request.get("reasoning"), max_output_tokens=model.settings["max_tokens"])
        if request.get("reasoning") is not None:
            settings = with_reasoning(settings, self.api, request["reasoning"])
        if self.provider == "deepseek" and self.api == "responses" and (settings or {}).get("openai_reasoning_effort") != "none":
            # Off-mode turns contain no reasoning item. DeepSeek requires the field when
            # replaying assistant history with tools after enabling thinking. Send an empty
            # raw item, never invented reasoning; leave real reasoning and stored history intact.
            for index, message in enumerate(messages):
                if (isinstance(message, ModelResponse) and message.provider_name == "deepseek"
                        and not any(isinstance(part, ThinkingPart) for part in message.parts)):
                    message.parts = [ThinkingPart("", id=f"rs_empty_{index}", provider_name="deepseek",
                                                  provider_details={"raw_content": [""]}), *message.parts]
        if request["command"] != "stream":
            return _response(await model.request(messages, settings, parameters), self.provider, request["model"], self.api)
        emit = on_event or (lambda event: None)
        emit({"type": "model_stream_start", "provider": self.provider, "model": request["model"], "api": self.api,
              "model_input": "instance" if self.preserve_settings else "model_id",
              "max_output_tokens": (settings or {}).get("max_tokens")})
        thinking_parts, thinking_text = {}, {}

        def emit_thinking(index, part):
            thinking_parts[index] = part
            raw = (part.provider_details or {}).get("raw_content")
            text = "\n".join(raw) if raw else part.content
            previous = thinking_text.get(index, "")
            if text != previous:
                if text.startswith(previous):
                    emit({"type": "thinking_delta", "index": index, "delta": text[len(previous):]})
                else:
                    emit({"type": "thinking_replace", "index": index, "content": text})
                thinking_text[index] = text

        async with model.request_stream(messages, settings, parameters) as stream:
            async for event in stream:
                if isinstance(event, PartStartEvent):
                    part, index = event.part, event.index
                    if isinstance(part, (TextPart, ThinkingPart)):
                        kind = "text" if isinstance(part, TextPart) else "thinking"
                        emit({"type": kind + "_start", "index": index})
                        if isinstance(part, ThinkingPart):
                            emit_thinking(index, part)
                        elif part.content:
                            emit({"type": kind + "_delta", "index": index, "delta": part.content})
                    elif isinstance(part, ToolCallPart):
                        emit({"type": "toolcall_start", "index": index, "id": part.tool_call_id, "name": part.tool_name})
                        if part.args:
                            emit({"type": "toolcall_delta", "index": index,
                                  "delta": part.args if isinstance(part.args, str) else json.dumps(part.args, ensure_ascii=False)})
                elif isinstance(event, PartDeltaEvent):
                    delta = event.delta
                    if isinstance(delta, ThinkingPartDelta):
                        # Responses raw reasoning arrives through provider_details, not content_delta.
                        # Apply the SDK's update callback; signatures remain transport-only metadata.
                        part = delta.apply(thinking_parts.get(event.index, ThinkingPart("")))
                        emit_thinking(event.index, part)
                    elif isinstance(delta, TextPartDelta) and delta.content_delta:
                        emit({"type": "text_delta", "index": event.index, "delta": delta.content_delta})
                    elif isinstance(delta, ToolCallPartDelta) and delta.args_delta:
                        emit({"type": "toolcall_delta", "index": event.index,
                              "delta": delta.args_delta if isinstance(delta.args_delta, str) else json.dumps(delta.args_delta, ensure_ascii=False)})
            result = _response(stream.get(), self.provider, request["model"], self.api)
        # End events carry authoritative blocks, including raw Responses reasoning.
        for index, block in enumerate(result["content"]):
            if block["type"] == "toolCall":
                emit({"type": "toolcall_end", "index": index, "toolCall": block})
            else:
                kind = block["type"]
                emit({"type": kind + "_end", "index": index, "content": block[kind]})
        emit({"type": "model_stream_end", "reason": result["stopReason"], "usage": result["usage"]})
        return result

    async def _guarded(self, operation, abort=None):
        task = asyncio.create_task(operation)
        try:
            async with asyncio.timeout(self.timeout):
                while not task.done():
                    if (self.cancel_event is not None and self.cancel_event.is_set()) or (abort is not None and abort.is_set()):
                        raise FrontierError("请求已取消。", diagnostics={"code": "cancelled"})
                    await asyncio.wait({task}, timeout=.02)
                return await task
        finally:
            if not task.done():
                task.cancel()
            # Always join SDK request cancellation before closing its connection.
            await asyncio.gather(task, return_exceptions=True)

    def _check_owner_loop(self):
        if self.loop is None or not self.loop.is_running():
            raise FrontierError("模型所属事件循环未运行。")
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self.loop:
            raise FrontierError("不能在模型所属事件循环中阻塞调用同步后端；请 await answer(...)。")

    def _plan_retry(self, error: BaseException, attempt: int) -> float | None:
        if self._retry_attempts <= 1 or self._retry_budget <= 0 or attempt >= self._retry_attempts:
            return None
        status = http_status(error)
        if status is not None:
            if status not in RETRYABLE_STATUSES:
                return None
        elif not transient_failure(error):
            return None
        wait = retry_delay(attempt, base=self._retry_base, limit=self._retry_max_delay,
                           retry_after=retry_after_seconds(error))
        if self._retry_spent + wait > self._retry_budget:
            return None
        self._retry_spent += wait
        return wait

    async def _interruptible_sleep(self, seconds: float):
        deadline = time.monotonic() + seconds
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise FrontierError("请求已取消。", diagnostics={"code": "cancelled"})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.2, remaining))

    async def _request_retried(self, request, on_event, abort=None):
        attempt = 1
        while True:
            if abort is not None and abort.is_set():
                raise FrontierError("请求已取消。", diagnostics={"code": "cancelled"})
            try:
                return await self._guarded(self._request(request, on_event), abort)
            except FrontierError:
                raise
            except Exception as error:
                wait = self._plan_retry(error, attempt)
                if wait is None:
                    raise
                _RETRY_LOG.warning("[hyw] retrying model request: attempt=%s wait=%.1fs status=%s",
                                   attempt + 1, wait, http_status(error))
                await self._interruptible_sleep(wait)
                attempt += 1

    def call(self, request: dict, on_event=None):
        if self.closed:
            raise FrontierError("模型连接已关闭。")
        try:
            if self.runner is not None:
                return self.runner.run(self._request_retried(request, on_event))
            # The agent is synchronous, but the borrowed model runs on the answer() caller's loop.
            # Dispatch events back on this worker, not on the caller's async loop.
            self._check_owner_loop()
            events, abort = SimpleQueue(), Event()
            pending = asyncio.run_coroutine_threadsafe(
                self._request_retried(request, events.put, abort), self.loop)
            try:
                while not pending.done() or not events.empty():
                    try:
                        event = events.get(timeout=.02)
                    except Empty:
                        continue
                    if on_event is not None:
                        on_event(event)
                return pending.result()
            except BaseException:
                abort.set()
                # Do not cancel the concurrent Future: it marks itself done before SDK cleanup.
                try:
                    pending.result()
                except BaseException:
                    pass
                raise
        except FrontierError:
            raise
        except Exception as error:
            raise safe_model_error(error) from None

    async def _list_models(self):
        await self._model("catalog", resolve_capacity=False)
        if self.connection.api == "google":
            return [{"id": row.name, "name": row.display_name or row.name} async for row in await self.client.aio.models.list()]
        page = await self.client.models.list()
        return [{"id": row.id, "name": getattr(row, "display_name", None) or row.id} async for row in page]

    def list_models(self):
        try:
            return self.runner.run(self._guarded(self._list_models()))
        except FrontierError:
            raise
        except Exception as error:
            raise safe_model_error(error) from None

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            # Only factory-created/owned clients are registered; never enter/exit a borrowed Model.
            await self.resources.aclose()
        finally:
            self.model = self.client = self.connection = self.model_factory = self.loop = None

    def close(self):
        if self.closed:
            return
        if self.runner is None:
            self._check_owner_loop()
            asyncio.run_coroutine_threadsafe(self.aclose(), self.loop).result()
        else:
            try:
                self.runner.run(self.aclose())
            finally:
                self.runner.close()


@asynccontextmanager
async def configured_model(connection: ModelConnection, name: str, timeout: float, reasoning=None):
    """Build an owned native Model with UI settings; yield the same type users pass to answer()."""
    session = ModelSession(connection, timeout, loop=asyncio.get_running_loop())
    try:
        yield await session._model(name, reasoning)
    finally:
        await session.aclose()
