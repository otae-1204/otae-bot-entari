"""Measure public Endfield queries without running a bot or using account credentials.

Live/record measures real public HTTP requests. Replay uses the recorded public
responses without network delay, allowing CPU/render/output comparisons against
exactly the same input. Replay timings are NOT live-network timings.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from PIL import Image

PUBLIC_HOSTS = {
    "api.fz.wiki",
    "api.warfarin.wiki",
    "data.akedata.wiki",
    "assets.fz.wiki",
    "static.warfarin.wiki",
    "bbs.hycdn.cn",
    "endfield.hypergryph.com",
    "web.hycdn.cn",
}


def request_key(url: str) -> str:
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    if parts.path.endswith("/manifest.json"):
        params = [(key, value) for key, value in params if key != "t"]
    canonical = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(sorted(params)), "")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class PublicHTTP:
    def __init__(self, mode: str, directory: Path):
        self.mode = mode
        self.directory = directory
        self.calls = 0
        self.downloaded_bytes = 0
        self.failures: list[str] = []
        self.rejected: list[str] = []

    def install(self):
        original = httpx.AsyncClient.send

        async def send(client, request, *args, **kwargs):
            parts = urlsplit(str(request.url))
            if request.method != "GET" or parts.hostname not in PUBLIC_HOSTS:
                self.rejected.append(
                    f"Non-public request: {request.method} {parts.hostname}"
                )
                raise RuntimeError(
                    f"Benchmark refuses non-public request: {request.method} {parts.hostname}"
                )
            if any(
                key.lower() in {"authorization", "cookie", "cred", "sign"}
                for key in request.headers
            ):
                self.rejected.append("Authenticated request")
                raise RuntimeError("Benchmark refuses authenticated requests")
            self.calls += 1
            key = request_key(str(request.url))
            metadata_path = self.directory / f"{key}.json"
            if self.mode == "replay":
                if not metadata_path.exists():
                    self.rejected.append(f"Missing public response cassette: {key}")
                    raise RuntimeError(
                        f"Missing public response cassette: {request.url}"
                    )
                meta = json.loads(metadata_path.read_text())
                if meta.get("transport_error"):
                    self.failures.append(meta["transport_error"])
                    raise httpx.ConnectError(meta["transport_error"], request=request)
                content = (self.directory / f"{key}.body").read_bytes()
                response = httpx.Response(
                    meta["status"],
                    content=content,
                    headers=meta["headers"],
                    request=request,
                )
            else:
                started = perf_counter()
                try:
                    response = await original(client, request, *args, **kwargs)
                    content = await response.aread()
                except httpx.HTTPError as exc:
                    reason = type(exc).__name__
                    self.failures.append(reason)
                    if self.mode == "record":
                        metadata_path.write_text(
                            json.dumps(
                                {"url": str(request.url), "transport_error": reason}
                            )
                        )
                    raise
                if self.mode == "record":
                    (self.directory / f"{key}.body").write_bytes(content)
                    metadata_path.write_text(
                        json.dumps(
                            {
                                "url": str(request.url),
                                "status": response.status_code,
                                "headers": {
                                    "content-type": response.headers.get(
                                        "content-type", ""
                                    )
                                },
                                "recorded_seconds": perf_counter() - started,
                            },
                            ensure_ascii=False,
                        )
                    )
            self.downloaded_bytes += len(content)
            if response.status_code >= 400:
                self.failures.append(f"HTTP {response.status_code}")
            return response

        return patch.object(httpx.AsyncClient, "send", send)


def signature(value):
    from dataclasses import asdict, is_dataclass

    if isinstance(value, bytes):
        with Image.open(io.BytesIO(value)) as image:
            pixels = image.convert("RGBA")
            return {
                "png_bytes": len(value),
                "png_sha256": hashlib.sha256(value).hexdigest(),
                "size": list(pixels.size),
                "rgba_sha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
            }
    if isinstance(value, (list, tuple)):
        return [signature(item) for item in value]
    if is_dataclass(value):
        value = asdict(value)
    return {
        "json_sha256": hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    }


async def benchmark(args):
    from loguru import logger

    logger.remove()
    import plugins.endfield.handlers as endfield
    from otae_bot.infrastructure.http import client as shared_http
    from otae_bot.infrastructure.rendering.browser import (
        close_browser,
        screenshot_web_element,
    )
    from otae_bot.infrastructure.rendering.executor import close_image_executor
    from plugins.endfield.catalog import commands
    from plugins.endfield.catalog.commands import ParsedEndfieldCommand
    from plugins.endfield.catalog.models import WeaponView
    from plugins.endfield.catalog.commands import EndfieldCandidate
    from plugins.endfield.catalog.service import EndfieldService

    http = PublicHTTP(args.mode, args.cassette)
    if args.mode == "record":
        args.cassette.mkdir(parents=True, exist_ok=True)

    async def finish_png(_matcher, png):
        return png

    async def no_prompt(*_args, **_kwargs):
        raise RuntimeError(
            "Benchmark query unexpectedly requires interactive selection"
        )

    class Matcher:
        async def finish(self, message):
            raise RuntimeError(f"Query did not produce its expected result: {message}")

    async def equipment_search():
        candidates = await endfield._collect_candidates("equipment", "长息轻护甲", "fz")
        if not any(
            item.kind == "equipment" and item.key == "装备/长息轻护甲"
            for item in candidates
        ):
            raise RuntimeError("Equipment lookup did not find the expected item")
        return candidates

    async def weapon_relations():
        view = WeaponView(
            "赤缨", "amaranthine-tassel", "武器/赤缨", weapon_id="wpn_claym_0017"
        )
        names = await endfield.service.find_weapon_operator_names(view)
        if not names:
            raise RuntimeError("Weapon relation lookup returned no operators")
        return names

    async def loadout():
        return await endfield._handle_loadout(
            Matcher(),
            ParsedEndfieldCommand(
                "loadout",
                query="莱万汀 熔铸火焰 长息轻护甲",
            ),
        )

    async def card(kind, key, source="fz"):
        pages = await endfield._render_candidate(
            EndfieldCandidate(kind, key, key, 100, source)
        )
        if not pages:
            raise RuntimeError("Card not found")
        return pages

    async def stage_card():
        catalog = await endfield.stage_service.get_catalog_view("akedata")
        item = next(
            item for group in catalog.groups for item in group.items if item.queryable
        )
        return await card("stage", item.stage_key, "akedata")

    async def medals():
        from dataclasses import asdict
        value = asdict(await endfield.service.fetch_medal_snapshot_akedata())
        value.pop("fetched_at", None)  # Local capture time is not source content.
        return value

    async def mixed():
        return await asyncio.gather(
            loadout(), weapon_relations(), medals(),
            card("stage_catalog", "", "akedata"),
        )

    cases = {
        "equipment_search": equipment_search,
        "weapon_relations": weapon_relations,
        "loadout": loadout,
        "operator_card": lambda: card("operator", "干员/莱万汀"),
        "weapon_card": lambda: card("weapon", "武器/熔铸火焰"),
        "equipment_card": lambda: card("equipment", "装备/长息轻护甲"),
        "operator_catalog": lambda: card("operator_catalog", ""),
        "weapon_catalog": lambda: card("weapon_catalog", ""),
        "equipment_catalog": lambda: card("equipment_catalog", ""),
        "stage_catalog": lambda: card("stage_catalog", "", "akedata"),
        "stage_card": stage_card,
        "calendar": endfield._render_current_version_calendar,
        "medals_data": medals,
        "mixed": mixed,
    }

    async def clear_derived():
        endfield.service = EndfieldService(endfield.client)
        endfield.stage_service = type(endfield.stage_service)(endfield.client)
        endfield.calendar_source = type(endfield.calendar_source)(endfield.client)
        for name in ("_CARD_CACHE", "_LOADOUT_CACHE", "_CALENDAR_CACHE"):
            cache = getattr(endfield, name, None)
            if cache is not None:
                await cache.clear()

    async def measure(fn, concurrency=1):
        before_calls, before_bytes = http.calls, http.downloaded_bytes
        before_failures = len(http.failures)
        before_rejected = len(http.rejected)
        started = perf_counter()
        values = await asyncio.wait_for(
            asyncio.gather(*(fn() for _ in range(concurrency))), args.timeout
        )
        seconds = perf_counter() - started
        if http.rejected[before_rejected:]:
            raise RuntimeError("; ".join(http.rejected[before_rejected:]))
        signatures = [signature(value) for value in values]
        return {
            "seconds": seconds,
            "http_calls": http.calls - before_calls,
            "response_bytes": http.downloaded_bytes - before_bytes,
            "http_failures": http.failures[before_failures:],
            "concurrency": concurrency,
            "outputs": signatures,
        }

    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "commit": args.commit,
        "dirty": args.dirty,
        "mode": args.mode,
        "network_delay_included": args.mode != "replay",
        "python": sys.version,
        "cold_runs": args.cold_runs,
        "warm_runs": args.warm_runs,
        "concurrent_runs": args.concurrent_runs,
        "cold_definition": "HTTP, domain, PNG and pinyin caches cleared; HTTP connections and browser may be reused.",
        "concurrent_definition": "Four identical queries, HTTP/pinyin warm, domain/PNG cold; batch completion time.",
        "cases": {},
        "note": "No QQ/Satori transport, account API, or interactive user wait is included. Browser is prestarted.",
        "volatile_fields_ignored": ["medals_data.fetched_at"],
        "mixed_definition": "loadout, weapon relations, medal tables and stage catalog started together",
    }
    try:
        with (
            http.install(),
            patch.object(endfield, "_finish_png", finish_png),
            patch.object(endfield, "prompt", no_prompt),
        ):
            if set(args.cases) - {
                "equipment_search",
                "weapon_relations",
                "medals_data",
            }:
                await screenshot_web_element(
                    "data:text/html,<body>benchmark warmup</body>", settle_ms=0
                )
            for name in args.cases:
                fn = cases[name]
                rows = report["cases"][name] = {
                    "cold": [],
                    "warm": [],
                    "concurrent": [],
                }
                try:
                    for _ in range(args.cold_runs):
                        await clear_derived()
                        await shared_http.clear_http_cache()
                        for cache_name in (
                            "_cached_search_keys",
                            "_cached_pinyin_syllables",
                        ):
                            cache = getattr(commands, cache_name, None)
                            if cache is not None:
                                cache.cache_clear()
                        rows["cold"].append(await measure(fn))
                        print(
                            json.dumps(
                                {
                                    "case": name,
                                    "phase": "cold",
                                    **{
                                        k: v
                                        for k, v in rows["cold"][-1].items()
                                        if k != "outputs"
                                    },
                                }
                            ),
                            flush=True,
                        )
                    for _ in range(args.warm_runs):
                        rows["warm"].append(await measure(fn))
                    for _ in range(args.concurrent_runs):
                        await clear_derived()
                        rows["concurrent"].append(await measure(fn, concurrency=4))
                    for phase in ("cold", "warm", "concurrent"):
                        values = [row["seconds"] for row in rows[phase]]
                        rows[f"{phase}_median_seconds"] = statistics.median(values)
                    print(
                        json.dumps(
                            {
                                "case": name,
                                "medians": {
                                    key: value
                                    for key, value in rows.items()
                                    if key.endswith("seconds")
                                },
                            }
                        ),
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001 -- preserve failures in the benchmark report
                    rows["error"] = f"{type(exc).__name__}: {exc}"
                    print(
                        json.dumps({"case": name, "error": rows["error"]}), flush=True
                    )
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await close_browser()
        await close_image_executor()
        await shared_http.close_http_client()
        await endfield.official_client.close()
    try:
        import resource
        report["python_peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        pass
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("live", "record", "replay"), default="live")
    parser.add_argument(
        "--cassette", type=Path, default=Path("output/endfield-performance/public-http")
    )
    parser.add_argument("--cases", default="equipment_search,weapon_relations,loadout")
    parser.add_argument("--cold-runs", type=int, default=3)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--concurrent-runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    args.repo, args.output, args.cassette = (
        args.repo.resolve(),
        args.output.resolve(),
        args.cassette.resolve(),
    )
    args.cases = args.cases.split(",")
    if set(args.cases) - {
        "equipment_search",
        "weapon_relations",
        "loadout",
        "operator_card",
        "weapon_card",
        "equipment_card",
        "operator_catalog",
        "weapon_catalog",
        "equipment_catalog",
        "stage_catalog",
        "stage_card",
        "calendar",
        "medals_data",
        "mixed",
    }:
        parser.error("Unknown benchmark case")
    if min(args.cold_runs, args.warm_runs, args.concurrent_runs, args.timeout) <= 0:
        parser.error("Run counts and timeout must be positive")
    args.commit = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    args.dirty = bool(
        subprocess.check_output(
            ["git", "-C", str(args.repo), "status", "--porcelain"], text=True
        ).strip()
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.repo))
    # Keep imports, temp screenshots, and runtime stores away from production data.
    with tempfile.TemporaryDirectory(prefix="endfield-query-bench-") as directory:
        previous = Path.cwd()
        os.chdir(directory)
        try:
            report = asyncio.run(benchmark(args))
        finally:
            os.chdir(previous)
    if any("error" in case for case in report["cases"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
