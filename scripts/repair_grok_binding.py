"""Offline recovery for Grok Bot conversation bindings.

Case A: the cloud Bot bound to a QQ conversation was deleted (or the cloud
account / computer was replaced). The plugin then refuses to send with
"当前会话的 Grok Bot 不存在或绑定不匹配，请管理员检查会话映射和云端 Bot。",
and `/grok 修复会话` cannot clear it by design: that command only clears an
unconfirmed creation record (`agent_id` is null) and never drops a bound chat,
because dropping it loses that conversation's history.

This tool removes only the binding entry you select, after a backup, so the next
question creates a fresh independent Bot. Run it with the bot STOPPED: the
running process keeps the bindings in memory and would write the entry back.

Usage from the bot project root:

    python -X utf8 scripts/repair_grok_binding.py                       # list only
    python -X utf8 scripts/repair_grok_binding.py --peer 123456         # dry run
    python -X utf8 scripts/repair_grok_binding.py --peer 123456 --yes   # remove
    python -X utf8 scripts/repair_grok_binding.py --agent-id <uuid> --yes

Exit codes: 0 success, 1 refused or failed (the file is left unchanged).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

DEFAULT_FILE = Path("data/grok_bot/sessions.json")


class RepairError(Exception):
    """A refusal or failure that never leaves the file half written."""


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RepairError(f"找不到会话映射文件：{path}（请在机器人项目根目录运行，或用 --file 指定）") from None
    except (OSError, UnicodeError, ValueError):
        raise RepairError(f"无法读取或解析：{path}，未做任何修改。") from None
    validate(data)
    return data


def validate(data) -> None:
    """Mirror SessionStore._load so a broken file is refused, not propagated."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RepairError("会话映射文件版本不是 1，未做任何修改。")
    try:
        UUID(data["installation"])
        bindings = data["bindings"]
        if not isinstance(bindings, dict):
            raise TypeError
        seen = set()
        for entry in bindings.values():
            UUID(entry["nonce"])
            agent_id = entry["agent_id"]
            if agent_id is not None:
                UUID(agent_id)
                if agent_id in seen:
                    raise ValueError
                seen.add(agent_id)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RepairError("会话映射文件结构不符合插件格式，未做任何修改。") from None


def bindings_of(data: dict) -> list[dict]:
    rows = []
    for key, entry in data["bindings"].items():
        scope = json.loads(key)
        if not isinstance(scope, list) or len(scope) != 5:
            raise RepairError("会话键格式不符合插件格式，未做任何修改。")
        platform, self_id, kind, peer_id, channel_id = scope
        digest = hashlib.sha256(key.encode()).hexdigest()
        rows.append({
            "key": key, "platform": str(platform), "self_id": str(self_id), "kind": str(kind),
            "peer_id": str(peer_id), "channel_id": str(channel_id),
            "name": f"多惠·{'群' if kind == 'group' else '私聊'}{str(peer_id)[:24]}·{digest[:8]}",
            "agent_id": entry["agent_id"],
            "marker": f"otae-qq-session:{data['installation']}:{digest}",
        })
    return rows


def describe(row: dict) -> str:
    return (f"{row['name']}\n"
            f"    平台/账号：{row['platform']} / {row['self_id']}\n"
            f"    范围：{row['kind']}  peer={row['peer_id']}  channel={row['channel_id'] or '-'}\n"
            f"    绑定 Bot：{row['agent_id'] or '（未确认的创建记录）'}\n"
            f"    会话标记：{row['marker']}")


def select(rows: list[dict], args: argparse.Namespace) -> dict:
    if args.agent_id:
        try:
            wanted = str(UUID(args.agent_id))
        except ValueError:
            raise RepairError("--agent-id 不是有效的 UUID，未做任何修改。") from None
        hits = [row for row in rows if row["agent_id"] == wanted]
    else:
        hits = [row for row in rows
                if row["peer_id"] == args.peer
                and (args.kind is None or row["kind"] == args.kind)
                and (args.platform is None or row["platform"] == args.platform)
                and (args.self_id is None or row["self_id"] == args.self_id)
                and (args.channel is None or row["channel_id"] == args.channel)]
    if not hits:
        raise RepairError("没有匹配的会话绑定，未做任何修改。请先用不带选择参数的方式列出全部绑定。")
    if len(hits) > 1:
        listed = "\n".join(f"  - {row['name']} ({row['platform']}/{row['self_id']}, {row['kind']}, "
                           f"peer={row['peer_id']}, channel={row['channel_id'] or '-'}, "
                           f"agent_id={row['agent_id']})" for row in hits)
        raise RepairError(f"匹配到多个会话绑定，未做任何修改。请补充 --kind/--channel/--self-id/--platform，"
                          f"或改用 --agent-id：\n{listed}")
    return hits[0]


def write(path: Path, data: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        raise RepairError("备份失败，未做任何修改。请检查目录权限。") from None
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except OSError:
        raise RepairError(f"写入失败，原文件保持不变；备份：{backup}") from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return backup


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(
        description="删除 Grok Bot 会话映射中已失效的绑定项（情况 A：云端 Bot 已删除）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE,
                        help=f"会话映射文件，默认 {DEFAULT_FILE}（相对机器人项目根目录）")
    parser.add_argument("--agent-id", help="按绑定的云端 Bot UUID 选择")
    parser.add_argument("--peer", help="按 QQ 群号或私聊用户号选择")
    parser.add_argument("--kind", choices=("group", "private"), help="限定 group 或 private")
    parser.add_argument("--channel", help="限定频道号；多频道平台下同群号可能有多个会话")
    parser.add_argument("--platform", help="限定平台，例如 qq")
    parser.add_argument("--self-id", help="限定机器人账号 self_id")
    parser.add_argument("--yes", action="store_true", help="真正写入；不加则为只读预演")
    args = parser.parse_args(argv)

    try:
        data = load(args.file)
        rows = bindings_of(data)
    except RepairError as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    print(f"文件：{args.file.resolve()}")
    print(f"installation：{data['installation']}    绑定项：{len(rows)}")
    if rows:
        print("")
        for row in rows:
            print(describe(row))
            print("")

    if not (args.agent_id or args.peer):
        print("以上为只读列表。确认某个会话在云端确实没有对应 Bot 后，用 --peer 或 --agent-id 选择它，")
        print("先不带 --yes 预演，再加 --yes 执行；执行前请先停止机器人进程。")
        return 0

    try:
        row = select(rows, args)
    except RepairError as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    print("将删除以下绑定项（只删这一项，其余会话不受影响）：")
    print(describe(row))
    print("")
    if row["agent_id"] is None:
        print("注意：该项是未确认的创建记录，删除它等同于 /grok 修复会话 的处理。")
    if not args.yes:
        print("这是预演，未做任何修改。确认无误后加 --yes 执行。")
        return 0

    updated = json.loads(json.dumps(data, ensure_ascii=False))
    del updated["bindings"][row["key"]]
    try:
        validate(updated)
    except RepairError as error:
        print(f"[错误] 删除后校验失败，未做任何修改：{error}", file=sys.stderr)
        return 1
    try:
        backup = write(args.file, updated)
    except RepairError as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    try:
        after = bindings_of(load(args.file))
    except RepairError as error:
        print(f"[错误] 写入后校验失败：{error}\n备份可用于还原：{backup}", file=sys.stderr)
        return 1

    print(f"已删除：{row['name']}（原绑定 {row['agent_id'] or '未确认创建记录'}）")
    print(f"备份：{backup}")
    print(f"剩余绑定项：{len(after)}")
    print("")
    print("下一步：启动机器人，在该会话执行 /grok 提问。插件会新建一个独立 Bot，")
    print("该会话此前的云端对话历史无法找回，其他会话不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
