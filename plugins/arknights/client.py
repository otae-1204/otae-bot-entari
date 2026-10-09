"""Official Skland (森空岛) client for the Arknights attendance protocol.

Protocol references (public documentation and third-party clients):

* https://github.com/ProbiusOfficial/Skland_API — endpoint shapes, the
  ``bindingList`` payload and the ``{"uid", "gameId"}`` attendance body.
* https://github.com/AEtherside/skland-daily-attendance — current client, which
  decides "already signed today" from the ``GET /api/v1/game/attendance``
  status payload instead of guessing from a POST failure.

Arknights is a *different* game from Endfield: the binding entry is a flat
``bindingList`` item with ``uid``/``channelMasterId``/``channelName``/``nickName``
and the attendance result carries ``data.awards[].resource.name``.  Nothing from
the Endfield client (Endfield-specific business codes, ``sk-game-role`` headers,
``/web/v1/game/endfield/attendance``) is reused here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar
from urllib.parse import urlencode

import httpx

from otae_bot.infrastructure.http.tls import shared_ssl_context

from .store import RoleCandidate

for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)


AS_BASE = "https://as.hypergryph.com"
SKLAND_BASE = "https://zonai.skland.com"
SKLAND_APP_CODE = "4ca99fa6b56cc2ba"

BINDING_PATH = "/api/v1/game/player/binding"
ATTENDANCE_PATH = "/api/v1/game/attendance"
REFRESH_PATH = "/web/v1/auth/refresh"
GRANT_PATH = "/user/oauth2/v2/grant"
PHONE_CODE_PATH = "/general/v1/send_phone_code"
PHONE_TOKEN_PATH = "/user/auth/v1/token_by_phone_code"
# OAuth codes are single-use, so the second route needs a freshly granted code.
CREDENTIAL_PATHS = (
    "/api/v1/user/auth/generate_cred_by_code",
    "/web/v1/user/auth/generate_cred_by_code",
)

# The phone-code request type is a Hypergryph passport parameter; ``1`` is the
# value the same account service accepts for the in-repo Endfield client.
PHONE_CODE_TYPE = 1

SIGN_PLATFORM = "1"
SIGN_DEVICE_ID = "de9759a5afaa634f"
SIGN_VERSION_NAME = "1.45.1"
SIGN_VERSION_CODE = "104501004"
REQUEST_USER_AGENT = (
    f"Skland/{SIGN_VERSION_NAME} (com.hypergryph.skland; "
    f"build:{SIGN_VERSION_CODE}; Android 34; ) Okhttp/4.11.0"
)

CONTEXT_TTL_SECONDS = 540.0
SHANGHAI_TZ = timezone(timedelta(hours=8))

OPERATION_ATTENDANCE = "森空岛签到"
OPERATION_ATTENDANCE_STATUS = "查询签到状态"
OPERATION_BINDING = "查询游戏绑定"

# The only POST business code that may produce an "already signed" result, and
# only for the attendance operation.  Credential failures must never be
# reported as "already signed".
ALREADY_SIGNED_CODE = "10001"
CREDENTIAL_INVALID_CODES = frozenset({"401", "403"})
# Skland rejects a stale or invalidated ``cred``/sign token with these codes,
# in the body ``code`` or as the HTTP status.  A freshly exchanged context can
# repair that, so a signed request is retried once with one (same set as the
# Endfield client's ``_SKLAND_CONTEXT_RETRY_CODES``).
CONTEXT_RETRY_CODES = frozenset({"401", "10000", "10003"})
SIGNED_OPERATIONS = frozenset({OPERATION_ATTENDANCE, OPERATION_ATTENDANCE_STATUS, OPERATION_BINDING})
CREDENTIAL_EXPIRED_MESSAGE = "森空岛凭据已失效或登录状态异常，请重新私聊使用 /ak 绑定。"

# Official calendar slot kinds.  Only "daily" entries form the cumulative
# sign-in ladder; "first" and "activity" are separate and must stay excluded.
DAILY_CALENDAR_TYPE = "daily"
# The reward whose cumulative slots the card highlights (name comes from the
# official resourceInfoMap; the id is never guessed).
ORIGINIUM_REWARD_NAME = "合成玉"

_SAFE_CODE = re.compile(r"[0-9]{1,16}")
_T = TypeVar("_T")


class ArknightsAPIError(RuntimeError):
    """Sanitized Skland failure.

    Messages are built from local operation labels only; the server's free-text
    ``message``/``msg`` is never echoed back to chat, so a response can never
    leak a token or any other account material into a group.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        code: str = "",
        already_signed: bool = False,
    ):
        super().__init__(message)
        self.operation = operation
        self.code = code
        self.already_signed = already_signed


@dataclass(frozen=True, slots=True)
class AttendanceReward:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class AttendanceMilestone:
    """One cumulative sign-in slot of the official Arknights calendar.

    ``day`` is the official cumulative day: the 1-based position of the slot
    among the ``type == "daily"`` entries.  The official sign-in page codec does
    ``calendar.filter(e => e.type === "daily").map((e, i) => ({...e, day: i + 1}))``
    (``dist-BZImVwlH.js``), so ``first`` / ``activity`` slots neither shift that
    numbering nor become milestones.

    ``name`` / ``count`` always come from the response (``resourceInfoMap``);
    no reward ladder is hardcoded here.
    """

    day: int
    name: str
    count: int
    done: bool
    available: bool


@dataclass(frozen=True, slots=True)
class AttendanceCalendarDay:
    """One raw entry of the official Arknights attendance calendar.

    ``index`` is the position in the raw server list, which may also contain
    ``first`` / ``activity`` slots.  Cumulative day numbering exists only for
    ``daily`` entries, so read a day from :func:`parse_attendance_milestones`
    (or :func:`daily_calendar_entries`) instead of from ``index``.
    """

    index: int
    resource_id: str
    name: str
    reward_type: str
    count: int
    available: bool
    done: bool


@dataclass(frozen=True, slots=True)
class AttendanceResult:
    status: str
    message: str
    rewards: tuple[AttendanceReward, ...] = ()
    monthly_count: int | None = None
    milestones: tuple[AttendanceMilestone, ...] = ()
    daily_progress: tuple[bool, ...] = ()


@dataclass(slots=True)
class _SklandContext:
    cred: str
    sign_token: str
    server_time: int
    client_time: int
    expires_at: float


# --------------------------------------------------------------------- signing


def sign_canonical_string(
    path: str,
    body_or_query: str,
    timestamp: str,
    *,
    platform: str = SIGN_PLATFORM,
    device_id: str = SIGN_DEVICE_ID,
    version_name: str = SIGN_VERSION_NAME,
) -> str:
    """Build the exact canonical string the HMAC is computed over.

    Field order (``platform``, ``timestamp``, ``dId``, ``vName``) is part of the
    protocol and must never be sorted.
    """
    sign_headers = {
        "platform": platform,
        "timestamp": timestamp,
        "dId": device_id,
        "vName": version_name,
    }
    return (
        f"{path}{body_or_query}{timestamp}"
        + json.dumps(sign_headers, ensure_ascii=False, separators=(",", ":"))
    )


def build_signature(
    sign_token: str,
    path: str,
    body_or_query: str,
    timestamp: str,
    *,
    platform: str = SIGN_PLATFORM,
    device_id: str = SIGN_DEVICE_ID,
    version_name: str = SIGN_VERSION_NAME,
) -> tuple[str, dict[str, str]]:
    """``sign = md5(hmac_sha256(sign_token, path + body_or_query + ts + headers))``."""
    canonical = sign_canonical_string(
        path,
        body_or_query,
        timestamp,
        platform=platform,
        device_id=device_id,
        version_name=version_name,
    )
    digest = hmac.new(
        str(sign_token).encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hashlib.md5(digest.encode("utf-8")).hexdigest(), {
        "platform": platform,
        "timestamp": timestamp,
        "dId": device_id,
        "vName": version_name,
    }


def build_attendance_body(uid: str, game_id: str) -> str:
    """Serialize the attendance body once so the signed bytes are the sent bytes."""
    return json.dumps(
        {"uid": str(uid), "gameId": str(game_id)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": REQUEST_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://www.skland.com",
        "Referer": "https://www.skland.com/",
        "manufacturer": "Xiaomi",
        "os": "34",
        "vname": SIGN_VERSION_NAME,
        "vcode": SIGN_VERSION_CODE,
        "platform": SIGN_PLATFORM,
        "nid": "1",
        "channel": "OF",
        "language": "zh_CN",
        "dId": SIGN_DEVICE_ID,
    }


def safe_business_code(value: Any) -> str:
    """Return a plain decimal business code, else ``"unknown"``.

    Only digits may be echoed into a chat message or a log line.  A server that
    puts free text, a hex blob or a token fragment into its ``code`` field must
    never have that value forwarded, so anything non-numeric collapses to
    ``"unknown"``.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text if _SAFE_CODE.fullmatch(text) else "unknown"


def shanghai_date(moment: datetime | None = None) -> date:
    """UTC+8 is a fixed offset for Asia/Shanghai, so no tz database is needed."""
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(SHANGHAI_TZ).date()


def shanghai_month(moment: datetime | None = None) -> tuple[int, int]:
    """Return ``(year, month)`` in UTC+8 for the given instant."""
    current = shanghai_date(moment)
    return (current.year, current.month)


# -------------------------------------------------------------------- parsing


def extract_account_token(value: Any) -> str:
    """Accept either a raw Hypergryph token or the ``data.content`` JSON page."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, Mapping):
        return text
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    for candidate in (
        data.get("content"),
        data.get("token"),
        data.get("accountToken"),
        payload.get("content"),
        payload.get("token"),
        payload.get("accountToken"),
    ):
        if candidate:
            return str(candidate).strip()
    return ""


def parse_arknights_bindings(payload: Mapping[str, Any]) -> list[RoleCandidate]:
    """Keep only the ``appCode == "arknights"`` binding list.

    Each item already describes one character (official or Bilibili channel);
    missing UIDs are skipped. ``gameId`` is the game identifier;
    ``channelMasterId`` only identifies the distribution channel. Legacy
    Arknights bindings without ``gameId`` use the official Arknights game ID 1.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    entries = data.get("list") if isinstance(data, Mapping) else None
    if not isinstance(entries, (list, tuple)):
        return []
    found: list[RoleCandidate] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("appCode") or "").strip().casefold() != "arknights":
            continue
        bindings = entry.get("bindingList")
        if bindings is None:
            bindings = entry.get("binding_list")
        if not isinstance(bindings, (list, tuple)):
            continue
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            uid = str(binding.get("uid") or "").strip()
            game_id = str(
                binding.get("gameId") or "1"
            ).strip()
            if not uid or not game_id:
                continue
            key = (uid, game_id)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                RoleCandidate(
                    uid=uid,
                    game_id=game_id,
                    nickname=localized_text(
                        binding.get("nickName") or binding.get("nickname"),
                        default=f"博士{uid[-4:]}",
                    ),
                    channel_name=localized_text(binding.get("channelName"), default=""),
                )
            )
    return found


def reward_count(value: Any) -> int:
    """Return the reported reward quantity, or ``0`` when it is unusable.

    Policy for a malformed ``count``: keep the award (never silently drop what
    the server did report) but never invent a quantity.  A missing, ``null``,
    boolean, negative or non-numeric value becomes ``0`` rather than a
    fabricated ``1``, so the card cannot overstate a reward.  A usable value,
    including an explicit ``1``, is preserved exactly.
    """
    if isinstance(value, bool):
        return 0
    count = as_int(value, 0)
    return max(0, count)


def parse_attendance_awards(payload: Mapping[str, Any]) -> tuple[AttendanceReward, ...]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    awards = data.get("awards") if isinstance(data, Mapping) else None
    if not isinstance(awards, (list, tuple)):
        return ()
    rewards: list[AttendanceReward] = []
    for award in awards:
        if not isinstance(award, Mapping):
            continue
        resource = award.get("resource")
        resource = resource if isinstance(resource, Mapping) else {}
        name = (
            localized_text(resource.get("name"), default="")
            or localized_text(resource.get("id"), default="")
            or "签到奖励"
        )
        rewards.append(AttendanceReward(name=name, count=reward_count(award.get("count"))))
    return tuple(rewards)


def attendance_has_today(
    payload: Mapping[str, Any], *, now: datetime | None = None
) -> bool | None:
    """Return whether today is already signed, or ``None`` if undeterminable.

    Official rule from the sign-in page: only today's slot can still be
    ``available``, so a trustworthy non-empty ``daily`` calendar means "already
    signed" exactly when no daily slot is available
    (``!calendar.find(e => e.available)``).  An empty or untrustworthy calendar
    never implies "signed"; in that case ``data.records[].ts`` (Arknights) and
    ``data.hasToday`` (defensive) are used, and anything unusable yields
    ``None`` so the caller falls back to the POST instead of inventing state.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    has_today = data.get("hasToday")
    if isinstance(has_today, bool):
        return has_today
    daily = daily_calendar_entries(data)
    if daily is not None and all(
        isinstance(entry.get("available"), bool) for entry in daily
    ):
        return not any(entry.get("available") is True for entry in daily)
    records = data.get("records")
    if not isinstance(records, (list, tuple)):
        return None
    today = shanghai_date(now)
    for record in records:
        if not isinstance(record, Mapping):
            continue
        timestamp = as_int(record.get("ts"))
        if not timestamp:
            continue
        moment = _timestamp_moment(timestamp)
        if moment is not None and shanghai_date(moment) == today:
            return True
    return False


def _timestamp_moment(value: Any) -> datetime | None:
    """Reject malformed/out-of-range server times without losing sign-in results."""
    timestamp = as_int(value)
    if timestamp <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        # Conversion itself can overflow near datetime.max at the UTC+8 boundary.
        moment.astimezone(SHANGHAI_TZ)
        return moment
    except (ValueError, OverflowError, OSError):
        return None


def _reference_month(data: Mapping[str, Any], now: datetime | None) -> tuple[int, int]:
    """Prefer the server's own clock: ``data.currentTs`` is a UTC+8 instant."""
    server_ts = as_int(data.get("currentTs"))
    moment = _timestamp_moment(server_ts)
    if moment is not None:
        return shanghai_month(moment)
    return shanghai_month(now)


def daily_calendar_entries(
    data: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...] | None:
    """Return the official ``daily`` calendar entries, or ``None``.

    ``None`` means the calendar cannot be trusted for cumulative-day numbering:
    missing/empty, containing a non-mapping entry (an unknown entry could have
    been a ``daily`` slot, which would shift every later ``day``), or holding no
    ``daily`` entry at all.  ``first`` / ``activity`` slots are never included.
    """
    calendar = data.get("calendar")
    if not isinstance(calendar, (list, tuple)) or not calendar:
        return None
    if any(not isinstance(entry, Mapping) for entry in calendar):
        return None
    daily = tuple(
        entry for entry in calendar if str(entry.get("type") or "") == DAILY_CALENDAR_TYPE
    )
    return daily or None


def daily_checkin_progress(payload: Mapping[str, Any] | None) -> tuple[bool, ...]:
    """Keep each daily slot's actual state for the attendance milestone track."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    daily = daily_calendar_entries(data) if isinstance(data, Mapping) else None
    if not daily or len(daily) > 31 or any(not isinstance(entry.get("done"), bool) for entry in daily):
        return ()
    return tuple(entry["done"] for entry in daily)


def monthly_checkin_count(
    payload: Mapping[str, Any] | None, *, now: datetime | None = None
) -> int | None:
    """Count signed days in the current month, or ``None``.

    Preferred source is the official ``daily`` calendar — the same set the
    milestone nodes come from — counting ``done is True``.  This matches the
    official sign-in page, which shows
    ``calendar.filter(e => e.done).length``; ``first`` / ``activity`` slots never
    contribute.

    Fallback, used only when the calendar is missing or cannot be reliably
    parsed: deduplicate ``data.records[].ts`` by UTC+8 date inside the month the
    server reports in ``data.currentTs`` (else the local UTC+8 month).  Records
    are history and may include ``first`` / ``activity`` grants, so they are
    never allowed to override a calendar that was parsed successfully.

    A well-formed but empty structure is a real ``0``; anything missing or
    unparsable yields ``None`` instead of inventing ``0``.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    daily = daily_calendar_entries(data)
    if daily is not None and all(isinstance(entry.get("done"), bool) for entry in daily):
        return sum(1 for entry in daily if entry["done"])
    records = data.get("records")
    if not isinstance(records, (list, tuple)):
        return None
    reference = _reference_month(data, now)
    days: set[date] = set()
    usable = 0
    for record in records:
        if not isinstance(record, Mapping):
            continue
        timestamp = as_int(record.get("ts"))
        if timestamp <= 0:
            continue
        moment = _timestamp_moment(timestamp)
        if moment is None:
            continue
        usable += 1
        if shanghai_month(moment) == reference:
            days.add(shanghai_date(moment))
    if records and not usable:
        # Present but every entry is malformed: report "unknown", not 0.
        return None
    return len(days)


def parse_attendance_milestones(
    payload: Mapping[str, Any] | None,
) -> tuple[AttendanceMilestone, ...]:
    """Parse the official cumulative sign-in slots that grant 合成玉.

    Rules taken from the official page codec (``dist-BZImVwlH.js``):

    * only ``type == "daily"`` entries form the ladder; ``first`` / ``activity``
      are excluded and never shift the numbering;
    * ``day`` is the 1-based position **among all daily entries**, so it is
      computed before any reward-name filtering;
    * the reward name is resolved through ``resourceInfoMap[resourceId].name``
      (never guessed from a resource id) and only ``合成玉`` is kept.

    A node is dropped when the name is missing, the quantity is not a positive
    integer field, or ``done`` / ``available`` are not real booleans — but its
    daily position still advances.  When any calendar entry is not a mapping the
    numbering cannot be trusted, so no node is produced at all.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return ()
    calendar = data.get("calendar")
    if not isinstance(calendar, (list, tuple)) or not calendar:
        return ()
    if any(not isinstance(entry, Mapping) for entry in calendar):
        # An unknown entry could have been a daily slot; refuse to mis-number.
        return ()
    info = data.get("resourceInfoMap")
    info = info if isinstance(info, Mapping) else {}
    milestones: list[AttendanceMilestone] = []
    day = 0
    for entry in calendar:
        if str(entry.get("type") or "") != DAILY_CALENDAR_TYPE:
            continue
        day += 1
        resource_id = str(entry.get("resourceId") or "").strip()
        meta = info.get(resource_id) if resource_id else None
        name = localized_text(
            meta.get("name") if isinstance(meta, Mapping) else None, default=""
        )
        if name != ORIGINIUM_REWARD_NAME:
            continue
        count = entry.get("count")
        done = entry.get("done")
        available = entry.get("available")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        if not isinstance(done, bool) or not isinstance(available, bool):
            continue
        milestones.append(
            AttendanceMilestone(
                day=day, name=name, count=count, done=done, available=available
            )
        )
    return tuple(milestones)


def attendance_calendar_days(
    payload: Mapping[str, Any] | None,
) -> tuple[AttendanceCalendarDay, ...]:
    """Parse every official calendar entry (pure, no UI).

    Raw view: ``index`` keeps the server order across ``daily`` / ``first`` /
    ``activity``.  Names are resolved through
    ``data.resourceInfoMap[resourceId].name`` when that map is present.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return ()
    calendar = data.get("calendar")
    if not isinstance(calendar, (list, tuple)):
        return ()
    info = data.get("resourceInfoMap")
    info = info if isinstance(info, Mapping) else {}
    days: list[AttendanceCalendarDay] = []
    for index, entry in enumerate(calendar):
        if not isinstance(entry, Mapping):
            continue
        resource_id = str(entry.get("resourceId") or "").strip()
        meta = info.get(resource_id) if resource_id else None
        meta = meta if isinstance(meta, Mapping) else {}
        days.append(
            AttendanceCalendarDay(
                index=index,
                resource_id=resource_id,
                name=localized_text(meta.get("name"), default="")
                or localized_text(entry.get("name"), default=""),
                reward_type=str(meta.get("type") or entry.get("type") or "").strip(),
                count=reward_count(entry.get("count")),
                available=entry.get("available") is True,
                done=entry.get("done") is True,
            )
        )
    return tuple(days)


def localized_text(value: Any, *, default: str = "") -> str:
    if isinstance(value, Mapping):
        for key in ("zh-CN", "zh_CN", "zh", "cn", "zh-Hans", "en"):
            item = value.get(key)
            if item:
                return str(item).strip()
        for item in value.values():
            if item:
                return str(item).strip()
        return str(default)
    if value is None:
        return str(default)
    text = str(value).strip()
    return text or str(default)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


# --------------------------------------------------------------------- client


class ArknightsClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        timeout: float = 25.0,
        context_ttl_seconds: float = CONTEXT_TTL_SECONDS,
    ):
        # A shared TLS context: httpx would otherwise read the CA bundle
        # synchronously for this client.  Callers on the event loop build it
        # first with ``ashared_ssl_context`` so this lookup is a cache hit.
        self.http = http or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
            verify=shared_ssl_context(trust_env=False),
        )
        self._owns_http = http is None
        self._contexts: dict[str, _SklandContext] = {}
        self._context_lock = asyncio.Lock()
        self._context_ttl_seconds = max(30.0, float(context_ttl_seconds))

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    # ------------------------------------------------------------ login flow

    async def send_phone_code(self, phone: str) -> None:
        await self._json_request(
            "发送验证码",
            "POST",
            f"{AS_BASE}{PHONE_CODE_PATH}",
            json_body={"phone": str(phone), "type": PHONE_CODE_TYPE},
        )

    async def token_by_phone_code(self, phone: str, code: str) -> str:
        payload = await self._json_request(
            "验证码登录",
            "POST",
            f"{AS_BASE}{PHONE_TOKEN_PATH}",
            json_body={"phone": str(phone), "code": str(code)},
        )
        token = str((payload.get("data") or {}).get("token") or "").strip()
        if not token:
            raise ArknightsAPIError(
                "官方接口未返回账号凭据，请稍后重试。", operation="验证码登录"
            )
        return token

    async def discover_roles(self, account_token: str) -> list[RoleCandidate]:
        payload = await self._with_context(
            account_token,
            lambda context: self._signed_request(OPERATION_BINDING, context, "GET", BINDING_PATH),
        )
        return parse_arknights_bindings(payload)

    # ------------------------------------------------------------ attendance

    async def attendance(self, account_token: str, role: Any) -> AttendanceResult:
        """Sign one character in and report ``success``/``already``/``failed``.

        A best-effort ``GET /api/v1/game/attendance`` probe runs first so the
        cumulative progress can be read, and so an already-signed day needs no
        POST.  After a successful POST the status is read once more so the
        progress includes today; that second read is optional and a failure
        there never downgrades the successful sign-in, but it also means the
        stale pre-POST calendar is never reused.

        A credential the server rejected (:data:`CONTEXT_RETRY_CODES`) is
        exchanged again and the whole attempt runs once more; the rejected POST
        signed nothing, and the repeated probe still reports a day that did get
        signed as ``already``.

        Raises :class:`ArknightsAPIError` for failures; an expired credential is
        therefore always an error and is never reported as already signed.
        """
        return await self._with_context(
            account_token, lambda context: self._attendance(context, role)
        )

    async def _attendance(self, context: _SklandContext, role: Any) -> AttendanceResult:
        status_payload = await self._attendance_status_payload(context, role)
        if attendance_has_today(status_payload) is True:
            # Today is already signed, so the first read already contains today.
            return AttendanceResult(
                "already",
                "今日已签到",
                (),
                monthly_checkin_count(status_payload),
                parse_attendance_milestones(status_payload),
                daily_checkin_progress(status_payload),
            )
        body = build_attendance_body(getattr(role, "uid", ""), getattr(role, "game_id", ""))
        try:
            payload = await self._signed_request(
                OPERATION_ATTENDANCE, context, "POST", ATTENDANCE_PATH, raw_body=body
            )
        except ArknightsAPIError as exc:
            if exc.already_signed:
                # The POST refused a duplicate, so read the progress once more
                # instead of reporting nothing at all.
                refreshed = await self._attendance_status_payload(context, role)
                return AttendanceResult(
                    "already",
                    "今日已签到",
                    (),
                    monthly_checkin_count(refreshed),
                    parse_attendance_milestones(refreshed),
                    daily_checkin_progress(refreshed),
                )
            raise
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ArknightsAPIError(
                "官方接口未返回签到奖励明细，请稍后重试。", operation=OPERATION_ATTENDANCE
            )
        rewards = parse_attendance_awards(payload)
        refreshed = await self._attendance_status_payload(context, role)
        return AttendanceResult(
            "success",
            "签到成功",
            rewards,
            monthly_checkin_count(refreshed),
            parse_attendance_milestones(refreshed),
            daily_checkin_progress(refreshed),
        )

    async def _attendance_status_payload(
        self, context: _SklandContext, role: Any
    ) -> dict[str, Any] | None:
        """Best-effort signed status read; a failure yields ``None``.

        This single call feeds both the "already signed today" decision and the
        monthly total, so it is fetched once per attempt where possible.
        """
        params = {
            "uid": str(getattr(role, "uid", "")),
            "gameId": str(getattr(role, "game_id", "")),
        }
        try:
            return await self._signed_request(
                OPERATION_ATTENDANCE_STATUS, context, "GET", ATTENDANCE_PATH, params=params
            )
        except ArknightsAPIError:
            return None

    async def _attendance_already_today(self, context: _SklandContext, role: Any) -> bool | None:
        """Backwards-compatible wrapper over :meth:`_attendance_status_payload`."""
        return attendance_has_today(await self._attendance_status_payload(context, role))

    # --------------------------------------------------------- skland context

    async def _with_context(
        self, account_token: str, call: Callable[[_SklandContext], Awaitable[_T]]
    ) -> _T:
        """Run signed requests, exchanging a rejected context once.

        A second rejection means the stored login itself no longer works, so
        the user is told to bind again instead of seeing a bare business code.
        """
        context = await self._context(account_token)
        try:
            return await call(context)
        except ArknightsAPIError as exc:
            if not _should_refresh_context(exc):
                raise
        fresh = await self._context(account_token, refresh=True, stale=context)
        try:
            return await call(fresh)
        except ArknightsAPIError as exc:
            if not _should_refresh_context(exc):
                raise
            raise ArknightsAPIError(
                CREDENTIAL_EXPIRED_MESSAGE, operation=exc.operation, code=exc.code
            ) from None

    async def _context(
        self,
        account_token: str,
        *,
        refresh: bool = False,
        stale: _SklandContext | None = None,
    ) -> _SklandContext:
        """Return the cached context, or exchange the account token for one.

        ``refresh`` skips the cache.  With ``stale`` it only skips that very
        context: when concurrent requests were all rejected with it, the first
        one exchanges a new context and the others reuse it.
        """
        key = hashlib.sha256(str(account_token).encode("utf-8")).hexdigest()[:24]
        cached = self._usable_context(key, refresh=refresh, stale=stale)
        if cached is not None:
            return cached
        async with self._context_lock:
            cached = self._usable_context(key, refresh=refresh, stale=stale)
            if cached is not None:
                return cached
            # A rejected context must not be reused even if the exchange fails.
            self._contexts.pop(key, None)
            context = await self._create_context(account_token)
            self._contexts[key] = context
            return context

    def _usable_context(
        self, key: str, *, refresh: bool, stale: _SklandContext | None
    ) -> _SklandContext | None:
        cached = self._contexts.get(key)
        if cached is None or cached.expires_at <= time.monotonic():
            return None
        if refresh and (stale is None or cached is stale):
            return None
        return cached

    async def _create_context(self, account_token: str) -> _SklandContext:
        raw_token = extract_account_token(account_token)
        if not raw_token:
            raise ArknightsAPIError("账号 Token 为空，请重新私聊绑定。", operation="读取账号凭据")
        oauth_code = await self._grant_code(raw_token)
        cred, sign_token, server_time = await self._generate_credential(raw_token, oauth_code)
        if not sign_token:
            sign_token, refresh_time = await self._refresh_sign_token(cred)
            server_time = refresh_time or server_time
        if not sign_token:
            raise ArknightsAPIError(
                "未能获取森空岛签名凭据，请稍后重试。", operation="获取社区签名"
            )
        now = int(time.time())
        return _SklandContext(
            cred=cred,
            sign_token=sign_token,
            server_time=server_time or now,
            client_time=now,
            expires_at=time.monotonic() + self._context_ttl_seconds,
        )

    async def _grant_code(self, raw_token: str) -> str:
        payload = await self._json_request(
            "账号授权",
            "POST",
            f"{AS_BASE}{GRANT_PATH}",
            json_body={"appCode": SKLAND_APP_CODE, "token": raw_token, "type": 0},
        )
        code = str((payload.get("data") or {}).get("code") or "").strip()
        if not code:
            raise ArknightsAPIError("官方接口未返回授权码，请稍后重试。", operation="账号授权")
        return code

    async def _generate_credential(
        self, raw_token: str, oauth_code: str
    ) -> tuple[str, str, int]:
        code = oauth_code
        for index, path in enumerate(CREDENTIAL_PATHS):
            if index:
                # OAuth codes are single-use; request a fresh one before retrying.
                code = await self._grant_code(raw_token)
            try:
                payload = await self._json_request(
                    "获取社区凭据",
                    "POST",
                    f"{SKLAND_BASE}{path}",
                    json_body={"code": code, "kind": 1},
                )
            except ArknightsAPIError as exc:
                if exc.code != "404":
                    raise
                continue
            data = payload.get("data") or {}
            cred = str(data.get("cred") or "").strip()
            sign_token = str(data.get("token") or data.get("salt") or "").strip()
            if cred:
                return cred, sign_token, as_int(payload.get("timestamp"))
        # Both credential routes are unavailable; follow the third-party clients
        # and use the OAuth code itself as ``cred``, then refresh for a sign token.
        return code, "", 0

    async def _refresh_sign_token(self, cred: str) -> tuple[str, int]:
        payload = await self._json_request(
            "刷新社区签名",
            "GET",
            f"{SKLAND_BASE}{REFRESH_PATH}",
            headers={
                "cred": str(cred),
                "Content-Type": "application/json",
                "User-Agent": REQUEST_USER_AGENT,
            },
        )
        data = payload.get("data") or {}
        token = str(data.get("token") or data.get("salt") or "").strip()
        return token, as_int(payload.get("timestamp"))

    # --------------------------------------------------------------- requests

    async def _signed_request(
        self,
        operation: str,
        context: _SklandContext,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        raw_body: str = "",
    ) -> dict[str, Any]:
        """Sign and send one request.

        The query string for GET and the serialized body for POST are built here
        and reused verbatim for the signature and the wire payload, so the signed
        bytes and the sent bytes are always identical.
        """
        query = urlencode(dict(params)) if params else ""
        timestamp = str(context.server_time + (int(time.time()) - context.client_time))
        sign, sign_headers = build_signature(
            context.sign_token,
            path,
            query if method == "GET" else raw_body,
            timestamp,
        )
        headers = request_headers()
        headers["cred"] = context.cred
        headers.update(sign_headers)
        headers["sign"] = sign
        url = f"{SKLAND_BASE}{path}"
        if method == "GET" and query:
            url = f"{url}?{query}"
        content = raw_body.encode("utf-8") if method == "POST" else None
        return await self._json_request(
            operation, method, url, headers=headers, content=content
        )

    async def _json_request(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.http.request(
                method,
                url,
                params=dict(params) if params else None,
                json=dict(json_body) if json_body is not None else None,
                headers=dict(headers) if headers else None,
                content=content,
            )
        except httpx.HTTPError:
            raise ArknightsAPIError(
                "网络请求失败，请稍后重试。", operation=operation
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if payload is None:
            if response.status_code >= 400:
                raise ArknightsAPIError(
                    f"官方服务暂时不可用（HTTP {response.status_code}）。",
                    operation=operation,
                    code=str(response.status_code),
                )
            raise ArknightsAPIError("官方接口返回了无法解析的数据。", operation=operation)
        if not isinstance(payload, Mapping):
            raise ArknightsAPIError("官方接口返回格式异常。", operation=operation)
        code = payload.get("code")
        status = payload.get("status")
        if code not in (None, 0, "0"):
            raise self._business_error(operation, code)
        if status not in (None, 0, "0"):
            raise self._business_error(operation, status)
        if response.status_code >= 400:
            raise ArknightsAPIError(
                f"官方服务暂时不可用（HTTP {response.status_code}）。",
                operation=operation,
                code=str(response.status_code),
            )
        return dict(payload)

    @staticmethod
    def _business_error(operation: str, code: Any) -> ArknightsAPIError:
        safe = safe_business_code(code)
        if safe == ALREADY_SIGNED_CODE and operation == OPERATION_ATTENDANCE:
            return ArknightsAPIError(
                "今日已签到。", operation=operation, code=safe, already_signed=True
            )
        if safe in CREDENTIAL_INVALID_CODES:
            return ArknightsAPIError(CREDENTIAL_EXPIRED_MESSAGE, operation=operation, code=safe)
        return ArknightsAPIError(
            f"{operation}失败（{safe or '未知'}）。", operation=operation, code=safe
        )


def _should_refresh_context(error: ArknightsAPIError) -> bool:
    """Retry only failures that a fresh signing context can actually repair."""
    return error.operation in SIGNED_OPERATIONS and error.code in CONTEXT_RETRY_CODES
