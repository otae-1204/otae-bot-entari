"""群内关卡成员使用率:抓取落库、聚合与刷新批次。

报告只描述"最佳记录阵容",不含逐次尝试;所有身份信息止步于
challenge_* 三张表,本模块对外只产出 UsageStageReport / UsageOverviewReport。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from loguru import logger

from ..account.challenge.models import (
    MonumentPayload,
    WarEchoPayload,
)
from ..account.challenge.i18n import get_challenge_locale
from ..account.challenge.parsing import parse_monument, parse_war_echoes
from ..account.client import EndfieldAPIError, EndfieldOfficialClient, is_asia_role
from ..account.crypto import CredentialCipher, CredentialKeyError
from ..account.store import (
    ChallengeMemberRow,
    ChallengeRecordMember,
    ChallengeRecordRow,
    EndfieldRole,
    EndfieldStore,
    OperatorCatalogEntry,
)
from ..gacha.service import ROLE_TASKS, TaskAlreadyRunning
from .models import (
    FAMILY_MONUMENT,
    FAMILY_WAR_ECHO,
    UsageHistoryReport,
    UsageOperator,
    UsageOverviewReport,
    UsageStageKey,
    UsageStageReport,
    UsageStageSummary,
    UsageTeam,
    UsageTeamMember,
)

SNAPSHOT_TTL_SECONDS = 48 * 3600
REFRESH_INTERVAL_SECONDS = 24 * 3600
REFRESH_CONCURRENCY = 2
REFRESH_BATCH_SIZE = 20
REFRESH_RETRY_BASE_SECONDS = 15 * 60
REFRESH_RETRY_MAX_SECONDS = 6 * 3600
AUTH_RETRY_BASE_SECONDS = 6 * 3600
AUTH_RETRY_MAX_SECONDS = 24 * 3600
SYSTEMIC_FAILURE_THRESHOLD = 3
SYSTEMIC_FAILURE_WINDOW = 10
SYSTEMIC_CIRCUIT_SECONDS = 15 * 60
MIN_SAMPLE_COUNT = 3
TEAM_LIMIT = 5
OPERATOR_LIMIT = 8

# 管理员男女共用一个 charId,统计时合并成一条。
ENDMIN_CHAR_ID = "chr_9000_endmin"
ENDMIN_OPERATOR_KEY = "endmin-shared"
ENDMIN_DIGEST = hashlib.md5(ENDMIN_CHAR_ID.encode("utf-8")).hexdigest()
ENDMIN_LABEL = "管理员（未区分性别）"
UNKNOWN_OPERATOR_LABEL = "未知干员"

AKEDATA_SPRITES_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)

DEFAULT_DIFFICULTY = {FAMILY_MONUMENT: "hard", FAMILY_WAR_ECHO: "cruel"}
FAMILY_DIFFICULTIES = {
    FAMILY_MONUMENT: ("normal", "hard"),
    FAMILY_WAR_ECHO: ("normal", "hard", "cruel"),
}


class UsageResolutionError(ValueError):
    """用户给的主题/关卡名无法唯一确定。"""


class UsageUnavailableError(RuntimeError):
    """统计范围内还没有可用数据。"""


@dataclass(frozen=True, slots=True)
class UsageRefreshIssue:
    key: str
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class UsageRefreshResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    requested: int = 0
    eligible: int = 0
    deferred: int = 0
    started_at: int = 0
    finished_at: int = 0
    stopped_early: bool = False
    stop_reason: str = ""
    issues: tuple[UsageRefreshIssue, ...] = ()

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int(self.finished_at) - int(self.started_at))


@dataclass(slots=True)
class _RoleOutcome:
    status: str
    requested: int = 0
    issue_key: str = ""
    issue_label: str = ""


@dataclass(frozen=True, slots=True)
class _RecordAgg:
    activity_id: str
    activity_name: str
    scope_id: str
    scope_name: str
    stage_id: str
    stage_name: str
    difficulty: str
    activity_start_ts: int
    activity_end_ts: int
    scope_start_ts: int
    scope_end_ts: int
    record_ts: int
    pass_time: int
    role_keys: frozenset[tuple[str, str]]
    members: tuple[tuple[int, str], ...]


def operator_key_for(
    raw_id: str,
    catalog_by_key: Mapping[str, OperatorCatalogEntry],
    catalog_by_source: Mapping[str, OperatorCatalogEntry],
) -> str:
    """把官方 charId 归一成 operator_key(见设计 §6.1)。"""
    value = str(raw_id or "").strip()
    if not value:
        return ""
    lowered = value.casefold()
    # 管理员男女共用一个 charId:原文形态和
    # md5 形态都要并到合成键,持有率那边的历史数据两种都出现过。
    if lowered in {ENDMIN_CHAR_ID, ENDMIN_DIGEST}:
        return ENDMIN_OPERATOR_KEY
    if lowered in catalog_by_key:
        return lowered
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()
    if digest == ENDMIN_DIGEST:
        return ENDMIN_OPERATOR_KEY
    if digest in catalog_by_key:
        return digest
    if lowered in catalog_by_source:
        return catalog_by_source[lowered].operator_key
    return digest


def operator_avatar_url(source_id: str) -> str:
    value = str(source_id or "").strip()
    if not value:
        return ""
    return f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_{value}.png"


def build_challenge_records(
    family: str,
    payload: MonumentPayload | WarEchoPayload,
    catalog: Sequence[OperatorCatalogEntry],
) -> tuple[ChallengeRecordRow, ...]:
    """把解析后的挑战响应摊平成待落库的行。"""
    by_key = {entry.operator_key: entry for entry in catalog}
    by_source = {entry.source_id.casefold(): entry for entry in catalog if entry.source_id}
    rows: list[ChallengeRecordRow] = []

    def member_rows(record) -> tuple[ChallengeRecordMember, ...]:
        output = []
        for index, member in enumerate(record.members):
            key = operator_key_for(member.char_id, by_key, by_source)
            if key:
                output.append(
                    ChallengeRecordMember(
                        slot=index,
                        operator_key=key,
                        level=int(member.level or 0),
                        potential=int(member.potential or 0),
                    )
                )
        return tuple(output)

    if family == FAMILY_MONUMENT and isinstance(payload, MonumentPayload):
        for group in payload.groups:
            activity_name = str(group.activity_name or group.name or "")
            for pair in group.stages:
                for dungeon in pair:
                    if dungeon is None:
                        continue
                    rows.append(
                        ChallengeRecordRow(
                            activity_id=str(group.id or ""),
                            activity_name=activity_name,
                            scope_id="",
                            scope_name="",
                            stage_id=str(dungeon.id or ""),
                            stage_name=str(dungeon.name or ""),
                            difficulty=str(dungeon.difficulty or ""),
                            activity_start_ts=int(group.start_ts or 0),
                            activity_end_ts=int(group.end_ts or 0),
                            scope_start_ts=0,
                            scope_end_ts=0,
                            passed=bool(dungeon.passed or dungeon.record.members),
                            record_ts=int(dungeon.record.record_ts or 0),
                            pass_time=int(dungeon.record.pass_time or 0),
                            first_pass_ts=int(dungeon.record.first_pass_ts or 0),
                            members=member_rows(dungeon.record),
                        )
                    )
        return tuple(rows)

    if family == FAMILY_WAR_ECHO and isinstance(payload, WarEchoPayload):
        for season in payload.seasons:
            for week in season.weeks:
                for group in week.groups:
                    for difficulty in ("normal", "hard", "cruel"):
                        dungeon = group.dungeon(difficulty)
                        if dungeon is None:
                            continue
                        rows.append(
                            ChallengeRecordRow(
                                activity_id=str(season.id or ""),
                                activity_name=str(season.name or ""),
                                scope_id=str(week.id or ""),
                                scope_name=str(week.name or ""),
                                stage_id=str(dungeon.id or ""),
                                stage_name=str(dungeon.name or ""),
                                difficulty=str(dungeon.difficulty or difficulty),
                                activity_start_ts=int(season.start_ts or 0),
                                activity_end_ts=int(season.end_ts or 0),
                                scope_start_ts=int(week.start_ts or 0),
                                scope_end_ts=int(week.end_ts or 0),
                                passed=bool(dungeon.passed or dungeon.record.members),
                                record_ts=int(dungeon.record.record_ts or 0),
                                pass_time=int(dungeon.record.pass_time or 0),
                                first_pass_ts=int(dungeon.record.first_pass_ts or 0),
                                members=member_rows(dungeon.record),
                            )
                        )
    return tuple(rows)

class UsageStatsService:
    """挑战记录快照的抓取、落库与群内聚合。"""

    def __init__(
        self,
        store: EndfieldStore,
        client: EndfieldOfficialClient,
        *,
        snapshot_ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
        refresh_interval_seconds: int = REFRESH_INTERVAL_SECONDS,
        concurrency: int = REFRESH_CONCURRENCY,
        batch_size: int = REFRESH_BATCH_SIZE,
        retry_base_seconds: int = REFRESH_RETRY_BASE_SECONDS,
        retry_max_seconds: int = REFRESH_RETRY_MAX_SECONDS,
        systemic_failure_threshold: int = SYSTEMIC_FAILURE_THRESHOLD,
        systemic_failure_window: int = SYSTEMIC_FAILURE_WINDOW,
        systemic_circuit_seconds: int = SYSTEMIC_CIRCUIT_SECONDS,
    ):
        self.store = store
        self.client = client
        self.snapshot_ttl_seconds = max(60, int(snapshot_ttl_seconds))
        self.refresh_interval_seconds = max(60, int(refresh_interval_seconds))
        self.concurrency = max(1, int(concurrency))
        self.batch_size = max(1, int(batch_size))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.systemic_failure_threshold = max(1, int(systemic_failure_threshold))
        self.systemic_failure_window = max(
            self.systemic_failure_threshold, int(systemic_failure_window)
        )
        self.systemic_circuit_seconds = max(60, int(systemic_circuit_seconds))
        self._refresh_lock = asyncio.Lock()
        self._auto_refresh_at = 0.0
        self._background: set[asyncio.Task[Any]] = set()
        self._circuit_open_until = 0.0
        self._circuit_reason = ""
        self._systemic_events: list[str] = []
        self._batch_sequence = 0

    # --- 落库 -------------------------------------------------------------

    async def persist_raw(
        self,
        role: EndfieldRole,
        family: str,
        raw: Mapping[str, Any],
        *,
        locale: Any = None,
        fetched_at: int | None = None,
    ) -> int:
        """把一次官方响应落库,返回写入的最佳记录条数。"""
        return await asyncio.to_thread(
            self._persist_raw, role, family, raw, locale, fetched_at
        )

    def _persist_raw(
        self,
        role: EndfieldRole,
        family: str,
        raw: Mapping[str, Any],
        locale: Any,
        fetched_at: int | None,
    ) -> int:
        payload: MonumentPayload | WarEchoPayload
        if family == FAMILY_MONUMENT:
            payload = parse_monument(raw, locale)
        else:
            payload = parse_war_echoes(raw, locale)
        rows = build_challenge_records(family, payload, self.store.list_operator_catalog())
        written = self.store.replace_challenge_snapshot(
            role.role_id,
            role.server_id,
            family,
            rows,
            fetched_at=fetched_at,
        )
        if not written:
            logger.debug(
                "[endfield-usage] stale snapshot discarded "
                f"family={family} records={len(rows)}"
            )
            return 0
        return len([item for item in rows if item.members])

    # --- 空库自愈 ---------------------------------------------------------

    #: 同一秒内被多人撞到空库时,别把刷新任务排成一串。
    AUTO_REFRESH_DEBOUNCE_SECONDS = 120.0

    def request_background_refresh(
        self, roles: Sequence[EndfieldRole], *, trigger: str = "auto-empty"
    ) -> bool:
        """查询发现没有记录时顺手起一次后台抓取。

        没有绑定、拿不到凭据或刚好刷过就返回 False,调用方改用纯提示文案。
        """
        roles = _challenge_roles(roles)
        if not roles:
            return False
        if time.monotonic() - self._auto_refresh_at < self.AUTO_REFRESH_DEBOUNCE_SECONDS:
            return False
        try:
            cipher = CredentialCipher.from_settings()
        except CredentialKeyError:
            return False
        self._auto_refresh_at = time.monotonic()
        task = asyncio.create_task(
            self.refresh_roles(
                roles, cipher, force=True, limit=self.batch_size, trigger=trigger
            )
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True

    # --- 刷新批次 ---------------------------------------------------------

    async def refresh_due(
        self,
        cipher: CredentialCipher,
        *,
        now: int | None = None,
        limit: int | None = None,
    ) -> UsageRefreshResult:
        return await self.refresh_roles(
            self.store.list_all_roles(),
            cipher,
            now=now,
            force=False,
            limit=self.batch_size if limit is None else limit,
            trigger="scheduled",
        )

    async def refresh_roles(
        self,
        roles: Sequence[EndfieldRole],
        cipher: CredentialCipher,
        *,
        now: int | None = None,
        force: bool = True,
        limit: int | None = None,
        trigger: str = "direct",
    ) -> UsageRefreshResult:
        started = int(now or time.time())
        roles = _challenge_roles(roles)
        self._batch_sequence += 1
        batch_id = f"{started:x}-{self._batch_sequence:x}"
        async with self._refresh_lock:
            tasks = _family_tasks(roles)
            snapshots = {
                (state.server_id, state.role_id, state.family): state
                for state in self.store.list_challenge_snapshots(
                    {key for key, _family in tasks}
                )
            }
            due_before = started - self.refresh_interval_seconds
            eligible: list[tuple[tuple[str, str], str]] = []
            retry_deferred = 0
            for key, family in tasks:
                state = snapshots.get((key[1], key[0], family))
                due = force or state is None or state.fetched_at < due_before
                if not due:
                    continue
                if not force and state is not None and state.next_attempt_at > started:
                    retry_deferred += 1
                    continue
                eligible.append((key, family))
            eligible.sort(
                key=lambda item: (
                    snapshots[(item[0][1], item[0][0], item[1])].fetched_at
                    if (item[0][1], item[0][0], item[1]) in snapshots
                    else -1,
                    item[0],
                    item[1],
                )
            )
            selected_limit = len(eligible) if limit is None else max(0, int(limit))
            selected = eligible[:selected_limit]
            deferred = retry_deferred + max(0, len(eligible) - len(selected))
            selection_log = logger.info if eligible or force else logger.debug
            selection_log(
                "[endfield-usage] refresh batch selected "
                f"batch_id={batch_id} trigger={trigger} force={str(force).lower()} "
                f"bindings={len(roles)} tasks={len(tasks)} eligible={len(eligible)} "
                f"queued={len(selected)} deferred={deferred} "
                f"limit={selected_limit} concurrency={self.concurrency}"
            )
            circuit_delay = self._circuit_open_until - time.monotonic()
            if selected and circuit_delay > 0:
                issue = UsageRefreshIssue(
                    "community-circuit-open", "官方社区接口冷却中", len(selected)
                )
                return UsageRefreshResult(
                    attempted=len(selected),
                    skipped=len(selected),
                    eligible=len(eligible),
                    deferred=deferred,
                    started_at=started,
                    finished_at=int(time.time()),
                    stopped_early=True,
                    stop_reason=self._circuit_reason or "官方社区接口处于调用冷却中",
                    issues=(issue,),
                )
            if not selected:
                return UsageRefreshResult(
                    eligible=len(eligible),
                    deferred=deferred,
                    started_at=started,
                    finished_at=int(time.time()),
                )
            locale = await _challenge_locale()
            outcomes, stopped_early, stop_reason = await self._run_workers(
                selected, roles, cipher, now=started, batch_id=batch_id, locale=locale
            )
            issue_counts: Counter[tuple[str, str]] = Counter(
                (item.issue_key, item.issue_label) for item in outcomes if item.issue_key
            )
            self.store.cleanup_orphan_challenge_snapshots()
            return UsageRefreshResult(
                attempted=len(selected),
                succeeded=sum(item.status == "success" for item in outcomes),
                failed=sum(item.status == "failed" for item in outcomes),
                skipped=sum(item.status == "skipped" for item in outcomes),
                requested=sum(item.requested for item in outcomes),
                eligible=len(eligible),
                deferred=deferred,
                started_at=started,
                finished_at=int(time.time()),
                stopped_early=stopped_early,
                stop_reason=stop_reason,
                issues=tuple(
                    UsageRefreshIssue(key, label, count)
                    for (key, label), count in sorted(
                        issue_counts.items(), key=lambda item: (-item[1], item[0][1])
                    )
                ),
            )

    async def _run_workers(
        self,
        selected: Sequence[tuple[tuple[str, str], str]],
        roles: Sequence[EndfieldRole],
        cipher: CredentialCipher,
        *,
        now: int,
        batch_id: str,
        locale: Any = None,
    ) -> tuple[list[_RoleOutcome], bool, str]:
        by_key = _roles_by_key(roles)
        queue: asyncio.Queue[tuple[tuple[str, str], str]] = asyncio.Queue()
        for item in selected:
            queue.put_nowait(item)
        outcomes: list[_RoleOutcome] = []
        stopped_early = False
        stop_reason = ""
        role_locks: dict[tuple[str, str], asyncio.Lock] = {}

        async def worker() -> None:
            nonlocal stopped_early, stop_reason
            while True:
                try:
                    key, family = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if self._circuit_open_until - time.monotonic() > 0:
                    outcomes.append(
                        _RoleOutcome(
                            "skipped",
                            issue_key="community-protective-stop",
                            issue_label="官方社区接口触发保护性熔断",
                        )
                    )
                    stopped_early = True
                    stop_reason = _STOP_REASON["community-protective-stop"]
                    continue
                role = by_key.get(key)
                if role is None:
                    outcomes.append(
                        _RoleOutcome(
                            "skipped", issue_key="role-missing", issue_label="角色已解绑"
                        )
                    )
                    continue
                lock = role_locks.setdefault(key, asyncio.Lock())
                async with lock:
                    outcome = await self._refresh_one(
                        role, family, cipher, now=now, batch_id=batch_id, locale=locale
                    )
                if outcome.issue_key == "community-circuit-open":
                    stopped_early = True
                    stop_reason = _STOP_REASON["community-circuit-open"]
                outcomes.append(outcome)

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self.concurrency, len(selected)))
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
        return outcomes, stopped_early, stop_reason

    async def _refresh_one(
        self,
        role: EndfieldRole,
        family: str,
        cipher: CredentialCipher,
        *,
        now: int,
        batch_id: str,
        locale: Any = None,
    ) -> _RoleOutcome:
        try:
            async with ROLE_TASKS.claim(role):
                token = self.store.decrypt_token(role, cipher)
                raw = await self._fetch(role, family, token)
                written = await self.persist_raw(
                    role, family, raw, locale=locale, fetched_at=now
                )
        except TaskAlreadyRunning:
            return _RoleOutcome(
                "skipped", issue_key="role-busy", issue_label="角色任务占用中"
            )
        except CredentialKeyError as exc:
            return await self._fail(role, family, now, "auth:凭据不可用", str(exc))
        except EndfieldAPIError as exc:
            return await self._fail(
                role, family, now, _api_issue_key(family, exc), _api_issue_label(exc)
            )
        except Exception as exc:  # pragma: no cover - 兜底,不把异常抛到批次外
            logger.warning(
                "[endfield-usage] refresh failed "
                f"batch_id={batch_id} family={family} "
                f"error_type={type(exc).__module__}.{type(exc).__name__}"
            )
            return await self._fail(role, family, now, "unknown:未分类", "接口返回异常")
        logger.debug(
            "[endfield-usage] refresh ok "
            f"batch_id={batch_id} family={family} records={written}"
        )
        return _RoleOutcome("success", requested=1)

    async def _fetch(
        self, role: EndfieldRole, family: str, token: str
    ) -> Mapping[str, Any]:
        if family == FAMILY_MONUMENT:
            return await self.client.indie_hard(token, role)
        return await self.client.war_echoes(token, role)

    async def _fail(
        self,
        role: EndfieldRole,
        family: str,
        now: int,
        issue_key: str,
        issue_label: str,
    ) -> _RoleOutcome:
        state = self.store.list_challenge_snapshots(
            [(role.role_id, role.server_id)], family=family
        )
        previous = state[0].failure_count if state else 0
        systemic = _systemic_code(issue_key)
        is_auth = issue_key.startswith("auth:")
        delay = _retry_delay(
            previous,
            self.retry_base_seconds,
            self.retry_max_seconds,
            base_seconds_override=AUTH_RETRY_BASE_SECONDS if is_auth else None,
            max_seconds_override=AUTH_RETRY_MAX_SECONDS if is_auth else None,
        )
        self.store.mark_challenge_failure(
            role.role_id,
            role.server_id,
            family,
            error=issue_key,
            attempted_at=now,
            next_attempt_at=now + int(delay),
        )
        opened = self._note_systemic(systemic) if systemic else False
        if opened:
            return _RoleOutcome(
                "failed",
                requested=1,
                issue_key="community-circuit-open",
                issue_label="官方社区接口冷却中",
            )
        return _RoleOutcome("failed", requested=1, issue_key=issue_key, issue_label=issue_label)

    def _note_systemic(self, code: str) -> bool:
        self._systemic_events.append(code)
        if len(self._systemic_events) > self.systemic_failure_window:
            del self._systemic_events[
                : len(self._systemic_events) - self.systemic_failure_window
            ]
        if len(self._systemic_events) < self.systemic_failure_threshold:
            return False
        tail = self._systemic_events[-self.systemic_failure_threshold :]
        if all(item == code for item in tail):
            self._circuit_open_until = time.monotonic() + self.systemic_circuit_seconds
            self._circuit_reason = (
                "官方社区接口连续返回 " + code + "，已冷却 "
                + str(self.systemic_circuit_seconds // 60) + " 分钟"
            )
            logger.warning(
                "[endfield-usage] circuit opened "
                f"reason={code} window={len(self._systemic_events)}"
            )
            return True
        return False

    # --- 聚合 -------------------------------------------------------------

    def build_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        *,
        terms: Sequence[str] = (),
        difficulty: str = "",
        now: int | None = None,
        locale: Any = None,
        subject_label: str = "",
    ) -> UsageStageReport | UsageOverviewReport:
        """按用户给的词决定出单关卡卡还是当期概览卡。

        词只点到主题/赛季（甚至什么都没给）时出概览;点到关卡才出单关卡卡。
        """
        now_ts = int(now or time.time())
        roles = _challenge_roles(roles)
        aggregates = _localized_rows(self._load_aggregates(roles, family), locale)
        if not aggregates:
            raise UsageUnavailableError("统计范围内还没有该玩法的挑战记录。")
        activity_id, scope_id, rest, target_difficulty = self._resolve_target(
            aggregates, family, terms, difficulty, now_ts
        )
        if not rest:
            return self._overview_report(
                scope,
                roles,
                family,
                aggregates,
                activity_id,
                scope_id,
                target_difficulty,
                subject_label=subject_label,
            )
        try:
            stage_id = self._pick_stage(
                aggregates, activity_id, scope_id, target_difficulty, rest
            )
        except UsageResolutionError:
            raise
        return self._stage_report(
            scope,
            roles,
            family,
            aggregates,
            activity_id,
            scope_id,
            target_difficulty,
            stage_id,
            subject_label=subject_label,
        )

    # --- 历史总使用率 -----------------------------------------------------

    def build_history_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        *,
        terms: Sequence[str] = (),
        difficulty: str = "",
        now: int | None = None,
        locale: Any = None,
        subject_label: str = "",
    ) -> UsageHistoryReport:
        """把全部期次的记录并成一份干员总使用率。

        terms 只用来筛期次(赛季/轮换名),不会把记录拆成一行一期。
        """
        now_ts = int(now or time.time())
        _ = now_ts  # 历史口径不看当前时间，保留参数便于复用
        roles = _challenge_roles(roles)
        aggregates = _localized_rows(self._load_aggregates(roles, family), locale)
        if not aggregates:
            raise UsageUnavailableError("统计范围内还没有该玩法的挑战记录。")
        target_difficulty = _normalize_difficulty(family, difficulty)
        filters = tuple(str(token).strip() for token in terms if str(token).strip())
        rows = [
            item
            for item in aggregates
            if item.difficulty == target_difficulty
            and (
                not filters
                or _history_matches(
                    filters, item.activity_name, item.scope_name, item.scope_id
                )
            )
        ]
        if not rows:
            if filters:
                raise UsageResolutionError("没有匹配到任何期次，试试期次名或轮换名。")
            raise UsageUnavailableError("该难度下还没有任何期次的记录。")

        catalog = self._catalog_maps()
        bound_roles = _bound_role_count(roles)
        periods = {(item.activity_id, item.scope_id) for item in rows}
        sample_roles = {key for item in rows for key in item.role_keys}
        single_subject = bool(subject_label)
        record_count = len(rows)
        sample_count = 1 if single_subject else record_count
        teams = _aggregate_teams(
            rows, catalog, sample_count=1 if single_subject else record_count
        )
        return UsageHistoryReport(
            family=family,
            scope=scope,
            difficulty=target_difficulty,
            bound_role_count=bound_roles if not single_subject else sample_count,
            sample_count=sample_count,
            recorded_role_count=1 if single_subject else len(sample_roles),
            period_count=len(periods),
            operators=_aggregate_operators(
                rows, catalog, sample_count=1 if single_subject else record_count
            ),
            teams=teams[: TEAM_LIMIT * 2],
            team_combo_total=len(teams),
            latest_record_ts=max((item.record_ts for item in rows), default=0),
            regions=_regions(roles),
            snapshot_updated_at=self._snapshot_updated_at(roles, family),
            subject_label=subject_label,
        )

    def build_stage_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        *,
        terms: Sequence[str] = (),
        difficulty: str = "",
        now: int | None = None,
        locale: Any = None,
    ) -> UsageStageReport:
        now_ts = int(now or time.time())
        roles = _challenge_roles(roles)
        aggregates = _localized_rows(self._load_aggregates(roles, family), locale)
        if not aggregates:
            raise UsageUnavailableError("统计范围内还没有该玩法的挑战记录。")
        activity_id, scope_id, rest, target_difficulty = self._resolve_target(
            aggregates, family, terms, difficulty, now_ts
        )
        stage_id = self._pick_stage(
            aggregates, activity_id, scope_id, target_difficulty, rest
        )
        return self._stage_report(
            scope,
            roles,
            family,
            aggregates,
            activity_id,
            scope_id,
            target_difficulty,
            stage_id,
        )

    def build_overview_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        *,
        terms: Sequence[str] = (),
        difficulty: str = "",
        now: int | None = None,
        locale: Any = None,
    ) -> UsageOverviewReport:
        now_ts = int(now or time.time())
        roles = _challenge_roles(roles)
        aggregates = _localized_rows(self._load_aggregates(roles, family), locale)
        if not aggregates:
            raise UsageUnavailableError("统计范围内还没有该玩法的挑战记录。")
        activity_id, scope_id, _rest, target_difficulty = self._resolve_target(
            aggregates, family, terms, difficulty, now_ts
        )
        return self._overview_report(
            scope, roles, family, aggregates, activity_id, scope_id, target_difficulty
        )

    def _resolve_target(
        self,
        aggregates: Sequence[_RecordAgg],
        family: str,
        terms: Sequence[str],
        difficulty: str,
        now_ts: int,
    ) -> tuple[str, str, tuple[str, ...], str]:
        activity_id, rest = self._pick_activity(aggregates, terms, now_ts)
        scope_id, rest = self._pick_scope(aggregates, family, activity_id, rest, now_ts)
        return activity_id, scope_id, rest, _normalize_difficulty(family, difficulty)

    def _stage_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        aggregates: Sequence[_RecordAgg],
        activity_id: str,
        scope_id: str,
        difficulty: str,
        stage_id: str,
        *,
        subject_label: str = "",
    ) -> UsageStageReport:
        catalog = self._catalog_maps()
        matching = [
            item
            for item in aggregates
            if item.activity_id == activity_id
            and item.scope_id == scope_id
            and item.stage_id == stage_id
            and item.difficulty == difficulty
        ]
        if not matching:
            raise UsageUnavailableError("该关卡在选定难度下还没有群内记录。")
        head = matching[0]
        bound_roles = _bound_role_count(roles)
        sample_roles = {key for item in matching for key in item.role_keys}
        sample_count = len(sample_roles)
        teams = _aggregate_teams(matching, catalog)
        return UsageStageReport(
            key=UsageStageKey(
                family=family,
                activity_id=activity_id,
                scope_id=scope_id,
                stage_id=stage_id,
                difficulty=difficulty,
            ),
            stage_name=head.stage_name,
            activity_name=head.activity_name,
            scope_name=head.scope_name,
            difficulty=difficulty,
            scope=scope,
            sample_count=sample_count,
            bound_role_count=bound_roles,
            coverage=(sample_count / bound_roles) if bound_roles else None,
            insufficient=(
                sample_count < MIN_SAMPLE_COUNT and not subject_label
            ),
            operators=_aggregate_operators(matching, catalog),
            teams=teams[:TEAM_LIMIT],
            team_combo_total=len(teams),
            latest_record_ts=max((item.record_ts for item in matching), default=0),
            regions=_regions(roles),
            snapshot_updated_at=self._snapshot_updated_at(roles, family),
            subject_label=subject_label,
        )

    def _overview_report(
        self,
        scope: str,
        roles: Sequence[EndfieldRole],
        family: str,
        aggregates: Sequence[_RecordAgg],
        activity_id: str,
        scope_id: str,
        difficulty: str,
        *,
        subject_label: str = "",
    ) -> UsageOverviewReport:
        catalog = self._catalog_maps()
        bound_roles = _bound_role_count(roles)
        summaries: list[UsageStageSummary] = []
        for stage_id in _stage_order(aggregates, activity_id, scope_id, difficulty):
            matching = [
                item
                for item in aggregates
                if item.activity_id == activity_id
                and item.scope_id == scope_id
                and item.stage_id == stage_id
                and item.difficulty == difficulty
            ]
            sample_roles = {key for item in matching for key in item.role_keys}
            summaries.append(
                UsageStageSummary(
                    stage_name=matching[0].stage_name,
                    stage_id=stage_id,
                    difficulty=difficulty,
                    sample_count=len(sample_roles),
                    insufficient=(
                        len(sample_roles) < MIN_SAMPLE_COUNT and not subject_label
                    ),
                    leads=_aggregate_operators(matching, catalog)[:3],
                    bound_role_count=bound_roles,
                )
            )
        if not summaries:
            raise UsageUnavailableError("当期该难度还没有群内记录。")
        activity_name = next(
            item.activity_name for item in aggregates if item.activity_id == activity_id
        )
        scope_name = next(
            (
                item.scope_name
                for item in aggregates
                if item.activity_id == activity_id and item.scope_id == scope_id
            ),
            "",
        )
        return UsageOverviewReport(
            family=family,
            activity_name=activity_name,
            scope_name=scope_name,
            difficulty=difficulty,
            scope=scope,
            bound_role_count=bound_roles,
            stages=tuple(summaries),
            recorded_role_count=len(
                {
                    key
                    for item in aggregates
                    if item.activity_id == activity_id
                    and item.scope_id == scope_id
                    and item.difficulty == difficulty
                    for key in item.role_keys
                }
            ),
            regions=_regions(roles),
            latest_record_ts=max((item.record_ts for item in aggregates), default=0),
            snapshot_updated_at=self._snapshot_updated_at(roles, family),
        )

    def has_records(self, roles: Sequence[EndfieldRole], family: str) -> bool:
        keys = {(role.role_id, role.server_id) for role in roles}
        return bool(self.store.list_challenge_member_rows(family, keys))

    def _catalog_maps(self) -> dict[str, OperatorCatalogEntry]:
        return {entry.operator_key: entry for entry in self.store.list_operator_catalog()}

    def _load_aggregates(
        self, roles: Sequence[EndfieldRole], family: str
    ) -> list[_RecordAgg]:
        keys = {(role.role_id, role.server_id) for role in roles}
        rows = self.store.list_challenge_member_rows(family, keys)
        grouped: OrderedDict[tuple[str, str, str, str, str], list[ChallengeMemberRow]] = (
            OrderedDict()
        )
        for row in rows:
            identity = (
                row.activity_id,
                row.scope_id,
                row.stage_id,
                row.difficulty,
                str(row.server_id) + "/" + str(row.role_id),
            )
            grouped.setdefault(identity, []).append(row)
        aggregates: list[_RecordAgg] = []
        for (_activity, _scope, _stage, _difficulty, role_key), items in grouped.items():
            head = items[0]
            server_id, role_id = role_key.split("/", 1)
            aggregates.append(
                _RecordAgg(
                    activity_id=head.activity_id,
                    activity_name=head.activity_name,
                    scope_id=head.scope_id,
                    scope_name=head.scope_name,
                    stage_id=head.stage_id,
                    stage_name=head.stage_name,
                    difficulty=head.difficulty,
                    activity_start_ts=head.activity_start_ts,
                    activity_end_ts=head.activity_end_ts,
                    scope_start_ts=head.scope_start_ts,
                    scope_end_ts=head.scope_end_ts,
                    record_ts=max(item.record_ts for item in items),
                    pass_time=max(item.pass_time for item in items),
                    role_keys=frozenset({(server_id, role_id)}),
                    members=tuple(sorted((item.slot, item.operator_key) for item in items)),
                )
            )
        return aggregates + self._stage_placeholders(roles, family, aggregates)

    def _stage_placeholders(
        self,
        roles: Sequence[EndfieldRole],
        family: str,
        aggregates: Sequence[_RecordAgg],
    ) -> list[_RecordAgg]:
        """给"快照里存在但没人通关"的关卡补占位行。

        影拓/回响的一期轮换通常有两三关,官方只给最佳记录,没人打过的那关在
        成员表里根本不存在。不补占位的话概览卡会只剩一关,看起来像少画了。
        """
        keys = {(role.role_id, role.server_id) for role in roles}
        known = {
            (item.activity_id, item.scope_id, item.stage_id, item.difficulty)
            for item in aggregates
        }
        placeholders: list[_RecordAgg] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in self.store.list_challenge_stage_rows(family, keys):
            identity = (
                row.activity_id,
                row.scope_id,
                row.stage_id,
                row.difficulty,
            )
            if identity in known or identity in seen:
                continue
            seen.add(identity)
            placeholders.append(
                _RecordAgg(
                    activity_id=row.activity_id,
                    activity_name=row.activity_name,
                    scope_id=row.scope_id,
                    scope_name=row.scope_name,
                    stage_id=row.stage_id,
                    stage_name=row.stage_name,
                    difficulty=row.difficulty,
                    activity_start_ts=row.activity_start_ts,
                    activity_end_ts=row.activity_end_ts,
                    scope_start_ts=row.scope_start_ts,
                    scope_end_ts=row.scope_end_ts,
                    record_ts=0,
                    pass_time=0,
                    role_keys=frozenset(),
                    members=(),
                )
            )
        return placeholders

    def _snapshot_updated_at(self, roles: Sequence[EndfieldRole], family: str) -> int:
        keys = [(role.role_id, role.server_id) for role in roles]
        states = self.store.list_challenge_snapshots(keys, family=family)
        return max((state.fetched_at for state in states), default=0)

    def _pick_activity(
        self, aggregates: Sequence[_RecordAgg], terms: Sequence[str], now_ts: int
    ) -> tuple[str, tuple[str, ...]]:
        names: OrderedDict[str, str] = OrderedDict()
        for item in aggregates:
            names.setdefault(item.activity_id, item.activity_name)
        if terms:
            matched = _match_tokens(terms, names)
            if matched is not None:
                activity_id, consumed = matched
                return activity_id, tuple(terms[consumed:])
        return _current_activity(aggregates, now_ts), tuple(terms)

    def _pick_scope(
        self,
        aggregates: Sequence[_RecordAgg],
        family: str,
        activity_id: str,
        terms: Sequence[str],
        now_ts: int,
    ) -> tuple[str, tuple[str, ...]]:
        if family != FAMILY_WAR_ECHO:
            return "", tuple(terms)
        scoped = [item for item in aggregates if item.activity_id == activity_id]
        names: OrderedDict[str, str] = OrderedDict()
        for item in scoped:
            if item.scope_id:
                names.setdefault(item.scope_id, item.scope_name)
        if terms:
            matched = _match_tokens(terms, names)
            if matched is not None:
                scope_id, consumed = matched
                return scope_id, tuple(terms[consumed:])
        return _current_scope(scoped, now_ts), tuple(terms)

    def _pick_stage(
        self,
        aggregates: Sequence[_RecordAgg],
        activity_id: str,
        scope_id: str,
        difficulty: str,
        terms: Sequence[str],
    ) -> str:
        candidates = [
            item
            for item in aggregates
            if item.activity_id == activity_id
            and item.scope_id == scope_id
            and item.difficulty == difficulty
        ]
        if not candidates:
            raise UsageUnavailableError("当期该难度还没有群内记录。")
        if not terms:
            return _single_stage(candidates)
        names: OrderedDict[str, str] = OrderedDict()
        for item in candidates:
            names.setdefault(item.stage_id, item.stage_name)
        query = " ".join(terms)
        matched = _match_name(query, names)
        if matched is None:
            options = "、".join(list(dict.fromkeys(names.values()))[:6])
            raise UsageResolutionError("没有找到叫“" + query + "”的关卡，可用：" + options)
        return matched[0]


# --- 模块级工具 -----------------------------------------------------------

_STOP_REASON = {
    "community-circuit-open": "官方社区接口处于冷却中，本轮提前结束",
    "community-protective-stop": "官方社区接口触发保护性熔断，本轮提前结束",
}

_SYSTEMIC_CODES = {"405", "429", "502", "503", "504"}

_API_LABELS = {
    "405": "官方拒绝访问",
    "429": "请求过于频繁",
    "502": "官方网关异常",
    "503": "官方服务不可用",
    "504": "官方网关超时",
}


def _family_tasks(roles: Sequence[EndfieldRole]) -> list[tuple[tuple[str, str], str]]:
    """角色 × 玩法 的待办清单；同角色的两族在同批内相邻,便于串行。"""
    tasks: list[tuple[tuple[str, str], str]] = []
    for role in roles:
        key = (str(role.role_id), str(role.server_id))
        for family in (FAMILY_MONUMENT, FAMILY_WAR_ECHO):
            tasks.append((key, family))
    return tasks


async def _challenge_locale() -> Any:
    """挑战关卡名需要 AKEData 的中文对照:官方响应会返回英文原文。

    取不到就返回 None,调用方退回官方原文,不阻塞批次。
    """
    try:
        return await get_challenge_locale(max_wait_seconds=5.0)
    except Exception as exc:  # pragma: no cover - 网络或快照异常时只降级
        logger.debug(
            "[endfield-usage] challenge locale unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return None


def _localized_rows(
    aggregates: Sequence[_RecordAgg], locale: Any
) -> list[_RecordAgg]:
    """把落库时的官方原文按 AKEData 对照改成中文(旧数据也一并修)。

    官方关卡名自带「·残酷」这类难度后缀,卡片另有难度标签,顺手去掉重复。
    """
    output: list[_RecordAgg] = []
    for item in aggregates:
        stage_name = item.stage_name
        if locale is not None:
            stage_name = _localized_name(locale, stage_name, item.stage_id)
        output.append(
            replace(
                item,
                activity_name=(
                    item.activity_name
                    if locale is None
                    else _localized_name(locale, item.activity_name, "")
                ),
                scope_name=(
                    item.scope_name
                    if locale is None
                    else _localized_name(locale, item.scope_name, "")
                ),
                stage_name=_strip_difficulty(stage_name, item.difficulty),
            )
        )
    return output


_DIFFICULTY_SUFFIXES = {
    "normal": ("·普通", "普通"),
    "hard": ("·困难", "·苦难", "困难", "苦难"),
    "cruel": ("·残酷", "残酷"),
}


def _strip_difficulty(text: str, difficulty: str) -> str:
    """官方关卡名末尾常自带「·残酷」,卡片另有难度标签,重复显示两次很难看。"""
    name = str(text or "").strip()
    for suffix in _DIFFICULTY_SUFFIXES.get(str(difficulty or ""), ()):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            return name[: -len(suffix)].strip()
    return name


def _localized_name(locale: Any, text: str, fallback: Any) -> str:
    raw = str(text or "").strip()
    try:
        if fallback:
            resolved = locale.dungeon_text(str(fallback), "name", raw)
        else:
            resolved = locale.text(raw)
    except Exception:  # pragma: no cover - 对照表结构变动时保持原文
        return raw
    resolved = str(resolved or "").strip()
    return resolved or raw


def _challenge_roles(roles: Sequence[EndfieldRole]) -> list[EndfieldRole]:
    """影拓丰碑与战争回响只有国服接口,亚服角色不参与统计。"""
    return [role for role in roles if not is_asia_role(role)]


def _roles_by_key(roles: Sequence[EndfieldRole]) -> dict[tuple[str, str], EndfieldRole]:
    return {(str(role.role_id), str(role.server_id)): role for role in roles}


def _retry_delay(
    previous_failures: int,
    base_seconds: int,
    max_seconds: int,
    *,
    base_seconds_override: int | None = None,
    max_seconds_override: int | None = None,
) -> float:
    base = int(base_seconds_override or base_seconds)
    ceiling = int(max_seconds_override or max_seconds)
    exponent = min(max(0, int(previous_failures)), 10)
    return float(min(ceiling, base * (2**exponent)))


def _systemic_code(issue_key: str) -> str:
    code = str(issue_key or "").rsplit(":", 1)[-1]
    return code if code in _SYSTEMIC_CODES else ""


def _api_issue_key(family: str, error: EndfieldAPIError) -> str:
    code = str(getattr(error, "code", "") or "").strip()
    label = FAMILY_LABELS_INTERNAL.get(family, family)
    return "api:" + label + ":" + (code or "unknown")


def _api_issue_label(error: EndfieldAPIError) -> str:
    code = str(getattr(error, "code", "") or "").strip()
    return _API_LABELS.get(code, "官方接口返回异常")


FAMILY_LABELS_INTERNAL = {FAMILY_MONUMENT: "影拓丰碑", FAMILY_WAR_ECHO: "战争回响"}


def _normalize_difficulty(family: str, difficulty: str) -> str:
    allowed = FAMILY_DIFFICULTIES.get(family, ("normal",))
    value = str(difficulty or "").strip().lower()
    return value if value in allowed else DEFAULT_DIFFICULTY.get(family, allowed[0])


def _bound_role_count(roles: Sequence[EndfieldRole]) -> int:
    return len({(role.server_id, role.role_id) for role in roles})


def _regions(roles: Sequence[EndfieldRole]) -> tuple[str, ...]:
    seen: list[str] = []
    for role in roles:
        label = "asia" if is_asia_role(role) else "cn"
        if label not in seen:
            seen.append(label)
    return tuple(seen)


def _current_activity(aggregates: Sequence[_RecordAgg], now_ts: int) -> str:
    active = [
        item
        for item in aggregates
        if item.activity_start_ts
        and item.activity_end_ts
        and item.activity_start_ts <= now_ts <= item.activity_end_ts
    ]
    if active:
        return max(active, key=lambda item: (item.activity_end_ts, item.record_ts)).activity_id
    with_end = [item for item in aggregates if item.activity_end_ts]
    if with_end:
        return max(with_end, key=lambda item: (item.activity_end_ts, item.record_ts)).activity_id
    return max(aggregates, key=lambda item: (item.record_ts, item.activity_id)).activity_id


def _current_scope(scoped: Sequence[_RecordAgg], now_ts: int) -> str:
    if not scoped:
        return ""
    active = [
        item
        for item in scoped
        if item.scope_start_ts
        and item.scope_end_ts
        and item.scope_start_ts <= now_ts <= item.scope_end_ts
    ]
    if active:
        return max(active, key=lambda item: (item.scope_end_ts, item.record_ts)).scope_id
    with_end = [item for item in scoped if item.scope_end_ts]
    if with_end:
        return max(with_end, key=lambda item: (item.scope_end_ts, item.record_ts)).scope_id
    return max(scoped, key=lambda item: (item.record_ts, item.scope_id)).scope_id


def _history_matches(
    filters: Sequence[str], activity_name: str, scope_name: str, scope_id: str
) -> bool:
    """期次过滤：每个词都要落在赛季名、轮换名或轮换 id 上。"""
    haystacks = [
        str(activity_name or "").casefold(),
        str(scope_name or "").casefold(),
        str(scope_id or "").casefold(),
    ]
    for token in filters:
        needle = str(token or "").strip().casefold()
        if not needle:
            continue
        if not any(needle in text or text in needle for text in haystacks if text):
            return False
    return True


def _stage_order(
    aggregates: Sequence[_RecordAgg], activity_id: str, scope_id: str, difficulty: str
) -> list[str]:
    ordered: list[str] = []
    for item in aggregates:
        if (
            item.activity_id == activity_id
            and item.scope_id == scope_id
            and item.difficulty == difficulty
            and item.stage_id not in ordered
        ):
            ordered.append(item.stage_id)
    return ordered


def _single_stage(candidates: Sequence[_RecordAgg]) -> str:
    stage_ids = list(dict.fromkeys(item.stage_id for item in candidates))
    if len(stage_ids) == 1:
        return stage_ids[0]
    raise UsageResolutionError("请补充关卡名称。")


def _match_name(
    query: str, names: Mapping[str, str]
) -> tuple[str, str] | None:
    """在 id -> 名称 里挑一个最贴近的,匹配不到返回 None。"""
    text = str(query or "").strip()
    if not text:
        return None
    normalized = text.casefold()
    folded = {key: str(value or "").casefold() for key, value in names.items()}
    for key, value in folded.items():
        if value and value == normalized:
            return key, names[key]
    prefix = [key for key, value in folded.items() if value and value.startswith(normalized)]
    if len(prefix) == 1:
        return prefix[0], names[prefix[0]]
    contains = [key for key, value in folded.items() if value and normalized in value]
    if len(contains) == 1:
        return contains[0], names[contains[0]]
    if len(prefix) > 1:
        contains = prefix
    return None


def _match_tokens(
    terms: Sequence[str], names: Mapping[str, str]
) -> tuple[str, int] | None:
    """按最长的词头去匹配名称,返回 (id, 吃掉几个词)。"""
    for size in range(len(terms), 0, -1):
        matched = _match_name(" ".join(str(item) for item in terms[:size]), names)
        if matched is not None:
            return matched[0], size
    return None


def _operator_meta(
    key: str, catalog: Mapping[str, OperatorCatalogEntry]
) -> tuple[str, int, str, int, str]:
    if key == ENDMIN_OPERATOR_KEY:
        entry = catalog.get(key)
        name = entry.name if entry and entry.name else ENDMIN_LABEL
        rarity = entry.rarity if entry else 6
        return name, rarity, "管理员", 0, operator_avatar_url(entry.source_id if entry else "")
    entry = catalog.get(key)
    if entry is None:
        return UNKNOWN_OPERATOR_LABEL, 0, "", 0, ""
    return entry.name or UNKNOWN_OPERATOR_LABEL, int(entry.rarity or 0), entry.profession, int(entry.sort_order or 0), operator_avatar_url(entry.source_id or "")


def _aggregate_operators(
    records: Sequence[_RecordAgg],
    catalog: Mapping[str, OperatorCatalogEntry],
    *,
    sample_count: int | None = None,
) -> tuple[UsageOperator, ...]:
    if sample_count is None:
        sample_count = len({key for item in records for key in item.role_keys})
    used: Counter[str] = Counter()
    for item in records:
        for _slot, operator_key in item.members:
            if operator_key:
                used[operator_key] += 1
    operators: list[UsageOperator] = []
    for key, count in used.items():
        name, rarity, profession, sort_order, avatar = _operator_meta(key, catalog)
        operators.append(
            UsageOperator(
                operator_key=key,
                name=name,
                rarity=rarity,
                profession=profession,
                sort_order=sort_order,
                avatar_url=avatar,
                used_count=count,
                sample_count=sample_count,
                usage_rate=(count / sample_count) if sample_count else None,
            )
        )
    operators.sort(
        key=lambda item: (
            -(item.usage_rate or 0.0),
            -item.used_count,
            -item.rarity,
            item.sort_order,
            item.operator_key,
        )
    )
    return tuple(operators)


def _aggregate_teams(
    records: Sequence[_RecordAgg],
    catalog: Mapping[str, OperatorCatalogEntry],
    *,
    sample_count: int | None = None,
) -> tuple[UsageTeam, ...]:
    if sample_count is None:
        sample_count = len({key for item in records for key in item.role_keys})
    grouped: dict[frozenset[str], list[_RecordAgg]] = {}
    for item in records:
        keys = frozenset(key for _slot, key in item.members if key)
        if not keys:
            continue
        grouped.setdefault(keys, []).append(item)
    teams: list[UsageTeam] = []
    for keys, items in grouped.items():
        representative = max(items, key=lambda item: item.record_ts)
        members = tuple(
            UsageTeamMember(
                operator_key=key,
                name=_operator_meta(key, catalog)[0],
                rarity=_operator_meta(key, catalog)[1],
                avatar_url=_operator_meta(key, catalog)[4],
            )
            for key in [operator_key for _slot, operator_key in representative.members if operator_key]
        )
        pass_times = [item.pass_time for item in items if item.pass_time > 0]
        teams.append(
            UsageTeam(
                members=members,
                count=len(items),
                rate=(len(items) / sample_count) if sample_count else 0.0,
                latest_ts=representative.record_ts,
                avg_pass_time=(round(sum(pass_times) / len(pass_times)) if pass_times else 0),
            )
        )
    teams.sort(key=lambda item: (-item.count, -item.latest_ts, item.members[0].operator_key))
    return tuple(teams)
