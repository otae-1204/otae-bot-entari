"""``/统计`` 的参数：``全局``、天数（``30天`` / ``30d`` / ``30``）与插件名，顺序随意。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from otae_bot.groups.features import resolve_plugin

DEFAULT_DAYS = 7
MAX_DAYS = 90
GLOBAL_WORDS = {"全局", "全部", "global", "all"}
DAYS_PATTERN = re.compile(r"(\d{1,3})\s*(?:天|日|d|days?)?", re.IGNORECASE)


@dataclass(frozen=True)
class StatsQuery:
    global_scope: bool = False
    days: int = DEFAULT_DAYS
    plugin: str = ""
    error: str = ""


def parse_query(text: str, plugins: list[str]) -> StatsQuery:
    global_scope, days, plugin = False, DEFAULT_DAYS, ""
    for word in text.split():
        if word.casefold() in GLOBAL_WORDS:
            global_scope = True
            continue
        if match := DAYS_PATTERN.fullmatch(word):
            days = int(match.group(1))
            if not 1 <= days <= MAX_DAYS:
                return StatsQuery(error=f"天数要在 1 到 {MAX_DAYS} 之间，例如 /统计 30天。")
            continue
        if resolved := resolve_plugin(word, plugins):
            plugin = resolved
            continue
        return StatsQuery(
            error=f"看不懂「{word}」。可以这样用：/统计、/统计 全局、/统计 30天、/统计 终末地，参数可以组合。"
        )
    return StatsQuery(global_scope, days, plugin)
