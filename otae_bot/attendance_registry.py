"""Shared registry behind the unified ``/签到`` entry point.

Each game plugin owns its own database, credential decryption and HTTP client.
Importing another plugin's ``handlers`` module would re-run its command
registration and initialization as a side effect, so a game instead registers a
small capability object while it loads and the unified entry point only walks
this registry.  Nothing here sends a message or finishes a matcher: the caller
renders and delivers, which keeps the loop testable without an Entari session.

This is shared code, so it must never import ``plugins.*`` — that dependency
direction is enforced by ``tests/test_architecture.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from arclet.entari.event.plugin import PluginUnloaded
from arclet.letoderea.exceptions import _ExitException
from loguru import logger

# Outcome states.  ``SIGNED`` means the game ran and produced its own result
# (the card may still contain per-role failures); the other states explain why a
# game produced nothing at all.
SIGNED = "signed"
UNBOUND = "unbound"
DISABLED = "disabled"
UNAVAILABLE = "unavailable"
FAILED = "failed"

# Games the unified entry point knows about, in execution order.  Labels and
# binding hints live here so a game whose plugin is not loaded is still reported
# precisely instead of being mistaken for "not bound".
GAME_ORDER: tuple[str, ...] = ("endfield", "arknights")
GAME_IDENTITIES: dict[str, tuple[str, str]] = {
    "endfield": ("终末地", "/zmd 绑定"),
    "arknights": ("明日方舟", "/ak 绑定"),
}

FAILURE_RETRY_MESSAGE = "签到失败，请稍后重试"


def game_label(game: str) -> str:
    return GAME_IDENTITIES.get(game, (game, ""))[0]


def game_bind_hint(game: str) -> str:
    default_hint = f"/{game} 绑定" if game else "绑定"
    return GAME_IDENTITIES.get(game, (game, default_hint))[1]


def unbound_notice() -> str:
    """Prompt used only when no game has anything bound for this user."""
    hints = "、".join(f"{hint}（{label}）" for label, hint in GAME_IDENTITIES.values())
    return f"尚未绑定任何签到账号。请私聊使用 {hints}完成绑定。"


@dataclass(frozen=True, slots=True)
class AttendanceResult:
    """One game's whole run: a card, its text fallback, or a failure reason.

    ``ok=False`` marks a game-level failure whose ``text`` is safe to show in
    chat (a missing key, an unreachable API, a storage fault).  Individual
    characters that failed are reported inside the card instead.
    """

    png: bytes | None = None
    text: str = ""
    ok: bool = True


@dataclass(frozen=True, slots=True)
class AttendanceOutcome:
    game: str
    label: str
    status: str
    text: str = ""
    png: bytes | None = None

    @property
    def has_card(self) -> bool:
        return self.status == SIGNED and self.png is not None


@dataclass(frozen=True)
class AttendanceCapability:
    """One game's contribution to ``/签到``.

    ``roles`` answers "does this user have anything bound here?" without
    touching credentials, so an unbound user never triggers a key error.
    ``sign`` performs the whole game run — every bound character, isolated
    failures, rendered card — and returns the result.  ``module`` identifies the
    registering module so a hot reload can replace stale state, and
    ``feature_key`` names the group switch that still governs this game (the
    game plugin's own key unless a game says otherwise).
    """

    game: str
    owner: str
    module: Any
    roles: Callable[[str], Sequence[Any]]
    sign: Callable[..., Awaitable[AttendanceResult]]
    label: str = ""
    feature_key: str = ""

    @property
    def display_label(self) -> str:
        return self.label or game_label(self.game)

    @property
    def group_feature(self) -> str:
        return self.feature_key or self.game


class AttendanceRegistry:
    """Game id → capability.  Re-registering one replaces it (hot reload)."""

    def __init__(self) -> None:
        self._items: dict[str, AttendanceCapability] = {}

    def register(self, capability: AttendanceCapability) -> None:
        if not capability.game:
            raise ValueError("attendance capability needs a game id")
        self._items[capability.game] = capability

    def unregister(self, game: str) -> bool:
        return self._items.pop(game, None) is not None

    def unregister_owner(self, owner: str) -> int:
        """Drop what an unloaded plugin registered, ignoring a hot reload.

        Entari pops the plugin module from ``sys.modules`` before publishing
        ``PluginUnloaded``, so an entry whose module is no longer the live one
        belongs to a plugin that is really gone.  A plugin that was already
        re-imported keeps its (fresh) entry.
        """
        if not owner:
            return 0
        removed = 0
        for game, capability in tuple(self._items.items()):
            if capability.owner != owner:
                continue
            if sys.modules.get(owner) is capability.module:
                continue
            self._items.pop(game, None)
            removed += 1
        return removed

    def get(self, game: str) -> AttendanceCapability | None:
        return self._items.get(game)

    def games(self) -> tuple[str, ...]:
        known = [game for game in GAME_ORDER if game in self._items]
        extra = [game for game in self._items if game not in GAME_ORDER]
        return (*known, *extra)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, game: object) -> bool:
        return game in self._items

    def __len__(self) -> int:
        return len(self._items)


registry = AttendanceRegistry()


def register_attendance_capability(capability: AttendanceCapability) -> None:
    registry.register(capability)


def unregister_attendance_capability(game: str) -> bool:
    return registry.unregister(game)


async def collect_outcomes(
    user_id: str,
    *,
    group: bool,
    enabled: Callable[[str], bool],
    games: Sequence[str] = GAME_ORDER,
) -> list[AttendanceOutcome]:
    """Run every registered game for one user, never letting one abort another.

    ``enabled`` answers the group switch question for a game's plugin key.  It
    is only consulted for games that are actually registered, so a group switch
    can never mask a plugin that failed to load.  ``CancelledError`` and Entari's
    ``_ExitException`` still propagate: cancellation must release the per-role
    lock and must not be reported as a sign-in failure.
    """
    outcomes: list[AttendanceOutcome] = []
    for game in games:
        capability = registry.get(game)
        label = capability.display_label if capability is not None else game_label(game)
        if capability is None:
            # The plugin is not loaded: say so instead of pretending the user
            # simply has no binding here.
            outcomes.append(
                AttendanceOutcome(
                    game, label, UNAVAILABLE, f"{label}：签到功能暂不可用（插件未加载），已跳过。"
                )
            )
            continue
        if not enabled(capability.group_feature):
            outcomes.append(
                AttendanceOutcome(game, label, DISABLED, f"{label}：本群未开启该功能，已跳过。")
            )
            continue
        try:
            roles = tuple(capability.roles(user_id) or ())
        except _ExitException:
            raise
        except Exception as exc:  # noqa: BLE001 - one game must not abort the other
            logger.error(
                f"[signin] binding lookup failed: game={game} error_type={type(exc).__name__}"
            )
            outcomes.append(
                AttendanceOutcome(
                    game, label, FAILED, f"{label}：无法读取绑定记录，{FAILURE_RETRY_MESSAGE}。"
                )
            )
            continue
        if not roles:
            outcomes.append(
                AttendanceOutcome(
                    game, label, UNBOUND, f"{label}：尚未绑定，请私聊使用 {game_bind_hint(game)}。"
                )
            )
            continue
        try:
            result = await capability.sign(user_id, group=group)
        except _ExitException:
            raise
        except Exception as exc:  # noqa: BLE001 - one game must not abort the other
            logger.error(
                f"[signin] attendance failed: game={game} error_type={type(exc).__name__}"
            )
            outcomes.append(
                AttendanceOutcome(game, label, FAILED, f"{label}：{FAILURE_RETRY_MESSAGE}。")
            )
            continue
        if result.ok:
            outcomes.append(AttendanceOutcome(game, label, SIGNED, result.text, result.png))
        else:
            reason = (result.text or "").strip() or FAILURE_RETRY_MESSAGE
            outcomes.append(AttendanceOutcome(game, label, FAILED, f"{label}：{reason}"))
    return outcomes


def build_notice(outcomes: Sequence[AttendanceOutcome]) -> str:
    """The text sent besides the per-game cards; empty when nothing is needed.

    Problems that need attention — a closed group switch, an unloaded plugin, a
    failed run — are always reported.  A game the user simply never bound stays
    silent while another game produced a result, so a single-game user is not
    nagged on every sign-in; it is only named when nothing ran at all.
    """
    notes = [
        outcome.text
        for outcome in outcomes
        if outcome.status in {DISABLED, UNAVAILABLE, FAILED} and outcome.text
    ]
    if any(outcome.status == SIGNED for outcome in outcomes):
        return "\n".join(notes)
    unbound = [outcome for outcome in outcomes if outcome.status == UNBOUND]
    if unbound and len(unbound) == len(outcomes):
        return unbound_notice()
    notes.extend(outcome.text for outcome in unbound if outcome.text)
    return "\n".join(notes)


async def on_plugin_unloaded(event: PluginUnloaded) -> None:
    """Release registrations of a plugin that will not come back."""
    plugin_id = str(getattr(event, "plugin_id", "") or "")
    if registry.unregister_owner(plugin_id):
        logger.debug(f"[signin] released attendance capability of {plugin_id}")


__all__ = [
    "DISABLED",
    "FAILED",
    "GAME_IDENTITIES",
    "GAME_ORDER",
    "SIGNED",
    "UNAVAILABLE",
    "UNBOUND",
    "AttendanceCapability",
    "AttendanceOutcome",
    "AttendanceRegistry",
    "AttendanceResult",
    "build_notice",
    "collect_outcomes",
    "game_bind_hint",
    "game_label",
    "on_plugin_unloaded",
    "register_attendance_capability",
    "registry",
    "unbound_notice",
    "unregister_attendance_capability",
]
