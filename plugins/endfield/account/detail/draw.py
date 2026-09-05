from __future__ import annotations

from dataclasses import replace

from loguru import logger

from .models import (
    ACCOUNT_DETAIL_PAGE_BUDGETS,
    ACCOUNT_DETAIL_PAGE_LIMIT,
    AccountDetailView,
    AccountEquipView,
    AccountOperatorView,
    AccountSkillView,
)
from ...rendering.cards import (
    PreparedCardHtml,
    _draw_gallery_catalog,
    _gallery_image,
    _prepare_assets,
    esc,
    esc_attr,
    is_height_limit_error,
    normalize_rich_color,
)


ACCOUNT_DETAIL_CARD_WIDTH = 1550
MAX_POTENTIAL_LEVEL = 5

_OVERFLOW_SELECTORS = (
    ".gallery-header",
    ".stat-cell",
    ".account-section",
    ".account-operator",
    ".account-cell",
    ".account-skill",
)

_EQUIP_RARITY_COLORS = {5: "#c88a00", 4: "#7446bc", 3: "#2874b8"}
_RARITY_COLORS = {6: "#ff3c2e", 5: "#e59a18", 4: "#8a56d6", 3: "#2d82c9"}
_SKILL_ORDER = ("普攻", "战技", "连携", "终结")
_SLOT_LABELS = ("护甲", "护手", "配件Ⅰ", "配件Ⅱ", "道具")
_MASTERY_UNIT_POINTS = (
    ("mastery-left", "156.01,449.99 243.22,325.05 377.80,324.95 424.37,450.01 377.85,575.03 243.17,574.96"),
    ("mastery-bottom", "597.00,704.61 445.15,691.59 377.85,575.03 462.80,472.17 594.35,450.03 661.65,566.59"),
    ("mastery-right", "596.99,195.39 661.63,333.36 594.35,450.03 462.83,427.81 377.80,324.95 445.18,208.45"),
)


async def draw_account_detail_card(view: AccountDetailView) -> bytes:
    return await _draw_gallery_catalog(
        await prepare_account_detail_card_html(view),
        ".account-detail-card",
        _OVERFLOW_SELECTORS,
        "account_detail",
    )


async def draw_account_detail_cards(view: AccountDetailView) -> tuple[bytes, ...]:
    """One image for a normal roster; several once it grows past what one image can show.

    Splitting happens on operator count first and on the height ceiling second: a roster
    only grows, so the card must degrade into extra images rather than either fail or
    hand back a picture too tall and too heavy to read.
    """
    last_error: RuntimeError | None = None
    if len(view.operators) <= ACCOUNT_DETAIL_PAGE_LIMIT:
        try:
            return (await draw_account_detail_card(view),)
        except RuntimeError as exc:
            if not is_height_limit_error(exc):
                raise
            last_error = exc

    for operator_budget in ACCOUNT_DETAIL_PAGE_BUDGETS:
        pages = _paginate_operators(view, operator_budget)
        try:
            rendered = [await draw_account_detail_card(page) for page in pages]
        except RuntimeError as exc:
            if not is_height_limit_error(exc):
                raise
            last_error = exc
            continue
        logger.info(
            f"[endfield] account detail paginated pages={len(rendered)} "
            f"operators={len(view.operators)} operator_budget={operator_budget}"
        )
        return tuple(rendered)
    raise last_error or RuntimeError("账号详情分页失败")


def _paginate_operators(
    view: AccountDetailView, operator_budget: int
) -> tuple[AccountDetailView, ...]:
    """Slice the roster into pages; only the first keeps the account stat strip."""
    operators = view.operators
    chunks = [
        operators[start : start + operator_budget]
        for start in range(0, len(operators), operator_budget)
    ] or [()]
    return tuple(
        replace(
            view,
            operators=chunk,
            stats=view.stats if index == 0 else (),
            page_number=index + 1,
            page_count=len(chunks),
            roster_count=len(operators),
        )
        for index, chunk in enumerate(chunks)
    )


async def prepare_account_detail_card_html(view: AccountDetailView) -> PreparedCardHtml:
    return await _prepare_account_detail_html(view, inline=False)


async def render_account_detail_card_html(view: AccountDetailView) -> str:
    return (await _prepare_account_detail_html(view, inline=True)).html


async def _prepare_account_detail_html(view: AccountDetailView, *, inline: bool) -> PreparedCardHtml:
    prepared = await _prepare_assets(_account_detail_icon_urls(view), inline=inline)
    return PreparedCardHtml(
        _render_account_detail_html(view, prepared.urls),
        prepared.resources,
        ACCOUNT_DETAIL_CARD_WIDTH,
    )


def _account_detail_icon_urls(view: AccountDetailView) -> list[str]:
    urls = [view.avatar_url]
    for operator in view.operators:
        urls.append(operator.portrait_url)
        urls.append(operator.tactical_icon_url)
        urls.extend(skill.icon_url for skill in operator.skills)
        if operator.weapon is not None:
            urls.append(operator.weapon.icon_url)
        urls.extend(equip.icon_url for equip in operator.equips if equip is not None)
    return [url for url in urls if url]


def _render_account_detail_html(view: AccountDetailView, icon_map: dict[str, str]) -> str:
    css = _account_detail_css(view)
    rows = "".join(_operator_row(operator, icon_map) for operator in view.operators)
    body = (
        '<div class="account-columns"><span>属性 / 等级</span><span>武器</span>'
        f'<span>技能 · {esc(" / ".join(_SKILL_ORDER))}</span>'
        f'<span>装备 · {esc(" / ".join(_SLOT_LABELS))}</span></div>'
        f'<div class="account-rows">{rows}</div>'
        if rows
        else '<div class="empty">该账号暂无可展示的干员数据</div>'
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="account-detail-card">
  <header class="gallery-header">
    <div class="gallery-title-row">
      <div class="gallery-identity">{_avatar(view, icon_map)}<div class="gallery-title">{esc(view.nickname)}</div></div>
      <div class="gallery-count">{esc(_count_badge(view))}</div>
    </div>
    <div class="gallery-subtitle">{esc(_identity_line(view))}</div>
    {f'<div class="stat-strip">{_stat_cells(view)}</div>' if view.stats else ''}
  </header>
  <section class="account-section">
    <div class="section-head">
      <h2>干员数据</h2>
      <span>{esc(_roster_note(view))}</span>
    </div>
    {body}
  </section>
  <footer class="gallery-footer"><span>数据来源 森空岛</span><span>数据更新 {esc(view.saved_at or "--")}</span></footer>
</div></body></html>"""


def _identity_line(view: AccountDetailView) -> str:
    parts = [f"UID {view.uid or '--'}"]
    if view.server_name:
        parts.append(view.server_name)
    parts.append(f"权限等级 {_number(view.level)}")
    parts.append(f"探索等级 {_number(view.world_level)}")
    if view.main_mission:
        parts.append(f"主线「{view.main_mission}」")
    return " · ".join(parts)


def _count_badge(view: AccountDetailView) -> str:
    if view.page_count > 1:
        return f"{view.operator_count} 位干员 · 第 {view.page_number} / {view.page_count} 张"
    return f"{view.operator_count} 位干员"


def _roster_note(view: AccountDetailView) -> str:
    note = f"按稀有度、等级与干员编号排序，共 {view.operator_count} 位"
    if view.page_count > 1:
        note += f"，本张 {view.page_operator_count} 位"
    return note


def _avatar(view: AccountDetailView, icon_map: dict[str, str]) -> str:
    avatar = _gallery_image(icon_map, view.avatar_url, "gallery-avatar-image", view.nickname)
    inner = avatar or '<span class="gallery-avatar-fallback">管理员</span>'
    return f'<div class="gallery-avatar">{inner}</div>'


def _stat_cells(view: AccountDetailView) -> str:
    return "".join(
        f'<div class="stat-cell"><span>{esc(stat.label)}</span><b>{esc(stat.value)}</b>'
        f'<i>{esc(stat.note)}</i></div>'
        for stat in view.stats
    )


def _operator_row(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    element_color = esc_attr(normalize_rich_color(operator.element_color))
    return (
        f'<article class="account-operator rarity-{operator.rarity}" style="--element-color:{element_color}">'
        f'{_identity_cell(operator, icon_map)}'
        f'{_weapon_cell(operator, icon_map)}'
        f'{_skills_cell(operator, icon_map)}'
        f'{_equips_cell(operator, icon_map)}'
        '</article>'
    )


def _identity_cell(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    portrait = _gallery_image(icon_map, operator.portrait_url, "", operator.name)
    if not portrait:
        portrait = '<div class="account-op-portrait-fallback">暂无头像</div>'
    tags = [f'<span class="account-tag account-tag-element">{esc(operator.element)}</span>'] if operator.element else []
    tags += [
        f'<span class="account-tag">{esc(label)}</span>'
        for label in (operator.profession, operator.weapon_type)
        if label
    ]
    tags_html = "".join(tags) or '<span class="account-tag">--</span>'
    return (
        '<div class="account-cell account-cell-identity">'
        f'<div class="account-op-portrait">{portrait}'
        f'<span class="account-op-level">Lv.{_number(operator.level)}</span></div>'
        '<div class="account-op-identity">'
        f'<div class="account-op-name-row"><span class="account-op-name">{esc(operator.name)}</span>'
        f'<span class="rarity-chip">{operator.rarity}★</span></div>'
        f'<div class="account-op-tags">{tags_html}</div>'
        f'{_progress_row(operator)}'
        '</div></div>'
    )


def _progress_row(operator: AccountOperatorView) -> str:
    filled = max(0, min(MAX_POTENTIAL_LEVEL, operator.potential_level or 0))
    pips = "".join(
        f'<i class="account-pip{" on" if index < filled else ""}"></i>'
        for index in range(MAX_POTENTIAL_LEVEL)
    )
    return (
        '<div class="account-op-progress">'
        f'<span class="account-progress-label">潜能</span>'
        f'<span class="account-pips">{pips}</span>'
        f'<b>{_number(operator.potential_level)}</b>'
        '</div>'
    )


def _weapon_cell(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    weapon = operator.weapon
    if weapon is None:
        return '<div class="account-cell account-cell-weapon"><span class="account-blank">未装备武器</span></div>'
    icon = _gallery_image(icon_map, weapon.icon_url, "", weapon.name)
    detail = " · ".join(
        part
        for part in (
            f"Lv.{_number(weapon.level)}",
            f"潜能 {weapon.potential_level}" if weapon.potential_level is not None else "",
        )
        if part
    )
    accent = esc_attr(_RARITY_COLORS.get(weapon.rarity, "#9aa2a5"))
    return (
        '<div class="account-cell account-cell-weapon">'
        f'<div class="account-weapon-frame" style="border-bottom-color:{accent}">{icon or "<span>--</span>"}</div>'
        '<div class="account-weapon-main">'
        f'<div class="account-weapon-name">{esc(weapon.name or "--")}</div>'
        f'<div class="account-weapon-meta">{esc(detail)}</div>'
        '</div>'
        '</div>'
    )


def _skills_cell(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    if not operator.skills:
        return '<div class="account-cell account-cell-skills"><span class="account-blank">技能数据缺失</span></div>'
    chips = "".join(_skill_chip(skill, icon_map) for skill in operator.skills)
    return f'<div class="account-cell account-cell-skills">{chips}</div>'


def _skill_chip(skill: AccountSkillView, icon_map: dict[str, str]) -> str:
    icon = _gallery_image(icon_map, skill.icon_url, "", skill.name)
    title = " · ".join(
        part
        for part in (
            skill.name,
            skill.type_label,
            skill.damage_type,
            _skill_level_label(skill.level),
        )
        if part
    )
    ultimate = " ultimate" if skill.is_ultimate else ""
    color = esc_attr(_skill_color(skill.damage_color))
    mastery = skill.mastery_level
    return (
        f'<div class="account-skill{ultimate}" style="--skill-color:{color}" title="{esc_attr(title)}">'
        f'<span class="account-skill-icon">{icon}</span>'
        f'{_skill_progress_marker(skill.level, mastery)}</div>'
    )


def _skill_progress_marker(level: int | None, mastery: int) -> str:
    label = _skill_level_label(level)
    if level is not None and level < 9:
        return f'<span class="account-skill-level">{esc(label)}</span>'
    return _mastery_marker(mastery, label)


def _mastery_marker(mastery: int, label: str) -> str:
    units = "".join(
        f'<polygon class="mastery-unit {class_name}" points="{points}"></polygon>'
        for class_name, points in _MASTERY_UNIT_POINTS
    )
    return (
        f'<span class="account-skill-mastery mastery-{mastery}" aria-label="{esc_attr(label)}">'
        f'<svg viewBox="130 170 560 560" aria-hidden="true">{units}</svg></span>'
    )


def _skill_level_label(level: int | None) -> str:
    if level is None:
        return ""
    if level >= 12:
        return "M3"
    if level >= 10:
        return f"M{level - 9}"
    return f"Lv{level}"


def _skill_color(value: str) -> str:
    color = str(value or "").strip()
    if len(color) == 7 and color.startswith("#") and all(character in "0123456789abcdefABCDEF" for character in color[1:]):
        return color.lower()
    return "#969a99"


def _equips_cell(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    slots = [
        _equip_slot(equip, label, icon_map)
        for equip, label in zip(operator.equips, _SLOT_LABELS)
    ]
    slots.append(_tactical_slot(operator, icon_map))
    return (
        '<div class="account-cell account-cell-equips">'
        f'<div class="account-slots">{"".join(slots)}</div>'
        f'{_suit_summary(operator)}</div>'
    )


def _equip_slot(equip: AccountEquipView | None, label: str, icon_map: dict[str, str]) -> str:
    if equip is None:
        return f'<div class="account-slot empty"><span>{esc(label)}</span></div>'
    icon = _gallery_image(icon_map, equip.icon_url, "", equip.name)
    color = esc_attr(_EQUIP_RARITY_COLORS.get(equip.rarity, "#9aa2a5"))
    title = " · ".join(part for part in (equip.name, equip.suit_name, equip.type_label) if part)
    return (
        f'<div class="account-slot" style="border-top-color:{color}" title="{esc_attr(title)}">'
        f'{icon or f"<span>{esc(equip.slot_label)}</span>"}</div>'
    )


def _tactical_slot(operator: AccountOperatorView, icon_map: dict[str, str]) -> str:
    if not operator.tactical_icon_url and not operator.tactical_name:
        return '<div class="account-slot empty tactical"><span>道具</span></div>'
    icon = _gallery_image(icon_map, operator.tactical_icon_url, "", operator.tactical_name)
    return (
        f'<div class="account-slot tactical" title="{esc_attr(operator.tactical_name)}">'
        f'{icon or "<span>道具</span>"}</div>'
    )


def _suit_summary(operator: AccountOperatorView) -> str:
    counts: dict[str, int] = {}
    for equip in operator.equips:
        if equip is not None and equip.suit_name:
            counts[equip.suit_name] = counts.get(equip.suit_name, 0) + 1
    if not counts:
        return '<div class="account-suits account-suits-empty">未成套</div>'
    chips = "".join(
        f'<span class="account-suit">{esc(name)} ×{count}</span>'
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    return f'<div class="account-suits">{chips}</div>'


def _number(value: int | None) -> str:
    return "--" if value is None else str(value)


def _account_detail_css(view: AccountDetailView) -> str:
    css = f"""
* {{ box-sizing:border-box; }}
html,body {{ margin:0; width:{ACCOUNT_DETAIL_CARD_WIDTH}px; min-height:680px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }}
.account-detail-card {{ width:{ACCOUNT_DETAIL_CARD_WIDTH}px; min-height:680px; padding:30px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }}
.gallery-header {{ padding:24px 28px 20px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); box-shadow:0 12px 32px rgba(23,27,31,.10); }}
.gallery-title-row {{ display:flex; align-items:center; justify-content:space-between; gap:20px; }}
.gallery-identity {{ display:flex; align-items:center; gap:16px; min-width:0; }}
.gallery-avatar {{ flex:0 0 auto; width:72px; height:72px; display:grid; place-items:center; overflow:hidden; border:1px solid rgba(23,27,31,.22); background:radial-gradient(circle,#fff,#e5e8e9); }}
.gallery-avatar-image {{ width:100%; height:100%; object-fit:cover; }}
.gallery-avatar-fallback {{ color:#8a9297; font-size:13px; font-weight:900; }}
.gallery-title {{ font-size:50px; line-height:1; font-weight:950; letter-spacing:-.035em; overflow-wrap:anywhere; }}
.gallery-count {{ flex:0 0 auto; padding:8px 14px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:17px; font-weight:950; }}
.gallery-subtitle {{ margin-top:12px; color:#667077; font-size:16px; font-weight:800; }}
.stat-strip {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; margin-top:16px; }}
.stat-cell {{ min-height:86px; padding:12px 14px; border:1px solid #abb2b5; border-left:6px solid #20262a; background:rgba(255,255,255,.88); }}
.stat-cell span {{ display:block; color:#6b7479; font-size:13px; font-weight:900; }}
.stat-cell b {{ display:block; margin-top:5px; font-size:22px; font-weight:950; overflow-wrap:anywhere; }}
.stat-cell i {{ display:block; margin-top:3px; color:#8c959a; font-size:11px; font-style:normal; font-weight:850; }}
.account-section {{ margin-top:16px; padding:14px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); overflow:visible; }}
.section-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:12px; padding-bottom:9px; border-bottom:4px solid #20262a; }}
.section-head h2 {{ margin:0; font-size:25px; font-weight:950; }}
.section-head span {{ color:#697277; font-size:14px; font-weight:850; }}
.account-columns,.account-operator {{ display:grid; grid-template-columns:{view.identity_column}px 210px 340px minmax(0,1fr); gap:14px; align-items:center; }}
.account-columns {{ padding:0 12px 8px; color:#7a838a; font-size:14px; font-weight:900; }}
.account-rows {{ display:flex; flex-direction:column; gap:8px; }}
.account-operator {{ --element-color:#888; padding:12px; overflow:visible; border:1px solid rgba(23,27,31,.20); border-left:7px solid var(--element-color); background:#f4f6f5; box-shadow:inset 0 4px 0 #20262a; }}
.account-cell {{ min-width:0; display:flex; align-items:center; gap:12px; }}
.account-blank {{ color:#949b9f; font-size:13px; font-weight:900; }}
.account-op-portrait {{ position:relative; flex:0 0 auto; width:{view.portrait_size}px; height:{view.portrait_size}px; display:grid; place-items:center; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); }}
.account-op-portrait img {{ width:100%; height:100%; object-fit:contain; }}
.account-op-portrait-fallback {{ color:#8b9297; font-size:11px; font-weight:900; }}
.account-op-level {{ position:absolute; left:0; bottom:0; padding:2px 6px; background:rgba(23,27,31,.86); color:#fff; font-size:12px; font-weight:950; }}
.account-op-identity {{ min-width:0; flex:1 1 auto; }}
.account-op-name-row {{ display:flex; align-items:center; gap:9px; }}
.account-op-name {{ min-width:0; font-size:26px; line-height:1.1; font-weight:950; overflow-wrap:anywhere; }}
.rarity-chip {{ flex:0 0 auto; padding:3px 6px; background:#20252a; color:#ffd55a; font-size:12px; font-weight:950; }}
.account-op-tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:7px; }}
.account-tag {{ padding:3px 7px; background:#e4e8ea; color:#4d575e; font-size:12px; font-weight:900; }}
.account-tag-element {{ border-left:4px solid var(--element-color); }}
.account-op-progress {{ display:flex; align-items:center; gap:8px; margin-top:8px; color:#6b757b; font-size:12px; font-weight:900; }}
.account-pips {{ display:flex; gap:3px; }}
.account-pip {{ width:13px; height:13px; background:rgba(23,27,31,.13); }}
.account-pip.on {{ background:#286cd6; }}
.account-op-progress b {{ color:#286cd6; font-size:14px; font-weight:950; }}
.account-weapon-frame {{ flex:0 0 auto; width:64px; height:64px; display:grid; place-items:center; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); border-bottom:5px solid #9aa2a5; color:#9aa1a6; font-size:12px; font-weight:900; }}
.account-weapon-frame img {{ width:100%; height:100%; object-fit:contain; }}
.account-weapon-main {{ min-width:0; flex:1 1 auto; }}
.account-weapon-name {{ font-size:18px; line-height:1.15; font-weight:950; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.account-weapon-meta {{ margin-top:5px; color:#6e787e; font-size:12px; font-weight:850; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.account-cell-skills {{ gap:10px; }}
.account-skill {{ flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:5px; }}
.account-skill-icon {{ position:relative; width:{view.skill_icon_size}px; height:{view.skill_icon_size}px; display:grid; place-items:center; overflow:hidden; border-radius:50%; border:3px solid rgba(255,255,255,.96); background:#696d6f; box-shadow:0 0 0 2px rgba(23,27,31,.22),inset 0 0 16px rgba(0,0,0,.34); }}
.account-skill-icon::before {{ content:""; position:absolute; inset:0; background:conic-gradient(transparent 0 120deg,var(--skill-color) 120deg 240deg,transparent 240deg 360deg); }}
.account-skill.ultimate .account-skill-icon::before {{ background:var(--skill-color); }}
.account-skill-icon img {{ position:relative; z-index:1; width:88%; height:88%; object-fit:contain; filter:drop-shadow(0 1px 2px rgba(0,0,0,.28)); }}
.account-skill-mastery {{ position:relative; width:48px; height:25px; margin-top:-2px; display:grid; place-items:center; overflow:hidden; border-radius:13px; background:rgba(23,27,31,.28); }}
.account-skill-level {{ width:48px; height:25px; margin-top:-2px; display:grid; place-items:center; border-radius:13px; background:#555b5e; color:#fff; font-size:12px; font-weight:950; line-height:1; }}
.account-skill-mastery svg {{ width:23px; height:23px; overflow:hidden; transform:translateX(-1.5px); }}
.account-skill-mastery .mastery-unit {{ fill:#5f6365; }}
.account-skill-mastery.mastery-1 .mastery-left,
.account-skill-mastery.mastery-2 .mastery-left,
.account-skill-mastery.mastery-2 .mastery-bottom,
.account-skill-mastery.mastery-3 .mastery-unit {{ fill:#fff; }}
.account-cell-equips {{ gap:12px; }}
.account-slots {{ flex:0 0 auto; display:flex; gap:6px; }}
.account-slot {{ position:relative; flex:0 0 auto; width:{view.slot_size}px; height:{view.slot_size}px; display:grid; place-items:center; overflow:hidden; background:radial-gradient(circle,#fff,#e5e8e9); border-top:4px solid #9aa2a5; }}
.account-slot img {{ width:100%; height:100%; object-fit:contain; }}
.account-slot span {{ color:#949b9f; font-size:11px; font-weight:900; }}
.account-slot.empty {{ background:rgba(23,27,31,.05); border-top-color:#c3c9cb; }}
.account-slot.tactical {{ border-top-style:dashed; }}
.account-suits {{ flex:1 1 auto; min-width:0; display:flex; flex-wrap:wrap; align-content:center; gap:5px; color:#5b656b; font-size:12px; font-weight:900; }}
.account-suit {{ padding:2px 7px; background:#dfe3e4; border-left:3px solid #286cd6; }}
.account-suits-empty {{ padding:2px 7px; background:rgba(23,27,31,.045); border-left:3px solid #c3c9cb; color:#949b9f; }}
.empty {{ padding:16px; border:1px dashed #9aa2a5; background:#edf0f0; color:#71797d; font-size:16px; font-weight:750; }}
.gallery-footer {{ margin-top:18px; padding:12px 14px; display:flex; justify-content:space-between; border-top:3px solid #20252a; color:#6c757b; font-size:13px; font-weight:850; }}
"""
    if view.compact:
        css += """
.account-operator { padding:9px 12px; }
.account-rows { gap:6px; }
.account-op-name { font-size:22px; }
.account-op-tags { margin-top:5px; }
.account-op-progress { margin-top:6px; }
.account-weapon-frame { width:54px; height:54px; }
.account-weapon-name { font-size:16px; }
.account-skill b { font-size:13px; min-width:30px; }
"""
    return css
