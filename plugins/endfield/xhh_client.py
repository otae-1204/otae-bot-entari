from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import time
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from .account_store import XhhGachaImport, XhhGachaPool, XhhSixStar


LOGIN_URL = "https://login.xiaoheihe.cn/"
API_BASE = "https://api.xiaoheihe.cn"
SEND_CODE_PATH = "/account/get_login_code/"
LOGIN_CODE_PATH = "/account/login_code/"
OVERVIEW_PATH = "/game/endfield/player/overview"
HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
PHONE_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDZgjVwAiKTjZ55nG+mW6r3TSU4
ECvNYqDMIS/bhCj2QaH5GI/KZb2TBp+CBvUj9SLFnmJQ0kzHzHoGZCQ88VevCffF
7JePGF9cmKQqotlfTKbV4oxV5iLz7JSG6b/Vg7AXtrTolNtWsa8HiB0tI0YClYaQ
lOXm4UxLeSxQwSFETwIDAQAB
-----END PUBLIC KEY-----"""


class XhhAPIError(RuntimeError):
    pass


class XhhLoginSession:
    def __init__(self, phone: str):
        self._phone = phone
        self._playwright = None
        self._browser = None
        self._context = None
        self._common_params: dict[str, str] = {}
        self._heybox_id = ""

    @classmethod
    async def start(cls, phone: str) -> "XhhLoginSession":
        session = cls(phone)
        try:
            await session._open()
            await session._send_code()
            return session
        except Exception:
            await session.close()
            raise

    async def login_and_fetch(self, code: str) -> XhhGachaImport:
        if self._context is None:
            raise XhhAPIError("小黑盒登录会话已失效，请重新执行导入。")
        encrypted_phone = _encrypt_phone(f"+86{self._phone}")
        payload = await self._request(
            "POST", LOGIN_CODE_PATH, query={"code": code}, form={"phone_num": encrypted_phone}
        )
        result = _require_ok(payload, "小黑盒验证码登录失败")
        self._heybox_id = str(
            _first(_as_dict(result.get("profile")), "heybox_id", "user_id")
            or _first(_as_dict(result.get("account_detail")), "userid", "user_id")
            or result.get("heybox_id")
            or ""
        ).strip()
        pkey = str(result.get("pkey") or "").strip()
        if not self._heybox_id or not pkey:
            raise XhhAPIError("小黑盒登录状态不完整，请稍后重试。")
        expires = int(time.time()) + 30 * 60
        await self._context.add_cookies(
            [
                {"name": name, "value": value, "domain": ".xiaoheihe.cn", "path": "/", "expires": expires}
                for name, value in (
                    ("heybox_id", self._heybox_id),
                    ("user_heybox_id", self._heybox_id),
                    ("x_xhh_heyboxid", self._heybox_id),
                    ("pkey", pkey),
                    ("user_pkey", pkey),
                )
            ]
        )
        overview = await self._request("GET", OVERVIEW_PATH)
        parsed = parse_xhh_overview(overview)
        if not parsed.source_uid:
            raise XhhAPIError("小黑盒数据缺少终末地 UID，无法安全导入。")
        return parsed

    async def close(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = self._browser = self._playwright = None
        for resource in (context, browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        self._phone = ""
        self._common_params.clear()
        self._heybox_id = ""

    async def _open(self) -> None:
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await _launch_xhh_browser(self._playwright)
            self._context = await self._browser.new_context()
            page = await self._context.new_page()
            restore_params: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

            def capture_request(request) -> None:
                if "/account/restore_login" not in request.url or restore_params.done():
                    return
                values = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
                for key in ("hkey", "_time", "nonce", "heybox_id"):
                    values.pop(key, None)
                restore_params.set_result({str(key): str(value) for key, value in values.items()})

            page.on("request", capture_request)
            await page.add_init_script("window.prompt = () => ''; ")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                self._common_params = await asyncio.wait_for(restore_params, timeout=10)
            except TimeoutError:
                self._common_params = {
                    "app": "heybox",
                    "os_type": "web",
                    "x_app": "heybox_website",
                    "x_client_type": "weboutapp",
                    "x_os_type": "Windows",
                    "web_version": "",
                    "device_id": secrets.token_hex(16),
                }
            for _ in range(40):
                cookies = await self._context.cookies(API_BASE)
                if any(cookie.get("name") == "x_xhh_tokenid" for cookie in cookies):
                    break
                await page.wait_for_timeout(250)
            else:
                raise XhhAPIError("小黑盒设备验证初始化失败，请稍后重试。")
        except XhhAPIError:
            raise
        except Exception as exc:
            raise XhhAPIError("无法启动小黑盒安全登录环境，请检查 Playwright 浏览器组件。") from exc

    async def _send_code(self) -> None:
        payload = await self._request(
            "POST", SEND_CODE_PATH, form={"phone_num": _encrypt_phone(f"+86{self._phone}")}
        )
        _require_ok(payload, "小黑盒验证码发送失败")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._context is None:
            raise XhhAPIError("小黑盒登录会话尚未初始化。")
        timestamp = int(time.time())
        nonce = secrets.token_hex(16).upper()
        params = dict(self._common_params)
        params.update(
            {
                "version": "999.0.4",
                "hkey": make_xhh_hkey(path, timestamp, nonce),
                "_time": str(timestamp),
                "nonce": nonce,
            }
        )
        if self._heybox_id:
            params["heybox_id"] = self._heybox_id
        if query:
            params.update(query)
        try:
            response = await self._context.request.fetch(
                f"{API_BASE}{path}", method=method, params=params, form=form,
                headers={"Origin": LOGIN_URL.rstrip("/"), "Referer": LOGIN_URL}, timeout=30_000,
            )
            payload = await response.json()
        except Exception as exc:
            raise XhhAPIError("小黑盒官方接口请求失败，请稍后重试。") from exc
        if not isinstance(payload, dict):
            raise XhhAPIError("小黑盒官方接口返回了无法识别的数据。")
        return payload


def make_xhh_hkey(path: str, timestamp: int, nonce: str) -> str:
    normalized = "/" + "/".join(part for part in path.split("/") if part) + "/"
    parts = (
        _map_chars(str(int(timestamp) + 1), HKEY_ALPHABET[:-2]),
        _map_chars(normalized, HKEY_ALPHABET),
        _map_chars(str(nonce), HKEY_ALPHABET),
    )
    mixed = "".join(
        part[index]
        for index in range(max(len(part) for part in parts))
        for part in parts
        if index < len(part)
    )[:20]
    digest = hashlib.md5(mixed.encode("utf-8")).hexdigest()
    values = [ord(char) for char in digest[-6:]]
    original = values[:4]
    values[:4] = [
        _ls(original[0]) ^ _yo(original[1]) ^ _ti(original[2]) ^ _wn(original[3]),
        _wn(original[0]) ^ _ls(original[1]) ^ _yo(original[2]) ^ _ti(original[3]),
        _ti(original[0]) ^ _wn(original[1]) ^ _ls(original[2]) ^ _yo(original[3]),
        _yo(original[0]) ^ _ti(original[1]) ^ _wn(original[2]) ^ _ls(original[3]),
    ]
    return _map_chars(digest[:5], HKEY_ALPHABET[:-4]) + str(sum(values) % 100).zfill(2)


async def _launch_xhh_browser(playwright):
    options = {"headless": True, "args": ["--no-proxy-server"]}
    try:
        return await playwright.chromium.launch(**options)
    except Exception:
        try:
            return await playwright.chromium.launch(channel="msedge", **options)
        except Exception as edge_error:
            raise XhhAPIError(
                "无法启动小黑盒安全登录环境；Playwright Chromium 和系统 Edge 均不可用。"
            ) from edge_error


def parse_xhh_overview(payload: object) -> XhhGachaImport:
    root = _as_dict(payload)
    if root.get("status") and root.get("status") != "ok":
        raise XhhAPIError(_safe_api_message(root.get("msg"), "小黑盒历史数据获取失败。"))
    data = _as_dict(root.get("result")) or root
    if data.get("is_bind") is False:
        raise XhhAPIError("该小黑盒账号未绑定终末地数据。")
    user_info = _as_dict(data.get("user_info") or data.get("player_info") or data.get("role_info"))
    source_uid = str(_first(user_info, "uid", "game_uid", "role_id", "roleId") or "").strip()
    nickname = str(_first(user_info, "nickname", "nick_name", "name") or "").strip()
    raw_records = data.get("gacha_record") or data.get("gacha_records") or data.get("pool_list") or []
    candidates = _collect_pool_dicts(raw_records)
    pools: list[XhhGachaPool] = []
    six_stars: list[XhhSixStar] = []
    pool_order: dict[str, int] = {}
    for index, raw_pool in enumerate(candidates):
        pool_id = str(_first(raw_pool, "pool_id", "poolId", "gacha_pool_id", "id") or "").strip()
        if not pool_id or pool_id in pool_order:
            continue
        pool_order[pool_id] = index
        pool_name = str(_first(raw_pool, "pool_name", "poolName", "name", "title") or pool_id).strip()
        item_type = _infer_item_type(raw_pool, pool_id, pool_name)
        pool_type = str(_first(raw_pool, "pool_type", "poolType", "gacha_type", "category") or "").strip()
        pool_type = pool_type or _infer_pool_type(pool_id, pool_name, item_type)
        total_count = _first_int(
            raw_pool, "total_count", "total", "count", "gacha_count", "draw_count", "total_gacha_num"
        )
        current_count = _first_int(
            raw_pool, "current_count", "current", "pity", "pity_count", "no_six_count",
            "guarantee_count", "cur_count", "current_num", "last_diff", "current_diff",
            "cur_diff", "last_pity", "last_count",
        )
        free_count = _first_int(
            raw_pool, "free_count", "free_pull_count", "gift_count", "free_draw_count",
            "free_gacha_count",
        )
        is_current = _as_bool(_first(raw_pool, "is_current", "current_pool", "isCurrent"))
        raw_six = _first(
            raw_pool, "six_star_record", "six_star_records", "six_star_list", "six_list",
            "six_records", "star6_list", "six_detail_list", "six_star_detail",
            "six_star_info", "six_star_data", "star6_record", "history",
        )
        discovered_six = _discover_six_star_records(raw_pool)
        if discovered_six:
            raw_six = discovered_six
        parsed_six = _parse_six_stars(pool_id, pool_name, item_type, raw_six, total_count, current_count)
        latest_ts = max(
            _parse_timestamp(_first(raw_pool, "latest_time", "update_time", "last_time", "end_time")),
            max((item.gacha_ts for item in parsed_six), default=0),
        )
        pools.append(
            XhhGachaPool(
                pool_id=pool_id, pool_name=pool_name, pool_type=pool_type, item_type=item_type,
                total_count=max(total_count, len(parsed_six)), current_count=current_count,
                free_count=free_count, latest_ts=latest_ts, is_current=is_current,
                sort_order=len(pools),
            )
        )
        six_stars.extend(parsed_six)
    if not pools:
        raise XhhAPIError("小黑盒返回数据中未找到终末地卡池记录。")
    if not source_uid:
        raise XhhAPIError("小黑盒数据缺少终末地 UID，无法安全导入。")
    pools = _mark_current_pools(pools, pool_order)
    statistic = _as_dict(data.get("statistic_info") or data.get("statistics"))
    total_count = _first_int(statistic, "total_count", "total", "gacha_count", "draw_count")
    if not total_count:
        total_count = sum(item.total_count for item in pools)
    return XhhGachaImport(
        source_uid=source_uid, nickname=nickname, total_count=total_count,
        imported_at=int(time.time()), pools=tuple(pools), six_stars=tuple(six_stars),
    )


def _parse_six_stars(
    pool_id: str,
    pool_name: str,
    item_type: str,
    raw_value: object,
    total_count: int,
    current_count: int,
) -> list[XhhSixStar]:
    raw_items = []
    for item in _walk_dicts(raw_value):
        if not _first(item, "name", "item_name", "char_name", "weapon_name"):
            continue
        rarity = _first_int(item, "rarity", "star", "star_level", "rank")
        if rarity and rarity < 6:
            continue
        raw_items.append(item)
    provisional: list[tuple[int, dict[str, Any], int, int]] = []
    for index, item in enumerate(raw_items):
        timestamp = _parse_timestamp(_first(item, "date", "gacha_date", "time", "gacha_time", "created_at"))
        interval = _first_int(item, "diff", "interval", "count", "gacha_count", "pity", "pull_count")
        provisional.append((index, item, timestamp, interval))
    if any(timestamp for _, _, timestamp, _ in provisional):
        provisional.sort(key=lambda value: (value[2] or 0, -value[0]))
    else:
        provisional.reverse()
    offset = max(0, total_count - current_count - sum(item[3] for item in provisional))
    running_position = offset
    result: list[XhhSixStar] = []
    for ordinal, (_, item, timestamp, interval) in enumerate(provisional, 1):
        running_position += interval
        position = _first_int(item, "pool_position", "position", "draw_position") or running_position
        name = str(_first(item, "name", "item_name", "char_name", "weapon_name") or "").strip()
        item_id = str(_first(item, "item_id", "itemId", "char_id", "weapon_id") or "").strip()
        miss_up = _as_bool(_first(item, "miss_up", "is_miss", "miss", "crooked"))
        is_free = _as_bool(
            _first(
                item, "is_free", "free", "is_free_pull", "free_pull",
                "is_gift", "gift", "__xhh_is_free",
            )
        )
        raw_key = f"{pool_id}|{name}|{timestamp}|{interval}|{int(miss_up)}|{ordinal}"
        result.append(
            XhhSixStar(
                pool_id=pool_id, unique_key=hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32],
                item_name=name, item_type=item_type, gacha_ts=timestamp, interval=interval,
                pool_position=position, item_id=item_id, miss_up=miss_up,
                is_free=is_free,
            )
        )
    return list(reversed(result))


def _discover_six_star_records(raw_pool: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    signal_keys = {
        "date", "gacha_date", "time", "gacha_time", "created_at",
        "diff", "interval", "gacha_count", "pity", "pull_count",
        "miss_up", "is_miss", "miss", "crooked",
        "pool_position", "position", "draw_position",
    }
    for path, item in _walk_dicts_with_path(raw_pool):
        if item is raw_pool:
            continue
        if not _first(item, "name", "item_name", "char_name", "weapon_name"):
            continue
        rarity = _first_int(item, "rarity", "star", "star_level", "rank")
        if rarity and rarity < 6:
            continue
        if rarity >= 6 or any(key in item for key in signal_keys):
            copied = dict(item)
            path_identity = " ".join(path).casefold()
            if any(marker in path_identity for marker in ("free", "gift", "gratis", "免费", "赠送")):
                copied["__xhh_is_free"] = True
            result.append(copied)
    return result


def _walk_dicts_with_path(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_dicts_with_path(child, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk_dicts_with_path(child, (*path, str(index)))


def _collect_pool_dicts(value: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _walk_dicts(value):
        if _first(item, "pool_id", "poolId", "gacha_pool_id") is not None:
            result.append(item)
    return result


def _walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_dicts(child)


def _mark_current_pools(pools: list[XhhGachaPool], order: dict[str, int]) -> list[XhhGachaPool]:
    result = list(pools)
    for item_type in {item.item_type for item in pools}:
        indexes = [index for index, item in enumerate(result) if item.item_type == item_type]
        if any(result[index].is_current for index in indexes):
            continue
        current_index = max(
            indexes,
            key=lambda index: (result[index].latest_ts, -order.get(result[index].pool_id, index)),
        )
        result[current_index] = replace(result[current_index], is_current=True)
    return result


def _require_ok(payload: dict[str, Any], fallback: str) -> dict[str, Any]:
    if payload.get("status") == "ok":
        return _as_dict(payload.get("result"))
    status = str(payload.get("status") or "")
    if status == "show_captcha":
        raise XhhAPIError("小黑盒要求完成图形验证，本次自动导入无法继续，请稍后重试。")
    if status == "need_google_check":
        raise XhhAPIError("该小黑盒账号启用了二次验证，暂不支持自动导入。")
    raise XhhAPIError(_safe_api_message(payload.get("msg"), fallback))


def _safe_api_message(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"1\d{10}", "[手机号]", text)
    text = re.sub(r"(?i)(token|cookie|pkey|code)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    return text[:160] if text else fallback


def _encrypt_phone(value: str) -> str:
    cipher = PKCS1_v1_5.new(RSA.import_key(PHONE_PUBLIC_KEY))
    return base64.b64encode(cipher.encrypt(value.encode("utf-8"))).decode("ascii")


def _map_chars(value: str, alphabet: str) -> str:
    return "".join(alphabet[ord(char) % len(alphabet)] for char in value)


def _bh(value: int) -> int:
    return ((value << 1) ^ 27) & 255 if value & 128 else value << 1


def _wn(value: int) -> int:
    return _bh(value) ^ value


def _ti(value: int) -> int:
    return _wn(_bh(value))


def _yo(value: int) -> int:
    return _ti(_wn(_bh(value)))


def _ls(value: int) -> int:
    return _yo(value) ^ _ti(value) ^ _wn(value)


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], *keys: str):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _first_int(mapping: dict[str, Any], *keys: str) -> int:
    value = _first(mapping, *keys)
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是"}


def _parse_timestamp(value: object) -> int:
    if value is None or value == "":
        return 0
    try:
        number = int(float(str(value)))
        return number // 1000 if number > 10_000_000_000 else max(0, number)
    except (TypeError, ValueError):
        pass
    text = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text[:19], fmt).timestamp())
        except ValueError:
            continue
    return 0


def _infer_item_type(raw: dict[str, Any], pool_id: str, pool_name: str) -> str:
    value = str(_first(raw, "item_type", "itemType", "gacha_type", "pool_category", "type") or "")
    identity = f"{value} {pool_id} {pool_name}".casefold()
    if any(marker in identity for marker in ("weapon", "wepon", "wpn", "武器", "申领")):
        return "武器"
    return "角色"


def _infer_pool_type(pool_id: str, pool_name: str, item_type: str) -> str:
    identity = f"{pool_id} {pool_name}".casefold()
    if item_type == "武器":
        return "weapon"
    if "joint" in identity or "庆典" in identity:
        return "E_CharacterGachaPoolType_Joint"
    if "standard" in identity or "constant" in identity or "基础" in identity or "常驻" in identity:
        return "E_CharacterGachaPoolType_Standard"
    if "beginner" in identity or "启程" in identity or "新手" in identity:
        return "E_CharacterGachaPoolType_Beginner"
    return "E_CharacterGachaPoolType_Special"
