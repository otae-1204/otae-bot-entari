"""Prewarmed isolated Pillow worker and private, authenticated image artifacts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
from threading import Event, Lock, BoundedSemaphore
import time
import uuid

from .runtime import FrontierError
from .worker_pipe import JsonPipeReader, PipeReadError
from md2png.fonts import FontSet

RENDER_ENGINE = "Pillow/md2png-hyw"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CARD_HEIGHT = 16000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RenderError(FrontierError):
    def __init__(self, code: str):
        messages = {
            "browser_unavailable": "未找到可用的 Chrome/Chromium，请安装浏览器或设置 HYW_CHROME_PATH。",
            "render_timeout": "图片渲染超时。",
            "answer_too_large": "渲染数据超过安全上限。",
            "render_text_limit": "正文超过渲染文本上限。",
            "render_asset_limit": "最终引用的图片和图标数量超过渲染上限。",
            "render_asset_pixels": "最终配图的解码总像素超过渲染上限。",
            "render_asset_invalid": "最终配图或图标数据不符合渲染要求。",
            "render_canvas_limit": "最终图片的高度或总像素超过渲染上限。",
            "render_width_limit": "正文包含无法排入画布宽度的内容。",
            "render_structure_limit": "正文的节点数量或嵌套层级超过渲染上限。",
            "render_formula_limit": "公式复杂度超过渲染上限。",
            "render_file_limit": "生成的 PNG 超过12 MiB上限。",
            "render_protocol_limit": "渲染进程返回的数据超过传输上限。",
            "cancelled": "图片生成已取消。",
            "render_failed": "图片生成失败。",
        }
        self.code = code if code in messages else "render_failed"
        super().__init__(messages[self.code], diagnostics={"code": self.code})


RENDER_LIMIT_CODES = frozenset({
    'answer_too_large', 'render_text_limit', 'render_asset_limit', 'render_asset_pixels',
    'render_canvas_limit', 'render_width_limit', 'render_structure_limit',
    'render_formula_limit', 'render_file_limit', 'render_protocol_limit',
})


def engine_error_code(error: Exception) -> str:
    """Stable, non-sensitive reasons survive the isolated worker boundary."""
    message = str(error).lower()
    if 'deadline' in message or 'timeout' in message:
        return 'render_timeout'
    if 'combined decoded asset pixel' in message:
        return 'render_asset_pixels'
    if 'asset count' in message:
        return 'render_asset_limit'
    if 'canvas pixel/height' in message:
        return 'render_canvas_limit'
    if any(word in message for word in ('too narrow', 'wider than', 'canvas width', 'content width')):
        return 'render_width_limit'
    if 'formula' in message:
        return 'render_formula_limit'
    if 'markdown exceeds' in message or 'bounded string' in message:
        return 'render_text_limit'
    if any(word in message for word in ('node budget', 'inline budget', 'nesting budget', 'structural budget')):
        return 'render_structure_limit'
    if 'asset' in message or 'embedded image' in message:
        return 'render_asset_invalid'
    return 'render_failed'


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) > MAX_IMAGE_BYTES:
        raise RenderError("render_file_limit")
    if (not 24 <= len(data) or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR"):
        raise RenderError("render_failed")
    width, height = struct.unpack(">II", data[16:24])
    if not 1 <= width <= 2160 or not 1 <= height <= MAX_CARD_HEIGHT * 2:
        raise RenderError("render_canvas_limit")
    return width, height


@dataclass(frozen=True)
class RenderedCard:
    png: bytes
    recovered: bool = False
    mode: str = "text"
    diagnostics: tuple[dict, ...] = ()
    timings: dict[str, float] = field(default_factory=dict)
    links: tuple[str, ...] = ()


class ImageStore:
    def __init__(self, home: Path):
        self.directory = home / "images"

    def save(self, card: RenderedCard) -> dict:
        width, height = png_size(card.png)
        if self.directory.is_symlink():
            raise RenderError("render_failed")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        identifier = uuid.uuid4().hex
        path = self.directory / f"{identifier}.png"
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created = True
            with os.fdopen(fd, "wb") as output:
                output.write(card.png)
        except OSError:
            if created:
                path.unlink(missing_ok=True)
            raise RenderError("render_failed") from None
        return {"id": identifier, "url": f"/api/images/{identifier}", "mime_type": "image/png",
                "width": width, "height": height, "bytes": len(card.png)}

    def read(self, identifier: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier) or self.directory.is_symlink():
            raise FileNotFoundError()
        fd = os.open(self.directory / f"{identifier}.png", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
                raise FileNotFoundError()
            return source.read(MAX_IMAGE_BYTES + 1)


class CardRenderer:
    """One request at a time, reusing an offline process, never Web-process font state.

    Startup waits for font/parser prewarm. Runtime errors/cancellation discard the entire
    group; a subsequent request rebuilds it (without retrying the failed request).
    Fully unwound input-size rejections may reuse font-only state.
    """
    MAX_WORKER_REQUESTS = 100
    MAX_WORKER_RSS_MB = 512

    def __init__(self, timeout: float = 45):
        self.timeout = timeout
        self.slots = BoundedSemaphore(1)
        self.closed = Event()
        self.lock = Lock()
        self.workers: set[subprocess.Popen] = set()
        self._worker: subprocess.Popen | None = None
        self._pipe_reader: JsonPipeReader | None = None
        self._ready = False
        self._requests = 0
        self._prewarm_ms = 0.0

    def status(self) -> dict:
        with self.lock:
            ready = self._ready and self._worker is not None and self._worker.poll() is None
            return {'mode': 'prewarmed_process', 'pipe_transport': 'bounded_jsonl_thread',
                    'ready': ready, 'requests': self._requests,
                    'prewarm_ms': self._prewarm_ms, 'max_requests': self.MAX_WORKER_REQUESTS,
                    'recycle_rss_mb': self.MAX_WORKER_RSS_MB, 'png_compress_level': 3}

    def _check(self, cancel: Event, deadline: float):
        if cancel.is_set() or self.closed.is_set():
            raise RenderError('cancelled')
        if time.monotonic() >= deadline:
            raise RenderError('render_timeout')

    def _acquire(self, cancel: Event, deadline: float):
        while True:
            self._check(cancel, deadline)
            if self.slots.acquire(timeout=min(.02, max(0, deadline - time.monotonic()))):
                return

    def _receive(self, process: subprocess.Popen, cancel: Event, deadline: float) -> dict:
        # Readiness wakes immediately: no 50ms process-exit polling tail. Check cancellation
        # every <=20ms even if the worker is stuck in native code and emits no bytes.
        reader = self._pipe_reader
        if reader is None or process is not self._worker:
            raise RenderError('render_failed')
        try:
            return reader.receive(deadline, lambda: self._check(cancel, deadline))
        except PipeReadError as exc:
            raise RenderError(exc.code) from exc

    def _discard(self):
        with self.lock:
            process, self._worker = self._worker, None
            reader, self._pipe_reader = self._pipe_reader, None
            self._ready = False
            if process is not None:
                self._stop(process)
                if reader is not None:
                    reader.close()
                self.workers.discard(process)

    def _ensure_worker(self, cancel: Event, deadline: float, font_set: FontSet | None = None):
        self._check(cancel, deadline)
        if self._worker is not None and self._ready and self._worker.poll() is None:
            return self._worker
        self._discard()
        started = time.monotonic()
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'TEMP', 'TMP',
               'USERPROFILE', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
        if font_set is not None:
            # Only explicit font configuration, never question text or credentials.
            env['HYW_RENDER_FONTS'] = json.dumps(asdict(font_set))
        with self.lock:
            self._check(cancel, deadline)
            process = subprocess.Popen([sys.executable, '-m', 'hyw_frontier.render_worker', '--serve'],
                                       cwd=Path(__file__).resolve().parent.parent, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')
            self._worker = process
            self.workers.add(process)
            self._pipe_reader = JsonPipeReader(process.stdout)
        result = self._receive(process, cancel, deadline)
        if result.get('type') != 'ready' or not result.get('ok'):
            raise RenderError(result.get('code', 'render_failed'))
        with self.lock:
            self._check(cancel, deadline)
            self._ready = True
            self._requests = 0
            self._prewarm_ms = round((time.monotonic() - started) * 1000, 2)
        return process

    def start(self):
        """Explicit startup gate; serve() calls this before opening the HTTP listener."""
        cancel, deadline = Event(), time.monotonic() + self.timeout
        self._acquire(cancel, deadline)
        try:
            self._ensure_worker(cancel, deadline)
        except Exception:
            self._discard()
            if self.closed.is_set():
                raise RenderError('cancelled') from None
            raise
        finally:
            self.slots.release()

    @staticmethod
    def _stop(process):
        # This PGID was created by our Popen(start_new_session=True), never a user browser.
        if getattr(process, "_hyw_stopped", False):
            return
        process._hyw_stopped = True
        if os.name == "nt":
            if process.poll() is None:
                # Built into Windows; terminate only the worker tree we launched.
                killer = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/taskkill.exe"
                subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self):
        self.closed.set()
        self._discard()

    def render(self, answer: str, *, metadata: dict, cancel: Event,
               font_set: FontSet | None = None, image_assets: dict[str, bytes] | None = None,
               favicon_assets: dict[str, bytes] | None = None, max_tool_images: int = 600) -> RenderedCard:
        from .media import MAX_JPEG_BYTES
        from .favicon_assets import MAX_ICONS, MAX_ICON_BYTES
        from md2png.hyw.document import source_origin
        from md2png.model import Limits, RenderError as EngineError
        from .pillow_card import adapt_answer, parse_protocol, referenced_assets
        if len(answer.encode()) > 256 * 1024:
            raise RenderError('render_text_limit')
        if cancel.is_set() or self.closed.is_set():
            raise RenderError('cancelled')
        started = time.monotonic()
        available_images, available_icons = len(image_assets or {}), len(favicon_assets or {})
        try:
            document = adapt_answer(parse_protocol(answer), metadata,
                                    Limits(max_chars=256 * 1024), reading=True,
                                    image_urls=set(image_assets or {}))
        except EngineError as error:
            raise RenderError(engine_error_code(error)) from error
        image_assets, favicon_assets = referenced_assets(document, image_assets or {}, favicon_assets or {})
        selection = {'code': 'render_asset_selection', 'available_images': available_images,
                     'selected_images': len(image_assets), 'available_icons': available_icons,
                     'selected_icons': len(favicon_assets)}
        if (len(favicon_assets) > MAX_ICONS or any(not isinstance(origin, str)
                or not origin or source_origin(origin) != origin or not isinstance(raw, bytes)
                or len(raw) > MAX_ICON_BYTES for origin, raw in favicon_assets.items())):
            raise RenderError('render_asset_invalid')
        image_assets = image_assets or {}
        if (type(max_tool_images) is not int or max_tool_images < 0
                or len(image_assets) > max_tool_images or any(not isinstance(url, str) or not isinstance(raw, bytes)
                or len(raw) > MAX_JPEG_BYTES for url, raw in image_assets.items())):
            raise RenderError('render_asset_invalid')
        selection_ms = (time.monotonic() - started) * 1000
        deadline = started + self.timeout
        queued = time.monotonic()
        self._acquire(cancel, deadline)
        queue_ms = (time.monotonic() - queued) * 1000
        clean_rejection = False
        try:
            prepare_started = time.monotonic()
            previous_worker = self._worker
            process = self._ensure_worker(cancel, deadline, font_set)
            # A reused, ready worker performs no preparation. Poll/lookup overhead
            # belongs to total_ms, not a machine-speed-dependent phantom prewarm.
            prepare_ms = ((time.monotonic() - prepare_started) * 1000
                          if process is not previous_worker else 0)
            with tempfile.TemporaryDirectory(prefix='hyw-card-') as directory:
                work = Path(directory)
                payload = work / 'input.json'
                payload.write_text(json.dumps({'answer': answer, 'metadata': metadata,
                    'font_set': asdict(font_set) if font_set else None,
                    'max_tool_images': max_tool_images,
                    'image_assets': {url: base64.b64encode(raw).decode('ascii') for url, raw in image_assets.items()},
                    'favicon_assets': {origin: base64.b64encode(raw).decode('ascii')
                                       for origin, raw in favicon_assets.items()}},
                    ensure_ascii=False), encoding='utf-8')
                payload.chmod(0o600)
                self._check(cancel, deadline)
                try:
                    process.stdin.write((json.dumps({'work': str(work)}) + '\n').encode())
                    process.stdin.flush()
                    result = self._receive(process, cancel, deadline)
                except BaseException:
                    # Stop writers before TemporaryDirectory removes their files.
                    self._discard()
                    raise
                with self.lock:
                    self._requests += 1
                    recycle = self._requests >= self.MAX_WORKER_REQUESTS or result.get('rss_mb', 0) >= self.MAX_WORKER_RSS_MB
                if not result.get('ok'):
                    clean_rejection = (not recycle and result.get('code') in RENDER_LIMIT_CODES
                                       and result.get('request_complete') is True)
                    raise RenderError(result.get('code', 'render_failed'))
                path = work / 'card.png'
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    raise RenderError('render_file_limit')
                png = path.read_bytes()
                png_size(png)
                self._check(cancel, deadline)
                if recycle:
                    self._discard()
                timings = result.get('timings', {})
                timings.update(selection_ms=round(selection_ms, 2), queue_ms=round(queue_ms, 2), prepare_ms=round(prepare_ms, 2),
                               total_ms=round((time.monotonic() - started) * 1000, 2))
                return RenderedCard(png, bool(result.get('recovered')), result.get('mode', 'text'),
                                    (selection, *result.get('diagnostics', ())), timings,
                                    tuple(result.get('links', ())))
        except Exception as error:
            if not clean_rejection or cancel.is_set() or self.closed.is_set():
                self._discard()
            if cancel.is_set() or self.closed.is_set():
                raise RenderError('cancelled') from None
            if isinstance(error, RenderError):
                raise
            raise RenderError('render_failed') from None
        finally:
            self.slots.release()
