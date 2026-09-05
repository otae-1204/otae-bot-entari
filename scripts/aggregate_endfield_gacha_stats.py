#!/usr/bin/env python3
"""聚合已授权用户的终末地寻访实测数据，并与官方概率公示对比。

数据来源：本地库 `data/endfield/endfield.db` 里所有绑定角色的 `gacha_records`
与小黑盒导入（`xhh_gacha_*`）。这些都来自主动授权/主动导入的用户，不涉及任何
陌生人账号。输出为**匿名聚合**：只有计数与比率，不含 role_id / QQ / 昵称。

实测指标（按角色池/武器池分别，跨全部用户合并）：
  - 六星综合出率 = 六星数 / 付费抽数（含 95% 置信区间）
  - 五星出率、稀有度分布
  - 平均保底（相邻六星之间的付费抽数，只计非左删失区间）与分布
  - 歪率 miss_up（限定角色池，来源：小黑盒导入，因官方记录不含 UP 归属）

对比基线来自官方概率公示与 gacha.py 的综合概率常量。付费抽的实测出率应当对齐
「综合概率」（已含保底），而非 0.8% 基础概率——脚本会同时给出两者以免误读。

保底/池族判定逻辑对齐 plugins/endfield/gacha/service.py，以其为准。
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import statistics
import sys
import time
import types
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "endfield"
sys.path.insert(0, str(ROOT))

# 仅加载 account_store（只依赖 account_crypto），避免拉起 service / http 层。
_PACKAGE = "endfield_stats_runtime"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(PLUGIN_DIR)]
sys.modules[_PACKAGE] = _package
account_store = importlib.import_module(f"{_PACKAGE}.account.store")

EndfieldStore = account_store.EndfieldStore
EndfieldRole = account_store.EndfieldRole
GachaRecord = account_store.GachaRecord

# --- 官方概率公示 + gacha.py 综合概率常量（对比基线，非实测） ---
# 角色：6★ 综合 0.8%（其中 UP 0.4%，即六星里 50% 为 UP），80 抽硬保底，120 抽大保底必出 UP。
# 武器：6★ 综合 4%（UP 1%），80 抽硬保底。
# comp_six 为「含保底的综合概率」区间，取自 gacha.py SIX_STAR_COMPREHENSIVE_RATES。
BASELINE = {
    "角色": {
        "base_six": 0.008, "base_up": 0.004, "up_share": 0.50,
        "comp_six": (0.020387, 0.022720), "hard_pity": 80, "big_pity": 120,
    },
    "武器": {
        "base_six": 0.040, "base_up": 0.010, "up_share": 0.25,
        "comp_six": (0.053546, 0.062212), "hard_pity": 80, "big_pity": None,
    },
}


# --- 以下四个纯函数对齐 gacha.py，保持算法一致（gacha.py 为准） ---
def _seq_sort(value: str) -> tuple[int, str]:
    text = str(value or "")
    return (0, text.zfill(24)) if text.isdigit() else (1, text)


def _record_sort_key(record: GachaRecord) -> tuple[int, tuple[int, str]]:
    return (record.gacha_ts, _seq_sort(record.seq_id))


def _is_joint_character_pool(record: GachaRecord) -> bool:
    return "joint" in f"{record.pool_type} {record.pool_id}".casefold()


def _is_beginner_character_pool(record: GachaRecord) -> bool:
    return "beginner" in f"{record.pool_type} {record.pool_id}".casefold()


def _is_standard_character_pool(record: GachaRecord) -> bool:
    identity = f"{record.pool_type} {record.pool_id}".casefold()
    return "standard" in identity or record.pool_name.strip() == "基础寻访"


def _character_pity_family(record: GachaRecord) -> str:
    if _is_joint_character_pool(record):
        return f"joint:{record.pool_id.casefold()}"
    if _is_beginner_character_pool(record):
        return f"beginner:{record.pool_id.casefold()}"
    if _is_standard_character_pool(record):
        return "standard"
    return "special"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "endfield" / "endfield.db", help="账号数据库路径")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output" / "endfield_gacha_stats" / "aggregate.json",
        help="JSON 输出路径",
    )
    return parser.parse_args()


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """六星出率的 95% Wilson 置信区间，小样本也不会给出越界的区间。"""
    if trials <= 0:
        return (0.0, 0.0)
    phat = successes / trials
    denom = 1 + z * z / trials
    center = (phat + z * z / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials))
    return (max(0.0, center - margin), min(1.0, center + margin))


class TypeAccumulator:
    """单个 item_type（角色/武器）跨用户的实测累加器。"""

    def __init__(self) -> None:
        self.paid_pulls = 0
        self.free_pulls = 0
        self.six_paid = 0
        self.rarity = Counter()
        self.intervals: list[int] = []          # 非左删失的相邻六星区间
        self.first_six_seen = 0                  # 左删失（本机记录不完整可能偏大），单列
        self.roles_with_data = 0
        # miss_up 仅来自小黑盒（官方记录不含 UP 归属）
        self.limited_up_six = 0
        self.limited_miss_up = 0

    def add_role_character(self, paid: list[GachaRecord]) -> None:
        if not paid:
            return
        self.roles_with_data += 1
        self.paid_pulls += len(paid)
        for record in paid:
            self.rarity[record.rarity] += 1
        # 角色保底按 pity_family 共享/隔离，逐族统计相邻六星区间。
        since: dict[str, int] = defaultdict(int)
        seen_family: set[str] = set()
        for record in sorted(paid, key=_record_sort_key):
            family = _character_pity_family(record)
            since[family] += 1
            if record.rarity >= 6:
                self.six_paid += 1
                if family in seen_family:
                    self.intervals.append(since[family])
                else:
                    self.first_six_seen += 1
                since[family] = 0
                seen_family.add(family)

    def add_role_weapon(self, paid: list[GachaRecord]) -> None:
        if not paid:
            return
        self.roles_with_data += 1
        self.paid_pulls += len(paid)
        for record in paid:
            self.rarity[record.rarity] += 1
        since = 0
        seen = False
        for record in sorted(paid, key=_record_sort_key):
            since += 1
            if record.rarity >= 6:
                self.six_paid += 1
                if seen:
                    self.intervals.append(since)
                else:
                    self.first_six_seen += 1
                since = 0
                seen = True

    def summary(self, item_type: str) -> dict[str, Any]:
        baseline = BASELINE[item_type]
        rate = self.six_paid / self.paid_pulls if self.paid_pulls else None
        lo, hi = _wilson_interval(self.six_paid, self.paid_pulls)
        five = self.rarity.get(5, 0)
        result: dict[str, Any] = {
            "item_type": item_type,
            "roles_with_data": self.roles_with_data,
            "paid_pulls": self.paid_pulls,
            "free_pulls": self.free_pulls,
            "six_star_count": self.six_paid,
            "six_star_rate": rate,
            "six_star_rate_ci95": [lo, hi] if self.paid_pulls else None,
            "five_star_rate": (five / self.paid_pulls) if self.paid_pulls else None,
            "rarity_distribution": dict(sorted(self.rarity.items(), reverse=True)),
            "pity": self._pity_summary(),
            "miss_up": self._miss_up_summary(),
            "baseline": {
                "base_six_rate": baseline["base_six"],
                "up_base_rate": baseline["base_up"],
                "comprehensive_six_rate": list(baseline["comp_six"]),
                "hard_pity": baseline["hard_pity"],
                "big_pity": baseline["big_pity"],
                "expected_avg_pity": [
                    round(1 / baseline["comp_six"][1], 2),
                    round(1 / baseline["comp_six"][0], 2),
                ],
            },
            "comparison": self._comparison(item_type, rate),
        }
        return result

    def _pity_summary(self) -> dict[str, Any]:
        if not self.intervals:
            return {"complete_samples": 0, "left_censored_first_six": self.first_six_seen}
        buckets = {"1-9": 0, "10-29": 0, "30-49": 0, "50-64": 0, "65-79": 0, "80": 0, "81+": 0}
        for value in self.intervals:
            if value >= 81:
                buckets["81+"] += 1
            elif value == 80:
                buckets["80"] += 1
            elif value >= 65:
                buckets["65-79"] += 1
            elif value >= 50:
                buckets["50-64"] += 1
            elif value >= 30:
                buckets["30-49"] += 1
            elif value >= 10:
                buckets["10-29"] += 1
            else:
                buckets["1-9"] += 1
        return {
            "complete_samples": len(self.intervals),
            "left_censored_first_six": self.first_six_seen,
            "average": round(statistics.fmean(self.intervals), 2),
            "median": statistics.median(self.intervals),
            "min": min(self.intervals),
            "max": max(self.intervals),
            "distribution": buckets,
        }

    def _miss_up_summary(self) -> dict[str, Any]:
        if not self.limited_up_six:
            return {"source": "xhh", "samples": 0, "note": "无小黑盒限定池六星样本"}
        return {
            "source": "xhh",
            "samples": self.limited_up_six,
            "miss_up_count": self.limited_miss_up,
            "miss_up_rate": self.limited_miss_up / self.limited_up_six,
            "up_hit_rate": 1 - self.limited_miss_up / self.limited_up_six,
            "baseline_up_share": BASELINE["角色"]["up_share"],
        }

    def _comparison(self, item_type: str, rate: float | None) -> dict[str, Any]:
        baseline = BASELINE[item_type]
        comp_lo, comp_hi = baseline["comp_six"]
        note = []
        if rate is None:
            return {"note": "无付费抽样本"}
        if rate < comp_lo:
            band = "低于综合概率区间"
        elif rate > comp_hi:
            band = "高于综合概率区间"
        else:
            band = "落在综合概率区间内"
        note.append(f"实测付费六星率 {rate * 100:.3f}% 应对齐综合概率 {comp_lo * 100:.3f}–{comp_hi * 100:.3f}%（{band}）")
        note.append(f"仅供参考：0.8%/4% 是基础概率，不含保底，勿直接与实测对比")
        avg = statistics.fmean(self.intervals) if self.intervals else None
        if avg is not None:
            exp_lo, exp_hi = 1 / comp_hi, 1 / comp_lo
            note.append(f"实测平均保底 {avg:.2f} 抽 vs 期望 {exp_lo:.1f}–{exp_hi:.1f} 抽")
        return {"six_rate_band": band, "notes": note}


def collect(store: EndfieldStore) -> dict[str, Any]:
    rows = store.conn.execute("SELECT DISTINCT role_id, server_id FROM gacha_records").fetchall()
    roles = [(str(r["role_id"]), str(r["server_id"])) for r in rows]
    accumulators = {"角色": TypeAccumulator(), "武器": TypeAccumulator()}

    def _role(role_id: str, server_id: str) -> EndfieldRole:
        return EndfieldRole(0, 0, "", "", role_id, server_id, "", "", False)

    for role_id, server_id in roles:
        role = _role(role_id, server_id)
        records = [r for r in store.list_gacha_records(role, limit=1_000_000) if r.item_id]
        by_type: dict[str, list[GachaRecord]] = defaultdict(list)
        for record in records:
            by_type[record.item_type].append(record)
        for item_type, items in by_type.items():
            accumulator = accumulators.get(item_type)
            if accumulator is None:
                continue
            paid = [item for item in items if not item.is_free]
            free = [item for item in items if item.is_free]
            accumulator.free_pulls += len(free)
            if item_type == "角色":
                accumulator.add_role_character(paid)
            else:
                accumulator.add_role_weapon(paid)

    # 歪率：官方 gacha_records 无 UP 归属，改用小黑盒导入的 miss_up 字段。
    xhh_roles = 0
    for role_id, server_id in _distinct_xhh_roles(store):
        imported = store.get_xhh_gacha_import(_role(role_id, server_id))
        if imported is None:
            continue
        xhh_roles += 1
        pool_kind = {pool.pool_id: (pool.item_type, pool.pool_type) for pool in imported.pools}
        char_acc = accumulators["角色"]
        for six in imported.six_stars:
            if six.is_free:
                continue
            item_type, pool_type = pool_kind.get(six.pool_id, ("角色", ""))
            identity = f"{pool_type} {six.pool_id}".casefold()
            if item_type != "角色" or "joint" in identity or "beginner" in identity:
                continue
            char_acc.limited_up_six += 1
            char_acc.limited_miss_up += 1 if six.miss_up else 0

    return {
        "generated_at": int(time.time()),
        "data_source": "consented users in endfield.db (gacha_records + xhh imports)",
        "sample": {
            "roles_with_gacha_records": len(roles),
            "roles_with_xhh_import": xhh_roles,
        },
        "note": "匿名聚合，仅含计数与比率；样本量小时结果仅供参考。",
        "by_item_type": {
            item_type: accumulator.summary(item_type)
            for item_type, accumulator in accumulators.items()
        },
    }


def _distinct_xhh_roles(store: EndfieldStore) -> list[tuple[str, str]]:
    try:
        rows = store.conn.execute("SELECT DISTINCT role_id, server_id FROM xhh_gacha_imports").fetchall()
    except Exception:
        return []
    return [(str(r["role_id"]), str(r["server_id"])) for r in rows]


def _fmt_rate(value: float | None) -> str:
    return f"{value * 100:.3f}%" if value is not None else "—"


def print_report(report: dict[str, Any]) -> None:
    sample = report["sample"]
    print(f"样本：{sample['roles_with_gacha_records']} 个角色有寻访记录，{sample['roles_with_xhh_import']} 个含小黑盒导入")
    for item_type, data in report["by_item_type"].items():
        print(f"\n【{item_type}池】付费 {data['paid_pulls']} 抽 / 六星 {data['six_star_count']} 个")
        if data["paid_pulls"] == 0:
            print("  无付费抽样本")
            continue
        ci = data["six_star_rate_ci95"]
        base = data["baseline"]
        print(f"  实测六星率：{_fmt_rate(data['six_star_rate'])}"
              f"（95%CI {_fmt_rate(ci[0])}–{_fmt_rate(ci[1])}）")
        print(f"  公示基线：基础 {_fmt_rate(base['base_six_rate'])}（不含保底）｜"
              f"综合 {_fmt_rate(base['comprehensive_six_rate'][0])}–{_fmt_rate(base['comprehensive_six_rate'][1])}")
        pity = data["pity"]
        if pity["complete_samples"]:
            print(f"  实测平均保底：{pity['average']} 抽（中位 {pity['median']}，n={pity['complete_samples']}，"
                  f"范围 {pity['min']}–{pity['max']}）｜期望 {base['expected_avg_pity'][0]}–{base['expected_avg_pity'][1]} 抽")
        miss = data["miss_up"]
        if miss.get("samples"):
            print(f"  歪率(限定池,小黑盒)：{_fmt_rate(miss['miss_up_rate'])}"
                  f"（UP 命中 {_fmt_rate(miss['up_hit_rate'])}，n={miss['samples']}，公示 UP 占比 50%）")


def main() -> None:
    args = parse_args()
    if not args.db.exists():
        raise SystemExit(f"数据库不存在：{args.db}")
    store = EndfieldStore(args.db)
    try:
        report = collect(store)
    finally:
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(report)
    print(f"\nJSON 已写入 {args.output}")


if __name__ == "__main__":
    main()
