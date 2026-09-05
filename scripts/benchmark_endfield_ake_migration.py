"""Live public-only AKE/FZ query comparison, with isolated disposable caches."""

from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import resource
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark_endfield_queries import PublicHTTP, signature  # noqa: E402


async def main(args):
    from loguru import logger
    from plugins.endfield import handlers as end
    from otae_bot.infrastructure.http import client as http
    from otae_bot.infrastructure.http.disk import PublicImageDiskCache
    from otae_bot.infrastructure.rendering.browser import (
        close_browser,
        screenshot_web_element,
    )
    from otae_bot.infrastructure.rendering.executor import close_image_executor
    from plugins.endfield.catalog.commands import choose_candidate

    logger.remove()
    recorder = PublicHTTP("record", args.output / "public-responses")
    recorder.directory.mkdir(parents=True, exist_ok=True)
    report = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "mode": "live public HTTP",
        "note": "No account credentials, bot startup/restart, QQ transport or human interaction. Browser prestarted. Each source/case uses isolated cold caches; disk_restart clears all RAM/domain/PNG state but retains SQLite (not OS page cache).",
        "cases": {},
    }

    async def reset(*, disk=False, data=True):
        await end._CARD_CACHE.clear()
        await end._LOADOUT_CACHE.clear()
        if data:
            await end.service.clear_query_caches()
            await http.clear_http_cache(include_disk=disk)

    async def query(kind, name, source):
        started = perf_counter()
        candidates = await end._collect_candidates(kind, name, source)
        candidate, _ = choose_candidate(candidates)
        if candidate is None:
            raise RuntimeError("No unambiguous candidate")
        lookup = perf_counter() - started
        pages = await end._render_candidate(candidate, source)
        if not pages:
            raise RuntimeError("No pages")
        return {
            "lookup_seconds": lookup,
            "source": candidate.source,
            "revision": candidate.revision,
            "pages": pages,
        }

    async def measure(fn):
        before, size = recorder.calls, recorder.downloaded_bytes
        started = perf_counter()
        value = await asyncio.wait_for(fn(), 180)
        elapsed = perf_counter() - started
        for page in value["pages"]:
            target = args.output / f"page-{hashlib.sha256(page).hexdigest()[:16]}.png"
            if not target.exists():
                target.write_bytes(page)
        value["pages"] = signature(
            value["pages"]
        )  # Verification is outside query timing.
        return {
            "seconds": elapsed,
            "http_calls": recorder.calls - before,
            "http_bytes": recorder.downloaded_bytes - size,
            "result": value,
        }

    def save():
        report["peak_python_rss_mib"] = (
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        )
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
        )

    async def mixed_metadata():
        from plugins.endfield.providers.repository import query_snapshot

        async with query_snapshot() as data:
            values = await asyncio.gather(
                end.service.get_operator_view_from_akedata("提弗洛斯"),
                end.service.get_operator_view_from_akedata("莱万汀"),
                end.service.get_weapon_view_from_akedata("熔铸火焰"),
                end.service.get_equipment_view_from_akedata("长息轻护甲"),
                end.service.get_operator_catalog_view(source="akedata"),
                end.service.get_weapon_catalog_view(source="akedata"),
                end.service.get_equipment_catalog_view(
                    include_details=False, source="akedata"
                ),
                end.service.get_equipment_catalog_view("长息", source="akedata"),
                end.service.get_ake_pool_metadata(),
                end.service.get_ake_items(["item_charpotentialup_chr_0016_laevat"]),
            )
            return {
                "revision": data.revision,
                "operations": len(values),
                "sources": sorted({view.source_name for view in values[:8]}),
                "operator_catalog": sum(
                    len(p.items) for e in values[4].elements for p in e.professions
                ),
                "weapon_catalog": sum(len(g.items) for g in values[5].groups),
                "equipment_catalog": sum(len(g.items) for g in values[6].groups),
                "pools": len(values[8]),
                "keepsakes": len(values[9]),
            }

    with tempfile.TemporaryDirectory(prefix="endfield-ake-bench-") as directory:
        directory = Path(directory)
        with (
            recorder.install(),
            patch.object(
                http,
                "public_images",
                PublicImageDiskCache(directory / "images.sqlite3", 256 * 1024 * 1024),
            ),
            patch.object(
                http,
                "public_tables",
                PublicImageDiskCache(directory / "tables.sqlite3", 256 * 1024 * 1024),
            ),
        ):
            try:
                if args.metadata_only:
                    report["note"] = (
                        "Public data-only mixed workload: ten concurrent operations, no browser or images, no account APIs. disk_restart clears Python caches/connections, not the process or OS page cache."
                    )
                    await reset(disk=True)
                    for phase in ("cold", "warm1", "warm2", "warm3", "disk_restart"):
                        if phase == "disk_restart":
                            await reset()
                            await http.close_http_client()
                        before, size = recorder.calls, recorder.downloaded_bytes
                        started = perf_counter()
                        result = await asyncio.wait_for(mixed_metadata(), 180)
                        report["cases"][phase] = {
                            "seconds": perf_counter() - started,
                            "http_calls": recorder.calls - before,
                            "http_bytes": recorder.downloaded_bytes - size,
                            "result": result,
                            "dto_cache": asdict(await end.service._ake_views.stats()),
                            "http_cache": asdict(await http.get_http_cache_stats()),
                        }
                        save()
                        print(
                            json.dumps(
                                {"phase": phase, **report["cases"][phase]},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    return
                await screenshot_web_element(
                    "data:text/html,<body>benchmark warmup</body>", settle_ms=0
                )
                for kind, name in (
                    ("operator", "提弗洛斯"),
                    ("operator", "莱万汀"),
                    ("weapon", "熔铸火焰"),
                    ("equipment", "长息轻护甲"),
                ):
                    if args.only and name not in args.only.split(","):
                        continue
                    for source in args.sources.split(","):
                        rows = report["cases"][f"{kind}:{name}:{source}"] = {}
                        try:
                            await reset(disk=True)
                            fn = lambda: query(kind, name, source)
                            for phase in (
                                "cold",
                                "png_hit",
                                "data_warm_png_cold",
                                "disk_restart",
                            ):
                                if phase == "data_warm_png_cold":
                                    await reset(data=False)
                                elif phase == "disk_restart":
                                    await reset()
                                    await http.close_http_client()
                                rows[phase] = await measure(fn)
                                print(
                                    json.dumps(
                                        {
                                            "case": name,
                                            "source": source,
                                            "phase": phase,
                                            **rows[phase],
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                save()
                        except Exception as exc:
                            rows["error"] = f"{type(exc).__name__}: {exc}"
                            print(
                                json.dumps(
                                    {
                                        "case": name,
                                        "source": source,
                                        "error": rows["error"],
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            save()
                # Compare numeric input/output fields separately from layout timing.
                comparison = report["loadout_comparison"] = {}
                for source in args.sources.split(","):
                    try:
                        started = perf_counter()
                        view = await end.service.get_loadout_view(
                            "干员/莱万汀",
                            "武器/熔铸火焰",
                            [("装备/长息轻护甲", 3, ())],
                            source=source,
                        )
                        comparison[source] = {
                            "seconds": perf_counter() - started,
                            "view": asdict(view),
                        }
                    except Exception as exc:
                        comparison[source] = {"error": f"{type(exc).__name__}: {exc}"}
                save()
            finally:
                await end.service._ake_views.close()
                await close_browser()
                await close_image_executor()
                await http.close_http_client()
                await end.official_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/endfield-ake-migration/live")
    )
    parser.add_argument("--sources", default="fz,akedata")
    parser.add_argument("--only", default="")
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args))
