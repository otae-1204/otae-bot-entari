#!/usr/bin/env python3
"""批量导出已绑定用户的终末地抽卡历史到 JSON。

用户列表来自鹰角 SDK 的 `GET /account/binding/v1/binding_list`
（`.runtime/skland_reverse/hg_sdk/725.*.js`），即每份凭据下的全部终末地 uid。
凭据来自 bot 本地库 `data/endfield/endfield.db`，用 ENDFIELD_CREDENTIAL_KEY 解密。

只读官方接口并写出 JSON，不改动 endfield.db。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import sys
import time
import types
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "endfield"
sys.path.insert(0, str(ROOT))

import otae_bot.config.settings  # noqa: E402,F401  导入即把 .env 载入 os.environ

# 以合成包加载插件模块，避免执行 plugins/endfield/handlers.py（会拉起整个 bot 依赖）。
_PACKAGE = "endfield_export_runtime"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(PLUGIN_DIR)]
sys.modules[_PACKAGE] = _package

account_client = importlib.import_module(f"{_PACKAGE}.account.client")
account_crypto = importlib.import_module(f"{_PACKAGE}.account.crypto")
account_store = importlib.import_module(f"{_PACKAGE}.account.store")

CHARACTER_POOL_TYPES = account_client.CHARACTER_POOL_TYPES
EndfieldAPIError = account_client.EndfieldAPIError
EndfieldOfficialClient = account_client.EndfieldOfficialClient
CredentialCipher = account_crypto.CredentialCipher
CredentialKeyError = account_crypto.CredentialKeyError
EndfieldStore = account_store.EndfieldStore
RoleCandidate = account_store.RoleCandidate

POOL_TYPE_LABELS = {
    "E_CharacterGachaPoolType_Special": "限定寻访",
    "E_CharacterGachaPoolType_Joint": "联合寻访",
    "E_CharacterGachaPoolType_Standard": "常驻寻访",
    "E_CharacterGachaPoolType_Beginner": "新手寻访",
}
MAX_PAGES = 500
UNSAFE_FILENAME = re.compile(r"[^0-9A-Za-z_.-]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "endfield" / "endfield.db", help="账号数据库路径")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output" / "endfield_gacha_history", help="JSON 导出目录"
    )
    parser.add_argument("--qq", action="append", default=[], help="只导出指定 QQ 用户，可重复")
    parser.add_argument("--concurrency", type=int, default=2, help="并发导出的角色数量")
    parser.add_argument("--delay", type=float, default=0.2, help="同一角色翻页之间的间隔秒数")
    parser.add_argument("--timeout", type=float, default=25.0, help="单次 HTTP 请求超时秒数")
    return parser.parse_args()


def safe_name(value: str) -> str:
    return UNSAFE_FILENAME.sub("_", str(value)) or "unknown"


def load_credentials(store: EndfieldStore, cipher: CredentialCipher, qq_filter: list[str]) -> list[dict[str, Any]]:
    """按凭据聚合本地绑定用户；每份凭据对应一本鹰角通行证。"""
    wanted = {str(item) for item in qq_filter}
    rows = store.conn.execute("SELECT DISTINCT qq_user_id FROM roles ORDER BY qq_user_id").fetchall()
    credentials: dict[int, dict[str, Any]] = {}
    for row in rows:
        qq_user_id = str(row["qq_user_id"])
        if wanted and qq_user_id not in wanted:
            continue
        for role in store.list_roles(qq_user_id):
            entry = credentials.get(role.credential_id)
            if entry is None:
                try:
                    token = store.decrypt_token(role, cipher)
                except (CredentialKeyError, LookupError) as exc:
                    print(f"[skip] credential {role.credential_id} 解密失败：{exc}", file=sys.stderr)
                    credentials[role.credential_id] = {"token": "", "qq_user_id": qq_user_id, "db_roles": []}
                    continue
                entry = {"token": token, "qq_user_id": qq_user_id, "db_roles": []}
                credentials[role.credential_id] = entry
            entry["db_roles"].append(role)
    return [
        {"credential_id": credential_id, **entry}
        for credential_id, entry in sorted(credentials.items())
        if entry["token"]
    ]


async def resolve_roles(client: EndfieldOfficialClient, entry: dict[str, Any]) -> tuple[list[RoleCandidate], str]:
    """SDK binding_list 拿该通行证下全部终末地 uid；失败时退回库内绑定角色。"""
    db_roles = [
        RoleCandidate(
            binding_uid=role.binding_uid, role_id=role.role_id, server_id=role.server_id,
            nickname=role.nickname, server_name=role.server_name,
        )
        for role in entry["db_roles"]
    ]
    try:
        api_roles = await client.get_gacha_roles(entry["token"])
    except EndfieldAPIError as exc:
        return db_roles, str(exc)
    merged = list(api_roles)
    known = {(role.role_id, role.server_id) for role in merged}
    for role in db_roles:
        if (role.role_id, role.server_id) not in known:
            merged.append(role)
    return merged, ""


async def collect_stream(
    fetch_page: Callable[[str], Awaitable[Any]], delay: float
) -> tuple[list[Any], str]:
    records: list[Any] = []
    cursor = ""
    seen_cursors: set[str] = set()
    for _ in range(MAX_PAGES):
        try:
            page = await fetch_page(cursor)
        except EndfieldAPIError as exc:
            return records, str(exc)
        records.extend(page.records)
        cursor = page.next_seq_id
        if not page.has_more or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        if delay > 0:
            await asyncio.sleep(delay)
    return records, ""


async def export_role(
    client: EndfieldOfficialClient,
    entry: dict[str, Any],
    role: RoleCandidate,
    output_dir: Path,
    delay: float,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "qq_user_id": entry["qq_user_id"],
        "role_id": role.role_id,
        "server_id": role.server_id,
        "nickname": role.nickname,
        "records": 0,
        "errors": [],
    }
    u8_token = await client.get_u8_token(entry["token"], role.binding_uid)
    try:
        character_names = await client.character_pool_names(u8_token, role.server_id)
    except EndfieldAPIError as exc:
        character_names = {}
        summary["errors"].append(f"角色卡池名称：{exc}")
    try:
        weapon_pools = await client.weapon_pools(u8_token, role.server_id)
    except EndfieldAPIError as exc:
        weapon_pools = []
        summary["errors"].append(f"武器卡池列表：{exc}")

    streams: list[dict[str, Any]] = []
    for pool_type in CHARACTER_POOL_TYPES:
        label = character_names.get(pool_type) or POOL_TYPE_LABELS.get(pool_type, pool_type)
        records, error = await collect_stream(
            lambda cursor, pool_type=pool_type, label=label: client.character_records(
                role, u8_token, pool_type, seq_id=cursor, pool_name=label
            ),
            delay,
        )
        if error:
            summary["errors"].append(f"{label}：{error}")
        streams.append(
            {
                "stream": f"char:{pool_type}", "label": label, "complete": not error,
                "records": [asdict(record) for record in records],
            }
        )
    records, error = await collect_stream(
        lambda cursor: client.weapon_records(role, u8_token, seq_id=cursor), delay
    )
    if error:
        summary["errors"].append(f"武器申领：{error}")
    streams.append(
        {
            "stream": "weapon:all", "label": "武器申领", "complete": not error,
            "records": [asdict(record) for record in records],
        }
    )

    summary["records"] = sum(len(stream["records"]) for stream in streams)
    payload = {
        "exported_at": int(time.time()),
        "qq_user_id": entry["qq_user_id"],
        "role": {
            "binding_uid": role.binding_uid, "role_id": role.role_id, "server_id": role.server_id,
            "nickname": role.nickname, "server_name": role.server_name,
        },
        "weapon_pools": [{"pool_id": pool_id, "pool_name": name} for pool_id, name in weapon_pools],
        "total_records": summary["records"],
        "complete": not summary["errors"],
        "errors": summary["errors"],
        "streams": streams,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{safe_name(entry['qq_user_id'])}_{safe_name(role.role_id)}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["file"] = str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target)
    return summary


async def run(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    try:
        cipher = CredentialCipher.from_env()
    except CredentialKeyError as exc:
        print(f"{exc}（请在 .env 配置 ENDFIELD_CREDENTIAL_KEY）", file=sys.stderr)
        return 2

    store = EndfieldStore(args.db)
    credentials = load_credentials(store, cipher, args.qq)
    if not credentials:
        store.close()
        print("没有可导出的绑定用户", file=sys.stderr)
        return 1

    client = EndfieldOfficialClient(timeout=args.timeout)
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    async def guarded(entry: dict[str, Any], role: RoleCandidate) -> None:
        async with semaphore:
            try:
                summary = await export_role(client, entry, role, args.output, args.delay)
            except EndfieldAPIError as exc:
                failures.append(
                    {"qq_user_id": entry["qq_user_id"], "role_id": role.role_id, "error": str(exc)}
                )
                print(f"[fail] {entry['qq_user_id']} / {role.role_id}：{exc}", file=sys.stderr)
                return
            summaries.append(summary)
            state = "ok" if not summary["errors"] else "partial"
            print(f"[{state}] {entry['qq_user_id']} / {role.role_id} {summary['records']} 条 -> {summary['file']}")

    try:
        jobs: list[Awaitable[None]] = []
        for entry in credentials:
            roles, error = await resolve_roles(client, entry)
            if error:
                print(f"[warn] {entry['qq_user_id']} 用户列表接口失败，改用库内绑定：{error}", file=sys.stderr)
            if not roles:
                failures.append({"qq_user_id": entry["qq_user_id"], "role_id": "", "error": error or "无终末地角色"})
                continue
            jobs.extend(guarded(entry, role) for role in roles)
        await asyncio.gather(*jobs)
    finally:
        await client.close()
        store.close()

    args.output.mkdir(parents=True, exist_ok=True)
    index = {
        "exported_at": int(time.time()),
        "credentials": len(credentials),
        "roles": len(summaries),
        "records": sum(item["records"] for item in summaries),
        "results": summaries,
        "failures": failures,
    }
    (args.output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"完成：{len(summaries)} 个角色，{index['records']} 条记录，失败 {len(failures)} 个 -> {args.output}"
    )
    return 0 if not failures else 1


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
