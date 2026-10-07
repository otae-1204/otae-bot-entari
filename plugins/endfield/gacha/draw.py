"""终末地抽卡分析图 v3：重构寻访作侧栏的三栏（无重构记录时两栏）、无折叠，长度只靠分页。

    right（默认）：特许寻访 | 武器申领 | 重构寻访 + 其他寻访
    left：         重构寻访 + 其他寻访 | 特许寻访 | 武器申领
    无重构记录：   特许寻访 + 其他寻访 | 武器申领

每个池、每条记录（六星、赠礼、免费十连、加急招募）都完整渲染；长文本换行，不截断、不省略。
分页流程：先在浏览器里一次量出每个块的高度（池头、续页池头、每一行记录、栏头、分组标题），
再按栏装箱——池卡片只在两行记录之间拆分，续段标「（续）」；先求最少页数，再二分求统一栏高，
让各页高度均衡。布局知识（kind → 栏/组、保底格文案、排序、分页）全部在本模块，
后端字段约定与新增池类型的步骤见 docs/endfield_gacha_card_v3.md。
"""

from __future__ import annotations

import base64
import html
import mimetypes
import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import accumulate
from pathlib import Path
from time import perf_counter
from typing import Callable, Sequence

from loguru import logger

from otae_bot.infrastructure.rendering.browser import evaluate_web_page, screenshot_web_element
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup
from otae_bot.paths import PROJECT_ROOT

from ..account.i18n import server_label
from ..rendering import cards as _cards
from ..rendering.cards import (
    _draw_gacha_pool_banner,
    _pool_paid_total,
    _write_temp_html,
    is_height_limit_error,
    optimize_png_container,
)
from .models import FreePullBatch, GachaAnalysis, KeepsakeGift, PoolAnalysis, SixStarEvent, SixStarExpectation
from .pools import KINDS_BY_KEY, PoolKind, resolve_pool_kind
from .service import calculate_group_expectation, format_timestamp


GACHA_CARD_WIDTH = 1600               # 抽卡专用宽度（CSS px，2× 截图 = 3200 px）；其它卡片不受影响
GACHA_PAGE_MAX_HEIGHT = 4096          # 抽卡专用单页上限（CSS px）；其它卡片仍用 cards.CARD_MAX_HEIGHT
GACHA_RERUN_SIDE_ENV = "ENDFIELD_GACHA_RERUN_SIDE"
GACHA_RERUN_SIDES = ("right", "left")
GACHA_RERUN_SIDE_DEFAULT = "right"    # 主栏位置不随“有没有重构记录”变化（见 docs/endfield_gacha_card_v3.md）
SIDE_COLUMN_SHARE = 0.86              # 重构侧栏相对主栏（特许 / 武器）的宽度比例
GACHA_PAGE_SAFETY = 16                # 预测误差余量
GACHA_RETRY_SAFETY = 4 * GACHA_PAGE_SAFETY   # 截图意外超高时，用 4 倍余量重新装箱再试一次
GACHA_LEVEL_FLOOR = 400               # 二分统一栏高的下界
STACK_GAP = 10                        # .pool-stack gap
STACK_PAD = 10                        # .pool-stack padding
COLUMN_BORDER = 1                     # .pool-column border
SPLIT_MIN_ROWS = 4                    # 记录行 ≥ 4 的池卡片才允许跨页拆分
SPLIT_KEEP_ROWS = 2                   # 拆分后每一段至少保留 2 行
FREE_ICON_LIMIT = 3                   # 免费十连行最多叠放 3 个头像（名字全部写在标题里）
GACHA_OVERFLOW_SELECTORS = (".pool-column", ".pool-card", ".pull-row", ".free-row", "header")
# 抽卡卡片自带字体（只作用于本卡）：渲染结果不再取决于机器上装了哪些中文字体。共享浏览器拦截所有请求，
# 被拦截的 file:// 字体会以 net::ERR_FAILED 失败，所以与雷达 / 更新日志卡一样内嵌为 data: URL。
GACHA_FONT_FAMILY = "EndfieldGachaSans"
GACHA_FONT_DIR = PROJECT_ROOT / "plugins/endfield/assets/fonts"
GACHA_FONT_FILES = (
    (400, "HarmonyOS_Sans_SC_Regular.ttf"),
    (500, "HarmonyOS_Sans_SC_Medium.ttf"),
    (700, "HarmonyOS_Sans_SC_Bold.ttf"),
)
# 当期卡（CURRENT 标签、当前累计行、保底格）只给有共享保底链或累计奖励的类型；
# 其他寻访、常驻申领即使是该类最新一期，也按历史池样式展示（垫抽数仍在池头）。
FEATURED_KINDS = frozenset({"special", "rerun", "weapon_limited", "weapon_rerun"})


@dataclass(frozen=True)
class SectionSpec:
    kinds: tuple[str, ...]
    empty: str                  # 本组无卡池时的占位；空串 = 整组省略
    divider: str = ""           # 栏内分组标题；空 = 直接接在栏头下
    divider_kicker: str = ""
    show_kind: bool = True      # 池头副标题是否写池类型（特许寻访组不写）


@dataclass(frozen=True)
class ColumnSpec:
    key: str                    # special | rerun | weapon：总览格的顺序跟随栏序
    title: str
    kicker: str
    sections: tuple[SectionSpec, ...]
    share: float = 1.0          # 栏宽比例（fr）


SPECIAL_SECTION = SectionSpec(("special",), "暂无特许寻访记录", show_kind=False)
RERUN_SECTION = SectionSpec(("rerun",), "暂无重构寻访记录")
OTHER_SECTION = SectionSpec(("joint", "standard", "beginner", "unknown_char"), "",
                            divider="其他寻访", divider_kicker="角色池 · 各池独立保底")
WEAPON_SECTION = SectionSpec(("weapon_limited", "weapon_rerun", "weapon_constant", "unknown_weapon"), "暂无武器申领记录")
SPECIAL_COLUMN = ColumnSpec("special", "特许寻访", "角色池 · 特许间共享 80 抽保底", (SPECIAL_SECTION,))
RERUN_COLUMN = ColumnSpec("rerun", "重构寻访", "角色池 · 重构间共享 80 抽保底", (RERUN_SECTION, OTHER_SECTION),
                          share=SIDE_COLUMN_SHARE)
WEAPON_COLUMN = ColumnSpec("weapon", "武器申领", "武器池 · 限时 / 重构 / 常驻", (WEAPON_SECTION,))
GACHA_LAYOUTS = {
    "right": (SPECIAL_COLUMN, WEAPON_COLUMN, RERUN_COLUMN),
    "left": (RERUN_COLUMN, SPECIAL_COLUMN, WEAPON_COLUMN),
    # 没有重构池：两栏、不留空列；「其他寻访」同为角色池，接在特许寻访下方（与 v1 的角色 | 武器两栏一致）
    "two": (replace(SPECIAL_COLUMN, sections=(SPECIAL_SECTION, OTHER_SECTION)), WEAPON_COLUMN),
}
# 注册表新增池类型时必须在这里指定栏位，否则导入即失败（见 docs/endfield_gacha_card_v3.md）。
assert all(
    {kind for column in columns for section in column.sections for kind in section.kinds}
    == set(KINDS_BY_KEY) - ({"rerun"} if name == "two" else set())
    for name, columns in GACHA_LAYOUTS.items()
), "GACHA_LAYOUTS 的每种排法都必须覆盖 gacha/pools.py 注册表里的每一种 kind_key（两栏只在没有重构池时使用）"


# ============================================================ 布局模型：栏 → 分组 → 卡片

@dataclass(frozen=True, eq=False)
class GachaCard:
    key: str                    # 唯一卡片键（card_key）；跨页续段、data-row、校验都用它
    pool: PoolAnalysis
    kind: PoolKind
    featured: bool


@dataclass(eq=False)
class GachaSection:
    spec: SectionSpec
    cards: list[GachaCard]      # 当期卡在前，其余按系列最近抽取时间倒序（同系列各期相邻、新期在前）
    hidden: list[GachaCard] = field(default_factory=list)   # 按 GACHA_SHOW_STANDARD 隐藏的基础寻访


@dataclass(eq=False)
class GachaColumn:
    index: int
    spec: ColumnSpec
    sections: list[GachaSection]


def pool_kind(pool: PoolAnalysis) -> PoolKind:
    """以 kind_key 为准；为空（旧夹具）时交给注册表的识别函数兜底，前端不另写子串判断。"""
    return KINDS_BY_KEY.get(pool.kind_key) or resolve_pool_kind(
        pool_id=pool.pool_id, pool_name=pool.name, item_type=pool.item_type,
    )


def gacha_rerun_side() -> str:
    """重构侧栏放在哪一边：ENDFIELD_GACHA_RERUN_SIDE=right（默认）| left；无法识别时按默认处理。"""
    raw = os.getenv(GACHA_RERUN_SIDE_ENV, "").strip().casefold()
    if raw in GACHA_RERUN_SIDES:
        return raw
    if raw:
        logger.warning(f"[endfield-gacha] invalid {GACHA_RERUN_SIDE_ENV}={raw!r}, using {GACHA_RERUN_SIDE_DEFAULT}")
    return GACHA_RERUN_SIDE_DEFAULT


def build_gacha_columns(
    view: GachaAnalysis,
    *,
    show_standard: bool | None = None,
    rerun_side: str | None = None,
) -> list[GachaColumn]:
    """把全部池分栏：有重构池时三栏（``rerun_side`` 默认读 ENDFIELD_GACHA_RERUN_SIDE），没有时两栏。

    ``show_standard`` 默认取 ``view.show_standard_pools``（GACHA_SHOW_STANDARD）。
    """
    if show_standard is None:
        show_standard = bool(getattr(view, "show_standard_pools", True))
    side = rerun_side or gacha_rerun_side()
    if side not in GACHA_RERUN_SIDES:
        raise ValueError(f"rerun_side must be one of {GACHA_RERUN_SIDES}, got {side!r}")
    cards = _gacha_cards(view.pools)
    layout = GACHA_LAYOUTS[side if any(card.kind.key == "rerun" for card in cards) else "two"]
    series_latest: dict[str, int] = {}
    for card in cards:
        series = _series_of(card)
        series_latest[series] = max(series_latest.get(series, 0), card.pool.latest_ts)

    def history_order(card: GachaCard):
        series = _series_of(card)
        return (series_latest[series], series, card.pool.series_index, card.pool.latest_ts, card.pool.name)

    columns: list[GachaColumn] = []
    for index, spec in enumerate(layout):
        sections = []
        for section in spec.sections:
            members = [card for card in cards if card.kind.key in section.kinds]
            hidden = [card for card in members if card.kind.key == "standard" and not show_standard]
            visible = [card for card in members if card not in hidden]
            pinned = sorted(
                (card for card in visible if card.featured),
                key=lambda card: (card.kind.sort_rank, -card.pool.latest_ts),
            )
            rest = sorted((card for card in visible if not card.featured), key=history_order, reverse=True)
            sections.append(GachaSection(section, pinned + rest, hidden))
        columns.append(GachaColumn(index, spec, sections))
    return columns


def _gacha_cards(pools: Sequence[PoolAnalysis]) -> list[GachaCard]:
    cards: list[GachaCard] = []
    used: set[str] = set()
    for pool in pools:
        kind = pool_kind(pool)
        base = pool.card_key or f"{pool.pool_id}#{pool.pool_version}"
        key, suffix = base, 1
        while key in used:
            suffix += 1
            key = f"{base}@{suffix}"
        used.add(key)
        cards.append(GachaCard(key, pool, kind, bool(pool.is_current and kind.key in FEATURED_KINDS)))
    return cards


def _series_of(card: GachaCard) -> str:
    return card.pool.series_key or f"card:{card.key}"


# ============================================================ 记录行（每行带 data-row，供“无遗漏”校验）

def render_pool_rows(card: GachaCard) -> list[str]:
    """一个池的全部记录行：当前累计 → 六星与赠礼（池内位置新→旧）→ 免费十连 / 加急招募 → 空态。"""
    pool = card.pool
    scale = 40 if card.kind.item_type == "武器" else 80
    rows: list[str] = []

    def attrs(kind: str) -> str:
        return f'data-row="{_esc(card.key)}:{len(rows)}" data-kind="{kind}"'

    if card.featured:
        rows.append(_pull_row(None, pool.since_six_star, scale, attrs("current")))
    timeline = [(item.pool_position, item.gacha_ts, 0, "six", item) for item in pool.six_stars]
    timeline.extend((item.pool_position, item.gacha_ts, 1, "gift", item) for item in pool.keepsake_gifts)
    timeline.sort(key=lambda entry: entry[:3], reverse=True)
    for *_order, kind, item in timeline:
        if kind == "gift":
            rows.append(_gift_row(card, item, attrs("gift")))
        else:
            rows.append(_pull_row(item, item.interval, scale, attrs("six")))
    for batch in pool.free_batches:
        rows.append(_free_row(card, batch, attrs("free")))
    if not (pool.six_stars or pool.keepsake_gifts or pool.free_batches):
        rows.append(f'<div class="pool-empty" {attrs("empty")}>本池记录中尚无六星</div>')
    return rows


def _pull_row(item: SixStarEvent | None, pulls: int, scale: int, attrs: str) -> str:
    width = max(8.0, min(100.0, pulls / scale * 100 if scale else 0))
    if item is None:
        marker = '<div class="current-marker">至今</div>'
        copy = '<div class="pull-copy"><strong>当前累计</strong><time>距最近六星</time></div>'
        fill, hits = " current", ""
    else:
        marker = f'<div class="gacha-thumb">{_icon_html(item.icon_path, item.name, "6★")}</div>'
        position = f"第{item.pool_position}抽 · " if item.pool_position else ""
        copy = (f'<div class="pull-copy"><strong>{_esc(item.name)}</strong>'
                f'<time>{_esc(position + _date(item.gacha_ts))}</time></div>')
        chips = "".join(_pity_hit(label) for label in item.pity_labels)
        fill, hits = "", (f'<div class="pity-hits">{chips}</div>' if chips else "")
    return (f'<div class="pull-row" {attrs}>{marker}{copy}<div class="bar-track"><div class="bar-fill{fill}" '
            f'style="width:{width:.1f}%"></div><div class="bar-value"><b>{pulls} 抽</b>{hits}</div></div></div>')


def _pity_hit(label: str) -> str:
    kind = {
        "小保底": " pity-hit-guarantee pity-hit-small",
        "大保底": " pity-hit-guarantee pity-hit-large",
        "歪": " pity-hit-miss",
    }.get(label, "")
    return f'<span class="pity-hit{kind}">{_esc(label)}</span>'


GIFT_LABELS = {"keepsake": "信物", "weapon_box": "武库赠礼"}
GIFT_THUMBS = {"信物": "信物", "武库赠礼": "武库", "点绘赠礼": "点绘", "UP武器": "赠送"}
_LEGACY_GIFT_KINDS = {"信物": "keepsake", "武器": "up_weapon", "武库箱": "weapon_box"}


def gift_label(kind: PoolKind, gift: KeepsakeGift) -> str:
    """赠礼显示名由 (kind_key, gift_kind) 决定：点绘申领的 UP 武器叫「点绘赠礼」，其他武器池叫「UP武器」。"""
    gift_kind = gift.gift_kind or _LEGACY_GIFT_KINDS.get(gift.gift_type, "keepsake")
    if gift_kind == "up_weapon":
        return "点绘赠礼" if kind.key == "weapon_rerun" else "UP武器"
    return GIFT_LABELS.get(gift_kind, "信物")


def _gift_row(card: GachaCard, gift: KeepsakeGift, attrs: str) -> str:
    label = gift_label(card.kind, gift)
    if card.kind.item_type == "武器" and gift.claim_count:
        position = f"第{gift.claim_count}次申领"
        detail = f"累计{position}赠送{label}"
    elif card.kind.cumulative_scope == "series":
        position = f"第{gift.series_position or gift.pool_position}抽"
        detail = f"累计{position}赠送{label}"
    else:
        position = f"第{gift.pool_position}抽"
        detail = f"{position}赠送{label}"
    if gift.estimated:
        detail += " · 按统计推算"
    thumb = _icon_html(gift.icon_path, gift.name, GIFT_THUMBS.get(label, "赠送"))
    return (f'<div class="pull-row" {attrs}><div class="gacha-thumb gift">{thumb}</div>'
            f'<div class="pull-copy"><strong>{_esc(gift.name)}</strong><time>{_esc(detail)}</time></div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:100.0%"></div><div class="bar-value">'
            f'<b>{_esc(position)}</b><div class="pity-hits"><span class="pity-hit">赠送</span></div></div></div></div>')


def _free_row(card: GachaCard, batch: FreePullBatch, attrs: str) -> str:
    label = "加急招募" if batch.source == "rush" else "免费十连"
    if batch.six_stars:
        shown = batch.six_stars[:FREE_ICON_LIMIT]
        icons = "".join(
            f'<div class="gacha-thumb">{_icon_html(item.icon_path, item.name, "6★")}</div>' for item in shown
        )
        marker = f'<div class="free-icons{" many" if len(shown) > 2 else ""}">{icons}</div>'
        title = f"{label} · {'、'.join(item.name for item in batch.six_stars)}"
    else:
        marker = f'<div class="free-marker">FREE ×{batch.pull_count}</div>'
        title = f"{label} · 未出六星"
    if batch.source == "rush":
        note = f"累计 {batch.threshold} 抽获得 · 不计保底" if batch.threshold else "不计保底"
    elif batch.source == "free_ten" and card.kind.free_ten_unlock:
        note = f"累计 {card.kind.free_ten_unlock} 抽解锁 · 不计保底"
    else:
        note = "不受且不影响任何保底"
    count = "免费十连" if batch.pull_count == 10 else f"免费 {batch.pull_count} 抽"
    return (f'<div class="free-row" {attrs}>{marker}<div class="pull-copy"><strong>{_esc(title)}</strong>'
            f'<time>{_esc(_date(batch.gacha_ts))} · {_esc(note)}</time></div>'
            f'<span class="free-count">{_esc(count)}</span></div>')


# ============================================================ 保底格

@dataclass(frozen=True)
class PityCell:
    label: str
    value: str
    notes: tuple[str, ...] = ()


def build_pity_cells(card: GachaCard) -> list[PityCell]:
    """当期卡的保底格：特许 3 格；重构 2×2；限时武库 2 格；点绘申领 2×2；其余不显示。"""
    if not card.featured:
        return []
    pool, key = card.pool, card.kind.key
    if key == "special":
        return [
            _small_cell(pool, "距小保底", "特许间共享继承"),
            _large_cell(pool, "本池首个当期UP"),
            _keepsake_cell(pool, card.kind, series=False),
        ]
    if key == "rerun":
        return [
            _small_cell(pool, "距六星保底", "全部重构共享继承"),
            _large_cell(pool, "本期首个当期UP"),
            _rush_cell(pool),
            _keepsake_cell(pool, card.kind, series=True),
        ]
    if key == "weapon_limited":
        return [_weapon_small_cell(pool), _weapon_large_cell(pool, "本池当期UP")]
    if key == "weapon_rerun":
        return [
            _weapon_small_cell(pool),
            _weapon_large_cell(pool, "本期当期UP"),
            _reward_cell(pool, "weapon_box", "距武库赠礼"),
            _reward_cell(pool, "up_weapon", "距点绘赠礼"),
        ]
    return []


def _small_cell(pool: PoolAnalysis, label: str, scope: str) -> PityCell:
    limit, progress = pool.small_pity_limit, pool.small_pity_progress
    if not limit:
        return PityCell(label, "--", ("规则待识别",))
    notes = [f"进度 {progress}/{limit}", scope]
    if pool.soft_pity_active:
        notes.append(f"已进软保底（第 {pool.soft_pity_start} 抽起）")
    return PityCell(label, f"{max(0, limit - progress)} 抽", tuple(notes))


def _large_cell(pool: PoolAnalysis, scope: str) -> PityCell:
    limit = pool.large_pity_limit
    if not limit:
        return PityCell("距大保底", "无", ("本池无大保底",))
    if pool.large_pity_consumed:
        return PityCell("距大保底", "已消耗", (
            f"第{pool.large_pity_consumed_at}抽获得{pool.large_pity_up_name or 'UP'}", "本期无下次",
        ))
    if not pool.large_pity_known:
        return PityCell("距大保底", "待识别", ("未取得当期UP配置",))
    progress = pool.large_pity_progress
    return PityCell("距大保底", f"{max(0, limit - progress)} 抽", (f"进度 {progress}/{limit}", scope))


def _keepsake_cell(pool: PoolAnalysis, kind: PoolKind, *, series: bool) -> PityCell:
    cycle = kind.keepsake_cycle or 240
    cumulative = pool.keepsake_claims * cycle + pool.keepsake_progress
    next_at = (pool.keepsake_claims + 1) * cycle
    if series:
        inherited = pool.series_inherited_total
        notes = (f"累计 {cumulative}/{next_at}", f"含继承 {inherited} 抽" if inherited else "同名重构继承")
    else:
        notes = (f"进度 {cumulative}/{next_at}",
                 f"已赠 {pool.keepsake_claims} 次" if pool.keepsake_claims else f"每 {cycle} 抽赠送")
    return PityCell("距下次信物", f"{next_at - cumulative} 抽", notes)


def _rush_cell(pool: PoolAnalysis) -> PityCell:
    thresholds = pool.rush_thresholds
    if not thresholds:
        return PityCell("距加急招募", "--", ("累计奖励待同步",))
    unused = max(0, pool.rush_claimed - pool.rush_used)
    claimed = f"已获 {pool.rush_claimed}/{len(thresholds)} 次" + (f" · 未用 {unused}" if unused else "")
    if pool.rush_next_threshold:
        return PityCell("距加急招募", f"{pool.rush_next_remaining} 抽", (
            f"累计 {pool.series_total}/{pool.rush_next_threshold}", claimed,
        ))
    return PityCell("加急招募", "已领完", (f"累计 {'/'.join(map(str, thresholds))}", claimed))


def _weapon_small_cell(pool: PoolAnalysis) -> PityCell:
    limit, progress = pool.small_pity_limit, pool.small_pity_progress
    if not limit:
        return PityCell("距小保底", "--", ("规则待识别",))
    return PityCell("距小保底", f"{max(0, limit - progress)} 次十连", (
        f"进度 {progress}/{limit}", f"第{limit}次十连必出六星",
    ))


def _weapon_large_cell(pool: PoolAnalysis, scope: str) -> PityCell:
    limit = pool.large_pity_limit
    if not limit:
        return PityCell("距大保底", "无", ("本池无大保底",))
    first_up = min((item.pool_position for item in pool.six_stars if item.up_status == "up"), default=0)
    if first_up and first_up <= limit:
        return PityCell("距大保底", "已触发", (f"第{first_up}抽获得UP", scope))
    if not pool.large_pity_known:
        return PityCell("距大保底", "待识别", ("未取得当期UP配置",))
    remaining = max(0, limit - pool.large_pity_progress)
    if not remaining:
        return PityCell("距大保底", "已触发", (f"进度 {pool.large_pity_progress}/{limit}", scope))
    return PityCell("距大保底", f"{remaining} 抽", (f"进度 {pool.large_pity_progress}/{limit}", scope))


def _reward_cell(pool: PoolAnalysis, reward_kind: str, label: str) -> PityCell:
    reward = next((item for item in pool.next_rewards if item.kind == reward_kind), None)
    if reward is None:
        return PityCell(label, "--", ("累计奖励待同步",))
    inherited = pool.series_inherited_claims
    return PityCell(label, f"{reward.remaining} {reward.unit}", (
        f"累计 {pool.series_claims}/{reward.at_position}", f"含继承 {inherited} 次" if inherited else "同系列累计",
    ))


def _pity_grid(cells: list[PityCell]) -> str:
    if not cells:
        return ""
    class_name = {2: "pity-grid pity-two", 4: "pity-grid pity-four"}.get(len(cells), "pity-grid")
    items = "".join(
        f'<div class="pity-item"><span>{_esc(cell.label)}</span><b>{_esc(cell.value)}</b>'
        + "".join(f"<small>{_esc(note)}</small>" for note in cell.notes) + "</div>"
        for cell in cells
    )
    return f'<div class="{class_name}">{items}</div>'


# ============================================================ 池卡片（一段）

def render_pool_piece(
    card: GachaCard,
    rows: Sequence[str],
    start: int,
    end: int,
    *,
    cont: bool,
    open_end: bool,
    show_kind: bool,
    prev_page: int = 0,
) -> str:
    """池卡片的一段：``cont=False`` 为完整池头（含保底格），``cont=True`` 为续页池头「（续）」。"""
    pool = card.pool
    multi_run = pool.series_run_count > 1
    name = (pool.series_name or pool.name) if multi_run else pool.name
    if cont:
        title = f"{name} · 第 {pool.series_index} 期" if multi_run else name
        head = (
            f'<div class="pool-head"><div class="pool-title"><strong>{_esc(title)}<em>（续）</em></strong>'
            f'<span class="pool-meta">{_meta((f"接第 {prev_page} 页", f"第 {start + 1}–{end} / {len(rows)} 条"))}</span>'
            f'</div><div class="pool-total"><b>{pool.total}</b><span>全池合计</span></div></div>'
        )
        pity = ""
    else:
        kind_label = "类型待识别" if card.kind.is_unknown else (
            card.kind.label if show_kind and card.kind.label not in name else ""
        )
        meta = _meta((
            kind_label,
            f"第 {pool.series_index} 期" if multi_run else "",
            _date(pool.latest_ts),
            f"{len(pool.six_stars)} 个付费六星",
            f"累计已继承至第 {pool.series_inherited_to} 期" if pool.series_inherited_to else "",
            f"逐抽 {pool.recorded_total} · 统计补齐 {pool.history_missing_count}" if pool.history_missing_count else "",
            "本池同步失败" if pool.sync_error else "",
        ))
        current = '<span class="current-tag">CURRENT</span>' if card.featured else ""
        banner_items = tuple(item for item in pool.banners if item.image_path)[:2]
        banner = _draw_gacha_pool_banner(banner_items) if banner_items else ""
        head_class = "pool-head has-banner" if banner else "pool-head"
        head = (
            f'<div class="{head_class}">{banner}<div class="pool-title">{current}<strong>{_esc(name)}</strong>'
            f'<span class="pool-meta">{meta}</span></div><div class="pool-total"><b>{pool.total}</b>'
            f'<span>付费 {_pool_paid_total(pool)} · 免费 {pool.free_pull_count}</span>'
            f'<span>垫抽 {pool.since_six_star}</span></div></div>'
        )
        pity = _pity_grid(build_pity_cells(card))
    class_name = "pool-card" + (" is-current" if card.featured and not cont else "") \
        + (" is-cont" if cont else "") + (" is-open" if open_end else "")
    return (f'<section class="{class_name}" data-pool="{_esc(card.key)}">{head}{pity}'
            f'<div class="pull-bars">{"".join(rows[start:end])}</div></section>')


def _meta(parts: Sequence[str]) -> str:
    """副标题各段整体不拆，只在「 · 」之后换行：避免“12 个付费六 / 星”，也避免行首出现「·」。"""
    return "&nbsp;· ".join(f'<span class="mp">{_esc(part)}</span>' for part in parts if part)


# ============================================================ 分页：单元、实测高度、装箱

@dataclass(frozen=True)
class GachaUnit:
    kind: str                   # card | divider | note | empty | hint | done
    card: GachaCard | None = None
    section: GachaSection | None = None
    start: int = 0              # card：本段记录行 [start, end)
    end: int = 0
    cont: bool = False          # card：续页池头；divider：续页分组标题
    open_end: bool = False      # card：本段之后还有续段
    text: str = ""


@dataclass(frozen=True)
class PoolMeasure:
    head: float                 # 完整池头 + 保底格
    head_cont: float            # 续页池头
    base: float                 # 卡片边框 + .pull-bars 上下内边距
    prefix: tuple[float, ...]   # 记录行高度前缀和


@dataclass(frozen=True)
class GachaMeasure:
    pools: dict[str, PoolMeasure]
    row_gap: float
    divider: float
    divider_cont: float
    note: float
    empty: float
    hint: float
    done: float
    heads: dict[tuple[int, bool], float]    # (栏序号, 是否续页) → 栏头高度
    overhead_first: float                   # 首页：header + 总览 + footer + 内边距
    overhead_cont: float                    # 续页：header + footer + 内边距

    def piece(self, key: str, start: int, end: int, cont: bool) -> float:
        pool = self.pools[key]
        return (pool.base + (pool.head_cont if cont else pool.head) + pool.prefix[end] - pool.prefix[start]
                + self.row_gap * max(end - start - 1, 0))


def hidden_note(section: GachaSection) -> str:
    pulls = sum(card.pool.total for card in section.hidden)
    six = sum(_six_star_count(card.pool) for card in section.hidden)
    return f"基础寻访已按设置隐藏：{pulls} 抽 · {six} 个六星，仍计入总数与角色寻访"


def column_units(column: GachaColumn, row_counts: dict[str, int]) -> list[GachaUnit]:
    units: list[GachaUnit] = []
    for section in column.sections:
        if section.spec.divider:
            if not section.cards and not section.hidden:
                continue
            units.append(GachaUnit("divider", section=section))
        elif not section.cards:
            units.append(GachaUnit("empty", section=section, text=section.spec.empty))
            continue
        units.extend(
            GachaUnit("card", card=card, section=section, end=row_counts[card.key]) for card in section.cards
        )
        if section.hidden:
            units.append(GachaUnit("note", section=section, text=hidden_note(section)))
    return units


def _unit_height(unit: GachaUnit, measure: GachaMeasure) -> float:
    if unit.kind == "card":
        return measure.piece(unit.card.key, unit.start, unit.end, unit.cont)
    if unit.kind == "divider":
        return measure.divider_cont if unit.cont else measure.divider
    return {"note": measure.note, "empty": measure.empty, "hint": measure.hint, "done": measure.done}[unit.kind]


def _stack_height(units: Sequence[GachaUnit], measure: GachaMeasure) -> float:
    return sum(_unit_height(unit, measure) for unit in units) + STACK_GAP * max(len(units) - 1, 0)


def column_height(units: Sequence[GachaUnit], head: float, measure: GachaMeasure) -> float:
    return 2 * COLUMN_BORDER + head + 2 * STACK_PAD + _stack_height(units, measure)


def _min_lead_height(unit: GachaUnit, measure: GachaMeasure) -> float:
    """分组标题必须和下一块的“最小首段”同页：卡片 = 池头 + 2 行（不可拆的卡片 = 整张）。"""
    if unit.kind != "card":
        return _unit_height(unit, measure)
    rows = unit.end - unit.start
    keep = SPLIT_KEEP_ROWS if rows >= SPLIT_MIN_ROWS else rows
    return measure.piece(unit.card.key, unit.start, unit.start + keep, unit.cont)


def _best_split(unit: GachaUnit, room: float, measure: GachaMeasure, *, force: bool = False) -> int:
    """本页还剩 room 高度时这张卡片最多放几行；0 = 不拆（整张移到下一页）。绝不切断一行。"""
    rows = unit.end - unit.start
    if force:
        low, high = 1, rows - 1
    elif rows < SPLIT_MIN_ROWS:
        return 0
    else:
        low, high = SPLIT_KEEP_ROWS, rows - SPLIT_KEEP_ROWS
    for keep in range(high, low - 1, -1):
        if measure.piece(unit.card.key, unit.start, unit.start + keep, unit.cont) <= room:
            return keep
    return 0


def _pack_column(
    column: GachaColumn,
    units: Sequence[GachaUnit],
    measure: GachaMeasure,
    cap_for: Callable[[int], float],
    max_pages: int,
) -> list[list[GachaUnit]] | None:
    """按栏贪心装页。

    * 剩余内容整栏放得下 → 本页即末页，不留「续见」提示；
    * 否则底部预留「续见第 n 页」提示的高度，逐块放入；放不下的卡片在两行之间拆分
      （≥4 行才拆，每段 ≥2 行），续段换成「（续）」池头放到下一页最前面；
    * 分组标题不落单；续页如果从分组中间开始，先补一个「其他寻访（续）」。
    """
    queue = list(units)
    pages: list[list[GachaUnit]] = []
    divided: set[int] = set()
    while queue:
        if len(pages) >= max_pages:
            return None
        page_index = len(pages)
        cap = cap_for(page_index)
        fixed = 2 * COLUMN_BORDER + measure.heads[(column.index, page_index > 0)] + 2 * STACK_PAD
        page: list[GachaUnit] = []
        lead = queue[0]
        if (page_index and lead.kind in ("card", "note") and lead.section is not None
                and lead.section.spec.divider and id(lead.section) in divided):
            page.append(GachaUnit("divider", section=lead.section, cont=True))
        if fixed + _stack_height(page + queue, measure) <= cap:
            pages.append(page + queue)
            break
        limit = cap - STACK_GAP - measure.hint
        used = fixed + _stack_height(page, measure)
        placed = 0
        while queue:
            unit = queue[0]
            gap = STACK_GAP if page else 0
            need = gap + _unit_height(unit, measure)
            if unit.kind == "divider" and len(queue) > 1:
                need += STACK_GAP + _min_lead_height(queue[1], measure)
            if used + need <= limit:
                page.append(queue.pop(0))
                used += gap + _unit_height(unit, measure)
                placed += 1
                if unit.kind == "divider":
                    divided.add(id(unit.section))
                continue
            if unit.kind == "card":
                keep = _best_split(unit, limit - used - gap, measure)
                if keep:
                    page.append(replace(unit, end=unit.start + keep, open_end=True))
                    queue[0] = replace(unit, start=unit.start + keep, cont=True)
                    placed += 1
            break
        if not placed:                  # 空页也放不下首块：强制在行间拆分，或整块硬放（交给截图校验）
            unit = queue[0]
            gap = STACK_GAP if page else 0
            keep = _best_split(unit, limit - used - gap, measure, force=True) if unit.kind == "card" else 0
            if keep:
                page.append(replace(unit, end=unit.start + keep, open_end=True))
                queue[0] = replace(unit, start=unit.start + keep, cont=True)
            else:
                page.append(queue.pop(0))
                if unit.kind == "divider":
                    divided.add(id(unit.section))
        pages.append(page)
    return pages


def paginate_gacha(
    columns: Sequence[GachaColumn],
    unit_lists: Sequence[Sequence[GachaUnit]],
    measure: GachaMeasure,
    *,
    max_height: int | None = None,
    safety: float = GACHA_PAGE_SAFETY,
) -> tuple[list[list[list[GachaUnit]]], dict]:
    """返回 ``pages[page][column] = units`` 与分页过程信息（纯函数）。``max_height`` 默认 GACHA_PAGE_MAX_HEIGHT。

    1. 单页放得下（首页开销 + 最高一栏 ≤ 上限 − 余量）→ 1 页；
    2. 否则在统一栏高 L 下逐栏装箱：首页栏高上限 L，续页 L + (首页开销 − 续页开销)，
       每页总高度都约等于「首页开销 + L」；
    3. 先用最宽松的 L 装一次得到最少页数 N，再二分求能装进 N 页的最小 L —— 各页高度均衡；
       其他栏在同一 L 下从第 1 页起填满，内容不够的栏在后续页显示「已展示完毕」。
    """
    if max_height is None:
        max_height = GACHA_PAGE_MAX_HEIGHT
    budget_first = max_height - measure.overhead_first - safety
    budget_cont = max_height - measure.overhead_cont - safety
    delta = measure.overhead_first - measure.overhead_cont
    single = [column_height(units, measure.heads[(index, False)], measure) for index, units in enumerate(unit_lists)]
    info: dict = {
        "single_page_column_heights": [round(value, 1) for value in single],
        "budget_first": round(budget_first, 1),
        "budget_cont": round(budget_cont, 1),
    }
    if max(single) <= budget_first:
        info.update(level=round(max(single), 1), tries=0, column_pages=[1] * len(unit_lists))
        return [[list(units) for units in unit_lists]], info

    def cap_for(level: float) -> Callable[[int], float]:
        return lambda page: min(level + (delta if page else 0), budget_first if page == 0 else budget_cont)

    def try_pack(level: float, max_pages: int):
        packed = [
            _pack_column(column, units, measure, cap_for(level), max_pages)
            for column, units in zip(columns, unit_lists)
        ]
        return None if any(item is None for item in packed) else packed

    best = try_pack(budget_first, 999)
    if best is None:
        raise RuntimeError("抽卡分析分页失败：超过 999 页")
    page_count = max(len(item) for item in best)
    low, high, tries = GACHA_LEVEL_FLOOR, int(budget_first), 1
    while low < high:
        middle = (low + high) // 2
        packed = try_pack(middle, page_count)
        tries += 1
        if packed is None:
            low = middle + 1
        else:
            best, high = packed, middle
    info.update(level=high, tries=tries, column_pages=[len(item) for item in best])
    pages: list[list[list[GachaUnit]]] = []
    for page_index in range(page_count):
        page = []
        for column_pages in best:
            if page_index < len(column_pages):
                units = list(column_pages[page_index])
                if page_index + 1 < len(column_pages):
                    text = (f"本池未完 · 续见第 {page_index + 2} 页" if units[-1].open_end
                            else f"本栏续见第 {page_index + 2} 页")
                    units.append(GachaUnit("hint", text=text))
            else:
                units = [GachaUnit("done", text=f"本栏已在第 {len(column_pages)} 页展示完毕")]
            page.append(units)
        pages.append(page)
    return pages, info


# ============================================================ 页面 HTML

def _stat_lines(cards: Sequence[GachaCard]) -> list[str]:
    paid = sum(_pool_paid_total(card.pool) for card in cards)
    free = sum(card.pool.free_pull_count for card in cards)
    six = sum(_six_star_count(card.pool) for card in cards)
    return [f"付费 {paid} 抽 · 免费 {free} 抽", f"{six} 个六星 · {len(cards)} 个卡池"]


def _render_unit(
    unit: GachaUnit,
    rows: dict[str, list[str]],
    page_no: int,
    page_units: Sequence[GachaUnit],
) -> str:
    if unit.kind == "card":
        return render_pool_piece(
            unit.card, rows[unit.card.key], unit.start, unit.end, cont=unit.cont,
            open_end=unit.open_end, show_kind=unit.section.spec.show_kind, prev_page=page_no - 1,
        )
    if unit.kind == "divider":
        spec = unit.section.spec
        if unit.cont:
            here = sum(1 for item in page_units if item.kind == "card" and item.section is unit.section)
            lines = [f"接第 {page_no - 1} 页", f"本页 {here} 个卡池"]
            title = f"{_esc(spec.divider)}<em>（续）</em>"
        else:
            lines = _stat_lines(unit.section.cards)
            title = _esc(spec.divider)
        return (f'<div class="stack-divider"><div><span class="column-kicker">{_esc(spec.divider_kicker)}</span>'
                f'<h3>{title}</h3></div><p>{"<br>".join(map(_esc, lines))}</p></div>')
    if unit.kind == "note":
        return f'<div class="empty slim note">{_esc(unit.text)}</div>'
    if unit.kind == "hint":
        return f'<div class="continue-hint">{_esc(unit.text)}</div>'
    if unit.kind == "done":
        return f'<div class="empty slim done">{_esc(unit.text)}</div>'
    return f'<div class="empty slim">{_esc(unit.text)}</div>'


def _column_html(
    column: GachaColumn,
    units: Sequence[GachaUnit],
    rows: dict[str, list[str]],
    *,
    page_no: int,
    body: str | None = None,
    lines: Sequence[str] | None = None,
) -> str:
    cont = page_no > 1
    if lines is None:
        if cont:
            pieces = sum(1 for unit in units if unit.kind == "card")
            lines = [f"接第 {page_no - 1} 页", f"本页 {pieces} 个卡池"] if pieces else ["", ""]
        else:
            lines = _stat_lines(column.sections[0].cards)
    head = (f'<div class="column-head"><div><span class="column-kicker">{_esc(column.spec.kicker)}</span>'
            f'<h2>{_esc(column.spec.title)}{"<em>（续）</em>" if cont else ""}</h2></div>'
            f'<p>{"<br>".join(_esc(line) or "&nbsp;" for line in lines)}</p></div>')
    if body is None:
        body = "".join(_render_unit(unit, rows, page_no, units) for unit in units)
    return (f'<div class="pool-column" data-col="{column.index}" data-key="{column.spec.key}">{head}'
            f'<div class="pool-stack">{body}</div></div>')


def _expectation_values(
    before: float, after: float | None, combined_before: float | None, combined_after: float | None,
    actual: float | None, *, known: bool = True,
) -> list[tuple[str, str, str]]:
    theory = f"{before:.1f}" if after is None else f"{before:.1f} → {after:.1f}"
    if combined_before is None:
        expectation = theory
    elif combined_after is None:
        expectation = f"{combined_before:.1f}"
    else:
        expectation = f"{combined_before:.1f} → {combined_after:.1f}"
    return [
        ("综合期望", expectation if known else "待确认", "抽"),
        ("账号当前", f"{actual:.1f}" if actual is not None else "暂无", "抽"),
    ]


def _expectation_row(label: str, scope: str, values: Sequence[tuple[str, str, str]]) -> str:
    """总览格里的分项块：左侧标签 + 范围说明，右侧两行「名称 数值 单位」；待确认 / 暂无灰显、不带单位。"""
    spans = "".join(
        f'<span>{_esc(name)} <b class="pending">{_esc(value)}</b></span>' if value in ("待确认", "暂无")
        else f'<span>{_esc(name)} <b>{_esc(value)}</b> {_esc(unit)}</span>'
        for name, value, unit in values
    )
    return (f'<div class="expectation-row"><div class="expectation-label"><strong>{_esc(label)}</strong>'
            f'<small>{_esc(scope)}</small></div><div class="expectation-values">{spans}</div></div>')


def _expectation_rows(expectation: SixStarExpectation, item_type: str) -> str:
    up_scope = "含120抽保底" if item_type == "角色" else "首个UP前 → 后"
    return (
        _expectation_row(f"UP{item_type}", up_scope, _expectation_values(
            expectation.up_before, expectation.up_after, expectation.up_combined_before,
            expectation.up_combined_after, expectation.actual_up, known=expectation.up_known,
        ))
        + _expectation_row(f"6星{item_type}", "首个UP前 → 后", _expectation_values(
            expectation.before_up, expectation.after_up, expectation.combined_before_up,
            expectation.combined_after_up, expectation.actual,
        ))
    )


def _expectation(view: GachaAnalysis, group: str) -> SixStarExpectation:
    expectations = getattr(view, "expectations", None) or {}
    return expectations.get(group) or calculate_group_expectation(view.pools, group)


def _summary_html(view: GachaAnalysis, columns: Sequence[GachaColumn]) -> str:
    """总数格 + 每栏一格，顺序与下方栏一致；两栏（无重构）时不出重构寻访格。
    总数格与池格同构：标题行右侧放总数，下面一行付费 / 免费，再下面是角色 / 武器两个分项块（累计抽数 + 六星数）。"""
    pools = [(pool, pool_kind(pool)) for pool in view.pools]
    special = [pool for pool, kind in pools if kind.key == "special"]
    rerun = [pool for pool, kind in pools if kind.key == "rerun"]
    weapon = [pool for pool, kind in pools if kind.item_type == "武器"]
    character = [pool for pool, kind in pools if kind.item_type == "角色"]
    constant = sum(pool.total for pool, kind in pools if kind.key == "weapon_constant")

    free_pull_count = getattr(view, "free_pull_count", 0)
    paid_total = getattr(view, "paid_total", 0) or max(0, view.total - free_pull_count)
    xhh_imported_at = getattr(view, "xhh_imported_at", 0)
    recorded_total = getattr(view, "recorded_total", 0)
    if not xhh_imported_at and not recorded_total:
        recorded_total = view.total
    history_missing_count = getattr(view, "history_missing_count", 0)
    detail = (
        (f"逐抽明细 {recorded_total}", f"统计补齐 {history_missing_count}") if history_missing_count
        else (f"付费 {paid_total}", f"免费 {free_pull_count}")
    )
    # 小字只放两段：小黑盒补齐时再加一段会折成两行，分项块就和相邻池格错开。
    # 六星数按角色 / 武器拆进分项块（口径同栏头 _stat_lines），两项之和即六星总数。
    type_rows = "".join(
        _expectation_row(label, scope, (
            ("累计", str(sum(pool.total for pool in group)), "抽"),
            ("六星", str(sum(_six_star_count(pool) for pool in group)), "个"),
        ))
        for label, scope, group in (("角色寻访", "全部角色池", character), ("武器申领", "全部武器池", weapon))
    )

    def heading(kicker: str, title: str) -> str:
        return f'<div class="metric-title"><small>{_esc(kicker)}</small><span>{_esc(title)}</span></div>'

    def tile(css: str, kicker: str, title: str, total: int, caption: Sequence[str], rows: str) -> str:
        return (f'<div class="{css}"><div class="{css}-head">{heading(kicker, title)}<strong>{total}</strong></div>'
                f'<small>{_meta(caption)}</small><div class="expectation-summary">{rows}</div></div>')

    def metric(title: str, kicker: str, group: list[PoolAnalysis], caption: Sequence[str], rows: str) -> str:
        return tile("metric", kicker, title, sum(pool.total for pool in group), caption, rows)

    def paid(group: list[PoolAnalysis]) -> int:
        return sum(_pool_paid_total(pool) for pool in group)

    def free(group: list[PoolAnalysis]) -> int:
        return sum(pool.free_pull_count for pool in group)

    tiles = {
        "special": lambda: metric("特许寻访", "角色池 · 累计抽数", special,
                                  (f"付费 {paid(special)}", f"免费 {free(special)}"),
                                  _expectation_rows(_expectation(view, "special"), "角色")),
        "rerun": lambda: metric("重构寻访", "角色池 · 累计抽数", rerun,
                                (f"付费 {paid(rerun)}", f"加急招募 {free(rerun)}"),
                                _expectation_rows(_expectation(view, "rerun"), "角色")),
        "weapon": lambda: metric("武器申领", "武器池 · 累计抽数", weapon,
                                 (f"付费 {paid(weapon)}", f"含常驻 {constant}"),
                                 _expectation_rows(_expectation(view, "weapon"), "武器")),
    }
    return (
        f'<section class="summary" style="grid-template-columns:minmax(0,.8fr) repeat({len(columns)},minmax(0,1fr))">'
        + tile("total", "全部卡池 · 累计抽数", "卡池总数", view.total, detail, type_rows)
        + "".join(tiles[column.spec.key]() for column in columns)
        + "</section>"
    )


def _grid_style(columns: Sequence[GachaColumn]) -> str:
    return "grid-template-columns:" + " ".join(f"minmax(0,{column.spec.share:g}fr)" for column in columns)


def _card_html(
    view: GachaAnalysis,
    columns: Sequence[GachaColumn],
    body: str,
    *,
    uid: str,
    page_no: int,
    page_count: int,
    attrs: str = "",
) -> str:
    cont = page_no > 1
    xhh_imported_at = getattr(view, "xhh_imported_at", 0)
    completeness = "统计已补齐" if xhh_imported_at and view.complete else ("同步正常" if view.complete else "部分数据")
    state = f"{completeness} · {page_no}/{page_count}" if page_count > 1 else completeness
    source = (
        f"小黑盒历史统计已补齐 · 导入 {format_timestamp(xhh_imported_at)} · 官方逐抽明细单独保留"
        if xhh_imported_at else "官方接口仅提供近 90 天记录 · 本地同步会持续累积保留"
    )
    server_name = server_label(view.role.server_name) or "默认服务器"
    title = (f'<span class="nick">{_esc(view.role.nickname)}</span>'
             f'<span class="title-suffix"> · 抽卡分析{"（续）" if cont else ""}</span>')
    warning = (
        f'<div class="warning">有 {len(view.errors)} 个卡池同步失败，其他成功数据已保留。</div>'
        if view.errors and not cont else ""
    )
    page_tail = f" · 第 {page_no}/{page_count} 页" if page_count > 1 else ""
    return (
        f'<div class="gacha-analysis-card"{attrs}>'
        f'<header><div><small>ENDFIELD / GACHA ARCHIVE</small><h1>{title}</h1>'
        f'<p>{_esc(server_name)} · {_esc(uid)}</p></div><div class="sync-state"><b>{_esc(state)}</b>'
        f'<span>同步 {_esc(format_timestamp(view.last_sync_at))}</span></div></header>'
        f'<main>{"" if cont else _summary_html(view, columns)}{warning}'
        f'<section class="pool-columns" data-cols="{len(columns)}" style="{_grid_style(columns)}">{body}</section>'
        f'<footer class="gacha-source"><span>{_esc(source)}</span>'
        f'<span>免费十连 / 加急招募单列展示，不计入任何保底{page_tail}</span></footer></main></div>'
    )


def _document(body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{gacha_font_face_css()}:root{{--gacha-width:{GACHA_CARD_WIDTH}px}}{GACHA_CSS}</style></head>"
            f"<body>{body}</body></html>")


@lru_cache(maxsize=1)
def gacha_font_face_css() -> str:
    """HarmonyOS Sans SC 400 / 500 / 700 的 @font-face（data: URL，进程内只读盘编码一次）。
    缺文件时跳过该字重并记 warning，版面回退到 GACHA_CSS 里的系统字体栈。"""
    faces = []
    for weight, name in GACHA_FONT_FILES:
        try:
            data = base64.b64encode((GACHA_FONT_DIR / name).read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning(f"[endfield-gacha] font {name} unavailable, falling back to system fonts: {exc}")
            continue
        faces.append(f'@font-face{{font-family:"{GACHA_FONT_FAMILY}";font-weight:{weight};font-style:normal;'
                     f'font-display:block;src:url(data:font/ttf;base64,{data}) format("truetype")}}')
    return "".join(faces)


def render_gacha_page_html(
    view: GachaAnalysis,
    columns: Sequence[GachaColumn],
    page_units: Sequence[Sequence[GachaUnit]],
    rows: dict[str, list[str]],
    *,
    uid: str,
    page_no: int,
    page_count: int,
) -> str:
    body = "".join(
        _column_html(column, units, rows, page_no=page_no) for column, units in zip(columns, page_units)
    )
    return _document(_card_html(view, columns, body, uid=uid, page_no=page_no, page_count=page_count))


def render_measure_html(
    view: GachaAnalysis,
    columns: Sequence[GachaColumn],
    rows: dict[str, list[str]],
    *,
    uid: str,
) -> str:
    """测量页：一个文档里放两张卡。

    * 首页卡：每栏放入该栏全部池的完整卡片（``data-mp``）与续页池头样本（``data-mc``，数字取最宽的
      99/999，结果偏保守）；含「其他寻访」分组的那一栏（重构侧栏，两栏时为特许栏）另放分组标题、
      隐藏说明、空态、提示块、“已展示完毕”块（``data-m``）——它在三栏里最窄，量出的高度偏保守；
    * 续页卡：只有续页外壳与续页栏头，用来量续页开销。
    两张卡的页眉都按 99/99 页渲染（右上角最宽），长昵称换行只会被高估。
    """
    stacks = []
    for column in columns:
        blocks = []
        for section in column.sections:
            for card in section.cards:
                card_rows = rows[card.key]
                count = len(card_rows)
                full = render_pool_piece(card, card_rows, 0, count, cont=False, open_end=False,
                                         show_kind=section.spec.show_kind)
                sample = render_pool_piece(card, card_rows, count - 1, count, cont=True, open_end=False,
                                           show_kind=section.spec.show_kind, prev_page=99)
                sample = sample.replace(f"第 {count}–{count} / {count} 条", "第 999–999 / 999 条")
                blocks.append(f'<div data-mp="{_esc(card.key)}">{full}</div>')
                blocks.append(f'<div data-mc="{_esc(card.key)}">{sample}</div>')
        section = next((section for section in column.sections if section.spec.divider), None)
        if section is not None:
            note = hidden_note(section) if section.hidden else \
                "基础寻访已按设置隐藏：99999 抽 · 999 个六星，仍计入总数与角色寻访"
            blocks.append(f'<div data-m="divider">{_render_unit(GachaUnit("divider", section=section), rows, 1, [])}</div>')
            blocks.append('<div data-m="divider_cont">'
                          f'{_render_unit(GachaUnit("divider", section=section, cont=True), rows, 99, [])}</div>')
            blocks.append(f'<div data-m="note"><div class="empty slim note">{_esc(note)}</div></div>')
            blocks.append('<div data-m="empty"><div class="empty slim">暂无重构寻访记录</div></div>')
            blocks.append('<div data-m="hint"><div class="continue-hint">本池未完 · 续见第 99 页</div></div>')
            blocks.append('<div data-m="done"><div class="empty slim done">本栏已在第 99 页展示完毕</div></div>')
        stacks.append(_column_html(column, [], rows, page_no=1, body="".join(blocks)))
    cont_columns = "".join(
        _column_html(column, [], rows, page_no=99, lines=("接第 98 页", "本页 99 个卡池"),
                     body='<div class="empty slim done">本栏已在第 99 页展示完毕</div>')
        for column in columns
    )
    return _document(
        _card_html(view, columns, "".join(stacks), uid=uid, page_no=1, page_count=99, attrs=' data-measure="first"')
        + _card_html(view, columns, cont_columns, uid=uid, page_no=99, page_count=99, attrs=' data-measure="cont"')
    )


MEASURE_JS = """
() => {
  const h = el => el ? el.getBoundingClientRect().height : 0;
  const read = card => {
    const cols = [...card.querySelectorAll(':scope > main > .pool-columns > .pool-column')];
    return {card: h(card), cols: cols.map(h), heads: cols.map(c => h(c.querySelector('.column-head')))};
  };
  const first = document.querySelector('[data-measure="first"]');
  const cont = document.querySelector('[data-measure="cont"]');
  const piece = el => {
    const c = el.querySelector('.pool-card');
    const pity = c.querySelector('.pity-grid');
    return {total: h(c), head: h(c.querySelector('.pool-head')) + (pity ? h(pity) : 0),
            rows: [...c.querySelectorAll('.pull-bars > *')].map(h)};
  };
  const out = {first: read(first), cont: read(cont), blocks: {}, full: {}, cont_heads: {}};
  for (const el of first.querySelectorAll('[data-m]')) out.blocks[el.dataset.m] = h(el);
  for (const el of first.querySelectorAll('[data-mp]')) out.full[el.dataset.mp] = piece(el);
  for (const el of first.querySelectorAll('[data-mc]')) out.cont_heads[el.dataset.mc] = piece(el).head;
  const bars = first.querySelector('.pull-bars');
  out.row_gap = bars ? (parseFloat(getComputedStyle(bars).rowGap) || 0) : 6;
  return out;
}
"""


def parse_measure(raw: dict) -> GachaMeasure:
    gap = float(raw["row_gap"])
    pools = {}
    for key, full in raw["full"].items():
        sizes = [float(value) for value in full["rows"]]
        base = full["total"] - full["head"] - sum(sizes) - gap * max(len(sizes) - 1, 0)
        pools[key] = PoolMeasure(full["head"], raw["cont_heads"][key], base, (0.0, *accumulate(sizes)))
    blocks = raw["blocks"]
    first, cont = raw["first"], raw["cont"]
    return GachaMeasure(
        pools=pools, row_gap=gap, divider=blocks["divider"], divider_cont=blocks["divider_cont"],
        note=blocks["note"], empty=blocks["empty"], hint=blocks["hint"], done=blocks["done"],
        heads={
            **{(index, False): value for index, value in enumerate(first["heads"])},
            **{(index, True): value for index, value in enumerate(cont["heads"])},
        },
        overhead_first=first["card"] - max(first["cols"]),
        overhead_cont=cont["card"] - max(cont["cols"]),
    )


# ============================================================ 入口

async def draw_gacha_analysis_cards(view: GachaAnalysis, *, uid: str) -> tuple[bytes, ...]:
    """v3 抽卡分析图：测量 1 次 + 每页截图 1 次。

    截图意外超高时用 4 倍余量重新装箱再试一次；测量失败、再次超高或版面溢出时回退到 v1 两栏实现。
    """
    started = perf_counter()
    columns = build_gacha_columns(view)
    rows = {card.key: render_pool_rows(card) for column in columns for section in column.sections
            for card in section.cards}
    try:
        measure = parse_measure(await _evaluate_html(render_measure_html(view, columns, rows, uid=uid), MEASURE_JS))
    except Exception as exc:
        logger.opt(exception=exc).error(
            f"[endfield-gacha] v3 measure failed, fallback=v1: {type(exc).__name__}: {exc}"
        )
        return await _cards._draw_gacha_analysis_cards_v1(view, uid=uid)
    measured_at = perf_counter()
    row_counts = {key: len(items) for key, items in rows.items()}
    unit_lists = [column_units(column, row_counts) for column in columns]
    for safety in (GACHA_PAGE_SAFETY, GACHA_RETRY_SAFETY):
        pages, info = paginate_gacha(columns, unit_lists, measure, safety=safety)
        try:
            pngs = [
                await _draw_gacha_shell(render_gacha_page_html(
                    view, columns, page_units, rows, uid=uid, page_no=index, page_count=len(pages),
                ))
                for index, page_units in enumerate(pages, start=1)
            ]
        except Exception as exc:
            if isinstance(exc, RuntimeError) and is_height_limit_error(exc) and safety == GACHA_PAGE_SAFETY:
                logger.warning(f"[endfield-gacha] v3 page exceeded height, repacking with safety={GACHA_RETRY_SAFETY}: {exc}")
                continue
            logger.opt(exception=exc).error(
                f"[endfield-gacha] v3 render failed, fallback=v1: {type(exc).__name__}: {exc}"
            )
            return await _cards._draw_gacha_analysis_cards_v1(view, uid=uid)
        logger.info(
            f"[endfield-gacha] v3 pages={len(pages)} columns={'|'.join(column.spec.key for column in columns)} "
            f"width={GACHA_CARD_WIDTH} level={info.get('level')} tries={info.get('tries')} "
            f"safety={safety} measure={measured_at - started:.2f}s total={perf_counter() - started:.2f}s"
        )
        return tuple(pngs)
    raise AssertionError("unreachable")  # pragma: no cover - 第二轮要么返回要么回退


async def _evaluate_html(document: str, script: str):
    html_path = _write_temp_html(document)
    try:
        return await evaluate_web_page(
            html_path.resolve().as_uri(), script, viewport=(GACHA_CARD_WIDTH, 900), device_scale_factor=2.0,
        )
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def _draw_gacha_shell(document: str) -> bytes:
    """抽卡专用外壳截图：token 与 cards._draw_neutral_card 相同（该函数多卡共用，不改），
    单页上限 GACHA_PAGE_MAX_HEIGHT，并检查栏、卡片、记录行与页眉的溢出。"""
    html_path = _write_temp_html(document)
    try:
        output = await screenshot_web_element(
            html_path.resolve().as_uri(), ".gacha-analysis-card", viewport=(GACHA_CARD_WIDTH, 1), timeout_ms=15000,
            max_height=GACHA_PAGE_MAX_HEIGHT, device_scale_factor=2.0, settle_ms=30,
            wait_for_images=True, wait_for_fonts=True, strict_max_height=True,
            overflow_selectors=GACHA_OVERFLOW_SELECTORS,
        )
        return await run_image_render(optimize_png_container, output)
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


# ============================================================ 小工具

def _esc(value: object) -> str:
    return html.escape(str(value))


def _date(timestamp: int) -> str:
    return format_timestamp(timestamp).split(" ", 1)[0]


def _six_star_count(pool: PoolAnalysis) -> int:
    return len(pool.six_stars) + sum(len(batch.six_stars) for batch in pool.free_batches)


def _icon_html(path: str, alt: str, fallback: str) -> str:
    url = _icon_data_url(path)
    if url:
        return f'<img src="{_esc(url)}" alt="{_esc(alt)}">'
    return f"<span>{_esc(fallback)}</span>"


def _icon_data_url(path: str) -> str:
    """同一图标在一张图里会出现很多次：按 (路径, 修改时间) 缓存 data URL，避免重复读盘编码。"""
    if not path:
        return ""
    try:
        stat = Path(path).stat()
    except OSError:
        return ""
    return _cached_icon_data_url(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _cached_icon_data_url(path: str, _mtime_ns: int, _size: int) -> str:
    target = Path(path)
    mime = mimetypes.guess_type(target.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(target.read_bytes()).decode('ascii')}"


# 外壳 token 复制自 cards._draw_neutral_card；宽度取 --gacha-width（_document 按 GACHA_CARD_WIDTH 写入）。
# 1600 宽时主栏约 518px、重构侧栏约 446px、两栏各约 747px；记录行的名称列取行宽 36%（120–260px），
# 两行名称用 text-wrap:balance 均分，CURRENT 标签最长 300px（两栏时不会拉成一整条黑带）。
# 没有任何 text-overflow:ellipsis / line-clamp：长文本一律 overflow-wrap:anywhere 换行。
# 字体：内置 HarmonyOS Sans SC（GACHA_FONT_FAMILY）排第一，后面是原系统字体栈；字重只用字体实际有的
# 400 / 500 / 700，并关掉 font-synthesis，避免 800–950 在不同机器上被映射成不同的粗体或合成加粗。
# 总览格四格共用标题块：12px 灰色分类 kicker + 21px 深色 700 标题，底部 2px 墨线；格内总数与标题底边对齐
# （行高 1 / 1.15 下基线差 <1px）。四格同构：标题行右侧总数 → 一行小字 → 分项块，总数格的分项是角色 / 武器拆分，
# 分项块与相邻池格逐行对齐。总数格边框 3px，内边距相应少 2px，四格标题同一水平线。
# 总览格小字：标签 16 / 范围说明 12 / 数值行 14 / 加粗数字 17 / 副标题 13（px），数字等宽。
GACHA_CSS = """
:root{--card-header-rule:5px solid rgba(223,236,50,.5)}
*{box-sizing:border-box}html,body{margin:0;width:var(--gacha-width);background:#d8d8d8;color:#181818;font-family:'EndfieldGachaSans','Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif;font-synthesis:none}
.gacha-analysis-card{width:var(--gacha-width);min-height:420px;padding:28px;background:linear-gradient(90deg,rgba(0,0,0,.055) 1px,transparent 1px) 0 0/32px 32px,linear-gradient(0deg,rgba(0,0,0,.055) 1px,transparent 1px) 0 0/32px 32px,#ededed}
.gacha-analysis-card[data-measure]{min-height:0}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding:22px 25px;background:#292929;color:#fff;border-bottom:5px solid #000}
header small{font-size:13px;letter-spacing:.2em;color:#c7c7c7}header h1{margin:5px 0 0;font-size:36px;line-height:1.1}header time{color:#d0d0d0}
main{padding:18px;border:1px solid #777;background:#f8f8f8}.empty{padding:28px;text-align:center;color:#777;background:#eee;border:1px dashed #888}

header>div:first-child{min-width:0}header h1{overflow-wrap:anywhere;text-wrap:balance}header h1 .title-suffix{white-space:nowrap}
header p{margin:8px 0 0;color:#d0d0d0;font-size:16px;overflow-wrap:anywhere}.sync-state{flex:none;margin-left:24px;text-align:right}.sync-state b{display:block;font-size:22px}.sync-state span{display:block;margin-top:8px;color:#ccc}

.summary{display:grid;grid-template-columns:minmax(0,.8fr) repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}
.total,.metric{min-width:0;min-height:172px;padding:15px 16px;border:1px solid #999;background:#fff}
.total{padding:13px 14px;border:3px solid #222}
.total-head,.metric-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;padding-bottom:10px;border-bottom:2px solid #222}
.metric-title{min-width:0}.metric-title small,.metric-title span{display:block}
.metric-title small{color:#777;font-size:12px;font-weight:500;line-height:1.3;letter-spacing:.04em;overflow-wrap:anywhere}
.metric-title span{margin-top:3px;color:#181818;font-size:21px;font-weight:700;line-height:1.15;white-space:nowrap}
.total-head strong,.metric-head strong{flex:none;font-size:31px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.total>small,.metric>small{display:block;margin-top:10px;color:#666;font-size:13px;font-weight:500;line-height:1.5;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.total .mp,.metric>small .mp{white-space:nowrap}
.expectation-summary{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;margin-top:14px}
.expectation-row{display:grid;grid-template-columns:96px minmax(0,1fr);align-items:center;gap:12px;padding:10px 12px;border-left:4px solid #333;background:#ededed;color:#555;font-size:14px;font-weight:500;line-height:1.5;font-variant-numeric:tabular-nums}
.expectation-label strong,.expectation-label small{display:block}.expectation-label strong{color:#111;font-size:16px;font-weight:700;line-height:1.3}
.expectation-label small{margin-top:3px;color:#777;font-size:12px;font-weight:400;line-height:1.35;white-space:nowrap}
.expectation-values{display:flex;flex-direction:column;justify-content:center;gap:4px;min-width:0}.expectation-values span{white-space:nowrap}
.expectation-row b{margin:0 2px;color:#111;font-size:17px;font-weight:700;line-height:1.2;white-space:nowrap}.expectation-row b.pending{color:#999;font-size:14px;font-weight:500}
.warning{margin:0 0 16px;padding:12px 16px;border:2px dashed #555;background:#f2f2f2;font-weight:700}

.pool-columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:start;gap:12px}
.pool-column{min-width:0;border:1px solid #777;background:#e4e4e4}
.column-head{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;padding:12px 14px;border-bottom:4px solid #222;background:#fff}
.column-head>div,.stack-divider>div{min-width:0}
.column-kicker{display:block;color:#888;font-size:10px;font-weight:500;letter-spacing:.04em;overflow-wrap:anywhere}
.column-head h2{margin:3px 0 0;font-size:24px;line-height:1.15;white-space:nowrap}.column-head h2 em,.stack-divider h3 em{margin-left:2px;color:#888;font-size:15px;font-style:normal}
.column-head p,.stack-divider p{flex:none;margin:0;color:#666;font-size:12px;line-height:1.45;text-align:right;white-space:nowrap}
.pool-stack{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;padding:10px}
.stack-divider{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;margin-top:6px;padding:9px 12px;border-bottom:3px solid #222;background:#fff}
.stack-divider h3{margin:2px 0 0;font-size:19px;line-height:1.15;white-space:nowrap}.stack-divider p{font-size:11px}
.empty.slim{padding:16px 12px;font-size:12px}.empty.slim.note{padding:10px 12px;font-size:11px;line-height:1.45;text-align:left;overflow-wrap:anywhere}
.continue-hint{padding:7px 10px;border:1px dashed #888;background:#f3f3f3;color:#666;font-size:11px;font-weight:500;text-align:center}

.pool-card{min-width:0;border:1px solid #888;background:#fff}
.pool-card.is-open{border-bottom-style:dashed}.pool-card.is-cont{border-top-style:dashed}
.pool-card.is-cont .pool-head{background:#f4f4f4}
.pool-head{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid #aaa}
.pool-head.has-banner{min-height:80px;padding-left:92px}
.pool-head>.pool-title,.pool-head>.pool-total{position:relative;z-index:1}
.pool-banner{position:absolute;z-index:0;inset:0 auto 0 0;width:86px;display:flex;justify-content:center;align-items:center;overflow:hidden;pointer-events:none}
.pool-banner::after{content:"";position:absolute;z-index:2;inset:0;background:linear-gradient(90deg,rgba(255,255,255,.04) 0%,rgba(255,255,255,.10) 58%,#fff 100%)}
.pool-banner img{position:relative;z-index:1;flex:none;filter:saturate(.96) contrast(1.08)}
.pool-banner img.character-banner{width:86px;height:100%;object-fit:cover;object-position:center 42%;-webkit-mask-image:radial-gradient(ellipse 82% 78% at center,#000 42%,rgba(0,0,0,.82) 64%,transparent 100%);mask-image:radial-gradient(ellipse 82% 78% at center,#000 42%,rgba(0,0,0,.82) 64%,transparent 100%)}
.pool-banner.multi-banner img.character-banner{width:43px}
.pool-banner img.weapon-banner{width:78px;height:100%;object-fit:contain;-webkit-mask-image:radial-gradient(ellipse 86% 82% at center,#000 58%,rgba(0,0,0,.82) 72%,transparent 100%);mask-image:radial-gradient(ellipse 86% 82% at center,#000 58%,rgba(0,0,0,.82) 72%,transparent 100%)}
.pool-banner.multi-banner img.weapon-banner{width:43px}
.pool-title{min-width:0}.pool-title strong{display:block;font-size:17px;line-height:1.3;overflow-wrap:anywhere;text-wrap:balance}
.pool-title strong em{margin-left:2px;color:#888;font-size:13px;font-style:normal;white-space:nowrap}
.pool-title .pool-meta{display:block;margin-top:3px;color:#777;font-size:11px;line-height:1.35;overflow-wrap:anywhere}.pool-meta .mp{white-space:nowrap}
.current-tag{display:block;max-width:300px;margin-bottom:4px;padding:2px 6px;border:1px solid #222;background:#222;color:#8a8a8a;font-size:10px;letter-spacing:.12em;line-height:1.2}
.pool-total{text-align:right}.pool-total b{display:block;font-size:22px;line-height:1.15}.pool-total span{display:block;color:#777;font-size:10px;line-height:1.4;white-space:nowrap}
.pity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-bottom:1px solid #aaa;background:#ececec}.pity-grid.pity-two{grid-template-columns:repeat(2,minmax(0,1fr))}
.pity-item{min-width:0;padding:7px 9px;border-right:1px solid #bbb}.pity-item:last-child{border-right:0}
.pity-grid.pity-four{grid-template-columns:repeat(2,minmax(0,1fr))}.pity-four .pity-item:nth-child(2n){border-right:0}.pity-four .pity-item:nth-child(-n+2){border-bottom:1px solid #bbb}
.pity-item span{display:block;color:#666;font-size:9px;font-weight:700;white-space:nowrap}.pity-item b{display:block;margin-top:2px;font-size:15px;white-space:nowrap}
.pity-item small{display:block;margin-top:1px;color:#777;font-size:9px;line-height:1.35;overflow-wrap:anywhere}.pity-item small+small{margin-top:0}
.pull-bars{display:grid;grid-template-columns:minmax(0,1fr);gap:6px;padding:9px 10px 10px}
.pull-row{display:grid;grid-template-columns:40px clamp(120px,36%,260px) minmax(0,1fr);align-items:center;gap:7px;min-width:0}
.gacha-thumb,.current-marker{width:40px;height:40px;display:grid;place-items:center;overflow:hidden;border:1px solid #777;background:#eee}
.gacha-thumb{border:2px solid #222}.gacha-thumb img{width:100%;height:100%;object-fit:contain}.gacha-thumb span{font-size:11px;font-weight:700}.gacha-thumb.gift span{font-size:10px}
.current-marker{color:#555;font-size:11px;font-weight:700}
.pull-copy{min-width:0}.pull-copy strong{display:block;font-size:13.5px;line-height:1.3;overflow-wrap:anywhere;text-wrap:balance}
.pull-copy time{display:block;margin-top:3px;color:#777;font-size:9px;line-height:1.35;overflow-wrap:anywhere}
.bar-track{position:relative;min-width:0;min-height:34px;border:1px solid #999;background:#ededed}
.bar-fill{position:absolute;top:0;bottom:0;left:0;min-width:58px;max-width:100%;background:#333}.bar-fill.current{background:#777}
.bar-value{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;min-height:32px;padding:4px 9px}.bar-value b{flex:0 0 auto;color:#fff;white-space:nowrap;font-size:15px}
.pity-hits{display:flex;flex-wrap:wrap;align-items:center;gap:4px;min-width:0}
.pity-hit{padding:3px 6px;border:2px solid #111;background:#fff;color:#111;font-size:10px;font-weight:700;line-height:1;white-space:nowrap;box-shadow:0 0 0 1px #fff}
.pity-hit-small{border-color:#3f6078;background:#e5eef4;color:#29485d;box-shadow:0 0 0 1px #f7fbfd}
.pity-hit-large{border-color:#7a5c2e;background:#f4ead7;color:#5d421d;box-shadow:0 0 0 1px #fffaf0}
.pity-hit-miss{padding:3px 7px;border:2px solid #f8e9e9;background:#8a3f46;color:#fff;font-size:11px;letter-spacing:.12em;box-shadow:0 0 0 1px #5a2328}
.pool-empty{padding:4px 0;color:#777;font-size:11px}
.free-row{display:grid;grid-template-columns:72px minmax(0,1fr) auto;align-items:center;gap:7px;min-height:46px;padding:4px 6px;border:1px dashed #777;background:#f0f0f0}
.free-icons{display:flex;align-items:center;min-width:0;min-height:36px}.free-icons .gacha-thumb{flex:none;width:34px;height:34px;margin-right:-6px;background:#fff}
.free-icons.many .gacha-thumb{margin-right:-16px}.free-icons .gacha-thumb:last-child{margin-right:0}
.free-marker{width:68px;height:34px;display:grid;place-items:center;border:2px solid #555;color:#444;font-size:10px;font-weight:700;letter-spacing:.06em;white-space:nowrap}
.free-count{padding:4px 6px;border:1px solid #777;background:#fff;font-size:10.5px;font-weight:700;white-space:nowrap}

.gacha-source{display:flex;justify-content:space-between;gap:20px;margin-top:12px;padding-top:10px;border-top:2px solid #222;color:#777;font-size:12px;font-weight:500}
[data-m],[data-mp],[data-mc]{display:grid;grid-template-columns:minmax(0,1fr);min-width:0}
"""
