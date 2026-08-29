from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .account_store import GachaRecord, RoleCandidate
from .account_i18n import localized_text


for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)


AS_BASE = "https://as.hypergryph.com"
SKLAND_BASE = "https://zonai.skland.com"
BINDING_BASE = "https://binding-api-account-prod.hypergryph.com"
GACHA_BASE = "https://ef-webview.hypergryph.com"
CUSTOMER_SERVICE_BASE = "https://customer-service.hypergryph.com"
CURRENCY_LOG_PATH = "/api/center/open/v1/endfield/game_logs/currency"
CURRENCY_TYPES = (1, 2, 3)
SKLAND_APP_CODE = "4ca99fa6b56cc2ba"
GACHA_APP_CODE = "be36d44aa36bfb5b"
SKLAND_REFRESH_USER_AGENT = (
    "Skland/1.21.0 (com.hypergryph.skland; build:102100065; iOS 17.6.0) Alamofire/5.7.1"
)
SKLAND_CONTEXT_TTL_SECONDS = 540
SKLAND_EXCHANGE_INTERVAL_SECONDS = 2.0
_SKLAND_CONTEXT_RETRY_CODES = {"401", "10000", "10003"}
ACCOUNT_PROVIDER_CN = "hypergryph"
ACCOUNT_PROVIDER_SKPORT = "gryphline"
_ACCOUNT_CREDENTIAL_KIND = "endfield-account-v1"
_SERVICE_TOKEN_KIND = "endfield-service-token-v1"
CHARACTER_POOL_TYPES = (
    "E_CharacterGachaPoolType_Special",
    "E_CharacterGachaPoolType_Joint",
    "E_CharacterGachaPoolType_Standard",
    "E_CharacterGachaPoolType_Beginner",
)

# Skland's ``endfield_attendance_*`` values are service-side reward aliases,
# not keys in AKEData's ItemTable.  These are the stable AKEData item ids that
# can be recovered from a resource's ``id``/``iconId``/icon URL.  The names
# were checked against AKEData ItemTable + I18nTextTable_CN.
_ATTENDANCE_AKEDATA_ITEM_NAMES = {
    "item_diamond": "嵌晶玉",
    "item_gold": "折金票",
    "item_originium_recharge": "衍质源石",
    "item_ticketgacha_beginner_ten": "十连启程寻访凭证",
    "item_ticketgacha_special_single": "特许寻访凭证",
    "item_ticketgacha_standard_single": "基础寻访凭证",
}
_ATTENDANCE_GENERIC_REWARD_NAMES = {"签到奖励", "attendance reward"}
_ATTENDANCE_AKEDATA_ITEM_ICON_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/"
    "gameplay/ui/sprites/itemiconbig"
)
_ATTENDANCE_AKEDATA_ITEM_ID_RE = re.compile(
    r"(?:item|ticketgacha|sysbp|preset)_[a-z0-9_]+$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ProviderConfig:
    account_base: str
    community_base: str
    binding_base: str
    gacha_base: str
    community_app_code: str
    gacha_app_code: str
    credential_path: str
    origin: str
    referer: str
    language: str


_PROVIDER_CONFIGS = {
    ACCOUNT_PROVIDER_CN: _ProviderConfig(
        account_base=AS_BASE,
        community_base=SKLAND_BASE,
        binding_base=BINDING_BASE,
        gacha_base=GACHA_BASE,
        community_app_code=SKLAND_APP_CODE,
        gacha_app_code=GACHA_APP_CODE,
        credential_path="/api/v1/user/auth/generate_cred_by_code",
        origin="https://game.skland.com",
        referer="https://game.skland.com/",
        language="zh-cn",
    ),
    ACCOUNT_PROVIDER_SKPORT: _ProviderConfig(
        account_base="https://as.gryphline.com",
        community_base="https://zonai.skport.com",
        binding_base="https://binding-api-account-prod.gryphline.com",
        gacha_base="https://ef-webview.gryphline.com",
        community_app_code="6eb76d4e13aa36e6",
        gacha_app_code="3dacefa138426cfe",
        credential_path="/web/v1/user/auth/generate_cred_by_code",
        origin="https://game.skport.com",
        referer="https://game.skport.com/",
        language="zh-tw",
    ),
}


class EndfieldAPIError(RuntimeError):
    def __init__(self, operation: str, code: str = "", message: str = ""):
        safe_message = _sanitize_message(message)
        detail = f"（{code}）" if code else ""
        super().__init__(f"{operation}失败{detail}{'：' + safe_message if safe_message else ''}")
        self.operation = operation
        self.code = code


@dataclass(frozen=True, slots=True)
class AttendanceReward:
    name: str
    count: int
    icon_url: str = ""


@dataclass(frozen=True, slots=True)
class AttendanceResult:
    status: str
    message: str
    rewards: tuple[AttendanceReward, ...] = ()
    monthly_count: int | None = None


@dataclass(frozen=True, slots=True)
class GachaPage:
    records: tuple[GachaRecord, ...]
    has_more: bool
    next_seq_id: str


@dataclass(frozen=True, slots=True)
class CurrencyLogItem:
    """One resource balance change returned by the customer-service API."""

    currency_type: int
    change_type: int
    change_reason: str
    change_num: int
    after: int
    change_time: int
    seq_id: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "CurrencyLogItem":
        return cls(
            currency_type=_as_int(raw.get("currencyType")),
            change_type=_as_int(raw.get("changeType")),
            change_reason=str(raw.get("changeReason") or "0"),
            change_num=_as_int(raw.get("changeNum")),
            after=_as_int(raw.get("after")),
            change_time=_as_int(raw.get("changeTime")),
            seq_id=_as_int(raw.get("seqId")),
        )


@dataclass(frozen=True, slots=True)
class QrLoginTicket:
    scan_id: str
    scan_url: str


@dataclass(frozen=True, slots=True)
class QrLoginStatus:
    state: str
    scan_code: str = ""


@dataclass(slots=True)
class _SklandContext:
    cred: str
    sign_token: str
    server_time: int
    client_time: int
    expires_at: float
    provider: str = ACCOUNT_PROVIDER_CN


class EndfieldOfficialClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        timeout: float = 25.0,
        community_exchange_interval_seconds: float = SKLAND_EXCHANGE_INTERVAL_SECONDS,
    ):
        self.http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False)
        self._owns_http = http is None
        self._skland_cache: dict[str, _SklandContext] = {}
        self._skland_exchange_lock = asyncio.Lock()
        self._skland_exchange_interval_seconds = max(0.0, float(community_exchange_interval_seconds))
        self._next_skland_exchange_at = 0.0
        self._u8_cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def send_phone_code(self, phone: str) -> None:
        await self._json_request(
            "发送验证码", "POST", f"{AS_BASE}/general/v1/send_phone_code", json_body={"phone": phone, "type": 1}
        )

    async def token_by_phone_code(self, phone: str, code: str) -> str:
        payload = await self._json_request(
            "验证码登录", "POST", f"{AS_BASE}/user/auth/v1/token_by_phone_code",
            json_body={"phone": phone, "code": code},
        )
        token = str((payload.get("data") or {}).get("token") or "")
        if not token:
            raise EndfieldAPIError("验证码登录", message="官方接口未返回账号凭据")
        return token

    async def create_qr_login(self) -> QrLoginTicket:
        payload = await self._json_request(
            "生成登录二维码",
            "POST",
            f"{AS_BASE}/general/v1/gen_scan/login",
            headers={
                "Origin": "https://user.hypergryph.com",
                "Referer": "https://user.hypergryph.com/",
            },
        )
        data = payload.get("data") or {}
        scan_id = str(data.get("scanId") or "").strip()
        scan_url = str(data.get("scanUrl") or "").strip()
        if not scan_id or not scan_url:
            raise EndfieldAPIError("生成登录二维码", message="官方接口未返回扫码凭据")
        return QrLoginTicket(scan_id=scan_id, scan_url=scan_url)

    async def check_qr_login(self, scan_id: str) -> QrLoginStatus:
        payload = await self._json_request(
            "查询扫码状态",
            "GET",
            f"{AS_BASE}/general/v1/scan_status",
            params={"scanId": scan_id},
            headers={
                "Origin": "https://user.hypergryph.com",
                "Referer": "https://user.hypergryph.com/",
            },
            allowed_statuses={100, 101, 102},
        )
        status = str(payload.get("status") or "0")
        if status == "100":
            return QrLoginStatus("pending")
        if status == "101":
            return QrLoginStatus("scanned")
        if status == "102":
            return QrLoginStatus("expired")
        scan_code = str((payload.get("data") or {}).get("scanCode") or "").strip()
        if not scan_code:
            raise EndfieldAPIError("查询扫码状态", message="官方接口未返回扫码授权码")
        return QrLoginStatus("confirmed", scan_code)

    async def token_by_scan_code(self, scan_code: str) -> str:
        payload = await self._json_request(
            "扫码登录",
            "POST",
            f"{AS_BASE}/user/auth/v1/token_by_scan_code",
            json_body={"scanCode": scan_code},
            headers={
                "Origin": "https://user.hypergryph.com",
                "Referer": "https://user.hypergryph.com/",
            },
        )
        token = str((payload.get("data") or {}).get("token") or "").strip()
        if not token:
            raise EndfieldAPIError("扫码登录", message="官方接口未返回账号凭据")
        return token

    async def discover_roles(self, account_token: str) -> list[RoleCandidate]:
        skland_roles = await self.get_skland_roles(account_token)
        try:
            gacha_roles = await self.get_gacha_roles(account_token)
        except EndfieldAPIError:
            gacha_roles = []
        result: list[RoleCandidate] = []
        for role in skland_roles:
            match = next(
                (
                    item for item in gacha_roles
                    if item.server_id == role.server_id
                    and (item.role_id == role.role_id or item.nickname == role.nickname)
                ),
                None,
            )
            result.append(
                RoleCandidate(
                    binding_uid=(match.binding_uid if match else role.binding_uid or role.role_id),
                    role_id=role.role_id,
                    server_id=role.server_id,
                    nickname=role.nickname,
                    server_name=role.server_name,
                )
            )
        return result

    async def get_skland_roles(self, account_token: str) -> list[RoleCandidate]:
        context = await self._skland_context(account_token)
        payload = await self._signed_skland_request(context, "GET", "/api/v1/game/player/binding")
        return _extract_endfield_roles(payload)

    async def attendance(self, account_token: str, role: RoleCandidate | Any) -> AttendanceResult:
        context = await self._skland_context(account_token, refresh=True)
        headers = {"sk-game-role": f"3_{role.role_id}_{role.server_id}"}
        status = "success"
        message = "签到成功"
        rewards: list[AttendanceReward] = []
        award_entries: list[tuple[Any, dict[str, Any]]] = []
        reward_maps: list[Any] = []
        try:
            payload = await self._signed_skland_request(
                context, "POST", "/web/v1/game/endfield/attendance", raw_body="", extra_headers=headers
            )
        except EndfieldAPIError as exc:
            if exc.code in {"10001", "10002", "10012", "10013"} or "已签到" in str(exc):
                status = "already"
                message = "今日已签到"
            else:
                raise
        else:
            data = payload.get("data") or {}
            if isinstance(data, dict):
                award_entries = _attendance_award_entries(data.get("awardIds"))
                reward_maps.append(data.get("resourceInfoMap"))
                rewards = _attendance_rewards(award_entries, reward_maps)

        monthly_count: int | None = None
        try:
            calendar_payload = await self._signed_skland_request(
                context, "GET", "/web/v1/game/endfield/attendance", extra_headers=headers
            )
        except EndfieldAPIError:
            pass
        else:
            calendar_data = calendar_payload.get("data") or {}
            calendar = calendar_data.get("calendar") if isinstance(calendar_data, dict) else None
            if isinstance(calendar_data, dict):
                # Some responses omit names from the POST result but include them
                # in the resource table returned by the subsequent calendar GET.
                reward_maps.append(calendar_data.get("resourceInfoMap"))
                if award_entries:
                    rewards = _attendance_rewards(award_entries, reward_maps)
            if isinstance(calendar, list):
                monthly_count = sum(
                    1 for item in calendar
                    if isinstance(item, dict) and bool(item.get("done"))
                )
        return AttendanceResult(status, message, tuple(rewards), monthly_count)

    async def card_detail(self, account_token: str, role: RoleCandidate | Any) -> dict[str, Any]:
        async def request(context: _SklandContext) -> dict[str, Any]:
            extra_headers = None
            if context.provider == ACCOUNT_PROVIDER_SKPORT:
                extra_headers = {"sk-game-role": f"3_{role.role_id}_{role.server_id}"}
            return await self._signed_skland_request(
                context,
                "GET",
                "/api/v1/game/endfield/card/detail",
                params={"roleId": str(role.role_id), "serverId": str(role.server_id)},
                extra_headers=extra_headers,
            )

        context = await self._skland_context(account_token)
        try:
            payload = await request(context)
        except EndfieldAPIError as exc:
            if not _should_refresh_skland_context(exc):
                raise
            context = await self._skland_context(account_token, refresh=True)
            payload = await request(context)
        detail = (payload.get("data") or {}).get("detail")
        if not isinstance(detail, dict) or not detail:
            raise EndfieldAPIError("查询终末地档案", message="官方接口未返回角色档案")
        return detail

    async def currency_balances(self, account_token: str, role: RoleCandidate | Any) -> dict[int, int]:
        headers = await self._currency_headers(account_token, role, operation="查询终末地货币")

        async def fetch_balance(currency_type: int) -> int | None:
            payload = await self._json_request(
                "查询终末地货币",
                "POST",
                f"{CUSTOMER_SERVICE_BASE}{CURRENCY_LOG_PATH}",
                headers=headers,
                json_body={"limit": 1, "currencyType": currency_type, "changeType": 0},
            )
            items = (payload.get("data") or {}).get("list") or []
            if not items or not isinstance(items[0], dict) or "after" not in items[0]:
                return None
            return _as_int(items[0].get("after"))

        results = await asyncio.gather(
            *(fetch_balance(currency_type) for currency_type in CURRENCY_TYPES),
            return_exceptions=True,
        )
        balances: dict[int, int] = {}
        errors: list[Exception] = []
        for currency_type, result in zip(CURRENCY_TYPES, results, strict=True):
            if isinstance(result, Exception):
                errors.append(result)
            elif result is not None:
                balances[currency_type] = result
        if not balances and errors:
            raise errors[0]
        return balances

    async def currency_logs(
        self,
        account_token: str,
        role: RoleCandidate | Any,
        *,
        currency_types: tuple[int, ...] = CURRENCY_TYPES,
        start_ts: int | None = None,
        end_ts: int | None = None,
        change_type: int = 0,
        limit: int = 50,
    ) -> dict[int, tuple[CurrencyLogItem, ...]]:
        """Fetch resource logs in the requested time window for one role.

        The customer-service API returns newest records first and uses the last
        record's ``seqId`` as the cursor for the next page.  Passing ``None``
        for ``start_ts`` fetches the complete available history; a bounded
        ``start_ts`` stops as soon as a record falls before that timestamp.
        """

        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            raise ValueError("资源流水查询的开始时间不能晚于结束时间")
        if int(change_type) not in {0, 1, 2}:
            raise ValueError("不支持的流水类型，必须是 0（全部）、1（获取）或 2（消耗）")
        requested_types = tuple(dict.fromkeys(int(item) for item in currency_types))
        invalid_types = [item for item in requested_types if item not in CURRENCY_TYPES]
        if invalid_types:
            raise ValueError(f"不支持的资源类型：{', '.join(map(str, invalid_types))}")
        if not requested_types:
            return {}

        headers = await self._currency_headers(account_token, role, operation="查询终末地资源流水")
        page_limit = max(1, min(int(limit), 100))
        result: dict[int, tuple[CurrencyLogItem, ...]] = {}
        for currency_type in requested_types:
            rows: list[CurrencyLogItem] = []
            seq_id: int | None = None
            while True:
                body: dict[str, Any] = {
                    "limit": page_limit,
                    "currencyType": currency_type,
                    "changeType": int(change_type),
                }
                if seq_id is not None:
                    body["seqId"] = seq_id
                payload = await self._json_request(
                    "查询终末地资源流水",
                    "POST",
                    f"{CUSTOMER_SERVICE_BASE}{CURRENCY_LOG_PATH}",
                    headers=headers,
                    json_body=body,
                )
                data = payload.get("data") or {}
                raw_items = (data.get("list") or []) if isinstance(data, dict) else []
                if not isinstance(raw_items, list) or not raw_items:
                    break

                stop_early = False
                page_rows: list[CurrencyLogItem] = []
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    item = CurrencyLogItem.from_api(raw)
                    if end_ts is not None and item.change_time > end_ts:
                        continue
                    if start_ts is not None and item.change_time < start_ts:
                        stop_early = True
                        break
                    page_rows.append(item)
                rows.extend(page_rows)

                has_next = data.get("hasNext") if isinstance(data, dict) else False
                if has_next is None and isinstance(data, dict):
                    has_next = data.get("hasMore")
                if stop_early or not bool(has_next):
                    break
                next_seq_id = _as_int(raw_items[-1].get("seqId")) if isinstance(raw_items[-1], dict) else 0
                if not next_seq_id or next_seq_id == seq_id:
                    break
                seq_id = next_seq_id
                await asyncio.sleep(0.25)
            result[currency_type] = tuple(rows)
        return result

    async def _currency_headers(
        self, account_token: str, role: RoleCandidate | Any, *, operation: str
    ) -> dict[str, str]:
        provider, raw_account_token = decode_account_credential(account_token)
        if provider == ACCOUNT_PROVIDER_SKPORT:
            message = "亚服暂不支持货币查询" if operation == "查询终末地货币" else "亚服暂不支持资源流水查询"
            raise EndfieldAPIError(operation, message=message)
        role_token = await self.get_u8_token(account_token, str(role.binding_uid))
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": CUSTOMER_SERVICE_BASE,
            "Referer": f"{CUSTOMER_SERVICE_BASE}/app/endfield/gamelogs/2",
            "x-account-token": raw_account_token,
            "x-role-token": role_token,
            "x-role-server-id": str(role.server_id),
            "x-hg-language": "zh-cn",
        }

    async def endfield_card_detail(self, account_token: str, role: RoleCandidate | Any) -> dict[str, Any]:
        """查询森空岛终末地个人详情（含奖章进度）。

        GET /api/v1/game/endfield/card/detail?roleId=<id>&serverId=<id>，签名 GET（query 入签名）。
        奖章进度在 ``data.detail.achieve.achieveMedals[]``，每枚含 ``achievementData.id`` /
        ``level`` / ``isPlated``。依据 docs/skland_endfield_personal_api.md（2026-07-15 实测）。
        """
        context = await self._skland_context(account_token, refresh=True)
        return await self._signed_skland_request(
            context,
            "GET",
            "/api/v1/game/endfield/card/detail",
            params={"roleId": str(role.role_id), "serverId": str(role.server_id)},
        )

    async def get_gacha_roles(self, account_token: str) -> list[RoleCandidate]:
        provider, _raw_token = decode_account_credential(account_token)
        config = _PROVIDER_CONFIGS[provider]
        oauth_token = await self._oauth_token(account_token, config.gacha_app_code, grant_type=1)
        payload = await self._json_request(
            "查询终末地账号", "GET", f"{config.binding_base}/account/binding/v1/binding_list",
            params={"token": oauth_token, "appCode": "endfield"},
        )
        return _extract_gacha_binding_roles(payload)

    async def get_u8_token(self, account_token: str, binding_uid: str) -> str:
        provider, _raw_token = decode_account_credential(account_token)
        config = _PROVIDER_CONFIGS[provider]
        fingerprint = hashlib.sha256(f"{provider}:{account_token}".encode("utf-8")).hexdigest()[:24]
        cache_key = (fingerprint, binding_uid)
        cached = self._u8_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        oauth_token = await self._oauth_token(account_token, config.gacha_app_code, grant_type=1)
        payload = await self._json_request(
            "获取抽卡凭据", "POST", f"{config.binding_base}/account/binding/v1/u8_token_by_uid",
            json_body={"token": oauth_token, "uid": binding_uid},
        )
        data = payload.get("data") or {}
        token = str(data.get("token") or data.get("u8Token") or data.get("u8_token") or "")
        if not token:
            raise EndfieldAPIError("获取抽卡凭据", message="官方接口未返回 U8 凭据")
        scoped_token = encode_service_token(token, provider)
        self._u8_cache[cache_key] = (scoped_token, time.monotonic() + 540)
        return scoped_token

    async def character_pool_names(self, u8_token: str, server_id: str) -> dict[str, str]:
        provider, raw_u8_token = decode_service_token(u8_token)
        config = _PROVIDER_CONFIGS[provider]
        # The Gryphline API exposes character records by their fixed pool type,
        # but does not expose the CN-only /char/pool metadata endpoint.
        if provider == ACCOUNT_PROVIDER_SKPORT:
            return {}
        payload = await self._json_request(
            "查询角色卡池", "GET", f"{config.gacha_base}/api/record/char/pool",
            params={"lang": "zh-cn", "token": raw_u8_token, "server_id": server_id},
        )
        result: dict[str, str] = {}
        for item in _response_items(payload):
            pool_type = str(item.get("poolType") or item.get("pool_type") or item.get("type") or "")
            name = localized_text(
                item.get("poolName") or item.get("pool_name") or item.get("name"),
                default=pool_type,
            )
            if pool_type:
                result[pool_type] = name
        return result

    async def weapon_pools(self, u8_token: str, server_id: str) -> list[tuple[str, str]]:
        provider, raw_u8_token = decode_service_token(u8_token)
        config = _PROVIDER_CONFIGS[provider]
        payload = await self._json_request(
            "查询武器卡池", "GET", f"{config.gacha_base}/api/record/weapon/pool",
            params={"lang": "zh-cn", "token": raw_u8_token, "server_id": server_id},
        )
        result: list[tuple[str, str]] = []
        for item in _response_items(payload):
            pool_id = str(item.get("poolId") or item.get("pool_id") or item.get("id") or "")
            if pool_id:
                result.append(
                    (
                        pool_id,
                        localized_text(
                            item.get("poolName") or item.get("pool_name") or item.get("name"),
                            default=pool_id,
                        ),
                    )
                )
        return result

    async def character_records(
        self, role: Any, u8_token: str, pool_type: str, *, seq_id: str = "", pool_name: str = ""
    ) -> GachaPage:
        provider, raw_u8_token = decode_service_token(u8_token)
        config = _PROVIDER_CONFIGS[provider]
        params = {"lang": "zh-cn", "pool_type": pool_type, "token": raw_u8_token, "server_id": role.server_id}
        if seq_id:
            params["seq_id"] = seq_id
        payload = await self._json_request("同步角色抽卡", "GET", f"{config.gacha_base}/api/record/char", params=params)
        items = _response_items(payload)
        records = tuple(
            _character_record(role, item, pool_type, pool_name)
            for item in items
            if item.get("seqId") is not None and item.get("charId")
        )
        next_seq_id = str(items[-1].get("seqId") or "") if items else ""
        return GachaPage(records, _response_has_more(payload), next_seq_id)

    async def weapon_records(
        self, role: Any, u8_token: str, pool_id: str = "", *, seq_id: str = "", pool_name: str = ""
    ) -> GachaPage:
        provider, raw_u8_token = decode_service_token(u8_token)
        config = _PROVIDER_CONFIGS[provider]
        params = {"lang": "zh-cn", "token": raw_u8_token, "server_id": role.server_id}
        if pool_id:
            params["pool_id"] = pool_id
        if seq_id:
            params["seq_id"] = seq_id
        payload = await self._json_request("同步武器抽卡", "GET", f"{config.gacha_base}/api/record/weapon", params=params)
        items = _response_items(payload)
        records = tuple(
            _weapon_record(role, item, pool_id, pool_name)
            for item in items
            if item.get("seqId") is not None and item.get("weaponId")
        )
        next_seq_id = str(items[-1].get("seqId") or "") if items else ""
        return GachaPage(records, _response_has_more(payload), next_seq_id)

    async def _skland_context(self, account_token: str, *, refresh: bool = False) -> _SklandContext:
        provider, raw_account_token = decode_account_credential(account_token)
        config = _PROVIDER_CONFIGS[provider]
        key = hashlib.sha256(f"{provider}:{raw_account_token}".encode("utf-8")).hexdigest()[:24]
        cached = self._skland_cache.get(key)
        if cached and not refresh and cached.expires_at > time.monotonic():
            return cached
        async with self._skland_exchange_lock:
            # Another role using the same binding may have filled the cache while
            # this coroutine was waiting for the exchange gate.
            cached = self._skland_cache.get(key)
            if cached and not refresh and cached.expires_at > time.monotonic():
                return cached
            delay = self._next_skland_exchange_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_skland_exchange_at = (
                time.monotonic() + self._skland_exchange_interval_seconds
            )
            context = await self._create_skland_context(account_token, provider, config)
            self._skland_cache[key] = context
            return context

    async def _create_skland_context(
        self,
        account_token: str,
        provider: str,
        config: _ProviderConfig,
    ) -> _SklandContext:
        cred = ""
        sign_token = ""
        server_time = 0
        oauth_code = await self._oauth_token(
            account_token, config.community_app_code, grant_type=0, field="code"
        )
        try:
            credential_payload = await self._json_request(
                "获取社区凭据", "POST", f"{config.community_base}{config.credential_path}",
                json_body={"code": oauth_code, "kind": 1},
            )
        except EndfieldAPIError as exc:
            if provider != ACCOUNT_PROVIDER_SKPORT or exc.code != "404":
                raise
            # SKPORT currently selects /web or /api according to its runtime.
            # OAuth codes are single-use, so obtain a fresh code before retrying.
            oauth_code = await self._oauth_token(
                account_token, config.community_app_code, grant_type=0, field="code"
            )
            try:
                credential_payload = await self._json_request(
                    "获取社区凭据",
                    "POST",
                    f"{config.community_base}/api/v1/user/auth/generate_cred_by_code",
                    json_body={"code": oauth_code, "kind": 1},
                )
            except EndfieldAPIError as fallback_exc:
                if fallback_exc.code != "404":
                    raise
                # Current third-party SKPORT clients use the OAuth code itself as
                # cred when the credential-exchange route is unavailable.
                cred = oauth_code
        if not cred:
            credential_data = credential_payload.get("data") or {}
            cred = str(credential_data.get("cred") or "")
            if not cred:
                raise EndfieldAPIError("获取社区凭据", message="官方接口未返回 cred")
            sign_token = str(credential_data.get("token") or credential_data.get("salt") or "")
            server_time = _as_int(credential_payload.get("timestamp"))
        if not sign_token:
            refresh_payload = await self._json_request(
                "刷新社区签名",
                "GET",
                f"{config.community_base}/web/v1/auth/refresh",
                headers={
                    "cred": cred,
                    "Content-Type": "application/json",
                    "User-Agent": SKLAND_REFRESH_USER_AGENT,
                },
            )
            data = refresh_payload.get("data") or {}
            sign_token = str(data.get("token") or data.get("salt") or "")
            if not sign_token:
                raise EndfieldAPIError("刷新社区签名", message="官方接口未返回签名凭据")
            server_time = _as_int(refresh_payload.get("timestamp"))
        now = int(time.time())
        context = _SklandContext(
            cred=cred,
            sign_token=sign_token,
            server_time=server_time or now,
            client_time=now,
            expires_at=time.monotonic() + SKLAND_CONTEXT_TTL_SECONDS,
            provider=provider,
        )
        return context

    async def _oauth_token(
        self, account_token: str, app_code: str, *, grant_type: int, field: str = "token"
    ) -> str:
        provider, raw_account_token = decode_account_credential(account_token)
        config = _PROVIDER_CONFIGS[provider]
        payload = await self._json_request(
            "账号授权", "POST", f"{config.account_base}/user/oauth2/v2/grant",
            json_body={"appCode": app_code, "token": raw_account_token, "type": grant_type},
            retry_network=True,
        )
        value = str((payload.get("data") or {}).get(field) or "")
        if not value:
            raise EndfieldAPIError("账号授权", message="官方接口未返回授权凭据")
        return value

    async def _signed_skland_request(
        self,
        context: _SklandContext,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw_body: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        config = _PROVIDER_CONFIGS[context.provider]
        query = urlencode(params or {})
        timestamp = str(context.server_time + (int(time.time()) - context.client_time))
        sign_headers = {"platform": "3", "timestamp": timestamp, "dId": "", "vName": "1.0.0"}
        canonical = path + (query if method == "GET" else raw_body) + timestamp + json.dumps(
            sign_headers, ensure_ascii=False, separators=(",", ":")
        )
        hmac_hex = hmac.new(
            context.sign_token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "cred": context.cred,
            "platform": "3",
            "timestamp": timestamp,
            "vName": "1.0.0",
            "sign": hashlib.md5(hmac_hex.encode("utf-8")).hexdigest(),
            "Origin": config.origin,
            "Referer": config.referer,
            "sk-language": config.language,
        }
        headers.update(extra_headers or {})
        return await self._json_request(
            "社区请求", method, f"{config.community_base}{path}", params=params, headers=headers,
            content=raw_body.encode("utf-8") if method == "POST" else None,
        )

    async def _json_request(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        allowed_statuses: set[int | str] | None = None,
        retry_network: bool = False,
    ) -> dict[str, Any]:
        attempts = 2 if retry_network else 1
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method, url, params=params, json=json_body, headers=headers, content=content
                )
                break
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4)
                    continue
                raise EndfieldAPIError(operation, message="网络请求失败，请稍后重试") from None
        try:
            payload = response.json()
        except ValueError:
            if response.status_code >= 400:
                raise EndfieldAPIError(operation, code=str(response.status_code), message="官方服务暂时不可用") from None
            raise EndfieldAPIError(operation, message="官方接口返回了无法解析的数据") from None
        if not isinstance(payload, dict):
            if response.status_code >= 400:
                raise EndfieldAPIError(operation, code=str(response.status_code), message="官方服务暂时不可用")
            raise EndfieldAPIError(operation, message="官方接口返回格式异常")
        code = payload.get("code")
        if code not in (None, 0, "0"):
            raise EndfieldAPIError(operation, str(code), str(payload.get("message") or payload.get("msg") or ""))
        status = payload.get("status")
        allowed_status_values = {str(item) for item in (allowed_statuses or set())}
        if status not in (None, 0, "0") and str(status) not in allowed_status_values:
            raise EndfieldAPIError(operation, str(status), str(payload.get("message") or payload.get("msg") or ""))
        if response.status_code >= 400:
            raise EndfieldAPIError(operation, code=str(response.status_code), message="官方服务暂时不可用")
        return payload


def _extract_endfield_roles(payload: dict[str, Any]) -> list[RoleCandidate]:
    candidates: list[RoleCandidate] = []
    data = payload.get("data") or {}
    app_entries = [
        item for item in _walk_dicts(data)
        if "endfield" in str(item.get("appCode") or item.get("gameId") or "").casefold()
        or "终末地" in str(item.get("appName") or item.get("gameName") or "")
    ]
    for entry in app_entries:
        for binding in entry.get("bindingList") or entry.get("binding_list") or []:
            if not isinstance(binding, dict):
                continue
            binding_uid = str(binding.get("uid") or "")
            roles = binding.get("roles") or []
            if not roles and isinstance(binding.get("defaultRole"), dict):
                roles = [binding["defaultRole"]]
            for role in roles:
                if not isinstance(role, dict) or not role.get("roleId") or role.get("serverId") is None:
                    continue
                candidates.append(
                    RoleCandidate(
                        binding_uid=binding_uid,
                        role_id=str(role.get("roleId")), server_id=str(role.get("serverId")),
                        nickname=localized_text(
                            role.get("nickname")
                            or role.get("nickName")
                            or binding.get("nickName"),
                            default="未命名角色",
                        ),
                        server_name=localized_text(
                            role.get("serverName")
                            or role.get("serverType")
                            or binding.get("channelName")
                        ),
                    )
                )
    return _dedupe_roles(candidates)


def encode_account_credential(account_token: str, provider: str = ACCOUNT_PROVIDER_CN) -> str:
    normalized_provider = str(provider or "").strip().casefold()
    if normalized_provider not in _PROVIDER_CONFIGS:
        raise ValueError("不支持的终末地账号服务")
    token = _extract_account_token(account_token)
    if not token:
        raise ValueError("账号 Token 不能为空")
    return json.dumps(
        {"kind": _ACCOUNT_CREDENTIAL_KIND, "provider": normalized_provider, "token": token},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_account_credential(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("kind") == _ACCOUNT_CREDENTIAL_KIND:
        provider = str(payload.get("provider") or "").strip().casefold()
        token = str(payload.get("token") or "").strip()
        if provider not in _PROVIDER_CONFIGS or not token:
            raise ValueError("终末地账号凭据格式无效")
        return provider, token
    token = _extract_account_token(text)
    if not token:
        raise ValueError("账号 Token 不能为空")
    return ACCOUNT_PROVIDER_CN, token


def encode_service_token(token: str, provider: str) -> str:
    if provider == ACCOUNT_PROVIDER_CN:
        return str(token)
    return json.dumps(
        {"kind": _SERVICE_TOKEN_KIND, "provider": provider, "token": str(token)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_service_token(value: str) -> tuple[str, str]:
    text = str(value or "")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("kind") == _SERVICE_TOKEN_KIND:
        provider = str(payload.get("provider") or "").strip().casefold()
        token = str(payload.get("token") or "")
        if provider not in _PROVIDER_CONFIGS or not token:
            raise ValueError("终末地服务凭据格式无效")
        return provider, token
    return ACCOUNT_PROVIDER_CN, text


def is_asia_role(role: RoleCandidate) -> bool:
    server = f"{role.server_name} {role.server_id}".casefold()
    return any(marker in server for marker in ("asia", "亚洲", "亚服", "亞服", "アジア", "아시아"))


def _extract_account_token(value: str) -> str:
    text = str(value or "").strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for candidate in (
        data.get("content"), data.get("token"), data.get("accountToken"),
        payload.get("content"), payload.get("token"), payload.get("accountToken"),
    ):
        if candidate:
            return str(candidate).strip()
    return text


def _extract_gacha_binding_roles(payload: dict[str, Any]) -> list[RoleCandidate]:
    result: list[RoleCandidate] = []
    data = payload.get("data") or payload
    app_entries = [
        item for item in _walk_dicts(data)
        if str(item.get("appCode") or item.get("app_code") or item.get("gameCode") or "").casefold() == "endfield"
    ]
    bindings: list[dict[str, Any]] = []
    for entry in app_entries:
        bindings.extend(item for item in entry.get("bindingList") or entry.get("binding_list") or [] if isinstance(item, dict))
    if not bindings and isinstance(data, dict):
        bindings = [item for item in data.get("bindingList") or data.get("binding_list") or [] if isinstance(item, dict)]
    for binding in bindings:
        binding_uid = str(binding.get("uid") or "")
        roles = binding.get("roles") or []
        if not roles and (binding.get("roleId") or binding.get("role_id")):
            roles = [binding]
        for role in roles:
            if not isinstance(role, dict):
                continue
            role_id = str(role.get("roleId") or role.get("role_id") or "")
            server_id = str(role.get("serverId") or role.get("server_id") or binding.get("serverId") or "")
            if not binding_uid or not role_id or not server_id:
                continue
            result.append(
                RoleCandidate(
                    binding_uid=binding_uid,
                    role_id=role_id,
                    server_id=server_id,
                    nickname=localized_text(
                        role.get("nickName")
                        or role.get("nickname")
                        or binding.get("nickName")
                        or binding.get("nickname"),
                        default="未命名角色",
                    ),
                    server_name=localized_text(
                        role.get("serverName")
                        or role.get("server_name")
                        or binding.get("serverName")
                    ),
                )
            )
    return _dedupe_roles(result)


def _response_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data: Any = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "items", "result", "poolList", "pools"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _response_has_more(payload: dict[str, Any]) -> bool:
    data = payload.get("data") or {}
    if isinstance(data, dict):
        return bool(data.get("hasMore") or data.get("has_more"))
    return False


def _character_record(role: Any, item: dict[str, Any], pool_type: str, pool_name: str) -> GachaRecord:
    return GachaRecord(
        role_id=role.role_id, server_id=role.server_id,
        pool_id=str(item.get("poolId") or item.get("pool_id") or pool_type),
        pool_name=localized_text(
            item.get("poolName") or item.get("pool_name") or pool_name,
            default=pool_type,
        ),
        pool_type=pool_type, seq_id=str(item.get("seqId")), gacha_ts=_normalize_timestamp(item.get("gachaTs")),
        item_id=str(item.get("charId") or ""), item_name=localized_text(item.get("charName"), default="未知角色"),
        rarity=_normalize_rarity(item.get("rarity")), item_type="角色",
        is_new=_as_bool(item.get("isNew")), is_free=_as_bool(item.get("isFree")),
    )


def _weapon_record(role: Any, item: dict[str, Any], pool_id: str, pool_name: str) -> GachaRecord:
    return GachaRecord(
        role_id=role.role_id, server_id=role.server_id,
        pool_id=str(item.get("poolId") or item.get("pool_id") or pool_id),
        pool_name=localized_text(
            item.get("poolName") or item.get("pool_name") or pool_name,
            default=pool_id,
        ),
        pool_type="weapon", seq_id=str(item.get("seqId")), gacha_ts=_normalize_timestamp(item.get("gachaTs")),
        item_id=str(item.get("weaponId") or ""), item_name=localized_text(item.get("weaponName"), default="未知武器"),
        rarity=_normalize_rarity(item.get("rarity")), item_type="武器",
        weapon_type=localized_text(item.get("weaponType")), is_new=_as_bool(item.get("isNew")),
        is_free=_as_bool(item.get("isFree")),
    )


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _dedupe_roles(roles: list[RoleCandidate]) -> list[RoleCandidate]:
    result: list[RoleCandidate] = []
    seen: set[tuple[str, str]] = set()
    for role in roles:
        key = (role.role_id, role.server_id)
        if not all(key) or key in seen:
            continue
        seen.add(key)
        result.append(role)
    return result


def _normalize_rarity(value: Any) -> int:
    if isinstance(value, str) and not value.strip().isdigit():
        match = re.search(r"(\d+)$", value.strip())
        rarity = int(match.group(1)) if match else 0
    else:
        rarity = _as_int(value)
    return rarity


def _normalize_timestamp(value: Any) -> int:
    timestamp = _as_int(value)
    return timestamp // 1000 if timestamp >= 1_000_000_000_000 else timestamp


def _attendance_award_entries(value: Any) -> list[tuple[Any, dict[str, Any]]]:
    if not isinstance(value, list):
        return []
    entries: list[tuple[Any, dict[str, Any]]] = []
    for award in value:
        entries.append((award, award if isinstance(award, dict) else {}))
    return entries


def _attendance_rewards(
    entries: list[tuple[Any, dict[str, Any]]], resource_maps: list[Any]
) -> list[AttendanceReward]:
    rewards: list[AttendanceReward] = []
    for award, award_details in entries:
        award_id = (
            award_details.get("id")
            or award_details.get("itemId")
            or award_details.get("resourceId")
            or (award if isinstance(award, (str, int)) else "")
        )
        item = _attendance_resource_info((award_id, award_details), resource_maps)
        raw_ids = {
            candidate.casefold()
            for candidate in _attendance_id_candidates(award_id)
        }
        name = _attendance_human_name(
            (
                item.get("name"),
                item.get("itemName"),
                item.get("resourceName"),
                award_details.get("name"),
                award_details.get("itemName"),
            ),
            raw_ids,
        )
        name = (
            name
            or _attendance_akedata_name(item, award_details, award_id)
            or "签到奖励"
        )
        count = _as_int(
            item.get("count")
            or item.get("quantity")
            or award_details.get("count")
            or award_details.get("quantity")
            or 1
        )
        canonical_id = _attendance_akedata_item_id(item, award_details, award_id)
        icon_url = _attendance_icon_url(item, award_details, canonical_id=canonical_id)
        rewards.append(AttendanceReward(name, count, icon_url))
    return rewards


def _attendance_resource_info(award_id: Any, resource_maps: list[Any]) -> dict[str, Any]:
    keys = _attendance_id_candidates(award_id)
    if not keys:
        keys = (str(award_id or ""),)
    merged: dict[str, Any] = {}
    for resource_map in resource_maps:
        if not isinstance(resource_map, dict):
            continue
        for key in keys:
            item = resource_map.get(key)
            if isinstance(item, dict):
                merged.update(item)
    return merged


def _attendance_human_name(values: tuple[Any, ...], raw_ids: set[str]) -> str:
    for value in values:
        text = _attendance_text(value)
        if not text:
            continue
        if (
            text.casefold() in raw_ids
            or text.casefold() in _ATTENDANCE_GENERIC_REWARD_NAMES
            or _attendance_is_internal_id(text)
        ):
            continue
        return text
    return ""


def _attendance_akedata_name(*values: Any) -> str:
    for value in values:
        for candidate in _attendance_id_candidates(value):
            name = _ATTENDANCE_AKEDATA_ITEM_NAMES.get(candidate.casefold())
            if name:
                return name
    return ""


def _attendance_akedata_item_id(*values: Any) -> str:
    """Return a safe AKEData item id, without turning Skland aliases into URLs."""
    for value in values:
        for candidate in _attendance_id_candidates(value):
            normalized = candidate.casefold()
            if normalized in _ATTENDANCE_AKEDATA_ITEM_NAMES:
                return normalized
            if _ATTENDANCE_AKEDATA_ITEM_ID_RE.fullmatch(candidate):
                return candidate
    return ""


def _attendance_icon_url(
    *values: Any,
    canonical_id: str = "",
) -> str:
    """Prefer the service icon, then fall back to the canonical AKEData icon."""
    for value in values:
        icon = _attendance_icon_value(value)
        if icon.startswith(("http://", "https://", "data:")):
            return icon
    if canonical_id:
        return f"{_ATTENDANCE_AKEDATA_ITEM_ICON_BASE}/{canonical_id}.png"
    return ""


def _attendance_icon_value(value: Any, *, _depth: int = 0) -> str:
    if _depth > 3:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("icon", "iconUrl", "iconPath", "image", "imageUrl", "url", "src"):
        icon = _attendance_icon_value(value.get(key), _depth=_depth + 1)
        if icon:
            return icon
    return ""


def _attendance_id_candidates(value: Any, *, _depth: int = 0) -> tuple[str, ...]:
    """Extract possible canonical item ids from a Skland resource object."""
    if _depth > 4:
        return ()

    candidates: list[str] = []

    def add(raw: Any) -> None:
        if isinstance(raw, bool) or raw is None:
            return
        text = str(raw).strip()
        if not text:
            return
        normalized = re.split(r"[?#]", text, maxsplit=1)[0].rstrip("/")
        parts = [text, normalized]
        basename = normalized.rsplit("/", 1)[-1]
        if basename:
            parts.append(basename)
        for part in tuple(parts):
            stem = re.sub(r"\.(?:png|jpe?g|webp|svg)$", "", part, flags=re.IGNORECASE)
            if stem != part:
                parts.append(stem)
        for part in parts:
            if part and part not in candidates:
                candidates.append(part)

    if isinstance(value, (str, int, float)):
        add(value)
    elif isinstance(value, dict):
        for key in (
            "id",
            "itemId",
            "resourceId",
            "item_id",
            "resource_id",
            "iconId",
            "icon",
            "image",
            "url",
            "name",
            "itemName",
            "resourceName",
        ):
            add_values = _attendance_id_candidates(value.get(key), _depth=_depth + 1)
            for candidate in add_values:
                add(candidate)
        for key in ("resource", "item", "resourceInfo", "data"):
            nested = _attendance_id_candidates(value.get(key), _depth=_depth + 1)
            for candidate in nested:
                add(candidate)
    elif isinstance(value, (list, tuple)):
        for child in value:
            for candidate in _attendance_id_candidates(child, _depth=_depth + 1):
                add(candidate)
    return tuple(candidates)


def _attendance_is_internal_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "://" in text or "/" in text or "\\" in text:
        return True
    return bool(re.fullmatch(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+", text.casefold()))


def _attendance_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("zh-cn", "zh_CN", "zh-CN", "zh", "cn", "text", "value", "name"):
            text = _attendance_text(value.get(key))
            if text:
                return text
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


def _sanitize_message(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://\S+", "<URL>", text)
    text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "<PHONE>", text)
    text = re.sub(r"(?i)(token|cred|code|sign|uid)\s*[:=]\s*[^\s,;]+", r"\1=<REDACTED>", text)
    text = re.sub(r"(?<!\d)\d{4,8}(?!\d)", "<NUMBER>", text)
    return text[:160]


def _should_refresh_skland_context(error: EndfieldAPIError) -> bool:
    """Retry only failures that a fresh signing context can actually repair."""
    return error.operation == "社区请求" and str(error.code) in _SKLAND_CONTEXT_RETRY_CODES
