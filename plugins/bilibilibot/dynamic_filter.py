"""Content policy for the Endfield official dynamic subscription, without I/O."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from .models import KIND_DYNAMIC, BiliCard

ENDFIELD_OFFICIAL_UID = "1265652806"

# Rewards earned in-game, banners and incidental reservation prizes are not
# social giveaways. Avoid standalone words such as 奖励、福利、免费、寻访.
LOTTERY_WORDS = re.compile(r"抽[奖獎]|开[奖獎]|開獎|中[奖獎]|兑[奖獎]|兌獎")
DRAW_PEOPLE = re.compile(
    r"(?:抽取|抽出|抽选|抽選|随机选出|隨機選出|随机挑选|隨機挑選)"
    r"[^。！？!?]{0,50}(?:位|名)(?:[^。！？!?]{0,25})"
    r"(?:送出|赠送|贈送|获得|獲得|奖励|獎勵|管理员|管理員|玩家|同学|同學|幸运|幸運)"
)
FORWARD_PRIZE = re.compile(
    r"(?:转发|轉發|评论|評論|关注|關注|点赞|點讚)(?:有奖|有獎|抽|赢|贏)"
)
LOTTERY_URL = re.compile(r"(?:bilibili\.com|b23\.tv)/[^\s]*lottery", re.IGNORECASE)


def _normalized(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", html.unescape(text))
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def text_lottery_reason(text: str) -> str:
    text = _normalized(text)
    if LOTTERY_WORDS.search(text):
        return "lottery-text"
    if DRAW_PEOPLE.search(text) or FORWARD_PRIZE.search(text):
        return "giveaway-text"
    if LOTTERY_URL.search(text):
        return "lottery-link"
    return ""


def _content_values(value: Any):
    """Inspect content fields only, never author names, stats or UI actions."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "text",
                "orig_text",
                "title",
                "desc",
                "description",
                "jump_url",
                "url",
                "type",
            } and isinstance(child, str):
                yield child
            elif isinstance(child, (dict, list)):
                yield from _content_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _content_values(child)


def dynamic_lottery_reason(item: dict[str, Any]) -> str:
    """Include forwarded originals; reservation widget prize text is excluded.

    The user's allowed livestream example contains both a lottery URL and an
    '已参与抽奖' button toast inside additional.reserve. Those are UI metadata,
    not a giveaway announcement. The main post and its originals still count.
    """
    visited: set[int] = set()
    while isinstance(item, dict) and id(item) not in visited:
        visited.add(id(item))
        dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
        additional = dynamic.get("additional") or {}
        content = [dynamic.get("desc"), dynamic.get("major")]
        if additional.get("type") != "ADDITIONAL_TYPE_RESERVE":
            content.append(additional)
        for value in _content_values(content):
            if "LOTTERY" in value:
                return "lottery-node"
            reason = text_lottery_reason(value)
            if reason:
                return reason
        item = item.get("orig")
    return ""


def suppress_dynamic(card: BiliCard, *, uid: str | None = None) -> bool:
    return (
        (card.uid if uid is None else uid) == ENDFIELD_OFFICIAL_UID
        and card.card_type == KIND_DYNAMIC
        and bool(
            card.lottery_reason
            or text_lottery_reason(card.title + "\n" + card.description)
        )
    )
