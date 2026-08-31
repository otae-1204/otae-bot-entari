from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from utils.onebot_api import call_onebot_action

from .account_client import EndfieldAPIError, EndfieldOfficialClient, is_asia_role
from .account_crypto import CredentialCipher, CredentialKeyError
from .account_i18n import localized_text, semantic_label
from .account_store import (
    EndfieldRole,
    EndfieldStore,
    OperatorCatalogEntry,
    OperatorRosterSnapshot,
    OperatorSnapshotMember,
)
from .akedata_client import _get, fetch_akedata_manifest
from .gacha import ROLE_TASKS, TaskAlreadyRunning


SNAPSHOT_TTL_SECONDS = 48 * 60 * 60
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
REFRESH_CONCURRENCY = 2
SYSTEMIC_FAILURE_THRESHOLD = 3
SYSTEMIC_BACKOFF_BASE_SECONDS = 2.0
_SYSTEMIC_COMMUNITY_OPERATIONS = {"账号授权", "获取社区凭据", "刷新社区签名"}
_SYSTEMIC_COMMUNITY_CODES = {"405", "429", "502", "503", "504"}
_TABLE_MAX_BYTES = 24 * 1024 * 1024
_I18N_MAX_BYTES = 64 * 1024 * 1024
_HEX_ID_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)
_ENDMIN_MALE_SOURCE = "chr_0002_endminm"
_ENDMIN_ACCOUNT_ALIAS_SOURCE = "chr_9000_endmin"
_ENDMIN_FEMALE_SOURCE = "chr_0003_endminf"
_ENDMIN_MALE_KEY = hashlib.md5(_ENDMIN_MALE_SOURCE.encode("utf-8")).hexdigest()
_ENDMIN_FEMALE_KEY = hashlib.md5(_ENDMIN_FEMALE_SOURCE.encode("utf-8")).hexdigest()
_ENDMIN_ACCOUNT_ALIAS_KEY = hashlib.md5(_ENDMIN_ACCOUNT_ALIAS_SOURCE.encode("utf-8")).hexdigest()
_ENDMIN_DISPLAY_NAMES = {
    _ENDMIN_MALE_SOURCE: "管理员·男",
    _ENDMIN_FEMALE_SOURCE: "管理员·女",
}


class SnapshotValidationError(ValueError):
    pass


class GroupMemberListError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        standard_error: Exception | None = None,
        fallback_error: Exception | None = None,
    ):
        super().__init__(message)
        self.standard_error_type = type(standard_error).__name__ if standard_error else "none"
        self.fallback_error_type = type(fallback_error).__name__ if fallback_error else "none"


class OwnershipStatsRendererUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PotentialBucket:
    key: str
    label: str
    count: int
    rate: float | None


@dataclass(frozen=True, slots=True)
class OperatorOwnership:
    operator_key: str
    source_id: str
    name: str
    rarity: int
    profession: str
    sort_order: int
    owned_count: int
    sample_count: int
    ownership_rate: float | None
    potential_buckets: tuple[PotentialBucket, ...]


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    kind: Literal["profession", "rarity"]
    label: str
    operator_count: int
    owned_slots: int
    possible_slots: int
    collection_rate: float | None


@dataclass(frozen=True, slots=True)
class OwnershipStatsSegment:
    region: Literal["all", "cn", "asia"]
    eligible_sample_count: int
    valid_sample_count: int
    excluded_sample_count: int
    operators: tuple[OperatorOwnership, ...]
    professions: tuple[CollectionSummary, ...]
    rarities: tuple[CollectionSummary, ...]


@dataclass(frozen=True, slots=True)
class OwnershipRefreshResult:
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    catalog_updated: bool
    started_at: int
    finished_at: int
    stopped_early: bool = False
    stop_reason: str = ""


@dataclass(frozen=True, slots=True)
class _RefreshRoleOutcome:
    status: Literal["success", "failed", "skipped"]
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class OwnershipStatsReport:
    scope: Literal["global", "group"]
    generated_at: int
    catalog_version: str
    segments: tuple[OwnershipStatsSegment, ...]
    refresh: OwnershipRefreshResult | None = None
    # 有效样本中最新一次完整快照的抓取时间;无有效样本时为 None。
    snapshot_updated_at: int | None = None

    def segment(self, region: str) -> OwnershipStatsSegment | None:
        return next((item for item in self.segments if item.region == region), None)


OwnershipStatsRenderer = Callable[[OwnershipStatsReport], Awaitable[Any] | Any]
_ownership_stats_renderer: OwnershipStatsRenderer | None = None


def register_ownership_stats_renderer(renderer: OwnershipStatsRenderer | None) -> None:
    """Register the presentation adapter without coupling statistics to a UI."""
    global _ownership_stats_renderer
    _ownership_stats_renderer = renderer


async def render_ownership_stats(report: OwnershipStatsReport) -> Any:
    if _ownership_stats_renderer is None:
        raise OwnershipStatsRendererUnavailable("持有率展示组件尚未接入")
    result = _ownership_stats_renderer(report)
    return await result if inspect.isawaitable(result) else result


async def fetch_operator_catalog() -> tuple[str, tuple[OperatorCatalogEntry, ...]]:
    manifest = await fetch_akedata_manifest()
    latest = str(manifest.get("latest") or "")
    version = next(
        (
            item
            for item in manifest.get("versions") or ()
            if isinstance(item, Mapping) and str(item.get("id") or "") == latest
        ),
        None,
    )
    table_path = str((version or {}).get("tableCfgPath") or "").strip("/")
    if not latest or not table_path:
        raise RuntimeError("AKEData 当前版本缺少干员目录路径")
    character_table, profession_table, i18n = await asyncio.gather(
        _get(f"/{table_path}/CharacterTable.json", max_bytes=_TABLE_MAX_BYTES),
        _get(f"/{table_path}/CharProfessionTable.json", max_bytes=4 * 1024 * 1024),
        _get(f"/{table_path}/I18nTextTable_CN.json", max_bytes=_I18N_MAX_BYTES),
    )
    return latest, build_operator_catalog(
        character_table,
        profession_table,
        i18n,
        version=latest,
    )


def build_operator_catalog(
    character_table: Any,
    profession_table: Any,
    i18n: Any,
    *,
    version: str = "",
) -> tuple[OperatorCatalogEntry, ...]:
    translations = i18n if isinstance(i18n, Mapping) else {}
    professions: dict[str, str] = {}
    for key, row in _rows(profession_table):
        profession_id = _text(row.get("profession")) or key
        professions[profession_id] = localized_text(row.get("name"), translations=translations) or "未知职业"

    entries: list[OperatorCatalogEntry] = []
    for key, row in _rows(character_table):
        source_id = _text(row.get("charId")) or key
        if not source_id:
            continue
        # AKEData also publishes chr_9000_endmin, but the account API uses its
        # MD5 as a shared placeholder for either selected Endministrator sex.
        # Gender, not this alias, decides which of the two operators is owned.
        if source_id == _ENDMIN_ACCOUNT_ALIAS_SOURCE:
            continue
        name = localized_text(row.get("name"), translations=translations) or source_id
        if source_id in _ENDMIN_DISPLAY_NAMES:
            name = _ENDMIN_DISPLAY_NAMES[source_id]
        entries.append(
            OperatorCatalogEntry(
                operator_key=hashlib.md5(source_id.encode("utf-8")).hexdigest(),
                source_id=source_id,
                name=name,
                rarity=max(0, _int(row.get("rarity"))),
                profession=professions.get(_text(row.get("profession")), "未知职业"),
                sort_order=_int(row.get("sortOrder")),
                source="akedata",
                version=version,
                available_cn=True,
                available_asia=True,
            )
        )
    if not entries:
        raise ValueError("AKEData 未返回可用干员")
    return tuple(entries)


def parse_operator_snapshot(
    detail: Mapping[str, Any],
    catalog: Sequence[OperatorCatalogEntry] = (),
) -> tuple[tuple[OperatorSnapshotMember, ...], int]:
    raw_characters = detail.get("chars")
    if not isinstance(raw_characters, (list, tuple)):
        raise SnapshotValidationError("官方档案缺少干员列表")
    by_key = {item.operator_key.casefold(): item for item in catalog}
    by_source = {item.source_id.casefold(): item for item in catalog if item.source_id}
    for item in catalog:
        if item.source_id:
            by_key.setdefault(hashlib.md5(item.source_id.encode("utf-8")).hexdigest(), item)
    base_gender = _mapping(detail.get("base")).get("gender")
    parsed: dict[str, OperatorSnapshotMember] = {}
    for raw_character in raw_characters:
        if not isinstance(raw_character, Mapping):
            continue
        char_data = _mapping(raw_character.get("charData"))
        raw_id = _text(char_data.get("id") or raw_character.get("charId") or raw_character.get("id"))
        if not raw_id:
            continue
        lowered = raw_id.casefold()
        endministrator_source = _endministrator_source(
            lowered,
            raw_character.get("gender"),
            char_data.get("gender"),
            base_gender,
        )
        catalog_entry = (
            by_source.get(endministrator_source)
            if endministrator_source
            else by_key.get(lowered) or by_source.get(lowered)
        )
        if endministrator_source:
            operator_key = hashlib.md5(endministrator_source.encode("utf-8")).hexdigest()
            source_id = endministrator_source
        elif catalog_entry is not None:
            operator_key = catalog_entry.operator_key
            source_id = catalog_entry.source_id
        elif _HEX_ID_RE.fullmatch(raw_id):
            operator_key = lowered
            source_id = ""
        else:
            source_id = raw_id
            operator_key = hashlib.md5(raw_id.encode("utf-8")).hexdigest()
        potential = _optional_int(raw_character.get("potentialLevel"))
        if potential is not None and potential not in range(0, 6):
            potential = None
        member = OperatorSnapshotMember(
            operator_key=operator_key,
            potential_level=potential,
            source_id=source_id,
            name=(
                _ENDMIN_DISPLAY_NAMES.get(source_id)
                or (catalog_entry.name if catalog_entry else localized_text(char_data.get("name")))
                or "未知干员"
            ),
            rarity=(catalog_entry.rarity if catalog_entry else _int(_semantic_value(char_data.get("rarity")))),
            profession=(catalog_entry.profession if catalog_entry else semantic_label(char_data.get("profession")))
            or "未知职业",
        )
        previous = parsed.get(operator_key)
        if previous is None or _potential_rank(member.potential_level) > _potential_rank(previous.potential_level):
            parsed[operator_key] = member

    if not parsed:
        raise SnapshotValidationError("官方档案未返回有效干员")
    expected = _int(_mapping(detail.get("base")).get("charNum"))
    if expected > 0 and expected != len(parsed):
        raise SnapshotValidationError(f"干员列表不完整：预期 {expected}，实际 {len(parsed)}")
    game_saved_at = _int(_mapping(detail.get("base")).get("saveTime"))
    return tuple(parsed.values()), max(0, game_saved_at)


class OwnershipStatsService:
    def __init__(
        self,
        store: EndfieldStore,
        client: EndfieldOfficialClient,
        *,
        snapshot_ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
        refresh_interval_seconds: int = REFRESH_INTERVAL_SECONDS,
        concurrency: int = REFRESH_CONCURRENCY,
        systemic_failure_threshold: int = SYSTEMIC_FAILURE_THRESHOLD,
        systemic_backoff_base_seconds: float = SYSTEMIC_BACKOFF_BASE_SECONDS,
    ):
        self.store = store
        self.client = client
        self.snapshot_ttl_seconds = max(60, int(snapshot_ttl_seconds))
        self.refresh_interval_seconds = max(60, int(refresh_interval_seconds))
        self.concurrency = max(1, int(concurrency))
        self.systemic_failure_threshold = max(1, int(systemic_failure_threshold))
        self.systemic_backoff_base_seconds = max(0.0, float(systemic_backoff_base_seconds))
        self._catalog_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    async def refresh_catalog(self) -> bool:
        async with self._catalog_lock:
            version, entries = await fetch_operator_catalog()
            current = self.store.list_operator_catalog()
            current_version = next((item.version for item in current if item.source == "akedata"), "")
            current_signature = {
                _catalog_entry_signature(item)
                for item in current
                if item.source == "akedata"
            }
            incoming_signature = {_catalog_entry_signature(item) for item in entries}
            if current_version == version and current_signature == incoming_signature:
                return False
            self.store.replace_operator_catalog(entries, version)
            return True

    async def persist_detail(
        self,
        role: EndfieldRole,
        detail: Mapping[str, Any],
        *,
        fetched_at: int | None = None,
    ) -> int:
        members, game_saved_at = parse_operator_snapshot(detail, self.store.list_operator_catalog())
        return self.store.replace_operator_snapshot(
            role,
            "asia" if is_asia_role(role) else "cn",
            members,
            fetched_at=fetched_at,
            game_saved_at=game_saved_at,
        )

    async def refresh_due(self, cipher: CredentialCipher, *, now: int | None = None) -> OwnershipRefreshResult:
        return await self.refresh_roles(self.store.list_all_roles(), cipher, now=now, force=False)

    async def refresh_roles(
        self,
        roles: Sequence[EndfieldRole],
        cipher: CredentialCipher,
        *,
        now: int | None = None,
        force: bool = True,
    ) -> OwnershipRefreshResult:
        started = int(now or time.time())
        try:
            catalog_updated = await self.refresh_catalog()
        except Exception:
            catalog_updated = False
        catalog = self.store.list_operator_catalog()
        groups = _group_roles(roles)
        if not force:
            snapshots = {
                (item.server_id, item.role_id): item
                for item in self.store.list_operator_snapshots()
            }
            due_before = started - self.refresh_interval_seconds
            groups = {
                key: candidates
                for key, candidates in groups.items()
                if (
                    key not in snapshots
                    or snapshots[key].fetched_at < due_before
                    or _has_legacy_shared_endministrator(snapshots[key])
                )
            }
        semaphore = asyncio.Semaphore(self.concurrency)
        state_lock = asyncio.Lock()
        stop_event = asyncio.Event()
        systemic_failures = 0
        backoff_until = 0.0
        stop_reason = ""

        async def run(candidates: tuple[EndfieldRole, ...]) -> _RefreshRoleOutcome:
            nonlocal systemic_failures, backoff_until, stop_reason
            if stop_event.is_set():
                return _RefreshRoleOutcome("skipped")
            async with semaphore:
                if stop_event.is_set():
                    return _RefreshRoleOutcome("skipped")
                async with state_lock:
                    delay = max(0.0, backoff_until - time.monotonic())
                if delay:
                    await asyncio.sleep(delay)
                if stop_event.is_set():
                    return _RefreshRoleOutcome("skipped")

                outcome = await self._refresh_one(candidates, cipher, catalog, started)
                async with state_lock:
                    systemic_code = _systemic_community_error_code(outcome.error)
                    if outcome.status == "success":
                        systemic_failures = 0
                    elif systemic_code:
                        systemic_failures += 1
                        if systemic_failures >= self.systemic_failure_threshold:
                            stop_reason = (
                                f"官方社区接口连续返回 {systemic_code}，"
                                "已保护性停止剩余刷新"
                            )
                            stop_event.set()
                        else:
                            backoff_seconds = self.systemic_backoff_base_seconds * (
                                2 ** (systemic_failures - 1)
                            )
                            backoff_until = max(
                                backoff_until,
                                time.monotonic() + backoff_seconds,
                            )
                    elif outcome.status == "failed":
                        systemic_failures = 0
                return outcome

        async with self._refresh_lock:
            results = await asyncio.gather(*(run(candidates) for candidates in groups.values()))
        self.store.cleanup_orphan_operator_snapshots()
        return OwnershipRefreshResult(
            attempted=len(results),
            succeeded=sum(item.status == "success" for item in results),
            failed=sum(item.status == "failed" for item in results),
            skipped=sum(item.status == "skipped" for item in results),
            catalog_updated=catalog_updated,
            started_at=started,
            finished_at=int(time.time()),
            stopped_early=stop_event.is_set(),
            stop_reason=stop_reason,
        )

    async def _refresh_one(
        self,
        candidates: tuple[EndfieldRole, ...],
        cipher: CredentialCipher,
        catalog: Sequence[OperatorCatalogEntry],
        attempted_at: int,
    ) -> _RefreshRoleOutcome:
        reference = candidates[0]
        region = "asia" if any(is_asia_role(role) for role in candidates) else "cn"
        last_error: Exception | None = None
        try:
            async with ROLE_TASKS.claim(reference):
                for index, role in enumerate(candidates):
                    try:
                        token = self.store.decrypt_token(role, cipher)
                        detail = await self.client.card_detail(token, role)
                        members, game_saved_at = parse_operator_snapshot(detail, catalog)
                        self.store.replace_operator_snapshot(
                            reference,
                            region,
                            members,
                            fetched_at=attempted_at,
                            game_saved_at=game_saved_at,
                        )
                        return _RefreshRoleOutcome("success")
                    except (CredentialKeyError, LookupError) as exc:
                        last_error = exc
                        continue
                    except EndfieldAPIError as exc:
                        last_error = exc
                        if (
                            index + 1 < len(candidates)
                            and _is_authentication_error(exc)
                            and not _systemic_community_error_code(exc)
                        ):
                            continue
                        break
                    except Exception as exc:
                        last_error = exc
                        break
        except TaskAlreadyRunning:
            return _RefreshRoleOutcome("skipped")
        self.store.record_operator_snapshot_failure(
            reference,
            region,
            _safe_refresh_error(last_error),
            attempted_at=attempted_at,
        )
        return _RefreshRoleOutcome("failed", last_error)

    def build_report(
        self,
        scope: Literal["global", "group"],
        roles: Sequence[EndfieldRole],
        *,
        now: int | None = None,
        refresh: OwnershipRefreshResult | None = None,
    ) -> OwnershipStatsReport:
        current = int(now or time.time())
        grouped = _group_roles(roles)
        regions = {
            key: ("asia" if any(is_asia_role(role) for role in candidates) else "cn")
            for key, candidates in grouped.items()
        }
        snapshots = {
            (item.server_id, item.role_id): item
            for item in self.store.list_operator_snapshots()
        }
        fresh_after = current - self.snapshot_ttl_seconds
        valid = {
            key: snapshot
            for key, snapshot in snapshots.items()
            if key in grouped
            and snapshot.fetched_at >= fresh_after
            and snapshot.members
            and snapshot.operator_count == len(snapshot.members)
            and not _has_legacy_shared_endministrator(snapshot)
        }
        catalog = self.store.list_operator_catalog()
        catalog_version = next((item.version for item in catalog if item.source == "akedata"), "")
        all_keys = set(grouped)
        cn_keys = {key for key, region in regions.items() if region == "cn"}
        asia_keys = {key for key, region in regions.items() if region == "asia"}
        segments = (
            _build_segment("all", all_keys, valid, catalog),
            _build_segment("cn", cn_keys, valid, catalog),
            _build_segment("asia", asia_keys, valid, catalog),
        )
        snapshot_updated_at = max(
            (snapshot.fetched_at for snapshot in valid.values()), default=None
        )
        return OwnershipStatsReport(
            scope=scope,
            generated_at=current,
            catalog_version=catalog_version,
            segments=segments,
            refresh=refresh,
            snapshot_updated_at=snapshot_updated_at,
        )


def with_refresh(report: OwnershipStatsReport, refresh: OwnershipRefreshResult) -> OwnershipStatsReport:
    return replace(report, refresh=refresh)


def _build_segment(
    region: Literal["all", "cn", "asia"],
    eligible_keys: set[tuple[str, str]],
    valid: Mapping[tuple[str, str], OperatorRosterSnapshot],
    catalog: Sequence[OperatorCatalogEntry],
) -> OwnershipStatsSegment:
    snapshots = [valid[key] for key in eligible_keys if key in valid]
    sample_count = len(snapshots)
    if region == "cn":
        segment_catalog = [item for item in catalog if item.available_cn]
    elif region == "asia":
        segment_catalog = [item for item in catalog if item.available_asia]
    else:
        segment_catalog = [item for item in catalog if item.available_cn or item.available_asia]
    segment_catalog = [
        item
        for item in segment_catalog
        if item.operator_key.casefold() != _ENDMIN_ACCOUNT_ALIAS_KEY
    ]

    member_maps = [
        {member.operator_key: member for member in snapshot.members}
        for snapshot in snapshots
    ]
    operators: list[OperatorOwnership] = []
    for entry in segment_catalog:
        potential_counts = {f"potential_{level}": 0 for level in range(6)}
        unknown = 0
        owned = 0
        for members in member_maps:
            member = members.get(entry.operator_key)
            if member is None:
                continue
            owned += 1
            if member.potential_level in range(0, 6):
                potential_counts[f"potential_{member.potential_level}"] += 1
            else:
                unknown += 1
        bucket_counts = {
            "unowned": sample_count - owned,
            **potential_counts,
            "unknown": unknown,
        }
        operators.append(
            OperatorOwnership(
                operator_key=entry.operator_key,
                source_id=entry.source_id,
                name=entry.name,
                rarity=entry.rarity,
                profession=entry.profession,
                sort_order=entry.sort_order,
                owned_count=owned,
                sample_count=sample_count,
                ownership_rate=_rate(owned, sample_count),
                potential_buckets=tuple(
                    PotentialBucket(key, _potential_bucket_label(key), count, _rate(count, sample_count))
                    for key, count in bucket_counts.items()
                ),
            )
        )
    operators.sort(
        key=lambda item: (
            -item.rarity,
            -item.owned_count,
            item.sort_order,
            item.operator_key,
        )
    )
    professions = _collection_summaries("profession", operators, sample_count)
    rarities = _collection_summaries("rarity", operators, sample_count)
    return OwnershipStatsSegment(
        region=region,
        eligible_sample_count=len(eligible_keys),
        valid_sample_count=sample_count,
        excluded_sample_count=len(eligible_keys) - sample_count,
        operators=tuple(operators),
        professions=professions,
        rarities=rarities,
    )


def _collection_summaries(
    kind: Literal["profession", "rarity"],
    operators: Sequence[OperatorOwnership],
    sample_count: int,
) -> tuple[CollectionSummary, ...]:
    groups: dict[str, list[OperatorOwnership]] = defaultdict(list)
    for operator in operators:
        label = operator.profession if kind == "profession" else str(operator.rarity)
        groups[label].append(operator)
    summaries = [
        CollectionSummary(
            kind=kind,
            label=label,
            operator_count=len(items),
            owned_slots=sum(item.owned_count for item in items),
            possible_slots=sample_count * len(items),
            collection_rate=_rate(sum(item.owned_count for item in items), sample_count * len(items)),
        )
        for label, items in groups.items()
    ]
    summaries.sort(
        key=(
            (lambda item: (-_int(item.label), item.label))
            if kind == "rarity"
            else (lambda item: item.label)
        )
    )
    return tuple(summaries)


async def collect_group_member_ids(bot: Any, guild_id: str) -> set[str]:
    getter = getattr(bot, "guild_member_list", None)
    if not guild_id:
        raise GroupMemberListError("当前会话缺少群号")
    standard_error: Exception | None = None
    if callable(getter):
        try:
            members = await _standard_group_members(getter, str(guild_id))
        except Exception as exc:
            standard_error = exc
        else:
            return _group_member_ids(members)

    try:
        result = await call_onebot_action(
            bot,
            "get_group_member_list",
            group_id=_numeric_id(guild_id),
            no_cache=True,
        )
        members = _onebot_group_members(result)
    except Exception as exc:
        message = (
            "获取当前群成员列表失败"
            if standard_error is not None
            else "当前适配器不支持获取群成员列表"
        )
        raise GroupMemberListError(
            message,
            standard_error=standard_error,
            fallback_error=exc,
        ) from exc
    return _group_member_ids(members)


async def _standard_group_members(getter: Callable[..., Any], guild_id: str) -> list[Any]:
    result = getter(guild_id=guild_id)
    # Satori IterablePageResult is both Awaitable and AsyncIterable. Iterating
    # it is what follows every `next` token; awaiting it returns only one page.
    if hasattr(result, "__aiter__"):
        members: list[Any] = []
        async for member in result:
            members.append(member)
        return members
    if inspect.isawaitable(result):
        result = await result
    data = getattr(result, "data", None)
    if data is not None:
        result = data
    elif isinstance(result, Mapping) and "data" in result:
        result = result["data"]
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
        return list(result)
    raise TypeError("群成员接口返回格式异常")


def _onebot_group_members(result: Any) -> list[Any]:
    data = getattr(result, "data", None)
    if data is not None:
        result = data
    elif isinstance(result, Mapping) and "data" in result:
        result = result["data"]
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
        return list(result)
    raise TypeError("OneBot 群成员接口返回格式异常")


def _group_member_ids(members: Iterable[Any]) -> set[str]:
    user_ids = {_member_user_id(member) for member in members}
    user_ids.discard("")
    return user_ids


def _numeric_id(value: Any) -> int | str:
    text = str(value or "")
    return int(text) if text.isdecimal() else text


GROUP_ADMIN_ROLE_TOKENS = {
    "admin",
    "administrator",
    "owner",
    "manager",
    "群主",
    "管理员",
}


def member_has_group_admin_role(member: Any) -> bool:
    if member is None:
        return False
    for attribute in ("is_owner", "is_admin", "is_administrator", "owner", "admin"):
        value = _field(member, attribute)
        if value is True or (
            isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}
        ):
            return True
    roles = _field(member, "roles") or ()
    if isinstance(roles, (str, bytes, Mapping)):
        roles = (roles,)
    for role in roles:
        values = role.values() if isinstance(role, Mapping) else (
            getattr(role, "id", ""),
            getattr(role, "name", ""),
        )
        for value in values:
            lowered = str(value or "").strip().casefold()
            if lowered and any(token in lowered for token in GROUP_ADMIN_ROLE_TOKENS):
                return True
    return False


async def is_group_manager(bot: Any, event: Any, guild_id: str, user_id: str) -> bool:
    member = getattr(event, "member", None)
    if member_has_group_admin_role(member):
        return True
    getter = getattr(bot, "guild_member_get", None)
    if not callable(getter) or not guild_id:
        return False
    try:
        member = getter(guild_id=str(guild_id), user_id=str(user_id))
        if inspect.isawaitable(member):
            member = await member
    except Exception:
        return False
    return member_has_group_admin_role(member)


def _group_roles(roles: Sequence[EndfieldRole]) -> dict[tuple[str, str], tuple[EndfieldRole, ...]]:
    grouped: dict[tuple[str, str], list[EndfieldRole]] = defaultdict(list)
    for role in roles:
        grouped[(str(role.server_id), str(role.role_id))].append(role)
    return {key: tuple(items) for key, items in grouped.items()}


def _catalog_entry_signature(entry: OperatorCatalogEntry) -> tuple[Any, ...]:
    return (
        entry.operator_key,
        entry.source_id,
        entry.name,
        entry.rarity,
        entry.profession,
        entry.sort_order,
        entry.available_cn,
        entry.available_asia,
    )


def _endministrator_source(raw_id: str, *gender_values: Any) -> str:
    normalized_id = str(raw_id or "").strip().casefold()
    if normalized_id in {_ENDMIN_MALE_SOURCE, _ENDMIN_MALE_KEY}:
        return _ENDMIN_MALE_SOURCE
    if normalized_id in {_ENDMIN_FEMALE_SOURCE, _ENDMIN_FEMALE_KEY}:
        return _ENDMIN_FEMALE_SOURCE
    if normalized_id not in {_ENDMIN_ACCOUNT_ALIAS_SOURCE, _ENDMIN_ACCOUNT_ALIAS_KEY}:
        return ""
    for value in gender_values:
        source_id = _endministrator_source_from_gender(value)
        if source_id:
            return source_id
    raise SnapshotValidationError("官方档案缺少管理员性别，无法生成准确快照")


def _endministrator_source_from_gender(value: Any) -> str:
    normalized = _text(_semantic_value(value)).casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"1", "m", "male", "char_gender_male"}:
        return _ENDMIN_MALE_SOURCE
    if normalized in {"2", "f", "female", "char_gender_female"}:
        return _ENDMIN_FEMALE_SOURCE
    return ""


def _has_legacy_shared_endministrator(snapshot: OperatorRosterSnapshot) -> bool:
    return any(
        member.operator_key.casefold() == _ENDMIN_ACCOUNT_ALIAS_KEY
        for member in snapshot.members
    )


def _member_user_id(member: Any) -> str:
    user = _field(member, "user")
    return str(_field(user, "id") or _field(member, "user_id") or _field(member, "id") or "")


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _is_authentication_error(error: EndfieldAPIError) -> bool:
    if error.operation in {"账号授权", "获取社区凭据", "刷新社区签名"}:
        return True
    return str(error.code).casefold() in {"401", "403", "10001", "10002", "10003", "10004"}


def _systemic_community_error_code(error: Exception | None) -> str:
    if not isinstance(error, EndfieldAPIError):
        return ""
    code = str(error.code)
    if error.operation not in _SYSTEMIC_COMMUNITY_OPERATIONS:
        return ""
    return code if code in _SYSTEMIC_COMMUNITY_CODES else ""


def _safe_refresh_error(error: Exception | None) -> str:
    if error is None:
        return "刷新失败"
    if isinstance(error, CredentialKeyError):
        return "账号凭据无法解密"
    if isinstance(error, LookupError):
        return "账号凭据不存在"
    if isinstance(error, SnapshotValidationError):
        return str(error)
    if isinstance(error, EndfieldAPIError):
        return str(error)
    return f"{type(error).__name__}: 刷新失败"


def _rows(value: Any) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple((str(key), row) for key, row in value.items() if isinstance(row, Mapping))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def _semantic_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value") if value.get("value") is not None else value.get("key")
    return value


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+", str(value or ""))
        return int(match.group()) if match else 0


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _potential_rank(value: int | None) -> int:
    return value if value is not None else -1


def _potential_bucket_label(key: str) -> str:
    if key == "unowned":
        return "未持有"
    if key == "unknown":
        return "未知"
    return f"潜能 {key.removeprefix('potential_')}"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None
