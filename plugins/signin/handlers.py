"""Unified ``/签到`` entry point.

This plugin owns no game data.  It asks every registered game for the sender's
bound roles, runs each game's own service in turn, and stacks the cards into
one image. The games keep their own databases, credentials and clients. A
game that is disabled for the group, not loaded, or failing never stops the
other one.
"""

from __future__ import annotations

import tempfile

from arclet.letoderea.exceptions import _ExitException
from loguru import logger

from otae_bot.adapters.entari import (
    ArgVal,
    ChainMsg,
    Session,
    cmd,
    event_user_id,
    get_rest,
    is_group,
    make_image,
)
from otae_bot.attendance_registry import collect_outcomes
from otae_bot.group_features import feature_store, scope_from_event
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from .presentation import SigninDelivery, build_delivery

USAGE = (
    "用法：/签到\n"
    "依次签到终末地与明日方舟的全部已绑定角色，合成一张结果图发送。\n"
    "账号绑定：私聊 /zmd 绑定（终末地）、/ak 绑定（明日方舟）。\n"
    "只签到单个游戏仍可使用 /ef 签到 或 /ak 签到。"
)

signin_cmd = cmd("签到", aliases={"checkin", "qiandao"}, priority=5, block=True)


@signin_cmd.handle()
async def handle_signin(session: Session, rest: ArgVal[str]) -> None:
    # Version one takes no arguments: anything extra is answered with the usage
    # note instead of silently signing something the user did not ask for.
    if get_rest(rest).strip():
        await session.send(USAGE)
        session.stop()
        return

    event = session.event
    scope = scope_from_event(session.account, event)
    outcomes = await collect_outcomes(
        event_user_id(event),
        group=is_group(event),
        # Each game keeps its own group switch, so /签到 can never bypass a
        # group that turned that game off.
        enabled=lambda game: feature_store.is_enabled(scope, game),
    )
    await _deliver(session, await build_delivery(outcomes))
    session.stop()


async def _deliver(session: Session, delivery: SigninDelivery) -> None:
    """Send one image and any notices together; retry only delivery as text."""
    try:
        if delivery.png is None:
            if delivery.text:
                await session.send(delivery.text)
            return
        parts = [_png_image(delivery.png)]
        if delivery.text:
            parts.append("\n" + delivery.text)
        await session.send(ChainMsg(parts))
    except _ExitException:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve results when image delivery fails
        logger.warning(f"[signin] delivery failed error_type={type(exc).__name__}")
        if delivery.png is None or not delivery.fallback_text:
            return
        try:
            await session.send(delivery.fallback_text)
        except _ExitException:
            raise
        except Exception as retry_exc:  # noqa: BLE001 - never repeat the attendance request
            logger.warning(f"[signin] text delivery failed error_type={type(retry_exc).__name__}")


def _png_image(png: bytes):
    """Persist one PNG to a temp file and wrap it as an image element.

    LLOneBot resolves ``file://`` locally, so a temp file avoids inlining a
    base64 data URI for every card, matching the two game plugins.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        return make_image(path=file.name)


__all__ = ["USAGE", "handle_signin", "signin_cmd"]
