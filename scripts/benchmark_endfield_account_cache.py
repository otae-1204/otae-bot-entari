"""Read-only own-account benchmark. Credentials and account snapshots stay in RAM.

Enter the credential with getpass, never a command-line argument. Child processes
receive input over stdin; only timings, counts and image digests go into reports.
No bot messages, attendance, binding, imports or production stores are touched.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import statistics
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from unittest.mock import patch


async def capture(token, *, history=False):
    from plugins.endfield.account.client import EndfieldOfficialClient

    client = EndfieldOfficialClient()
    timings = {}
    try:
        roles = await client.discover_roles(token)
        if not roles:
            raise RuntimeError("No authorized role")
        role = roles[0]
        snapshot = {"role": asdict(role)}
        for name, fn in (
            ("detail", lambda: client.card_detail(token, role)),
            ("balances", lambda: client.currency_balances(token, role)),
            ("monument", lambda: client.indie_hard(token, role)),
            ("war_echoes", lambda: client.war_echoes(token, role)),
        ):
            times = []
            try:
                for _ in range(3):
                    start = perf_counter()
                    snapshot[name] = await fn()
                    times.append(perf_counter() - start)
                timings[name] = {
                    "seconds": times,
                    "median_seconds": statistics.median(times),
                    "ok": True,
                }
            except Exception as exc:
                timings[name] = {"ok": False, "error_type": type(exc).__name__}
        if history:
            from plugins.endfield.account.store import EndfieldRole, EndfieldStore
            from plugins.endfield.gacha.service import EndfieldGachaService

            stored_role = EndfieldRole(
                id=1,
                credential_id=1,
                qq_user_id="benchmark",
                is_primary=True,
                **asdict(role),
            )
            memory = EndfieldStore(":memory:")
            try:
                with patch.object(memory, "decrypt_token", return_value=token):
                    start = perf_counter()
                    result = await EndfieldGachaService(memory, client, None).sync(
                        stored_role, full=True
                    )
                    records = memory.list_gacha_records(stored_role, limit=100000)
                    snapshot["gacha_records"] = [asdict(item) for item in records]
                    timings["gacha_full_sync"] = {
                        "seconds": perf_counter() - start,
                        "records": len(records),
                        "failed_streams": len(result.failed),
                    }
                start = perf_counter()
                logs = await client.currency_logs(token, role)
                snapshot["currency_logs"] = [
                    asdict(item) for records in logs.values() for item in records
                ]
                timings["currency_full_query"] = {
                    "seconds": perf_counter() - start,
                    "records": sum(map(len, logs.values())),
                }
            finally:
                memory.close()
        return snapshot, timings
    finally:
        await client.close()


async def child(args, payload):
    from benchmark_endfield_queries import PublicHTTP, signature
    import plugins.endfield.handlers as endfield
    from loguru import logger

    logger.remove()
    logging.disable(logging.CRITICAL)
    from plugins.endfield.account.store import EndfieldRole, EndfieldStore
    from plugins.endfield.account.detail import names
    from plugins.endfield.account.investment import service as investment
    from otae_bot.infrastructure.http import client as shared
    from otae_bot.infrastructure.rendering.browser import (
        close_browser,
        screenshot_web_element,
    )
    from otae_bot.infrastructure.rendering.executor import close_image_executor

    role = EndfieldRole(
        id=1,
        credential_id=1,
        qq_user_id="benchmark-only",
        is_primary=True,
        **payload["role"],
    )
    store = EndfieldStore(":memory:")
    public = PublicHTTP(args.mode, args.cassette)
    if args.mode == "record":
        args.cassette.mkdir(parents=True, exist_ok=True)
    report = {"mode": args.mode, "cases": {}, "private_data_on_disk": False}
    calls = {"detail": 0, "balances": 0}

    async def detail(*_args):
        calls["detail"] += 1
        if args.mode == "live":
            return await endfield.official_client.card_detail(payload["token"], role)
        return payload["detail"]

    async def balances(*_args):
        calls["balances"] += 1
        if args.mode == "live":
            return await original_balances(payload["token"], role)
        return {int(key): value for key, value in payload.get("balances", {}).items()}

    original_balances = endfield.official_client.currency_balances

    async def finish_pages(_matcher, pages):
        return tuple(pages)

    async def finish_one(_matcher, page):
        return (page,)

    async def clear():
        for cache_name in ("_ACCOUNT_PAGE_CACHE", "_CARD_CACHE", "_LOADOUT_CACHE"):
            cache = getattr(endfield, cache_name, None)
            if cache is not None:
                await cache.clear()
        names._name_map_cache = None
        investment._catalog_cache = None
        await shared.clear_http_cache()

    try:
        with ExitStack() as stack:
            if args.mode != "live":
                stack.enter_context(public.install())
            stack.enter_context(patch.object(endfield, "account_store", store))
            stack.enter_context(
                patch.object(
                    store, "decrypt_token", return_value="benchmark-placeholder"
                )
            )
            stack.enter_context(
                patch.object(endfield, "_card_detail_with_snapshot", detail)
            )
            stack.enter_context(
                patch.object(endfield.official_client, "currency_balances", balances)
            )
            stack.enter_context(patch.object(endfield, "_finish_pngs", finish_pages))
            stack.enter_context(patch.object(endfield, "_finish_png", finish_one))
            await screenshot_web_element(
                "data:text/html,<body>benchmark</body>", settle_ms=0
            )
            for name in ("detail", "base", "investment"):
                fn = getattr(endfield, f"_render_account_{name}")
                rows = report["cases"][name] = {"cold": [], "warm": []}
                try:
                    for phase, count in (("cold", args.runs), ("warm", args.runs)):
                        for _ in range(count):
                            if phase == "cold":
                                await clear()
                            before_calls = dict(calls)
                            start_calls, start_bytes = (
                                public.calls,
                                public.downloaded_bytes,
                            )
                            start_failures, start_rejected = (
                                len(public.failures),
                                len(public.rejected),
                            )
                            start = perf_counter()
                            pages = await asyncio.wait_for(
                                fn(None, role, None, group=True), 240
                            )
                            seconds = perf_counter() - start
                            if public.rejected[start_rejected:]:
                                raise RuntimeError("Public cassette incomplete")
                            rows[phase].append(
                                {
                                    "seconds": seconds,
                                    "http_calls": public.calls - start_calls,
                                    "response_bytes": public.downloaded_bytes
                                    - start_bytes,
                                    "http_failures": public.failures[start_failures:],
                                    "account_data_reads": {
                                        key: calls[key] - before_calls[key]
                                        for key in calls
                                    },
                                    "outputs": signature(pages),
                                }
                            )
                        rows[f"{phase}_median_seconds"] = statistics.median(
                            row["seconds"] for row in rows[phase]
                        )
                except Exception as exc:
                    rows["error_type"] = type(exc).__name__
            if "gacha_records" in payload:
                from plugins.endfield.account.store import GachaRecord
                from plugins.endfield.account.client import CurrencyLogItem
                from plugins.endfield.account.currency.service import (
                    aggregate_currency_logs,
                )

                records = [GachaRecord(**row) for row in payload["gacha_records"]]
                logs = [
                    CurrencyLogItem(**row) for row in payload.get("currency_logs", [])
                ]
                history = report["history_cpu"] = {
                    "gacha_records": len(records),
                    "currency_records": len(logs),
                }
                for name, fn in (
                    ("gacha_insert", lambda: store.insert_gacha_records(records)),
                    (
                        "gacha_repeat_upsert",
                        lambda: store.insert_gacha_records(records),
                    ),
                    (
                        "currency_aggregate",
                        lambda: [
                            aggregate_currency_logs(logs, kind) for kind in (1, 2, 3)
                        ],
                    ),
                ):
                    times = []
                    for _ in range(args.runs):
                        if name == "gacha_insert":
                            store.conn.execute("DELETE FROM gacha_records")
                            store.conn.commit()
                        start = perf_counter()
                        value = fn()
                        times.append(perf_counter() - start)
                    history[name] = {
                        "seconds": times,
                        "median_seconds": statistics.median(times),
                        "output": signature(value),
                    }
            if args.mode == "live":
                report["live_personal_queries"] = {}
                for name in ("indie_hard", "war_echoes", "endfield_card_detail"):
                    times = []
                    try:
                        for _ in range(args.runs):
                            start = perf_counter()
                            await getattr(endfield.official_client, name)(
                                payload["token"], role
                            )
                            times.append(perf_counter() - start)
                        report["live_personal_queries"][name] = {
                            "seconds": times,
                            "median_seconds": statistics.median(times),
                        }
                    except Exception as exc:
                        report["live_personal_queries"][name] = {
                            "error_type": type(exc).__name__
                        }
    finally:
        await close_browser()
        await close_image_executor()
        await shared.close_http_client()
        await endfield.official_client.close()
        store.close()
    try:
        import resource

        report["python_peak_rss_mib"] = (
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        )
    except ImportError:
        pass
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--cassette",
        type=Path,
        default=Path("output/endfield-performance/account-public-http"),
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--child", action="store_true")
    parser.add_argument(
        "--mode", choices=("live", "record", "replay"), default="record"
    )
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.cassette = args.cassette.resolve()
    if args.output:
        args.output = args.output.resolve()
    if args.baseline:
        args.baseline = args.baseline.resolve()
    sys.path.insert(0, str(args.repo))
    from loguru import logger

    logger.remove()
    logging.disable(logging.CRITICAL)
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="endfield-account-bench-") as directory:
        os.chdir(directory)
        try:
            if args.child:
                payload = json.load(sys.stdin)
                report = asyncio.run(child(args, payload))
                print(json.dumps(report), flush=True)
                return
            if args.baseline is None or args.output is None:
                parser.error("--baseline and --output are required")
            if not sys.stdin.isatty():
                parser.error("Run in an interactive terminal for hidden credential input")
            token = getpass.getpass("Account token (hidden, RAM only): ")
            snapshot, timings = asyncio.run(capture(token, history=args.history))
            print(json.dumps({"live_readonly_apis": timings}), flush=True)
            if "detail" not in snapshot:
                raise RuntimeError("Account detail capture failed")
            report = {
                "live_readonly_apis": timings,
                "comparison": "Identical own-account snapshot in RAM; render pipeline only, excludes private API latency and QQ transport.",
                "runs": args.runs,
            }
            for label, repo, mode in (
                ("before", args.baseline, "record"),
                ("after", args.repo, "replay"),
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child",
                        "--repo",
                        str(repo),
                        "--mode",
                        mode,
                        "--runs",
                        str(args.runs),
                        "--cassette",
                        str(args.cassette),
                    ],
                    input=json.dumps(snapshot),
                    text=True,
                    capture_output=True,
                    timeout=1500,
                )
                if result.returncode:
                    # Never echo subprocess stderr: exceptions can contain private API data.
                    raise RuntimeError("Account benchmark child failed")
                report[label] = json.loads(result.stdout.splitlines()[-1])
                report[label]["commit"] = subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
                ).strip()
                print(
                    json.dumps(
                        {
                            "phase": label,
                            "cases": {
                                key: {
                                    k: v
                                    for k, v in value.items()
                                    if k.endswith("seconds") or k == "error_type"
                                }
                                for key, value in report[label]["cases"].items()
                            },
                        }
                    ),
                    flush=True,
                )
            # Also replay the baseline: record/live timings must not be compared
            # against replay timings as if network conditions were identical.
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    "--repo",
                    str(args.baseline),
                    "--mode",
                    "replay",
                    "--runs",
                    str(args.runs),
                    "--cassette",
                    str(args.cassette),
                ],
                input=json.dumps(snapshot),
                text=True,
                capture_output=True,
                timeout=1500,
            )
            if result.returncode:
                raise RuntimeError("Baseline replay failed")
            report["before_replay"] = json.loads(result.stdout.splitlines()[-1])
            snapshot["token"] = token
            for label, repo in (
                ("before_live", args.baseline),
                ("after_live", args.repo),
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child",
                        "--repo",
                        str(repo),
                        "--mode",
                        "live",
                        "--runs",
                        str(args.runs),
                        "--cassette",
                        str(args.cassette),
                    ],
                    input=json.dumps(snapshot),
                    text=True,
                    capture_output=True,
                    timeout=1500,
                )
                if result.returncode:
                    raise RuntimeError("Live account benchmark failed")
                report[label] = json.loads(result.stdout.splitlines()[-1])
                print(json.dumps({"phase": label, "complete": True}), flush=True)
            del token, snapshot
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                json.dumps({"report": str(args.output), "complete": True}), flush=True
            )
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
