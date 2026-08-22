"""Render a preview of the X-feed style reply card (draw_xfeed) with sample data.

Usage: python scripts/preview_tibo_xfeed.py
Output: output/tibo_xfeed_preview.png
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.tibo_radar.draw_xfeed import render_xfeed
from plugins.tibo_radar.models import TiboPost


def _sample_posts() -> list[TiboPost]:
    def post(pid: str, text: str, translation: str, analysis: str, relevance: str, at: str, phrases: str = "") -> TiboPost:
        return TiboPost(
            post_id=pid,
            text=text,
            url=f"https://x.com/thsottiaux/status/{pid}",
            source_time=datetime.fromisoformat(at),
            translation=translation,
            analysis=analysis,
            relevance=relevance,
            phrases=phrases,
        )

    return [
        post(
            "1887123456789012345",
            "Usage limits have been reset for all accounts. The weekly reset pipeline is back on track — see you all next week.",
            "所有账户的用量额度已重置。每周重置流水线已恢复正常——下周见。",
            "这是完成确认：额度已对全部账户重置，属于直接相关信号，可视为本轮周期完成。",
            "direct",
            "2026-08-21T11:00:00+08:00",
            "usage limits reset, weekly reset pipeline",
        ),
        post(
            "1886943200000000001",
            "Working on the weekly reset pipeline, should land before Friday.",
            "正在推进每周重置流水线，应该会在周五之前上线。",
            "预告类内容：作者自述正在开发流水线并给出时间预期，未确认已完成，只能算间接相关。",
            "indirect",
            "2026-08-20T14:22:00+08:00",
            "weekly reset pipeline, before Friday",
        ),
        post(
            "1886801200000000002",
            "Telemetry shows elevated 429s across Pro workspaces this evening — likely the usual crowd before a reset.",
            "遥测数据显示今晚 Pro 工作区的 429 错误明显上升——很可能是重置前常见的流量高峰。",
            "观察类内容：429 上升是重置前兆的旁证，不构成直接证据，置信度有限。",
            "indirect",
            "2026-08-19T23:05:00+08:00",
            "429s, elevated traffic",
        ),
        post(
            "1886655000000000003",
            "A small heads-up: we're rolling out a better reset status page. You'll be able to see the window in real time.",
            "小提醒：我们正在推出更好的重置状态页，你可以实时看到窗口。",
            "功能预告：与重置观测相关，但并非重置本身，属于间接信息。",
            "indirect",
            "2026-08-18T09:40:00+08:00",
            "reset status page, real time",
        ),
        post(
            "1886510000000000004",
            "Zero Data Retention preview is live for a few workspaces. Feedback welcome!",
            "零数据保留预览已对部分工作区开放。欢迎反馈！",
            "产品动态：与额度/重置机制弱相关，不指向具体重置行为。",
            "indirect",
            "2026-08-17T16:10:00+08:00",
            "",
        ),
        post(
            "1886370000000000005",
            "Just pushed a fix for the reset timer edge case. Thanks for the reports!",
            "刚推送了重置计时器边界情况的修复。感谢大家反馈！",
            "",
            "indirect",
            "2026-08-16T02:30:00+08:00",
            "reset timer fix",
        ),
    ]


async def main() -> None:
    png = await render_xfeed(
        _sample_posts(),
        lambda value: {"direct": "直接相关", "indirect": "间接相关", "none": "无重置信号"}.get(value, value),
        title="Tibo 最新 X 动态",
        subtitle="最近 6 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
    )
    out = Path("output/tibo_xfeed_preview.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"saved {out} ({len(png)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
