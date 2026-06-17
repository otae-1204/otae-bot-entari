"""Ark 群邀请卡片解析工具。

该模块不依赖 Entari，方便单元测试和后续复用。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

_ARK_INVITE_RE = re.compile(r"邀请你加入群聊|group.*invite|qun\.invite", re.I)


def first_query_value(query: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = query.get(key)
        if values:
            return str(values[0])
    return ""


def parse_ark_invite_raw(raw: str) -> dict | None:
    """解析 Ark JSON 字符串，返回群邀请信息；非群邀请返回 None."""
    if not raw or not _ARK_INVITE_RE.search(raw):
        return None
    try:
        ark = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    meta = ark.get("meta", {}).get("news", {})
    jump = meta.get("jumpUrl", "")
    group_name = meta.get("title", "")
    query = parse_qs(urlparse(jump).query or jump)
    group_code = first_query_value(query, "groupcode", "group_code", "group_id")
    group_name = group_name or first_query_value(query, "groupname", "group_name")
    inviter_uin = first_query_value(query, "senderuin", "inviter_uin", "user_id")
    msgseq = first_query_value(query, "msgseq", "request_id", "flag")

    return {
        "group_code": unquote(group_code),
        "group_name": unquote(group_name),
        "inviter_uin": unquote(inviter_uin),
        "msgseq": msgseq,
        "token": ark.get("config", {}).get("token", ""),
    }


def parse_ark_invite_segment(seg) -> dict | None:
    """从 Satori/OneBot 消息段对象中解析 Ark 群邀请。"""
    data = getattr(seg, "data", None)
    if not isinstance(data, dict):
        return None
    return parse_ark_invite_raw(data.get("data", ""))
