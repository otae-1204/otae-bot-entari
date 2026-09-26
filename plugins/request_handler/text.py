"""群邀请通知文案与审批回复解析。"""

from __future__ import annotations

from typing import Any

_APPROVE_EXACT = {
    "yes",
    "y",
    "ok",
    "approve",
    "同意",
    "是",
    "通过",
    "接受",
}
_REJECT_EXACT = {
    "no",
    "n",
    "reject",
    "拒绝",
    "否",
    "忽略",
}
_APPROVE_PREFIX = ("yes", "approve", "同意", "通过", "接受")
_REJECT_PREFIX = ("reject", "拒绝", "忽略")


def text_or_empty(*values: Any) -> str:
    """取第一个有效展示文本，避免把 Python None 渲染成 'None'。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.casefold() != "none":
            return text
    return ""


def entity_label(entity: Any, *extra: Any) -> str:
    """从 User/Guild 或 dict 中取出 nick/name/id。"""
    if entity is None:
        return text_or_empty(*extra)
    if isinstance(entity, dict):
        return text_or_empty(
            entity.get("nick"),
            entity.get("name"),
            entity.get("id"),
            *extra,
        )
    return text_or_empty(
        getattr(entity, "nick", None),
        getattr(entity, "name", None),
        getattr(entity, "id", None),
        *extra,
    )


def select_invite_reply(
    *,
    user_id: str,
    superuser_id: str,
    private: bool,
    self_id: str,
    text: str,
    pending: dict[str, dict],
) -> tuple[str, dict] | None:
    """只接受该 bot 私聊里的明确审批，避免串到其他群或其他 bot。"""
    if not private or not superuser_id or str(user_id) != str(superuser_id) or not self_id:
        return None
    if parse_decision(text) is None:
        return None
    for key, info in pending.items():
        if str(info.get("self_id") or "") == str(self_id):
            return key, info
    return None


def parse_decision(text: str) -> bool | None:
    """解析超级用户审批回复。True 同意，False 拒绝，无法识别则 None。"""
    compact = "".join(str(text or "").strip().split()).casefold()
    if not compact:
        return None
    if compact in _APPROVE_EXACT:
        return True
    if compact in _REJECT_EXACT:
        return False
    if compact.startswith(_APPROVE_PREFIX):
        return True
    if compact.startswith(_REJECT_PREFIX):
        return False
    return None
