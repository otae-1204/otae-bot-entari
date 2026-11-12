import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "skland_reverse"
sys.path.insert(0, str(RUNTIME_DIR))

from reproduce_endfield import (
    RESPONSE_DIR,
    load_session,
    pick_endfield_role,
    redact,
    refresh_sign_context,
    request_json,
    save_json,
)


ENDPOINTS = {
    "detail": "/api/v1/game/endfield/card/detail",
    "war_echoes": "/api/v1/game/endfield/card/war-echoes",
    "crisis_contract": "/api/v1/game/endfield/card/crisis-contract",
    "indie_hard": "/api/v1/game/endfield/card/indie-hard",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Query public Endfield profile data with an existing Skland session."
    )
    parser.add_argument(
        "--user-id",
        help="Skland internal user ID used as otherUid (not profile/share ID).",
    )
    parser.add_argument("--role-id", help="Endfield role ID; bypasses binding lookup.")
    parser.add_argument("--server-id", default="1", help="Endfield server ID.")
    parser.add_argument("--label", default="public", help="Safe output filename label.")
    parser.add_argument("--output", type=Path, help="Markdown report path.")
    return parser.parse_args()


def safe_label(value):
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def character_names(detail):
    result = {}
    chars = (((detail.get("data") or {}).get("detail") or {}).get("chars") or [])
    for char in chars:
        char_id = char.get("id")
        name = (char.get("charData") or {}).get("name")
        if char_id and name:
            result[char_id] = name
    return result


def format_time(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def format_duration(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_team(record, names):
    result = []
    for char in (record or {}).get("chars") or []:
        name = names.get(char.get("charId"), char.get("charId", "unknown"))
        result.append(
            f"{name} Lv.{char.get('level', '-')} 潜能{char.get('potentialLevel', '-')}"
        )
    return "、".join(result) or "-"


def build_markdown(label, role_id, server_id, responses):
    names = character_names(responses["detail"])
    detail = ((responses["detail"].get("data") or {}).get("detail") or {})
    base = detail.get("base") or {}
    lines = [
        f"# {label} 的终末地公开数据",
        "",
        "## 个人详情",
        "",
        f"- 角色名：{base.get('name', '-')}",
        f"- 角色 ID：`{role_id}`",
        f"- 服务器 ID：`{server_id}`",
        f"- 等级：{base.get('level', '-')}",
        f"- 世界等级：{base.get('worldLevel', '-')}",
        f"- 主线进度：{(base.get('mainMission') or {}).get('description', '-')}",
        f"- 干员数量：{base.get('charNum', '-')}",
        f"- 武器数量：{base.get('weaponNum', '-')}",
        f"- 档案数量：{base.get('docNum', '-')}",
        f"- 查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 战争回响",
        "",
    ]

    war_echoes = (responses["war_echoes"].get("data") or {}).get("warEchoes")
    if war_echoes is None:
        lines.extend(
            [
                "- 接口状态：成功（`code: 0`）",
                "- 业务字段：`warEchoes: null`",
                "- 结论：当前没有可展示的战争回响数据，或该展示项尚未开启。",
            ]
        )
    else:
        achieves = war_echoes.get("achieves") or []
        lines.append(f"- 荣誉记录：{len(achieves)} 项")
        for season in war_echoes.get("seasons") or []:
            lines.extend(
                [
                    "",
                    f"### {season.get('name', '未命名赛季')}",
                    "",
                    f"- 赛季星数：{season.get('stars', '-')}；附加目标全完成：{'是' if season.get('allPlusTasks') else '否'}",
                    f"- 开放时间：{format_time(season.get('startTs'))} 至 {format_time(season.get('endTs'))}",
                ]
            )
            for week in season.get("weeks") or []:
                lines.append(
                    f"- **{week.get('name', '未命名轮换')}**：{week.get('stars', '-')} 星；附加目标全完成：{'是' if week.get('allPlusTasks') else '否'}"
                )
                for group in week.get("dungeonGroups") or []:
                    records = []
                    for key, difficulty in (
                        ("普通", group.get("normalDungeon")),
                        ("困难", group.get("hardDungeon")),
                        ("残酷", group.get("cruelDungeon")),
                    ):
                        if not difficulty:
                            continue
                        record = difficulty.get("bestRecord") or {}
                        result = "已通过" if difficulty.get("isPass") else "未通过"
                        if record:
                            result += f"，最佳 {format_duration(record.get('passTs'))}"
                        records.append(f"{key}{result}")
                    lines.append(
                        f"  - {group.get('name', '-')}：{group.get('star', '-')} 星；"
                        + "；".join(records)
                    )

    lines.extend(
        [
            "",
            "## 危机合约",
            "",
        ]
    )

    crisis = (responses["crisis_contract"].get("data") or {}).get("crisisContract")
    if crisis is None:
        lines.extend(
            [
                "- 接口状态：成功（`code: 0`）",
                "- 业务字段：`crisisContract: null`",
                "- 结论：当前没有可公开展示的危机合约记录，或该展示项尚未开启。",
            ]
        )
    else:
        status = crisis.get("status") or {}
        best = (crisis.get("history") or {}).get("bestRecord") or {}
        achieve = status.get("achieve") or {}
        achievement = achieve.get("achievementData") or {}
        lines.extend(
            [
                f"- 活动：{status.get('name', '-')}",
                f"- 最高指标：{status.get('highest', '-')}",
                f"- 挑战次数：{status.get('challengeCount', '-')}",
                f"- 最佳记录：指标 {best.get('indicatorCount', '-')}，波次 {best.get('passWave', '-')}，耗时 {format_duration(best.get('passTs'))}",
                f"- 记录时间：{format_time(best.get('ts'))}",
                f"- 奖章：{achievement.get('name', '-')}；镀层：{'是' if achieve.get('isPlated') else '否'}",
                f"- 队伍：{format_team(best, names)}",
            ]
        )

    lines.extend(["", "## 影拓丰碑", ""])
    indie_hard = (responses["indie_hard"].get("data") or {}).get("indieHard")
    if indie_hard is None:
        lines.extend(
            [
                "- 接口状态：成功（`code: 0`）",
                "- 业务字段：`indieHard: null`",
                "- 结论：当前没有可公开展示的影拓丰碑记录，或该展示项尚未开启。",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    for group in indie_hard.get("indieHardGroups") or []:
        lines.extend([f"### {group.get('name', '未命名主题')}", ""])
        for pair in group.get("dungeonGroups") or []:
            for key in ("normalDungeon", "hardDungeon"):
                dungeon = pair.get(key) or {}
                if not dungeon:
                    continue
                record = dungeon.get("bestRecord") or {}
                lines.extend(
                    [
                        f"- **{dungeon.get('name', '-')}**：{'已通过' if dungeon.get('isPass') else '未通过'}",
                        f"  - 最佳耗时：{format_duration(record.get('passTs'))}",
                        f"  - 记录时间：{format_time(record.get('ts'))}",
                        f"  - 队伍：{format_team(record, names)}",
                    ]
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main():
    args = parse_args()
    if bool(args.role_id) == bool(args.user_id):
        raise SystemExit("exactly one of --user-id or --role-id is required")

    label = safe_label(args.label)
    session = load_session()
    cred = session["cred"]["data"]["cred"]
    d_id = session.get("dId", "")
    token, timestamp = refresh_sign_context(cred, d_id)

    role_id = args.role_id
    server_id = args.server_id
    if args.user_id:
        binding, _ = request_json(
            cred,
            token,
            "/api/v1/game/player/binding",
            {"uid": args.user_id},
            timestamp=timestamp,
            d_id=d_id,
        )
        save_json(RESPONSE_DIR / f"public_{label}_binding_sensitive.json", binding)
        save_json(RESPONSE_DIR / f"redacted_public_{label}_binding.json", redact(binding))
        if binding.get("code") != 0:
            raise SystemExit(
                "binding lookup failed: "
                f"code={binding.get('code')} message={binding.get('message')}; "
                "--user-id must be the internal otherUid, not profile/share ID"
            )
        role, _ = pick_endfield_role(binding)
        role_id = role["roleId"]
        server_id = role["serverId"]

    params = {"roleId": role_id, "serverId": server_id}
    if args.user_id:
        params["userId"] = args.user_id

    responses = {}
    for name, path in ENDPOINTS.items():
        response, _ = request_json(
            cred, token, path, params, timestamp=timestamp, d_id=d_id
        )
        save_json(RESPONSE_DIR / f"public_{label}_{name}_sensitive.json", response)
        save_json(RESPONSE_DIR / f"redacted_public_{label}_{name}.json", redact(response))
        if response.get("code") != 0:
            raise SystemExit(
                f"{name} failed: code={response.get('code')} message={response.get('message')}"
            )
        responses[name] = response

    best_record = (
        (((responses["crisis_contract"].get("data") or {}).get("crisisContract") or {}).get("history") or {}).get("bestRecord")
        or {}
    )
    record_id = best_record.get("id")
    if record_id:
        record_response, _ = request_json(
            cred,
            token,
            "/api/v1/game/endfield/card/crisis-contract/record",
            {**params, "recordId": record_id},
            timestamp=timestamp,
            d_id=d_id,
        )
        save_json(RESPONSE_DIR / f"public_{label}_crisis_record_sensitive.json", record_response)
        save_json(RESPONSE_DIR / f"redacted_public_{label}_crisis_record.json", redact(record_response))
        if record_response.get("code") == 0:
            responses["crisis_record"] = record_response

    output = args.output or RESPONSE_DIR / f"public_{label}_summary.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        build_markdown(args.label, role_id, server_id, responses), encoding="utf-8"
    )
    print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
