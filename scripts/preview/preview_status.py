"""渲染 ``/status`` 公开版与详细版预览：用本进程的真实采集，外加合成的今日指令、任务与告警。

    python scripts/preview/preview_status.py --output-dir output/status

不连 QQ、不发消息。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from otae_bot.command_usage.store import TodaySummary
from otae_bot.infrastructure.recent_alerts import Alert
from otae_bot.infrastructure.rendering.art_card import render_card
from otae_bot.infrastructure.rendering.browser import close_browser
from plugins.status.collect import Job, collect
from plugins.status.presentation import build_card

HOURLY = [18, 9, 4, 2, 2, 3, 22, 31, 38, 35, 42, 55, 61, 48, 44, 52, 58, 66, 74, 88, 100, 63, 0, 0]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/status")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, detail in (("public", False), ("detail", True)):
            snapshot = await collect(detail=detail)
            now = snapshot.now
            snapshot.started_at = now - timedelta(days=3, hours=7)
            snapshot.accounts = [("qq", "3940000000", 12)]
            # 预览进程没有真正加载插件，按「只有叉出去没加载」显示。
            snapshot.plugins = [(name, name != "forkout") for name, _ in snapshot.plugins]
            snapshot.loop_lag = {"last": 0.004, "max": 0.031}
            snapshot.today = TodaySummary(1284, 1146, 3, 1800.0, HOURLY, [("/ef 持有率", 214), ("/q", 187), ("/ef 抽卡", 162), ("/radar", 98)])
            snapshot.render_timing = {"count": 412, "failures": 1, "mean": 1.8, "p95": 4.6}
            if detail:
                snapshot.jobs = [
                    Job("Tibo 雷达 · refresh", 600, now - timedelta(minutes=18), False, "HTTPStatusError: 502 Bad Gateway", 3, False),
                    Job("B站 · poll_dynamics", 120, now - timedelta(minutes=2), True, "", 0, False),
                    Job("终末地 · announcement_tick", 300, now - timedelta(minutes=5), True, "", 0, False),
                    Job("终末地 · daily_attendance", 86400, now.replace(hour=6, minute=0), True, "", 0, False),
                    Job("终末地 · usage_snapshot", 600, None, None, "", 0, True),
                ]
                snapshot.alerts = [
                    Alert(now - timedelta(minutes=18), "WARNING", "plugins.tibo_radar", "Tibo 雷达上游返回 502，10 分钟后重试"),
                    Alert(now - timedelta(hours=2), "ERROR", "watchdog", "[watchdog] event loop blocked 1.240s at 13:07:22.101"),
                    Alert(now - timedelta(hours=3), "WARNING", "plugins.endfield", "[endfield] FZ Wiki resolver failed for weapon 提丰（#38）"),
                ]
            png = await render_card(build_card(snapshot, detail=detail))
            path = args.output_dir / f"{name}.png"
            path.write_bytes(png)
            print(f"{path.name}  {len(png)} 字节")
    finally:
        await close_browser()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
