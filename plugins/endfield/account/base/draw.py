from __future__ import annotations

from .models import (
    AccountBaseView,
    MoodOperatorView,
    SettlementRate,
    SettlementRegionView,
    SettlementView,
    SpaceshipRoomView,
)
from ...rendering.cards import (
    PreparedCardHtml,
    _draw_gallery_catalog,
    _prepare_assets,
    esc,
    esc_attr,
)


ACCOUNT_BASE_CARD_WIDTH = 1550
_REGION_THEME_COLORS = {
    "domain_1": "#c1ff55",
    "domain_2": "#6bffff",
}
_REGION_NAME_THEME_COLORS = {
    "四号谷地": "#c1ff55",
    "武陵": "#6bffff",
}
_OVERFLOW_SELECTORS = (
    ".base-header",
    ".base-section",
    ".settlement-card",
    ".room-card",
    ".room-operator",
)


async def draw_account_base_card(view: AccountBaseView) -> bytes:
    return await _draw_gallery_catalog(
        await prepare_account_base_card_html(view),
        ".account-base-card",
        _OVERFLOW_SELECTORS,
        "account_base",
    )


async def prepare_account_base_card_html(view: AccountBaseView) -> PreparedCardHtml:
    return await _prepare_account_base_html(view, inline=False)


async def render_account_base_card_html(view: AccountBaseView) -> str:
    return (await _prepare_account_base_html(view, inline=True)).html


async def _prepare_account_base_html(
    view: AccountBaseView,
    *,
    inline: bool,
) -> PreparedCardHtml:
    prepared = await _prepare_assets(_asset_urls(view), inline=inline)
    return PreparedCardHtml(
        _render_account_base_html(view, prepared.urls),
        prepared.resources,
        ACCOUNT_BASE_CARD_WIDTH,
    )


def _asset_urls(view: AccountBaseView) -> list[str]:
    urls: list[str] = []
    for region in view.regions:
        urls.extend(item.officer_avatar_url for item in region.settlements)
    for room in view.rooms:
        for operator in room.operators:
            urls.append(operator.avatar_url)
            urls.extend(skill.icon_url for skill in operator.skills)
    return [url for url in urls if url]


def _render_account_base_html(view: AccountBaseView, assets: dict[str, str]) -> str:
    regions = "".join(_region_html(region, assets) for region in view.regions)
    rooms = "".join(_room_html(room, assets) for room in view.rooms)
    if not regions:
        regions = '<div class="empty-state">森空岛快照中暂无据点数据</div>'
    if not rooms:
        rooms = '<div class="empty-state">森空岛快照中暂无帝江号舱室数据</div>'
    identity = " · ".join(
        item
        for item in (
            view.nickname,
            view.server_name,
            f"UID {view.uid or '--'}",
        )
        if item
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head>
<body><main class="account-base-card">
  <header class="base-header">
    <div>
      <div class="base-kicker">ENDFIELD / ACCOUNT INFRASTRUCTURE</div>
      <h1>基建与帝江号</h1>
      <div class="base-identity">{esc(identity)}</div>
    </div>
    <div class="base-source"><strong>ARKNIGHTS: ENDFIELD</strong><span>数据来源 森空岛 · 更新 {esc(view.saved_at or "--")}</span></div>
  </header>
  <section class="base-section settlements-section">
    <div class="section-head"><h2>据点存票</h2><span>{view.settlement_count} 个据点 · 增长速度来自历史快照</span></div>
    {regions}
    <div class="source-legend"><span class="source-badge sampled">实测</span><span>历史快照有效增量</span><span class="source-badge pending">待采样</span><span>需要间隔至少 60 秒的下一次快照</span></div>
  </section>
  <section class="base-section rooms-section">
    <div class="section-head"><h2>帝江号心情</h2><span>{view.room_count} 个舱室 · 连续工作/休息理论值</span></div>
    <div class="rooms-grid">{rooms}</div>
  </section>
  <footer class="base-footer"><span>ⓘ　心情时间为连续工作/休息理论值</span><span>增长速度来自同等级、同上限、同派驻配置的历史快照</span></footer>
</main></body></html>"""


def _region_html(region: SettlementRegionView, assets: dict[str, str]) -> str:
    cards = "".join(_settlement_html(item, assets) for item in region.settlements)
    theme_color = _REGION_THEME_COLORS.get(
        region.region_id,
        _REGION_NAME_THEME_COLORS.get(region.name, "#ffd000"),
    )
    return f"""<div class="region-block" style="--region-color:{esc_attr(theme_color)}">
      <div class="region-title"><span></span><h3>{esc(region.name)}</h3></div>
      <div class="settlement-grid">{cards}</div>
    </div>"""


def _settlement_html(item: SettlementView, assets: dict[str, str]) -> str:
    officer_url = assets.get(item.officer_avatar_url, "")
    officer = (
        f'<img src="{esc_attr(officer_url)}" alt="">'
        if officer_url
        else '<span class="avatar-fallback">OP</span>'
    )
    rate_value = (
        f"+{_compact_number(item.rate.value_per_hour or 0)}<small>/ 小时</small>"
        if item.rate.available
        else "待采样"
    )
    rate_class = "available" if item.rate.available else "pending"
    return f"""<article class="settlement-card">
      <div class="settlement-top">
        <div><h4>{esc(item.name)}</h4><span>LV.{item.level or "--"}</span></div>
        {_rate_badge(item.rate)}
      </div>
      <div class="settlement-main">
        <div class="settlement-avatar">{officer}</div>
        <div class="settlement-metrics">
          <label>当前存票</label>
          <div class="money-line"><strong>{_number(item.current_money)}</strong><span>/ {_number(item.money_max)}</span></div>
          <div class="progress"><i style="width:{item.fill_ratio * 100:.2f}%"></i></div>
          <label>增长速度</label>
          <div class="rate-value {rate_class}">{rate_value}</div>
        </div>
      </div>
      <div class="settlement-bottom"><span>预计满仓</span><strong>{esc(_full_eta(item))}</strong></div>
    </article>"""


def _rate_badge(rate: SettlementRate) -> str:
    if not rate.available:
        return '<span class="source-badge pending">待采样</span>'
    confidence = {"low": "低可信", "medium": "中可信", "high": "高可信"}.get(
        rate.confidence, ""
    )
    suffix = f" · {confidence}" if confidence else ""
    return f'<span class="source-badge sampled">实测{esc(suffix)}</span>'


def _room_html(room: SpaceshipRoomView, assets: dict[str, str]) -> str:
    operators = "".join(_operator_html(operator, assets) for operator in room.operators)
    if not operators:
        operators = '<div class="room-empty">暂无进驻干员</div>'
    return f"""<article class="room-card">
      <header><h3>{esc(room.name)}</h3><span>LV.{room.level or "--"}</span></header>
      <div class="room-operators">{operators}</div>
    </article>"""


def _operator_html(operator: MoodOperatorView, assets: dict[str, str]) -> str:
    image_url = assets.get(operator.avatar_url, "")
    portrait = (
        f'<img src="{esc_attr(image_url)}" alt="">'
        if image_url
        else '<span class="avatar-fallback">OP</span>'
    )
    state = "critical" if operator.mood_percent < 35 else "warning" if operator.mood_percent < 65 else "normal"
    mood_skills = [skill for skill in operator.skills if skill.mood_effect]
    visible_skills = mood_skills or list(operator.skills[:1])
    skill_text = " · ".join(
        f"{skill.name}{f'（{skill.mood_effect}）' if skill.mood_effect else ''}"
        for skill in visible_skills[:2]
    ) or "无心情技能"
    return f"""<div class="room-operator">
      <div class="operator-avatar">{portrait}</div>
      <div class="operator-content">
        <div class="operator-line"><strong>{esc(operator.name)}</strong><span>心情 <b>{operator.mood_percent:.0f}%</b></span></div>
        <div class="mood-progress {state}"><i style="width:{operator.mood_percent:.2f}%"></i></div>
        <div class="operator-skill">{esc(skill_text)}</div>
        <div class="operator-stats">
          <span>工作 {_hours(operator.continuous_work_hours)}</span>
          <span>回满 {_hours(operator.full_recovery_hours)}</span>
          <span>消耗 -{operator.drain_percent_per_hour:.1f}%/h</span>
        </div>
      </div>
    </div>"""


def _full_eta(item: SettlementView) -> str:
    if item.is_full:
        return "已满仓"
    if item.hours_to_full is None:
        return "等待采样"
    return _duration(item.hours_to_full)


def _duration(hours: float) -> str:
    total_minutes = max(0, round(hours * 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    whole_hours, minutes = divmod(remainder, 60)
    if days:
        return f"约 {days}天 {whole_hours}小时"
    if whole_hours:
        return f"约 {whole_hours}小时 {minutes}分"
    return f"约 {minutes}分钟"


def _hours(value: float | None) -> str:
    if value is None:
        return "--"
    if value >= 10:
        return f"{value:.0f}小时"
    return f"{value:.1f}小时"


def _number(value: int) -> str:
    return f"{max(0, int(value)):,}"


def _compact_number(value: float) -> str:
    return f"{max(0.0, value):,.0f}"


def _css() -> str:
    return f"""
* {{ box-sizing:border-box; }}
html,body {{ margin:0; width:{ACCOUNT_BASE_CARD_WIDTH}px; min-height:900px; background:#e1e5e6; color:#171b1f; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; }}
body {{ overflow:visible; }}
.account-base-card {{ width:{ACCOUNT_BASE_CARD_WIDTH}px; min-height:900px; padding:26px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e8ebec); }}
.base-header {{ min-height:220px; display:grid; grid-template-columns:1fr auto; align-items:center; gap:32px; padding:34px 38px; background:#171b1f; color:#fff; border-bottom:7px solid #59636a; }}
.base-kicker {{ color:#9ca5ab; font-size:18px; font-weight:950; letter-spacing:.14em; }}
.base-header h1 {{ margin:12px 0 15px; font-size:64px; line-height:1; font-weight:950; letter-spacing:-.045em; }}
.base-identity {{ color:#d4d9dc; font-size:20px; font-weight:800; }}
.base-source {{ display:flex; flex-direction:column; align-items:flex-end; gap:10px; color:#c6ccd0; font-size:18px; font-weight:850; }}
.base-source strong {{ color:#fff; font-size:22px; }}
.base-section {{ margin-top:22px; padding:22px 20px 20px; overflow:visible; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); }}
.section-head {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; padding-bottom:14px; border-bottom:5px solid #20262a; }}
.section-head h2 {{ position:relative; margin:0; padding-left:20px; font-size:34px; line-height:1; font-weight:950; }}
.section-head h2::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:8px; background:#ffd000; }}
.section-head span {{ color:#737c82; font-size:16px; font-weight:850; }}
.region-block {{ margin-top:18px; border:1px solid rgba(23,27,31,.24); background:rgba(255,255,255,.38); }}
.region-title {{ height:58px; display:flex; align-items:center; gap:12px; padding:0 18px; border-bottom:1px solid rgba(23,27,31,.20); }}
.region-title span {{ width:0; height:0; border-top:8px solid transparent; border-bottom:8px solid transparent; border-left:13px solid var(--region-color,#ffd000); }}
.region-title h3 {{ margin:0; font-size:23px; font-weight:950; }}
.settlement-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; padding:18px; }}
.settlement-card {{ min-height:325px; padding:18px; overflow:visible; border:1px solid rgba(23,27,31,.30); background:#f8f9f7; box-shadow:inset 0 4px 0 #20262a; }}
.settlement-top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
.settlement-top > div {{ min-width:0; display:flex; align-items:baseline; gap:12px; }}
.settlement-top h4 {{ margin:0; min-width:0; font-size:25px; font-weight:950; overflow-wrap:anywhere; }}
.settlement-top > div > span {{ flex:none; color:#59646b; font-size:17px; font-weight:900; }}
.source-badge {{ display:inline-flex; align-items:center; min-height:30px; padding:4px 9px; border:1px solid currentColor; background:#fff; font-size:13px; font-weight:950; white-space:nowrap; }}
.source-badge.sampled {{ color:#286cd6; }}
.source-badge.pending {{ color:#7b8388; }}
.settlement-main {{ display:grid; grid-template-columns:130px 1fr; align-items:start; gap:18px; margin-top:20px; }}
.settlement-avatar {{ width:130px; height:130px; display:grid; place-items:center; overflow:hidden; border:1px solid rgba(23,27,31,.22); background:radial-gradient(circle,#fff,#e2e6e7); }}
.settlement-avatar img {{ width:100%; height:100%; object-fit:contain; }}
.avatar-fallback {{ color:#92999d; font-size:18px; font-weight:950; }}
.settlement-metrics label {{ display:block; margin-top:1px; color:#727b80; font-size:14px; font-weight:900; }}
.money-line {{ display:flex; align-items:baseline; gap:8px; margin-top:4px; }}
.money-line strong {{ font-size:30px; line-height:1; font-weight:950; }}
.money-line span {{ color:#69737a; font-size:14px; font-weight:850; }}
.progress {{ height:17px; margin:11px 0 18px; overflow:hidden; border:1px solid #8f989d; background:#edf0f1; }}
.progress i {{ display:block; height:100%; background:#286cd6; }}
.rate-value {{ margin-top:4px; color:#286cd6; font-size:30px; line-height:1; font-weight:950; }}
.rate-value small {{ margin-left:8px; font-size:14px; font-weight:900; }}
.rate-value.pending {{ color:#899196; font-size:22px; }}
.settlement-bottom {{ margin-top:18px; padding-top:15px; display:flex; flex-direction:column; gap:7px; border-top:1px dashed rgba(23,27,31,.28); }}
.settlement-bottom span {{ color:#747d82; font-size:14px; font-weight:900; }}
.settlement-bottom strong {{ font-size:22px; font-weight:950; }}
.source-legend {{ display:flex; align-items:center; gap:10px; margin-top:14px; color:#737c82; font-size:14px; font-weight:850; }}
.source-legend .source-badge.pending {{ margin-left:22px; }}
.rooms-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; margin-top:18px; align-items:start; }}
.room-card {{ align-self:start; overflow:hidden; border:1px solid rgba(23,27,31,.30); background:#f8f9f7; }}
.room-card > header {{ height:58px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 18px; background:#20252a; color:#fff; }}
.room-card > header h3 {{ margin:0; font-size:24px; font-weight:950; }}
.room-card > header span {{ color:#d6dbde; font-size:17px; font-weight:900; }}
.room-operators {{ display:flex; flex-direction:column; }}
.room-operator {{ min-height:155px; display:grid; grid-template-columns:100px minmax(0,1fr); gap:13px; padding:13px; border-bottom:1px dotted rgba(23,27,31,.28); background:rgba(255,255,255,.44); }}
.room-operator:last-child {{ border-bottom:0; }}
.operator-avatar {{ width:100px; height:100px; display:grid; place-items:center; align-self:start; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); border:1px solid rgba(23,27,31,.18); }}
.operator-avatar img {{ width:100%; height:100%; object-fit:contain; }}
.operator-content {{ min-width:0; }}
.operator-line {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; }}
.operator-line > strong {{ min-width:0; font-size:21px; font-weight:950; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.operator-line > span {{ flex:none; color:#616b72; font-size:13px; font-weight:900; }}
.operator-line b {{ color:#171b1f; font-size:18px; }}
.mood-progress {{ height:11px; margin-top:8px; overflow:hidden; background:#e1e5e6; border:1px solid rgba(23,27,31,.22); }}
.mood-progress i {{ display:block; height:100%; }}
.mood-progress.normal i {{ background:#286cd6; }}
.mood-progress.warning i {{ background:#d29a00; }}
.mood-progress.critical i {{ background:#c72d35; }}
.operator-skill {{ min-height:20px; margin-top:8px; overflow:hidden; color:#59656c; font-size:12px; line-height:1.35; font-weight:850; text-overflow:ellipsis; white-space:nowrap; }}
.operator-stats {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:5px; margin-top:7px; }}
.operator-stats span {{ min-width:0; padding:5px 4px; overflow:hidden; border:1px solid rgba(40,108,214,.24); background:#eef3fb; color:#3468ad; font-size:10px; font-weight:900; text-align:center; white-space:nowrap; }}
.room-empty,.empty-state {{ padding:28px; color:#7b8489; background:#edf0f0; font-size:17px; font-weight:850; }}
.base-footer {{ min-height:76px; margin-top:22px; padding:0 24px; display:flex; align-items:center; justify-content:space-between; gap:20px; background:#20252a; color:#cfd4d7; font-size:15px; font-weight:850; }}
"""
