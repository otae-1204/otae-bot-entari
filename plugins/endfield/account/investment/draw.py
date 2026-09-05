from __future__ import annotations

from .models import AccountInvestmentView, InvestmentContributionView, InvestmentResourceView
from ...rendering.cards import (
    PreparedCardHtml,
    _draw_gallery_catalog,
    _gallery_image,
    _prepare_assets,
    esc,
    esc_attr,
)


ACCOUNT_INVESTMENT_CARD_WIDTH = 1550
_SUMMARY_OVERFLOW = (".investment-header", ".investment-section", ".investment-stat", ".investment-category")
_DETAIL_OVERFLOW = (".investment-header", ".investment-section", ".investment-resource", ".investment-ranking-row")


async def draw_account_investment_cards(view: AccountInvestmentView) -> tuple[bytes, ...]:
    return (
        await _draw_gallery_catalog(
            await prepare_account_investment_summary_html(view),
            ".account-investment-summary",
            _SUMMARY_OVERFLOW,
            "account_investment_summary",
        ),
        await _draw_gallery_catalog(
            await prepare_account_investment_detail_html(view),
            ".account-investment-detail",
            _DETAIL_OVERFLOW,
            "account_investment_detail",
        ),
    )


async def prepare_account_investment_summary_html(view: AccountInvestmentView) -> PreparedCardHtml:
    assets = await _prepare_assets(_investment_asset_urls(view), inline=False)
    return PreparedCardHtml(_render_summary_html(view, assets.urls), assets.resources, ACCOUNT_INVESTMENT_CARD_WIDTH)


async def prepare_account_investment_detail_html(view: AccountInvestmentView) -> PreparedCardHtml:
    assets = await _prepare_assets(_investment_asset_urls(view), inline=False)
    return PreparedCardHtml(_render_detail_html(view, assets.urls), assets.resources, ACCOUNT_INVESTMENT_CARD_WIDTH)


async def render_account_investment_summary_html(view: AccountInvestmentView) -> str:
    return (await _prepare_inline(view, summary=True)).html


async def render_account_investment_detail_html(view: AccountInvestmentView) -> str:
    return (await _prepare_inline(view, summary=False)).html


async def _prepare_inline(view: AccountInvestmentView, *, summary: bool) -> PreparedCardHtml:
    assets = await _prepare_assets(_investment_asset_urls(view), inline=True)
    html = _render_summary_html(view, assets.urls) if summary else _render_detail_html(view, assets.urls)
    return PreparedCardHtml(html, assets.resources, ACCOUNT_INVESTMENT_CARD_WIDTH)


def _investment_asset_urls(view: AccountInvestmentView) -> list[str]:
    return [
        *[resource.icon_url for resource in view.resources],
        *[entry.portrait_url for entry in view.contributions],
    ]


def _render_summary_html(view: AccountInvestmentView, icon_map: dict[str, str]) -> str:
    categories = "".join(_category_card(category.label, category.stamina, category.note) for category in view.categories)
    missing = _missing_note(view)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head><body>
<div class="account-investment-summary">
  {_header(view, "账号养成统计")}
  <section class="investment-section">
    <div class="section-head"><h2>投入总览</h2><span>{esc(view.total_label)} · 仅统计当前档案可见对象</span></div>
    <div class="investment-stat-grid">
      {_stat("折金票", _number(view.gold), "等级、技能与突破")}
      {_stat("干员经验", _number(view.character_exp), "按经验值统计")}
      {_stat("武器经验", _number(view.weapon_exp), "按经验值统计")}
      {_stat("可折算理智", _stamina(view.stamina), "理论最低等价")}
      {_stat("统计对象", f"{view.operator_count} / {view.equipped_weapon_count}", "干员 / 已装备武器")}
      {_stat("数据覆盖", view.coverage_label, "静态成本表映射")}
    </div>
  </section>
  <section class="investment-section">
    <div class="section-head"><h2>五类投入</h2><span>排行仅使用可折算理智</span></div>
    <div class="investment-category-grid">{categories}</div>
  </section>
  <section class="investment-section investment-notes">
    <div class="section-head"><h2>口径说明</h2><span>AKEData {esc(_text(view.source_revision))}</span></div>
    <p>不含潜能、武器精炼、装备价值、未装备武器与历史替换投入；经验显示为经验值，不还原实际经验卡组合。</p>
    <p>{esc(missing)}</p>
  </section>
  {_footer(view)}
</div></body></html>"""


def _render_detail_html(view: AccountInvestmentView, icon_map: dict[str, str]) -> str:
    resources = "".join(_resource_tile(resource, icon_map) for resource in view.resources)
    resource_body = resources or '<div class="empty">当前档案没有可展示的资源投入</div>'
    rankings = "".join(_ranking_row(item, icon_map) for item in view.contributions)
    ranking_body = rankings or '<div class="empty">暂无可折算理智的干员排行</div>'
    missing = _missing_note(view)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head><body>
<div class="account-investment-detail">
  {_header(view, "材料明细 / 干员排行")}
  <section class="investment-section detail-section">
    <div class="section-head"><h2>材料明细</h2><span>原始配方数量 · 经验已在汇总卡单列</span></div>
    <div class="resource-groups">
      <div class="resource-group"><h3>可折算理智材料</h3><div class="resource-grid">{_group_resources(view.resources, icon_map, True)}</div></div>
      <div class="resource-group"><h3>非理智 / 稀有材料</h3><div class="resource-grid">{_group_resources(view.resources, icon_map, False)}</div></div>
    </div>
    {'' if resources else resource_body}
  </section>
  <section class="investment-section ranking-section">
    <div class="section-head"><h2>前 10 干员排行</h2><span>按可折算理智从高到低</span></div>
    <div class="ranking-head"><span>干员</span><span>本体</span><span>技能</span><span>武器</span><span>合计</span></div>
    <div class="ranking-list">{ranking_body}</div>
  </section>
  <section class="investment-section investment-notes"><p>{esc(missing)}</p></section>
  {_footer(view)}
</div></body></html>"""


def _header(view: AccountInvestmentView, title: str) -> str:
    badge = f"{view.operator_count} 位干员 · {view.equipped_weapon_count} 把已装备武器"
    identity = " · ".join(part for part in (f"UID {view.uid or '--'}", view.server_name, f"数据更新 {view.saved_at or '--'}") if part)
    return (
        '<header class="investment-header">'
        f'<div class="investment-title-row"><div><h1>{esc(title)}</h1><div class="investment-nickname">{esc(view.nickname)}</div></div>'
        f'<div class="investment-badge">{esc(badge)}</div></div>'
        f'<div class="investment-subtitle">{esc(identity)}</div>'
        '</header>'
    )


def _footer(view: AccountInvestmentView) -> str:
    server_name = view.server_name or "未知服务器"
    return (
        '<footer class="investment-footer">'
        f'<span>档案 {esc(view.saved_at or "--")} · AKEData {esc(view.source_revision)}</span>'
        f'<span>未装备武器不计 · 非理智材料保留原始数量 · {esc(server_name)}</span>'
        '</footer>'
    )


def _stat(label: str, value: str, note: str) -> str:
    return f'<div class="investment-stat"><span>{esc(label)}</span><b>{esc(value)}</b><i>{esc(note)}</i></div>'


def _category_card(label: str, stamina: float, note: str) -> str:
    return (
        '<article class="investment-category">'
        f'<span>{esc(label)}</span><b>{esc(_stamina(stamina))}</b><i>{esc(note or "可折算理智")}</i>'
        f'<div class="category-bar"><em style="width:{_bar_width(stamina)}%"></em></div>'
        '</article>'
    )


def _group_resources(resources: tuple[InvestmentResourceView, ...], icon_map: dict[str, str], farmable: bool) -> str:
    selected = [item for item in resources if (item.stamina_cost is not None) == farmable]
    return "".join(_resource_tile(item, icon_map) for item in selected) or '<div class="empty">暂无</div>'


def _resource_tile(resource: InvestmentResourceView, icon_map: dict[str, str]) -> str:
    icon = _gallery_image(icon_map, resource.icon_url, "resource-icon", resource.name)
    icon = icon or '<span class="resource-icon-fallback">材</span>'
    # Rates are normalized to one actual material item (not one dungeon run).
    # Protocol Space's fixed reward is six items per 80-stamina run, so the
    # rendered unit must be 13.3 理智/个 rather than the raw 80 理智/run.
    unit = f"{resource.stamina_cost:.1f} 理智/个" if resource.stamina_cost is not None else "暂无可靠理智价"
    return (
        '<article class="investment-resource">'
        f'<div class="resource-icon-frame">{icon}</div><div class="resource-copy">'
        f'<b>{esc(resource.name)}</b><span>{esc(resource.item_id)}</span></div>'
        f'<strong>×{_number(resource.count)}</strong><small>{esc(unit)}</small>'
        '</article>'
    )


def _ranking_row(item: InvestmentContributionView, icon_map: dict[str, str]) -> str:
    portrait = _gallery_image(icon_map, item.portrait_url, "ranking-portrait", item.name)
    portrait = portrait or '<span class="ranking-portrait-fallback">人</span>'
    note = f' · {"、".join(item.missing[:2])}' if item.missing else ""
    return (
        '<article class="investment-ranking-row">'
        f'<div class="ranking-identity"><div class="ranking-portrait-frame">{portrait}</div>'
        f'<div><b>{esc(item.name)}</b><span>{item.rarity}★{esc(note)}</span></div></div>'
        f'<strong>{esc(_stamina(item.body_stamina))}</strong>'
        f'<strong>{esc(_stamina(item.skill_stamina))}</strong>'
        f'<strong>{esc(_stamina(item.weapon_stamina))}</strong>'
        f'<strong class="ranking-total">{esc(_stamina(item.total_stamina))}</strong>'
        '</article>'
    )


def _missing_note(view: AccountInvestmentView) -> str:
    if not view.missing:
        return "数据已完整映射；非理智材料仍按原始数量展示，不参与理智排行。"
    labels = "、".join(view.missing[:6])
    suffix = "……" if len(view.missing) > 6 else ""
    return f"{view.coverage_label}；以下对象未计入完整成本：{labels}{suffix}。总量为已知投入至少。"


def _bar_width(value: float) -> int:
    return max(2, min(100, int(round(value / 500 * 100))))


def _number(value: int | float) -> str:
    return f"{value:,}"


def _stamina(value: float) -> str:
    return f"{value:,.1f} 理智"


def _text(value: str) -> str:
    return str(value or "--")


def _css() -> str:
    return f"""
* {{ box-sizing:border-box; }}
html,body {{ margin:0; width:{ACCOUNT_INVESTMENT_CARD_WIDTH}px; min-height:760px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }}
.account-investment-summary,.account-investment-detail {{ width:{ACCOUNT_INVESTMENT_CARD_WIDTH}px; min-height:760px; padding:30px; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }}
.investment-header {{ padding:24px 28px 20px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); box-shadow:0 12px 32px rgba(23,27,31,.10); }}
.investment-title-row {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }}
.investment-title-row h1 {{ margin:0; font-size:36px; line-height:1; font-weight:950; letter-spacing:-.035em; }}
.investment-nickname {{ margin-top:10px; color:#5f6a70; font-size:23px; font-weight:900; }}
.investment-badge {{ flex:0 0 auto; padding:9px 14px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:17px; font-weight:950; }}
.investment-subtitle {{ margin-top:13px; color:#667077; font-size:15px; font-weight:850; }}
.investment-section {{ margin-top:16px; padding:14px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); overflow:visible; }}
.section-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:12px; padding-bottom:9px; border-bottom:4px solid #20262a; }}
.section-head h2 {{ margin:0; font-size:24px; font-weight:950; }}
.section-head span {{ color:#697277; font-size:13px; font-weight:850; }}
.investment-stat-grid {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }}
.investment-stat {{ min-height:94px; padding:12px 14px; border:1px solid #abb2b5; border-left:6px solid #20262a; background:rgba(255,255,255,.88); }}
.investment-stat span,.investment-stat i {{ display:block; color:#6b7479; font-size:12px; font-weight:900; font-style:normal; }}
.investment-stat b {{ display:block; margin-top:6px; font-size:22px; line-height:1.1; font-weight:950; overflow-wrap:anywhere; }}
.investment-stat i {{ margin-top:5px; color:#8c959a; font-size:11px; }}
.investment-category-grid {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }}
.investment-category {{ min-height:118px; padding:13px 14px; border:1px solid #abb2b5; background:#f4f6f5; }}
.investment-category span,.investment-category i {{ display:block; color:#647077; font-size:13px; font-weight:900; font-style:normal; }}
.investment-category b {{ display:block; margin-top:7px; color:#20252a; font-size:23px; font-weight:950; }}
.investment-category i {{ margin-top:5px; color:#8a9499; font-size:11px; }}
.category-bar {{ height:6px; margin-top:13px; background:#d8dddf; }}
.category-bar em {{ display:block; height:100%; background:#286cd6; }}
.investment-notes p {{ margin:7px 0 0; color:#687379; font-size:13px; font-weight:800; line-height:1.55; }}
.investment-footer {{ margin-top:18px; padding:12px 14px; display:flex; justify-content:space-between; border-top:3px solid #20252a; color:#6c757b; font-size:13px; font-weight:850; }}
.detail-section {{ min-height:350px; }}
.resource-groups {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.resource-group {{ min-width:0; }}
.resource-group h3 {{ margin:0 0 9px; padding-left:9px; border-left:5px solid #286cd6; color:#39444a; font-size:16px; font-weight:950; }}
.resource-group:nth-child(2) h3 {{ border-left-color:#d59800; }}
.resource-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; align-items:stretch; }}
.investment-resource {{ min-width:0; display:grid; grid-template-columns:46px minmax(0,1fr) auto; grid-template-rows:auto auto; column-gap:8px; align-items:center; padding:8px; border:1px solid rgba(23,27,31,.18); background:#f4f6f5; }}
.resource-icon-frame {{ grid-row:1 / span 2; width:46px; height:46px; display:grid; place-items:center; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); border-bottom:4px solid #9aa2a5; }}
.resource-icon {{ width:100%; height:100%; object-fit:contain; }}
.resource-icon-fallback {{ color:#8a9499; font-weight:950; }}
.resource-copy {{ min-width:0; }}
.resource-copy b {{ display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#2c373d; font-size:13px; font-weight:950; }}
.resource-copy span {{ display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#929b9f; font-size:9px; font-weight:800; }}
.investment-resource strong {{ align-self:center; color:#20252a; font-size:16px; font-weight:950; }}
.investment-resource small {{ grid-column:2 / span 2; color:#7d878c; font-size:10px; font-weight:850; }}
.ranking-section {{ min-height:300px; }}
.ranking-head,.investment-ranking-row {{ display:grid; grid-template-columns:minmax(0,1fr) 130px 130px 130px 140px; gap:10px; align-items:center; }}
.ranking-head {{ padding:0 12px 8px; color:#788389; font-size:12px; font-weight:950; text-align:right; }}
.ranking-head span:first-child {{ text-align:left; }}
.ranking-list {{ display:flex; flex-direction:column; gap:7px; }}
.investment-ranking-row {{ min-height:62px; padding:8px 12px; border:1px solid rgba(23,27,31,.20); border-left:6px solid #286cd6; background:#f4f6f5; text-align:right; }}
.ranking-identity {{ min-width:0; display:flex; align-items:center; gap:10px; text-align:left; }}
.ranking-portrait-frame {{ flex:0 0 auto; width:46px; height:46px; display:grid; place-items:center; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); }}
.ranking-portrait {{ width:100%; height:100%; object-fit:contain; }}
.ranking-portrait-fallback {{ color:#8a9499; font-weight:950; }}
.ranking-identity b {{ display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#2c373d; font-size:16px; font-weight:950; }}
.ranking-identity span {{ display:block; margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#7c878c; font-size:10px; font-weight:850; }}
.investment-ranking-row > strong {{ color:#303a40; font-size:16px; font-weight:950; }}
.investment-ranking-row .ranking-total {{ color:#bd7600; }}
.empty {{ padding:15px; border:1px dashed #9aa2a5; background:#edf0f0; color:#71797d; font-size:14px; font-weight:750; }}
"""
