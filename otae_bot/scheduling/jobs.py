"""Interval jobs on Entari's scheduler, with ids, overlap skipping and run statistics.

Each job runs in its own task so a slow tick never delays the scheduler; a tick
is skipped while ``max_instances`` runs are still active. ``snapshot()`` feeds
``/status``; ``close_scheduled_jobs()`` cancels everything at shutdown.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import Any, Callable

from arclet.entari import scheduler as entari_scheduler
from arclet.entari.plugin import current_plugin
from loguru import logger


@dataclass
class _ManagedJob:
    job_id: str
    subscriber: Any
    active_tasks: set[asyncio.Task[Any]]
    max_instances: int
    interval_seconds: float = 0.0
    runs: int = 0
    failures: int = 0
    last_finished: float | None = None
    last_ok: bool | None = None
    last_error: str = ""


class _Scheduler:
    def __init__(self):
        self._jobs: dict[str, _ManagedJob] = {}
        self._retired_tasks: set[asyncio.Task[Any]] = set()

    def scheduled_job(self, trigger: str, **kwargs):
        def decorator(func: Callable[..., Any]):
            self._register(
                func,
                trigger,
                kwargs,
                handler_module=getattr(func, "__module__", __name__),
            )
            return func

        return decorator

    def add_job(self, func: Callable[..., Any], trigger: str, **kwargs):
        return self._register(
            func,
            trigger,
            kwargs,
            handler_module=_plugin_module(getattr(func, "__module__", __name__)),
        )

    def _register(
        self,
        func: Callable[..., Any],
        trigger: str,
        kwargs: dict[str, Any],
        *,
        handler_module: str,
    ):
        seconds = _seconds(trigger, kwargs)
        job_id = str(kwargs.get("id") or f"{handler_module}.{func.__qualname__}")
        max_instances = max(1, int(kwargs.get("max_instances", 1)))
        replace_existing = bool(kwargs.get("replace_existing", True))

        existing = self._jobs.get(job_id)
        if existing is not None:
            if not replace_existing:
                raise ValueError(f"scheduled job already exists: {job_id}")
            self._dispose_job(existing)

        managed = _ManagedJob(job_id, None, set(), max_instances, interval_seconds=seconds)

        async def _job():
            if len(managed.active_tasks) >= managed.max_instances:
                logger.debug(f"[scheduler] skipped overlapping job id={job_id}")
                return None
            task = asyncio.create_task(self._run_job(job_id, func, managed))
            managed.active_tasks.add(task)
            task.add_done_callback(managed.active_tasks.discard)
            return None

        _job.__module__ = handler_module
        managed.subscriber = entari_scheduler.schedule(
            lambda: timedelta(seconds=seconds)
        )(_job)
        self._jobs[job_id] = managed
        return _job

    async def _run_job(self, job_id: str, func: Callable[..., Any], managed: _ManagedJob | None = None) -> None:
        started = perf_counter()
        try:
            if inspect.iscoroutinefunction(func):
                result = func()
            else:
                result = await asyncio.to_thread(func)
            if inspect.isawaitable(result):
                await result
            if managed is not None:
                managed.last_ok, managed.last_error = True, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"[scheduler] job failed id={job_id}")
            if managed is not None:
                managed.failures += 1
                managed.last_ok, managed.last_error = False, f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if managed is not None:
                managed.runs += 1
                managed.last_finished = time.time()
            logger.debug(
                f"[scheduler] job completed id={job_id} elapsed={perf_counter() - started:.3f}s"
            )

    def _dispose_job(self, job: _ManagedJob) -> None:
        if job.subscriber is not None:
            job.subscriber.dispose()
        for task in tuple(job.active_tasks):
            task.cancel()
            self._retired_tasks.add(task)
            task.add_done_callback(self._retired_tasks.discard)
        job.active_tasks.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        """Interval, run count and last outcome of every registered job (for /status)."""
        return [
            {
                "id": job.job_id,
                "interval_seconds": job.interval_seconds,
                "runs": job.runs,
                "failures": job.failures,
                "last_finished": job.last_finished,
                "last_ok": job.last_ok,
                "last_error": job.last_error,
                "running": bool(job.active_tasks),
            }
            for job in self._jobs.values()
        ]

    async def close(self) -> None:
        jobs = list(self._jobs.values())
        self._jobs.clear()
        tasks = [task for job in jobs for task in job.active_tasks]
        tasks.extend(self._retired_tasks)
        self._retired_tasks.clear()
        for job in jobs:
            if job.subscriber is not None:
                job.subscriber.dispose()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


timer = _Scheduler()


def _seconds(trigger: str, kwargs: dict[str, Any]) -> float:
    if trigger != "interval":
        return 60.0
    if "seconds" in kwargs:
        return float(kwargs["seconds"])
    if "minutes" in kwargs:
        return float(kwargs["minutes"]) * 60
    if "hours" in kwargs:
        return float(kwargs["hours"]) * 3600
    return 60.0


async def close_scheduled_jobs() -> None:
    await timer.close()


def _plugin_module(module: str) -> str:
    # Imported service methods belong to the scope registering the job, which
    # can be a handlers submodule rather than the top-level plugin package.
    if plugin := current_plugin.get(None):
        return plugin.module.__name__
    parts = module.split(".")
    # Entari owns handlers.py as a plugin submodule; scheduled callbacks must
    # retain that owner so registration and disposal use the same scope.
    if len(parts) >= 3 and parts[0] == "plugins" and parts[2] == "handlers":
        return ".".join(parts[:3])
    if len(parts) >= 3 and parts[0] == "plugins":
        return ".".join(parts[:2])
    return module
