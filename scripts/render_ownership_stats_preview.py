"""渲染持有率统计页设计预览:真实 AKEData 目录 + 合成样本分布。"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.endfield.ownership_stats import (  # noqa: E402
    CollectionSummary,
    OperatorOwnership,
    OwnershipRefreshResult,
    OwnershipStatsReport,
    OwnershipStatsSegment,
    PotentialBucket,
    fetch_operator_catalog,
)
from plugins.endfield.ownership_stats_draw import draw_ownership_stats  # noqa: E402


OUT_DIR = ROOT / "preview_out" / "ownership_stats"
GENERATED_AT = int(time.mktime((2026, 8, 28, 12, 0, 0, 0, 0, -1)))
BUCKET_ORDER = ("unowned", *[f"potential_{level}" for level in range(6)], "unknown")
# (未持有, 潜能0..5, 未知) 的典型分布权重。
POTENTIAL_WEIGHTS = (0.0, 0.34, 0.22, 0.18, 0.12, 0.08, 0.05, 0.01)

FALLBACK_ROSTER = (
    ("chr_0002_endminm", "管理员(男)", 6, "先锋"),
    ("chr_0003_endminf", "管理员(女)", 6, "先锋"),
    ("chr_0001_perlica", "佩丽卡", 6, "突击"),
    ("chr_0004_chenqianyu", "陈千语", 6, "突击"),
    ("chr_0005_laevatain", "莱万汀", 6, "近卫"),
    ("chr_0006_last_rite", "别礼", 6, "近卫"),
    ("chr_0007_arcane", "诀", 6, "术师"),
    ("chr_0008_arclight", "弧光", 5, "术师"),
    ("chr_0009_xaihi", "赛希", 5, "辅助"),
    ("chr_0010_ember", "余烬", 5, "术师"),
    ("chr_0011_gilberta", "洁尔佩塔", 5, "辅助"),
    ("chr_0012_yvonne", "伊冯", 5, "突击"),
    ("chr_0013_ardelia", "艾尔黛拉", 5, "术师"),
    ("chr_0014_tangtang", "汤汤", 5, "先锋"),
    ("chr_0015_rossi", "洛茜", 5, "近卫"),
    ("chr_0016_pogranichnik", "骏卫", 5, "重装"),
    ("chr_0017_zhuangfangyi", "庄方宜", 5, "术师"),
    ("chr_0018_mifu", "弭弗", 5, "重装"),
    ("chr_0019_wulfgard", "狼卫", 5, "近卫"),
    ("chr_0020_lifeng", "黎风", 5, "突击"),
    ("chr_0021_camille", "卡缪", 5, "先锋"),
)

# 顶尖干员的持有率基线;管理员(男)是开局必得,其余递减。
OWNERSHIP_BASELINE = {
    "chr_0002_endminm": 1.0,
    "chr_0003_endminf": 1.0,
    "chr_0001_perlica": 0.9,
    "chr_0004_chenqianyu": 0.82,
    "chr_0005_laevatain": 0.74,
}


async def _roster() -> tuple[tuple[str, str, int, str], ...]:
    try:
        _, entries = await fetch_operator_catalog()
        playable = [
            (entry.source_id, entry.name, entry.rarity, entry.profession)
            for entry in entries
            if entry.source_id
        ][:23]
        if len(playable) >= 6:
            # 观测补录干员:无 source_id、职业兜底,验证首字头像与灰色描边。
            playable.append(("", "未知干员", 5, "未知职业"))
            return tuple(playable)
    except Exception as exc:  # noqa: BLE001 - 预览脚本允许离线兜底
        print(f"[preview] AKEData catalog unavailable ({exc}); using fallback roster")
    return (*FALLBACK_ROSTER, ("", "未知干员", 5, "未知职业"))


def _baselines(roster: tuple[tuple[str, str, int, str], ...]) -> dict[str, float]:
    """按稀有度分组内平滑衰减的持有率基线,模拟真实分布而非制造平局长尾。"""
    tiers: dict[int, list[int]] = {}
    for index, (_, _, rarity, _) in enumerate(roster):
        tiers.setdefault(rarity, []).append(index)
    result: dict[str, float] = {}
    for rarity, indices in tiers.items():
        count = len(indices)
        for position, index in enumerate(indices):
            source_id = roster[index][0]
            if source_id in OWNERSHIP_BASELINE:
                result[source_id] = OWNERSHIP_BASELINE[source_id]
                continue
            decay = position / max(1, count - 1)
            jitter = ((index * 37) % 9 - 4) * 0.013
            result[source_id] = max(0.05, min(0.92, 0.62 - decay * 0.58 + jitter))
    return result


def _synthesize(
    roster: tuple[tuple[str, str, int, str], ...],
    sample_count: int,
    rng: random.Random,
    baselines: dict[str, float] | None = None,
) -> OwnershipStatsSegment:
    baselines = baselines or _baselines(roster)
    operators: list[OperatorOwnership] = []
    for index, (source_id, name, rarity, profession) in enumerate(roster):
        baseline = baselines.get(source_id, 0.08)
        owned = round(sample_count * baseline)
        buckets = dict.fromkeys(BUCKET_ORDER, 0)
        buckets["unowned"] = sample_count - owned
        for _ in range(owned):
            level = rng.choices(
                [f"potential_{i}" for i in range(6)] + ["unknown"],
                weights=POTENTIAL_WEIGHTS[1:],
            )[0]
            buckets[level] += 1
        operators.append(
            OperatorOwnership(
                operator_key=f"preview-{source_id}",
                source_id=source_id,
                name=name,
                rarity=rarity,
                profession=profession,
                sort_order=index,
                owned_count=owned,
                sample_count=sample_count,
                ownership_rate=owned / sample_count if sample_count else None,
                potential_buckets=tuple(
                    PotentialBucket(key, key, buckets[key], buckets[key] / sample_count if sample_count else None)
                    for key in BUCKET_ORDER
                ),
            )
        )
    operators.sort(key=lambda item: (-item.rarity, -item.owned_count, item.sort_order))
    professions = _summarize("profession", operators, sample_count)
    rarities = _summarize("rarity", operators, sample_count)
    return OwnershipStatsSegment(
        region="all",
        eligible_sample_count=sample_count + 4,
        valid_sample_count=sample_count,
        excluded_sample_count=4,
        operators=tuple(operators),
        professions=professions,
        rarities=rarities,
    )


def _summarize(kind: str, operators: list[OperatorOwnership], sample_count: int) -> tuple[CollectionSummary, ...]:
    groups: dict[str, list[OperatorOwnership]] = {}
    for operator in operators:
        groups.setdefault(operator.profession if kind == "profession" else str(operator.rarity), []).append(operator)
    return tuple(
        CollectionSummary(
            kind=kind,
            label=label,
            operator_count=len(items),
            owned_slots=sum(item.owned_count for item in items),
            possible_slots=sample_count * len(items),
            collection_rate=(
                sum(item.owned_count for item in items) / (sample_count * len(items))
                if sample_count and items
                else None
            ),
        )
        for label, items in groups.items()
    )


def _relabel(segment: OwnershipStatsSegment, region: str) -> OwnershipStatsSegment:
    return OwnershipStatsSegment(
        region=region,
        eligible_sample_count=segment.eligible_sample_count,
        valid_sample_count=segment.valid_sample_count,
        excluded_sample_count=segment.excluded_sample_count,
        operators=segment.operators,
        professions=segment.professions,
        rarities=segment.rarities,
    )


def _scale(segment: OwnershipStatsSegment, sample_count: int, rng: random.Random) -> OwnershipStatsSegment:
    operators: list[OperatorOwnership] = []
    for operator in segment.operators:
        owned = round(operator.ownership_rate * sample_count) if operator.ownership_rate is not None else 0
        buckets = dict.fromkeys(BUCKET_ORDER, 0)
        buckets["unowned"] = sample_count - owned
        for _ in range(owned):
            level = rng.choices(
                [f"potential_{i}" for i in range(6)] + ["unknown"],
                weights=POTENTIAL_WEIGHTS[1:],
            )[0]
            buckets[level] += 1
        operators.append(
            OperatorOwnership(
                operator_key=operator.operator_key,
                source_id=operator.source_id,
                name=operator.name,
                rarity=operator.rarity,
                profession=operator.profession,
                sort_order=operator.sort_order,
                owned_count=owned,
                sample_count=sample_count,
                ownership_rate=owned / sample_count if sample_count else None,
                potential_buckets=tuple(
                    PotentialBucket(key, key, buckets[key], buckets[key] / sample_count if sample_count else None)
                    for key in BUCKET_ORDER
                ),
            )
        )
    return OwnershipStatsSegment(
        region=segment.region,
        eligible_sample_count=segment.eligible_sample_count,
        valid_sample_count=sample_count,
        excluded_sample_count=max(0, segment.eligible_sample_count - sample_count),
        operators=tuple(operators),
        professions=_summarize("profession", operators, sample_count),
        rarities=_summarize("rarity", operators, sample_count),
    )


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    roster = await _roster()
    rng = random.Random(20260828)

    all_segment = _synthesize(roster, 36, rng)
    cn_segment = _scale(all_segment, 33, rng)
    asia_segment = _scale(all_segment, 3, rng)

    global_report = OwnershipStatsReport(
        scope="global",
        generated_at=GENERATED_AT,
        catalog_version="v20260825",
        snapshot_updated_at=GENERATED_AT - 5 * 3600,
        segments=(
            _relabel(all_segment, "all"),
            _relabel(cn_segment, "cn"),
            _relabel(asia_segment, "asia"),
        ),
        refresh=None,
    )

    group_all = _scale(all_segment, 10, rng)
    group_cn = _scale(all_segment, 10, rng)
    empty_asia = OwnershipStatsSegment(
        region="asia",
        eligible_sample_count=0,
        valid_sample_count=0,
        excluded_sample_count=0,
        operators=(),
        professions=(),
        rarities=(),
    )
    group_report = OwnershipStatsReport(
        scope="group",
        generated_at=GENERATED_AT,
        catalog_version="v20260825",
        snapshot_updated_at=GENERATED_AT - 2 * 3600,
        segments=(
            # 亚服无样本时总计段与国服同样本集,渲染层会去重只展示国服。
            _relabel(group_all, "all"),
            _relabel(group_cn, "cn"),
            empty_asia,
        ),
        refresh=OwnershipRefreshResult(
            attempted=15,
            succeeded=12,
            failed=2,
            skipped=1,
            catalog_updated=True,
            started_at=GENERATED_AT - 95,
            finished_at=GENERATED_AT - 37,
        ),
    )

    for name, report in (
        ("preview_global.png", global_report),
        ("preview_group.png", group_report),
    ):
        pages = await draw_ownership_stats(report)
        for index, page in enumerate(pages):
            path = OUT_DIR / (name if len(pages) == 1 else f"{name}.page{index + 1}.png")
            path.write_bytes(page)
            print(f"[preview] wrote {path} ({len(page):,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
