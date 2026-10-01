"""Task-owned speculative favicon prefetch. Rendering never waits for this network work."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Lock
import time
from urllib.parse import urlsplit

from md2png.hyw.document import source_origin

from .favicon_assets import ICON_EDGE, MAX_ICONS, MAX_ICON_BYTES, validate_icon
from .jina import JinaError, public_url

DOWNLOAD_TIMEOUT = 10
CONCURRENCY = 4
FAVICON_CONFIG = {'enabled': True, 'discovery': 'tool_sources_and_streamed_origins',
                  'cache_scope': 'task', 'max_icons': MAX_ICONS, 'concurrency': CONCURRENCY,
                  'download_timeout_seconds': DOWNLOAD_TIMEOUT, 'format': 'image/png',
                  'max_edge': ICON_EDGE, 'max_icon_bytes': MAX_ICON_BYTES,
                  'render_wait': False, 'render_cutoff': 'final_answer_ready',
                  'cleanup': 'background_stop_then_release_join',
                  'fallback': 'offline_page_icon', 'position': 'source_title', 'size': 21}
_URL = re.compile(r'''https?://[^\s<>"'`\[\]，。；！？、（）【】]+''')


class FaviconPipeline:
    def __init__(self, cancel: Event | None = None):
        self.cancel = cancel or Event()
        self.closed = Event()
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix='hyw-favicon')
        self.seen: set[str] = set()
        self.assets: dict[str, bytes] = {}
        self.processes: set[subprocess.Popen] = set()
        self.tails: dict[int, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def submit(self, url: str):
        if not isinstance(url, str) or len(url) > 8192:
            return
        try:
            origin = source_origin(public_url(url))
        except (JinaError, ValueError):
            return
        if not origin or len(origin) > 512:
            return
        with self.lock:
            if self.closed.is_set() or self.cancel.is_set() or origin in self.seen or len(self.seen) >= MAX_ICONS:
                return
            self.seen.add(origin)
            self.pool.submit(self._download, origin)

    def feed(self, delta: str, index: int = 0, *, final=False):
        """Wait for a complete hostname, not the entire path or closing Markdown bracket."""
        if not isinstance(delta, str) or self.closed.is_set() or self.cancel.is_set():
            return
        # A stream can split scheme/hostname anywhere. Retain only a bounded trailing window.
        text = self.tails.get(index, '') + delta
        for match in _URL.finditer(text):
            url = match[0].rstrip('.,;:!?)')
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            if final or match.end() < len(text) or parts.path or '?' in url or '#' in url:
                self.submit(url)
        if len(self.tails) < 16 or index in self.tails:
            self.tails[index] = '' if final else text[-2048:]

    def reset_stream(self):
        self.tails.clear()

    def snapshot(self) -> dict[str, bytes]:
        with self.lock:
            return dict(self.assets)  # Never wait for pending downloads or return mutable shared state.

    def freeze(self) -> dict[str, bytes]:
        """Cut off prefetch immediately. Child reaping/joining must not precede rendering."""
        with self.lock:
            self.closed.set()
            ready = dict(self.assets)
            self.assets.clear()
        self.tails.clear()
        # Running workers observe closed and kill/reap their own subprocess in the background.
        self.pool.shutdown(wait=False, cancel_futures=True)
        return ready

    def _download(self, origin):
        process = None
        try:
            with self.lock:
                if self.closed.is_set() or self.cancel.is_set():
                    return
                process = subprocess.Popen(
                    [sys.executable, '-I', str(Path(__file__).with_name('favicon_fetch.py'))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env={k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR') if k in os.environ},
                )
                self.processes.add(process)
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT
            payload = origin.encode('utf-8')
            while not self.closed.is_set() and not self.cancel.is_set():
                if time.monotonic() >= deadline:
                    return
                try:
                    raw, _ = process.communicate(payload, timeout=.05)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            else:
                return
            if process.returncode != 0:
                return
            validate_icon(raw)
            with self.lock:
                if not self.closed.is_set() and not self.cancel.is_set():
                    self.assets[origin] = raw
        except Exception:
            # A favicon is optional. Do not retain exception objects/traceback frames in futures.
            pass
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                process.communicate()
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
                with self.lock:
                    self.processes.discard(process)

    def close(self):
        self.closed.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.assets.clear()
            self.seen.clear()
            self.processes.clear()
        self.tails.clear()
