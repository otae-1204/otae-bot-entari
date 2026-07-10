from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.image_executor import close_image_executor, run_image_render


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_bili_module(module_name: str):
    package_name = f"runtime_bilibilibot_{module_name}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "plugins/bilibilibot")]
    sys.modules[package_name] = package
    models = _load_module(f"{package_name}.models", "plugins/bilibilibot/models.py")
    sys.modules[f"{package_name}.models"] = models
    return _load_module(
        f"{package_name}.{module_name}",
        f"plugins/bilibilibot/{module_name}.py",
    )


class ImageExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_image_executor()

    async def test_render_concurrency_is_two_and_event_loop_stays_responsive(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def renderer(value: int) -> int:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return value

        renders = asyncio.gather(*(run_image_render(renderer, index) for index in range(6)))
        heartbeats = 0

        async def heartbeat():
            nonlocal heartbeats
            while not renders.done():
                heartbeats += 1
                await asyncio.sleep(0.005)

        results, _ = await asyncio.gather(renders, heartbeat())
        self.assertEqual(results, list(range(6)))
        self.assertEqual(maximum, 2)
        self.assertGreaterEqual(heartbeats, 5)

    async def test_close_allows_lazy_recreation(self):
        self.assertEqual(await run_image_render(lambda: "first"), "first")
        await close_image_executor()
        self.assertEqual(await run_image_render(lambda: "second"), "second")


class ManagedSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_module("entari_native_runtime_for_test", "utils/entari_native.py")
        self.subscribers = []

        class Subscriber:
            def __init__(subscriber_self, callback):
                subscriber_self.callback = callback
                subscriber_self.disposed = False

            def dispose(subscriber_self):
                subscriber_self.disposed = True

        def schedule(_supplier):
            def decorator(callback):
                subscriber = Subscriber(callback)
                self.subscribers.append(subscriber)
                return subscriber
            return decorator

        self.original_schedule = self.module.entari_scheduler.schedule
        self.module.entari_scheduler.schedule = schedule
        self.scheduler = self.module._Scheduler()

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.module.entari_scheduler.schedule = self.original_schedule

    async def test_tick_returns_immediately_and_prevents_overlap(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_job():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        tick = self.scheduler.add_job(slow_job, "interval", seconds=1, id="slow")
        await asyncio.wait_for(tick(), timeout=0.05)
        await started.wait()
        await tick()
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        await asyncio.sleep(0.01)
        await tick()
        await asyncio.sleep(0.01)
        self.assertEqual(calls, 2)

    async def test_replace_existing_disposes_old_subscription(self):
        async def job():
            return None

        self.scheduler.add_job(job, "interval", seconds=1, id="same")
        first = self.subscribers[-1]
        self.scheduler.add_job(job, "interval", seconds=1, id="same", replace_existing=True)
        self.assertTrue(first.disposed)
        self.assertEqual(len(self.scheduler._jobs), 1)

    async def test_slow_job_does_not_delay_another_job(self):
        slow_started = asyncio.Event()
        fast_finished = asyncio.Event()

        async def slow_job():
            slow_started.set()
            await asyncio.Event().wait()

        async def fast_job():
            fast_finished.set()

        slow_tick = self.scheduler.add_job(slow_job, "interval", seconds=1, id="slow")
        fast_tick = self.scheduler.add_job(fast_job, "interval", seconds=1, id="fast")
        await slow_tick()
        await slow_started.wait()
        await fast_tick()
        await asyncio.wait_for(fast_finished.wait(), timeout=0.05)

    async def test_synchronous_job_runs_off_event_loop(self):
        heartbeat = asyncio.Event()

        def sync_job():
            time.sleep(0.05)

        tick = self.scheduler.add_job(sync_job, "interval", seconds=1, id="sync")
        await tick()
        await asyncio.sleep(0.005)
        heartbeat.set()
        await asyncio.wait_for(heartbeat.wait(), timeout=0.02)

    async def test_failed_job_is_released_for_next_tick(self):
        calls = 0

        async def job():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first failure")

        tick = self.scheduler.add_job(job, "interval", seconds=1, id="failure")
        await tick()
        await asyncio.sleep(0.01)
        await tick()
        await asyncio.sleep(0.01)
        self.assertEqual(calls, 2)

    async def test_close_cancels_running_jobs(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def job():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        tick = self.scheduler.add_job(job, "interval", seconds=1, id="close")
        await tick()
        await started.wait()
        await self.scheduler.close()
        self.assertTrue(cancelled.is_set())


class BilibiliPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_close_is_idempotent(self):
        store_module = _load_bili_module("store")
        with TemporaryDirectory() as temp_dir:
            store = store_module.BiliStore(
                Path(temp_dir) / "bilibili.db",
                Path(temp_dir) / "missing.db",
            )
            store.close()
            store.close()

    async def test_twenty_four_targets_use_four_workers_and_isolate_failures(self):
        service_module = _load_bili_module("service")
        models = sys.modules[service_module.__package__ + ".models"]
        active = 0
        maximum = 0
        completed = []
        heartbeat_times = []
        targets = [models.TargetInfo("live", str(index), name=f"UP {index}") for index in range(24)]

        class Store:
            def list_active_targets(self, _kind):
                return targets

            def upsert_target(self, _target):
                return None

        class Client:
            async def latest_live_state(self, target):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1
                completed.append(target.uid)
                if target.uid == "7":
                    raise RuntimeError("failed target")
                return target

        service = service_module.BiliService(Store(), Client(), poll_concurrency=4)
        poll_task = asyncio.create_task(service.check_live())

        async def heartbeat():
            while not poll_task.done():
                heartbeat_times.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.005)

        await asyncio.gather(poll_task, heartbeat())
        heartbeat_gaps = [
            right - left
            for left, right in zip(heartbeat_times, heartbeat_times[1:])
        ]
        self.assertEqual(maximum, 4)
        self.assertEqual(len(completed), 24)
        self.assertTrue(heartbeat_gaps)
        self.assertLess(max(heartbeat_gaps), 0.1)

    async def test_same_kind_poll_is_singleflighted(self):
        store_module = _load_bili_module("store")
        service_module = _load_bili_module("service")
        models = sys.modules[store_module.__package__ + ".models"]
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Client:
            async def latest_live_state(self, target):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return target

        with TemporaryDirectory() as temp_dir:
            store = store_module.BiliStore(
                Path(temp_dir) / "bilibili.db",
                Path(temp_dir) / "missing.db",
            )
            store.upsert_target(models.TargetInfo("live", "1", name="UP"))
            store.add_subscription("live", "1", "group", "900")
            try:
                service = service_module.BiliService(store, Client(), poll_concurrency=4)
                first = asyncio.create_task(service.check_live())
                await started.wait()
                await service.check_live()
                self.assertEqual(calls, 1)
                release.set()
                await first
            finally:
                store.close()

    async def test_broadcasts_to_same_subscriber_are_serialized(self):
        service_module = _load_bili_module("service")
        models = sys.modules[service_module.__package__ + ".models"]
        active = 0
        maximum = 0

        class Subscription:
            subscriber_type = "group"
            subscriber_id = "900"

        class Store:
            def subscriptions_for_target(self, _kind, _uid):
                return [Subscription()]

        class Client:
            pass

        class Message:
            def __init__(self, segments):
                self.uid = segments[0]

            async def send(self, _target, _bot):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1

        service = service_module.BiliService(Store(), Client(), poll_concurrency=4)
        service.card_to_segment = lambda card: asyncio.sleep(0, result=card.uid)
        service_module.get_bot = lambda: object()
        service_module.account_adapter_name = lambda _bot: ""
        service_module.ChainMsg = Message

        await asyncio.gather(
            *(
                service.broadcast(
                    "live",
                    str(index),
                    models.BiliCard("live", str(index), uid=str(index)),
                )
                for index in range(8)
            )
        )
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
