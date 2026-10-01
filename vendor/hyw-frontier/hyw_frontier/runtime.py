"""Python-owned context, credentials and task-scoped model lifecycle. No Node bridge."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
from threading import Event

from .credentials import CredentialStore, PROVIDERS
from .errors import FrontierError
from .image_input import IMAGE_ONLY_TEXT, validate_images, validate_message_content
from .media import MAX_IMAGES, MAX_READER_IMAGES
from .tools import tool_definitions

PACKAGE = Path(__file__).resolve().parent
DEFAULT_PROMPT = PACKAGE / "prompts" / "system.md"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_LANGUAGE = "中文"
DEFAULT_MODEL = "deepseek-flash"


def render_prompt(prompt: str, *, language: str = DEFAULT_LANGUAGE) -> str:
    if not isinstance(language, str):
        raise TypeError("language 必须是字符串。")
    language = language.strip()
    if not language or len(language) > 100 or any(ord(char) < 32 for char in language):
        raise ValueError("language 必须是1–100字符的单行语言名称。")
    if not prompt.strip():
        raise FrontierError("系统提示词不能为空；不会回退到任何默认提示词。")
    now = datetime.now().astimezone()
    return (prompt.replace("{{current_date}}", now.date().isoformat())
            .replace("{{current_time}}", now.isoformat(timespec="seconds"))
            .replace("{{language}}", language))


def load_prompt(path: Path = DEFAULT_PROMPT, *, language: str = DEFAULT_LANGUAGE) -> str:
    return render_prompt(path.read_text(encoding="utf-8"), language=language)


def build_context(text: str, system_prompt: str, history: list[dict] | None = None,
                  *, images: list[dict] | None = None, message_content: list[dict] | None = None) -> dict:
    if message_content is not None and images is not None:
        raise ValueError('Choose images or ordered message_content, not both')
    blocks = (validate_message_content(message_content) if message_content is not None
              else validate_images(images if images is not None else []))
    if (not text.strip() and not blocks) or not system_prompt.strip():
        raise FrontierError("消息（文字或图片）和系统提示词不能为空。")
    if not text.strip():
        text = IMAGE_ONLY_TEXT
    if message_content is not None:
        # Source material precedes the current request; interleaved image bindings survive.
        suffix = {"type": "text", "text": "\n【用户当前请求】\n" + text}
        content = validate_message_content([*blocks, suffix])
    else:
        content = [{"type": "text", "text": text}, *blocks] if blocks else text
    return {"systemPrompt": system_prompt,
            "messages": [*(history or []), {"role": "user", "content": content, "timestamp": int(datetime.now().timestamp() * 1000)}],
            "tools": tool_definitions()}


class Bridge:
    """Historical Python API name; now owns a Python SDK session, never a subprocess."""

    def __init__(self, home: Path | None = None, timeout: float = 300, cancel_event: Event | None = None,
                 *, api: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 model=None, loop=None, model_factory=None):
        self.home = (home or Path(os.environ.get("HYW_FRONTIER_HOME", "~/.hyw-frontier"))).expanduser().resolve()
        if timeout <= 0:
            raise FrontierError("timeout 必须大于零。")
        self.timeout = timeout
        self.cancel_event = cancel_event
        self._options = {"api": api, "base_url": base_url, "api_key": api_key}
        if model is not None and (loop is None or model_factory is not None):
            raise ValueError("A borrowed model requires its owner loop and cannot also use a factory")
        self.native_model = model
        self.model_loop = loop
        self.model_factory = model_factory
        self.uses_model_settings = model is not None or model_factory is not None
        self._task_provider = None
        self._session = None
        self.credentials = CredentialStore(self.home, timeout, cancel_event)

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise FrontierError("请求已取消。")

    def _locked(self):
        return self.credentials.locked()

    @contextmanager
    def task(self, provider: str):
        if self._task_provider is not None:
            raise FrontierError("同一个 Bridge 不能同时运行多个任务。")
        self._task_provider = provider
        try:
            yield self
        finally:
            session, self._session = self._session, None
            try:
                if session is not None:
                    session.close()
            finally:
                self._task_provider = None

    def _model_session(self, provider):
        if self._session is None:
            # Keep model SDKs off the offline-render/import path.
            from .model_backend import ModelSession
            connection = None if self.uses_model_settings else self.credentials.connection(provider, **self._options)
            self._session = ModelSession(connection, self.timeout, self.cancel_event,
                                         model=self.native_model, loop=self.model_loop, model_factory=self.model_factory)
        return self._session

    def call(self, request: dict, *, interactive: bool = False, on_event=None):
        self.check_cancelled()
        command = request.get("command")
        provider = request.get("provider")
        if interactive:
            self.credentials.login(provider, request.get("method", "api_key"))
            return None
        if command == "providers":
            return [{"id": name, "methods": ["api_key"]} for name in PROVIDERS]
        if command == "status":
            return self.credentials.status()
        if command == "logout":
            return self.credentials.logout(provider)
        if command not in ("complete", "stream", "models"):
            raise FrontierError("未知操作。")
        if self._task_provider is None:
            with self.task(provider):
                return self.call(request, on_event=on_event)
        if self._task_provider != provider:
            raise FrontierError("同一任务不能切换提供商。")
        session = self._model_session(provider)
        return session.list_models() if command == "models" else session.call(request, on_event)

    def ask(self, provider: str, model: str, text: str, prompt: Path = DEFAULT_PROMPT,
            *, history: list[dict] | None = None, max_rounds: int = 30, images: list[dict] | None = None,
            language: str = DEFAULT_LANGUAGE, max_tool_images: int = MAX_IMAGES,
            max_reader_images: int = MAX_READER_IMAGES, reader_engine: str = 'browser') -> str:
        from .agent import AgentLimitError, SearchAgent
        agent = SearchAgent(self, max_rounds=max_rounds, max_tool_images=max_tool_images, prefetch_icons=False,
                            max_reader_images=max_reader_images, reader_engine=reader_engine)
        try:
            result = agent.run(provider, model, build_context(
                text, load_prompt(prompt, language=language), history, images=images))
            return "".join(block["text"] for block in result["content"] if block["type"] == "text")
        except AgentLimitError as exc:
            raise FrontierError(str(exc)) from exc
        finally:
            agent.release()
