"""MCSM plugin dark dashboard image renderer."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from textwrap import wrap
from datetime import datetime
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

from configs.path_config import FONT_PATH

CANVAS_W = 960
CARD_X = 32
CARD_W = CANVAS_W - CARD_X * 2
PADDING = 26
MAX_HEIGHT = 2400
CONSOLE_SOURCE_TAIL_LINES = 96
CONSOLE_DISPLAY_LINES = 78
CONSOLE_LOG_ENTRIES = 10
CONSOLE_LINE_H = 21
ROOT_DIR = Path(__file__).resolve().parents[2]
METRIC_ICON_DIR = ROOT_DIR / "assets" / "image" / "mcsm" / "icons"
METRIC_ICON_FILES = {
    "cpu": "cpu.png",
    "memory": "memory.png",
    "disk": "disk.png",
}
_METRIC_ICON_CACHE: dict[str, Image.Image | None] = {}

BG = (14, 18, 28, 255)
SURFACE = (25, 31, 44, 255)
SURFACE_2 = (31, 39, 55, 255)
BORDER = (53, 65, 84, 255)
ACCENT = (64, 145, 255, 255)
ACCENT_DIM = (32, 78, 145, 255)
TEXT = (229, 236, 246, 255)
TEXT_2 = (168, 180, 196, 255)
MUTED = (111, 124, 145, 255)
TERMINAL = (8, 11, 18, 255)
SUCCESS = (55, 211, 126, 255)
WARNING = (245, 178, 66, 255)
DANGER = (255, 103, 116, 255)
INFO = (93, 173, 255, 255)

_LOG_LEVEL_RE = re.compile(r"\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\b", re.IGNORECASE)
_LOG_ENTRY_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?(?:\s+[^\]]+)?\]")
_LOG_LEVEL_ENTRY_RE = re.compile(r"^\s*\[[^\]]*(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)[^\]]*\]", re.IGNORECASE)
_URL_RE = re.compile(r"^(?:https?://|wss?://|ftp://)", re.IGNORECASE)
_STACK_CONTINUATION_RE = re.compile(r"^(?:at\s+|Caused by:|Suppressed:|\.\.\.\s+\d+\s+more)", re.IGNORECASE)
_COMMAND_LIKE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./:-]*(?:\s+[-+@#./:=,;A-Za-z0-9_\u4e00-\u9fff]+)*$")

STATUS_MAP = {-1: "BUSY", 0: "STOPPED", 1: "STOPPING", 2: "STARTING", 3: "RUNNING"}
STATUS_COLOR = {-1: WARNING, 0: MUTED, 1: WARNING, 2: INFO, 3: SUCCESS}
STATUS_CN = {-1: "繁忙", 0: "已停止", 1: "停止中", 2: "启动中", 3: "运行中"}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = Path(FONT_PATH) / "steamInfo" / name
    return ImageFont.truetype(str(path), size)


FONT_TITLE = _font("MiSans-Bold.ttf", 30)
FONT_H2 = _font("MiSans-Bold.ttf", 22)
FONT_BODY = _font("MiSans-Regular.ttf", 20)
FONT_BODY_BOLD = _font("MiSans-Bold.ttf", 20)
FONT_SMALL = _font("MiSans-Regular.ttf", 17)
FONT_SMALL_BOLD = _font("MiSans-Bold.ttf", 17)
FONT_MONO = _font("MiSans-Regular.ttf", 18)

TYPE_MAP = {
    "universal": "通用",
    "java": "Java",
    "bedrock": "基岩版",
    "minecraft/java": "MC Java 版服务端",
    "minecraft/bedrock": "MC 基岩版服务端",
}


def _text_h(font: ImageFont.FreeTypeFont) -> int:
    bbox = font.getbbox("Hg")
    return bbox[3] - bbox[1]


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _fit(text: object, max_chars: int) -> str:
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)] + "…"


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_percent(value: object) -> str:
    number = _to_float(value)
    if number is None:
        return "N/A"
    if number <= 1 and number != 0:
        number *= 100
    return f"{number:.1f}".rstrip("0").rstrip(".") + "%"


def _clamp_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def _percent_ratio(value: object) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    if number <= 1 and number != 0:
        return _clamp_ratio(number)
    return _clamp_ratio(number / 100)


def _usage_ratio(used: object, limit: object) -> float | None:
    used_number = _to_float(used)
    limit_number = _to_float(limit)
    if used_number is None or limit_number is None or limit_number <= 0:
        return None
    return _clamp_ratio(used_number / limit_number)


def _first_number(*values: object, allow_zero: bool = True) -> float | None:
    for value in values:
        number = _to_float(value)
        if number is None or number < 0:
            continue
        if number == 0 and not allow_zero:
            continue
        return number
    return None


def _first_sourced_number(
    *items: tuple[object, str], allow_zero: bool = True
) -> tuple[float, str] | tuple[None, None]:
    for value, source in items:
        number = _to_float(value)
        if number is None or number < 0:
            continue
        if number == 0 and not allow_zero:
            continue
        return number, source
    return None, None


def _nested_number(mapping: object, *path: str, allow_zero: bool = True) -> float | None:
    value = mapping
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _first_number(value, allow_zero=allow_zero)


def _nested_value(mapping: object, *path: str) -> object:
    value = mapping
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _has_number(value: object, *, allow_zero: bool = True) -> bool:
    return _first_number(value, allow_zero=allow_zero) is not None


def _normalize_size_to_mib(value: object, source: str) -> float | None:
    number = _to_float(value)
    if number is None or number < 0:
        return None
    source_lower = source.lower()
    byte_hint = any(token in source_lower for token in ("usage", ".used", ".total"))
    if byte_hint and number >= 1024 * 1024:
        return number / 1024 / 1024
    if number >= 1024 * 1024 * 1024:
        return number / 1024 / 1024
    return number


def _normalize_disk_to_mib(value: object, source: str) -> float | None:
    number = _to_float(value)
    if number is None or number < 0:
        return None
    source_lower = source.lower()
    byte_hint = any(token in source_lower for token in ("usage", ".used", ".total", "bytes"))
    if byte_hint and number >= 1024 * 1024:
        return number / 1024 / 1024
    return number


def _format_size(value: object) -> str:
    number = _to_float(value)
    if number is None:
        return "N/A"
    if number < 0:
        return "N/A"
    # MCSManager uses MiB for memory and disk values in InstanceDetail.
    if number >= 1024:
        return f"{number / 1024:.2f} GiB"
    return f"{number:.0f} MiB"


def _format_size_pair(used: object, limit: object = None) -> str:
    used_text = _format_size(used)
    limit_text = _format_size(limit)
    if used_text == "N/A":
        return "N/A"
    if limit_text != "N/A" and _to_float(limit) not in (None, 0):
        return f"{used_text} / {limit_text}"
    return used_text


def _format_time(value: object, *, unlimited_zero: bool = False) -> str:
    if value in (None, ""):
        return "N/A"
    number = _to_float(value)
    if number is not None:
        if number <= 0:
            return "无限制" if unlimited_zero else "N/A"
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return str(value)
    return str(value)


def _format_bool(value: object) -> str:
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if isinstance(value, (int, float)):
        return "开启" if value else "关闭"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "enable", "enabled"}:
            return "开启"
        if lowered in {"false", "0", "no", "off", "disable", "disabled"}:
            return "关闭"
    return "N/A"


def _format_players(info: dict) -> str:
    current = info.get("currentPlayers")
    maximum = info.get("maxPlayers")
    if _to_float(current) in (None, -1) or _to_float(maximum) in (None, -1):
        return "未知"
    return f"{int(float(current))} / {int(float(maximum))}"


def _format_ports(cfg: dict) -> str:
    docker = cfg.get("docker", {}) or {}
    ports = docker.get("ports") or []
    rendered: list[str] = []
    if isinstance(ports, dict):
        ports = list(ports.values())
    for item in ports:
        if isinstance(item, dict):
            protocol = str(item.get("protocol") or item.get("type") or "TCP").upper()
            host = item.get("hostPort") or item.get("publicPort") or item.get("port")
            container = item.get("containerPort") or item.get("targetPort")
            if host and container and str(host) != str(container):
                rendered.append(f"{protocol} 主机:{host} 容器:{container}")
            elif host:
                rendered.append(f"{protocol} {host}")
        elif item not in (None, ""):
            rendered.append(str(item))
    if rendered:
        return "；".join(rendered[:3])
    ping_config = cfg.get("pingConfig", {}) or {}
    port = ping_config.get("port")
    if port not in (None, "", -1):
        return f"TCP {port}"
    return "N/A"


def status_summary(detail: dict, bind_info: dict) -> dict[str, object]:
    cfg = detail.get("config", {}) or {}
    info = detail.get("info", {}) or {}
    process_info = detail.get("processInfo", {}) or {}
    resource = detail.get("resource", {}) or {}
    resources = detail.get("resources", {}) or {}
    docker = cfg.get("docker", {}) or {}
    detail_docker = detail.get("docker", {}) or {}
    event_task = cfg.get("eventTask", {}) or {}
    inst_type = str(cfg.get("type") or cfg.get("processType") or "")
    admins = bind_info.get("admins", []) or []
    cpu_used = _first_number(
        process_info.get("cpu"),
        process_info.get("cpuUsage"),
        info.get("cpu"),
        info.get("cpuUsage"),
        resource.get("cpu"),
        resource.get("cpuUsage"),
        resources.get("cpu"),
        resources.get("cpuUsage"),
        _nested_number(resources, "cpu", "usage"),
    )
    memory_used_raw, memory_used_source = _first_sourced_number(
        (process_info.get("memory"), "processInfo.memory"),
        (process_info.get("memoryUsage"), "processInfo.memoryUsage"),
        (info.get("memory"), "info.memory"),
        (info.get("memoryUsage"), "info.memoryUsage"),
        (resource.get("memory"), "resource.memory"),
        (resource.get("memoryUsage"), "resource.memoryUsage"),
        (resources.get("memory"), "resources.memory"),
        (resources.get("memoryUsage"), "resources.memoryUsage"),
        (_nested_value(resources, "memory", "used"), "resources.memory.used"),
    )
    memory_limit_raw, memory_limit_source = _first_sourced_number(
        (docker.get("memory"), "config.docker.memory"),
        (detail_docker.get("memory"), "docker.memory"),
        (cfg.get("memory"), "config.memory"),
        (process_info.get("totalMemory"), "processInfo.totalMemory"),
        (info.get("totalMemory"), "info.totalMemory"),
        (resource.get("totalMemory"), "resource.totalMemory"),
        (resources.get("totalMemory"), "resources.totalMemory"),
        (_nested_value(resources, "memory", "total"), "resources.memory.total"),
    )
    memory_used = _normalize_size_to_mib(memory_used_raw, memory_used_source or "")
    memory_limit = _normalize_size_to_mib(memory_limit_raw, memory_limit_source or "")
    disk_limit_raw, disk_limit_source = _first_sourced_number(
        (docker.get("maxSpace"), "config.docker.maxSpace"),
        (detail_docker.get("maxSpace"), "docker.maxSpace"),
        (cfg.get("maxSpace"), "config.maxSpace"),
        (resources.get("maxSpace"), "resources.maxSpace"),
        (resource.get("maxSpace"), "resource.maxSpace"),
        (_nested_value(resources, "disk", "total"), "resources.disk.total"),
        (_nested_value(resources, "space", "total"), "resources.space.total"),
        (_nested_value(resources, "storage", "total"), "resources.storage.total"),
        (_nested_value(resource, "disk", "total"), "resource.disk.total"),
        (_nested_value(resource, "space", "total"), "resource.space.total"),
        (_nested_value(resource, "storage", "total"), "resource.storage.total"),
        (_nested_value(detail, "disk", "total"), "disk.total"),
        (_nested_value(detail, "space", "total"), "space.total"),
        (_nested_value(detail, "storage", "total"), "storage.total"),
    )
    disk_used_raw, disk_used_source = _first_sourced_number(
        (detail.get("space"), "space"),
        (detail.get("spaceUsage"), "spaceUsage"),
        (detail.get("diskUsage"), "diskUsage"),
        (detail.get("storageUsage"), "storageUsage"),
        (info.get("space"), "info.space"),
        (info.get("spaceUsage"), "info.spaceUsage"),
        (info.get("diskUsage"), "info.diskUsage"),
        (info.get("storageUsage"), "info.storageUsage"),
        (resource.get("space"), "resource.space"),
        (resource.get("spaceUsage"), "resource.spaceUsage"),
        (resource.get("diskUsage"), "resource.diskUsage"),
        (resource.get("storageUsage"), "resource.storageUsage"),
        (resources.get("space"), "resources.space"),
        (resources.get("spaceUsage"), "resources.spaceUsage"),
        (resources.get("diskUsage"), "resources.diskUsage"),
        (resources.get("storageUsage"), "resources.storageUsage"),
        (_nested_value(resources, "disk", "used"), "resources.disk.used"),
        (_nested_value(resources, "disk", "usage"), "resources.disk.usage"),
        (_nested_value(resources, "space", "used"), "resources.space.used"),
        (_nested_value(resources, "space", "usage"), "resources.space.usage"),
        (_nested_value(resources, "storage", "used"), "resources.storage.used"),
        (_nested_value(resources, "storage", "usage"), "resources.storage.usage"),
        (_nested_value(resource, "disk", "used"), "resource.disk.used"),
        (_nested_value(resource, "disk", "usage"), "resource.disk.usage"),
        (_nested_value(resource, "space", "used"), "resource.space.used"),
        (_nested_value(resource, "space", "usage"), "resource.space.usage"),
        (_nested_value(resource, "storage", "used"), "resource.storage.used"),
        (_nested_value(resource, "storage", "usage"), "resource.storage.usage"),
        (_nested_value(detail, "disk", "used"), "disk.used"),
        (_nested_value(detail, "disk", "usage"), "disk.usage"),
        (_nested_value(detail, "space", "used"), "space.used"),
        (_nested_value(detail, "space", "usage"), "space.usage"),
        (_nested_value(detail, "storage", "used"), "storage.used"),
        (_nested_value(detail, "storage", "usage"), "storage.usage"),
        (detail.get("disk"), "disk"),
        (detail.get("storage"), "storage"),
        allow_zero=False,
    )
    disk_limit = _normalize_disk_to_mib(disk_limit_raw, disk_limit_source or "")
    disk_used = _normalize_disk_to_mib(disk_used_raw, disk_used_source or "")
    if disk_used is None and disk_limit:
        disk_used_raw, disk_used_source = _first_sourced_number(
            (detail.get("space"), "space"),
            (detail.get("spaceUsage"), "spaceUsage"),
            (detail.get("diskUsage"), "diskUsage"),
            (_nested_value(detail, "disk", "used"), "disk.used"),
            (_nested_value(resources, "disk", "used"), "resources.disk.used"),
            allow_zero=True,
        )
        disk_used = _normalize_disk_to_mib(disk_used_raw, disk_used_source or "")

    return {
        "display_name": cfg.get("nickname", "") or detail.get("instanceName", "") or detail.get("name", ""),
        "type": TYPE_MAP.get(inst_type, inst_type),
        "start_command": str(cfg.get("startCommand", "") or ""),
        "admins": ", ".join(map(str, admins)) if admins else "(未设置)",
        "cpu": _format_percent(cpu_used),
        "cpu_ratio": _percent_ratio(cpu_used),
        "memory": _format_size_pair(memory_used, memory_limit),
        "memory_ratio": _usage_ratio(memory_used, memory_limit),
        "disk": _format_size_pair(disk_used, disk_limit),
        "disk_ratio": _usage_ratio(disk_used, disk_limit),
        "started": str(detail.get("started")) if detail.get("started") not in (None, "") else "N/A",
        "auto_restart": _format_bool(event_task.get("autoRestart")),
        "players": _format_players(info),
        "version": str(info.get("version") or "N/A"),
        "end_time": _format_time(cfg.get("endTime"), unlimited_zero=True),
        "last_datetime": _format_time(cfg.get("lastDatetime")),
        "ports": _format_ports(cfg),
    }


def merge_status_detail(detail: dict, snapshot: dict | None) -> dict:
    if not snapshot:
        return detail

    merged = dict(detail)
    detail_cfg = dict(detail.get("config", {}) or {})
    snapshot_cfg = snapshot.get("config", {}) or {}
    detail_docker = dict(detail_cfg.get("docker", {}) or {})
    snapshot_docker = snapshot_cfg.get("docker", {}) or {}

    snapshot_process = snapshot.get("processInfo", {}) or {}
    if (
        _has_number(snapshot_process.get("cpu"))
        or _has_number(snapshot_process.get("cpuUsage"))
        or _has_number(snapshot_process.get("memory"))
        or _has_number(snapshot_process.get("memoryUsage"))
    ):
        merged["processInfo"] = snapshot_process

    top_level_docker = snapshot.get("docker", {}) or {}
    if isinstance(top_level_docker, dict) and top_level_docker:
        snapshot_docker = {**snapshot_docker, **top_level_docker}

    has_disk_limit = bool(
        detail_docker.get("maxSpace")
        or snapshot_docker.get("maxSpace")
        or (isinstance(detail.get("docker"), dict) and detail["docker"].get("maxSpace"))
        or _nested_value(snapshot, "disk", "total")
        or _nested_value(snapshot, "storage", "total")
    )
    for key in ("space", "spaceUsage", "disk", "diskUsage", "storage", "storageUsage"):
        value = snapshot.get(key)
        if isinstance(value, dict):
            if any(_has_number(value.get(k), allow_zero=has_disk_limit) for k in ("used", "usage", "total")):
                merged[key] = value
        elif _has_number(value, allow_zero=has_disk_limit):
            merged[key] = value

    for key in ("resource", "resources"):
        if isinstance(snapshot.get(key), dict):
            merged[key] = snapshot[key]

    if snapshot.get("status") is not None:
        merged["status"] = snapshot["status"]

    if snapshot_docker:
        detail_docker.update({k: v for k, v in snapshot_docker.items() if v not in (None, "")})
        detail_cfg["docker"] = detail_docker
        merged["config"] = detail_cfg

    return merged


def _wrap_text(text: object, width: int = 48) -> list[str]:
    value = str(text or "")
    if not value:
        return [""]
    lines: list[str] = []
    for raw_line in value.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        lines.extend(wrap(raw_line, width=width, replace_whitespace=False) or [raw_line])
    return lines


def _height_for_lines(lines: Sequence[str], line_h: int = 28) -> int:
    return max(1, len(lines)) * line_h


def _base(height: int, title: str, subtitle: str = "") -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    height = max(260, min(height, MAX_HEIGHT))
    img = Image.new("RGBA", (CANVAS_W, height), BG)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((24, 22, CANVAS_W - 24, 92), radius=20, fill=SURFACE, outline=BORDER, width=1)
    draw.rounded_rectangle((42, 42, 56, 72), radius=7, fill=ACCENT)
    draw.text((72, 35), title, font=FONT_TITLE, fill=TEXT)
    if subtitle:
        draw.text((72, 68), subtitle, font=FONT_SMALL, fill=TEXT_2)
    return img, draw, 116


def _console_base(height: int, title: str, subtitle: str = "") -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    height = max(260, height)
    img = Image.new("RGBA", (CANVAS_W, height), BG)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((24, 22, CANVAS_W - 24, 92), radius=20, fill=SURFACE, outline=BORDER, width=1)
    draw.rounded_rectangle((42, 42, 56, 72), radius=7, fill=ACCENT)
    draw.text((72, 35), title, font=FONT_TITLE, fill=TEXT)
    if subtitle:
        draw.text((72, 68), subtitle, font=FONT_SMALL, fill=TEXT_2)
    return img, draw, 116


def _card(draw: ImageDraw.ImageDraw, y: int, h: int, *, fill=SURFACE) -> tuple[int, int, int, int]:
    box = (CARD_X, y, CARD_X + CARD_W, y + h)
    draw.rounded_rectangle(box, radius=18, fill=fill, outline=BORDER, width=1)
    return box


def _pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int, int],
    *,
    font: ImageFont.FreeTypeFont = FONT_SMALL_BOLD,
) -> int:
    text_w = _measure(draw, text, font)
    w = text_w + 28
    draw.rounded_rectangle((x, y, x + w, y + 30), radius=15, fill=(*color[:3], 38), outline=color, width=1)
    draw.text((x + 14, y + 5), text, font=font, fill=color)
    return x + w + 10


def _overview_stat_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int, int],
) -> int:
    text_w = _measure(draw, text, FONT_SMALL_BOLD)
    w = text_w + 42
    draw.rounded_rectangle((x, y, x + w, y + 30), radius=15, fill=SURFACE_2, outline=BORDER, width=1)
    draw.ellipse((x + 13, y + 10, x + 23, y + 20), fill=color)
    draw.text((x + 32, y + 5), text, font=FONT_SMALL_BOLD, fill=TEXT_2)
    return x + w + 10


def _kv(draw: ImageDraw.ImageDraw, x: int, y: int, key: str, value: object, *, width: int = 54) -> int:
    draw.text((x, y), key, font=FONT_SMALL, fill=MUTED)
    lines = _wrap_text(_fit(value, 180), width=width)
    yy = y
    for line in lines:
        draw.text((x + 132, yy), line, font=FONT_SMALL, fill=TEXT_2)
        yy += 25
    return max(y + 28, yy)


def _info_item_height(value: object, width: int) -> int:
    return 24 + _height_for_lines(_wrap_text(_fit(value, 180), width), 25)


def _info_item(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    key: str,
    value: object,
    *,
    wrap_width: int,
) -> int:
    draw.text((x, y), key, font=FONT_SMALL, fill=MUTED)
    yy = y + 24
    for line in _wrap_text(_fit(value, 180), wrap_width):
        draw.text((x, yy), line, font=FONT_SMALL_BOLD, fill=TEXT_2)
        yy += 25
    return max(y + 50, yy)


def _two_column_grid_height(
    left_rows: Sequence[tuple[str, object]],
    right_rows: Sequence[tuple[str, object]],
    full_rows: Sequence[tuple[str, object]],
    *,
    column_width: int,
) -> int:
    wrap_width = max(18, column_width // 11)
    total = 0
    pair_count = max(len(left_rows), len(right_rows))
    for idx in range(pair_count):
        left_h = _info_item_height(left_rows[idx][1], wrap_width) if idx < len(left_rows) else 0
        right_h = _info_item_height(right_rows[idx][1], wrap_width) if idx < len(right_rows) else 0
        total += max(56, left_h, right_h) + 18
    full_wrap_width = max(36, (column_width * 2 + 24) // 11)
    for _, value in full_rows:
        total += max(56, _info_item_height(value, full_wrap_width)) + 18
    return max(0, total - 18)


def _draw_two_column_grid(
    draw: ImageDraw.ImageDraw,
    y: int,
    left_rows: Sequence[tuple[str, object]],
    right_rows: Sequence[tuple[str, object]],
    full_rows: Sequence[tuple[str, object]],
) -> int:
    column_gap = 28
    column_w = (CARD_W - PADDING * 2 - column_gap) // 2
    left_x = CARD_X + PADDING
    right_x = left_x + column_w + column_gap
    wrap_width = max(18, column_w // 11)
    yy = y

    pair_count = max(len(left_rows), len(right_rows))
    for idx in range(pair_count):
        row_bottom = yy
        if idx < len(left_rows):
            row_bottom = max(row_bottom, _info_item(draw, left_x, yy, column_w, *left_rows[idx], wrap_width=wrap_width))
        if idx < len(right_rows):
            row_bottom = max(row_bottom, _info_item(draw, right_x, yy, column_w, *right_rows[idx], wrap_width=wrap_width))
        yy = row_bottom + 18

    full_w = column_w * 2 + column_gap
    full_wrap_width = max(36, full_w // 11)
    for key, value in full_rows:
        yy = _info_item(draw, left_x, yy, full_w, key, value, wrap_width=full_wrap_width) + 18
    return yy


def _status_name(status: int) -> str:
    return STATUS_MAP.get(status, f"UNKNOWN({status})")


def _status_color(status: int) -> tuple[int, int, int, int]:
    return STATUS_COLOR.get(status, MUTED)


def _status_badge(draw: ImageDraw.ImageDraw, right: int, y: int, status: int) -> None:
    text = _status_name(status)
    color = _status_color(status)
    text_w = _measure(draw, text, FONT_SMALL_BOLD)
    w = max(108, text_w + 34)
    x = right - w
    draw.rounded_rectangle((x, y, right, y + 32), radius=16, fill=SURFACE_2, outline=color, width=1)
    draw.text((x + (w - text_w) / 2, y + 6), text, font=FONT_SMALL_BOLD, fill=color)


def _draw_metric_icon(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    kind: str,
    primary: tuple[int, int, int, int],
    secondary: tuple[int, int, int, int],
) -> None:
    if kind == "cpu":
        draw.rounded_rectangle((x + 9, y + 9, x + 29, y + 29), radius=4, outline=primary, width=2)
        draw.rectangle((x + 15, y + 15, x + 23, y + 23), outline=secondary, width=1)
        for offset in (8, 16, 24):
            draw.line((x + offset, y + 4, x + offset, y + 9), fill=primary, width=2)
            draw.line((x + offset, y + 29, x + offset, y + 34), fill=primary, width=2)
            draw.line((x + 4, y + offset, x + 9, y + offset), fill=primary, width=2)
            draw.line((x + 29, y + offset, x + 34, y + offset), fill=primary, width=2)
    elif kind == "memory":
        draw.rounded_rectangle((x + 5, y + 11, x + 33, y + 27), radius=4, outline=primary, width=2)
        for pin_x in (10, 16, 22, 28):
            draw.line((x + pin_x, y + 28, x + pin_x, y + 33), fill=primary, width=2)
        for chip_x in (11, 18, 25):
            draw.rectangle((x + chip_x, y + 15, x + chip_x + 4, y + 22), fill=secondary)
    else:
        draw.rounded_rectangle((x + 7, y + 8, x + 31, y + 30), radius=5, outline=primary, width=2)
        draw.line((x + 11, y + 24, x + 27, y + 24), fill=secondary, width=2)
        draw.ellipse((x + 14, y + 12, x + 24, y + 22), outline=secondary, width=2)
        draw.ellipse((x + 26, y + 26, x + 29, y + 29), fill=primary)


def _load_metric_icon(kind: str) -> Image.Image | None:
    if kind in _METRIC_ICON_CACHE:
        cached = _METRIC_ICON_CACHE[kind]
        return cached.copy() if cached is not None else None
    name = METRIC_ICON_FILES.get(kind)
    if not name:
        _METRIC_ICON_CACHE[kind] = None
        return None
    path = METRIC_ICON_DIR / name
    try:
        icon = Image.open(path).convert("RGBA")
    except Exception:
        _METRIC_ICON_CACHE[kind] = None
        return None
    _METRIC_ICON_CACHE[kind] = icon
    return icon.copy()


def _tint_metric_icon(icon: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
    icon = icon.convert("RGBA")
    alpha = icon.getchannel("A")
    tinted = Image.new("RGBA", icon.size, color)
    tinted.putalpha(alpha)
    return tinted


def _paste_metric_icon(
    base: Image.Image,
    x: int,
    y: int,
    kind: str,
    color: tuple[int, int, int, int],
) -> bool:
    icon = _load_metric_icon(kind)
    if icon is None:
        return False
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    icon.thumbnail((24, 24), resampling)
    icon = _tint_metric_icon(icon, color)
    px = x + (38 - icon.size[0]) // 2
    py = y + (38 - icon.size[1]) // 2
    base.alpha_composite(icon, (px, py))
    return True


def _metric_icon_palette(color: tuple[int, int, int, int]) -> tuple[
    tuple[int, int, int, int],
    tuple[int, int, int, int],
    tuple[int, int, int, int],
]:
    return (
        (18, 24, 35, 255),
        (150, 164, 184, 255),
        (104, 119, 142, 180),
    )


def _metric_card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    kind: str,
    label: str,
    value: str,
    color: tuple[int, int, int, int],
    ratio: float | None,
) -> None:
    draw.rounded_rectangle((x, y, x + w, y + 78), radius=14, fill=TERMINAL, outline=BORDER, width=1)
    icon_fill, icon_primary, icon_secondary = _metric_icon_palette(color)
    draw.rounded_rectangle((x + 14, y + 18, x + 52, y + 56), radius=9, fill=icon_fill)
    if not _paste_metric_icon(draw._image, x + 14, y + 18, kind, icon_primary):
        _draw_metric_icon(draw, x + 14, y + 18, kind, icon_primary, icon_secondary)
    if ratio is None:
        bar_color = MUTED
        bar_w = 38
    elif ratio <= 0:
        bar_color = (*color[:3], 90)
        bar_w = 4
    else:
        bar_color = color
        bar_w = max(12, int(w * ratio))
    draw.rectangle((x, y + 74, x + min(w, bar_w), y + 78), fill=bar_color)
    draw.text((x + 66, y + 16), label, font=FONT_SMALL, fill=TEXT_2)
    draw.text((x + 66, y + 42), _fit(value, 24), font=FONT_SMALL_BOLD, fill=color)


def _save(img: Image.Image) -> BytesIO:
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out


def draw_notice(title: str, lines: Iterable[str] = (), *, level: str = "info") -> BytesIO:
    line_list = [str(line) for line in lines if str(line)]
    wrapped = [part for line in line_list for part in _wrap_text(line, 56)]
    color = {"success": SUCCESS, "warning": WARNING, "error": DANGER}.get(level, INFO)
    card_h = 86 + _height_for_lines(wrapped, 30)
    img, draw, y = _base(150 + card_h, "MCSM", "操作结果")
    _card(draw, y, card_h)
    draw.rounded_rectangle((CARD_X + PADDING, y + 24, CARD_X + PADDING + 16, y + 62), radius=8, fill=color)
    draw.text((CARD_X + PADDING + 28, y + 22), title, font=FONT_H2, fill=color)
    yy = y + 62
    if wrapped:
        for line in wrapped:
            draw.text((CARD_X + PADDING, yy), line, font=FONT_BODY, fill=TEXT_2)
            yy += 30
    return _save(img)


def draw_error(message: str) -> BytesIO:
    return draw_notice("请求未完成", _wrap_text(message, 56), level="error")


def draw_admin_list(alias: str, admins: Sequence[str]) -> BytesIO:
    lines = list(admins) if admins else ["(未设置)"]
    card_h = 92 + len(lines) * 34
    subtitle = "群级权限" if alias == "本群" else f"实例 {alias}"
    img, draw, y = _base(150 + card_h, "MCSM 管理员", subtitle)
    _card(draw, y, card_h)
    draw.text((CARD_X + PADDING, y + 24), "管理员列表", font=FONT_H2, fill=TEXT)
    yy = y + 66
    for admin in lines:
        _pill(draw, CARD_X + PADDING, yy, str(admin), ACCENT if admin != "(未设置)" else MUTED)
        yy += 34
    return _save(img)


def draw_bind_result(alias: str, uuid: str, daemon_id: str) -> BytesIO:
    return draw_notice(
        "实例绑定成功",
        [
            f"别名: {alias}",
            f"UUID: {uuid}",
            f"节点: {daemon_id[:24]}...",
            "/mcsm admin add @某人  添加本群 MCSM 管理员",
        ],
        level="success",
    )


def draw_status(alias: str, detail: dict, bind_info: dict) -> BytesIO:
    status = int(detail.get("status", -1))
    cfg = detail.get("config", {}) or {}
    summary = status_summary(detail, bind_info)
    display_name = summary["display_name"] or alias

    left_rows = [
        ("面板名称", display_name),
        ("启动次数", summary["started"]),
        ("玩家数", summary["players"]),
        ("可用端口", summary["ports"]),
        ("最后启动", summary["last_datetime"]),
        ("节点", f"{str(bind_info.get('daemonId', ''))[:24]}..."),
    ]
    right_rows = [
        ("实例类型", summary["type"] or "N/A"),
        ("自动重启", summary["auto_restart"]),
        ("游戏版本", summary["version"]),
        ("到期时间", summary["end_time"]),
        ("UUID", bind_info.get("uuid", "")),
        ("管理员", summary["admins"]),
    ]
    full_rows = []
    if summary["start_command"]:
        full_rows.append(("启动命令", summary["start_command"]))

    column_w = (CARD_W - PADDING * 2 - 28) // 2
    row_h = _two_column_grid_height(left_rows, right_rows, full_rows, column_width=column_w)
    card_h = 230 + row_h
    img, draw, y = _base(156 + card_h, f"MCSM / {alias}", "实例详情")
    _card(draw, y, card_h)

    draw.text((CARD_X + PADDING, y + 24), _fit(display_name, 34), font=FONT_H2, fill=TEXT)
    _status_badge(draw, CARD_X + CARD_W - PADDING, y + 22, status)
    draw.text((CARD_X + PADDING, y + 58), STATUS_CN.get(status, "未知状态"), font=FONT_SMALL, fill=TEXT_2)

    draw.line((CARD_X + PADDING, y + 94, CARD_X + CARD_W - PADDING, y + 94), fill=BORDER, width=1)
    metric_y = y + 116
    metric_gap = 14
    metric_w = (CARD_W - PADDING * 2 - metric_gap * 2) // 3
    metric_x = CARD_X + PADDING
    _metric_card(draw, metric_x, metric_y, metric_w, "cpu", "CPU 负载", summary["cpu"], ACCENT, summary["cpu_ratio"])
    _metric_card(
        draw,
        metric_x + metric_w + metric_gap,
        metric_y,
        metric_w,
        "memory",
        "内存",
        summary["memory"],
        (126, 87, 255, 255),
        summary["memory_ratio"],
    )
    _metric_card(
        draw,
        metric_x + (metric_w + metric_gap) * 2,
        metric_y,
        metric_w,
        "disk",
        "磁盘",
        summary["disk"],
        (44, 211, 203, 255),
        summary["disk_ratio"],
    )

    _draw_two_column_grid(draw, metric_y + 104, left_rows, right_rows, full_rows)

    return _save(img)


def _is_console_prompt(line: str) -> bool:
    stripped = line.strip()
    return stripped in {">", "$"}


def _canonical_command(command: str) -> str:
    return command.strip()


def _command_header(command: str) -> str:
    normalized = _canonical_command(command)
    return f">{normalized}" if normalized else ""


def _normalize_command_echo(line: str, command: str = "") -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    normalized = _canonical_command(command)
    for marker in (">", "$"):
        if stripped.startswith(marker):
            body = stripped[1:].strip()
            if body and (not normalized or body == normalized):
                return _command_header(body)
    if normalized and stripped == normalized:
        return _command_header(normalized)
    return None


def _is_plain_log_command(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _is_console_prompt(stripped):
        return False
    if _is_log_entry_start(stripped) or _is_log_continuation(line):
        return False
    if any(char in stripped for char in "[]{}"):
        return False
    if ":" in stripped:
        return False
    return bool(_COMMAND_LIKE_RE.match(stripped))


def _is_command_echo(line: str, command: str) -> bool:
    return _normalize_command_echo(line, command) is not None


def _console_entry_color(lines: Sequence[str]) -> tuple[int, int, int, int]:
    text = "\n".join(lines).strip()
    lower = text.lower()
    if any(word in lower for word in ("error", "exception", "fail", "failed", "fatal")):
        return DANGER
    if "warn" in lower:
        return WARNING
    first = lines[0].strip() if lines else ""
    if _normalize_command_echo(first) or _is_plain_log_command(first):
        return TEXT
    if _LOG_ENTRY_RE.match(first):
        return INFO
    return TEXT_2


def _is_log_entry_start(line: str) -> bool:
    stripped = line.lstrip()
    if _LOG_ENTRY_RE.match(stripped):
        return True
    if _LOG_LEVEL_ENTRY_RE.match(stripped):
        return True
    return False


def _is_log_continuation(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if line[:1].isspace():
        return True
    if _URL_RE.match(stripped):
        return True
    if _STACK_CONTINUATION_RE.match(stripped):
        return True
    return False


def parse_console_entries(
    output: str,
    *,
    command: str = "",
    mode: str = "cmd",
) -> list[dict[str, object]]:
    entries: list[list[str]] = []
    current: list[str] = []
    source_lines = [raw.rstrip() for raw in (output or "").splitlines()]
    has_log_anchor = mode == "log" and any(_is_log_entry_start(line) for line in source_lines if line.strip())
    seen_log_anchor = False

    def push_current() -> None:
        nonlocal current
        if current:
            entries.append(current)
            current = []

    for line in source_lines:
        if not line.strip() or _is_console_prompt(line) or (mode != "log" and _is_command_echo(line, command)):
            continue
        if mode == "log" and has_log_anchor and not seen_log_anchor and not _is_log_entry_start(line):
            continue
        if _is_log_entry_start(line):
            seen_log_anchor = True
            push_current()
            current = [line]
        elif mode == "log" and current and _is_log_continuation(line):
            current.append(line)
        elif mode == "log":
            push_current()
            current = [line]
        elif current:
            current.append(line)
        else:
            current = [line]
    push_current()

    return [{"lines": lines, "color": _console_entry_color(lines)} for lines in entries]


def _display_console_line(line: str, *, mode: str = "cmd") -> str:
    normalized = _normalize_command_echo(line)
    if normalized:
        return normalized
    if mode == "log" and _is_plain_log_command(line):
        return _command_header(line.strip())
    return line


def render_console_text(
    output: str,
    *,
    command: str = "",
    max_entries: int | None = None,
    mode: str = "cmd",
    show_command: bool = False,
    empty_text: str = "(无输出)",
) -> str:
    entries = parse_console_entries(output, command=command, mode=mode)
    if max_entries is not None:
        entries = entries[-max_entries:]
    header = _command_header(command) if show_command else ""
    if not entries:
        return "\n".join(part for part in (header, empty_text) if part)
    body = "\n".join(
        "\n".join(_display_console_line(str(line), mode=mode) for line in entry["lines"])
        for entry in entries
    )
    return "\n".join(part for part in (header, body) if part)


def _strip_command_output(output: str, command: str) -> str:
    lines: list[str] = []
    for line in (output or "").splitlines():
        if _is_console_prompt(line) or _is_command_echo(line, command):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _find_last_command_echo_end(output: str, command: str) -> int:
    normalized = _canonical_command(command)
    if not normalized:
        return -1
    best = -1
    offset = 0
    for raw_line in (output or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if _normalize_command_echo(line, normalized):
            best = offset + len(raw_line)
        offset += len(raw_line)
    return best


def extract_command_output(before: str, after: str, command: str) -> str:
    before = before or ""
    after = after or ""
    if not after:
        return ""
    if before and after.startswith(before):
        return _strip_command_output(after[len(before):], command)
    if before:
        for size in (6000, 4000, 2000, 1000, 500):
            tail = before[-size:]
            if tail and tail in after:
                return _strip_command_output(after[after.rfind(tail) + len(tail):], command)

    command_end = _find_last_command_echo_end(after, command)
    if command_end >= 0:
        return _strip_command_output(after[command_end:], command)
    return ""


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return float(bbox[2] - bbox[0])


def _wrap_console_text(draw: ImageDraw.ImageDraw, text: object, max_px: int) -> list[str]:
    value = str(text or "")
    if not value:
        return [""]
    rows: list[str] = []
    for raw_line in value.splitlines() or [""]:
        if not raw_line:
            rows.append("")
            continue
        current = ""
        for char in raw_line:
            candidate = current + char
            if current and _text_width(draw, candidate, FONT_MONO) > max_px:
                rows.append(current)
                current = char
            else:
                current = candidate
        if current:
            rows.append(current)
    return rows or [""]


def _console_display_rows(
    output: str,
    *,
    command: str = "",
    max_entries: int | None = None,
    display_line_limit: int | None = CONSOLE_DISPLAY_LINES,
    mode: str = "cmd",
) -> tuple[list[tuple[str, tuple[int, int, int, int]]], int, int]:
    entries = parse_console_entries(output, command=command, mode=mode)
    omitted_entries = 0
    omitted_lines = 0
    if max_entries is not None and len(entries) > max_entries:
        omitted_entries += len(entries) - max_entries
        entries = entries[-max_entries:]
    elif max_entries is None and len(entries) > CONSOLE_SOURCE_TAIL_LINES:
        omitted_entries += len(entries) - CONSOLE_SOURCE_TAIL_LINES
        entries = entries[-CONSOLE_SOURCE_TAIL_LINES:]

    scratch = Image.new("RGBA", (1, 1))
    scratch_draw = ImageDraw.Draw(scratch)
    max_px = CARD_W - PADDING * 2
    entry_rows: list[list[tuple[str, tuple[int, int, int, int]]]] = []
    for entry in entries:
        color = entry["color"]
        rows: list[tuple[str, tuple[int, int, int, int]]] = []
        for raw_line in entry["lines"]:
            display_line = _display_console_line(str(raw_line), mode=mode)
            for wrapped in _wrap_console_text(scratch_draw, display_line, max_px):
                rows.append((wrapped, color))
        if rows:
            entry_rows.append(rows)

    if display_line_limit is not None:
        total_rows = sum(len(rows) for rows in entry_rows)
        while entry_rows and total_rows > display_line_limit:
            first = entry_rows.pop(0)
            total_rows -= len(first)
            omitted_entries += 1
            omitted_lines += len(first)

    rows = [row for group in entry_rows for row in group]
    return rows, omitted_entries, omitted_lines


def draw_console_output(
    alias: str,
    command: str,
    output: str,
    *,
    max_entries: int | None = None,
    display_line_limit: int | None = CONSOLE_DISPLAY_LINES,
    mode: str = "cmd",
    show_command: bool = False,
    empty_text: str = "(无输出)",
) -> BytesIO:
    rows, omitted_entries, omitted_lines = _console_display_rows(
        output,
        command=command,
        max_entries=max_entries,
        display_line_limit=display_line_limit,
        mode=mode,
    )
    if not rows:
        rows = [(empty_text, MUTED)]
    header_added = False
    if show_command:
        header = _command_header(command)
        if header:
            rows = [(header, TEXT)] + rows
            header_added = True

    has_omitted = omitted_entries or omitted_lines
    card_h = 58 + len(rows) * CONSOLE_LINE_H + (26 if has_omitted else 0)
    img, draw, y = _console_base(154 + card_h, f"MCSM / {alias}", "控制台")
    _card(draw, y, card_h, fill=TERMINAL)

    yy = y + 22
    if header_added and rows:
        line, color = rows[0]
        draw.text((CARD_X + PADDING, yy), line or " ", font=FONT_MONO, fill=color)
        yy += CONSOLE_LINE_H
        rows = rows[1:]

    if has_omitted:
        parts: list[str] = []
        if omitted_entries:
            parts.append(f"{omitted_entries} 条日志")
        if omitted_lines:
            parts.append(f"{omitted_lines} 行内容")
        draw.text((CARD_X + PADDING, yy), f"... 已省略 {' / '.join(parts)}", font=FONT_SMALL, fill=WARNING)
        yy += 26

    for line, color in rows:
        draw.text((CARD_X + PADDING, yy), line or " ", font=FONT_MONO, fill=color)
        yy += CONSOLE_LINE_H

    return _save(img)


def draw_panel_overview(
    daemon_map: dict[str, dict],
    panel_url: str = "",
    *,
    is_superuser: bool = False,
    show_all: bool = False,
) -> BytesIO:
    visible_instances = [inst for dm in daemon_map.values() for inst in dm.get("instances", [])]
    total_daemons = len(daemon_map)
    total_instances = len(visible_instances)
    running = sum(1 for inst in visible_instances if inst.get("status") == 3)

    rows: list[tuple[str, dict | None]] = []
    hidden_extra = 0
    for dm in daemon_map.values():
        rows.append(("daemon", dm))
        instances = list(dm.get("instances", []))
        if len(instances) > 10:
            hidden_extra += len(instances) - 10
            instances = instances[:10]
        for inst in instances:
            rows.append(("instance", inst))
        if not instances:
            rows.append(("empty", None))

    if len(rows) > 54:
        hidden_extra += len(rows) - 54
        rows = rows[:54]

    card_h = 132 + len(rows) * 38 + (34 if hidden_extra else 0)
    img, draw, y = _base(164 + card_h, "MCSM 面板概览", _fit(panel_url, 70))
    _card(draw, y, card_h)

    stat_y = y + 22
    x = CARD_X + PADDING
    x = _overview_stat_pill(draw, x, stat_y, f"{total_daemons} 节点", ACCENT)
    x = _overview_stat_pill(draw, x, stat_y, f"{total_instances} 实例", INFO)
    x = _overview_stat_pill(draw, x, stat_y, f"{running} 运行中", SUCCESS)
    if show_all:
        _overview_stat_pill(draw, x, stat_y, "显示隐藏", WARNING)

    yy = y + 72
    for kind, data in rows:
        if kind == "daemon" and data is not None:
            name = _fit(data.get("name") or data.get("uuid") or "Unknown Daemon", 34)
            online = data.get("online", 0)
            total = data.get("total", 0)
            draw.rounded_rectangle((CARD_X + PADDING, yy, CARD_X + CARD_W - PADDING, yy + 34), radius=10, fill=SURFACE_2)
            draw.text((CARD_X + PADDING + 14, yy + 6), name, font=FONT_SMALL_BOLD, fill=TEXT)
            draw.text((CARD_X + CARD_W - PADDING - 130, yy + 6), f"{online}/{total} 在线", font=FONT_SMALL, fill=TEXT_2)
            yy += 40
        elif kind == "instance" and data is not None:
            status = int(data.get("status", -1))
            label = _fit(data.get("alias") or data.get("name") or data.get("uuid") or "unknown", 36)
            meta = "别名" if data.get("alias") else _fit(str(data.get("uuid", ""))[:12] + "...", 18)
            if data.get("hidden"):
                meta = f"{meta} · hidden"
            draw.ellipse((CARD_X + PADDING + 10, yy + 9, CARD_X + PADDING + 22, yy + 21), fill=_status_color(status))
            draw.text((CARD_X + PADDING + 36, yy + 3), label, font=FONT_SMALL, fill=TEXT)
            draw.text((CARD_X + 430, yy + 3), _status_name(status), font=FONT_SMALL, fill=_status_color(status))
            draw.text((CARD_X + CARD_W - PADDING - 170, yy + 3), meta, font=FONT_SMALL, fill=MUTED)
            yy += 34
        else:
            draw.text((CARD_X + PADDING + 36, yy + 3), "(无实例)", font=FONT_SMALL, fill=MUTED)
            yy += 34

    if hidden_extra:
        draw.text((CARD_X + PADDING, yy + 6), f"还有 {hidden_extra} 项未显示，请缩小范围或查看详情", font=FONT_SMALL, fill=WARNING)
        yy += 34

    footer = "/mcsm status <别名> 查看详情；/help mcsm 查看帮助"
    if is_superuser:
        footer += "；/mcsm list -a 查看隐藏实例"
    draw.text((CARD_X + PADDING, y + card_h - 34), footer, font=FONT_SMALL, fill=MUTED)
    return _save(img)
