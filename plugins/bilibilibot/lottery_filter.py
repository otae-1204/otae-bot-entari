"""社交抽奖动态的过滤规则。

B 站账号会把社交抽奖混进动态流里，这类内容不是游戏资讯，动态订阅应当跳过。
判定完全本地、确定性，只读取 B 站标记的结构化节点类型、正文与转发原文，
不调用模型，也不做图片 OCR。

默认对所有账号生效，账号列表由 entari.yml 的 plugins.bilibilibot.lottery_filter_uids 配置：逗号分隔的 UID
收窄到指定账号，``*`` 或 ``all`` 表示全部账号，显式留空表示关闭过滤。
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from .config import bili_settings
from .models import KIND_DYNAMIC, BiliCard

# 写成这个值表示所有账号都过滤。
ALL_ACCOUNTS = "*"
ALL_ACCOUNTS_ALIASES = frozenset({ALL_ACCOUNTS, "all"})
# 未配置时对所有账号生效：抽奖不是资讯，默认就不推送。
DEFAULT_FILTER_UIDS = (ALL_ACCOUNTS,)

# 只认社交抽奖。游戏内奖励、卡池与预约附赠都是正常内容，因此「奖励」「福利」
# 「免费」「寻访」这类单独出现的词一律不触发。
LOTTERY_WORDS = re.compile(r"抽[奖獎]|开[奖獎]|開獎|中[奖獎]|兑[奖獎]|兌獎")
DRAW_PEOPLE = re.compile(
    r"(?:抽取|抽出|抽选|抽選|随机选出|隨機選出|随机挑选|隨機挑選)"
    r"[^。！？!?]{0,50}(?:位|名)[^。！？!?]{0,25}"
    r"(?:送出|赠送|贈送|获得|獲得|奖励|獎勵|管理员|管理員|玩家|同学|同學|幸运|幸運)"
)
PRIZE_WORDS = re.compile(
    r"(?:抽取|抽出)[^。！？!?\n]{0,80}(?:周边|周邊|京东卡|京東卡|iPad|手机|手機|手柄)"
)
FORWARD_PRIZE = re.compile(r"(?:转发|轉發|评论|評論|关注|關注|点赞|點讚)(?:有奖|有獎|抽|赢|贏)")
LOTTERY_URL = re.compile(r"(?:bilibili\.com|b23\.tv)/\S*lottery", re.IGNORECASE)

# 只有这些键承载正文；作者昵称、计数与界面动作不参与判定。
_CONTENT_KEYS = frozenset(
    {"text", "orig_text", "title", "desc", "description", "jump_url", "url", "type"}
)
# B 站给抽奖节点标记的类型；节点可见文字不一定写作「抽奖」。
_LOTTERY_NODE = "LOTTERY"
# 预约组件自带的按钮提示不是抽奖公告，整个 additional 子树都不检查。
_RESERVE = "ADDITIONAL_TYPE_RESERVE"


def parse_filter_uids(raw: str | None) -> frozenset[str]:
    """解析账号列表；``None`` 表示未配置，对所有账号生效；空串表示关闭过滤。"""
    if raw is None:
        return frozenset(DEFAULT_FILTER_UIDS)
    tokens = [token.strip() for token in raw.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return frozenset()
    if any(token.lower() in ALL_ACCOUNTS_ALIASES for token in tokens):
        return frozenset({ALL_ACCOUNTS})
    return frozenset(tokens)


def filter_uids() -> frozenset[str]:
    """当前生效的账号列表。"""
    return parse_filter_uids(bili_settings().lottery_filter_uids)


def applies_to(uid: str, uids: frozenset[str] | None = None) -> bool:
    """这个账号是否参与抽奖过滤。"""
    active = filter_uids() if uids is None else uids
    if not active:
        return False
    return ALL_ACCOUNTS in active or str(uid or "") in active


def _normalized(text: str) -> str:
    """去掉格式与不可见字符，全角或零宽写法绕不过规则。"""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", html.unescape(text))
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def text_lottery_reason(text: str) -> str:
    """这段文字属于社交抽奖的原因；不是抽奖则返回空串。"""
    text = _normalized(text)
    if LOTTERY_WORDS.search(text):
        return "lottery-text"
    if DRAW_PEOPLE.search(text) or PRIZE_WORDS.search(text) or FORWARD_PRIZE.search(text):
        return "giveaway-text"
    if LOTTERY_URL.search(text):
        return "lottery-link"
    return ""


def _content_values(value: Any) -> Iterator[str]:
    """只产出正文类字符串，跳过昵称、计数与界面动作。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _CONTENT_KEYS and isinstance(child, str):
                yield child
            elif isinstance(child, (dict, list)):
                yield from _content_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _content_values(child)


def lottery_reason(item: dict[str, Any]) -> str:
    """这条动态属于抽奖的原因；连带检查转发原文。

    预约组件的按钮提示写作「预约成功，已参与抽奖」，属于界面元数据而不是抽奖
    公告，因此 ADDITIONAL_TYPE_RESERVE 的 additional 子树整体跳过；正文与转发
    原文照常检查。
    """
    visited: set[int] = set()
    while isinstance(item, dict) and id(item) not in visited:
        visited.add(id(item))
        dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
        additional = dynamic.get("additional") or {}
        content = [dynamic.get("desc"), dynamic.get("major")]
        if additional.get("type") != _RESERVE:
            content.append(additional)
        for value in _content_values(content):
            if _LOTTERY_NODE in value:
                return "lottery-node"
            reason = text_lottery_reason(value)
            if reason:
                return reason
        item = item.get("orig")
    return ""


def suppress_dynamic(
    card: BiliCard,
    *,
    uid: str | None = None,
    uids: frozenset[str] | None = None,
) -> bool:
    """这张卡是否属于应当跳过的抽奖动态。"""
    target = card.uid if uid is None else uid
    return (
        applies_to(target, uids)
        and card.card_type == KIND_DYNAMIC
        and bool(
            card.lottery_reason
            or text_lottery_reason(card.title + "\n" + card.description)
        )
    )
