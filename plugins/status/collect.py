"""运行状态的数据收集。只读本进程里已有的数据，不发网络请求；耗时的部分放进线程。"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from pathlib import Path

from arclet.entari.plugin.service import plugin_service

from otae_bot.accounts import runtime
from otae_bot.command_usage import usage_recorder
from otae_bot.command_usage.store import TodaySummary
from otae_bot.compat.entari import timer
from otae_bot.config.settings import SATORI_CLIENTS
from otae_bot.groups.features import PROTECTED_PLUGINS, plugin_key
from otae_bot.infrastructure.loop_watchdog import loop_lag_snapshot
from otae_bot.infrastructure.recent_alerts import Alert, recent_alerts
from otae_bot.infrastructure.rendering.browser import (
    browser_queue_snapshot,
    render_timing_snapshot,
)
from otae_bot.paths import PROJECT_ROOT
from otae_bot.plugin_registry import discover_plugins

try:
    import psutil
except ImportError:  # pragma: no cover - psutil 在 requirements 里，缺了也只是不显示资源
    psutil = None

#: 公开版不列出的管理类插件。
ADMIN_PLUGINS = frozenset(PROTECTED_PLUGINS | {"peek"})
CACHE_DIR = Path("data") / "cache"
CACHE_SIZE_TTL = 600.0
_PROCESS_STARTED = psutil.Process().create_time() if psutil else time.time()
_cache_size: tuple[float, int] | None = None
_git_head: str | None = None


@dataclass(frozen=True)
class Meter:
    label: str
    ratio: float | None
    value: str
    warn: bool = False


@dataclass(frozen=True)
class Job:
    label: str
    interval_seconds: float
    last_finished: datetime | None
    last_ok: bool | None
    last_error: str
    failures: int
    running: bool


@dataclass
class StatusSnapshot:
    now: datetime
    version: str
    started_at: datetime
    accounts: list[tuple[str, str, int | None]]  # (platform, self_id, 群数)
    render: dict
    render_timing: dict
    loop_lag: dict
    meters: list[Meter]
    plugins: list[tuple[str, bool]]  # (插件 key, 是否已加载)
    today: TodaySummary
    # 以下只在详细版里收集
    endpoints: list[str] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    runtime_info: list[tuple[str, str]] = field(default_factory=list)

    @property
    def uptime(self) -> str:
        seconds = int((self.now - self.started_at).total_seconds())
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {rest // 60} 分钟"
        return f"{rest // 60} 分钟"


def _size(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB", "MB"} else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _version() -> str:
    try:
        from plugins.changelog.models import load_changelog

        return load_changelog().latest.version
    except Exception:  # noqa: BLE001 - 版本号只是展示
        return ""


def _git_short_head() -> str:
    global _git_head
    if _git_head is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3,
                check=False,
            )
            branch = result.stdout.strip()
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3,
                check=False,
            )
            _git_head = f"{branch}@{result.stdout.strip()}" if result.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            _git_head = ""
    return _git_head


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _cache_bytes() -> int:
    global _cache_size
    now = time.monotonic()
    if _cache_size is None or now - _cache_size[0] > CACHE_SIZE_TTL:
        _cache_size = (now, _directory_size(CACHE_DIR) if CACHE_DIR.is_dir() else 0)
    return _cache_size[1]


def _meters(detail: bool) -> list[Meter]:
    meters: list[Meter] = []
    if psutil is not None:
        cpu = psutil.cpu_percent(interval=0.3)
        meters.append(Meter("CPU", cpu / 100, f"{cpu:.0f}%", cpu >= 85))
        memory = psutil.virtual_memory()
        used = memory.total - memory.available
        meters.append(Meter("内存", used / memory.total, f"{_size(used)} / {_size(memory.total)}", memory.percent >= 90))
    disk = shutil.disk_usage(PROJECT_ROOT)
    meters.append(Meter("磁盘", disk.used / disk.total, f"{_size(disk.used)} / {_size(disk.total)}", disk.used / disk.total >= 0.9))
    if detail:
        if psutil is not None:
            meters.append(Meter("bot 进程", None, f"内存 {_size(psutil.Process().memory_info().rss)}"))
        meters.append(Meter("缓存目录", None, _size(_cache_bytes())))
    return meters


def _plugins() -> list[tuple[str, bool]]:
    loaded = {plugin_key(plugin.path) for plugin in plugin_service.plugins.values()}
    names = [name.split(".", 1)[1] for name in discover_plugins(PROJECT_ROOT / "plugins")]
    return [(name, name in loaded) for name in names]


def _job_label(job_id: str) -> str:
    from otae_bot.groups.features import plugin_label

    parts = job_id.split(".")
    plugin = parts[1] if len(parts) > 2 and parts[0] == "plugins" else ""
    func = parts[-1].strip("_") or job_id
    return f"{plugin_label(plugin)} · {func}" if plugin else func


def _jobs() -> list[Job]:
    jobs = []
    for job in timer.snapshot():
        finished = job["last_finished"]
        jobs.append(
            Job(
                _job_label(job["id"]),
                job["interval_seconds"],
                datetime.fromtimestamp(finished) if finished else None,
                job["last_ok"],
                job["last_error"],
                job["failures"],
                job["running"],
            )
        )
    # 出过错的排前面，其次按上次运行时间倒序。
    return sorted(jobs, key=lambda job: (job.last_ok is not False, -(job.last_finished.timestamp() if job.last_finished else 0)))


def _runtime_info() -> list[tuple[str, str]]:
    def version(package: str) -> str:
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return "?"

    return [
        ("代码", _git_short_head() or "未知"),
        ("Python", f"{sys.version.split()[0]} · {platform.system()} {platform.release()}"),
        ("框架", f"Entari {version('arclet-entari')} · Playwright {version('playwright')}"),
    ]


def _accounts() -> list[tuple[str, str, int | None]]:
    rows = []
    for account in runtime.get_bots():
        guilds = runtime.known_guilds(account)
        rows.append((str(account.platform or ""), str(account.self_id or ""), len(guilds) if guilds is not None else None))
    return rows


async def collect(*, detail: bool) -> StatusSnapshot:
    await usage_recorder.flush()
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    meters, today, version = await asyncio.gather(
        asyncio.to_thread(_meters, detail),
        asyncio.to_thread(usage_recorder.store.today, now),
        asyncio.to_thread(_version),
    )
    snapshot = StatusSnapshot(
        now=now,
        version=version,
        started_at=datetime.fromtimestamp(_PROCESS_STARTED),
        accounts=_accounts(),
        render=browser_queue_snapshot(),
        render_timing=render_timing_snapshot(midnight.timestamp()),
        loop_lag=loop_lag_snapshot(),
        meters=meters,
        plugins=_plugins(),
        today=today,
    )
    if detail:
        snapshot.endpoints = [f"{c.get('host', 'localhost')}:{c.get('port', 5500)}" for c in (SATORI_CLIENTS or [{}])]
        snapshot.jobs = _jobs()
        snapshot.alerts = recent_alerts(5)
        snapshot.runtime_info = await asyncio.to_thread(_runtime_info)
    return snapshot
