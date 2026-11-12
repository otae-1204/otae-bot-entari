"""终末地 /ef 绑定 的对话流程（服务器与授权方式选择、Token / 手机验证码 / 人机验证、角色选择）。"""

from __future__ import annotations

import re
import unicodedata

from arclet.entari import Session
from loguru import logger

from otae_bot.hypergryph.captcha import (
    CAPTCHA_GIVE_UP,
    CAPTCHA_GUIDE,
    CAPTCHA_ROUNDS,
    CAPTCHA_TIMEOUT_SECONDS,
    CAPTCHA_UPLOAD_FAILED,
    CaptchaPageUploadError,
    CaptchaPasteError,
    parse_captcha_paste,
    render_geetest_html,
    upload_captcha_page,
)
from otae_bot.messaging.prompts import prompt

from ..state import account_store, official_client, ownership_stats_service
from .client import (
    ACCOUNT_PROVIDER_CN,
    ACCOUNT_PROVIDER_SKPORT,
    encode_account_credential,
    EndfieldAPIError,
    HypergryphCaptchaRequired,
    is_asia_role,
)
from .crypto import CredentialCipher
from .i18n import server_label
from .store import RoleCandidate


BINDING_SELECT_ATTEMPTS = 3


_BINDING_STOPPED = {
    "cancel": "已取消绑定。",
    "timeout": "等待回复超时，绑定已取消；需要时请重新发送 /ef 绑定。",
    "invalid": f"连续 {BINDING_SELECT_ATTEMPTS} 次未选中有效角色，绑定已取消；需要时请重新发送 /ef 绑定。",
}


class _BindingStopped(Exception):
    """The user cancelled, a prompt timed out, or role selection stayed invalid."""

    def __init__(self, stage: str, reason: str):
        super().__init__(stage, reason)
        self.stage, self.reason = stage, reason


def _binding_log(qq_user_id: str, step: str, **fields) -> None:
    # Steps and counts only: never a phone number, SMS code or token.
    details = "".join(f" {key}={value}" for key, value in fields.items())
    logger.info(f"[endfield-bind] user={qq_user_id} step={step}{details}")


async def _prompt_binding(session: Session, message: str, *, timeout: int, stage: str) -> str:
    answer = await prompt(session, message, timeout=timeout)
    if answer is None:
        raise _BindingStopped(stage, "timeout")
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in {"取消", "cancel", "q", "quit"}:
        raise _BindingStopped(stage, "cancel")
    return text


async def _handle_binding(session, qq_user_id: str, cipher: CredentialCipher) -> None:
    _binding_log(qq_user_id, "start")
    try:
        return await _run_binding(session, qq_user_id, cipher)
    except _BindingStopped as stopped:
        _binding_log(qq_user_id, stopped.reason, at=stopped.stage)
        await session.send(_BINDING_STOPPED[stopped.reason])
        return
    except EndfieldAPIError as exc:
        _binding_log(qq_user_id, "failed", operation=exc.operation, code=exc.code)
        raise


async def _run_binding(session, qq_user_id: str, cipher: CredentialCipher) -> None:
    region = await _prompt_binding(
        session,
        "请选择服务器分区：\n1. 国服（森空岛，支持 Token 与短信验证，二维码暂未开放）\n"
        "2. 亚服（SKPORT，当前支持 Token）\n"
        "回复数字 1 或 2，如需放弃请回复“取消”。",
        timeout=90,
        stage="region",
    )
    normalized_region = region.casefold()
    if normalized_region in {"1", "国服", "cn", "china"}:
        provider = ACCOUNT_PROVIDER_CN
    elif normalized_region in {"2", "亚服", "亞洲", "亚洲", "asia", "skport"}:
        provider = ACCOUNT_PROVIDER_SKPORT
    else:
        _binding_log(qq_user_id, "abort", at="region", reason="unrecognized")
        await session.send("无法识别所选服务器，绑定流程已终止。")
        return
    _binding_log(qq_user_id, "region", provider=provider)

    if provider == ACCOUNT_PROVIDER_SKPORT:
        await session.send(
            "请在浏览器登录 SKPORT（https://www.skport.com/）后访问：\n"
            "https://web-api.skport.com/cookie_store/account_token\n"
            "页面将返回类似以下示例内容（仅为格式示意，切勿直接发送范例 Token）：\n"
            '{"code":0,"data":{"content":"FlJTn48gU1OwP9R7lQUpDFZJ"},"msg":""}\n'
            "示例中需提取的关键内容为：\n"
            "FlJTn48gU1OwP9R7lQUpDFZJ\n"
            "请从你访问的页面中，仅提取 content 双引号包裹的字符串。\n"
            "切勿包含外层双引号或完整 JSON 数据。\n"
            "切勿发送上述示范 Token。\n"
            "请妥善保管凭证，切勿在群聊或公共渠道公开。"
        )
        raw_account_token = await _prompt_binding(
            session,
            "请发送 content 对应的 Token 字符串，如需放弃请回复“取消”。", timeout=150, stage="token"
        )
        account_token = encode_account_credential(raw_account_token, provider)
        _binding_log(qq_user_id, "token_received", provider=provider)
    else:
        account_token = await _bind_cn_account_token(session, qq_user_id)
        if account_token is None:
            return None

    roles = await official_client.discover_roles(account_token)
    if provider == ACCOUNT_PROVIDER_SKPORT:
        roles = [role for role in roles if is_asia_role(role)]
    _binding_log(qq_user_id, "roles", count=len(roles))
    if not roles:
        await session.send(
            "该 Gryphline 通行证下未检索到终末地亚服角色。" if provider == ACCOUNT_PROVIDER_SKPORT
            else "该鹰角网络通行证下未检索到终末地角色。"
        )
        return
    selected = await _select_binding_roles(session, roles, qq_user_id)
    previous_roles = account_store.list_roles(qq_user_id)
    previous_keys = {(role.role_id, role.server_id) for role in previous_roles}
    bound_roles = account_store.bind_roles(qq_user_id, account_token, selected, cipher)
    added_count = sum(
        (role.role_id, role.server_id) not in previous_keys for role in selected
    )
    updated_count = len(selected) - added_count
    region_label = "亚服" if provider == ACCOUNT_PROVIDER_SKPORT else "国服"
    summary = f"{region_label}角色绑定成功：新增 {added_count} 个账号"
    if updated_count:
        summary += f"，更新 {updated_count} 个账号"
    summary += f"；目前累计绑定 {len(bound_roles)} 个账号。"
    selected_keys = {(role.role_id, role.server_id) for role in selected}
    try:
        refresh = await ownership_stats_service.refresh_roles(
            [role for role in bound_roles if (role.role_id, role.server_id) in selected_keys],
            cipher,
            force=True,
            trigger="binding",
        )
    except Exception as exc:
        logger.warning(
            "[endfield-ownership] binding refresh unavailable "
            f"error_type={type(exc).__module__}.{type(exc).__name__}"
        )
    _binding_log(qq_user_id, "done", provider=provider, added=added_count, updated=updated_count, total=len(bound_roles))
    await session.send(
        summary + "\n" + "\n".join(
            f"- {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}" for role in selected
        )
    )


async def _bind_cn_account_token(session, qq_user_id: str) -> str | None:
    method = await _prompt_binding(
        session,
        "请选择绑定授权途径：\n1. Token 授权绑定\n2. 手机短信验证码绑定\n"
        "二维码扫码绑定暂未开放。\n"
        "支持追加绑定多个鹰角账号，已绑定的角色数据不会被覆盖。\n回复数字 1 或 2，如需放弃请回复“取消”。",
        timeout=90,
        stage="method",
    )
    normalized = method.casefold()
    if normalized in {"1", "token", "t"}:
        _binding_log(qq_user_id, "method", method="token")
        await session.send(
            "请在浏览器中登录森空岛并访问：\nhttps://web-api.skland.com/account/info/hg\n"
            "复制页面响应中 data.content 对应的完整字符串并发送。请妥善保管凭据，切勿向他人公开。"
        )
        account_token = await _prompt_binding(session, "请发送 data.content 内容，如需放弃请回复“取消”。", timeout=150, stage="token")
        _binding_log(qq_user_id, "token_received", provider=ACCOUNT_PROVIDER_CN)
    elif normalized in {"2", "短信", "手机", "sms"}:
        _binding_log(qq_user_id, "method", method="sms")
        phone = await _prompt_binding(session, "请输入鹰角网络账号对应的 11 位手机号，如需放弃请回复“取消”。", timeout=90, stage="phone")
        if not re.fullmatch(r"1\d{10}", phone):
            _binding_log(qq_user_id, "abort", at="phone", reason="format")
            await session.send("手机号格式不符合标准，绑定流程已终止。")
            return None
        _binding_log(qq_user_id, "send_code")
        if not await _send_cn_phone_code(session, qq_user_id, phone):
            return None
        _binding_log(qq_user_id, "code_sent")
        code = await _prompt_binding(session, "短信验证码已下发，请输入收到的验证码，如需放弃请回复“取消”。", timeout=120, stage="code")
        if not re.fullmatch(r"\d{4,8}", code):
            _binding_log(qq_user_id, "abort", at="code", reason="format")
            await session.send("验证码格式输入有误，绑定流程已终止。")
            return None
        _binding_log(qq_user_id, "verify")
        account_token = await official_client.token_by_phone_code(phone, code)
        _binding_log(qq_user_id, "verified")
    # 暂时禁用二维码绑定，保留以下代码以便后续恢复：
    # elif normalized in {"3", "二维码", "扫码", "qr", "qrcode"}:
    #     account_token = await _bind_cn_qr_account(session)
    #     if account_token is None:
    #         return None
    else:
        qr = normalized in {"3", "二维码", "扫码", "qr", "qrcode"}
        _binding_log(qq_user_id, "abort", at="method", reason="qr" if qr else "unrecognized")
        await session.send("二维码扫码暂未开放，请回复 1 或 2 选择其他方式。" if qr else "无法识别所选绑定方式，流程已终止。")
        return None
    return encode_account_credential(account_token, ACCOUNT_PROVIDER_CN)


async def _send_cn_phone_code(session, qq_user_id: str, phone: str) -> bool:
    """Send the SMS code; when Hypergryph asks for a GeeTest check, let the user solve it first."""
    captcha = None
    for attempt in range(1, CAPTCHA_ROUNDS + 2):
        try:
            await official_client.send_phone_code(phone, captcha=captcha)
            return True
        except HypergryphCaptchaRequired as required:
            _binding_log(qq_user_id, "captcha_required", attempt=attempt)
            if attempt > CAPTCHA_ROUNDS:
                break
            captcha = await _solve_cn_captcha(session, qq_user_id, required)
            if captcha is None:
                return False
    _binding_log(qq_user_id, "abort", at="captcha", reason="rounds")
    await session.send(CAPTCHA_GIVE_UP)
    return False


async def _solve_cn_captcha(session, qq_user_id: str, required: HypergryphCaptchaRequired) -> dict[str, str] | None:
    html = render_geetest_html(gt=required.gt, challenge=required.challenge, new_captcha=required.new_captcha)
    try:
        link = await upload_captcha_page(html)
    except CaptchaPageUploadError:
        _binding_log(qq_user_id, "abort", at="captcha", reason="upload")
        await session.send(CAPTCHA_UPLOAD_FAILED)
        return None
    _binding_log(qq_user_id, "captcha_page_sent")
    await session.send(f"{CAPTCHA_GUIDE}\n{link}")
    paste = await _prompt_binding(
        session,
        "请发送验证页面显示的一整行结果，如需放弃请回复“取消”。",
        timeout=CAPTCHA_TIMEOUT_SECONDS,
        stage="captcha",
    )
    try:
        captcha = parse_captcha_paste(paste)
    except CaptchaPasteError as exc:
        _binding_log(qq_user_id, "abort", at="captcha", reason="format")
        await session.send(f"{exc}绑定流程已终止。")
        return None
    _binding_log(qq_user_id, "captcha_received")
    return captcha


def parse_binding_selection(answer: str, roles: list[RoleCandidate]) -> list[RoleCandidate] | None:
    """Listed numbers and/or UIDs, in any mix; None unless every item names a listed role."""
    text = unicodedata.normalize("NFKC", answer).strip()
    if text.casefold() in {"全部", "all"}:
        return list(roles)
    items = [item.strip(".。号#") for item in re.split(r"[\s,，、;；/|]+|(?i:uid)[:：]?", text)]
    items = [item for item in items if item]
    if not items:
        return None
    picked: set[int] = set()
    for item in items:
        if not item.isdecimal():
            return None
        if len(item) <= 3 and 1 <= int(item) <= len(roles):
            picked.add(int(item) - 1)
            continue
        matches = {index for index, role in enumerate(roles) if role.role_id == item}
        if not matches:
            return None
        picked |= matches
    return [role for index, role in enumerate(roles) if index in picked]


async def _select_binding_roles(session: Session, roles: list[RoleCandidate], qq_user_id: str) -> list[RoleCandidate]:
    if len(roles) == 1:
        _binding_log(qq_user_id, "select", selected=1, total=1)
        return roles
    listing = "\n".join(
        f"{index}. {role.nickname} · {server_label(role.server_name or role.server_id)} · UID {role.role_id}"
        for index, role in enumerate(roles, 1)
    )
    message = (
        "检测到多个终末地角色，请回复序号或 UID，可一次回复多个（用空格、逗号或顿号分隔），"
        "也可回复“全部”；回复“取消”退出：\n" + listing
    )
    for attempt in range(1, BINDING_SELECT_ATTEMPTS + 1):
        selected = parse_binding_selection(await _prompt_binding(session, message, timeout=120, stage="select"), roles)
        if selected:
            _binding_log(qq_user_id, "select", selected=len(selected), total=len(roles), attempt=attempt)
            return selected
        _binding_log(qq_user_id, "select_invalid", attempt=attempt, limit=BINDING_SELECT_ATTEMPTS)
        message = "编号无效，请回复列表前面的序号（或列表中的 UID）：\n" + listing
    raise _BindingStopped("select", "invalid")
