"""Private, per-request JSONL traces. No HTTP headers, auth stores or raw SDK errors."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
import time
import uuid
from .telemetry import redact

MAX_LOG_BYTES = 16 * 1024 * 1024
TERMINAL = {"done", "error", "cancelled", "client_disconnected"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RequestLog:
    def __init__(self, home: Path, metadata: dict, *, max_bytes: int = MAX_LOG_BYTES):
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:12]
        self.path = home / "logs" / f"{self.id}.jsonl"
        self.lock = Lock()
        self.file = None
        self.failed = False
        self.truncated = False
        self.size = 0
        self.max_bytes = max_bytes
        self.status = "interrupted"
        self.started = time.monotonic()
        try:
            if self.path.parent.is_symlink():
                raise OSError("Log directory must not be a symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.file = os.fdopen(fd, "wb")
        except OSError:
            self.failed = True
        self.write({"type": "request", "log_schema_version": 2, **metadata})

    def _write(self, record: dict):
        raw = (json.dumps({"timestamp": utc_now(), "request_id": self.id, **record}, ensure_ascii=False) + "\n").encode()
        self.file.write(raw)
        self.file.flush()  # Visible immediately while streaming, without per-token fsync overhead.
        self.size += len(raw)

    def write(self, record: dict):
        # Keep the final response and timings, not repeated token streams or images.
        if record.get('type', '').endswith('_delta'):
            return
        record = redact(record)
        with self.lock:
            if record.get("type") in TERMINAL:
                self.status = record["type"]
            if self.failed or self.file is None:
                return
            try:
                if self.size + len(json.dumps(record, ensure_ascii=False).encode()) + 200 > self.max_bytes:
                    if not self.truncated:
                        self.truncated = True
                        self._write({"type": "log_truncated", "limit_bytes": self.max_bytes,
                                     "message": "日志达到大小上限，后续仅保留终态；页面流不受影响。"})
                    if record.get("type") in TERMINAL or record.get("type") == "request_closed":
                        self._write({key: record[key] for key in ("type", "seq", "elapsed_ms", "status") if key in record})
                    return
                if not self.truncated:
                    self._write(record)
                elif record.get("type") in TERMINAL or record.get("type") == "request_closed":
                    self._write({key: record[key] for key in ("type", "seq", "elapsed_ms", "status") if key in record})
            except (OSError, ValueError):
                self.failed = True  # Disk errors must not fail a model request or expose raw errors.

    def close(self):
        self.write({"type": "request_closed", "status": self.status,
                    "elapsed_ms": round((time.monotonic() - self.started) * 1000)})
        with self.lock:
            if self.file is not None:
                try:
                    self.file.close()
                except OSError:
                    self.failed = True
                self.file = None


def log_summaries(home: Path, *, limit: int = 5, query: str = "") -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("日志条数必须在1–100之间")
    summaries = []
    for path in sorted((home / "logs").glob("*.jsonl"), reverse=True):
        if path.is_symlink():
            continue
        summary = None
        rounds, searches, pages, tools, stages = {}, {}, {}, {}, {}
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue  # A process may have been interrupted mid-record.
                if not isinstance(event, dict):
                    continue
                kind, ms = event.get("type"), event.get("elapsed_ms")
                if kind == "request":
                    if query.casefold() not in event.get("message", "").casefold():
                        break
                    summary = {key: event.get(key) for key in ("request_id", "timestamp", "message", "provider", "model", "reasoning", "search_provider", "search_mode", 'max_reader_images', 'reader_engine')}
                    summary.update(file=str(path), status="incomplete", log_truncated=False)
                    summary.update(entry=event.get('entry', 'local_web'), log_schema_version=event.get('log_schema_version', 1))
                if summary is None:
                    continue
                if kind in TERMINAL:
                    summary.update(status=kind, elapsed_ms=ms)
                    if kind == "error":
                        summary["error"] = {key: event[key] for key in ("code", "http_status", "retryable", "message", "error_type") if key in event}
                elif kind == "request_closed":
                    summary["status"] = event["status"]
                    summary['elapsed_ms'] = ms
                elif kind == "bot_summary":
                    summary["bot"] = {key: value for key, value in event.items()
                                      if key not in ("type", "timestamp", "request_id", "elapsed_ms", "thread")}
                elif kind == "outgoing_image":
                    summary["outgoing_image"] = {key: event.get(key) for key in
                                                 ("mime_type", "png_bytes", "bytes", "quality", "width", "height")}
                elif kind == "details_truncated":
                    summary["details_truncated"] = True
                elif kind == "log_truncated":
                    summary["log_truncated"] = True
                elif kind == "answer_ready":
                    summary["answer_ready_ms"] = ms
                elif kind == "render_start":
                    summary["rendering"] = {"engine": event.get("engine"), "start_ms": ms}
                elif kind in ("render_end", "render_error"):
                    summary.setdefault("rendering", {}).update(end_ms=ms, duration_ms=event.get("duration_ms"),
                                                              status=kind, code=event.get("code"))
                    if event.get("image"):
                        summary["image"] = event["image"]
                    if event.get("stages_ms"):
                        summary["rendering"]["stages_ms"] = event["stages_ms"]
                elif kind == "model_start":
                    rounds[event["round"]] = {"round": event["round"], "start_ms": ms, "reasoning": event.get("reasoning")}
                elif kind == 'model_first_output' and event['round'] in rounds:
                    rounds[event['round']]['first_output_ms'] = event.get('duration_ms')
                elif kind == "round_timing" and event["round"] in rounds:
                    rounds[event["round"]].update({key: event[key] for key in
                        ("total_ms", "model_ms", "tool_ms", "media_download_ms", "image_processing_ms", "images_sent", "complete", "timing_version")
                        if key in event})
                elif kind == "model_response" and event["round"] in rounds:
                    response = event.get("response", {})
                    if response.get('usage'):
                        rounds[event['round']]['usage'] = response['usage']
                    rounds[event["round"]]["tool_calls"] = [block.get("name") for block in response.get("content", [])
                                                            if block.get("type") == "toolCall"]
                elif kind == "model_stream_end" and event["round"] in rounds:
                    row = rounds[event["round"]]
                    row.update(end_ms=ms, duration_ms=ms - row["start_ms"], usage=event.get("usage"))
                elif kind in ("text_delta", "thinking_delta") and event["round"] in rounds:
                    rounds[event["round"]].setdefault("first_" + kind + "_ms", ms)
                elif kind == 'page_timing':
                    pages.setdefault(event['id'], {}).update({key: event.get(key) for key in
                        ('id', 'url', 'reader_ms', 'download_ms', 'processing_ms', 'images', 'max_reader_images', 'status')})
                elif kind == "query_start":
                    searches[(event.get("id", ""), event["query_index"])] = {
                        "round": event["round"], "query": event["query"], "start_ms": ms,
                        "provider": event.get("provider"), "search_mode": event.get("search_mode")}
                elif kind == "query_end":
                    row = searches.get((event.get("id", ""), event["query_index"]))
                    if row is not None:
                        row.update(end_ms=ms, duration_ms=event["duration_ms"], ok=event["ok"], cached=event.get("cached"))
                elif kind in ('tool_start', 'tool_end'):
                    row = tools.setdefault((event.get('round'), event.get('id')), {
                        'round': event.get('round'), 'id': event.get('id'), 'name': event.get('name')})
                    row['start_ms' if kind == 'tool_start' else 'end_ms'] = ms
                    if kind == 'tool_end':
                        row.update(duration_ms=event.get('duration_ms'), ok=event.get('ok'), code=event.get('code'))
                elif kind in ('stage_start', 'stage_end'):
                    row = stages.setdefault(event['stage_id'], {})
                    row.update({k: v for k, v in event.items() if k not in
                                ('type', 'timestamp', 'request_id', 'seq', 'thread', 'elapsed_ms')})
                    row['start_ms' if kind == 'stage_start' else 'end_ms'] = ms
        if summary is not None:
            summary.update(model_rounds=list(rounds.values()), searches=list(searches.values()), pages=list(pages.values()),
                           tool_calls=list(tools.values()), stages=list(stages.values()))
            summaries.append(summary)
        if len(summaries) == limit:
            break
    return summaries
