"""请求处理插件 - 自动同意好友申请，群邀请交由超级用户审批。"""

import asyncio
from typing import Any, Dict

import httpx
from utils.entari_native import listen_notice, listen_message
from arclet.entari import Account as Bot, Event
from utils.entari_native import Pred
from loguru import logger
from utils.entari_native import ChainMsg, SendDest, event_chain, event_plain_text, event_user_id, account_adapter_name

from configs.config import Config, _env
from .ark import parse_ark_invite_segment

superuser = str(Config.SUPERUSERS[0]) if Config.SUPERUSERS else ""

# 待审批的群邀请
# {key: {guild_name, user_name, group_code, raw_data}}
_pending: Dict[str, dict] = {}
_lock = asyncio.Lock()

ONEBOT_HTTP_URL = str(_env("ONEBOT_HTTP_URL", "") or _env("LLONEBOT_HTTP_URL", "") or "").rstrip("/")
ONEBOT_ACCESS_TOKEN = str(_env("ONEBOT_ACCESS_TOKEN", "") or "")


def _parse_ark_invite(seg) -> dict | None:
    """解析 Ark 群邀请卡片，返回 {group_code, group_name, inviter_uin, msgseq, token} 或 None。"""
    return parse_ark_invite_segment(seg)


def _result_data(result: Any) -> Any:
    """兼容 OneBot 风格 {status, data} 和直接 data 返回。"""
    if isinstance(result, dict):
        if "data" in result:
            return result["data"]
    return result


async def _try_internal(bot: Bot, action: str, **params) -> Any:
    logger.debug(f"[request_handler] 灏濊瘯 API {action}: {params}")
    return await bot.internal(action=action, **params)


def _onebot_base_urls() -> list[str]:
    urls: list[str] = []
    if ONEBOT_HTTP_URL:
        urls.append(ONEBOT_HTTP_URL)

    clients = _env("SATORI_CLIENTS", [])
    if isinstance(clients, list):
        for client in clients:
            if not isinstance(client, dict):
                continue
            host = client.get("host") or client.get("hostname")
            port = client.get("port")
            if host and port:
                url = f"http://{host}:{port}".rstrip("/")
                if url not in urls:
                    urls.append(url)
    return urls


def _onebot_token() -> str:
    if ONEBOT_ACCESS_TOKEN:
        return ONEBOT_ACCESS_TOKEN
    clients = _env("SATORI_CLIENTS", [])
    if isinstance(clients, list) and clients and isinstance(clients[0], dict):
        return str(clients[0].get("token", "") or "")
    return ""


async def _try_onebot_http(action: str, **params) -> Any:
    """直连 OneBot HTTP API，绕过 Satori internal 404。"""
    urls = _onebot_base_urls()
    if not urls:
        raise RuntimeError("未配置 ONEBOT_HTTP_URL")

    headers = {"Content-Type": "application/json"}
    token = _onebot_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for base in urls:
            for path in (f"/{action}", f"/api/{action}"):
                url = f"{base}{path}"
                methods = ("GET", "POST") if action.startswith("get_") else ("POST",)
                for method in methods:
                    try:
                        if method == "GET":
                            resp = await client.get(url, params=params, headers=headers)
                        else:
                            resp = await client.post(url, json=params, headers=headers)
                        if resp.status_code == 404:
                            errors.append(f"{method} {base}{path}: 404")
                            continue
                        resp.raise_for_status()
                        try:
                            data = resp.json()
                        except ValueError:
                            data = resp.text
                        if isinstance(data, dict) and data.get("status") == "failed":
                            raise RuntimeError(data.get("wording") or data.get("message") or data)
                        logger.debug(f"[request_handler] OneBot HTTP {action} 鎴愬姛: {method} {base}{path}")
                        return data
                    except Exception as e:
                        errors.append(f"{method} {base}{path}: {e}")
                        continue
    raise RuntimeError("; ".join(errors[-4:]))


async def _try_api(bot: Bot, action: str, **params) -> Any:
    """先试 Satori internal，再试 OneBot HTTP 直连。"""
    try:
        return await _try_internal(bot, action, **params)
    except Exception as internal_exc:
        try:
            return await _try_onebot_http(action, **params)
        except Exception as http_exc:
            raise RuntimeError(f"internal: {internal_exc}; onebot_http: {http_exc}") from http_exc


def _same_id(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and str(a) == str(b)


def _iter_system_requests(data: Any) -> list[tuple[str, dict]]:
    if not isinstance(data, dict):
        return []
    result: list[tuple[str, dict]] = []
    for key in ("invited_requests", "InvitedRequest", "invitedRequests"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                result.append(("invite", item))
    for key in ("join_requests", "JoinRequest", "joinRequests"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                result.append(("add", item))
    return result


def _request_is_unchecked(item: dict) -> bool:
    if "checked" not in item and "actor" not in item:
        return True
    return not item.get("checked") or str(item.get("actor", "0")) == "0"


def _match_request(item: dict, group_code: str, user_id: str = "") -> bool:
    group_candidates = (
        item.get("group_id"),
        item.get("group_code"),
        item.get("group"),
    )
    if group_code and not any(_same_id(v, group_code) for v in group_candidates):
        return False
    if user_id:
        candidates = (
            item.get("user_id"),
            item.get("invitor_uin"),
            item.get("requester_uin"),
            item.get("inviter_uin"),
        )
        if not any(_same_id(v, user_id) for v in candidates):
            return False
    return True


def _iter_request_dicts(data: Any) -> list[dict]:
    """从 list 或包装 dict 中取出请求条目。"""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if not isinstance(data, dict):
        return []
    result: list[dict] = []
    for value in data.values():
        if isinstance(value, list):
            result.extend(i for i in value if isinstance(i, dict))
    return result


async def _fill_onebot_request(bot: Bot, info: dict) -> dict:
    """从 LLOneBot 的请求列表中补齐 flag/request_id/sub_type。"""
    group_code = str(info.get("group_code", ""))
    inviter = str(info.get("user_name", "") or info.get("inviter_uin", ""))
    info.setdefault("probe_errors", [])
    info.setdefault("probe_summary", [])

    if not info.get("request_id"):
        try:
            raw = await _try_api(bot, "get_group_system_msg")
            all_items = [
                (sub_type, item)
                for sub_type, item in _iter_system_requests(_result_data(raw))
            ]
            unchecked_items = [(s, i) for s, i in all_items if _request_is_unchecked(i)]
            sample = ", ".join(
                f"{sub}:{item.get('group_id') or item.get('group_code')}/{item.get('request_id') or item.get('flag')}"
                for sub, item in all_items[:5]
            )
            info["probe_summary"].append(
                f"get_group_system_msg: {len(all_items)} 条，未处理 {len(unchecked_items)} 条"
                + (f"，样例 {sample}" if sample else "")
            )
            for candidates in (unchecked_items, all_items):
                for target_user in (inviter, ""):
                    for sub_type, item in candidates:
                        if _match_request(item, group_code, target_user):
                            request_id = item.get("request_id") or item.get("flag")
                            if request_id:
                                info["request_id"] = str(request_id)
                                if not info.get("flag"):
                                    info["flag"] = str(request_id)
                                info["sub_type"] = sub_type
                                info["group_name"] = item.get("group_name") or info.get("group_name", "")
                                state = "unchecked" if _request_is_unchecked(item) else "checked"
                                logger.info(
                                    f"从 get_group_system_msg 匹配到群邀请 request_id: {request_id} ({state})"
                                )
                                return info
        except Exception as e:
            msg = f"get_group_system_msg: {e}"
            info["probe_errors"].append(msg)
            logger.debug(f"{msg}")

    if not info.get("flag"):
        try:
            raw = await _try_api(bot, "get_group_ignore_add_request")
            items = _iter_request_dicts(_result_data(raw))
            info["probe_summary"].append(f"get_group_ignore_add_request: {len(items)} 条")
            for target_user in (inviter, ""):
                for item in items:
                    if _match_request(item, group_code, target_user):
                        info["flag"] = str(item.get("flag", ""))
                        info["sub_type"] = item.get("sub_type") or "invite"
                        logger.info(f"从 get_group_ignore_add_request 匹配到群邀请 flag: {info['flag']}")
                        return info
        except Exception as e:
            msg = f"get_group_ignore_add_request: {e}"
            info["probe_errors"].append(msg)
            logger.debug(f"{msg}")

    return info


# 好友申请 - 自动同意

async def _is_friend_request(event: Event) -> bool:
    return event.__class__.__name__ == "FriendRequestEvent"


friend_req = listen_notice(rule=Pred(_is_friend_request), priority=5, block=True)


@friend_req.handle()
async def handle_friend_request(event: Event, bot: Bot):
    try:
        msg_id = event.message.id if event.message else str(event.sn)
        await bot.friend_approve(request_id=msg_id, approve=True, comment="")
        name = (event.user.name if event.user else None) or msg_id
        logger.info(f"已自动通过好友申请: {name}")
    except Exception as e:
        logger.error(f"好友申请处理失败: {e}")
    await friend_req.finish()


# 群邀请（Satori request 事件）- 通知超级用户

async def _is_guild_request(event: Event) -> bool:
    return event.__class__.__name__ in {"GuildRequestEvent", "GuildMemberRequestEvent"}


guild_req = listen_notice(rule=Pred(_is_guild_request), priority=5, block=True)


@guild_req.handle()
async def handle_guild_request(event: Event, bot: Bot):
    if not superuser:
        logger.warning("未配置 SUPERUSERS，无法审批群邀请")
        await guild_req.finish()
        return

    guild_name = event.guild.name if event.guild else "未知群"
    inviter = getattr(event, "operator", None) or event.user
    user_name = inviter.name if inviter else "未知用户"
    msg_id = event.message.id if event.message else str(event.sn)
    event_type = "member" if event.__class__.__name__ == "GuildMemberRequestEvent" else "guild"

    async with _lock:
        _pending[msg_id] = {
            "guild_name": guild_name,
            "user_name": user_name,
            "api_type": "satori",
            "event_type": event_type,
            "request_id": msg_id,
            "guild_id": str(event.guild.id) if event.guild else "",
        }

    await _notify_superuser(bot, guild_name, user_name, msg_id)
    await guild_req.finish()


# 群邀请（Ark 卡片消息）- 解析并通知超级用户

async def _is_ark_group_invite(event: Event) -> bool:
    try:
        msg = event_chain(event)
        for seg in msg:
            if seg.type == "llonebot:ark" and _parse_ark_invite(seg):
                return True
    except Exception:
        return False
    return False


ark_invite = listen_message(rule=Pred(_is_ark_group_invite), priority=5, block=True)


@ark_invite.handle()
async def handle_ark_group_invite(event: Event, bot: Bot):
    if not superuser:
        await ark_invite.finish()
        return

    for seg in event_chain(event):
        info = _parse_ark_invite(seg)
        if not info:
            continue

        key = info["token"] or info["msgseq"] or info["group_code"]
        guild_name = info["group_name"] or f"group {info['group_code']}"
        inviter_name = info["inviter_uin"] or "未知用户"

        await _notify_ark_invite(bot, guild_name, inviter_name, info)
        break

    await ark_invite.finish()


# 通用：通知超级用户

async def _notify_superuser(bot: Bot, guild_name: str, user_name: str, key: str):
    try:
        target = SendDest(
            superuser, "", False, True, "",
            account_adapter_name(bot),
        )
        await ChainMsg.text(
            f"收到群邀请\n"
            f"群名: {guild_name}\n"
            f"邀请人: {user_name}\n\n"
            f"回复 同意 尝试自动加群，或 拒绝 忽略"
        ).send(target, bot)
        logger.info(f"群邀请已转发给超级用户: {guild_name}")
    except Exception as e:
        logger.error(f"通知超级用户失败: {e}")
        async with _lock:
            _pending.pop(key, None)


async def _notify_ark_invite(bot: Bot, guild_name: str, user_name: str, info: dict):
    """Ark 群卡片不是可审批请求，只通知超级用户手动处理。"""
    try:
        target = SendDest(
            superuser, "", False, True, "",
            account_adapter_name(bot),
        )
        await ChainMsg.text(
            f"检测到群邀请卡片\n"
            f"群名: {guild_name}\n"
            f"群号: {info.get('group_code') or '未知'}\n"
            f"邀请人: {user_name}\n\n"
            f"这是 Ark 群卡片，不是 LLOneBot 暴露的系统入群请求。\n"
            f"当前没有 request_id/flag，无法自动同意；请在 QQ 客户端手动处理。"
        ).send(target, bot)
        logger.info(f"群邀请卡片已转发给超级用户（不可自动审批）: {guild_name}")
    except Exception as e:
        logger.error(f"通知超级用户失败: {e}")


# 超级用户审批回复

async def _is_superuser_reply(event: Event) -> bool:
    uid = str(event_user_id(event))
    return uid == superuser and bool(_pending)


approve_handler = listen_message(rule=Pred(_is_superuser_reply), priority=4, block=True)


@approve_handler.handle()
async def handle_approve_reply(event: Event, bot: Bot):
    text = event_plain_text(event).strip()

    lowered = text.lower()
    approved = lowered.startswith("yes") or lowered.startswith("approve")
    rejected = lowered.startswith("no") or lowered.startswith("reject")

    if not approved and not rejected:
        await approve_handler.finish("Reply yes/approve or no/reject to handle the invite.")

    async with _lock:
        if not _pending:
            await approve_handler.finish("当前没有待审批的群邀请")
            return

        key, info = next(iter(_pending.items()))

        try:
            api_type = info.get("api_type", "satori")
            if approved:
                await _approve_invite(bot, key, info, api_type)
                await approve_handler.send(f"已同意 {info['guild_name']}")
            else:
                await _reject_invite(bot, key, info, api_type)
                await approve_handler.send(f"已拒绝 {info['guild_name']}")
        except Exception as e:
            await approve_handler.send(f"操作失败: {e}")
            logger.error(f"群邀请审批失败: {e}")
        finally:
            del _pending[key]

    await approve_handler.finish()


async def _approve_invite(bot: Bot, key: str, info: dict, api_type: str):
    if api_type == "satori":
        request_id = info.get("request_id") or key
        if info.get("event_type") == "member" and hasattr(bot, "guild_member_approve"):
            await bot.guild_member_approve(request_id=request_id, approve=True, comment="")
        else:
            await bot.guild_approve(request_id=request_id, approve=True, comment="")
        return

    info = await _fill_onebot_request(bot, info)
    gc = str(info.get("group_code", ""))
    sub_type = info.get("sub_type") or "invite"
    flag = str(info.get("flag", ""))
    request_id = str(info.get("request_id", ""))

    attempts = []
    if flag:
        attempts.extend([
            ("set_group_add_request", {"flag": flag, "sub_type": sub_type, "approve": True}),
            ("set_group_add_request", {"flag": flag, "type": sub_type, "approve": True}),
        ])
    if request_id:
        attempts.extend([
            ("set_group_add_request", {"flag": request_id, "sub_type": sub_type, "approve": True}),
            ("set_group_add_request", {"request_id": request_id, "sub_type": sub_type, "approve": True}),
            ("approve_group_invite", {"request_id": request_id, "group_id": gc, "operator_id": info.get("user_name", "")}),
        ])
    if not flag and not request_id:
        details = []
        if info.get("probe_summary"):
            details.append("request list: " + "; ".join(info["probe_summary"]))
        if info.get("probe_errors"):
            details.append("probe errors: " + "; ".join(info["probe_errors"][-2:]))
        details.append(
            "没有拿到 OneBot flag/request_id。当前收到的可能只是群分享卡片，"
            "不是 QQ 系统层的 bot 入群邀请；请在 QQ 里使用“邀请 bot 加入群聊”的系统入口。"
        )
        raise RuntimeError(f"无法自动同意群邀请，请手动邀请 bot 进群 {gc}\n" + "\n".join(details))

    if gc:
        attempts.extend([
            ("approve_group_invite", {"group_id": gc}),
            ("set_group_invite", {"group_id": gc, "approve": True}),
        ])

    errors = []
    for action, params in attempts:
        try:
            await _try_api(bot, action, **params)
            logger.info(f"群邀请已通过 [{action}]: {info['guild_name']}")
            return
        except Exception as e:
            errors.append(f"{action}{params}: {e}")
            continue

    details = []
    if info.get("probe_summary"):
        details.append("request list: " + "; ".join(info["probe_summary"]))
    if info.get("probe_errors"):
        details.append("probe errors: " + "; ".join(info["probe_errors"][-2:]))
    if errors:
        details.append("审批失败: " + "\n".join(errors))
    reason = "\n".join(details) if details else "未找到可用 flag/request_id"
    raise RuntimeError(f"无法自动同意群邀请，请手动邀请 bot 进群 {gc}\n{reason}")


async def _reject_invite(bot: Bot, key: str, info: dict, api_type: str):
    if api_type == "satori":
        request_id = info.get("request_id") or key
        if info.get("event_type") == "member" and hasattr(bot, "guild_member_approve"):
            await bot.guild_member_approve(request_id=request_id, approve=False, comment="")
        else:
            await bot.guild_approve(request_id=request_id, approve=False, comment="")
        return

    info = await _fill_onebot_request(bot, info)
    gc = str(info.get("group_code", ""))
    sub_type = info.get("sub_type") or "invite"
    flag = str(info.get("flag", ""))
    request_id = str(info.get("request_id", ""))

    attempts = []
    if flag:
        attempts.extend([
            ("set_group_add_request", {"flag": flag, "sub_type": sub_type, "approve": False}),
            ("set_group_add_request", {"flag": flag, "type": sub_type, "approve": False}),
        ])
    if request_id:
        attempts.extend([
            ("set_group_add_request", {"flag": request_id, "sub_type": sub_type, "approve": False}),
            ("set_group_add_request", {"request_id": request_id, "sub_type": sub_type, "approve": False}),
            ("reject_group_invite", {"request_id": request_id, "group_id": gc}),
        ])
    if gc:
        attempts.append(("reject_group_invite", {"group_id": gc}))

    for action, params in attempts:
        try:
            await _try_api(bot, action, **params)
            return
        except Exception:
            continue

    logger.info(f"已忽略群邀请 {info.get('guild_name', gc)}")

