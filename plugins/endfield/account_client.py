from __future__ import annotations

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


for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)


AS_BASE = "https://as.hypergryph.com"
SKLAND_BASE = "https://zonai.skland.com"
BINDING_BASE = "https://binding-api-account-prod.hypergryph.com"
GACHA_BASE = "https://ef-webview.hypergryph.com"
SKLAND_APP_CODE = "4ca99fa6b56cc2ba"
GACHA_APP_CODE = "be36d44aa36bfb5b"
CHARACTER_POOL_TYPES = (
    "E_CharacterGachaPoolType_Special",
    "E_CharacterGachaPoolType_Joint",
    "E_CharacterGachaPoolType_Standard",
    "E_CharacterGachaPoolType_Beginner",
)


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


@dataclass(frozen=True, slots=True)
class AttendanceResult:
    status: str
    message: str
    rewards: tuple[AttendanceReward, ...] = ()


@dataclass(frozen=True, slots=True)
class GachaPage:
    records: tuple[GachaRecord, ...]
    has_more: bool
    next_seq_id: str


@dataclass(slots=True)
class _SklandContext:
    cred: str
    sign_token: str
    server_time: int
    client_time: int
    expires_at: float


class EndfieldOfficialClient:
    def __init__(self, http: httpx.AsyncClient | None = None, *, timeout: float = 25.0):
        self.http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False)
        self._owns_http = http is None
        self._skland_cache: dict[str, _SklandContext] = {}
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
        try:
            payload = await self._signed_skland_request(
                context, "POST", "/web/v1/game/endfield/attendance", raw_body="", extra_headers=headers
            )
        except EndfieldAPIError as exc:
            if exc.code in {"10001", "10002", "10012", "10013"} or "已签到" in str(exc):
                return AttendanceResult("already", "今日已签到")
            raise
        data = payload.get("data") or {}
        resource_map = data.get("resourceInfoMap") or {}
        rewards: list[AttendanceReward] = []
        for award in data.get("awardIds") or []:
            award_details = award if isinstance(award, dict) else {}
            award_id = award_details.get("id") or award_details.get("itemId") or award_details.get("resourceId") or award
            item = resource_map.get(str(award_id)) or {}
            rewards.append(
                AttendanceReward(
                    str(item.get("name") or item.get("itemName") or award_details.get("name") or award_id),
                    _as_int(
                        item.get("count")
                        or item.get("quantity")
                        or award_details.get("count")
                        or award_details.get("quantity")
                        or 1
                    ),
                )
            )
        return AttendanceResult("success", "签到成功", tuple(rewards))

    async def get_gacha_roles(self, account_token: str) -> list[RoleCandidate]:
        oauth_token = await self._oauth_token(account_token, GACHA_APP_CODE, grant_type=1)
        payload = await self._json_request(
            "查询终末地账号", "GET", f"{BINDING_BASE}/account/binding/v1/binding_list",
            params={"token": oauth_token, "appCode": "endfield"},
        )
        return _extract_gacha_binding_roles(payload)

    async def get_u8_token(self, account_token: str, binding_uid: str) -> str:
        fingerprint = hashlib.sha256(account_token.encode("utf-8")).hexdigest()[:24]
        cache_key = (fingerprint, binding_uid)
        cached = self._u8_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        oauth_token = await self._oauth_token(account_token, GACHA_APP_CODE, grant_type=1)
        payload = await self._json_request(
            "获取抽卡凭据", "POST", f"{BINDING_BASE}/account/binding/v1/u8_token_by_uid",
            json_body={"token": oauth_token, "uid": binding_uid},
        )
        data = payload.get("data") or {}
        token = str(data.get("token") or data.get("u8Token") or data.get("u8_token") or "")
        if not token:
            raise EndfieldAPIError("获取抽卡凭据", message="官方接口未返回 U8 凭据")
        self._u8_cache[cache_key] = (token, time.monotonic() + 540)
        return token

    async def character_pool_names(self, u8_token: str, server_id: str) -> dict[str, str]:
        payload = await self._json_request(
            "查询角色卡池", "GET", f"{GACHA_BASE}/api/record/char/pool",
            params={"lang": "zh-cn", "token": u8_token, "server_id": server_id},
        )
        result: dict[str, str] = {}
        for item in _response_items(payload):
            pool_type = str(item.get("poolType") or item.get("pool_type") or item.get("type") or "")
            name = str(item.get("poolName") or item.get("pool_name") or item.get("name") or pool_type)
            if pool_type:
                result[pool_type] = name
        return result

    async def weapon_pools(self, u8_token: str, server_id: str) -> list[tuple[str, str]]:
        payload = await self._json_request(
            "查询武器卡池", "GET", f"{GACHA_BASE}/api/record/weapon/pool",
            params={"lang": "zh-cn", "token": u8_token, "server_id": server_id},
        )
        result: list[tuple[str, str]] = []
        for item in _response_items(payload):
            pool_id = str(item.get("poolId") or item.get("pool_id") or item.get("id") or "")
            if pool_id:
                result.append((pool_id, str(item.get("poolName") or item.get("pool_name") or item.get("name") or pool_id)))
        return result

    async def character_records(
        self, role: Any, u8_token: str, pool_type: str, *, seq_id: str = "", pool_name: str = ""
    ) -> GachaPage:
        params = {"lang": "zh-cn", "pool_type": pool_type, "token": u8_token, "server_id": role.server_id}
        if seq_id:
            params["seq_id"] = seq_id
        payload = await self._json_request("同步角色抽卡", "GET", f"{GACHA_BASE}/api/record/char", params=params)
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
        params = {"lang": "zh-cn", "token": u8_token, "server_id": role.server_id}
        if pool_id:
            params["pool_id"] = pool_id
        if seq_id:
            params["seq_id"] = seq_id
        payload = await self._json_request("同步武器抽卡", "GET", f"{GACHA_BASE}/api/record/weapon", params=params)
        items = _response_items(payload)
        records = tuple(
            _weapon_record(role, item, pool_id, pool_name)
            for item in items
            if item.get("seqId") is not None and item.get("weaponId")
        )
        next_seq_id = str(items[-1].get("seqId") or "") if items else ""
        return GachaPage(records, _response_has_more(payload), next_seq_id)

    async def _skland_context(self, account_token: str, *, refresh: bool = False) -> _SklandContext:
        key = hashlib.sha256(account_token.encode("utf-8")).hexdigest()[:24]
        cached = self._skland_cache.get(key)
        if cached and not refresh and cached.expires_at > time.monotonic():
            return cached
        oauth_code = await self._oauth_token(account_token, SKLAND_APP_CODE, grant_type=0, field="code")
        credential_payload = await self._json_request(
            "获取森空岛凭据", "POST", f"{SKLAND_BASE}/api/v1/user/auth/generate_cred_by_code",
            json_body={"code": oauth_code, "kind": 1},
        )
        cred = str((credential_payload.get("data") or {}).get("cred") or "")
        if not cred:
            raise EndfieldAPIError("获取森空岛凭据", message="官方接口未返回 cred")
        refresh_payload = await self._json_request(
            "刷新森空岛签名", "GET", f"{SKLAND_BASE}/web/v1/auth/refresh", headers={"cred": cred}
        )
        data = refresh_payload.get("data") or {}
        sign_token = str(data.get("token") or "")
        if not sign_token:
            raise EndfieldAPIError("刷新森空岛签名", message="官方接口未返回签名凭据")
        now = int(time.time())
        context = _SklandContext(
            cred=cred,
            sign_token=sign_token,
            server_time=_as_int(refresh_payload.get("timestamp") or now),
            client_time=now,
            expires_at=time.monotonic() + 540,
        )
        self._skland_cache[key] = context
        return context

    async def _oauth_token(
        self, account_token: str, app_code: str, *, grant_type: int, field: str = "token"
    ) -> str:
        payload = await self._json_request(
            "账号授权", "POST", f"{AS_BASE}/user/oauth2/v2/grant",
            json_body={"appCode": app_code, "token": account_token, "type": grant_type},
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
            "Origin": "https://game.skland.com",
            "Referer": "https://game.skland.com/",
        }
        headers.update(extra_headers or {})
        return await self._json_request(
            "森空岛请求", method, f"{SKLAND_BASE}{path}", params=params, headers=headers,
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
    ) -> dict[str, Any]:
        try:
            response = await self.http.request(
                method, url, params=params, json=json_body, headers=headers, content=content
            )
        except httpx.HTTPError:
            raise EndfieldAPIError(operation, message="网络请求失败") from None
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
        if status not in (None, 0, "0"):
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
                        nickname=str(role.get("nickname") or role.get("nickName") or binding.get("nickName") or "未命名角色"),
                        server_name=str(role.get("serverName") or role.get("serverType") or binding.get("channelName") or ""),
                    )
                )
    return _dedupe_roles(candidates)


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
                    nickname=str(role.get("nickName") or role.get("nickname") or binding.get("nickName") or binding.get("nickname") or "未命名角色"),
                    server_name=str(role.get("serverName") or role.get("server_name") or binding.get("serverName") or ""),
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
        pool_name=str(item.get("poolName") or item.get("pool_name") or pool_name or pool_type),
        pool_type=pool_type, seq_id=str(item.get("seqId")), gacha_ts=_normalize_timestamp(item.get("gachaTs")),
        item_id=str(item.get("charId") or ""), item_name=str(item.get("charName") or "未知角色"),
        rarity=_normalize_rarity(item.get("rarity")), item_type="角色",
        is_new=_as_bool(item.get("isNew")), is_free=_as_bool(item.get("isFree")),
    )


def _weapon_record(role: Any, item: dict[str, Any], pool_id: str, pool_name: str) -> GachaRecord:
    return GachaRecord(
        role_id=role.role_id, server_id=role.server_id,
        pool_id=str(item.get("poolId") or item.get("pool_id") or pool_id),
        pool_name=str(item.get("poolName") or item.get("pool_name") or pool_name or pool_id),
        pool_type="weapon", seq_id=str(item.get("seqId")), gacha_ts=_normalize_timestamp(item.get("gachaTs")),
        item_id=str(item.get("weaponId") or ""), item_name=str(item.get("weaponName") or "未知武器"),
        rarity=_normalize_rarity(item.get("rarity")), item_type="武器",
        weapon_type=str(item.get("weaponType") or ""), is_new=_as_bool(item.get("isNew")),
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
