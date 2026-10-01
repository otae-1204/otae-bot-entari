"""Headless library entry point. No HTTP server, browser DOM, or image-store dependency."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from threading import Event
import time
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pydantic_ai.models import Model

from md2png.fonts import FontSet

from .agent import SearchAgent
from .cleanup import clear_exception_frames
from .costs import Costs, model_items, summarize
from .jina import JinaClient
from .media import MAX_IMAGES, MAX_READER_IMAGES
from .rendering import CardRenderer, RenderError
from .render_protocol import answer_text, parse_answer
from .runtime import Bridge, DEFAULT_LANGUAGE, DEFAULT_MODEL, DEFAULT_PROVIDER, build_context, load_prompt, render_prompt
from .source_titles import source_titles
from .tools import ToolRuntime

Send = Callable[[str], object | Awaitable[object]]


class ModelBackend(Protocol):
    """Inject a caller-owned backend instead of the default Pydantic AI model transport."""
    home: Path

    def call(self, request: dict) -> dict: ...


@dataclass(frozen=True)
class Answer:
    text: str
    png: bytes | None
    messages: list[dict]
    answer_ms: int
    render_ms: int
    truncated: bool
    diagnostics: tuple[dict, ...]
    links: tuple[str, ...] = ()
    costs: Costs = field(default_factory=Costs)
    kind: Literal['text', 'image'] = 'image'
    display_text: str = ''


async def answer(
    question: str,
    *,
    send: Send = print,
    on_event: Callable[[dict], None] | None = None,
    language: str = DEFAULT_LANGUAGE,
    provider: str | None = None,
    model: str | Model = DEFAULT_MODEL,
    api: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    home: Path | None = None,
    search_provider: str = "parallel",
    search_mode: str = "turbo",
    reasoning: dict[str, str] | None = None,
    reasoning_mode: Literal["auto", "high", "medium", "low"] = "auto",
    max_rounds: int = 30,
    max_tool_images: int = MAX_IMAGES,
    max_reader_images: int = MAX_READER_IMAGES,
    reader_engine: Literal['default', 'browser'] = 'browser',
    timeout: float = 300,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
    images: list[dict] | None = None,
    message_content: list[dict] | None = None,
    fonts: FontSet | None = None,
    backend: ModelBackend | None = None,
) -> Answer:
    """Return a typed reply without starting the test UI.

    `kind='text'`: send `display_text` directly; `png=None`, `render_ms=0`.
    `kind='image'`: Markdown is rendered to `png` bytes; `display_text` is the
    cleaned Markdown body for inspection. `text` always retains the raw model output.
    Rendering failures raise RenderError; image answers never fall back to text.

    `send(text)` is invoked ONLY by a valid `send_process_intro` tool call: normally
    once per question, with one optional pre-read supplement. Sync and async callbacks
    run on the caller's event loop.
    Default: print. No fabricated progress message if the model omits the tool.
    Callback failure is reported to the model as intro_delivery_failed; it is not
    retried because delivery may already have occurred. Tool results remain in messages.
    The final reply is returned, not sent through `send`.
    `on_event` is an optional synchronous, thread-safe diagnostics observer, called
    from worker threads. It receives copied model/tool/media/render events; it must
    not block. Observer failures disable further diagnostics, not the answer.
    Raw events may contain model text/tool results; the caller owns redaction and retention.
    `language` defaults to "中文" and fills {{language}} in the prompt for final
    answers and process messages. Custom prompts may use the same placeholder.

    Rendering uses bundled, offline Noto fonts unless `fonts` is explicitly supplied.
    History is copied; credentials/settings are scoped to `home` (or backend.home).
    No conversation, logs or images are persisted by this entry point; rendering
    uses private temporary files that are removed after the request.
    The default model backend is Pydantic AI Slim; no Node/npm runtime is needed.
    Built-in DeepSeek configuration always uses Responses. Other compatible endpoints can choose `api`.
    `base_url` and `api_key` override project-local connection settings.
    A native Pydantic AI `Model` instance may replace the model ID. Its provider and
    settings are authoritative; do not also pass provider/api/base_url/api_key/backend.
    `reasoning` optionally selects effort: exactly high/medium/low keys, each
    mapped to off/low/high/max for verified DeepSeek models, or low/medium/high for
    Gemini 3.8 Flash (duplicates allowed).
    Dynamic switching is temporarily disabled. `reasoning_mode='auto'` keeps medium
    throughout each question; high/medium/low locks the corresponding tier.
    Model IDs use the project mapping by default; native Models keep their own settings
    when reasoning is omitted. Explicit mappings override only request-local effort.
    Its requests run on this caller's event loop. Its client lifecycle remains caller-owned;
    timeout/cancellation stop and join this invocation, not other users of that client.
    `home` still controls search credentials for this form.
    `max_tool_images` defaults to 600; non-negative integer, 0 disables new tool
    images. History tool images count toward this budget; user/record images do not.
    `max_reader_images` defaults to 30 candidate attempts per Reader page;
    non-negative integer, 0 disables new Reader images. Text remains complete.
    `reader_engine` selects Jina's default fetching or forced browser rendering.
    `message_content` is an ordered list of text/image blocks for parsed chat records
    or cards, mutually exclusive with `images`. Maximum 256 MiB including question
    text and decoded image bytes; callers truncate before submitting. Ordinary
    `images` retains its four-image limit. Provider context/payload limits still apply.
    `links` reuses the renderer's ordered, deduplicated HTTP(S) reference list.
    `costs` covers only this invocation, not history; missing charges remain None.
    SDK catalog estimates are separate from actual USD charges.
    """
    if not callable(send):
        raise TypeError("send must be callable")
    if on_event is not None and (not callable(on_event) or inspect.iscoroutinefunction(on_event)):
        raise TypeError("on_event must be a synchronous, thread-safe callback")
    if type(max_tool_images) is not int or max_tool_images < 0:
        raise ValueError('max_tool_images must be a non-negative integer')
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if backend is not None and any(value is not None for value in (home, api, base_url, api_key)):
        raise ValueError("Choose backend or connection settings; backend owns its credentials and transport")
    native_model = None
    try:
        if isinstance(model, str):
            provider = DEFAULT_PROVIDER if provider is None else provider
        else:
            if any(value is not None for value in (provider, api, base_url, api_key, backend)):
                raise ValueError("A Model instance owns provider and settings; do not combine it with connection/backend arguments")
            from .model_backend import model_identity
            native_model = model
            provider, model, _ = model_identity(native_model)
        prompt = (load_prompt(language=language) if system_prompt is None
                  else render_prompt(system_prompt, language=language))
        context = build_context(question, prompt, deepcopy(history), images=images, message_content=message_content)
    except BaseException as exc:
        clear_exception_frames(exc)
        history = images = message_content = backend = fonts = send = model = native_model = on_event = None
        raise
    loop = asyncio.get_running_loop()
    cancelled = Event()

    def observe(event: dict):
        # Diagnostics must not mutate agent state or turn a successful request into a failure.
        nonlocal on_event
        if on_event is not None:
            try:
                on_event(deepcopy(event))
            except Exception:
                on_event = None

    async def deliver(text: str):
        result = send(text)
        if inspect.isawaitable(result):
            await result

    def send_from_worker(text: str):
        if cancelled.is_set():
            raise RuntimeError("request cancelled before introduction delivery")
        pending = asyncio.run_coroutine_threadsafe(deliver(text), loop)
        try:
            deadline = time.monotonic() + timeout
            while not pending.done():
                if cancelled.wait(.05):
                    raise RuntimeError("request cancelled during introduction delivery")
                if time.monotonic() >= deadline:
                    raise TimeoutError("introduction delivery timed out")
            pending.result()
        except BaseException:
            pending.cancel()
            raise

    def run() -> Answer:
        bridge = jina = tools = agent = renderer = response = card = None
        try:
            with ExitStack() as resources:
                bridge = backend or Bridge(home, timeout, cancelled, api=api, base_url=base_url, api_key=api_key,
                                           model=native_model, loop=loop if native_model is not None else None)
                jina = JinaClient(bridge.home, reader_engine=reader_engine)
                # Also covers ToolRuntime construction failure; close is idempotent.
                resources.callback(jina.close)
                with ToolRuntime(jina, search_provider=search_provider,
                                 search_mode=search_mode, send=send_from_worker) as tools:
                    agent = SearchAgent(bridge, tools, max_rounds=max_rounds, max_tool_images=max_tool_images, cancel_event=cancelled,
                                        max_reader_images=max_reader_images, reader_engine=reader_engine,
                                        reasoning=reasoning, reasoning_mode=reasoning_mode,
                                        search_provider=search_provider, search_mode=search_mode)
                    resources.callback(agent.release)
                    # Do not force simple caller-owned backends into the streaming protocol.
                    agent.on_event = observe
                    started = time.monotonic()
                    response = agent.run(provider, model, context)
                    costs = summarize([*model_items(agent.context["messages"][len(context["messages"]):], provider, model),
                                       *tools.cost_items()])
                text = "".join(b["text"] for b in response["content"] if b["type"] == "text")
                truncated = response.get("stopReason") == "length"
                parsed = parse_answer(text)
                kind = 'text' if parsed['mode'] == 'text' else 'image'
                display_text = answer_text(parsed)
                rendering = time.monotonic()
                observe({"type": "answer_ready", "text": text, "truncated": truncated,
                         "kind": kind, "display_text": display_text,
                         "available_image_urls": list(agent.image_assets)})
                if kind == 'text':
                    from md2png.model import Limits
                    from .pillow_card import adapt_answer
                    document = adapt_answer(parsed, {}, Limits())
                    return Answer(text=text, png=None, messages=deepcopy(agent.context['messages']),
                                  answer_ms=round((rendering - started) * 1000), render_ms=0,
                                  truncated=truncated, diagnostics=(),
                                  links=tuple(ref.url for ref in document.references), costs=costs,
                                  kind=kind, display_text=display_text)
                observe({"type": "render_start", "engine": "Pillow/md2png-hyw"})
                renderer = CardRenderer()
                resources.callback(renderer.close)
                try:
                    card = renderer.render(text, metadata={"truncated": truncated,
                                                          "source_titles": source_titles(agent.context["messages"])},
                                           cancel=cancelled, max_tool_images=max_tool_images,
                                           font_set=fonts or FontSet.bundled(),
                                           **({'image_assets': agent.image_assets} if agent.image_assets else {}),
                                           **({'favicon_assets': agent.favicon_assets} if agent.favicon_assets else {}))
                except RenderError as error:
                    observe({"type": "render_error", "code": error.code,
                             "duration_ms": round((time.monotonic() - rendering) * 1000)})
                    raise
                observe({"type": "render_end", "duration_ms": round((time.monotonic() - rendering) * 1000),
                         "image": {"mime_type": "image/png", "bytes": len(card.png)},
                         "stages_ms": card.timings,
                         "diagnostics": list(card.diagnostics)})
                return Answer(text, card.png, deepcopy(agent.context["messages"]),
                              round((rendering - started) * 1000), round((time.monotonic() - rendering) * 1000),
                              truncated, card.diagnostics, card.links, costs, kind, display_text)
        except BaseException as exc:
            clear_exception_frames(exc)
            raise
        finally:
            # The running frame cannot be cleared by traceback.clear_frames().
            bridge = jina = tools = agent = renderer = response = card = None

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # Repeated cancel() calls must not interrupt cleanup and orphan a worker.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    clear_exception_frames(error)
            raise
    except BaseException as exc:
        clear_exception_frames(exc)
        raise
    finally:
        context.clear()
        history = images = message_content = backend = fonts = send = task = model = native_model = None
        question = system_prompt = prompt = api_key = on_event = None
