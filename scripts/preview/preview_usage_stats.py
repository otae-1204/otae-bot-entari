"""渲染 ``/指令统计`` 卡片预览：往临时库灌入合成调用记录，走线上同一条渲染路径。

    python scripts/preview/preview_usage_stats.py                 # 本群 7 天、全局 30 天、单插件三张
    python scripts/preview/preview_usage_stats.py --output-dir output/usage

不连 QQ、不发消息，也不碰 data/ 下的真实统计库。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from otae_bot.command_usage.store import CallRecord, Scope, UsageStore
from otae_bot.infrastructure.rendering.art_card import render_card
from otae_bot.infrastructure.rendering.browser import close_browser
from plugins.usage_stats.presentation import build_card

COMMANDS = [  # (指令, 子命令, 插件, 权重, 平均耗时 ms)
    ("/ef", "持有率", "endfield", 120, 2100), ("/q", "", "hyw", 95, 8400), ("/ef", "抽卡", "endfield", 85, 3600),
    ("/ef", "影拓", "endfield", 64, 2400), ("/radar", "", "radar", 50, 1200), ("/ak", "签到", "arknights", 45, 4000),
    ("/help", "", "help_plugin", 38, 300), ("/grok", "", "grok_bot", 34, 11200), ("/tibo", "", "tibo_radar", 26, 900),
    ("/更新日志", "", "changelog", 16, 400), ("/ef", "资料查询", "endfield", 70, 1800), ("/bili", "", "bilibilibot", 12, 700),
]
NAMES = ["夜岚", "晨星", "小佩丽卡", "不吃香菜", "Shenger", "惠", "路过的博士", "管理员", "Ash", "白昼"]
HOURLY = [3, 1, 1, 0, 0, 0, 1, 3, 5, 6, 6, 8, 9, 7, 6, 7, 8, 9, 10, 12, 14, 13, 10, 6]


def seed(store: UsageStore, now: datetime, days: int = 40) -> None:
    rng = random.Random(20261027)
    weights = [weight for *_, weight, _ in COMMANDS]
    records = []
    for back in range(days):
        day = now - timedelta(days=back)
        volume = int(rng.uniform(80, 160) * (1.3 if day.weekday() >= 5 else 1))
        for _ in range(volume):
            hour = rng.choices(range(24), HOURLY)[0]
            moment = day.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))
            if moment > now:
                continue
            command, sub, plugin, _, cost = rng.choices(COMMANDS, weights)[0]
            user = rng.choices(range(len(NAMES)), [12, 10, 8, 7, 6, 5, 3, 2, 2, 1])[0]
            group = "100" if rng.random() < 0.7 else "101"
            records.append(CallRecord(
                moment.timestamp(), "qq", group, str(1000 + user), plugin, command, sub,
                int(rng.gauss(cost, cost / 4)), rng.random() > 0.008, NAMES[user], f"{NAMES[user]}的QQ",
            ))
    store.insert_many(records)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/usage-stats")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    with tempfile.TemporaryDirectory() as directory:
        store = UsageStore(Path(directory) / "usage.sqlite3")
        seed(store, now)
        cases = {
            "group-7d": (Scope("qq", "100"), 7),
            "global-30d": (Scope(), 30),
            "endfield-7d": (Scope("qq", "100", plugin="endfield"), 7),
            "empty-1d": (Scope("qq", "999"), 1),
        }
        try:
            for name, (scope, days) in cases.items():
                png = await render_card(build_card(store.report(scope, days, now), days, now))
                path = args.output_dir / f"{name}.png"
                path.write_bytes(png)
                print(f"{path.name}  {len(png)} 字节")
        finally:
            store.close()
            await close_browser()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
