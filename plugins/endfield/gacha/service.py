from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Awaitable, Callable, Iterable

from loguru import logger

from ..account.client import (
    ACCOUNT_PROVIDER_SKPORT,
    CHARACTER_POOL_TYPES,
    EndfieldAPIError,
    EndfieldOfficialClient,
    decode_account_credential,
)
from ..account.crypto import CredentialCipher
from ..account.store import EndfieldRole, EndfieldStore, GachaRecord, XhhGachaImport, XhhGachaPool, XhhSixStar
from .assets import GachaItemMetadata, GachaPoolBanner, GachaPoolRule, apply_gacha_metadata
from .models import (
    FreePullBatch,
    GachaAnalysis,
    KeepsakeGift,
    KindSummary,
    NextReward,
    PityChainState,
    PoolAnalysis,
    RunRef,
    SeriesState,
    SixStarEvent,
    SixStarExpectation,
)
from .pools import (
    DEFAULT_CHARACTER_POOL_TYPES,
    ENUM_PREFIX,
    EXPECTATION_GROUPS,
    KINDS,
    KINDS_BY_KEY,
    LEGACY_CHARACTER_POOL_TYPES,
    PoolKind,
    chain_label,
    kind_for_record,
    kind_for_xhh_pool,
    kind_from_enum,
    kind_from_stream_key,
    pity_family,
    resolve_character_pool_types,
    resolve_pool_kind,
    show_standard_pools,
    weapon_reward_claims,
)
from .series import (
    attach_series,
    gift_type_label,
    keepsake_identity,
    series_next_rewards,
    weapon_gift_identity,
)


__all__ = [
    "CHARACTER_POOL_TYPES",
    "DEFAULT_CHARACTER_POOL_TYPES",
    "EndfieldGachaService",
    "FreePullBatch",
    "GachaAnalysis",
    "KeepsakeGift",
    "KindSummary",
    "NextReward",
    "PityChainState",
    "PoolAnalysis",
    "ROLE_TASKS",
    "RunRef",
    "SeriesState",
    "SixStarEvent",
    "SixStarExpectation",
    "StreamSyncResult",
    "SyncResult",
    "TaskAlreadyRunning",
    "build_gacha_analysis",
    "calculate_group_expectation",
    "calculate_six_star_expectation",
    "filter_xhh_import_six_stars",
    "format_timestamp",
]

OFFICIAL_GACHA_LOOKBACK_DAYS = 90
XHH_PRIORITY_BOUNDARY_MARGIN_DAYS = 30
SPECIAL_POOL_FREE_TEN_UNLOCK_PULLS = 30
SIX_STAR_COMPREHENSIVE_RATES = {
    "角色": (0.020387, 0.022720),
    "武器": (0.053546, 0.062212),
}
UP_COMPREHENSIVE_RATES = {
    "武器": (0.018533, 0.018678),
}
FREE_SIX_STAR_BASE_RATES = {
    "角色": 0.008,
    "武器": 0.04,
}
FREE_UP_BASE_RATES = {
    "角色": 0.004,
    "武器": 0.01,
}


class TaskAlreadyRunning(RuntimeError):
    pass


class RoleTaskRegistry:
    def __init__(self):
        self._guard = asyncio.Lock()
        self._active: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def claim(self, role: EndfieldRole):
        key = (role.role_id, role.server_id)
        async with self._guard:
            if key in self._active:
                raise TaskAlreadyRunning("当前角色已有任务正在执行中。")
            self._active.add(key)
        try:
            yield
        finally:
            async with self._guard:
                self._active.discard(key)


ROLE_TASKS = RoleTaskRegistry()


@dataclass(frozen=True, slots=True)
class StreamSyncResult:
    stream_key: str
    label: str
    inserted: int
    fetched: int
    complete: bool
    error: str = ""
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    role: EndfieldRole
    streams: tuple[StreamSyncResult, ...]
    full: bool
    synced_at: int

    @property
    def inserted(self) -> int:
        return sum(item.inserted for item in self.streams)

    @property
    def failed(self) -> tuple[StreamSyncResult, ...]:
        return tuple(item for item in self.streams if not item.complete)


def _stream_label(pool_type: str) -> str:
    kind = kind_from_enum(pool_type)
    if not kind.is_unknown:
        return kind.label
    return str(pool_type).rsplit("_", 1)[-1] or str(pool_type)


class EndfieldGachaService:
    def __init__(self, store: EndfieldStore, client: EndfieldOfficialClient, cipher: CredentialCipher):
        self.store = store
        self.client = client
        self.cipher = cipher

    async def sync(
        self,
        role: EndfieldRole,
        *,
        full: bool = False,
        pool_rules: dict[str, GachaPoolRule] | None = None,
    ) -> SyncResult:
        async with ROLE_TASKS.claim(role):
            account_token = self.store.decrypt_token(role, self.cipher)
            provider, _raw_account_token = decode_account_credential(account_token)
            u8_token = await self.client.get_u8_token(account_token, role.binding_uid)
            character_names = await self.client.character_pool_names(u8_token, role.server_id)
            pool_types = resolve_character_pool_types(character_names)
            api_types = {key for key in character_names if str(key).startswith(ENUM_PREFIX)}
            ignored = [key for key in character_names if key not in api_types]
            if ignored:
                logger.debug("[endfield] gacha char pool directory ignored non-enum keys {}", ignored)
            logger.info(
                "[endfield] gacha char pool types api={} synced={}", sorted(api_types), list(pool_types),
            )
            semaphore = asyncio.Semaphore(3)
            jobs: list[Awaitable[StreamSyncResult]] = []
            for pool_type in pool_types:
                label = character_names.get(pool_type) or _stream_label(pool_type)
                # 海外没有 /char/pool 目录，只能靠默认列表补 Rerun；若接口不认该枚举则静默跳过。
                optional = (
                    provider == ACCOUNT_PROVIDER_SKPORT
                    and pool_type not in api_types
                    and pool_type not in LEGACY_CHARACTER_POOL_TYPES
                )
                jobs.append(
                    self._sync_stream(
                        role, f"char:{pool_type}", label, full, semaphore,
                        lambda cursor, pool_type=pool_type, label=label: self.client.character_records(
                            role, u8_token, pool_type, seq_id=cursor, pool_name=label
                        ),
                        optional=optional,
                    )
                )
            if provider == ACCOUNT_PROVIDER_SKPORT:
                weapon_pools = await self.client.weapon_pools(u8_token, role.server_id)
                for pool_id, pool_name in weapon_pools:
                    jobs.append(
                        self._sync_stream(
                            role, f"weapon:{pool_id}", pool_name or pool_id, full, semaphore,
                            lambda cursor, pool_id=pool_id, pool_name=pool_name: self.client.weapon_records(
                                role, u8_token, pool_id, seq_id=cursor, pool_name=pool_name
                            ),
                        )
                    )
            else:
                jobs.append(
                    self._sync_stream(
                        role, "weapon:all", "武器申领", full, semaphore,
                        lambda cursor: self.client.weapon_records(role, u8_token, seq_id=cursor),
                    )
                )
            results = tuple(await asyncio.gather(*jobs))
            return SyncResult(role, results, full, int(time.time()))

    async def _sync_stream(
        self,
        role: EndfieldRole,
        stream_key: str,
        label: str,
        full: bool,
        semaphore: asyncio.Semaphore,
        fetch_page: Callable[[str], Awaitable],
        *,
        optional: bool = False,
    ) -> StreamSyncResult:
        async with semaphore:
            state = self.store.get_sync_state(role, stream_key)
            known_boundary = "" if full else state.newest_seq_id
            cursor = ""
            newest = ""
            fetched = 0
            inserted = 0
            try:
                for _ in range(500):
                    page = await fetch_page(cursor)
                    if not page.records:
                        if page.has_more and page.next_seq_id and page.next_seq_id != cursor:
                            cursor = page.next_seq_id
                            continue
                        break
                    if not newest:
                        newest = page.records[0].seq_id
                    fresh: list[GachaRecord] = []
                    boundary_hit = False
                    for record in page.records:
                        if known_boundary and record.seq_id == known_boundary:
                            boundary_hit = True
                            break
                        fresh.append(record)
                    fetched += len(fresh)
                    inserted += self.store.insert_gacha_records(fresh)
                    cursor = page.next_seq_id
                    if boundary_hit or not page.has_more or not cursor:
                        break
                self.store.save_sync_state(
                    role, stream_key, newest_seq_id=newest or state.newest_seq_id,
                    page_cursor=cursor, error="",
                )
                return StreamSyncResult(stream_key, label, inserted, fetched, True)
            except EndfieldAPIError as exc:
                if optional:
                    logger.warning("[endfield] optional gacha stream {} unsupported: {}", stream_key, exc)
                    self.store.save_sync_state(
                        role, stream_key, newest_seq_id=state.newest_seq_id,
                        page_cursor=cursor or state.page_cursor, error="",
                    )
                    return StreamSyncResult(stream_key, label, inserted, fetched, True, "", skipped=True)
                error = str(exc)[:180]
            except Exception:
                error = "同步过程中发生未知错误"
            self.store.save_sync_state(
                role, stream_key, newest_seq_id=state.newest_seq_id,
                page_cursor=cursor or state.page_cursor, error=error,
            )
            return StreamSyncResult(stream_key, label, inserted, fetched, False, error)

    def analysis(
        self,
        role: EndfieldRole,
        metadata: dict[str, GachaItemMetadata] | None = None,
        pool_rules: dict[str, GachaPoolRule] | None = None,
        xhh_metadata: dict[str, GachaItemMetadata] | None = None,
        keepsake_metadata: dict[str, GachaItemMetadata] | None = None,
        pool_banners: dict[str, tuple[GachaPoolBanner, ...]] | None = None,
    ) -> GachaAnalysis:
        records = self.store.list_gacha_records(role, limit=100000)
        states = self.store.list_sync_states(role)
        pool_totals = self.store.list_gacha_pool_totals(role)
        xhh_import = self.store.get_xhh_gacha_import(role)
        return build_gacha_analysis(
            role, records, states, metadata, pool_rules, pool_totals,
            xhh_import=xhh_import, xhh_metadata=xhh_metadata,
            keepsake_metadata=keepsake_metadata,
            pool_banners=pool_banners,
        )


RunKey = tuple[str, int, str]   # (pool_id, pool_version, item_type)


def build_gacha_analysis(
    role: EndfieldRole,
    records: list[GachaRecord],
    states,
    metadata: dict[str, GachaItemMetadata] | None = None,
    pool_rules: dict[str, GachaPoolRule] | None = None,
    pool_total_overrides: dict[str, int] | None = None,
    *,
    xhh_import: XhhGachaImport | None = None,
    xhh_metadata: dict[str, GachaItemMetadata] | None = None,
    keepsake_metadata: dict[str, GachaItemMetadata] | None = None,
    pool_banners: dict[str, tuple[GachaPoolBanner, ...]] | None = None,
    show_standard: bool | None = None,
) -> GachaAnalysis:
    metadata = metadata or {}
    pool_rules = pool_rules or {}
    pool_total_overrides = pool_total_overrides or {}
    xhh_metadata = xhh_metadata or {}
    keepsake_metadata = keepsake_metadata or {}
    pool_banners = pool_banners or {}
    if show_standard is None:
        show_standard = show_standard_pools()
    if xhh_import is not None:
        xhh_import = filter_xhh_import_six_stars(xhh_import, xhh_metadata)
    records = list(apply_gacha_metadata(records, metadata))
    rarity_counts = dict(sorted(Counter(item.rarity for item in records).items(), reverse=True))

    # ---- kind 与 run 分组：只有系列型 kind 才按 pool_version 拆成多次开放 ----
    kinds: dict[tuple[str, str], PoolKind] = {}
    grouped: dict[RunKey, list[GachaRecord]] = defaultdict(list)
    for record in records:
        kind = kinds.get((record.pool_id, record.item_type))
        if kind is None:
            kind = kind_for_record(record, pool_rules.get(record.pool_id))
            kinds[(record.pool_id, record.item_type)] = kind
        version = record.pool_version if kind.cumulative_scope == "series" else 0
        grouped[(record.pool_id, version, record.item_type)].append(record)

    # ---- 当期池：每个 kind 一个（该 kind 最新记录所在的 run）----
    current_runs: dict[str, RunKey] = {}
    for key, items in grouped.items():
        kind_key = kinds[(key[0], key[2])].key
        current = current_runs.get(kind_key)
        if current is None or _latest_record_key(items) > _latest_record_key(grouped[current]):
            current_runs[kind_key] = key
    latest_runs: dict[tuple[str, str], RunKey] = {}
    for key, items in grouped.items():
        pool_key = (key[0], key[2])
        current = latest_runs.get(pool_key)
        if current is None or _latest_record_key(items) > _latest_record_key(grouped[current]):
            latest_runs[pool_key] = key

    # ---- 角色 80 抽链：按 pity_family 共享（特许一条、重构一条、基础一条、特殊/启程各池一条）----
    role_paid = sorted(
        (item for item in records if item.item_type == "角色" and not item.is_free),
        key=_record_sort_key,
    )
    role_intervals: dict[tuple[str, str], int] = {}
    role_average_keys: set[tuple[str, str]] = set()
    role_since_six: dict[str, int] = defaultdict(int)
    role_since_five: dict[str, int] = defaultdict(int)
    role_has_previous_six: set[str] = set()
    role_last_six: dict[str, tuple[str, int]] = {}
    family_kinds: dict[str, PoolKind] = {}
    for item in role_paid:
        kind = kinds[(item.pool_id, item.item_type)]
        family = pity_family(kind, item.pool_id)
        family_kinds.setdefault(family, kind)
        role_since_six[family] += 1
        role_since_five[family] += 1
        if item.rarity >= 5:
            role_since_five[family] = 0
        if item.rarity >= 6:
            event_key = (item.pool_id, item.seq_id)
            role_intervals[event_key] = role_since_six[family]
            if family in role_has_previous_six:
                role_average_keys.add(event_key)
            role_since_six[family] = 0
            role_has_previous_six.add(family)
            role_last_six[family] = (item.pool_id, item.gacha_ts)

    pools: list[PoolAnalysis] = []
    six_events: list[SixStarEvent] = []
    interval_events: list[tuple[int, int]] = []
    run_records: dict[RunKey, list[GachaRecord]] = {}
    for run_key, items in grouped.items():
        pool_id, pool_version, item_type = run_key
        kind = kinds[(pool_id, item_type)]
        ordered = sorted(items, key=_record_sort_key)
        paid = [item for item in ordered if not item.is_free]
        free = [item for item in ordered if item.is_free]
        run_records[run_key] = paid
        is_current = current_runs.get(kind.key) == run_key
        is_latest_run = latest_runs.get((pool_id, item_type)) == run_key
        since_six = _pulls_since_six(paid)
        pool_rule = pool_rules.get(pool_id)
        family = pity_family(kind, pool_id)
        hard_limit, hard_source = _hard_guarantee(kind, pool_rule)
        up_known = _up_status_known(kind, pool_rule)
        weapon_pity_labels = _weapon_pity_labels(paid, pool_rule, kind) if item_type == "武器" else {}
        pulls_since_six = 0
        has_previous_pool_six = False
        pool_six_events: list[SixStarEvent] = []
        seen_current_up = False
        for pool_position, item in enumerate(paid, 1):
            pulls_since_six += 1
            if item.rarity < 6:
                continue
            interval = (
                role_intervals.get((item.pool_id, item.seq_id), pulls_since_six)
                if item_type == "角色"
                else pulls_since_six
            )
            if (
                item_type == "角色" and (item.pool_id, item.seq_id) in role_average_keys
            ) or (item_type != "角色" and has_previous_pool_six):
                interval_events.append((item.gacha_ts, interval))
            item_metadata = metadata.get(item.item_id)
            up_status = _up_status(item.item_id, pool_rule, kind)
            pity_labels: list[str] = []
            if item_type == "角色":
                if kind.small_pity and interval >= kind.small_pity:
                    pity_labels.append("小保底")
                if kind.fixed_guarantee_position and pool_position == kind.fixed_guarantee_position:
                    pity_labels.append("大保底")
                elif (
                    kind.has_up and up_status == "up" and not seen_current_up
                    and hard_limit and pool_position == hard_limit
                ):
                    pity_labels.append("大保底")
                seen_current_up = seen_current_up or up_status == "up"
            else:
                pity_labels.extend(weapon_pity_labels.get(item.seq_id, ()))
            if up_status == "off":
                pity_labels.append("歪")
            event = SixStarEvent(
                item.item_name,
                item.pool_name,
                item.item_type,
                item.gacha_ts,
                item_id=item.item_id,
                interval=interval,
                icon_path=item_metadata.icon_path if item_metadata else "",
                pool_position=pool_position,
                pity_labels=tuple(pity_labels),
                up_status=up_status,
                soft_pity_hit=bool(
                    item_type == "角色" and kind.soft_pity_start
                    and kind.soft_pity_start <= interval < kind.small_pity
                ),
            )
            pool_six_events.append(event)
            six_events.append(event)
            pulls_since_six = 0
            has_previous_pool_six = True
        free_batches = _build_free_batches(free, metadata, kind=kind, rule=pool_rule)
        keepsake_gifts = (
            _build_keepsake_gifts(paid, pool_rule, metadata, keepsake_metadata, kind=kind)
            if kind.cumulative_scope == "pool" else []
        )
        for batch in free_batches:
            six_events.extend(batch.six_stars)
        pool_scope = kind.cumulative_scope == "pool"
        if item_type == "角色":
            small_progress = role_since_six[family] if is_current else 0
            small_limit = kind.small_pity if is_current else 0
            large_limit = hard_limit if is_current and kind.has_up else 0
            large_known = bool(is_current and up_known and large_limit)
            large_consumed_at, large_up_name = _large_pity_consumption(paid, pool_rule, metadata, kind)
            large_consumed = bool(is_current and large_known and large_consumed_at)
            keepsake_progress = (
                len(paid) % kind.keepsake_cycle if is_current and pool_scope and kind.keepsake_cycle else 0
            )
            keepsake_claims = (
                len(paid) // kind.keepsake_cycle if is_current and pool_scope and kind.keepsake_cycle else 0
            )
            five_star_progress = role_since_five[family] if is_current and kind.five_star_pity else None
        else:
            small_progress = _weapon_ten_batch_progress(paid) if is_current and kind.small_pity else 0
            small_limit = kind.small_pity if is_current else 0
            large_limit = hard_limit if is_current and kind.has_up else 0
            large_known = bool(is_current and up_known and large_limit)
            large_consumed = False
            large_consumed_at = 0
            large_up_name = ""
            keepsake_progress = 0
            keepsake_claims = 0
            five_star_progress = None
        recorded_total = len(items)
        override = int(pool_total_overrides.get(pool_id, 0) or 0) if is_latest_run else 0
        total = max(recorded_total, override)
        weapon_claims = len(paid) // 10 if item_type == "武器" else 0
        next_rewards = (
            series_next_rewards(kind, len(paid), weapon_claims)
            if is_current and pool_scope and (kind.keepsake_cycle or kind.reward_schedule) else ()
        )
        pools.append(
            PoolAnalysis(
                pool_id=pool_id,
                name=items[0].pool_name or pool_id,
                item_type=item_type,
                total=total,
                since_six_star=since_six,
                latest_ts=max(item.gacha_ts for item in items),
                six_stars=tuple(reversed(pool_six_events)),
                is_current=is_current,
                paid_total=len(paid),
                free_pull_count=len(free),
                free_batches=tuple(reversed(free_batches)),
                small_pity_progress=small_progress,
                small_pity_limit=small_limit,
                large_pity_progress=(
                    min(large_consumed_at or len(paid), large_limit) if large_limit else 0
                ),
                large_pity_limit=large_limit,
                large_pity_known=large_known,
                large_pity_consumed=large_consumed,
                large_pity_consumed_at=large_consumed_at if large_consumed else 0,
                large_pity_up_name=large_up_name if large_consumed else "",
                keepsake_progress=keepsake_progress,
                keepsake_claims=keepsake_claims,
                recorded_total=recorded_total,
                history_missing_count=total - recorded_total,
                keepsake_gifts=tuple(reversed(keepsake_gifts)),
                up_item_ids=pool_rule.up_item_ids if pool_rule else (),
                banners=pool_banners.get(pool_id, ()),
                card_key=_card_key(pool_id, pool_version),
                pool_version=pool_version,
                kind_key=kind.key,
                kind_label=kind.label,
                kind_short=kind.short,
                is_unknown_kind=kind.is_unknown,
                hidden_by_default=kind.key == "standard" and not show_standard,
                up_status_known=up_known,
                pity_family=family,
                pity_scope=kind.pity_scope,
                soft_pity_start=kind.soft_pity_start if item_type == "角色" else 0,
                soft_pity_active=bool(
                    is_current and kind.soft_pity_start and small_progress >= kind.soft_pity_start - 1
                ),
                five_star_pity_progress=five_star_progress,
                five_star_pity_limit=kind.five_star_pity if is_current else 0,
                large_pity_source=hard_source if large_limit else "",
                series_key=f"pool:{pool_id}",
                series_name=items[0].pool_name or pool_id,
                weapon_claims=weapon_claims,
                series_total=len(paid),
                series_claims=weapon_claims,
                next_rewards=next_rewards,
            )
        )
    if xhh_import is not None:
        pools = _merge_xhh_pools(
            pools, xhh_import, metadata, xhh_metadata, pool_rules, keepsake_metadata,
            pool_banners, kinds=kinds, show_standard=show_standard,
        )
    pools.sort(key=_pool_analysis_sort_key)
    pools = _ensure_unique_card_keys(pools)
    pools, series_states = attach_series(
        pools, pool_rules, run_records, metadata, keepsake_metadata, pool_banners,
    )
    stream_errors = tuple(
        (state.stream_key, state.last_error) for state in states if state.last_error
    )
    pools = _attach_sync_errors(pools, stream_errors)
    if xhh_import is not None:
        all_six_stars = [
            event
            for pool in pools
            for event in (*pool.six_stars, *(event for batch in pool.free_batches for event in batch.six_stars))
        ]
        six_events = all_six_stars
        interval_events = [
            (event.gacha_ts, event.interval) for event in all_six_stars if event.interval > 0
        ]
        rarity_counts[6] = len(all_six_stars)
    intervals = [interval for _, interval in sorted(interval_events)]
    six_stars = tuple(sorted(six_events, key=lambda item: item.gacha_ts, reverse=True)[:10])
    errors = tuple(state.last_error for state in states if state.last_error)
    history_missing_count = sum(pool.history_missing_count for pool in pools)
    chains = _build_chains(pools, family_kinds, role_since_six, role_since_five, role_last_six)
    summaries = _build_kind_summaries(pools, chains, series_states, stream_errors, show_standard)
    return GachaAnalysis(
        role=role,
        total=sum(pool.total for pool in pools),
        rarity_counts=rarity_counts,
        pools=tuple(pools),
        six_stars=six_stars,
        intervals=tuple(intervals),
        average_interval=(statistics.fmean(intervals) if intervals else None),
        last_sync_at=max(
            max((state.last_sync_at for state in states), default=0),
            xhh_import.imported_at if xhh_import else 0,
        ),
        complete=bool(states or xhh_import) and not errors and (
            xhh_import is not None or not history_missing_count
        ),
        errors=errors,
        paid_total=sum(pool.paid_total for pool in pools),
        free_pull_count=sum(pool.free_pull_count for pool in pools),
        free_ten_count=sum(len(pool.free_batches) for pool in pools),
        recorded_total=len(records),
        history_missing_count=history_missing_count,
        xhh_imported_at=xhh_import.imported_at if xhh_import else 0,
        expectations={group: calculate_group_expectation(pools, group) for group in EXPECTATION_GROUPS},
        kind_summaries=summaries,
        chains=chains,
        series=series_states,
        show_standard_pools=show_standard,
        stream_errors=stream_errors,
    )


def _card_key(pool_id: str, pool_version: int) -> str:
    return f"{pool_id}#{int(pool_version or 0)}"


def _ensure_unique_card_keys(pools: list[PoolAnalysis]) -> list[PoolAnalysis]:
    seen: Counter[str] = Counter(pool.card_key for pool in pools)
    if all(count == 1 for count in seen.values()):
        return pools
    used: set[str] = set()
    result: list[PoolAnalysis] = []
    for pool in pools:
        key = pool.card_key
        if seen[key] > 1 and key in used:
            key = f"{pool.card_key}@{pool.item_type}"
        used.add(key)
        result.append(replace(pool, card_key=key) if key != pool.card_key else pool)
    return result


def _attach_sync_errors(
    pools: list[PoolAnalysis],
    stream_errors: tuple[tuple[str, str], ...],
) -> list[PoolAnalysis]:
    if not stream_errors:
        return pools
    result: list[PoolAnalysis] = []
    for pool in pools:
        error = ""
        for stream_key, message in stream_errors:
            kind, target = kind_from_stream_key(stream_key)
            if kind is not None and not kind.is_unknown and kind.key == pool.kind_key:
                error = message
            elif target == "weapon" and pool.item_type == "武器":
                error = message
            elif target and target != "weapon" and target == pool.pool_id:
                error = message
            if error:
                break
        result.append(replace(pool, sync_error=error) if error else pool)
    return result


def _build_chains(
    pools: list[PoolAnalysis],
    family_kinds: dict[str, PoolKind],
    since_six: dict[str, int],
    since_five: dict[str, int],
    last_six: dict[str, tuple[str, int]],
) -> tuple[PityChainState, ...]:
    """角色 80 抽链状态：有官方逐抽的链用链计数，xhh-only 的链用当期池的进度。"""
    families: dict[str, PoolKind] = dict(family_kinds)
    pools_by_family: dict[str, list[PoolAnalysis]] = defaultdict(list)
    for pool in pools:
        if pool.item_type != "角色" or not pool.pity_family:
            continue
        pools_by_family[pool.pity_family].append(pool)
        families.setdefault(pool.pity_family, KINDS_BY_KEY.get(pool.kind_key, KINDS_BY_KEY["unknown_char"]))
    chains: list[PityChainState] = []
    for family, kind in sorted(families.items(), key=lambda item: (item[1].sort_rank, item[0])):
        members = pools_by_family.get(family, [])
        current = next((pool for pool in members if pool.is_current), None)
        official = family in since_six
        if current is not None:
            progress = current.small_pity_progress
            five = current.five_star_pity_progress
            soft_active = current.soft_pity_active
        else:
            progress = since_six.get(family, 0)
            five = since_five.get(family) if official and kind.five_star_pity else None
            soft_active = bool(kind.soft_pity_start and progress >= kind.soft_pity_start - 1)
        last_pool_id, last_ts = last_six.get(family, ("", 0))
        if not last_ts:
            latest_event = max(
                ((event.gacha_ts, pool.pool_id) for pool in members for event in pool.six_stars),
                default=(0, ""),
            )
            last_ts, last_pool_id = latest_event
        chains.append(
            PityChainState(
                family=family,
                scope=kind.pity_scope,
                label=chain_label(kind),
                progress=progress,
                limit=kind.small_pity,
                soft_pity_start=kind.soft_pity_start,
                soft_pity_active=soft_active,
                five_star_progress=five,
                five_star_limit=kind.five_star_pity,
                last_six_pool_id=last_pool_id,
                last_six_ts=last_ts,
                pool_ids=tuple(dict.fromkeys(pool.pool_id for pool in members)),
            )
        )
    return tuple(chains)


def _build_kind_summaries(
    pools: list[PoolAnalysis],
    chains: tuple[PityChainState, ...],
    series_states: tuple[SeriesState, ...],
    stream_errors: tuple[tuple[str, str], ...],
    show_standard: bool,
) -> tuple[KindSummary, ...]:
    chains_by_family = {chain.family: chain for chain in chains}
    series_by_key = {state.series_key: state for state in series_states}
    grouped: dict[str, list[PoolAnalysis]] = defaultdict(list)
    for pool in pools:
        grouped[pool.kind_key or _analysis_pool_kind(pool).key].append(pool)
    summaries: list[KindSummary] = []
    for kind in KINDS:
        members = grouped.get(kind.key)
        if not members:
            continue
        current = next((pool for pool in members if pool.is_current), None)
        chain = None
        if current is not None and kind.pity_scope.startswith("shared:"):
            chain = chains_by_family.get(current.pity_family)
        elif kind.pity_scope.startswith("shared:") and members:
            chain = chains_by_family.get(members[0].pity_family)
        series = series_by_key.get(current.series_key) if current is not None else None
        if series is None and kind.cumulative_scope == "series" and members:
            series = series_by_key.get(members[0].series_key)
        sync_error = next((pool.sync_error for pool in members if pool.sync_error), "")
        summaries.append(
            KindSummary(
                key=kind.key,
                label=kind.label,
                short=kind.short,
                item_type=kind.item_type,
                pool_count=len(members),
                paid_total=sum(pool.paid_total for pool in members),
                free_pull_count=sum(pool.free_pull_count for pool in members),
                six_star_count=sum(len(pool.six_stars) for pool in members),
                free_six_star_count=sum(
                    len(batch.six_stars) for pool in members for batch in pool.free_batches
                ),
                current_pool_id=current.pool_id if current else "",
                current_card_key=current.card_key if current else "",
                chain=chain,
                series=series,
                expectation_group=kind.expectation_group,
                sync_error=sync_error,
                is_unknown=kind.is_unknown,
                hidden_by_default=kind.key == "standard" and not show_standard,
            )
        )
    return tuple(summaries)


def _hard_guarantee(kind: PoolKind, rule: GachaPoolRule | None) -> tuple[int, str]:
    """大保底抽数与来源：FZ hardGuarantee 优先，否则注册表默认（特许/重构 120、武器 80）。"""
    if not kind.has_up:
        return 0, ""
    if rule is not None and rule.hard_guarantee:
        return int(rule.hard_guarantee), "fz"
    if kind.hard_guarantee_default:
        return kind.hard_guarantee_default, "default"
    return 0, ""


def _up_status_known(kind: PoolKind, rule: GachaPoolRule | None) -> bool:
    return bool(kind.has_up and rule is not None and rule.up_item_ids)


def _up_status(item_id: str, rule: GachaPoolRule | None, kind: PoolKind) -> str:
    if not (kind.has_up and rule is not None and rule.up_item_ids and item_id):
        return ""
    return "up" if item_id in rule.up_item_ids else "off"


def _analysis_pool_kind(pool: PoolAnalysis) -> PoolKind:
    """PoolAnalysis 级别的 kind：优先 kind_key，否则（前端夹具 / 旧调用）按 id、名称启发式。"""
    kind = KINDS_BY_KEY.get(pool.kind_key)
    if kind is not None:
        return kind
    return resolve_pool_kind(pool_id=pool.pool_id, pool_type="", pool_name=pool.name, item_type=pool.item_type)


def _merge_xhh_pools(
    pools: list[PoolAnalysis],
    imported: XhhGachaImport,
    metadata: dict[str, GachaItemMetadata],
    xhh_metadata: dict[str, GachaItemMetadata],
    pool_rules: dict[str, GachaPoolRule],
    keepsake_metadata: dict[str, GachaItemMetadata],
    pool_banners: dict[str, tuple[GachaPoolBanner, ...]],
    *,
    kinds: dict[tuple[str, str], PoolKind] | None = None,
    show_standard: bool = True,
) -> list[PoolAnalysis]:
    kinds = kinds if kinds is not None else {}
    snapshots = list(imported.pools)
    snapshot_kinds = {
        (snapshot.pool_id, snapshot.item_type): kinds.get((snapshot.pool_id, snapshot.item_type))
        or kind_for_xhh_pool(snapshot, pool_rules.get(snapshot.pool_id))
        for snapshot in snapshots
    }
    character_intervals, character_progress = _xhh_character_pity_state(imported, snapshot_kinds)
    result = list(pools)
    xhh_sort_timestamps = _xhh_pool_sort_timestamps(snapshots)
    six_by_pool: dict[str, list[XhhSixStar]] = defaultdict(list)
    for item in imported.six_stars:
        six_by_pool[item.pool_id].append(item)

    for snapshot in snapshots:
        kind = snapshot_kinds[(snapshot.pool_id, snapshot.item_type)]
        kinds.setdefault((snapshot.pool_id, snapshot.item_type), kind)
        candidates = [
            index for index, pool in enumerate(result)
            if pool.pool_id == snapshot.pool_id and pool.item_type == snapshot.item_type
        ]
        # 同 poolId 多次开放时，快照只合并进最新一期；快照总数先扣掉其他期已有的官方抽数。
        existing_index = max(
            candidates,
            key=lambda index: (result[index].pool_version, result[index].latest_ts),
            default=None,
        )
        existing = result[existing_index] if existing_index is not None else None
        other_runs_paid = sum(
            result[index].paid_total for index in candidates if index != existing_index
        )
        snapshot_total = max(0, snapshot.total_count - other_runs_paid)
        rule = pool_rules.get(snapshot.pool_id)
        imported_paid_events = [
            _xhh_six_star_event(
                replace(
                    item,
                    interval=character_intervals.get(
                        (item.pool_id, item.unique_key), item.interval,
                    ),
                ),
                snapshot,
                xhh_metadata,
                rule,
                kind,
            )
            for item in six_by_pool.get(snapshot.pool_id, ())
            if not item.is_free
        ]
        imported_free_events = [
            _xhh_six_star_event(item, snapshot, xhh_metadata, rule, kind)
            for item in six_by_pool.get(snapshot.pool_id, ())
            if item.is_free
        ]
        existing_free_batches = existing.free_batches if existing else ()
        free_pull_count = max(
            existing.free_pull_count if existing else 0,
            _xhh_expected_free_pull_count(snapshot, kind),
            10 if imported_free_events else 0,
        )
        free_batches = _merge_xhh_free_batches(
            existing_free_batches,
            free_pull_count,
            snapshot.latest_ts,
            imported_free_events,
            source="rush" if kind.rush_thresholds else ("free_ten" if kind.free_ten_unlock else ""),
        )
        merged_events = _merge_xhh_six_star_events(
            existing.six_stars if existing else (),
            free_batches,
            imported_paid_events,
            prefer_imported_values=_prefer_xhh_event_values(
                snapshot, existing, imported.imported_at,
            ),
        )
        merged_events.sort(key=lambda item: (item.gacha_ts, item.pool_position), reverse=True)

        recorded_total = existing.recorded_total if existing else 0
        paid_total = max(existing.paid_total if existing else 0, snapshot_total)
        total = max(existing.total if existing else 0, paid_total + free_pull_count)
        last_paid_position = max(
            (item.pool_position for item in merged_events if item.pool_position),
            default=0,
        )
        derived_since_six = max(0, paid_total - last_paid_position) if merged_events else paid_total
        pool_since_six = (
            snapshot.current_count
            or character_progress.get(snapshot.pool_id, 0)
            or derived_since_six
        )
        current_count = (
            snapshot.current_count
            or (existing.small_pity_progress if existing else 0)
            or pool_since_six
        ) if snapshot.is_current else 0
        hard_limit, hard_source = _hard_guarantee(kind, rule)
        pool_scope = kind.cumulative_scope == "pool"
        if snapshot.item_type == "角色":
            small_progress = current_count
            small_limit = kind.small_pity if snapshot.is_current else 0
            large_limit = hard_limit if snapshot.is_current and kind.has_up else 0
            keepsake_progress = (
                paid_total % kind.keepsake_cycle
                if snapshot.is_current and pool_scope and kind.keepsake_cycle else 0
            )
            keepsake_claims = (
                paid_total // kind.keepsake_cycle
                if snapshot.is_current and pool_scope and kind.keepsake_cycle else 0
            )
        else:
            small_progress = (
                min(kind.small_pity - 1, (current_count + 9) // 10)
                if snapshot.is_current and kind.small_pity else 0
            )
            small_limit = kind.small_pity if snapshot.is_current else 0
            large_limit = hard_limit if snapshot.is_current and kind.has_up else 0
            keepsake_progress = 0
            keepsake_claims = 0
        large_known = bool(snapshot.is_current and large_limit and rule and rule.up_item_ids)
        consumed_at, consumed_name = _xhh_large_pity_consumption(merged_events, rule, xhh_metadata)
        large_consumed = bool(large_known and consumed_at and consumed_at <= large_limit)
        keepsake_gifts = _merge_xhh_keepsake_gifts(
            existing.keepsake_gifts if existing else (), snapshot, paid_total, rule,
            metadata, keepsake_metadata,
            xhh_metadata=xhh_metadata,
            xhh_events=six_by_pool.get(snapshot.pool_id, ()),
            pool_banners=pool_banners.get(snapshot.pool_id, ()),
            kind=kind,
        )
        weapon_claims = paid_total // 10 if snapshot.item_type == "武器" else 0
        next_rewards = (
            series_next_rewards(kind, paid_total, weapon_claims)
            if snapshot.is_current and pool_scope and (kind.keepsake_cycle or kind.reward_schedule) else ()
        )
        merged = PoolAnalysis(
            pool_id=snapshot.pool_id,
            name=snapshot.pool_name or (existing.name if existing else snapshot.pool_id),
            item_type=snapshot.item_type,
            total=total,
            since_six_star=pool_since_six,
            latest_ts=max(
                snapshot.latest_ts,
                existing.latest_ts if existing else 0,
                max((item.gacha_ts for item in merged_events), default=0),
            ),
            six_stars=tuple(merged_events),
            is_current=snapshot.is_current,
            paid_total=paid_total,
            free_pull_count=free_pull_count,
            free_batches=free_batches,
            small_pity_progress=small_progress,
            small_pity_limit=small_limit,
            large_pity_progress=min(consumed_at or paid_total, large_limit) if large_limit else 0,
            large_pity_limit=large_limit,
            large_pity_known=large_known,
            large_pity_consumed=large_consumed,
            large_pity_consumed_at=consumed_at if large_consumed else 0,
            large_pity_up_name=consumed_name if large_consumed else "",
            keepsake_progress=keepsake_progress,
            keepsake_claims=keepsake_claims,
            recorded_total=recorded_total,
            history_missing_count=max(0, total - recorded_total),
            keepsake_gifts=keepsake_gifts,
            sort_order=snapshot.sort_order,
            up_item_ids=(
                rule.up_item_ids if rule else (existing.up_item_ids if existing else ())
            ),
            banners=pool_banners.get(
                snapshot.pool_id, existing.banners if existing else (),
            ),
            card_key=existing.card_key if existing else _card_key(snapshot.pool_id, 0),
            pool_version=existing.pool_version if existing else 0,
            kind_key=kind.key,
            kind_label=kind.label,
            kind_short=kind.short,
            is_unknown_kind=kind.is_unknown,
            hidden_by_default=kind.key == "standard" and not show_standard,
            up_status_known=_up_status_known(kind, rule),
            pity_family=pity_family(kind, snapshot.pool_id),
            pity_scope=kind.pity_scope,
            soft_pity_start=kind.soft_pity_start if snapshot.item_type == "角色" else 0,
            soft_pity_active=bool(
                snapshot.is_current and kind.soft_pity_start and small_progress >= kind.soft_pity_start - 1
            ),
            five_star_pity_progress=(
                existing.five_star_pity_progress if existing and snapshot.is_current else None
            ),
            five_star_pity_limit=kind.five_star_pity if snapshot.is_current else 0,
            large_pity_source=hard_source if large_limit else "",
            series_key=f"pool:{snapshot.pool_id}",
            series_name=snapshot.pool_name or (existing.name if existing else snapshot.pool_id),
            series_estimated=bool(paid_total > recorded_total),
            weapon_claims=weapon_claims,
            series_total=paid_total,
            series_claims=weapon_claims,
            next_rewards=next_rewards,
        )
        if existing_index is None:
            result.append(merged)
        else:
            result[existing_index] = merged
    return _reorder_merged_pools(result, xhh_sort_timestamps)


def _xhh_character_pity_state(
    imported: XhhGachaImport,
    snapshot_kinds: dict[tuple[str, str], PoolKind] | None = None,
) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """按共享链各串一条：特许一条、重构一条、基础一条；独立链的池不串。"""
    snapshot_kinds = snapshot_kinds or {}
    families: dict[str, list[XhhGachaPool]] = defaultdict(list)
    for item in imported.pools:
        if item.item_type != "角色":
            continue
        kind = snapshot_kinds.get((item.pool_id, item.item_type)) or kind_for_xhh_pool(item)
        if not kind.pity_scope.startswith("shared:"):
            continue
        families[pity_family(kind, item.pool_id)].append(item)
    events_by_pool: dict[str, list[XhhSixStar]] = defaultdict(list)
    for item in imported.six_stars:
        if not item.is_free:
            events_by_pool[item.pool_id].append(item)
    intervals: dict[tuple[str, str], int] = {}
    progress: dict[str, int] = {}
    for pools in families.values():
        if pools and all(item.sort_order >= 0 for item in pools):
            pools.sort(key=lambda item: item.sort_order, reverse=True)
        else:
            pools.sort(key=lambda item: (item.latest_ts, item.pool_name, item.pool_id))
        carry = 0
        for pool in pools:
            events = sorted(
                events_by_pool.get(pool.pool_id, ()),
                key=lambda item: (item.pool_position, item.gacha_ts, item.unique_key),
            )
            previous_position = 0
            for item in events:
                local_interval = max(0, item.pool_position - previous_position)
                intervals[(item.pool_id, item.unique_key)] = carry + local_interval
                carry = 0
                previous_position = item.pool_position
            carry += max(0, pool.total_count - previous_position)
            progress[pool.pool_id] = carry
    return intervals, progress


def calculate_group_expectation(
    pools: tuple[PoolAnalysis, ...] | list[PoolAnalysis],
    group: str,
) -> SixStarExpectation:
    """按期望分组（special / rerun / weapon）计算六星与 UP 期望；常驻武库计入 weapon 组。"""
    kind_keys = EXPECTATION_GROUPS[group]
    item_type = "武器" if group == "weapon" else "角色"
    before_rate, after_rate = SIX_STAR_COMPREHENSIVE_RATES[item_type]
    selected = [
        pool
        for pool in pools
        if pool.item_type == item_type and _analysis_pool_kind(pool).key in kind_keys
    ]
    paid_pulls = sum(
        pool.paid_total if pool.paid_total or pool.free_pull_count else pool.total
        for pool in selected
    )
    free_pulls = sum(pool.free_pull_count for pool in selected)
    account_pulls = paid_pulls + free_pulls
    outcomes = sum(
        len(pool.six_stars)
        + sum(len(batch.six_stars) for batch in pool.free_batches)
        + (len(pool.keepsake_gifts) if item_type == "角色" else 0)
        for pool in selected
    )
    up_outcomes = sum(
        sum(item.item_id in set(pool.up_item_ids) for item in pool.six_stars)
        + sum(
            item.item_id in set(pool.up_item_ids)
            for batch in pool.free_batches
            for item in batch.six_stars
        )
        + (len(pool.keepsake_gifts) if item_type == "角色" else 0)
        for pool in selected
    )
    if item_type == "角色":
        up_before = _character_first_up_expectation()
        up_after = None
    else:
        up_before_rate, up_after_rate = UP_COMPREHENSIVE_RATES[item_type]
        up_before = 1 / up_before_rate
        up_after = 1 / up_after_rate
    combined_before_up = _combined_expectation(
        paid_pulls, free_pulls, before_rate, FREE_SIX_STAR_BASE_RATES[item_type],
    )
    combined_after_up = _combined_expectation(
        paid_pulls, free_pulls, after_rate, FREE_SIX_STAR_BASE_RATES[item_type],
    )
    up_combined_before = _combined_expectation(
        paid_pulls, free_pulls, 1 / up_before, FREE_UP_BASE_RATES[item_type],
    )
    up_combined_after = (
        _combined_expectation(
            paid_pulls, free_pulls, 1 / up_after, FREE_UP_BASE_RATES[item_type],
        )
        if up_after is not None else None
    )
    return SixStarExpectation(
        before_up=1 / before_rate,
        after_up=1 / after_rate,
        combined_before_up=combined_before_up,
        combined_after_up=combined_after_up,
        actual=account_pulls / outcomes if outcomes else None,
        paid_pulls=paid_pulls,
        free_pulls=free_pulls,
        account_pulls=account_pulls,
        outcomes=outcomes,
        up_before=up_before,
        up_after=up_after,
        up_combined_before=up_combined_before,
        up_combined_after=up_combined_after,
        actual_up=account_pulls / up_outcomes if up_outcomes else None,
        up_outcomes=up_outcomes,
        group=group,
        up_known=True,
    )


def calculate_six_star_expectation(
    pools: tuple[PoolAnalysis, ...] | list[PoolAnalysis],
    item_type: str,
) -> SixStarExpectation:
    """兼容入口："角色" → 特许寻访组，"武器" → 武器申领组（含限时、点绘、常驻）。"""
    return calculate_group_expectation(pools, "weapon" if item_type == "武器" else "special")


def _combined_expectation(
    paid_pulls: int,
    free_pulls: int,
    paid_rate: float,
    free_rate: float,
) -> float | None:
    expected_outcomes = paid_pulls * paid_rate + free_pulls * free_rate
    if not expected_outcomes:
        return None
    return (paid_pulls + free_pulls) / expected_outcomes


def _character_first_up_expectation() -> float:
    surviving_states = {0: 1.0}
    expected_pulls = 0.0
    for pool_position in range(1, 121):
        expected_pulls += sum(surviving_states.values())
        if pool_position == 120:
            break
        next_states: dict[int, float] = defaultdict(float)
        for pity_progress, probability in surviving_states.items():
            pull_position = pity_progress + 1
            six_star_rate = (
                1.0
                if pull_position >= 80
                else min(1.0, 0.008 + max(0, pull_position - 65) * 0.05)
            )
            next_states[pity_progress + 1] += probability * (1 - six_star_rate)
            next_states[0] += probability * six_star_rate * 0.5
        surviving_states = next_states
    return expected_pulls


def filter_xhh_import_six_stars(
    imported: XhhGachaImport,
    metadata_by_name: dict[str, GachaItemMetadata],
) -> XhhGachaImport:
    verified_by_pool: dict[str, list[tuple[XhhSixStar, GachaItemMetadata]]] = defaultdict(list)
    for item in imported.six_stars:
        metadata = metadata_by_name.get(_normalized_item_name(item.item_name))
        if metadata is not None and metadata.rarity >= 6:
            verified_by_pool[item.pool_id].append((item, metadata))

    verified: list[XhhSixStar] = []
    for pool in imported.pools:
        chronological = sorted(
            verified_by_pool.get(pool.pool_id, ()),
            key=lambda value: (
                value[0].gacha_ts,
                value[0].pool_position,
                value[0].unique_key,
            ),
        )
        running_position = 0
        pool_items: list[XhhSixStar] = []
        for item, metadata in chronological:
            if not item.is_free:
                running_position += item.interval
            pool_items.append(
                replace(
                    item,
                    item_name=metadata.name or item.item_name,
                    item_type=metadata.item_type or item.item_type,
                    item_id=metadata.item_id or item.item_id,
                    pool_position=0 if item.is_free else running_position,
                )
            )
        verified.extend(reversed(pool_items))
    return replace(imported, six_stars=tuple(verified))


def _xhh_six_star_event(
    item: XhhSixStar,
    pool: XhhGachaPool,
    metadata_by_name: dict[str, GachaItemMetadata],
    rule: GachaPoolRule | None,
    kind: PoolKind | None = None,
) -> SixStarEvent:
    kind = kind or kind_for_xhh_pool(pool, rule)
    item_metadata = metadata_by_name.get(_normalized_item_name(item.item_name))
    item_id = item.item_id or (item_metadata.item_id if item_metadata else "")
    labels: list[str] = []
    if not item.is_free and kind.small_pity:
        if pool.item_type == "角色" and item.interval >= kind.small_pity:
            labels.append("小保底")
        if pool.item_type == "武器" and item.interval >= kind.small_pity * 10:
            labels.append("小保底")
    hard_limit, _source = _hard_guarantee(kind, rule)
    has_up_rule = bool(kind.has_up and rule and rule.up_item_ids)
    if has_up_rule:
        up_status = "up" if item_id in set(rule.up_item_ids) else "off"
    elif item.miss_up:
        up_status = "off"
    else:
        up_status = ""
    is_current_up = up_status != "off"
    if not item.is_free and kind.fixed_guarantee_position and item.pool_position == kind.fixed_guarantee_position:
        labels.append("大保底")
    elif (
        not item.is_free
        and kind.has_up
        and hard_limit
        and item.pool_position == hard_limit
        and is_current_up
    ):
        labels.append("大保底")
    if up_status == "off":
        labels.append("歪")
    return SixStarEvent(
        name=item_metadata.name if item_metadata and item_metadata.name else item.item_name,
        pool_name=pool.pool_name,
        item_type=pool.item_type,
        gacha_ts=item.gacha_ts,
        item_id=item_id,
        interval=item.interval,
        icon_path=item_metadata.icon_path if item_metadata else "",
        pool_position=item.pool_position,
        pity_labels=tuple(labels),
        is_free=item.is_free,
        up_status=up_status if has_up_rule or item.miss_up else "",
        soft_pity_hit=bool(
            pool.item_type == "角色" and not item.is_free and kind.soft_pity_start
            and kind.soft_pity_start <= item.interval < kind.small_pity
        ),
    )


def _same_six_star_event(left: SixStarEvent, right: SixStarEvent) -> bool:
    if _normalized_item_name(left.name) != _normalized_item_name(right.name):
        return False
    if left.pool_position and right.pool_position and left.pool_position == right.pool_position:
        return True
    if left.gacha_ts and right.gacha_ts:
        same_day = datetime.fromtimestamp(left.gacha_ts).date() == datetime.fromtimestamp(right.gacha_ts).date()
        if same_day:
            return True
    return False


def _merge_pity_labels(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(label for group in groups for label in group))


def _merge_xhh_six_star_events(
    paid_events: tuple[SixStarEvent, ...],
    free_batches: tuple[FreePullBatch, ...],
    imported_events: list[SixStarEvent],
    *,
    prefer_imported_values: bool = False,
) -> list[SixStarEvent]:
    references = [
        *paid_events,
        *(event for batch in free_batches for event in batch.six_stars),
    ]
    consumed: set[int] = set()
    merged = list(paid_events)
    for imported in imported_events:
        matched_index = next(
            (
                index
                for index, saved in enumerate(references)
                if index not in consumed and _same_six_star_event(imported, saved)
            ),
            None,
        )
        if matched_index is None:
            merged.append(imported)
        else:
            consumed.add(matched_index)
            if matched_index < len(paid_events):
                saved = merged[matched_index]
                changes = {
                    "pity_labels": _merge_pity_labels(saved.pity_labels, imported.pity_labels),
                    "interval": max(saved.interval, imported.interval),
                    "up_status": saved.up_status or imported.up_status,
                }
                if prefer_imported_values:
                    changes.update(
                        interval=imported.interval,
                        pool_position=imported.pool_position,
                    )
                merged[matched_index] = replace(saved, **changes)
    return merged


def _prefer_xhh_event_values(
    snapshot: XhhGachaPool,
    existing: PoolAnalysis | None,
    imported_at: int,
) -> bool:
    if existing is None or snapshot.total_count <= existing.paid_total:
        return False
    if not snapshot.latest_ts or not imported_at:
        return False
    priority_age_days = OFFICIAL_GACHA_LOOKBACK_DAYS - XHH_PRIORITY_BOUNDARY_MARGIN_DAYS
    return snapshot.latest_ts <= imported_at - priority_age_days * 86_400


def _merge_xhh_free_batches(
    existing: tuple[FreePullBatch, ...],
    total_free: int,
    latest_ts: int,
    imported_events: list[SixStarEvent] | None = None,
    *,
    source: str = "",
) -> tuple[FreePullBatch, ...]:
    batches = list(existing)
    reference_locations = [
        (batch_index, event_index)
        for batch_index, batch in enumerate(batches)
        for event_index, _event in enumerate(batch.six_stars)
    ]
    references = [
        batches[batch_index].six_stars[event_index]
        for batch_index, event_index in reference_locations
    ]
    consumed: set[int] = set()
    pending: list[SixStarEvent] = []
    for imported in imported_events or ():
        matched_index = next(
            (
                index
                for index, saved in enumerate(references)
                if index not in consumed and _same_six_star_event(imported, saved)
            ),
            None,
        )
        if matched_index is None:
            pending.append(imported)
        else:
            consumed.add(matched_index)
            batch_index, event_index = reference_locations[matched_index]
            batch = batches[batch_index]
            saved = batch.six_stars[event_index]
            events = list(batch.six_stars)
            events[event_index] = replace(
                saved,
                pity_labels=_merge_pity_labels(saved.pity_labels, imported.pity_labels),
                up_status=saved.up_status or imported.up_status,
            )
            batches[batch_index] = replace(batch, six_stars=tuple(events))
            references[matched_index] = events[event_index]

    for imported in pending:
        batch_index = next(
            (
                index for index, batch in enumerate(batches)
                if batch.gacha_ts and imported.gacha_ts
                and datetime.fromtimestamp(batch.gacha_ts).date()
                == datetime.fromtimestamp(imported.gacha_ts).date()
            ),
            None,
        )
        if batch_index is not None:
            batch = batches[batch_index]
            batches[batch_index] = replace(batch, six_stars=(*batch.six_stars, replace(imported, is_free=True)))

    missing = max(0, total_free - sum(item.pull_count for item in batches))
    unassigned = [
        item
        for item in pending
        if not any(
            _same_six_star_event(item, saved)
            for batch in batches
            for saved in batch.six_stars
        )
    ]
    while missing:
        pull_count = min(10, missing)
        event_time = unassigned[0].gacha_ts if unassigned else latest_ts
        batch_events = tuple(
            item
            for item in unassigned
            if not event_time or not item.gacha_ts
            or datetime.fromtimestamp(item.gacha_ts).date()
            == datetime.fromtimestamp(event_time).date()
        )
        batches.append(
            FreePullBatch(
                event_time, pull_count,
                tuple(replace(item, is_free=True) for item in batch_events),
                source=source,
            )
        )
        unassigned = [item for item in unassigned if item not in batch_events]
        missing -= pull_count
    return tuple(sorted(batches, key=lambda item: item.gacha_ts, reverse=True))


def _xhh_expected_free_pull_count(snapshot: XhhGachaPool, kind: PoolKind | None = None) -> int:
    if snapshot.free_count:
        return snapshot.free_count
    if snapshot.item_type != "角色":
        return 0
    kind = kind or kind_for_xhh_pool(snapshot)
    if kind.rush_thresholds:
        return 10 * sum(1 for threshold in kind.rush_thresholds if snapshot.total_count >= threshold)
    if kind.free_ten_unlock:
        return 10 if snapshot.total_count >= kind.free_ten_unlock else 0
    return 0


def _xhh_large_pity_consumption(
    events: list[SixStarEvent],
    rule: GachaPoolRule | None,
    metadata_by_name: dict[str, GachaItemMetadata],
) -> tuple[int, str]:
    if rule is None or not rule.up_item_ids:
        return 0, ""
    up_ids = set(rule.up_item_ids)
    up_names = {
        _normalized_item_name(item.name)
        for item in metadata_by_name.values()
        if item.item_id in up_ids
    }
    candidates = [
        item for item in events
        if item.pool_position and (item.item_id in up_ids or _normalized_item_name(item.name) in up_names)
    ]
    if not candidates:
        return 0, ""
    first = min(candidates, key=lambda item: item.pool_position)
    return first.pool_position, first.name


def _merge_xhh_keepsake_gifts(
    existing: tuple[KeepsakeGift, ...],
    snapshot: XhhGachaPool,
    paid_total: int,
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    keepsake_metadata: dict[str, GachaItemMetadata],
    *,
    xhh_metadata: dict[str, GachaItemMetadata] | None = None,
    xhh_events: Iterable[XhhSixStar] = (),
    pool_banners: tuple[GachaPoolBanner, ...] = (),
    kind: PoolKind | None = None,
) -> tuple[KeepsakeGift, ...]:
    kind = kind or kind_for_xhh_pool(snapshot, rule)
    if kind.cumulative_scope != "pool" or (not kind.keepsake_cycle and kind.reward_schedule is None):
        return existing
    if not rule or not rule.up_item_ids:
        return existing
    gifts = {item.pool_position: item for item in existing}
    operator_id = rule.up_item_ids[0]
    operator_metadata = metadata.get(operator_id)
    xhh_metadata = xhh_metadata or {}
    if operator_metadata is None:
        operator_metadata = next(
            (item for item in xhh_metadata.values() if item.item_id == operator_id),
            None,
        )
    if operator_metadata is None:
        operator_metadata = next(
            (
                xhh_metadata.get(_normalized_item_name(item.item_name))
                or GachaItemMetadata(item.item_id, item.item_name, 6, snapshot.item_type)
                for item in xhh_events
                if not item.is_free and not item.miss_up and item.item_name
            ),
            None,
        )
    if operator_metadata is None:
        operator_metadata = next(
            (
                GachaItemMetadata(
                    item.item_id, item.name, 6, item.item_type,
                    icon_path=item.image_path,
                )
                for item in pool_banners
                if item.item_type == snapshot.item_type and item.name
            ),
            None,
        )
    if snapshot.item_type == "武器":
        gift_name = operator_metadata.name if operator_metadata and operator_metadata.name else "当期UP武器"
        gift_id = operator_metadata.item_id if operator_metadata else operator_id
        gift_icon = operator_metadata.icon_path if operator_metadata else ""
        for claim_count, reward in weapon_reward_claims(kind.reward_schedule, paid_total // 10):
            pool_position = claim_count * 10
            if reward == "weapon_box":
                name, item_id, icon = weapon_gift_identity(reward, rule, metadata, pool_banners)
            else:
                name, item_id, icon = gift_name, gift_id, gift_icon
            gifts.setdefault(
                pool_position,
                KeepsakeGift(
                    name, item_id, snapshot.latest_ts, pool_position,
                    icon, gift_type_label(reward), claim_count,
                    gift_kind=reward, series_position=pool_position, estimated=True,
                ),
            )
        return tuple(sorted(gifts.values(), key=lambda item: item.pool_position, reverse=True))
    gift_metadata = keepsake_metadata.get(operator_id)
    gift_name = (
        gift_metadata.name if gift_metadata and gift_metadata.name
        else f"{operator_metadata.name}的信物" if operator_metadata and operator_metadata.name
        else "当期UP干员的信物"
    )
    gift_id = gift_metadata.item_id if gift_metadata else f"item_charpotentialup_{operator_id}"
    gift_icon = gift_metadata.icon_path if gift_metadata else ""
    for position in range(kind.keepsake_cycle, paid_total + 1, kind.keepsake_cycle):
        gifts.setdefault(
            position,
            KeepsakeGift(
                gift_name,
                gift_id,
                snapshot.latest_ts,
                position,
                gift_icon,
                gift_kind="keepsake",
                series_position=position,
                estimated=True,
            ),
        )
    return tuple(sorted(gifts.values(), key=lambda item: item.pool_position, reverse=True))


def _normalized_item_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def _pool_analysis_sort_key(pool: PoolAnalysis) -> tuple[int, int, int, str]:
    if pool.sort_order >= 0:
        return (0, pool.sort_order, 0, pool.name)
    return (1, 0, -pool.latest_ts, pool.name)


def _xhh_pool_sort_timestamps(
    pools: list[XhhGachaPool],
) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    grouped: dict[str, list[XhhGachaPool]] = defaultdict(list)
    for pool in pools:
        grouped[pool.item_type].append(pool)
    for item_type, items in grouped.items():
        ordered = sorted(
            items,
            key=lambda item: (
                item.sort_order if item.sort_order >= 0 else 1_000_000,
                -item.latest_ts,
                item.pool_name,
            ),
        )
        for index, pool in enumerate(ordered):
            timestamp = pool.latest_ts
            if not timestamp:
                previous = next(
                    (
                        (previous_index, ordered[previous_index].latest_ts)
                        for previous_index in range(index - 1, -1, -1)
                        if ordered[previous_index].latest_ts
                    ),
                    None,
                )
                following = next(
                    (
                        (following_index, ordered[following_index].latest_ts)
                        for following_index in range(index + 1, len(ordered))
                        if ordered[following_index].latest_ts
                    ),
                    None,
                )
                if previous and following and previous[1] > following[1]:
                    span = following[0] - previous[0]
                    timestamp = previous[1] - (
                        (previous[1] - following[1]) * (index - previous[0]) // span
                    )
                elif previous:
                    timestamp = max(1, previous[1] - (index - previous[0]) * 86_400)
                elif following:
                    timestamp = following[1] + (following[0] - index) * 86_400
                else:
                    timestamp = len(ordered) - index
            result[(pool.pool_id, item_type)] = timestamp
    return result


def _reorder_merged_pools(
    pools: list[PoolAnalysis],
    xhh_sort_timestamps: dict[tuple[str, str], int],
) -> list[PoolAnalysis]:
    """合并后按时间重排：sort_order 按 item_type 连续编号，当期按 (item_type, kind) 各取组内第一个。"""
    result = list(pools)
    for item_type in {pool.item_type for pool in result}:
        indexes = [index for index, pool in enumerate(result) if pool.item_type == item_type]
        indexes.sort(
            key=lambda index: (
                -max(
                    result[index].latest_ts,
                    xhh_sort_timestamps.get((result[index].pool_id, item_type), 0),
                ),
                result[index].name,
                result[index].pool_id,
            )
        )
        seen_kinds: set[str] = set()
        for sort_order, index in enumerate(indexes):
            kind_key = result[index].kind_key or _analysis_pool_kind(result[index]).key
            is_current = kind_key not in seen_kinds
            seen_kinds.add(kind_key)
            result[index] = replace(
                result[index],
                sort_order=sort_order,
                is_current=is_current,
            )
    return result


def _record_sort_key(record: GachaRecord) -> tuple[int, tuple[int, str]]:
    return (record.gacha_ts, _seq_sort(record.seq_id))


def _latest_record_key(records: list[GachaRecord]) -> tuple[int, tuple[int, str]]:
    return max((_record_sort_key(item) for item in records), default=(0, (0, "")))


def _pulls_since_six(records: list[GachaRecord]) -> int:
    pulls = 0
    for item in reversed(records):
        if item.rarity >= 6:
            break
        pulls += 1
    return pulls


def _weapon_ten_batch_progress(records: list[GachaRecord]) -> int:
    batches: dict[int, list[GachaRecord]] = defaultdict(list)
    for item in records:
        batches[item.gacha_ts].append(item)
    progress = 0
    for batch in (batches[key] for key in sorted(batches)):
        if any(item.rarity >= 6 for item in batch):
            progress = 0
        else:
            progress += 1
    return progress


def _large_pity_consumption(
    records: list[GachaRecord],
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    kind: PoolKind | None = None,
) -> tuple[int, str]:
    if rule is None or not rule.up_item_ids:
        return 0, ""
    if kind is not None:
        if not kind.has_up or not _hard_guarantee(kind, rule)[0]:
            return 0, ""
    elif not rule.hard_guarantee:
        return 0, ""
    up_item_ids = set(rule.up_item_ids)
    for index, item in enumerate(records, 1):
        if item.rarity < 6 or item.item_id not in up_item_ids:
            continue
        item_metadata = metadata.get(item.item_id)
        return index, item_metadata.name if item_metadata and item_metadata.name else item.item_name
    return 0, ""


def _weapon_pity_labels(
    records: list[GachaRecord],
    rule: GachaPoolRule | None,
    kind: PoolKind | None = None,
) -> dict[str, tuple[str, ...]]:
    kind = kind or KINDS_BY_KEY["weapon_limited"]
    batches: dict[int, list[tuple[int, GachaRecord]]] = defaultdict(list)
    for position, item in enumerate(records, 1):
        batches[item.gacha_ts].append((position, item))
    result: dict[str, list[str]] = defaultdict(list)
    batch_progress = 0
    up_item_ids = set(rule.up_item_ids) if rule else set()
    hard_guarantee, _source = _hard_guarantee(kind, rule)
    for batch in (batches[key] for key in sorted(batches)):
        batch_progress += 1
        six_stars = [(position, item) for position, item in batch if item.rarity >= 6]
        if six_stars:
            if kind.small_pity and batch_progress >= kind.small_pity:
                for _, item in six_stars:
                    result[item.seq_id].append("小保底")
            batch_progress = 0
        for position, item in six_stars:
            if hard_guarantee and position == hard_guarantee and item.item_id in up_item_ids:
                result[item.seq_id].append("大保底")
    return {seq_id: tuple(labels) for seq_id, labels in result.items()}


def _build_keepsake_gifts(
    records: list[GachaRecord],
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    keepsake_metadata: dict[str, GachaItemMetadata],
    *,
    item_type: str = "角色",
    kind: PoolKind | None = None,
) -> list[KeepsakeGift]:
    if kind is None:
        kind = KINDS_BY_KEY["weapon_limited" if item_type == "武器" else "special"]
    if not rule or not rule.up_item_ids:
        return []
    if kind.item_type == "武器":
        if kind.reward_schedule is None:
            return []
        batches: dict[int, list[GachaRecord]] = defaultdict(list)
        for record in records:
            batches[record.gacha_ts].append(record)
        ordered_batches = [batches[key] for key in sorted(batches)]
        batch_end_positions: list[int] = []
        running_position = 0
        for batch in ordered_batches:
            running_position += len(batch)
            batch_end_positions.append(running_position)
        gifts: list[KeepsakeGift] = []
        for claim_count, reward in weapon_reward_claims(kind.reward_schedule, len(ordered_batches)):
            name, item_id, icon_path = weapon_gift_identity(reward, rule, metadata)
            gifts.append(
                KeepsakeGift(
                    name, item_id, ordered_batches[claim_count - 1][-1].gacha_ts,
                    batch_end_positions[claim_count - 1], icon_path, gift_type_label(reward), claim_count,
                    gift_kind=reward, series_position=batch_end_positions[claim_count - 1],
                )
            )
        return gifts
    if not kind.keepsake_cycle:
        return []
    name, item_id, icon_path = keepsake_identity(rule, metadata, keepsake_metadata)
    return [
        KeepsakeGift(
            name, item_id, records[position - 1].gacha_ts, position, icon_path,
            gift_kind="keepsake", series_position=position,
        )
        for position in range(kind.keepsake_cycle, len(records) + 1, kind.keepsake_cycle)
    ]


def _build_free_batches(
    records: list[GachaRecord],
    metadata: dict[str, GachaItemMetadata],
    *,
    kind: PoolKind | None = None,
    rule: GachaPoolRule | None = None,
) -> list[FreePullBatch]:
    batches: dict[int, list[GachaRecord]] = defaultdict(list)
    for item in records:
        batches[item.gacha_ts].append(item)
    source = ""
    thresholds: tuple[int, ...] = ()
    if kind is not None:
        if kind.rush_thresholds:
            source, thresholds = "rush", kind.rush_thresholds
        elif kind.free_ten_unlock:
            source = "free_ten"
    result: list[FreePullBatch] = []
    for index, gacha_ts in enumerate(sorted(batches)):
        items = sorted(batches[gacha_ts], key=_record_sort_key)
        six_stars: list[SixStarEvent] = []
        for item in items:
            if item.rarity < 6:
                continue
            item_metadata = metadata.get(item.item_id)
            six_stars.append(
                SixStarEvent(
                    item.item_name,
                    item.pool_name,
                    item.item_type,
                    item.gacha_ts,
                    item_id=item.item_id,
                    icon_path=item_metadata.icon_path if item_metadata else "",
                    is_free=True,
                    up_status=_up_status(item.item_id, rule, kind) if kind is not None else "",
                )
            )
        result.append(
            FreePullBatch(
                gacha_ts, len(items), tuple(six_stars),
                source=source,
                threshold=thresholds[index] if index < len(thresholds) else 0,
            )
        )
    return result


def format_timestamp(value: int) -> str:
    if not value:
        return "--"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _seq_sort(value: str) -> tuple[int, str]:
    try:
        return (0, f"{int(value):030d}")
    except (TypeError, ValueError):
        return (1, str(value))
