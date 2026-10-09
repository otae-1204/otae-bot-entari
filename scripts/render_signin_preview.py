"""Render the combined ``/ak 签到`` / ``/ef 签到`` messages using sample accounts.

Drives the real orchestration (``otae_bot.attendance_registry`` and
``otae_bot.attendance_delivery``) and the two games' real card renderers with
fabricated roles, then writes the exact message sequence the chat would receive.
The requested game's card is on top; the other game's bound roles follow:

* ``both-success``      — ``/ak 签到``：明日方舟在上、终末地在下的一张拼接图
* ``six-accounts``      — ``/ef 签到``：终末地 3 个角色 + 明日方舟 3 个角色，均含奖励和累签轨道
* ``one-failed``        — ``/ef 签到``：终末地卡片 + 明日方舟的文字失败提示（同一条消息）
* ``one-role-failed``   — ``/ef 签到``：明日方舟卡片里单个角色失败
* ``other-unbound``     — ``/ak 签到``：未绑定终末地，只发明日方舟卡片、不提示

Never reads ``.env``, the account databases or any real credential.  Both game
packages are imported through throwaway package names so neither plugin's
Entari command registration runs. Endfield reward artwork is fetched from its
public asset URLs through the production renderer; no game API is called.

Usage:
    .venv/Scripts/python.exe scripts/render_signin_preview.py
    ... --output-dir output/signin-preview --html-only
    ... --case six-accounts
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ENDFIELD_PACKAGE = "endfield_signin_preview"
ARKNIGHTS_PACKAGE = "arknights_signin_preview"


def _package(name: str, subdirectory: str) -> types.ModuleType:
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / "plugins" / subdirectory)]
    sys.modules[name] = package
    return package


# Shared rendering settings normally load .env relative to cwd. Import from an
# empty temporary workspace so this offline tool never reads the bot's secrets.
_runtime = tempfile.TemporaryDirectory(prefix="signin-preview-")
_previous_cwd = Path.cwd()
try:
    os.chdir(_runtime.name)
    _package(ENDFIELD_PACKAGE, "endfield")
    for module in (
        "account.crypto",
        "account.store",
        "gacha.xhh",
        "account.client",
        "account.currency.service",
        "gacha.service",
        "catalog.models",
        "catalog.aliases",
        "providers.registry",
        "catalog.commands",
        "rendering.cards",
    ):
        importlib.import_module(f"{ENDFIELD_PACKAGE}.{module}")
    _package(ARKNIGHTS_PACKAGE, "arknights")
    for module in ("models", "rendering.cards"):
        importlib.import_module(f"{ARKNIGHTS_PACKAGE}.{module}")

    from otae_bot import attendance_delivery, attendance_registry
    from otae_bot.infrastructure.rendering.browser import close_browser
    from otae_bot.infrastructure.rendering.executor import close_image_executor
finally:
    os.chdir(_previous_cwd)

endfield_models = importlib.import_module(f"{ENDFIELD_PACKAGE}.catalog.models")
endfield_client = importlib.import_module(f"{ENDFIELD_PACKAGE}.account.client")
endfield_cards = importlib.import_module(f"{ENDFIELD_PACKAGE}.rendering.cards")
arknights_models = importlib.import_module(f"{ARKNIGHTS_PACKAGE}.models")
arknights_cards = importlib.import_module(f"{ARKNIGHTS_PACKAGE}.rendering.cards")

GENERATED_AT = "样例数据 · 合并签到预览"
ENDFIELD_DIAMOND_ICON = endfield_client._attendance_icon_url(canonical_id="item_diamond")
ENDFIELD_GOLD_ICON = endfield_client._attendance_icon_url(canonical_id="item_gold")


def endfield_success_view() -> endfield_models.AttendanceCardView:
    return endfield_models.AttendanceCardView(
        roles=[
            endfield_models.AttendanceRoleView(
                nickname="管理员",
                uid="****1024",
                server_name="官方服务器",
                status="success",
                message="签到成功",
                rewards=[endfield_models.AttendanceRewardView("嵌晶玉", 100, ENDFIELD_DIAMOND_ICON)],
                monthly_count=8,
                calendar_days=31,
                milestones=[endfield_models.AttendanceMilestoneView(day, count, ENDFIELD_DIAMOND_ICON)
                            for day, count in ((4, 100), (9, 200), (17, 300))],
            ),
            endfield_models.AttendanceRoleView(
                nickname="亚服小号",
                uid="****8888",
                server_name="亚洲服务器",
                status="already",
                message="今日已签到",
            ),
        ],
        generated_at=GENERATED_AT,
    )


def arknights_success_view() -> arknights_models.AttendanceCardView:
    return arknights_models.AttendanceCardView(
        roles=(
            arknights_models.AttendanceRoleView(
                nickname="夜岚",
                uid="****1024",
                full_uid="10001024",
                channel_name="官服",
                status="success",
                rewards=(
                    arknights_models.AttendanceRewardView("合成玉", 500),
                    arknights_models.AttendanceRewardView("初级作战记录", 3),
                ),
                monthly_count=8,
                daily_progress=tuple(day <= 8 for day in range(1, 32)),
                milestones=tuple(arknights_models.AttendanceMilestoneView(day, "合成玉", count, day <= 8, False)
                                 for day, count in ((4, 100), (9, 200), (17, 300))),
            ),
            arknights_models.AttendanceRoleView(
                nickname="B服小号",
                uid="****8888",
                full_uid="20008888",
                channel_name="B服",
                status="already",
            ),
        ),
        generated_at=GENERATED_AT,
    )


def arknights_mixed_view() -> arknights_models.AttendanceCardView:
    """One role succeeded, one role's Skland credential expired."""
    view = arknights_success_view()
    return arknights_models.AttendanceCardView(
        roles=(
            view.roles[0],
            arknights_models.AttendanceRoleView(
                nickname="失效账号",
                uid="****6666",
                full_uid="30006666",
                channel_name="官服",
                status="failed",
                message="森空岛凭据已失效或登录状态异常，请重新私聊使用 /ak 绑定。",
            ),
        ),
        generated_at=GENERATED_AT,
    )


def six_account_views():
    """Six fabricated successful roles, including each role's full calendar."""
    ef_base = endfield_success_view().roles[0]
    ef_roles = [
        replace(
            ef_base, nickname=name, uid=uid, monthly_count=signed,
            rewards=[endfield_models.AttendanceRewardView(reward, count, icon)],
        )
        for name, uid, signed, reward, count, icon in (
            ("管理员·主号", "****1024", 4, "嵌晶玉", 100, ENDFIELD_DIAMOND_ICON),
            ("管理员·二号", "****2048", 8, "折金票", 2000, ENDFIELD_GOLD_ICON),
            ("管理员·三号", "****4096", 17, "嵌晶玉", 300, ENDFIELD_DIAMOND_ICON),
        )
    ]
    ak_base = arknights_success_view().roles[0]
    ak_roles = tuple(
        replace(
            ak_base, nickname=name, uid=uid, full_uid="", channel_name=channel,
            monthly_count=signed,
            rewards=tuple(arknights_models.AttendanceRewardView(*reward) for reward in rewards),
            daily_progress=tuple(day <= signed for day in range(1, 32)),
            milestones=tuple(
                arknights_models.AttendanceMilestoneView(day, "合成玉", count, day <= signed, False)
                for day, count in ((4, 80), (12, 100), (20, 120))
            ),
        )
        for name, uid, channel, signed, rewards in (
            ("博士·主号", "****1024", "官服", 4, (("合成玉", 80),)),
            ("博士·二号", "****2048", "B服", 8, (("龙门币", 1800), ("中级作战记录", 2))),
            ("博士·三号", "****4096", "官服", 20, (("合成玉", 120),)),
        )
    )
    return (
        endfield_models.AttendanceCardView(roles=ef_roles, generated_at=GENERATED_AT),
        arknights_models.AttendanceCardView(roles=ak_roles, generated_at=GENERATED_AT),
    )


def capability(game: str, view, renderer, report, result=None, roles=("role",)):
    """A synthetic capability that renders with the game's own card renderer.

    ``renderer=None`` models "no card available": the run then reports the
    game's text result, exactly like a failed render in production.
    """

    async def sign(user_id: str, *, group: bool):
        if result is not None:
            return result
        png = await renderer(view) if renderer is not None else None
        return attendance_registry.AttendanceResult(png=png, text=report(view))

    return attendance_registry.AttendanceCapability(
        game=game,
        owner=f"scripts.preview.{game}",
        module=sys.modules[__name__],
        roles=lambda user_id: list(roles),
        sign=sign,
    )


async def run_case(name: str, current: str, capabilities, *, output_dir: Path) -> dict:
    """Model ``/<current> 签到``: sign ``current``, then every other bound game."""
    registry = attendance_registry.AttendanceRegistry()
    own = next(item for item in capabilities if item.game == current)
    for item in capabilities:
        if item is not own:
            registry.register(item)
    result = await own.sign("10001", group=True)
    outcomes = [attendance_registry.signed_outcome(current, png=result.png, text=result.text)]
    original = attendance_registry.registry
    attendance_registry.registry = registry
    try:
        outcomes.extend(
            await attendance_registry.collect_companion_outcomes(
                "10001", current=current, group=True, enabled=lambda game: True
            )
        )
    finally:
        attendance_registry.registry = original

    messages: list[dict] = []
    delivery = await attendance_delivery.build_delivery(outcomes)
    if delivery.png is not None:
        path = output_dir / f"{name}.png"
        path.write_bytes(delivery.png)
        messages.append({"kind": "image", "path": path.relative_to(ROOT).as_posix(),
                         "bytes": len(delivery.png), "text": delivery.text,
                         "fallbackText": delivery.fallback_text})
    elif delivery.text:
        messages.append({"kind": "text", "text": delivery.text})
    return {
        "messages": messages,
        "outcomes": [
            {"game": item.game, "status": item.status, "hasCard": item.has_card}
            for item in outcomes
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/signin-preview")
    parser.add_argument(
        "--case", default="all",
        choices=("all", "both-success", "six-accounts", "one-failed", "one-role-failed", "other-unbound"),
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="skip the browser screenshot and print the text fallbacks instead",
    )
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    endfield_renderer = (
        None if args.html_only else endfield_cards.draw_attendance_card
    )
    arknights_renderer = (
        None if args.html_only else arknights_cards.draw_attendance_card
    )
    both_success = [
        capability(
            "endfield",
            endfield_success_view(),
            endfield_renderer,
            lambda view: "（终末地文字兜底结果）",
        ),
        capability(
            "arknights",
            arknights_success_view(),
            arknights_renderer,
            lambda view: "（明日方舟文字兜底结果）",
        ),
    ]
    one_failed = [
        capability(
            "endfield",
            endfield_success_view(),
            endfield_renderer,
            lambda view: "（终末地文字兜底结果）",
        ),
        capability(
            "arknights",
            None,
            None,
            lambda view: "",
            result=attendance_registry.AttendanceResult(
                ok=False,
                text="未配置环境变量 ARKNIGHTS_CREDENTIAL_KEY，明日方舟账号绑定与签到已禁用。",
            ),
        ),
    ]
    other_unbound = [
        capability(
            "arknights",
            arknights_success_view(),
            arknights_renderer,
            lambda view: "（明日方舟文字兜底结果）",
        ),
        capability("endfield", None, None, lambda view: "", roles=()),
    ]
    one_role_failed = [
        capability(
            "endfield",
            endfield_success_view(),
            endfield_renderer,
            lambda view: "（终末地文字兜底结果）",
        ),
        capability(
            "arknights",
            arknights_mixed_view(),
            arknights_renderer,
            lambda view: "（明日方舟文字兜底结果）",
        ),
    ]
    ef_six, ak_six = six_account_views()
    six_accounts = [
        capability("endfield", ef_six, endfield_renderer,
                   lambda view: "（终末地 3 个角色文字兜底结果）", roles=ef_six.roles),
        capability("arknights", ak_six, arknights_renderer,
                   lambda view: "（明日方舟 3 个角色文字兜底结果）", roles=ak_six.roles),
    ]

    report: dict[str, dict] = {}
    try:
        for name, current, capabilities in (
            ("both-success", "arknights", both_success),
            ("six-accounts", "endfield", six_accounts),
            ("one-failed", "endfield", one_failed),
            ("one-role-failed", "endfield", one_role_failed),
            ("other-unbound", "arknights", other_unbound),
        ):
            if args.case not in ("all", name):
                continue
            report[name] = await run_case(name, current, capabilities, output_dir=args.output_dir)
            print(f"== {name} ==")
            for message in report[name]["messages"]:
                if message["kind"] == "image":
                    print(f"  [图片] {message['path']} ({message['bytes']} bytes)")
                else:
                    print(f"  [文字] {message['text']}")
    finally:
        await close_browser()
        await close_image_executor()
        _runtime.cleanup()

    (args.output_dir / "preview.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwritten: {args.output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
