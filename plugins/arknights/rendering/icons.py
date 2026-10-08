"""Approved, bundled Skland reward artwork. No caller-controlled URL or path."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "rewards"
REWARD_ICON_IDS = {
    "龙门币": "4001",
    "合成玉": "4003",
    "初级作战记录": "2002",
    "中级作战记录": "2003",
    "高级作战记录": "2004",
    "技巧概要·卷2": "3302",
    "技巧概要·卷3": "3303",
    "固源岩组": "30013",
    "转质盐组": "31063",
    "褐素纤维": "31073",
    "环烃聚质": "31083",
    "医疗芯片": "3261",
}


@lru_cache(maxsize=12)
def _load_icon(resource_id: str) -> str:
    try:
        content = (ASSET_DIR / f"{resource_id}.png").read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def reward_icon_url(name: str) -> str:
    """Unknown rewards keep their text instead of borrowing another icon."""
    resource_id = REWARD_ICON_IDS.get(name)
    return _load_icon(resource_id) if resource_id else ""
