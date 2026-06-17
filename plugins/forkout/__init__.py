"""Forkout image command and per-group ranking."""

import io
import os
import re
from pathlib import Path

import httpx
from arclet.entari import (
    Account,
    At,
    Event,
    Image as EntariImage,
    MessageChain,
    MessageCreatedEvent,
    Quote,
    Session,
    Text,
    listen,
)
from PIL import Image

from configs.path_config import IMAGE_PATH
from utils.entari_native import cmd as _cmd
from utils.json_store import JsonStore


_FORKOUT_TEXT_RE = re.compile(r"/(?:forkout|叉出去|叉|[Xx])(?=$|\s)")

fork_rank = _cmd(
    "forkrank",
    aliases={"叉排行", "被叉排行榜", "叉人排行", "Xrank", "XR", "xrank", "xr"},
    priority=5,
    block=True,
)

imgpath = IMAGE_PATH + "forkout/"
os.makedirs(imgpath, exist_ok=True)

_fork_store = JsonStore("data/forkout_counts.json")


def _event_message(event: Event) -> MessageChain:
    message = getattr(event, "message", None)
    if message is not None:
        return MessageChain(getattr(message, "message", message))
    content = getattr(event, "content", None)
    if content is not None:
        return MessageChain(content)
    return MessageChain()


def _text_of(seg: object) -> str:
    return str(getattr(seg, "text", None) or getattr(seg, "content", None) or seg)


def _message_has_command(message: MessageChain) -> bool:
    return any(isinstance(seg, Text) and _FORKOUT_TEXT_RE.search(_text_of(seg)) for seg in message)


def _get_group_id(event: Event) -> str | None:
    guild = getattr(event, "guild", None)
    channel = getattr(event, "channel", None)
    if guild and guild.id:
        return str(guild.id)
    if channel and channel.id:
        return str(channel.id)
    return None


async def _extract_at_users(event: Event) -> list[str]:
    users: list[str] = []
    for seg in _event_message(event):
        if isinstance(seg, At):
            uid = (
                getattr(seg, "id", "")
                or getattr(seg, "target", "")
                or getattr(seg, "user_id", "")
            )
            if uid:
                users.append(str(uid))
    return users


async def _extract_reply_id(event: Event) -> str | None:
    for seg in _event_message(event):
        if isinstance(seg, Quote):
            reply_id = getattr(seg, "id", "") or getattr(seg, "message_id", "")
            if reply_id:
                return str(reply_id)
    return None


def _increment_fork_count(group_id: str | None, user_id: str):
    if not group_id:
        return
    key = f"{group_id}.{user_id}"
    current = _fork_store.get(key, 0)
    _fork_store[key] = current + 1


_QQ_AVATAR_APIS = [
    "https://api.qqsuu.cn/api/dm-qtouxiang?qq={qq}",
    "https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640",
    "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",
    "https://q2.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640",
    "https://q4.qlogo.cn/g?b=qq&nk={qq}&s=640",
]


async def _download_avatar(account: Account | None, satori_url: str | None, qq_number: str) -> bytes | None:
    if satori_url and account is not None:
        try:
            return await account.download(satori_url)
        except Exception:
            pass

    for api in _QQ_AVATAR_APIS:
        try:
            url = api.format(qq=qq_number)
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
        except Exception:
            continue

    return None


async def _make_fork_image(qq_number: str, satori_url: str | None, account: Account | None = None) -> str | None:
    fork_img_path = imgpath + "forkout.jpg"
    if not os.path.exists(fork_img_path):
        return None

    img = Image.open(fork_img_path)
    avatar_data = await _download_avatar(account, satori_url, qq_number)

    if not avatar_data:
        return fork_img_path

    avatar = Image.open(io.BytesIO(avatar_data))
    avatar = avatar.resize((192, 192), Image.LANCZOS)
    img.paste(avatar, (40, 263))

    result_path = imgpath + "forkout_result.jpg"
    img.save(result_path)
    return result_path


async def _send_fork_image(session: Session, path: str, reply_id: str | None = None):
    segments = []
    if reply_id:
        segments.append(Quote(reply_id))
    segments.append(EntariImage.of(path=Path(path)))
    await session.send(MessageChain(segments))
    session.stop()


@listen(MessageCreatedEvent)
async def handle_forkout(session: Session, account: Account):
    event = session.event
    message = _event_message(event)
    has_command = _message_has_command(message)
    if not has_command:
        return

    at_users = await _extract_at_users(event)
    reply_id = await _extract_reply_id(event)
    if not at_users and not reply_id:
        return

    group_id = _get_group_id(event)

    if at_users:
        target_uid = at_users[0]
        avatar_url = None
        try:
            member = await account.guild_member_get(guild_id=group_id, user_id=target_uid)
            avatar_url = member.user.avatar if member and member.user else None
        except Exception:
            pass

        result = await _make_fork_image(target_uid, avatar_url, account)
        if not result:
            await session.send("叉出去图片不存在")
            session.stop()
            return

        _increment_fork_count(group_id, target_uid)
        await _send_fork_image(session, result, reply_id)
        return

    fork_img = imgpath + "forkout.jpg"
    if not os.path.exists(fork_img):
        await session.send("叉出去图片不存在")
        session.stop()
        return
    await _send_fork_image(session, fork_img, reply_id)


@fork_rank.handle()
async def handle_fork_rank(event: Event, bot: Account):
    group_id = _get_group_id(event)
    if not group_id:
        await fork_rank.finish("这个命令只能在群聊/频道中使用")

    prefix = f"{group_id}."
    records: list[tuple[str, int]] = []
    for key, count in _fork_store.items():
        if key.startswith(prefix):
            uid = key[len(prefix):]
            records.append((uid, count))

    if not records:
        await fork_rank.finish("本群还没有人被叉过")

    records.sort(key=lambda x: x[1], reverse=True)
    top = records[:15]

    names: dict[str, str] = {}
    for uid, _ in top:
        try:
            member = await bot.guild_member_get(guild_id=group_id, user_id=uid)
            names[uid] = member.nick or member.user.name if member else uid
        except Exception:
            names[uid] = uid

    lines = ["=== 被叉排行榜 ==="]
    for i, (uid, count) in enumerate(top, 1):
        medal = {1: "1ST", 2: "2ND", 3: "3RD"}.get(i, f"{i}.")
        lines.append(f"{medal} {names[uid]} - 被叉 {count} 次")

    await fork_rank.finish("\n".join(lines))
