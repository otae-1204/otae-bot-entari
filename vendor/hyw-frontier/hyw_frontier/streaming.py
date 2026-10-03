"""Nonblocking, LF-only JSONL transport for the Node model stream."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

MAX_RECORD_BYTES = 8 * 1024 * 1024


class StreamProtocolError(RuntimeError):
    pass


def consume_stream(process, request: dict, timeout: float, check_cancelled, on_event, *, keep_open: bool = False) -> dict:
    pending = memoryview((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    buffer = bytearray()
    terminal = None
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for pipe, mode, name in ((process.stdin, selectors.EVENT_WRITE, "stdin"),
                                 (process.stdout, selectors.EVENT_READ, "stdout"),
                                 (process.stderr, selectors.EVENT_READ, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, mode, name)
        while selector.get_map() or process.poll() is None:
            check_cancelled()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for selected, _ in selector.select(timeout=0.1):
                pipe, name = selected.fileobj, selected.data
                if name == "stdin":
                    count = os.write(pipe.fileno(), pending[:65536])
                    pending = pending[count:]
                    if not pending:
                        selector.unregister(pipe)
                        if not keep_open:
                            pipe.close()
                            process.stdin = None  # Allows safe communicate() during cancellation cleanup.
                    continue
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    selector.unregister(pipe)
                    continue
                if name == "stderr":
                    continue  # Drain without retaining secret-bearing SDK diagnostics.
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(line) > MAX_RECORD_BYTES:
                        raise StreamProtocolError("流式记录超过8MB限制，已明确停止，未静默截断。")
                    if not line.strip():
                        continue
                    if terminal is not None:
                        raise StreamProtocolError("终结记录之后收到额外输出。")
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        raise StreamProtocolError("桥接流中包含无效 JSON/UTF-8。") from exc
                    if not isinstance(record, dict):
                        raise StreamProtocolError("桥接流记录必须为 JSON 对象。")
                    if "event" in record and isinstance(record["event"], dict):
                        on_event(record["event"])
                    elif isinstance(record.get("ok"), bool):
                        terminal = record
                    else:
                        raise StreamProtocolError("未知的桥接流记录。")
                if len(buffer) > MAX_RECORD_BYTES:
                    raise StreamProtocolError("流式记录超过8MB限制，已停止。")
                if keep_open and terminal is not None:
                    if pending or buffer:
                        raise StreamProtocolError("常驻桥接终结记录与请求边界不匹配。")
                    return terminal
    process.wait(timeout=max(0.01, deadline - time.monotonic()))
    if buffer.strip() or terminal is None:
        raise StreamProtocolError("模型流提前结束，未收到完整终结记录；保留已显示内容。")
    return terminal
