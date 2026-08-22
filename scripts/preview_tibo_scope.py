"""Render previews of every tibo_radar reply card in the draw_scope redesign.

Usage: python scripts/preview_tibo_scope.py
Output (output/):
  tibo_scope_overview.png — /tibo 总览
  tibo_scope_status.png   — /tibo 状态
  tibo_scope_recent.png   — /tibo 最近
  tibo_scope_history.png  — /tibo 历史
  tibo_scope_feed3.png    — /tibo 动态（3 条单图版）
  tibo_scope_feed6.png    — /tibo 动态（6 条）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.tibo_radar.draw_scope import event_sections, render_card, render_scope
from scripts.preview_tibo_x_all import _overview_sections, _sample_events, _status_sections
from scripts.preview_tibo_xfeed import _sample_posts


def _relevance_label(value: str) -> str:
    return {"direct": "直接相关", "indirect": "间接相关", "none": "无重置信号"}.get(value, value)


def _event_label(value: str) -> str:
    return {
        "confirmed": "已确认完成",
        "expected_window": "预计时间窗口",
        "rejected": "预告未兑现/已否定",
    }.get(value, value)


async def main() -> None:
    out = Path("output")
    out.mkdir(parents=True, exist_ok=True)
    posts = _sample_posts()
    events = _sample_events()
    jobs = [
        ("tibo_scope_overview.png", render_card(
            "Tibo 雷达总览",
            "本地缓存查询 · 最近采集周期 10 分钟",
            _overview_sections(),
        )),
        ("tibo_scope_status.png", render_card(
            "Tibo 重置状态",
            "只区分预告、窗口、疑似和已确认，不把预测写成事实",
            _status_sections(),
        )),
        ("tibo_scope_recent.png", render_card(
            "最近一次已确认重置",
            "只显示可核验完成事件",
            event_sections(events[:1], lambda value: "已确认完成"),
        )),
        ("tibo_scope_history.png", render_card(
            "Tibo 重置历史",
            "预告、窗口、疑似与完成状态分开标注",
            event_sections(events, _event_label),
            page="1/1",
        )),
        ("tibo_scope_feed3.png", render_scope(
            posts[:3],
            _relevance_label,
            title="Tibo 最新 X 动态",
            subtitle="最近 3 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
        )),
        ("tibo_scope_feed6.png", render_scope(
            posts[:6],
            _relevance_label,
            title="Tibo 最新 X 动态",
            subtitle="最近 6 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
            page="1 / 2",
        )),
    ]
    for name, task in jobs:
        png = await task
        (out / name).write_bytes(png)
        print(f"saved {out / name} ({len(png)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
