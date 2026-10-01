"""Render Endfield gacha analysis previews (v3 layout) from synthetic accounts.

No credentials and no network: records, item names, icons and pool banners are all
synthetic (grayscale placeholder PNGs drawn locally). Every account goes through the
real code path ``build_gacha_analysis`` → ``rendering.cards.draw_gacha_analysis_cards``;
the page HTML the renderer screenshots is captured and re-checked in the same browser
(width, height limit, horizontal overflow, clipping, column order, every record rendered
exactly once), and the report is written to ``<output-dir>/validation.json``.

    python scripts/render_endfield_gacha_preview.py --output-dir output/gacha-preview

Extra variants in the same run (width / height cap / rerun side are patched for that render only):

    --both-sides normal no-rerun          also render these as <prefix>-<account>-right / -left
    --candidate-widths 1600 1920 1920x5120
                                          render --candidate-accounts (default: normal) at each width
                                          (optionally WxH = width x page height cap) as <prefix>-cand-<W>-<account>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from otae_bot.infrastructure.rendering.browser import close_browser, evaluate_web_page  # noqa: E402
from plugins.endfield.account.store import EndfieldRole, GachaRecord, SyncState  # noqa: E402
from plugins.endfield.gacha import draw as gacha_draw  # noqa: E402
from plugins.endfield.gacha.assets import GachaItemMetadata, GachaPoolBanner, GachaPoolRule  # noqa: E402
from plugins.endfield.gacha.service import build_gacha_analysis  # noqa: E402
from plugins.endfield.rendering import cards  # noqa: E402

ACCOUNTS = ("normal", "heavy", "sparse", "stress", "standard-hidden", "no-rerun")
UIDS = {"normal": "100000001234", "standard-hidden": "100000001234", "sparse": "100000005678",
        "no-rerun": "100000004321"}
FORWARD_ABOVE = 3                       # 与 handlers.py 默认的 ENDFIELD_GACHA_FORWARD_ABOVE 一致，只用于报告
NOW = datetime(2026, 9, 30, 8, 23)
SPECIAL, RERUN, JOINT, STANDARD, BEGINNER = (
    f"E_CharacterGachaPoolType_{name}" for name in ("Special", "Rerun", "Joint", "Standard", "Beginner")
)
STD_CHARS = ("示例常驻角色甲", "示例常驻角色乙", "示例常驻角色丙", "示例常驻角色丁", "示例超长名字的常驻六星干员测试")
STD_WEAPONS = ("示例六星武器甲", "示例六星武器乙", "示例六星武器丙", "示例超长名字的六星武器测试名称")


# ============================================================ 本地占位素材（灰度，不联网）

def _placeholder_icon(path: Path, seed: int, *, weapon: bool) -> None:
    rng = random.Random(seed)
    shade = rng.randint(150, 215)
    image = Image.new("L", (96, 96), shade)
    draw = ImageDraw.Draw(image)
    tone = max(40, shade - 90)
    if weapon:
        draw.polygon([(20, 76), (70, 18), (80, 28), (30, 86)], fill=tone)
        draw.rectangle((16, 70, 34, 88), fill=max(20, tone - 30))
    else:
        draw.ellipse((30, 14, 66, 50), fill=tone)
        draw.pieslice((14, 48, 82, 120), 180, 360, fill=tone)
    draw.rectangle((0, 0, 95, 95), outline=max(20, tone - 40), width=3)
    image.convert("RGB").save(path)


def _placeholder_banner(path: Path, seed: int, *, weapon: bool) -> None:
    rng = random.Random(seed)
    image = Image.new("LA", (240, 260), (0, 0))       # 透明底，与正式立绘 / 武器图一致
    draw = ImageDraw.Draw(image)
    tone = (rng.randint(70, 130), 255)
    if weapon:
        draw.polygon([(40, 220), (180, 40), (205, 62), (66, 242)], fill=tone)
    else:
        draw.ellipse((80, 30, 160, 120), fill=tone)
        draw.pieslice((30, 110, 210, 330), 180, 360, fill=tone)
    image.convert("RGBA").save(path)


# ============================================================ 合成抽卡记录（全部虚构）

@dataclass
class PoolPlan:
    pool_id: str
    name: str
    pool_type: str          # 角色：官方枚举；武器：weapon
    item_type: str
    paid: int               # 角色：付费抽数；武器：申领次数（每次 10 抽）
    days: int
    up_id: str = ""
    version: int = 0
    series: str = ""        # 重构寻访：系列状态键（加急招募按系列累计 30/60/90）
    banner: bool = False


@dataclass
class Synth:
    seed: int
    assets: Path
    records: list[GachaRecord] = field(default_factory=list)
    metadata: dict[str, GachaItemMetadata] = field(default_factory=dict)
    keepsakes: dict[str, GachaItemMetadata] = field(default_factory=dict)
    rules: dict[str, GachaPoolRule] = field(default_factory=dict)
    banners: dict[str, tuple[GachaPoolBanner, ...]] = field(default_factory=dict)
    chains: Counter = field(default_factory=Counter)
    series_paid: Counter = field(default_factory=Counter)
    series_rush: dict[str, set[int]] = field(default_factory=dict)
    seq: int = 0

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        for index, name in enumerate(STD_CHARS):
            self.item(f"chr_std_{index}", name, "角色")
        for index, name in enumerate(STD_WEAPONS):
            self.item(f"wpn_std_{index}", name, "武器")

    def item(self, item_id: str, name: str, item_type: str) -> str:
        if item_id not in self.metadata:
            icon = self.assets / f"icon_{item_id}.png"
            if not icon.exists():
                _placeholder_icon(icon, zlib.crc32(item_id.encode()), weapon=item_type == "武器")
            self.metadata[item_id] = GachaItemMetadata(item_id, name, 6, item_type, icon_path=str(icon))
        return item_id

    def up(self, item_id: str, name: str, item_type: str, *, keepsake: bool = True) -> str:
        self.item(item_id, name, item_type)
        if item_type == "角色" and keepsake and item_id not in self.keepsakes:
            icon = self.assets / f"keepsake_{item_id}.png"
            if not icon.exists():
                _placeholder_icon(icon, zlib.crc32(item_id.encode()) + 7, weapon=True)
            self.keepsakes[item_id] = GachaItemMetadata(
                f"item_charpotentialup_{item_id}", f"{name}的信物", 6, "信物", icon_path=str(icon),
            )
        return item_id

    def _add(self, plan: PoolPlan, ts: int, item_id: str, rarity: int, *, free: bool = False) -> None:
        self.seq += 1
        name = self.metadata[item_id].name if item_id in self.metadata else ("五星" if rarity == 5 else "四星")
        self.records.append(GachaRecord(
            "100000001234", "1", plan.pool_id, plan.name, plan.pool_type, str(self.seq), ts,
            item_id, name, rarity, plan.item_type, is_free=free, pool_version=plan.version,
        ))

    def _register(self, plan: PoolPlan) -> None:
        if plan.up_id:
            self.rules[plan.pool_id] = GachaPoolRule(plan.pool_id, (plan.up_id,), 0, plan.name)
        if plan.banner and plan.up_id:
            image = self.assets / f"banner_{plan.up_id}.png"
            if not image.exists():
                _placeholder_banner(image, zlib.crc32(plan.up_id.encode()), weapon=plan.item_type == "武器")
            self.banners[plan.pool_id] = (
                GachaPoolBanner(plan.up_id, self.metadata[plan.up_id].name, plan.item_type, str(image)),
            )

    def timeline(self, plans: list[PoolPlan], *, end: datetime = NOW, gap_days: int = 2) -> None:
        """按给定顺序（旧 → 新）生成；最后一个池结束于 end，之前的池依次往前排，互不重叠。"""
        windows = []
        cursor = end
        for plan in reversed(plans):
            windows.append((plan, cursor - timedelta(days=plan.days), cursor))
            cursor -= timedelta(days=plan.days + gap_days)
        for plan, start, stop in reversed(windows):
            self._register(plan)
            if plan.item_type == "武器":
                self._weapon(plan, start, stop)
            else:
                self._character(plan, start, stop)

    def _character(self, plan: PoolPlan, start: datetime, stop: datetime) -> None:
        family = {SPECIAL: "special", RERUN: "rerun", STANDARD: "standard"}.get(plan.pool_type, plan.pool_id)
        batches = max(1, (plan.paid + 9) // 10)
        step = max(60, int((stop - start).total_seconds() // (batches + 4)))
        ts = int(start.timestamp())
        position = 0
        got_up = False
        pool_six = False
        free_ten_done = False
        while position < plan.paid:
            for _ in range(min(10, plan.paid - position)):
                position += 1
                self.chains[family] += 1
                since = self.chains[family]
                rate = 1.0 if since >= 80 else 0.008 + max(0, since - 65) * 0.06
                forced_up = bool(plan.up_id) and not got_up and position == 120
                beginner_guarantee = plan.pool_type == BEGINNER and position == 40 and not pool_six
                six = forced_up or beginner_guarantee or self.rng.random() < rate
                if six:
                    is_up = bool(plan.up_id) and (forced_up or self.rng.random() < 0.5)
                    got_up = got_up or is_up
                    pool_six = True
                    item_id = plan.up_id if is_up else f"chr_std_{self.rng.randrange(len(STD_CHARS))}"
                    self.chains[family] = 0
                    self._add(plan, ts, item_id, 6)
                else:
                    rarity = 5 if self.rng.random() < 0.08 else 4
                    self._add(plan, ts, f"filler_{rarity}", rarity)
            ts += step
            if plan.pool_type == SPECIAL and not free_ten_done and position >= 30:
                self._free_ten(plan, ts, up_rate=0.02)
                free_ten_done = True
                ts += step
            if plan.series:
                total = self.series_paid[plan.series] + position
                granted = self.series_rush.setdefault(plan.series, set())
                for threshold in (30, 60, 90):
                    if total >= threshold and threshold not in granted:
                        granted.add(threshold)
                        self._free_ten(plan, ts, up_rate=0.04)
                        ts += step
        if plan.series:
            self.series_paid[plan.series] += plan.paid

    def _free_ten(self, plan: PoolPlan, ts: int, *, up_rate: float) -> None:
        for _ in range(10):
            if self.rng.random() < up_rate:
                item_id = plan.up_id if plan.up_id and self.rng.random() < 0.5 else \
                    f"chr_std_{self.rng.randrange(len(STD_CHARS))}"
                self._add(plan, ts, item_id, 6, free=True)
            else:
                self._add(plan, ts, "filler_4", 4, free=True)

    def _weapon(self, plan: PoolPlan, start: datetime, stop: datetime) -> None:
        step = max(60, int((stop - start).total_seconds() // (plan.paid + 2)))
        ts = int(start.timestamp())
        position = 0
        got_up = False
        dry = 0
        for _ in range(plan.paid):
            batch_six = False
            for slot in range(10):
                position += 1
                forced_up = bool(plan.up_id) and not got_up and position == 80
                six = forced_up or self.rng.random() < 0.04 or (dry == 3 and slot == 9 and not batch_six)
                if six:
                    is_up = bool(plan.up_id) and (forced_up or self.rng.random() < 0.5)
                    got_up = got_up or is_up
                    item_id = plan.up_id if is_up else f"wpn_std_{self.rng.randrange(len(STD_WEAPONS))}"
                    batch_six = True
                    self._add(plan, ts, item_id, 6)
                else:
                    self._add(plan, ts, "filler_w", 5 if self.rng.random() < 0.1 else 4)
            dry = 0 if batch_six else dry + 1
            ts += step


def _char(pool_id, name, pool_type, paid, days, up="", **kwargs) -> PoolPlan:
    return PoolPlan(pool_id, name, pool_type, "角色", paid, days, up, **kwargs)


def _weapon(pool_id, name, claims, days, up="", **kwargs) -> PoolPlan:
    return PoolPlan(pool_id, name, "weapon", "武器", claims, days, up, **kwargs)


def _build(slug: str, assets: Path):
    """返回 (role, synth, show_standard)。"""
    show_standard = slug != "standard-hidden"
    if slug in ("normal", "standard-hidden"):
        synth = Synth(20261001, assets)
        a = synth.up("chr_up_a", "示例UP角色", "角色")
        b = synth.up("chr_up_b", "示例UP角色B", "角色")
        c = synth.up("chr_up_c", "示例UP角色C", "角色")
        rr = synth.up("chr_up_rerun", "示例重构UP", "角色")
        wa = synth.up("wpn_up_a", "示例UP武器", "武器")
        art = synth.up("wpn_up_art", "艺术暴君", "武器")
        synth.timeline([
            _char("special_c", "特许寻访·示例C", SPECIAL, 140, 9, c),
            _char("special_b", "特许寻访·示例B", SPECIAL, 75, 2, b),
            _char("special_a", "特许寻访·示例A", SPECIAL, 132, 6, a, banner=True),
        ])
        synth.timeline([_char("rerun_chr_example", "绚丽异彩", RERUN, 64, 5, rr, version=1, series="rr",
                              banner=True)])
        synth.timeline([_char("beginner", "启程寻访", BEGINNER, 40, 1)], end=NOW - timedelta(days=38))
        synth.timeline([_char("standard", "基础寻访", STANDARD, 210, 25)], end=NOW - timedelta(days=3))
        synth.timeline([
            _weapon("weaponbox_constant_a", "常驻武库申领", 3, 3),
            _weapon("rerun_wpn_example", "点绘申领", 11, 4, art, version=1, banner=True),
            _weapon("weponbox_a", "武库申领·示例A", 6, 1, wa, banner=True),
        ])
        nickname = "示例玩家"
    elif slug == "sparse":
        synth = Synth(20261002, assets)
        a = synth.up("chr_up_a", "示例UP角色", "角色")
        synth.timeline([_char("beginner", "启程寻访", BEGINNER, 10, 1)], end=NOW - timedelta(days=1))
        synth.timeline([_char("special_a", "特许寻访·示例A", SPECIAL, 25, 1, a)])
        # 新号：只抽了 25 抽的特许池不出六星（固定种子下也强制）
        synth.records = [
            record if record.rarity < 6 else GachaRecord(
                record.role_id, record.server_id, record.pool_id, record.pool_name, record.pool_type,
                record.seq_id, record.gacha_ts, "filler_4", "四星", 4, record.item_type,
                is_free=record.is_free, pool_version=record.pool_version,
            )
            for record in synth.records
        ]
        nickname = "示例新玩家"
    elif slug == "no-rerun":
        # 从不抽重构寻访：版面应折叠为两栏（特许寻访 + 其他寻访 | 武器申领），不留空列
        synth = Synth(20261003, assets)
        a = synth.up("chr_up_a", "示例UP角色", "角色")
        b = synth.up("chr_up_b", "示例UP角色B", "角色")
        wa = synth.up("wpn_up_a", "示例UP武器", "武器")
        wb = synth.up("wpn_up_b", "示例UP武器B", "武器")
        synth.timeline([
            _char("special_b", "特许寻访·示例B", SPECIAL, 160, 8, b),
            _char("special_a", "特许寻访·示例A", SPECIAL, 96, 6, a, banner=True),
        ])
        synth.timeline([_char("joint_glow", "辉光庆典", JOINT, 60, 6, synth.up("chr_up_joint", "示例联合UP", "角色",
                                                                              keepsake=False))],
                       end=NOW - timedelta(days=20))
        synth.timeline([_char("beginner", "启程寻访", BEGINNER, 40, 1)], end=NOW - timedelta(days=40))
        synth.timeline([_char("standard", "基础寻访", STANDARD, 150, 25)], end=NOW - timedelta(days=3))
        synth.timeline([
            _weapon("weaponbox_constant_a", "常驻武库申领", 4, 3),
            _weapon("weponbox_b", "武库申领·示例B", 5, 2, wb),
            _weapon("weponbox_a", "武库申领·示例A", 7, 2, wa, banner=True),
        ])
        nickname = "示例无重构玩家"
    else:
        stress = slug == "stress"
        synth = Synth(20261001 + (7 if stress else 0), assets)
        rng = random.Random(99 if stress else 98)
        cur = synth.up("chr_up_dawn", "示例UP角色·晨曦", "角色")
        special = [_char("special_long", "特许寻访·示例名称很长很长的历史卡池用于换行测试", SPECIAL, 860, 18,
                         synth.up("chr_up_01", "示例UP角色·01", "角色"))]
        special += [
            _char(f"special_h{index:02d}", f"特许寻访·示例历史池{index:02d}", SPECIAL, count * 62, 18,
                  synth.up(f"chr_up_{index:02d}", f"示例UP角色·{index:02d}", "角色"))
            for index, count in enumerate((13, 12, 12, 11, 11, 10, 10, 10, 10, 10), start=2)
        ]
        if stress:
            special = [
                _char(f"special_s{index:03d}", f"特许寻访·压力测试池{index:03d}", SPECIAL, rng.randint(1, 4) * 62,
                      3, synth.up(f"chr_up_s{index:03d}", f"示例UP角色·压测{index:03d}", "角色"))
                for index in range(1, 91)
            ] + [
                _char(f"special_big{index}", f"特许寻访·压力测试超大池{index}", SPECIAL, count * 62, 30,
                      synth.up(f"chr_up_big{index}", f"示例UP角色·超大{index}", "角色"))
                for index, count in enumerate((48, 36), start=1)
            ] + special
        special.append(_char("special_dawn", "特许寻访·示例晨曦之下漫长归途的再会与告别", SPECIAL, 1180, 20, cur,
                             banner=True))
        synth.timeline(special)

        rr = synth.up("chr_up_rerun", "示例重构UP", "角色")
        rerun = [
            _char("rerun_chr_example", "绚丽异彩", RERUN, 150, 6, rr, version=1, series="rr"),
            _char("rerun_chr_long", "示例重构·名字非常非常长的重构寻访卡池", RERUN, 130, 10,
                  synth.up("chr_up_r2", "示例重构UP·长", "角色"), version=1, series="r2"),
            _char("rerun_chr_flow", "示例重构·流光回响", RERUN, 190, 10,
                  synth.up("chr_up_r1", "示例重构UP·流光", "角色"), version=1, series="r1"),
        ]
        if stress:
            rerun[1:1] = [
                _char(f"rerun_chr_s{index:02d}", f"示例重构·压力测试{index:02d}", RERUN, rng.randint(1, 5) * 60, 4,
                      synth.up(f"chr_up_rs{index:02d}", f"示例重构UP·压测{index:02d}", "角色"),
                      version=1, series=f"rs{index}")
                for index in range(1, 9)
            ]
        # heavy：同 poolId 再开放（poolVersion 1 → 2）；stress：复刻换新 poolId
        rerun.append(_char("rerun_chr_example_b" if stress else "rerun_chr_example", "绚丽异彩", RERUN, 380, 6, rr,
                           version=1 if stress else 2, series="rr", banner=True))
        synth.timeline(rerun)

        synth.timeline([_char("joint_glow", "辉光庆典", JOINT, 120, 8, synth.up("chr_up_joint", "示例联合UP", "角色",
                                                                               keepsake=False))],
                       end=NOW - timedelta(days=120))
        synth.timeline([_char("standard", "基础寻访", STANDARD, (72 if stress else 46) * 62, 300)],
                       end=NOW - timedelta(days=2))
        synth.timeline([_char("beginner", "启程寻访", BEGINNER, 40, 1)], end=NOW - timedelta(days=314))

        art = synth.up("wpn_up_art", "艺术暴君", "武器")
        weapons = [
            _weapon(f"weponbox_h{index:02d}", f"武库申领·示例历史{index:02d}", rng.randint(22, 32), 15,
                    synth.up(f"wpn_up_{index:02d}", f"示例UP武器·{index:02d}", "武器"))
            for index in range(1, 11)
        ]
        if stress:
            weapons = [
                _weapon(f"weponbox_s{index:03d}", f"武库申领·压力测试{index:03d}", rng.randint(2, 9), 2,
                        synth.up(f"wpn_up_s{index:03d}", f"示例UP武器·压测{index:03d}", "武器"))
                for index in range(1, 71)
            ] + weapons
        weapons += [
            _weapon(f"weaponbox_constant_{index}", f"常驻武库申领·示例{label}", claims, 30,
                    synth.up(f"wpn_const_{index}", f"示例常驻UP武器·{index}", "武器"))
            for index, (label, claims) in enumerate((("三", 3), ("二", 6), ("一", 10)), start=1)
        ]
        weapons += [
            _weapon("rerun_wpn_example", "点绘申领", 12, 5, art, version=1),
            _weapon("rerun_wpn_example_b" if stress else "rerun_wpn_example", "点绘申领", 28, 5, art,
                    version=1 if stress else 2, banner=True),
            _weapon("weponbox_dawn", "武库申领·示例超长名称的限时武器申领卡池测试", 36, 20,
                    synth.up("wpn_up_dawn", "示例UP武器·晨曦", "武器"), banner=True),
        ]
        synth.timeline(weapons)
        nickname = "示例压力测试账号" if stress else "示例玩家名字特别特别长的重度测试账号昵称再长一点"
    role = EndfieldRole(1, 1, "preview", "preview", UIDS.get(slug, "100000009876"), "1", nickname, "示例服务器", True)
    return role, synth, show_standard


def build_preview_analysis(slug: str, assets: Path):
    role, synth, show_standard = _build(slug, assets)
    synced = int(NOW.timestamp())
    states = [
        SyncState(role.role_id, role.server_id, stream, last_sync_at=synced)
        for stream in (f"char:{SPECIAL}", f"char:{RERUN}", f"char:{JOINT}", f"char:{STANDARD}",
                       f"char:{BEGINNER}", "weapon:all")
    ]
    return build_gacha_analysis(
        role, synth.records, states, synth.metadata, synth.rules,
        keepsake_metadata=synth.keepsakes, pool_banners=synth.banners, show_standard=show_standard,
    )


# ============================================================ 校验（在同一浏览器里重新加载截图用的 HTML）

VALIDATE_JS = """
() => {
  const card = document.querySelector('.gacha-analysis-card');
  const cr = card.getBoundingClientRect();
  const issues = {overflow_x: [], clipped: [], escapes: [], overlaps: [], ellipsis_styles: []};
  const desc = el => {
    const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
    return el.tagName.toLowerCase() + cls + ' «' + (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40) + '»';
  };
  const decorative = el => !!el.closest('.pool-banner');
  for (const el of [card, ...card.querySelectorAll('*')]) {
    const cs = getComputedStyle(el);
    if (cs.textOverflow === 'ellipsis' || (cs.webkitLineClamp && cs.webkitLineClamp !== 'none')) issues.ellipsis_styles.push(desc(el));
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (el !== card && (r.right > cr.right + 0.5 || r.left < cr.left - 0.5 || r.bottom > cr.bottom + 0.5 || r.top < cr.top - 0.5))
      issues.escapes.push(desc(el));
    if (cs.display === 'inline' || decorative(el)) continue;
    const ox = el.scrollWidth - el.clientWidth;
    if (ox > 1) (cs.overflowX !== 'visible' ? issues.clipped : issues.overflow_x).push(desc(el) + ' +' + ox + 'px');
    if (cs.overflowY !== 'visible' && el.scrollHeight - el.clientHeight > 1) issues.clipped.push(desc(el) + ' vertical');
  }
  for (const box of card.querySelectorAll('.pool-column, .total, .metric, header, .pool-card, .pull-row, .free-row')) {
    const br = box.getBoundingClientRect();
    for (const el of box.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (decorative(el)) continue;
      if (r.width && (r.right > br.right + 0.5 || r.left < br.left - 0.5 || r.bottom > br.bottom + 0.5 || r.top < br.top - 0.5))
        issues.escapes.push(desc(el) + ' ⊄ ' + desc(box).slice(0, 30));
    }
  }
  const groups = '.summary,.metric-head,.expectation-row,.expectation-values,.pool-columns,.column-head,.pool-stack,.stack-divider,'
    + '.pool-head,.pool-title,.pool-total,.pity-grid,.pity-item,.pull-bars,.pull-row,.pull-copy,.bar-value,.pity-hits,.free-row,'
    + 'header,header h1,.gacha-source';
  for (const g of card.querySelectorAll(groups)) {
    const kids = [...g.children].filter(k => { const r = k.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(k).display !== 'inline' && getComputedStyle(k).position !== 'absolute'; });
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), hgt = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 1 && hgt > 1) issues.overlaps.push(desc(kids[i]) + ' × ' + desc(kids[j]));
    }
  }
  const rows = [...card.querySelectorAll('[data-row]')].map(el => {
    const piece = el.closest('.pool-card'), pr = piece.getBoundingClientRect(), r = el.getBoundingClientRect();
    return {id: el.dataset.row, kind: el.dataset.kind, pool: piece.dataset.pool,
            whole: r.top >= pr.top - 0.5 && r.bottom <= pr.bottom + 0.5 && r.top >= cr.top - 0.5 && r.bottom <= cr.bottom + 0.5 && r.height > 0};
  });
  const pieces = [...card.querySelectorAll('.pool-card')].map(c => ({pool: c.dataset.pool, cont: c.classList.contains('is-cont')}));
  const images = [...card.querySelectorAll('img')];
  return {width: cr.width, height: cr.height, rows, pieces,
          broken_images: images.filter(i => !i.naturalWidth).map(desc),
          external_images: images.filter(i => !/^(data|file):/.test(i.getAttribute('src') || '')).map(desc),
          folded_elements: card.querySelectorAll('.fold-card,.fold-row,.fold-head,.pool-more').length,
          columns: [...card.querySelectorAll('.pool-column')].map(c => {
            const r = c.getBoundingClientRect();
            return {key: c.dataset.key, width: Math.round(r.width), height: Math.round(r.height)};
          }),
          summary_tiles: [...card.querySelectorAll('.summary .metric-head span')].map(s => s.textContent.trim()),
          ...issues};
}
"""


def _png_size(content: bytes) -> tuple[int, int]:
    return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")


@dataclass(frozen=True)
class Variant:
    slug: str
    stem: str               # 输出文件名前缀：<stem>.png 或 <stem>-<n>.png
    width: int
    max_height: int
    rerun_side: str


async def _validate(document: str, width: int) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(document)
        path = Path(file.name)
    try:
        return await evaluate_web_page(path.as_uri(), VALIDATE_JS, viewport=(width, 900))
    finally:
        path.unlink(missing_ok=True)


async def render_account(variant: Variant, output_dir: Path, assets: Path, keep_html: bool) -> dict:
    slug = variant.slug
    analysis = build_preview_analysis(slug, assets)
    layout = gacha_draw.build_gacha_columns(analysis, rerun_side=variant.rerun_side)
    expected_columns = [column.spec.key for column in layout]
    expected_tiles = [column.spec.title for column in layout]        # 总览格顺序与栏序一致
    documents: list[str] = []
    original_write = gacha_draw._write_temp_html

    def capture(document: str):
        documents.append(document)
        return original_write(document)

    started = perf_counter()
    with (
        patch.object(gacha_draw, "_write_temp_html", capture),
        patch.object(gacha_draw, "GACHA_CARD_WIDTH", variant.width),
        patch.object(gacha_draw, "GACHA_PAGE_MAX_HEIGHT", variant.max_height),
        patch.dict(os.environ, {gacha_draw.GACHA_RERUN_SIDE_ENV: variant.rerun_side}),
    ):
        pngs = await cards.draw_gacha_analysis_cards(analysis, uid=analysis.role.masked_uid)
    elapsed = perf_counter() - started
    page_documents = [doc for doc in documents if 'data-measure="first"' not in doc]
    for stale in [*output_dir.glob(f"{variant.stem}.png"), *output_dir.glob(f"{variant.stem}-[0-9]*.png"),
                  *output_dir.glob(f"{variant.stem}.html"), *output_dir.glob(f"{variant.stem}-[0-9]*.html")]:
        stale.unlink()

    results = []
    for index, png in enumerate(pngs, start=1):
        stem = variant.stem if len(pngs) == 1 else f"{variant.stem}-{index}"
        (output_dir / f"{stem}.png").write_bytes(png)
        document = page_documents[index - 1] if index - 1 < len(page_documents) else ""
        if keep_html and document:
            (output_dir / f"{stem}.html").write_text(document, encoding="utf-8")
        report = await _validate(document, variant.width) if document else \
            {"rows": [], "pieces": [], "missing_html": True}
        pixels = _png_size(png)
        css_height = report.get("height", pixels[1] / 2)
        report.update({
            "file": f"{stem}.png",
            "png_pixels": pixels,
            "png_bytes": len(png),
            "css_size": [round(report.get("width", pixels[0] / 2), 1), round(css_height, 1)],
            "width_ok": pixels[0] == 2 * variant.width,
            "within_height_limit": pixels[1] <= 2 * variant.max_height and css_height <= variant.max_height,
            "columns_ok": [column["key"] for column in report.get("columns", [])] == expected_columns,
            "summary_ok": report.get("summary_tiles") == (expected_tiles if index == 1 else []),
        })
        report["ok"] = bool(
            not report.get("missing_html") and report["width_ok"] and report["within_height_limit"]
            and report["columns_ok"] and report["summary_ok"]
            and not any(report.get(key) for key in (
                "overflow_x", "clipped", "escapes", "overlaps", "ellipsis_styles", "broken_images",
                "external_images", "folded_elements",
            ))
            and all(row["whole"] for row in report["rows"])
        )
        results.append(report)

    # ---- 记录完整性：期望值直接从 GachaAnalysis 计算（与渲染代码无关）
    hidden = [pool for pool in analysis.pools if pool.kind_key == "standard" and not analysis.show_standard_pools]
    visible = [pool for pool in analysis.pools if pool not in hidden]
    expected = {
        pool.card_key: len(pool.six_stars) + len(pool.keepsake_gifts) + len(pool.free_batches) for pool in visible
    }
    rendered_rows = [row for report in results for row in report["rows"]]
    record_rows = [row for row in rendered_rows if row["kind"] in ("six", "gift", "free")]
    per_pool = Counter(row["pool"] for row in record_rows)
    ids = Counter(row["id"] for row in rendered_rows)
    heads = Counter(piece["pool"] for report in results for piece in report["pieces"] if not piece["cont"])
    split_pools = sorted({piece["pool"] for report in results for piece in report["pieces"] if piece["cont"]})
    completeness = {
        "records_expected": sum(expected.values()),
        "records_rendered": len(record_rows),
        "records_missing": sum(max(0, count - per_pool.get(key, 0)) for key, count in expected.items()),
        "records_unexpected": sum(max(0, count - expected.get(key, 0)) for key, count in per_pool.items()),
        "rows_duplicated": sum(count - 1 for count in ids.values() if count > 1),
        "rows_cut": sum(1 for row in rendered_rows if not row["whole"]),
        "pools_expected": len(visible),
        "pools_with_one_full_head": sum(1 for pool in visible if heads[pool.card_key] == 1),
        "pools_hidden_by_setting": [pool.name for pool in hidden],
        "pools_split_across_pages": len(split_pools),
    }
    completeness["truncated_or_omitted"] = (
        completeness["records_missing"] + completeness["records_unexpected"] + completeness["rows_duplicated"]
        + completeness["rows_cut"] + completeness["pools_expected"] - completeness["pools_with_one_full_head"]
    )
    for report in results:
        report["records_on_page"] = len(report.pop("rows"))
        report["pool_pieces"] = len(report.pop("pieces"))
    heights = [report["css_size"][1] for report in results]
    return {
        "account": slug,
        "stem": variant.stem,
        "variant": "GACHA_SHOW_STANDARD=False" if not analysis.show_standard_pools else "default",
        "width": variant.width,
        "max_height": variant.max_height,
        "rerun_side": variant.rerun_side,
        "columns": expected_columns,
        "facts": {
            "nickname": analysis.role.nickname,
            "pools": len(analysis.pools),
            "kinds": dict(Counter(pool.kind_key for pool in analysis.pools)),
            "total": analysis.total,
            "character_pulls": sum(pool.total for pool in analysis.pools if pool.item_type == "角色"),
            "weapon_pulls": sum(pool.total for pool in analysis.pools if pool.item_type == "武器"),
            "rerun_periods": [
                {"card_key": pool.card_key, "pool_id": pool.pool_id, "pool_version": pool.pool_version,
                 "series_key": pool.series_key, "series_index": pool.series_index, "current": pool.is_current}
                for pool in sorted(analysis.pools, key=lambda item: (item.series_key, item.series_index))
                if pool.series_run_count > 1
            ],
        },
        "pages": len(pngs),
        "delivery": "合并转发" if len(pngs) > FORWARD_ABOVE else "普通消息",
        "page_heights": heights,
        "page_height_spread": round(max(heights) - min(heights), 1) if heights else 0,
        "png_bytes_total": sum(report["png_bytes"] for report in results),
        "completeness": completeness,
        "render_seconds": round(elapsed, 2),
        "ok": all(report["ok"] for report in results) and completeness["truncated_or_omitted"] == 0,
        "results": results,
    }


def _size_token(value: str) -> tuple[int, int | None]:
    """候选尺寸：``1920`` 或 ``1920x5120``（宽 x 单页高度上限）。"""
    width, _, height = value.lower().partition("x")
    try:
        return int(width), (int(height) if height else None)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WIDTH or WIDTHxHEIGHT, got {value!r}") from None


def build_variants(args) -> list[Variant]:
    width, max_height, side = args.width, args.max_height, args.rerun_side
    variants = [Variant(slug, f"{args.prefix}-{slug}", width, max_height, side) for slug in args.accounts]
    variants += [
        Variant(slug, f"{args.prefix}-{slug}-{each}", width, max_height, each)
        for slug in args.both_sides for each in gacha_draw.GACHA_RERUN_SIDES
    ]
    for cand_width, cand_height in args.candidate_widths:
        size = f"{cand_width}" if cand_height in (None, max_height) else f"{cand_width}x{cand_height}"
        variants += [
            Variant(slug, f"{args.prefix}-cand-{size}-{slug}", cand_width, cand_height or max_height, side)
            for slug in args.candidate_accounts
        ]
    return variants


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/gacha-preview")
    parser.add_argument("--accounts", nargs="*", choices=ACCOUNTS, default=list(ACCOUNTS))
    parser.add_argument("--prefix", default="gacha-impl")
    parser.add_argument("--width", type=int, default=gacha_draw.GACHA_CARD_WIDTH)
    parser.add_argument("--max-height", type=int, default=gacha_draw.GACHA_PAGE_MAX_HEIGHT)
    parser.add_argument("--rerun-side", choices=gacha_draw.GACHA_RERUN_SIDES, default=gacha_draw.gacha_rerun_side())
    parser.add_argument("--both-sides", nargs="*", choices=ACCOUNTS, default=[],
                        help="这些账号另按 right / left 各渲染一份（<prefix>-<account>-right / -left）")
    parser.add_argument("--candidate-widths", nargs="*", type=_size_token, default=[],
                        help="候选宽度（可写 WxH 同时改单页高度上限），文件名 <prefix>-cand-<W>-<account>")
    parser.add_argument("--candidate-accounts", nargs="*", choices=ACCOUNTS, default=["normal"])
    parser.add_argument("--keep-html", action="store_true", help="同时保存每页 HTML，便于排查")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = []
    with tempfile.TemporaryDirectory(prefix="gacha-preview-assets-") as assets:
        try:
            for variant in build_variants(args):
                entry = await render_account(variant, args.output_dir, Path(assets), args.keep_html)
                report.append(entry)
                c = entry["completeness"]
                print(f"[{variant.stem}] {variant.width}w/{variant.max_height}h {'|'.join(entry['columns'])} "
                      f"pages={entry['pages']} delivery={entry['delivery']} heights={entry['page_heights']} "
                      f"records={c['records_rendered']}/{c['records_expected']} truncated_or_omitted={c['truncated_or_omitted']} "
                      f"ok={entry['ok']} ({entry['render_seconds']}s)", flush=True)
                for result in entry["results"]:
                    problems = [key for key in ("overflow_x", "clipped", "escapes", "overlaps", "ellipsis_styles",
                                                "broken_images", "external_images") if result.get(key)]
                    problems += [key for key in ("columns_ok", "summary_ok") if not result.get(key, True)]
                    print(f"    {result['file']:40} {result['png_pixels'][0]}x{result['png_pixels'][1]} "
                          f"css {result['css_size'][0]:.0f}x{result['css_size'][1]:.0f} "
                          f"{result['png_bytes'] / 1e6:.2f}MB ok={result['ok']} {problems}", flush=True)
        finally:
            await close_browser()
    summary = {
        "all_ok": all(entry["ok"] for entry in report),
        "rules": {
            "GACHA_CARD_WIDTH": args.width,
            "GACHA_PAGE_MAX_HEIGHT": args.max_height,
            "GACHA_RERUN_SIDE": args.rerun_side,
            "SIDE_COLUMN_SHARE": gacha_draw.SIDE_COLUMN_SHARE,
            "GACHA_PAGE_SAFETY": gacha_draw.GACHA_PAGE_SAFETY,
            "SPLIT_MIN_ROWS": gacha_draw.SPLIT_MIN_ROWS,
            "SPLIT_KEEP_ROWS": gacha_draw.SPLIT_KEEP_ROWS,
            "FORWARD_ABOVE": FORWARD_ABOVE,
        },
        "overview": [
            {"stem": entry["stem"], "account": entry["account"], "width": entry["width"],
             "max_height": entry["max_height"], "rerun_side": entry["rerun_side"], "columns": entry["columns"],
             "column_widths": [column["width"] for column in entry["results"][0].get("columns", [])],
             "pages": entry["pages"], "page_heights": entry["page_heights"],
             "png_pixels": [result["png_pixels"] for result in entry["results"]],
             "png_bytes_total": entry["png_bytes_total"], "ok": entry["ok"]}
            for entry in report
        ],
        "accounts": report,
    }
    (args.output_dir / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("all_ok =", summary["all_ok"])


if __name__ == "__main__":
    asyncio.run(main())
