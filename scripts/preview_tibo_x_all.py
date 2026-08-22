"""Render previews of every tibo_radar reply card in the X design (draw_x).

Usage: python scripts/preview_tibo_x_all.py
Output:
  output/tibo_x_overview.png  — /tibo 总览
  output/tibo_x_status.png    — /tibo 状态
  output/tibo_x_recent.png    — /tibo 最近
  output/tibo_x_history.png   — /tibo 历史
  output/tibo_x_feed.png      — /tibo 动态
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.tibo_radar.draw_x import AMBER, CYAN, GREEN, RED, CardSection, event_sections, render_card, render_xfeed
from plugins.tibo_radar.models import EVENT_CONFIRMED, EVENT_EXPECTED_WINDOW, EVENT_REJECTED, ResetEvent

from scripts.preview_tibo_xfeed import _sample_posts


def _feed_section_lines() -> list[str]:
    labels = {"direct": "直接相关", "indirect": "间接相关", "none": "无重置信号"}

    def ellipsize(text: str, limit: int = 140) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    lines: list[str] = []
    for post in _sample_posts()[:3]:
        when = post.source_time.astimezone().strftime("%m-%d %H:%M") if post.source_time else "未知"
        lines.append(f"{labels.get(post.relevance, post.relevance)} · {when} · {post.post_id}")
        lines.append(f"原文：{ellipsize(post.text)}")
        if post.translation:
            lines.append(f"翻译：{ellipsize(post.translation)}")
        if post.analysis:
            lines.append(f"解读：{ellipsize(post.analysis)}")
    return lines


def _overview_sections() -> list[CardSection]:
    return [
        CardSection(
            "当前雷达状态",
            [
                "官方重置预告窗口进行中",
                "codexradar 检测到官方预告仍处于声明的时间窗口内，等待完成核验。",
                "证据：evt-20260819 · https://x.com/thsottiaux/status/1234567890",
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
            _feed_section_lines(),
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


def _sample_events() -> list[ResetEvent]:
    return [
        ResetEvent(
            event_id="evt-20260819",
            summary="It is done.",
            localized_summary="完成了。",
            url="https://x.com/thsottiaux/status/1887123456789012345",
            announced_at=datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc),
            effective_at=datetime(2026, 8, 19, 3, 12, tzinfo=timezone.utc),
            status=EVENT_CONFIRMED,
            source_label="codexradar 公开核验",
        ),
        ResetEvent(
            event_id="evt-20260812",
            summary="Landing within an hour.",
            localized_summary="一小时内到达。",
            url="https://x.com/thsottiaux/status/1886000000000000000",
            announced_at=datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc),
            window_start=datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc),
            status=EVENT_EXPECTED_WINDOW,
            preview=True,
            source_label="codex-reset timeline",
        ),
        ResetEvent(
            event_id="evt-20260805",
            summary="Reset scheduled for tonight.",
            localized_summary="今晚安排重置。",
            url="https://x.com/thsottiaux/status/1885000000000000000",
            announced_at=datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc),
            status=EVENT_REJECTED,
            source_label="codex-reset timeline",
        ),
    ]


def _status_sections() -> list[CardSection]:
    return [
        CardSection(
            "当前状态",
            [
                "官方重置预告窗口进行中",
                "codexradar 检测到官方预告仍处于声明的时间窗口内，等待完成核验。",
                "事件：evt-20260819",
                "证据原文：一小时内到达。",
            ],
            GREEN,
        )
    ]


def _recent_sections() -> list[CardSection]:
    return event_sections([_sample_events()[0]], lambda value: "已确认完成")


async def main() -> None:
    out = Path("output")
    out.mkdir(parents=True, exist_ok=True)
    jobs = [
        (
            "tibo_x_overview.png",
            render_card("Tibo 雷达总览", "本地缓存查询 · 最近采集周期 10 分钟", _overview_sections()),
        ),
        (
            "tibo_x_status.png",
            render_card("Tibo 重置状态", "只区分预告、窗口、疑似和已确认，不把预测写成事实", _status_sections()),
        ),
        (
            "tibo_x_recent.png",
            render_card("最近一次已确认重置", "只显示可核验完成事件", _recent_sections()),
        ),
        (
            "tibo_x_history.png",
            render_card("Tibo 重置历史", "预告、窗口、疑似与完成状态分开标注", event_sections(_sample_events(), lambda value: {"confirmed": "已确认完成", "expected_window": "预计时间窗口", "rejected": "预告未兑现/已否定"}.get(value, value)), page="1/1"),
        ),
        (
            "tibo_x_feed.png",
            render_xfeed(
                _sample_posts()[:3],
                lambda value: {"direct": "直接相关", "indirect": "间接相关", "none": "无重置信号"}.get(value, value),
                title="Tibo 最新 X 动态",
                subtitle="最近 3 条 · 英文原文 + 中文翻译 + 模型解读 · 北京时间倒序",
                page="1/2",
            ),
        ),
    ]
    for name, png in jobs:
        data = await png
        (out / name).write_bytes(data)
        print(f"saved {out / name} ({len(data)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
