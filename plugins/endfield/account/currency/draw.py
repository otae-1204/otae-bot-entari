from __future__ import annotations

from datetime import date
from typing import Sequence

from .service import (
    CurrencyReasonSummary,
    CurrencyLogSummary,
    change_type_name,
    currency_name,
)
from ...rendering.cards import _draw_neutral_card, esc


async def draw_currency_log_cards(
    summaries: Sequence[CurrencyLogSummary],
    *,
    role_label: str,
    start: date,
    end: date,
    change_type: int,
    period_label: str = "",
) -> tuple[bytes, ...]:
    return (
        await _draw_currency_log_card(
            summaries,
            role_label=role_label,
            start=start,
            end=end,
            change_type=change_type,
            period_label=period_label,
        ),
    )


async def _draw_currency_log_card(
    summaries: Sequence[CurrencyLogSummary],
    *,
    role_label: str,
    start: date,
    end: date,
    change_type: int,
    period_label: str,
) -> bytes:
    total_count = sum(len(summary.records) for summary in summaries)
    summary_html = "".join(_currency_summary_panel(summary) for summary in summaries)
    detail_html = "".join(_currency_detail_panel(summary) for summary in summaries)
    effective_period = period_label or f"{start.isoformat()} ~ {end.isoformat()}"
    body = f"""
    <header>
      <div>
        <small>ENDFIELD / CURRENCY LOG</small>
        <h1>资源流水汇总</h1>
        <p>{esc(role_label)} / {esc(effective_period)} / 类型 {esc(change_type_name(change_type))}</p>
      </div>
      <time>共 {total_count} 条</time>
    </header>
    <main>
      <section class="currency-summary-grid">{summary_html or '<div class="empty">没有选择资源类型</div>'}</section>
      <section class="currency-detail-block">
        <div class="currency-detail-heading"><h2>流水类型明细</h2><span>按获取 / 消耗原因合计</span></div>
        <section class="currency-detail-grid">{detail_html or '<div class="empty">没有流水明细</div>'}</section>
      </section>
      <footer class="currency-footer"><span>源石 / 嵌晶玉 / 武库配额</span><span>数据来自官方资源流水接口</span></footer>
    </main>
    """
    return await _draw_neutral_card(
        "currency-log-card",
        body,
        extra_css="""
        header p{margin:8px 0 0;color:#cfd4d6;font-size:15px}
        header time{font-size:18px;font-weight:900;white-space:nowrap}
        .currency-summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;align-items:stretch}.currency-summary-card{min-width:0;padding:15px 16px 13px;border:1px solid #9b9b9b;border-top:7px solid var(--currency-accent);background:#fff}.currency-summary-card.currency-1{--currency-accent:#b77b18}.currency-summary-card.currency-2{--currency-accent:#d24652}.currency-summary-card.currency-3{--currency-accent:#3978d0}
        .currency-summary-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px}.currency-summary-head b{font-size:25px;font-weight:950}.currency-summary-head span{color:#777;font-size:13px;font-weight:850;white-space:nowrap}.currency-summary-balance{display:flex;align-items:baseline;gap:7px;margin-top:11px;color:#777;font-size:12px;font-weight:850;white-space:nowrap}.currency-summary-balance strong{color:#222;font-size:31px;line-height:1;font-weight:950}.currency-summary-balance em{color:#999;font-style:normal;font-size:16px}.currency-summary-divider{height:1px;margin-top:12px;background:#c9cccd}.currency-summary-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:11px}.currency-summary-metrics span{display:flex;flex-direction:column;color:#70787c;font-size:12px;font-weight:850}.currency-summary-metrics b{margin-top:4px;color:#222;font-size:17px;line-height:1.1;white-space:nowrap}.currency-summary-metrics .gain b{color:#187943}.currency-summary-metrics .consume b{color:#b23a31}.currency-summary-metrics .net b{color:#286cd6}.currency-summary-metrics .net.negative b{color:#b23a31}
        .currency-detail-block{margin-top:16px}.currency-detail-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:0 2px 9px;border-bottom:4px solid #222}.currency-detail-heading h2{margin:0;font-size:22px;font-weight:950}.currency-detail-heading span{color:#777;font-size:12px;font-weight:800}.currency-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-top:12px;align-items:start}.currency-detail-panel{min-width:0;border:1px solid #9b9b9b;border-top:5px solid var(--currency-accent);background:#fff}.currency-detail-panel.currency-1{--currency-accent:#b77b18}.currency-detail-panel.currency-2{--currency-accent:#d24652}.currency-detail-panel.currency-3{--currency-accent:#3978d0}.currency-detail-panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:10px 12px;border-bottom:1px solid #c4c8c9}.currency-detail-panel-head b{font-size:19px;font-weight:950}.currency-detail-panel-head span{color:#777;font-size:11px;font-weight:800}.currency-detail-stack{display:grid;gap:10px;padding:10px}.reason-group{min-width:0;border:1px solid #c4c8c9;background:#fafafa}.reason-group h3{display:flex;justify-content:space-between;align-items:baseline;margin:0;padding:9px 11px;border-bottom:2px solid #777;color:#30383c;font-size:15px;font-weight:950}.reason-group.gain h3{border-bottom-color:#2d8a55}.reason-group.consume h3{border-bottom-color:#c14b43}.reason-group h3 b{font-size:13px}.reason-group.gain h3 b{color:#187943}.reason-group.consume h3 b{color:#b23a31}.reason-list{padding:5px 10px}.reason-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:8px 1px;border-bottom:1px solid #e0e2e2}.reason-row:last-child{border-bottom:0}.reason-row span{min-width:0;display:flex;flex-direction:column;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#353d41;font-size:13px;font-weight:900}.reason-row small{margin-top:3px;color:#92999c;font-size:10px;font-weight:750}.reason-row b{font-size:15px;font-weight:950;white-space:nowrap}.reason-group.gain .reason-row b{color:#187943}.reason-group.consume .reason-row b{color:#b23a31}.reason-empty{padding:16px 11px;color:#8a9296;font-size:13px;font-weight:800}
        .currency-footer{display:flex;justify-content:space-between;margin-top:13px;padding-top:10px;border-top:2px solid #222;color:#777;font-size:12px;font-weight:800}
        """,
    )


def _currency_summary_panel(summary: CurrencyLogSummary) -> str:
    opening = _number(summary.opening)
    closing = _number(summary.closing)
    net_class = "negative" if summary.net < 0 else ""
    return (
        f'<article class="currency-summary-card currency-{summary.currency_type}">'
        f'<div class="currency-summary-head"><b>{esc(currency_name(summary.currency_type))}</b>'
        f'<span>{len(summary.records)} 条</span></div>'
        f'<div class="currency-summary-balance"><strong>{esc(closing)}</strong><span>期末 · 期初 {esc(opening)}</span></div>'
        '<div class="currency-summary-divider"></div>'
        '<div class="currency-summary-metrics">'
        f'<span class="gain">获取<b>+{summary.gain:,}</b></span>'
        f'<span class="consume">消耗<b>-{summary.consume:,}</b></span>'
        f'<span class="net {net_class}">净变化<b>{_signed(summary.net)}</b></span>'
        '</div></article>'
    )


def _currency_detail_panel(summary: CurrencyLogSummary) -> str:
    gain_rows = "".join(_reason_row(row, "gain") for row in summary.reasons if row.kind == "gain")
    consume_rows = "".join(_reason_row(row, "consume") for row in summary.reasons if row.kind == "consume")
    gain_body = gain_rows or '<div class="reason-empty">无获取记录</div>'
    consume_body = consume_rows or '<div class="reason-empty">无消耗记录</div>'
    return (
        f'<article class="currency-detail-panel currency-{summary.currency_type}">'
        f'<div class="currency-detail-panel-head"><b>{esc(currency_name(summary.currency_type))}</b>'
        f'<span>按原因汇总 · {len(summary.reasons)} 类</span></div>'
        '<div class="currency-detail-stack">'
        f'<section class="reason-group gain"><h3>获取类型 <b>+{summary.gain:,}</b></h3><div class="reason-list">{gain_body}</div></section>'
        f'<section class="reason-group consume"><h3>消耗类型 <b>-{summary.consume:,}</b></h3><div class="reason-list">{consume_body}</div></section>'
        '</div></article>'
    )


def _reason_row(row: CurrencyReasonSummary, kind: str) -> str:
    amount = _signed(row.amount) if kind == "gain" else f"-{row.amount:,}"
    return (
        '<div class="reason-row">'
        f'<span>{esc(row.label)}<small>{row.count} 次</small></span>'
        f'<b>{esc(amount)}</b>'
        '</div>'
    )


def _number(value: int | None) -> str:
    return "--" if value is None else f"{value:,}"


def _signed(value: int) -> str:
    return f"{value:+,}"
