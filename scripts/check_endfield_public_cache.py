"""Read-only public-image restart/revalidation probe; no account credentials.

Each phase runs in a new process against a disposable cache directory. The last
phase ages only that temporary cache to exercise actual upstream validators.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

PUBLIC_IMAGE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/"
    "gameplay/ui/sprites/charremoteicon/icon_chr_0005_chen.png"
)


async def probe(path):
    import httpx
    from loguru import logger
    from otae_bot.infrastructure.http import client
    from otae_bot.infrastructure.http.disk import PublicImageDiskCache

    logger.remove()
    client.public_images = PublicImageDiskCache(path, 4 * 1024 * 1024)
    statuses = []
    original = httpx.AsyncClient.send

    async def send(self, request, *args, **kwargs):
        response = await original(self, request, *args, **kwargs)
        statuses.append(response.status_code)
        return response

    try:
        with patch.object(httpx.AsyncClient, "send", send):
            started = perf_counter()
            resource = await client.fetch_bytes(
                PUBLIC_IMAGE, namespace="endfield-assets"
            )
            elapsed = perf_counter() - started
        return {
            "seconds": elapsed,
            "http_statuses": statuses,
            "bytes": len(resource.content),
            "sha256": hashlib.sha256(resource.content).hexdigest(),
        }
    finally:
        await client.close_http_client()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-cache", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.child_cache:
        print(json.dumps(asyncio.run(probe(args.child_cache))))
        return
    if args.output is None:
        parser.error("--output is required")
    results = {}
    with tempfile.TemporaryDirectory(
        prefix="endfield-public-cache-probe-"
    ) as directory:
        path = Path(directory) / "images.sqlite3"
        for phase in ("cold", "new_process_fresh_disk", "new_process_forced_expiry"):
            if phase == "new_process_forced_expiry":
                with sqlite3.connect(path) as connection:
                    connection.execute("UPDATE public_images_v1 SET validated_at=0")
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child-cache",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            results[phase] = json.loads(result.stdout.splitlines()[-1])
    report = {
        "public_resource": PUBLIC_IMAGE,
        "cache_expiry_forced_in_temporary_database": True,
        "results": results,
        "identical_bodies": len({item["sha256"] for item in results.values()}) == 1,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
