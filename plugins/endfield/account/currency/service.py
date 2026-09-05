from __future__ import annotations

from dataclasses import dataclass
import calendar
from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from ..client import CurrencyLogItem


TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_QUERY_MONTHS = 1

CURRENCY_NAMES = {
    1: "源石",
    2: "嵌晶玉",
    3: "武库配额",
}

CHANGE_TYPE_NAMES = {
    0: "全部",
    1: "获取",
    2: "消耗",
}

# The service uses the same reason code table for all three currencies.  Keep
# unknown values visible instead of silently presenting an incorrect label.
CHANGE_REASON_NAMES = {
    "0": "其他",
    "2": "邮件领取",
    "3": "采购中心-源石交易所",
    "4": "采购中心-组合包",
    "5": "购买月卡立得",
    "6": "解锁源石配给",
    "7": "源石兑换嵌晶玉",
    "8": "衍质源石兑换武库配额",
    "9": "源石恢复理智",
    "10": "干员寻访",
    "11": "寻访自动兑换找零",
    "12": "协议通行证奖励",
    "13": "任务奖励",
    "14": "世界探索奖励",
    "15": "副本奖励",
    "16": "活动中心奖励",
    "17": "提交醚质后获得",
    "19": "行动手册日常活跃度奖励",
    "21": "月卡每日领取",
    "22": "信用交易所兑换",
    "24": "系统玩法奖励",
    "25": "武库交易所消耗",
}


@dataclass(frozen=True, slots=True)
class CurrencyReasonSummary:
    reason_code: str
    label: str
    kind: str
    count: int
    amount: int


@dataclass(frozen=True, slots=True)
class CurrencyLogSummary:
    currency_type: int
    records: tuple[CurrencyLogItem, ...]
    opening: int | None
    closing: int | None
    gain: int
    consume: int
    net: int
    reasons: tuple[CurrencyReasonSummary, ...]


def currency_name(currency_type: int) -> str:
    return CURRENCY_NAMES.get(int(currency_type), f"未知资源({currency_type})")


def change_type_name(change_type: int) -> str:
    return CHANGE_TYPE_NAMES.get(int(change_type), f"未知类型({change_type})")


def reason_label(reason_code: str | int) -> str:
    key = str(reason_code).strip()
    return CHANGE_REASON_NAMES.get(key, f"未知原因({key})")


def signed_delta(item: CurrencyLogItem) -> int:
    if item.change_type == 2:
        return -abs(item.change_num)
    if item.change_type == 1:
        return abs(item.change_num)
    return item.change_num


def _kind_for(item: CurrencyLogItem) -> str:
    if item.change_type == 2:
        return "consume"
    if item.change_type == 1:
        return "gain"
    return "gain" if signed_delta(item) >= 0 else "consume"


def aggregate_currency_logs(
    items: Iterable[CurrencyLogItem], currency_type: int
) -> CurrencyLogSummary:
    ordered = tuple(
        sorted(
            (item for item in items if item.currency_type == currency_type),
            key=lambda item: (item.change_time, item.seq_id),
        )
    )
    gained = 0
    consumed = 0
    by_reason: dict[tuple[str, str], dict[str, int | str]] = {}
    for item in ordered:
        amount = abs(signed_delta(item))
        kind = _kind_for(item)
        if kind == "gain":
            gained += amount
        else:
            consumed += amount
        key = (kind, str(item.change_reason).strip())
        row = by_reason.setdefault(
            key,
            {
                "reason_code": key[1],
                "label": reason_label(key[1]),
                "kind": kind,
                "count": 0,
                "amount": 0,
            },
        )
        row["count"] = int(row["count"]) + 1
        row["amount"] = int(row["amount"]) + amount

    opening: int | None = None
    closing: int | None = None
    if ordered:
        opening = ordered[0].after - signed_delta(ordered[0])
        closing = ordered[-1].after
    net = closing - opening if opening is not None and closing is not None else gained - consumed
    reason_rows = tuple(
        CurrencyReasonSummary(
            reason_code=str(row["reason_code"]),
            label=str(row["label"]),
            kind=str(row["kind"]),
            count=int(row["count"]),
            amount=int(row["amount"]),
        )
        for row in sorted(
            by_reason.values(),
            key=lambda row: (0 if row["kind"] == "gain" else 1, -int(row["amount"]), str(row["label"])),
        )
    )
    return CurrencyLogSummary(
        currency_type=currency_type,
        records=ordered,
        opening=opening,
        closing=closing,
        gain=gained,
        consume=consumed,
        net=net,
        reasons=reason_rows,
    )


def date_bounds(start: date, end: date) -> tuple[int, int]:
    if start > end:
        raise ValueError("资源流水查询的开始日期不能晚于结束日期")
    start_dt = datetime.combine(start, time.min, tzinfo=TIMEZONE)
    end_dt = datetime.combine(end, time.max.replace(microsecond=0), tzinfo=TIMEZONE)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def earliest_currency_log_date(items: Iterable[CurrencyLogItem]) -> date | None:
    timestamps = [int(item.change_time) for item in items if int(item.change_time) > 0]
    if not timestamps:
        return None
    return datetime.fromtimestamp(min(timestamps), tz=TIMEZONE).date()


def format_all_history_period_label(
    items: Iterable[CurrencyLogItem], *, end: date, quota_start: date | None = None
) -> str:
    start = earliest_currency_log_date(items) or end
    label = f"{start:%Y/%m/%d}-如今"
    if quota_start is not None:
        label += f"（武库配额最早：{quota_start:%Y/%m/%d}；接口约30天，更早仅限本地备份）"
    return label


def resolve_query_dates(
    start_text: str = "",
    end_text: str = "",
    *,
    days: int | None = None,
    today: date | None = None,
) -> tuple[date, date]:
    current = today or datetime.now(TIMEZONE).date()
    if days is not None:
        if int(days) <= 0:
            raise ValueError("资源流水查询天数必须大于 0")
        if start_text or end_text:
            raise ValueError("资源流水查询天数不能与日期范围同时使用")
        end = current
        return end - timedelta(days=int(days) - 1), end
    end = _parse_date_text(end_text) if end_text else current
    start = _parse_date_text(start_text) if start_text else _month_before(end, DEFAULT_QUERY_MONTHS)
    if start > end:
        raise ValueError("资源流水查询的开始日期不能晚于结束日期")
    return start, end


def format_currency_log_report(
    summaries: Sequence[CurrencyLogSummary],
    *,
    role_label: str,
    start: date,
    end: date,
    change_type: int,
    period_label: str = "",
) -> str:
    selected_names = "、".join(currency_name(item.currency_type) for item in summaries)
    effective_period = period_label or f"{start.isoformat()} 至 {end.isoformat()}"
    lines = [
        "终末地资源流水",
        f"角色：{role_label}",
        f"时间：{effective_period} · 资源：{selected_names or '全部'} · 类型：{change_type_name(change_type)}",
    ]
    total_count = sum(len(item.records) for item in summaries)
    if total_count == 0:
        lines.append("查询范围内没有流水记录。")
        return "\n".join(lines)

    for summary in summaries:
        lines.append("")
        lines.append(f"【{currency_name(summary.currency_type)}】共 {len(summary.records)} 条")
        if not summary.records:
            lines.append("  查询范围内无记录")
            continue
        lines.append(
            "  "
            f"期初 {_format_number(summary.opening)} → 期末 {_format_number(summary.closing)}；"
            f"获取 {summary.gain:,}；消耗 {summary.consume:,}；净变化 {_format_signed(summary.net)}"
        )
        if summary.reasons:
            lines.append(
                "  类型汇总："
                + "；".join(
                    f"{row.label} {_format_signed(row.amount) if row.kind == 'gain' else '-' + format(row.amount, ',')}×{row.count}"
                    for row in summary.reasons
                )
            )
    return "\n".join(lines)


def split_report(text: str, *, max_length: int = 3500) -> tuple[str, ...]:
    """Split a long plain-text report without cutting a detail row in half."""

    if len(text) <= max_length:
        return (text,)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        line_length = len(line) + (1 if current else 0)
        if current and current_length + line_length > max_length:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        if len(line) > max_length:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            for index in range(0, len(line), max_length):
                chunks.append(line[index : index + max_length])
            continue
        current.append(line)
        current_length += line_length
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks) or ("",)


def _parse_date_text(value: str) -> date:
    text = str(value or "").strip().lower()
    if text in {"今天", "today"}:
        return datetime.now(TIMEZONE).date()
    if text in {"昨天", "yesterday"}:
        return datetime.now(TIMEZONE).date() - timedelta(days=1)
    return date.fromisoformat(text)


def _month_before(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - int(months)
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _format_number(value: int | None) -> str:
    return "未知" if value is None else f"{value:,}"


def _format_signed(value: int) -> str:
    return f"{value:+,}"
