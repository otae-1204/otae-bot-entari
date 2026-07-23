from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Awaitable, Callable

from .account_client import CHARACTER_POOL_TYPES, EndfieldAPIError, EndfieldOfficialClient
from .account_crypto import CredentialCipher
from .account_store import EndfieldRole, EndfieldStore, GachaRecord, XhhGachaImport, XhhGachaPool, XhhSixStar
from .gacha_assets import GachaItemMetadata, GachaPoolRule, apply_gacha_metadata


POOL_TYPE_LABELS = {
    "E_CharacterGachaPoolType_Special": "特许寻访",
    "E_CharacterGachaPoolType_Joint": "联合寻访",
    "E_CharacterGachaPoolType_Standard": "常驻寻访",
    "E_CharacterGachaPoolType_Beginner": "启程寻访",
}
OFFICIAL_GACHA_LOOKBACK_DAYS = 90
XHH_PRIORITY_BOUNDARY_MARGIN_DAYS = 30
SIX_STAR_COMPREHENSIVE_RATES = {
    "角色": (0.020387, 0.022720),
    "武器": (0.053546, 0.062212),
}
UP_COMPREHENSIVE_RATES = {
    "武器": (0.018533, 0.018678),
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
                raise TaskAlreadyRunning("任务正在进行")
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


@dataclass(frozen=True, slots=True)
class PoolAnalysis:
    pool_id: str
    name: str
    item_type: str
    total: int
    since_six_star: int
    latest_ts: int = 0
    six_stars: tuple[SixStarEvent, ...] = ()
    is_current: bool = False
    paid_total: int = 0
    free_pull_count: int = 0
    free_batches: tuple[FreePullBatch, ...] = ()
    small_pity_progress: int = 0
    small_pity_limit: int = 0
    large_pity_progress: int = 0
    large_pity_limit: int = 0
    large_pity_known: bool = False
    large_pity_consumed: bool = False
    large_pity_consumed_at: int = 0
    large_pity_up_name: str = ""
    keepsake_progress: int = 0
    keepsake_claims: int = 0
    recorded_total: int = 0
    history_missing_count: int = 0
    keepsake_gifts: tuple[KeepsakeGift, ...] = ()
    sort_order: int = -1
    up_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SixStarEvent:
    name: str
    pool_name: str
    item_type: str
    gacha_ts: int
    item_id: str = ""
    interval: int = 0
    icon_path: str = ""
    pool_position: int = 0
    pity_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FreePullBatch:
    gacha_ts: int
    pull_count: int
    six_stars: tuple[SixStarEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class KeepsakeGift:
    name: str
    item_id: str
    gacha_ts: int
    pool_position: int
    icon_path: str = ""


@dataclass(frozen=True, slots=True)
class SixStarExpectation:
    before_up: float
    after_up: float
    actual: float | None
    paid_pulls: int
    account_pulls: int
    outcomes: int
    up_before: float
    up_after: float | None
    actual_up: float | None
    up_outcomes: int


@dataclass(frozen=True, slots=True)
class GachaAnalysis:
    role: EndfieldRole
    total: int
    rarity_counts: dict[int, int]
    pools: tuple[PoolAnalysis, ...]
    six_stars: tuple[SixStarEvent, ...]
    intervals: tuple[int, ...]
    average_interval: float | None
    last_sync_at: int
    complete: bool
    errors: tuple[str, ...]
    paid_total: int = 0
    free_pull_count: int = 0
    free_ten_count: int = 0
    recorded_total: int = 0
    history_missing_count: int = 0
    xhh_imported_at: int = 0


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
            u8_token = await self.client.get_u8_token(account_token, role.binding_uid)
            character_names = await self.client.character_pool_names(u8_token, role.server_id)
            semaphore = asyncio.Semaphore(3)
            jobs: list[Awaitable[StreamSyncResult]] = []
            for pool_type in CHARACTER_POOL_TYPES:
                label = character_names.get(pool_type) or POOL_TYPE_LABELS.get(pool_type, pool_type)
                jobs.append(
                    self._sync_stream(
                        role, f"char:{pool_type}", label, full, semaphore,
                        lambda cursor, pool_type=pool_type, label=label: self.client.character_records(
                            role, u8_token, pool_type, seq_id=cursor, pool_name=label
                        ),
                    )
                )
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
    ) -> GachaAnalysis:
        records = self.store.list_gacha_records(role, limit=100000)
        states = self.store.list_sync_states(role)
        pool_totals = self.store.list_gacha_pool_totals(role)
        xhh_import = self.store.get_xhh_gacha_import(role)
        return build_gacha_analysis(
            role, records, states, metadata, pool_rules, pool_totals,
            xhh_import=xhh_import, xhh_metadata=xhh_metadata,
            keepsake_metadata=keepsake_metadata,
        )


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
) -> GachaAnalysis:
    metadata = metadata or {}
    pool_rules = pool_rules or {}
    pool_total_overrides = pool_total_overrides or {}
    xhh_metadata = xhh_metadata or {}
    keepsake_metadata = keepsake_metadata or {}
    if xhh_import is not None:
        xhh_import = filter_xhh_import_six_stars(xhh_import, xhh_metadata)
    records = [
        item
        for item in apply_gacha_metadata(records, metadata)
        if not _is_hidden_standard_pool(item)
    ]
    rarity_counts = dict(sorted(Counter(item.rarity for item in records).items(), reverse=True))
    grouped: dict[tuple[str, str], list[GachaRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.pool_id, record.item_type)].append(record)

    current_pool_keys: dict[str, tuple[str, str]] = {}
    for key, items in grouped.items():
        current = current_pool_keys.get(key[1])
        if current is None or _latest_record_key(items) > _latest_record_key(grouped[current]):
            current_pool_keys[key[1]] = key

    role_paid = sorted(
        (item for item in records if item.item_type == "角色" and not item.is_free),
        key=_record_sort_key,
    )
    role_intervals: dict[tuple[str, str], int] = {}
    role_average_keys: set[tuple[str, str]] = set()
    role_since_six: dict[str, int] = defaultdict(int)
    role_has_previous_six: set[str] = set()
    for item in role_paid:
        pity_family = _character_pity_family(item)
        role_since_six[pity_family] += 1
        if item.rarity >= 6:
            event_key = (item.pool_id, item.seq_id)
            role_intervals[event_key] = role_since_six[pity_family]
            if pity_family in role_has_previous_six:
                role_average_keys.add(event_key)
            role_since_six[pity_family] = 0
            role_has_previous_six.add(pity_family)

    pools: list[PoolAnalysis] = []
    six_events: list[SixStarEvent] = []
    interval_events: list[tuple[int, int]] = []
    for (pool_id, item_type), items in grouped.items():
        ordered = sorted(items, key=_record_sort_key)
        paid = [item for item in ordered if not item.is_free]
        free = [item for item in ordered if item.is_free]
        is_current = current_pool_keys.get(item_type) == (pool_id, item_type)
        since_six = _pulls_since_six(paid)
        pulls_since_six = 0
        has_previous_pool_six = False
        pool_six_events: list[SixStarEvent] = []
        pool_rule = pool_rules.get(pool_id)
        pity_family = _character_pity_family(ordered[0]) if item_type == "角色" else ""
        is_joint_pool = item_type == "角色" and _is_joint_character_pool(ordered[0])
        is_beginner_pool = item_type == "角色" and _is_beginner_character_pool(ordered[0])
        is_non_up_pool = is_joint_pool or is_beginner_pool
        weapon_pity_labels = _weapon_pity_labels(paid, pool_rule) if item_type == "武器" else {}
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
            pity_labels: list[str] = []
            if item_type == "角色":
                if interval >= 80:
                    pity_labels.append("小保底")
                if is_beginner_pool and pool_position == 40:
                    pity_labels.append("大保底")
                is_current_up = bool(pool_rule and item.item_id in pool_rule.up_item_ids)
                if (
                    not is_beginner_pool and is_current_up and not seen_current_up and pool_rule
                    and pool_rule.hard_guarantee and pool_position == pool_rule.hard_guarantee
                ):
                    pity_labels.append("大保底")
                seen_current_up = seen_current_up or is_current_up
            else:
                pity_labels.extend(weapon_pity_labels.get(item.seq_id, ()))
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
            )
            pool_six_events.append(event)
            six_events.append(event)
            pulls_since_six = 0
            has_previous_pool_six = True
        free_batches = _build_free_batches(free, metadata)
        keepsake_gifts = (
            _build_keepsake_gifts(paid, pool_rule, metadata, keepsake_metadata)
            if item_type == "角色" and not is_joint_pool else []
        )
        for batch in free_batches:
            six_events.extend(batch.six_stars)
        if item_type == "角色":
            small_progress = role_since_six[pity_family] if is_current else 0
            small_limit = 80 if is_current else 0
            large_limit = (
                (pool_rule.hard_guarantee if pool_rule else 120)
                if is_current and not is_non_up_pool else 0
            )
            large_known = bool(is_current and pool_rule and pool_rule.up_item_ids and large_limit)
            large_consumed_at, large_up_name = _large_pity_consumption(paid, pool_rule, metadata)
            large_consumed = bool(is_current and large_known and large_consumed_at)
            keepsake_progress = len(paid) % 240 if is_current and not is_non_up_pool else 0
            keepsake_claims = len(paid) // 240 if is_current and not is_non_up_pool else 0
        else:
            small_progress = _weapon_ten_batch_progress(paid) if is_current else 0
            small_limit = 4 if is_current else 0
            large_limit = (pool_rule.hard_guarantee if pool_rule else 80) if is_current else 0
            large_known = bool(is_current and pool_rule and pool_rule.up_item_ids)
            large_consumed = False
            large_consumed_at = 0
            large_up_name = ""
            keepsake_progress = 0
            keepsake_claims = 0
        recorded_total = len(items)
        total = max(recorded_total, int(pool_total_overrides.get(pool_id, 0) or 0))
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
            )
        )
    if xhh_import is not None:
        pools = _merge_xhh_pools(
            pools, xhh_import, metadata, xhh_metadata, pool_rules, keepsake_metadata
        )
    pools.sort(key=_pool_analysis_sort_key)
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
    )


def _merge_xhh_pools(
    pools: list[PoolAnalysis],
    imported: XhhGachaImport,
    metadata: dict[str, GachaItemMetadata],
    xhh_metadata: dict[str, GachaItemMetadata],
    pool_rules: dict[str, GachaPoolRule],
    keepsake_metadata: dict[str, GachaItemMetadata],
) -> list[PoolAnalysis]:
    visible_snapshots = [item for item in imported.pools if not _is_hidden_xhh_pool(item)]
    character_intervals, character_progress = _xhh_character_pity_state(imported)
    current_types = {item.item_type for item in visible_snapshots if item.is_current}
    result = [
        replace(item, is_current=False) if item.item_type in current_types else item
        for item in pools
    ]
    six_by_pool: dict[str, list[XhhSixStar]] = defaultdict(list)
    for item in imported.six_stars:
        six_by_pool[item.pool_id].append(item)

    for snapshot in visible_snapshots:
        existing_index = next(
            (
                index for index, pool in enumerate(result)
                if pool.pool_id == snapshot.pool_id and pool.item_type == snapshot.item_type
            ),
            None,
        )
        existing = result[existing_index] if existing_index is not None else None
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
            )
            for item in six_by_pool.get(snapshot.pool_id, ())
            if not item.is_free
        ]
        imported_free_events = [
            _xhh_six_star_event(item, snapshot, xhh_metadata, rule)
            for item in six_by_pool.get(snapshot.pool_id, ())
            if item.is_free
        ]
        existing_free_batches = existing.free_batches if existing else ()
        free_pull_count = max(
            existing.free_pull_count if existing else 0,
            _xhh_expected_free_pull_count(snapshot),
            10 if imported_free_events else 0,
        )
        free_batches = _merge_xhh_free_batches(
            existing_free_batches,
            free_pull_count,
            snapshot.latest_ts,
            imported_free_events,
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
        paid_total = max(existing.paid_total if existing else 0, snapshot.total_count)
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
        pool_identity = f"{snapshot.pool_type} {snapshot.pool_id}".casefold()
        is_joint = snapshot.item_type == "角色" and "joint" in pool_identity
        is_beginner = snapshot.item_type == "角色" and "beginner" in pool_identity
        is_non_up = is_joint or is_beginner
        if snapshot.item_type == "角色":
            small_progress = current_count
            small_limit = 80 if snapshot.is_current else 0
            large_limit = (
                (rule.hard_guarantee if rule and rule.hard_guarantee else 120)
                if snapshot.is_current and not is_non_up else 0
            )
            keepsake_progress = paid_total % 240 if snapshot.is_current and not is_non_up else 0
            keepsake_claims = paid_total // 240 if snapshot.is_current and not is_non_up else 0
        else:
            small_progress = min(3, (current_count + 9) // 10) if snapshot.is_current else 0
            small_limit = 4 if snapshot.is_current else 0
            large_limit = (
                (rule.hard_guarantee if rule and rule.hard_guarantee else 80)
                if snapshot.is_current else 0
            )
            keepsake_progress = 0
            keepsake_claims = 0
        large_known = bool(snapshot.is_current and large_limit and rule and rule.up_item_ids)
        consumed_at, consumed_name = _xhh_large_pity_consumption(merged_events, rule, xhh_metadata)
        large_consumed = bool(large_known and consumed_at and consumed_at <= large_limit)
        keepsake_gifts = _merge_xhh_keepsake_gifts(
            existing.keepsake_gifts if existing else (), snapshot, paid_total, rule,
            metadata, keepsake_metadata,
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
        )
        if existing_index is None:
            result.append(merged)
        else:
            result[existing_index] = merged
    return result


def _xhh_character_pity_state(
    imported: XhhGachaImport,
) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    pools = [item for item in imported.pools if _is_xhh_special_character_pool(item)]
    if pools and all(item.sort_order >= 0 for item in pools):
        pools.sort(key=lambda item: item.sort_order, reverse=True)
    else:
        pools.sort(key=lambda item: (item.latest_ts, item.pool_name, item.pool_id))
    events_by_pool: dict[str, list[XhhSixStar]] = defaultdict(list)
    for item in imported.six_stars:
        if not item.is_free:
            events_by_pool[item.pool_id].append(item)
    intervals: dict[tuple[str, str], int] = {}
    progress: dict[str, int] = {}
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


def calculate_six_star_expectation(
    pools: tuple[PoolAnalysis, ...] | list[PoolAnalysis],
    item_type: str,
) -> SixStarExpectation:
    before_rate, after_rate = SIX_STAR_COMPREHENSIVE_RATES[item_type]
    selected = [
        pool
        for pool in pools
        if pool.item_type == item_type
        and (item_type != "角色" or _is_expectation_character_pool(pool))
    ]
    paid_pulls = sum(
        pool.paid_total if pool.paid_total or pool.free_pull_count else pool.total
        for pool in selected
    )
    account_pulls = paid_pulls + sum(pool.free_pull_count for pool in selected)
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
    return SixStarExpectation(
        before_up=1 / before_rate,
        after_up=1 / after_rate,
        actual=account_pulls / outcomes if outcomes else None,
        paid_pulls=paid_pulls,
        account_pulls=account_pulls,
        outcomes=outcomes,
        up_before=up_before,
        up_after=up_after,
        actual_up=account_pulls / up_outcomes if up_outcomes else None,
        up_outcomes=up_outcomes,
    )


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


def _is_expectation_character_pool(pool: PoolAnalysis) -> bool:
    identity = f"{pool.pool_id} {pool.name}".casefold()
    return not any(
        marker in identity
        for marker in ("joint", "beginner", "standard", "辉光庆典", "新手池", "基础寻访")
    )


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
) -> SixStarEvent:
    item_metadata = metadata_by_name.get(_normalized_item_name(item.item_name))
    item_id = item.item_id or (item_metadata.item_id if item_metadata else "")
    labels: list[str] = []
    pool_identity = f"{pool.pool_type} {pool.pool_id}".casefold()
    is_beginner = pool.item_type == "角色" and "beginner" in pool_identity
    if not item.is_free and pool.item_type == "角色" and item.interval >= 80:
        labels.append("小保底")
    if not item.is_free and pool.item_type == "武器" and item.interval >= 40:
        labels.append("小保底")
    hard_guarantee = 40 if is_beginner else (
        rule.hard_guarantee if rule and rule.hard_guarantee else (80 if pool.item_type == "武器" else 120)
    )
    has_up_rule = bool(rule and rule.up_item_ids)
    is_current_up = item_id in set(rule.up_item_ids) if has_up_rule else not item.miss_up
    if not item.is_free and is_beginner and item.pool_position == hard_guarantee:
        labels.append("大保底")
    elif (
        not item.is_free
        and item.pool_position == hard_guarantee
        and is_current_up
    ):
        labels.append("大保底")
    if item.miss_up:
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
            batches[batch_index] = replace(batch, six_stars=(*batch.six_stars, imported))

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
        batches.append(FreePullBatch(event_time, pull_count, batch_events))
        unassigned = [item for item in unassigned if item not in batch_events]
        missing -= pull_count
    return tuple(sorted(batches, key=lambda item: item.gacha_ts, reverse=True))


def _xhh_expected_free_pull_count(snapshot: XhhGachaPool) -> int:
    if snapshot.free_count:
        return snapshot.free_count
    identity = f"{snapshot.pool_type} {snapshot.pool_id}".casefold()
    if snapshot.item_type != "角色":
        return 0
    if any(marker in identity for marker in ("joint", "standard", "beginner")):
        return 0
    return 10 if "special" in identity else 0


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
) -> tuple[KeepsakeGift, ...]:
    if snapshot.item_type != "角色" or "joint" in snapshot.pool_type.casefold() or not rule or not rule.up_item_ids:
        return existing
    gifts = {item.pool_position: item for item in existing}
    operator_id = rule.up_item_ids[0]
    operator_metadata = metadata.get(operator_id)
    gift_metadata = keepsake_metadata.get(operator_id)
    gift_name = (
        gift_metadata.name if gift_metadata and gift_metadata.name
        else f"{operator_metadata.name}的信物" if operator_metadata and operator_metadata.name
        else "当期UP干员的信物"
    )
    gift_id = gift_metadata.item_id if gift_metadata else f"item_charpotentialup_{operator_id}"
    gift_icon = gift_metadata.icon_path if gift_metadata else ""
    for position in range(240, paid_total + 1, 240):
        gifts.setdefault(
            position,
            KeepsakeGift(
                gift_name,
                gift_id,
                snapshot.latest_ts,
                position,
                gift_icon,
            ),
        )
    return tuple(sorted(gifts.values(), key=lambda item: item.pool_position, reverse=True))


def _normalized_item_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def _pool_analysis_sort_key(pool: PoolAnalysis) -> tuple[int, int, int, str]:
    if pool.sort_order >= 0:
        return (0, pool.sort_order, 0, pool.name)
    return (1, 0, -pool.latest_ts, pool.name)


def _is_hidden_xhh_pool(pool: XhhGachaPool) -> bool:
    return (
        pool.pool_id.casefold() == "standard"
        or "standard" in pool.pool_type.casefold()
        or pool.pool_name.strip() == "基础寻访"
    )


def _is_xhh_special_character_pool(pool: XhhGachaPool) -> bool:
    identity = f"{pool.pool_type} {pool.pool_id}".casefold()
    return (
        pool.item_type == "角色"
        and not any(marker in identity for marker in ("joint", "beginner", "standard"))
        and pool.pool_name.strip() != "基础寻访"
    )


def _is_hidden_standard_pool(record: GachaRecord) -> bool:
    return (
        record.pool_id.casefold() == "standard"
        or "standard" in record.pool_type.casefold()
        or record.pool_name.strip() == "基础寻访"
    )


def _is_joint_character_pool(record: GachaRecord) -> bool:
    identity = f"{record.pool_type} {record.pool_id}".casefold()
    return "joint" in identity


def _character_pity_family(record: GachaRecord) -> str:
    if _is_joint_character_pool(record):
        return f"joint:{record.pool_id.casefold()}"
    if _is_beginner_character_pool(record):
        return f"beginner:{record.pool_id.casefold()}"
    return "special"


def _is_beginner_character_pool(record: GachaRecord) -> bool:
    identity = f"{record.pool_type} {record.pool_id}".casefold()
    return "beginner" in identity


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
) -> tuple[int, str]:
    if rule is None or not rule.up_item_ids or not rule.hard_guarantee:
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
) -> dict[str, tuple[str, ...]]:
    batches: dict[int, list[tuple[int, GachaRecord]]] = defaultdict(list)
    for position, item in enumerate(records, 1):
        batches[item.gacha_ts].append((position, item))
    result: dict[str, list[str]] = defaultdict(list)
    batch_progress = 0
    up_item_ids = set(rule.up_item_ids) if rule else set()
    hard_guarantee = rule.hard_guarantee if rule else 80
    for batch in (batches[key] for key in sorted(batches)):
        batch_progress += 1
        six_stars = [(position, item) for position, item in batch if item.rarity >= 6]
        if six_stars:
            if batch_progress >= 4:
                for _, item in six_stars:
                    result[item.seq_id].append("小保底")
            batch_progress = 0
        for position, item in six_stars:
            if position == hard_guarantee and item.item_id in up_item_ids:
                result[item.seq_id].append("大保底")
    return {seq_id: tuple(labels) for seq_id, labels in result.items()}


def _build_keepsake_gifts(
    records: list[GachaRecord],
    rule: GachaPoolRule | None,
    metadata: dict[str, GachaItemMetadata],
    keepsake_metadata: dict[str, GachaItemMetadata],
) -> list[KeepsakeGift]:
    if not rule or not rule.up_item_ids:
        return []
    operator_id = rule.up_item_ids[0]
    operator_metadata = metadata.get(operator_id)
    gift_metadata = keepsake_metadata.get(operator_id)
    name = (
        gift_metadata.name if gift_metadata and gift_metadata.name
        else f"{operator_metadata.name}的信物" if operator_metadata and operator_metadata.name
        else "当期UP干员的信物"
    )
    item_id = gift_metadata.item_id if gift_metadata else f"item_charpotentialup_{operator_id}"
    icon_path = gift_metadata.icon_path if gift_metadata else ""
    return [
        KeepsakeGift(name, item_id, records[position - 1].gacha_ts, position, icon_path)
        for position in range(240, len(records) + 1, 240)
    ]


def _build_free_batches(
    records: list[GachaRecord],
    metadata: dict[str, GachaItemMetadata],
) -> list[FreePullBatch]:
    batches: dict[int, list[GachaRecord]] = defaultdict(list)
    for item in records:
        batches[item.gacha_ts].append(item)
    result: list[FreePullBatch] = []
    for gacha_ts in sorted(batches):
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
                )
            )
        result.append(FreePullBatch(gacha_ts, len(items), tuple(six_stars)))
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
