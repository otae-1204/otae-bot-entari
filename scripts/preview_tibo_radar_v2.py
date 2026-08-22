"""Render a preview card with the v2 renderer (draw_v2) using sample data.

Usage: python scripts/preview_tibo_radar_v2.py
Output: output/tibo_radar_v2_preview.png
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.tibo_radar.draw_v2 import AMBER, CYAN, GREEN, CardSection, render_card


def _sample_sections() -> list[CardSection]:
    return [
        CardSection(
            "当前雷达状态",
            [
                "官方重置预告窗口进行中",
                "codexradar 检测到官方预告仍处于声明的时间窗口内，等待完成核验。",
                "证据：evt-20260819 · https://x.com/tiborockss/status/1234567890",
            ],
            GREEN,
        ),
        CardSection(
            "最近一次已确认重置",
            [
                "时间：2026-08-14 03:12:00 -0700",
                "距今：7 天 2 小时",
                "证据：Usage limits have been reset for all accounts.",
                "确认：codexradar 公开核验",
            ],
            GREEN,
        ),
        CardSection(
            "Tibo 所在 PT 时区",
            ["2026-08-20 20:35:54 PDT · 可能在线（粗粒度公开推测）", "仅用于公开活动时段的粗粒度展示，不代表实时在线"],
            AMBER,
        ),
        CardSection(
            "历史统计",
            [
                "已确认样本：6 条",
                "最近间隔：97 小时",
                "北京时间小时分布：03:00(2)、09:00(1)、15:00(2)、21:00(1)",
            ],
            CYAN,
        ),
        CardSection(
            "最新相关动态",
            [
                "直接相关 · 08-20 14:22 · tiborockss-1098",
                "Working on the weekly reset pipeline, should land before Friday. 附带链接 https://codex-reset.com/tibo 供参考",
                "间接相关 · 08-19 23:05 · codexradar-204",
                "Telemetry shows elevated 429s across Pro workspaces this evening.",
            ],
            AMBER,
        ),
        CardSection(
            "来源健康",
            [
                "codexradar.com：正常 · 12 分钟前",
                "codex-reset.com feed：正常 · 12 分钟前",
                "codex-reset.com timeline：陈旧 · 2 小时前",
            ],
            CYAN,
        ),
    ]


async def main() -> None:
    png = await render_card(
        "Tibo 雷达总览",
        "本地缓存查询 · 最近采集周期 10 分钟",
        _sample_sections(),
    )
    out = Path("output/tibo_radar_v2_preview.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"saved {out} ({len(png)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
