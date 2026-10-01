"""Command validation; permission checks and state changes live in runtime."""

from __future__ import annotations

from dataclasses import dataclass

from .models import KINDS

HELP = """终末地国服公告提醒（北京时间）
/ef 公告 订阅 [全部|活动|维护|卡池|签到|其他]：可同时选择多种类型
/ef 公告 取消订阅
/ef 公告 状态：查看当前群或本人的订阅与采集状态
/ef 公告 列表：查看最近 5 条已采集公告
/ef 公告 提前 开始 60：活动/卡池/签到开始前 60 分钟提醒
/ef 公告 提前 结束 1440：结束前 1440 分钟提醒
/ef 公告 提前 维护 60：维护开始前 60 分钟提醒
分钟数支持 1–10080，也可写“关闭”。默认分别为 60、1440、60。
群订阅仅群主、管理员或 SUPERUSER 可修改；私聊只管理本人的订阅。
首次成功采集建立基线，不补推历史公告；未来日程仍会提醒。
只为官网明确写出的日期和时刻设置提醒，不推算“版本更新后”等时间。"""


@dataclass(frozen=True)
class AnnouncementCommand:
    action: str
    kinds: tuple[str, ...] = ()
    setting: str = ""
    minutes: int = 0
    error: str = ""


def parse(args: tuple[str, ...]) -> AnnouncementCommand:
    if not args or args[0].lower() in {"帮助", "help", "?"}:
        return AnnouncementCommand(
            "help", error="帮助命令不接受其他参数" if len(args) > 1 else ""
        )
    head = args[0].lower()
    action = {
        "订阅": "subscribe",
        "subscribe": "subscribe",
        "取消订阅": "unsubscribe",
        "退订": "unsubscribe",
        "unsubscribe": "unsubscribe",
        "状态": "status",
        "status": "status",
        "列表": "list",
        "list": "list",
        "提前": "lead",
        "lead": "lead",
    }.get(head)
    if action is None:
        return AnnouncementCommand(
            "invalid", error="未知公告命令，使用 /ef 公告 帮助 查看用法"
        )
    if action == "subscribe":
        aliases = {
            **{label: key for key, label in KINDS.items()},
            **{key: key for key in KINDS},
            "其他": "notice",
            "公告": "notice",
        }
        words = tuple(word.lower() for word in args[1:])
        if not words or words in {("全部",), ("all",)}:
            return AnnouncementCommand(action, tuple(KINDS))
        if any(word not in aliases for word in words):
            return AnnouncementCommand(
                action,
                error="订阅类型支持：全部、活动、维护、卡池、签到、其他；多种类型用空格分隔",
            )
        return AnnouncementCommand(
            action, tuple(dict.fromkeys(aliases[word] for word in words))
        )
    if action == "lead":
        settings = {
            "开始": "start_minutes",
            "结束": "end_minutes",
            "维护": "maintenance_minutes",
        }
        if len(args) != 3 or args[1] not in settings:
            return AnnouncementCommand(
                action, error="用法：/ef 公告 提前 开始|结束|维护 <1–10080分钟|关闭>"
            )
        if args[2].lower() in {"关闭", "off"}:
            minutes = 0
        elif (
            args[2].isascii()
            and args[2].isdigit()
            and len(args[2]) <= 5
            and 1 <= int(args[2]) <= 10080
        ):
            minutes = int(args[2])
        else:
            return AnnouncementCommand(
                action, error="提前分钟数必须是 1–10080 的整数，或填写“关闭”"
            )
        return AnnouncementCommand(action, setting=settings[args[1]], minutes=minutes)
    return AnnouncementCommand(
        action, error="此命令不接受其他参数" if len(args) != 1 else ""
    )
