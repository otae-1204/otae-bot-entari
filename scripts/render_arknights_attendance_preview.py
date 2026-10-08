"""Render synthetic Arknights attendance cards offline.

Never reads ``.env``, the account database, or any real credential: every role
below is fabricated.  Imports the plugin modules through a throwaway package so
the Entari command registration in ``handlers.py`` never runs.

Usage:
    .venv/Scripts/python.exe scripts/render_arknights_attendance_preview.py
    ... --output-dir output/arknights-attendance --html-only

Outputs ``<case>.html``, ``<case>.png`` and ``validation.json``.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from lxml import html as lxml_html
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PACKAGE = "arknights_attendance_preview"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins/arknights")]
sys.modules[PACKAGE] = package

# Shared rendering settings normally load .env relative to cwd. Import from an
# empty temporary workspace so this offline tool never reads the bot's secrets.
_runtime = tempfile.TemporaryDirectory(prefix="arknights-preview-")
_previous_cwd = Path.cwd()
try:
    os.chdir(_runtime.name)
    models = importlib.import_module(f"{PACKAGE}.models")
    cards = importlib.import_module(f"{PACKAGE}.rendering.cards")
    from otae_bot.infrastructure.rendering.browser import close_browser
    from otae_bot.infrastructure.rendering.executor import close_image_executor
finally:
    os.chdir(_previous_cwd)



def sample_view(name: str) -> models.AttendanceCardView:
    """All amounts and milestone days are synthetic layout fixtures, not a reward schedule."""
    success = models.AttendanceRoleView(
        nickname="夜岚",
        uid="****1024",
        full_uid="10001024",
        channel_name="官服",
        status="success",
        rewards=(
            models.AttendanceRewardView("合成玉", 500),
            models.AttendanceRewardView("初级作战记录", 3),
        ),
    )
    monthly = replace(success, monthly_count=8)
    if name.startswith("milestone-"):
        # Fabricated fixture days/amounts deliberately do not encode official rules.
        stage = name.removeprefix("milestone-")
        signed = 17 if stage == "all-done" else 8
        milestones = () if stage == "missing" else tuple(
            models.AttendanceMilestoneView(
                day=day, name="合成玉", count=amount,
                done=stage == "all-done" or index == 0,
                available=stage == "available" and index == 1,
            ) for index, (day, amount) in enumerate(((4, 100), (9, 200), (17, 300)))
        )
        if stage == "dense":
            milestones = tuple(models.AttendanceMilestoneView(
                day=day, name="合成玉", count=999999 if day == 2 else 100,
                done=day <= signed,
            ) for day in (1, 2, 3, 30, 31))
        return models.AttendanceCardView(
            roles=(replace(success, monthly_count=None if stage in {"missing", "unknown"} else signed,
                           status="already" if stage == "already" else "success",
                           rewards=() if stage == "already" else success.rewards,
                           daily_progress=() if stage in {"missing", "unknown"} else tuple(day <= signed for day in range(1, 32)),
                           milestones=milestones),), generated_at="2026-10-08 11:06",
        )
    return models.AttendanceCardView(
        roles={
            "single": (success,),
            "monthly": (monthly,),
            "mixed": (
                success,
                models.AttendanceRoleView(
                    nickname="B服小号", uid="****8888", full_uid="20008888",
                    channel_name="B服", status="already",
                ),
                models.AttendanceRoleView(
                    nickname="失效账号 <备用>", uid="****6666", full_uid="30006666",
                    channel_name="官服", status="failed",
                    message="森空岛凭据已失效或登录状态异常，请重新私聊使用 /ak 绑定。",
                ),
            ),
            "boundaries": (
                models.AttendanceRoleView(
                    nickname="这是一个用于检查换行的超长昵称ABCDEFGHIJKLMNOPQRSTUVWXYZ博士",
                    uid="----", channel_name="官服", status="success",
                    rewards=(models.AttendanceRewardView("超长奖励名称_" + "REWARD" * 15, 999999),),
                ),
                models.AttendanceRoleView(
                    nickname="<script>alert(1)</script>", uid="****0001",
                    channel_name="B服", status="failed",
                    message='服务端返回 <img src="https://example.invalid/x.png"> 与超长文本 ' + "ERR_" * 40,
                ),
                models.AttendanceRoleView(nickname="无渠道", uid="****0002", status="already"),
            ),
            "empty": (),
            "all-already": tuple(models.AttendanceRoleView(
                nickname=f"博士 {index + 1}", uid=f"****{index:04}",
                channel_name="官服" if index == 0 else "B服", status="already",
            ) for index in range(2)),
            "all-failed": (models.AttendanceRoleView(
                nickname="远行的博士", uid="****1024", channel_name="官服", status="failed",
                message="森空岛凭据已失效，请重新私聊使用 /ak 绑定。",
            ),),
            "many": tuple(models.AttendanceRoleView(
                nickname=f"罗德岛博士 {index + 1:02}", uid=f"****{index:04}",
                channel_name="官服" if index % 2 == 0 else "B服",
                status=("success", "already", "failed")[index % 3],
                rewards=success.rewards if index % 3 == 0 else (),
                message="网络请求失败，请稍后重试。" if index % 3 == 2 else "",
            ) for index in range(12)),
        }[name],
        generated_at="2026-10-08 11:06",
    )


def inspect_html(document: str) -> dict:
    """Static layout/security checks that need no browser."""
    icons = importlib.import_module(f"{PACKAGE}.rendering.icons")
    allowed = {icons.reward_icon_url(name) for name in icons.REWARD_ICON_IDS}
    tree = lxml_html.fromstring(document)
    unsafe_images = [node for node in tree.findall('.//img') if node.get('src') not in allowed]
    resources_removed = document
    for url in allowed - {""}:
        resources_removed = resources_removed.replace(url, "bundled-icon")
    return {
        "bytes": len(document.encode("utf-8")),
        "remoteUrls": len(re.findall(r"(?:https?|data|file|blob):", resources_removed)),
        "rawTags": sorted(set(re.findall(r"<(?:script|iframe)\b", document, re.IGNORECASE))) + (["<img"] if unsafe_images else []),
        "rows": document.count('class="ak-row'),
        "hasPlaceholder": cards._URL_PLACEHOLDER in document,
    }


async def render_png(view: models.AttendanceCardView) -> bytes:
    return await cards.draw_attendance_card(view)


async def render_zmd_reference(output_dir: Path, ref: str | None = None) -> None:
    """Execute only the attendance rendering functions, never the plugin.

    The source is read-only, the view synthetic and reward URLs empty. This
    keeps the comparison tied to the actual local baseline without importing
    Endfield's registration, account client or any of its real data.
    """
    source = ROOT / "plugins/endfield/rendering/cards.py"
    if ref:
        revision = (await asyncio.to_thread(
            subprocess.check_output, ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=ROOT, text=True,
        )).strip()
        content = (await asyncio.to_thread(
            subprocess.check_output, ["git", "show", f"{revision}:plugins/endfield/rendering/cards.py"], cwd=ROOT,
        )).decode("utf-8")
    else:
        revision = "working-tree"
        content = source.read_text(encoding="utf-8")
    tree = ast.parse(content)
    names = {"draw_attendance_card", "_draw_neutral_card", "_attendance_milestone_strip"}
    selected = [node for node in tree.body if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name in names]
    if not {"draw_attendance_card", "_draw_neutral_card"} <= {node.name for node in selected}:
        raise RuntimeError("Could not locate the Endfield reference functions")

    async def passthrough(_function, output):
        return output

    def capture_html(content):
        (output_dir / "zmd-reference.html").write_text(content, encoding="utf-8")
        return cards._write_temp_html(content)

    scope = {
        "AttendanceCardView": models.AttendanceCardView,
        "AttendanceRoleView": models.AttendanceRoleView,
        "esc": cards.esc, "esc_attr": cards.esc_attr,
        "server_label": lambda name: name,
        "_write_temp_html": capture_html,
        "screenshot_web_element": cards.screenshot_web_element,
        "schedule_temp_file_cleanup": cards.schedule_temp_file_cleanup,
        "CARD_MAX_HEIGHT": cards.CARD_MAX_HEIGHT,
        "run_image_render": passthrough, "optimize_png_container": None,
    }
    # Only named repository function definitions execute; no plugin imports or network requests.
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), scope)  # noqa: S102
    reference_roles = tuple(types.SimpleNamespace(
        nickname=role.nickname, uid=role.uid, server_name=role.server_label,
        status=role.status, message=role.status_label,
        monthly_count=getattr(role, "monthly_count", None),
        calendar_days=31,
        milestones=tuple(types.SimpleNamespace(day=item.day, count=item.count, icon_url="")
                         for item in role.milestones),
        rewards=tuple(types.SimpleNamespace(name=item.name, count=item.count, icon_url="")
                      for item in role.rewards),
    ) for role in sample_view("milestone-next").roles)
    view = types.SimpleNamespace(roles=reference_roles, generated_at="2026-10-08 11:06")
    png = await scope["draw_attendance_card"](view)
    (output_dir / "zmd-reference.png").write_bytes(png)
    (output_dir / "zmd-reference-source.txt").write_text(
        f"{revision}\nplugins/endfield/rendering/cards.py\n", encoding="utf-8",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/arknights-attendance")
    parser.add_argument("--html-only", action="store_true", help="skip the browser screenshot")
    parser.add_argument("--zmd-reference", action="store_true", help="render a synthetic comparison from the local Endfield card functions")
    parser.add_argument("--zmd-ref", help="read the Endfield reference from a fetched Git revision, e.g. upstream/main")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, dict] = {}
    try:
        for name in ("single", "mixed", "boundaries", "empty", "all-already", "all-failed", "many", "monthly", "milestone-next", "milestone-available", "milestone-all-done", "milestone-missing", "milestone-unknown", "milestone-already", "milestone-dense"):
            view = sample_view(name)
            document = cards.render_attendance_card_html(view)
            (args.output_dir / f"{name}.html").write_text(document, encoding="utf-8")
            checks = inspect_html(document)
            assert not checks["remoteUrls"], (name, checks)
            assert not checks["rawTags"], (name, checks)
            if not args.html_only:
                png = await render_png(view)
                assert png.startswith(b"\x89PNG"), name
                (args.output_dir / f"{name}.png").write_bytes(png)
                checks["pngBytes"] = len(png)
                with Image.open(BytesIO(png)) as image:
                    checks["width"], checks["height"] = image.size
                assert checks["width"] == cards.CARD_WIDTH * 2, (name, checks)
                assert checks["width"] / checks["height"] < 3, (name, checks)
            report[name] = checks
            print(f"{name}: {json.dumps(checks, ensure_ascii=False)}")
        if args.zmd_reference and not args.html_only:
            await render_zmd_reference(args.output_dir, args.zmd_ref)
    finally:
        await close_browser()
        await close_image_executor()
        _runtime.cleanup()

    (args.output_dir / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
