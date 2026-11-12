"""终末地插件各功能共用的回复与发送辅助（图片、合并转发、分页异常、权限判断）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from arclet.entari import Image, MessageChain
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.config.settings import Config
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup
from otae_bot.messaging.forward import send_forward


_FORWARD_SENDER_NAME = "Endfield"


def _is_endfield_superuser(user_id: str) -> bool:
    configured = Config.SUPERUSERS or ()
    if isinstance(configured, str):
        configured = [configured]
    return str(user_id) in {str(value) for value in configured}


async def _send_forward_pngs(
    session,
    bot,
    pngs: tuple[bytes, ...],
    *,
    sender_name: str = _FORWARD_SENDER_NAME,
    log_tag: str = "endfield-challenge",
) -> str:
    """Send PNG pages as one merged forward; returns the path used (``satori`` / ``images``).

    The standard ``<message forward>`` element is tried first.  If it raises (or
    there is no session), the pages go out as ordinary image messages, one per
    message in the same order; an error from that propagates to the caller.
    """
    uin = str(getattr(bot, "self_id", "") or getattr(bot, "id", "") or "") or None
    images: list = []
    try:
        images = [_png_image(png) for png in pngs]
        await send_forward(session, images, name=sender_name, uin=uin)
        return "satori"
    except ExitState:
        raise
    except Exception as exc:
        logger.warning(
            f"[{log_tag}] satori forward element failed, "
            f"sending {len(pngs)} pages as images: {type(exc).__name__}: {exc}"
        )
    if len(images) != len(pngs):
        images = [_png_image(png) for png in pngs]
    for image in images:
        await session.send(MessageChain([image]))
    return "images"


async def _one_page(page) -> tuple[bytes, ...]:
    return (await page,)


class _IncompletePages(Exception):
    def __init__(self, pages: tuple[bytes, ...]):
        self.pages = pages


async def _finish_png(session, png: bytes) -> None:
    return await _finish_pngs(session, (png,))


async def _finish_pngs(session, pngs: tuple[bytes, ...]) -> None:
    await session.send(MessageChain([_png_image(png) for png in pngs]))


def _png_image(png: bytes):
    """Persist one PNG to a temp file and wrap it as an image element.

    LLOneBot resolves ``file://`` locally, so a temp file avoids inflating the
    payload with a base64 data URI for every page of a merged forward.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        return Image.of(path=Path(file.name))
