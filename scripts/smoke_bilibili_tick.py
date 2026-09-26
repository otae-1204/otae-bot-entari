"""Real-network smoke run for the Bilibili poller. Run: python scripts/smoke_bilibili_tick.py.

Plan section 7 asks for a manual (not-in-CI) script that takes the production
database's targets, runs one tick per kind against the real Bilibili endpoints
with the notifier replaced by a printer, and reports the request count, the
elapsed time and the worst event-loop stall.

The production database is only read: its targets are copied into a temporary
database first, so this script can never modify production state.

Two rounds are run by default so a cold start (TLS handshakes, WBI fetch) can be
compared with the warm state, which is the comparison the plan's stop-the-bleed
measurement used.

Usage:
    python scripts/smoke_bilibili_tick.py                 # one cold + one warm round
    python scripts/smoke_bilibili_tick.py --rounds 3
    python scripts/smoke_bilibili_tick.py --db path/to/bilibili.db
    python scripts/smoke_bilibili_tick.py --kinds live,video
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from loguru import logger  # noqa: E402

from plugins.bilibilibot.api import BiliApi  # noqa: E402
from plugins.bilibilibot.poller import Poller  # noqa: E402
from plugins.bilibilibot.store import BiliStore  # noqa: E402

# The plan's budget: no single stall may exceed this during one tick.
STALL_BUDGET_SECONDS = 0.05
DEFAULT_DB = ROOT / "data" / "bilibilibot" / "bilibili.db"
WATCHDOG_INTERVAL_SECONDS = 0.005


class CountingTransport(httpx.AsyncBaseTransport):
    """A real network transport that also records every request it makes."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(f"{request.method} {request.url}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class PrintingNotifier:
    """Stands in for the real notifier: records events instead of sending them.

    The poller only calls `wait_backlog_below` and `wake`, so this keeps the
    poll path identical while guaranteeing nothing is delivered.
    """

    def __init__(self) -> None:
        self.woken = 0

    async def wait_backlog_below(self, limit: int | None = None) -> None:
        return None

    def wake(self) -> None:
        self.woken += 1


async def _watch_loop(stalls: list[float], stop: asyncio.Event, budget: float) -> None:
    """Sample the loop like the production watchdog and keep the worst stall."""
    while not stop.is_set():
        started = perf_counter()
        try:
            await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        stalls.append(perf_counter() - started - WATCHDOG_INTERVAL_SECONDS)


async def _run_round(
    poller: Poller, store: BiliStore, transport: CountingTransport, kinds: list[str], budget: float
) -> dict:
    """One tick per kind, watched for stalls, with the request count it caused."""
    before = len(transport.urls)
    stalls: list[float] = []
    stop = asyncio.Event()
    watchdog = asyncio.create_task(_watch_loop(stalls, stop, budget))

    started = perf_counter()
    failures: list[str] = []
    try:
        for kind in kinds:
            try:
                if kind == "live":
                    await poller.tick_live()
                elif kind == "video":
                    await poller.tick_video()
                else:
                    await poller.tick_dynamic()
            except Exception as exc:  # one kind failing must not hide the others
                failures.append(f"{kind}: {exc}")
    finally:
        elapsed = perf_counter() - started
        stop.set()
        await asyncio.gather(watchdog, return_exceptions=True)

    return {
        "elapsed": elapsed,
        "requests": len(transport.urls) - before,
        "worst_stall": max(stalls) if stalls else 0.0,
        "failures": failures,
        "pending": await store.outbox_count(),
    }


async def _main(args: argparse.Namespace) -> int:
    source = Path(args.db)
    if not source.exists():
        print(f"production database not found: {source}")
        return 2

    with tempfile.TemporaryDirectory(prefix="bili-smoke-") as tmp:
        # Copy first: the tick writes outbox rows, and production must not change.
        work_db = Path(tmp) / "bilibili.db"
        shutil.copy2(source, work_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(work_db) + suffix))

        store = BiliStore(work_db, Path(tmp) / "missing-legacy.db")
        await store.open()
        transport = CountingTransport()
        api = BiliApi(transport=transport)
        notifier = PrintingNotifier()
        poller = Poller(api, store, notifier)

        kinds = [item.strip() for item in args.kinds.split(",") if item.strip()]
        try:
            targets = await store.list_active_targets()
            live_targets = [target for target in targets if target.kind == "live"]
            print(f"database      : {source} (copied read-only into a temp dir)")
            print(f"active targets: {len(targets)} (live={len(live_targets)}, "
                  f"video={len([t for t in targets if t.kind == 'video'])}, "
                  f"dynamic={len([t for t in targets if t.kind == 'dynamic'])})")
            print(f"kinds         : {', '.join(kinds)}")
            print(f"stall budget  : {STALL_BUDGET_SECONDS * 1000:.0f} ms")
            print()

            worst_overall = 0.0
            exit_code = 0
            for index in range(1, args.rounds + 1):
                label = "cold" if index == 1 else "warm"
                result = await _run_round(poller, store, transport, kinds, STALL_BUDGET_SECONDS)
                worst_overall = max(worst_overall, result["worst_stall"])
                print(
                    f"round {index} ({label:4s}): "
                    f"requests={result['requests']:3d} "
                    f"elapsed={result['elapsed']:6.2f}s "
                    f"worst_stall={result['worst_stall'] * 1000:7.1f} ms "
                    f"outbox_pending={result['pending']}"
                )
                for failure in result["failures"]:
                    print(f"    kind failed: {failure}")
                    exit_code = 1
                if result["worst_stall"] > STALL_BUDGET_SECONDS:
                    print(
                        f"    STALL OVER BUDGET: {result['worst_stall'] * 1000:.1f} ms "
                        f"> {STALL_BUDGET_SECONDS * 1000:.0f} ms"
                    )
                    exit_code = 1

            print()
            print(f"worst stall across {args.rounds} round(s): {worst_overall * 1000:.1f} ms")
            if notifier.woken:
                print(f"events committed: {notifier.woken} wake-up(s) (nothing was delivered)")
            return exit_code
        finally:
            await api.aclose()
            await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="production database to read targets from")
    parser.add_argument("--rounds", type=int, default=2, help="number of rounds (1 = cold only)")
    parser.add_argument("--kinds", default="live,video,dynamic", help="comma-separated kinds to tick")
    args = parser.parse_args()

    # The script's own output is the report; keep loguru's poll lines visible.
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
