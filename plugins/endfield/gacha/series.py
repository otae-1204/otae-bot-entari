"""系列累计：重构寻访 / 重构申领跨期继承的加急招募、信物与申领赠礼。

系列（series）= 同一 UP 的全部开放；开放（run）= ``(pool_id, pool_version)``。
两种复刻方式（同 poolId 再开放 / 新 poolId）在这里走同一条路径：先按 ``series_key`` 归组，
按开放先后排序，再把系列累计位置映射回具体某一期的某一抽。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from ..account.store import GachaRecord
from .assets import GachaItemMetadata, GachaPoolBanner, GachaPoolRule
from .models import FreePullBatch, KeepsakeGift, NextReward, PoolAnalysis, RunRef, SeriesState
from .pools import (
    KINDS_BY_KEY,
    PoolKind,
    series_base_name,
    series_key as build_series_key,
    upcoming_weapon_rewards,
    weapon_reward_claims,
)


WEAPON_BOX_GIFT_NAME = "自选武库箱"
REWARD_LABELS = {
    "rush": "加急招募",
    "keepsake": "信物",
    "weapon_box": "武库赠礼",
}


def reward_label(kind: PoolKind, reward: str) -> str:
    if reward == "up_weapon":
        return "点绘赠礼" if kind.key == "weapon_rerun" else "UP武器"
    return REWARD_LABELS.get(reward, reward)


def keepsake_identity(
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    keepsake_metadata: dict[str, GachaItemMetadata],
) -> tuple[str, str, str]:
    """信物的 (name, item_id, icon_path)。"""
    operator_id = rule.up_item_ids[0] if rule and rule.up_item_ids else ""
    operator = metadata.get(operator_id) if operator_id else None
    gift = keepsake_metadata.get(operator_id) if operator_id else None
    name = (
        gift.name if gift and gift.name
        else f"{operator.name}的信物" if operator and operator.name
        else "当期UP干员的信物"
    )
    item_id = gift.item_id if gift else (f"item_charpotentialup_{operator_id}" if operator_id else "")
    return name, item_id, gift.icon_path if gift else ""


def weapon_gift_identity(
    reward: str,
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    pool_banners: Iterable[GachaPoolBanner] = (),
) -> tuple[str, str, str]:
    """申领赠礼的 (name, item_id, icon_path)：UP 武器或自选武库箱。"""
    if reward == "weapon_box":
        return WEAPON_BOX_GIFT_NAME, "", ""
    operator_id = rule.up_item_ids[0] if rule and rule.up_item_ids else ""
    operator = metadata.get(operator_id) if operator_id else None
    if operator is None:
        banner = next((item for item in pool_banners if item.item_type == "武器" and item.name), None)
        if banner is not None:
            return banner.name, banner.item_id, banner.image_path
    name = operator.name if operator and operator.name else "当期UP武器"
    item_id = operator.item_id if operator else operator_id
    return name, item_id, operator.icon_path if operator else ""


def gift_type_label(reward: str) -> str:
    return {"keepsake": "信物", "up_weapon": "武器", "weapon_box": "武库箱"}.get(reward, reward)


def series_next_rewards(kind: PoolKind, total: int, claims: int) -> tuple[NextReward, ...]:
    """下一次累计奖励：重构寻访 → 加急招募 + 信物；武器 → 下一个武库箱 / UP 武器。"""
    result: list[NextReward] = []
    if kind.item_type == "角色":
        next_rush = next((threshold for threshold in kind.rush_thresholds if total < threshold), 0)
        if next_rush:
            result.append(NextReward("rush", reward_label(kind, "rush"), next_rush, next_rush - total, "抽"))
        if kind.keepsake_cycle:
            next_keepsake = (total // kind.keepsake_cycle + 1) * kind.keepsake_cycle
            result.append(
                NextReward("keepsake", reward_label(kind, "keepsake"), next_keepsake, next_keepsake - total, "抽")
            )
        return tuple(result)
    for count, reward in upcoming_weapon_rewards(kind.reward_schedule, claims):
        result.append(NextReward(reward, reward_label(kind, reward), count, count - claims, "次申领"))
    return tuple(result)


def attach_series(
    pools: list[PoolAnalysis],
    pool_rules: dict[str, GachaPoolRule],
    run_records: dict[tuple[str, int, str], list[GachaRecord]],
    metadata: dict[str, GachaItemMetadata],
    keepsake_metadata: dict[str, GachaItemMetadata],
    pool_banners: dict[str, tuple[GachaPoolBanner, ...]] | None = None,
) -> tuple[list[PoolAnalysis], tuple[SeriesState, ...]]:
    """给系列型 kind 的池补齐系列字段、系列累计赠礼与加急招募状态。"""
    pool_banners = pool_banners or {}
    result = list(pools)
    by_series: dict[str, list[int]] = defaultdict(list)
    for index, pool in enumerate(result):
        kind = KINDS_BY_KEY.get(pool.kind_key)
        if kind is None or kind.cumulative_scope != "series":
            continue
        key = build_series_key(kind, pool_rules.get(pool.pool_id), pool.pool_id, pool.name)
        by_series[key].append(index)

    states: list[SeriesState] = []
    for key, indexes in by_series.items():
        runs = sorted(
            indexes,
            key=lambda index: (
                _first_ts(result[index], run_records),
                result[index].pool_version,
                result[index].latest_ts,
            ),
        )
        kind = KINDS_BY_KEY[result[runs[0]].kind_key]
        latest = runs[-1]
        rule = next(
            (pool_rules[result[index].pool_id] for index in reversed(runs)
             if pool_rules.get(result[index].pool_id) and pool_rules[result[index].pool_id].up_item_ids),
            pool_rules.get(result[latest].pool_id),
        )
        name = series_base_name(result[latest].name) or result[latest].name
        offsets: dict[int, int] = {}
        total = 0
        for index in runs:
            offsets[index] = total
            total += result[index].paid_total
        estimated = any(result[index].history_missing_count > 0 for index in runs)
        rush_used = sum(len(result[index].free_batches) for index in runs)
        rush_claimed = sum(1 for threshold in kind.rush_thresholds if total >= threshold)
        rush_next = next((threshold for threshold in kind.rush_thresholds if total < threshold), 0)
        keepsake_progress = total % kind.keepsake_cycle if kind.keepsake_cycle else 0
        keepsake_claims = total // kind.keepsake_cycle if kind.keepsake_cycle else 0
        claims = total // 10 if kind.item_type == "武器" else 0
        next_rewards = series_next_rewards(kind, total, claims)

        gifts_by_run: dict[int, list[KeepsakeGift]] = {index: [] for index in runs}
        if kind.keepsake_cycle:
            gift_name, gift_id, gift_icon = keepsake_identity(rule, metadata, keepsake_metadata)
            for position in range(kind.keepsake_cycle, total + 1, kind.keepsake_cycle):
                index, local, gacha_ts, guessed = _place(result, runs, offsets, run_records, position)
                gifts_by_run[index].append(
                    KeepsakeGift(
                        gift_name, gift_id, gacha_ts, local, gift_icon, gift_type_label("keepsake"),
                        gift_kind="keepsake", series_position=position, estimated=guessed,
                    )
                )
        if kind.reward_schedule is not None and kind.item_type == "武器":
            banners = pool_banners.get(result[latest].pool_id, ())
            for count, reward in weapon_reward_claims(kind.reward_schedule, claims):
                position = count * 10
                gift_name, gift_id, gift_icon = weapon_gift_identity(reward, rule, metadata, banners)
                index, local, gacha_ts, guessed = _place(result, runs, offsets, run_records, position)
                gifts_by_run[index].append(
                    KeepsakeGift(
                        gift_name, gift_id, gacha_ts, local, gift_icon, gift_type_label(reward),
                        count, gift_kind=reward, series_position=position, estimated=guessed,
                    )
                )

        free_thresholds = _assign_rush_thresholds(result, runs, kind)
        refs: list[RunRef] = []
        for ordinal, index in enumerate(runs, 1):
            pool = result[index]
            run_label = f"#{ordinal}"
            is_latest = index == latest
            inherited = offsets[index]
            refs.append(
                RunRef(
                    pool.pool_id, pool.pool_version, pool.card_key, pool.name, run_label,
                    _first_ts(pool, run_records), pool.latest_ts, pool.paid_total,
                    len(pool.free_batches), pool.is_current,
                )
            )
            result[index] = replace(
                pool,
                series_key=key,
                series_name=name,
                series_index=ordinal,
                series_run_count=len(runs),
                series_inherited_to=0 if is_latest else len(runs),
                series_inherited_total=inherited,
                series_total=total,
                series_estimated=estimated,
                run_label=run_label,
                series_claims=(inherited + pool.paid_total) // 10 if kind.item_type == "武器" else 0,
                series_inherited_claims=inherited // 10 if kind.item_type == "武器" else 0,
                keepsake_gifts=tuple(sorted(gifts_by_run[index], key=lambda gift: gift.pool_position, reverse=True)),
                six_stars=tuple(replace(event, run_label=run_label) for event in pool.six_stars),
                free_batches=free_thresholds.get(index, pool.free_batches),
                keepsake_progress=keepsake_progress if is_latest else 0,
                keepsake_claims=keepsake_claims if is_latest else 0,
                rush_thresholds=kind.rush_thresholds if is_latest else (),
                rush_claimed=rush_claimed if is_latest else 0,
                rush_used=rush_used if is_latest else 0,
                rush_next_threshold=rush_next if is_latest else 0,
                rush_next_remaining=(rush_next - total) if (is_latest and rush_next) else 0,
                next_rewards=next_rewards if is_latest else (),
            )
        states.append(
            SeriesState(
                key, kind.key, name, tuple(refs), total, estimated,
                kind.rush_thresholds, rush_claimed, rush_used, rush_next,
                (rush_next - total) if rush_next else 0,
                kind.keepsake_cycle, keepsake_progress, keepsake_claims, claims, next_rewards,
            )
        )
    return result, tuple(states)


def _first_ts(pool: PoolAnalysis, run_records: dict[tuple[str, int, str], list[GachaRecord]]) -> int:
    records = run_records.get((pool.pool_id, pool.pool_version, pool.item_type))
    if records:
        return min(record.gacha_ts for record in records)
    earliest = min(
        (event.gacha_ts for event in pool.six_stars if event.gacha_ts),
        default=0,
    )
    return earliest or pool.latest_ts


def _place(
    pools: list[PoolAnalysis],
    runs: list[int],
    offsets: dict[int, int],
    run_records: dict[tuple[str, int, str], list[GachaRecord]],
    series_position: int,
) -> tuple[int, int, int, bool]:
    """把系列累计第 N 抽映射到 (run index, 本期位置, 时间戳, 是否推算)。"""
    for index in runs:
        pool = pools[index]
        start = offsets[index]
        if start < series_position <= start + pool.paid_total:
            local = series_position - start
            records = run_records.get((pool.pool_id, pool.pool_version, pool.item_type), [])
            if not pool.history_missing_count and local <= len(records):
                return index, local, records[local - 1].gacha_ts, False
            return index, local, pool.latest_ts, True
    last = runs[-1]
    return last, 0, pools[last].latest_ts, True


def _assign_rush_thresholds(
    pools: list[PoolAnalysis],
    runs: list[int],
    kind: PoolKind,
) -> dict[int, tuple[FreePullBatch, ...]]:
    """系列内的免费批次按时间顺序分配 30/60/90 档位（超出档位为 0）。"""
    if not kind.rush_thresholds:
        return {}
    ordered: list[tuple[int, int, int]] = sorted(
        (batch.gacha_ts, index, batch_index)
        for index in runs
        for batch_index, batch in enumerate(pools[index].free_batches)
    )
    thresholds: dict[tuple[int, int], int] = {}
    for ordinal, (_, index, batch_index) in enumerate(ordered):
        thresholds[(index, batch_index)] = (
            kind.rush_thresholds[ordinal] if ordinal < len(kind.rush_thresholds) else 0
        )
    result: dict[int, tuple[FreePullBatch, ...]] = {}
    for index in runs:
        result[index] = tuple(
            replace(batch, source="rush", threshold=thresholds.get((index, batch_index), 0))
            for batch_index, batch in enumerate(pools[index].free_batches)
        )
    return result


__all__ = [
    "attach_series",
    "gift_type_label",
    "keepsake_identity",
    "reward_label",
    "series_next_rewards",
    "weapon_gift_identity",
]
