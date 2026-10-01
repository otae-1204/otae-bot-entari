"""Portable bounded JSONL reader; Windows selectors cannot monitor anonymous pipes.

One reader thread owns the pipe; the process owner kills/waits for its child before
closing this reader. No threads, IO or global state are created at import time.
"""
from __future__ import annotations

import json
from queue import Empty, Full, Queue
from threading import Event, Thread
import time


class PipeReadError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class JsonPipeReader:
    LIMIT = 2 * 1024 * 1024

    def __init__(self, stream):
        self.stream = stream
        self.stopped = Event()
        self.records = Queue(maxsize=1)
        self.thread = Thread(target=self._read, name="hyw-json-pipe", daemon=True)
        self.thread.start()

    def _put(self, value):
        while not self.stopped.is_set():
            try:
                self.records.put(value, timeout=.02)
                return
            except Full:
                continue

    def _read(self):
        output = bytearray()
        try:
            while not self.stopped.is_set():
                chunk = self.stream.read1(65536)
                if not chunk:
                    raise PipeReadError("render_failed")
                output.extend(chunk)
                if len(output) > self.LIMIT:
                    raise PipeReadError("render_protocol_limit")
                if b"\n" in output:
                    if not output.endswith(b"\n") or output.count(b"\n") != 1:
                        raise PipeReadError("render_failed")
                    record = json.loads(output)
                    if not isinstance(record, dict):
                        raise PipeReadError("render_failed")
                    self._put(record)
                    output.clear()
        except PipeReadError as exc:
            self._put(exc)
        except (OSError, ValueError, UnicodeError):
            self._put(PipeReadError("render_failed"))

    def receive(self, deadline: float, check=lambda: None) -> dict:
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PipeReadError("render_timeout")
            if self.stopped.is_set():
                raise PipeReadError("render_failed")
            try:
                result = self.records.get(timeout=min(.02, remaining))
            except Empty:
                continue
            check()
            if isinstance(result, PipeReadError):
                raise result
            return result

    def close(self):
        # Child must already be stopped so a blocking read can reach EOF.
        self.stopped.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("JSON pipe reader did not stop after child shutdown")
