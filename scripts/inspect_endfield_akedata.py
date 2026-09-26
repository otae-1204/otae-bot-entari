"""Record public AKE schemas for migration checks; never accepts credentials.

两个模式：

- 默认模式下载 `--tables` 里的表到 `--output`，按版本目录落盘。
- `--report encyclopedia` 只读已落盘的 JSON，打印图鉴需要的分类事实（零请求）。

报告模式刻意不构造 `httpx.AsyncClient`：图鉴第 0 期的结论必须能从别人已经
下载过的目录里复现，而不是每次再打一遍 AKE。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import httpx

DEFAULT_TABLES = (
    "CharacterTable,CharGrowthTable,CharacterPotentialTable,CharWpnRecommendTable,"
    "CharProfessionTable,CharTypeTable,CharBattleTagTable,TagDataTable,ItemTable,"
    "I18nTextTable_CN,SkillPatchTable,PotentialTalentEffectTable,WeaponBasicTable,"
    "WeaponUpgradeTemplateTable,WeaponBreakThroughTemplateTable,WeaponTalentTemplateTable,"
    "EquipTable,EquipSuitTable,AttributeShowConfigTable,AttributeFilterTable,SystemJumpTable,"
    "EquipFormulaTable,RichTextStyleTable,HyperlinkTextTable,GachaCharPoolTable,"
    "GachaWeaponPoolTable,ActivityTable,TimeRangeTable,"
    # 图鉴第 0 期追加：分类、道具、敌人
    "ItemTypeTable,ItemShowingTypeTable,UseItemTable,EquipItemTable,EnemyTable,"
    "EnemyTemplateDisplayInfoTable,EnemyAttributeTemplateTable,EnemyAbilityDescTable,"
    # 报告要回答「distributionIds 的表名」，那张表必须在本地
    "DistributionInfoTable"
)

# 报告要求这些表落盘；缺一张就把下载命令打回去，不用半份报告骗人。
REPORT_TABLES = (
    "ItemTable",
    "ItemTypeTable",
    "ItemShowingTypeTable",
    "UseItemTable",
    "EquipItemTable",
    "EnemyTable",
    "EnemyTemplateDisplayInfoTable",
    "EnemyAttributeTemplateTable",
    "EnemyAbilityDescTable",
    "I18nTextTable_CN",
    "SystemJumpTable",
)

# v2 §4.1 由展示类型反推白名单时用到的七个展示类型
SHOWING_TYPES_OF_INTEREST = (1, 2, 4, 15, 16, 17, 18)

PLACEHOLDER_RE = re.compile(r"\{([^{}:]+)(?::[^{}]*)?\}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/endfield-ake-migration/public")
    )
    parser.add_argument("--tables", default=DEFAULT_TABLES)
    parser.add_argument(
        "--report",
        choices=["encyclopedia"],
        help="只读已落盘的 JSON 打印图鉴分类事实，不发任何请求",
    )
    return parser.parse_args(argv)


async def download(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=True,
        timeout=40,
        headers={
            "User-Agent": "otae-bot-entari/1.0",
            "Referer": "https://cf.akedata.top/",
        },
    ) as client:
        manifest = (await client.get("https://data.akedata.wiki/manifest.json")).json()
        version = next(v for v in manifest["versions"] if v["id"] == manifest["latest"])
        args.output.joinpath("manifest.json").write_text(json.dumps(manifest))
        directory = args.output / version["id"]
        directory.mkdir(exist_ok=True)
        semaphore = asyncio.Semaphore(3)

        async def fetch(name):
            if not name.replace("_", "").isalnum():
                raise ValueError("Invalid table")
            target = directory / f"{name}.json"
            async with semaphore:
                try:
                    if not target.exists():
                        response = await client.get(
                            f"https://data.akedata.wiki/{version['tableCfgPath']}/{name}.json"
                        )
                        response.raise_for_status()
                        response.json()
                        target.write_bytes(response.content)
                    payload = json.loads(target.read_bytes())
                    sample = (
                        next(iter(payload.values()), {})
                        if isinstance(payload, dict)
                        else {}
                    )
                    print(
                        json.dumps(
                            {
                                "table": name,
                                "bytes": target.stat().st_size,
                                "rows": len(payload),
                                "fields": list(sample)
                                if isinstance(sample, dict)
                                else type(sample).__name__,
                            }
                        ),
                        flush=True,
                    )
                except Exception as error:
                    print(
                        json.dumps({"table": name, "error": type(error).__name__}),
                        flush=True,
                    )

        print(json.dumps({"version": version}), flush=True)
        await asyncio.gather(*(fetch(name) for name in args.tables.split(",")))
    return 0


# ---------------------------------------------------------------- report


def _load_table(directory: Path, name: str) -> Any:
    return json.loads((directory / f"{name}.json").read_bytes())


def _localized_translator(i18n: dict[str, Any]):
    def translate(value: Any) -> str:
        if isinstance(value, dict) and "id" in value and "text" in value:
            return str(i18n.get(str(value["id"])) or value.get("text") or "")
        if value is None:
            return ""
        return str(value)

    return translate


def _missing_tables(directory: Path, names: Iterable[str]) -> list[str]:
    return [name for name in names if not (directory / f"{name}.json").exists()]


def report_encyclopedia(output_dir: Path) -> int:
    """打印图鉴第 0 期要的分类事实；不构造 httpx 客户端，不发请求。"""
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"缺少 {manifest_path}；先不带 --report 跑一次下载。")
        return 1
    manifest = json.loads(manifest_path.read_bytes())
    latest = str(manifest["latest"])
    directory = output_dir / latest
    missing = _missing_tables(directory, REPORT_TABLES)
    if missing:
        print(f"缺少表：{', '.join(missing)}")
        print("先不带 --report 跑一次下载（--tables 默认串已含这些表）。")
        return 1

    i18n = _load_table(directory, "I18nTextTable_CN")
    translate = _localized_translator(i18n)

    _report_item_types(directory, translate)
    _report_showing_types(directory, translate)
    _report_enemy_display(directory, translate)
    _report_prop_effects(directory, translate)

    print()
    print("# BuffData.applyTags 是否含 ba.* 未在本报告探；只记一行，不进任何一期。")
    return 0


def _report_item_types(directory: Path, translate) -> None:
    rows = _load_table(directory, "ItemTypeTable")
    items = _load_table(directory, "ItemTable")
    print()
    print("## ItemTypeTable（全部 id → 中文名，供 ITEM_TYPE_WHITELIST 与排除类型）")
    print(f"rows={len(rows)}")
    counts = Counter(row.get("type") for row in items.values())
    for key in sorted(rows, key=lambda value: int(value)):
        row = rows[key]
        type_id = row.get("itemType", key)
        print(f"{type_id}\t{translate(row.get('name'))}\t物品行数={counts.get(type_id, 0)}")
    for type_id in (76, 12, 73, 72, 5, 6, 100, 48, 52, 55, 8, 47, 19):
        row = rows.get(str(type_id)) or {}
        print(f"spot-check type {type_id} -> {translate(row.get('name'))}")


def _report_showing_types(directory: Path, translate) -> None:
    rows = _load_table(directory, "ItemShowingTypeTable")
    items = _load_table(directory, "ItemTable")
    type_rows = _load_table(directory, "ItemTypeTable")
    buckets: dict[Any, Counter] = defaultdict(Counter)
    for row in items.values():
        buckets[row.get("showingType")][row.get("type")] += 1
    print()
    print("## showingType → type（白名单里由展示类型反推的部分）")
    for showing_type in SHOWING_TYPES_OF_INTEREST:
        name = translate((rows.get(str(showing_type)) or {}).get("name"))
        counts = buckets.get(showing_type) or Counter()
        print(f"showingType {showing_type} [{name}] rows={sum(counts.values())}")
        for type_id, count in counts.most_common():
            type_name = translate((type_rows.get(str(type_id)) or {}).get("name"))
            print(f"    type {type_id}={type_name}×{count}")
    zero = sum(1 for row in items.values() if not row.get("showingType"))
    print(f"showingType 为空/0 的行数={zero}（不按展示类型汇总）")


def _report_enemy_display(directory: Path, translate) -> None:
    templates = _load_table(directory, "EnemyTemplateDisplayInfoTable")
    enemies = _load_table(directory, "EnemyTable")
    print()
    print("## 敌人 displayType / distributionIds / attrTemplateId")
    print(f"EnemyTemplateDisplayInfoTable rows={len(templates)}")
    distribution = Counter(row.get("displayType") for row in templates.values())
    print(f"displayType 分布={dict(sorted(distribution.items(), key=lambda kv: str(kv[0])))}")
    enum_table = _find_enum_table(directory, "displayType", "EnemyTemplateDisplayInfoTable")
    if enum_table:
        print(f"displayType 枚举表= {enum_table} → DISPLAY_TYPE_NAMES")
    else:
        print("displayType 枚举表= 未找到 → DISPLAY_TYPE_NAMES 留空，兜底用「类型{n}」")
    distribution_table = _find_enum_table(directory, "distributionId")
    if distribution_table:
        rows = _load_table(directory, distribution_table)
        sample = next(iter(rows.values()), {}) if rows else {}
        print(f"distributionIds 的表名= {distribution_table} rows={len(rows)}")
        print(f"    样例行字段={sorted(sample)}")
        field = next((key for key in ("areaName", "name", "text") if key in sample), "")
        for key, row in list(rows.items())[:3]:
            print(f"    {key} → {translate(row.get(field)) if field else ''}")
        print(f"    → DISTRIBUTION_TABLE = {distribution_table!r}")
    else:
        print("distributionIds 的表名= 未落盘 → DISTRIBUTION_TABLE 留空字符串")
    print(f"EnemyTable rows={len(enemies)}")
    empty = sum(1 for row in enemies.values() if not row.get("attrTemplateId"))
    print(f"EnemyTable.attrTemplateId 为空的行数={empty}")
    distinct = {row.get("attrTemplateId") for row in enemies.values() if row.get("attrTemplateId")}
    print(f"EnemyTable 不同 attrTemplateId 个数={len(distinct)}")
    print(f"EnemyTagTable 形态= {_find_enum_table(directory, 'tagId') or '未落盘'}")


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unresolved_reason(keys: set[str], actions: list[Any]) -> str:
    """给「没有任何 action 能填满」的句子分类，附录据此判断破例是否误杀。

    - 命名空间：key 形如 <buffId>\\<key>，且该 buffId 是本次某个 action 的 buffId，
      数字其实就在同一条 action 里，只是字面 key 对不上。
    - 算式：key 不是裸标识符，也不是命名空间 key（如 1-value）。
    """
    buff_ids = {
        str((action.get("buffBBData") or {}).get("buffId") or "") for action in actions
    }
    namespaced = all(
        "\\" in key
        and key.split("\\", 1)[0] in buff_ids
        and IDENTIFIER_RE.match(key.split("\\", 1)[1])
        for key in keys
    )
    if namespaced:
        return "命名空间"
    expressions = [
        key for key in keys if not IDENTIFIER_RE.match(key) and "\\" not in key
    ]
    return "算式" if expressions else "缺 key"


def _find_enum_table(directory: Path, field: str, *exclude: str) -> str:
    """在已落盘的表里找含该字段的表；报告零请求，只能看本地有什么。"""
    for path in sorted(directory.glob("*.json")):
        if path.stem in {"I18nTextTable_CN", *exclude}:
            continue
        try:
            payload = json.loads(path.read_bytes())
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        sample = next(iter(payload.values()), None)
        if isinstance(sample, dict) and field in sample:
            return path.stem
    return ""


def _report_prop_effects(directory: Path, translate) -> None:
    rows = _load_table(directory, "UseItemTable")
    equip = _load_table(directory, "EquipItemTable")
    with_placeholders = 0
    no_single_action = 0
    union_only = 0
    conflicting = 0
    multi_action = 0
    reasons: Counter = Counter()
    samples: list[tuple[str, list[str], str]] = []
    for item_id, row in rows.items():
        keys = set(PLACEHOLDER_RE.findall(translate(row.get("itemUseDesc"))))
        if not keys:
            continue
        with_placeholders += 1
        actions = row.get("useActions") or []
        if len(actions) > 1:
            multi_action += 1
        per_action: list[dict[str, Any]] = []
        for action in actions:
            blackboard = ((action.get("buffBBData") or {}).get("blackboard")) or []
            values: dict[str, Any] = {}
            duplicate = False
            for entry in blackboard:
                key = str(entry.get("key"))
                value = entry.get("value")
                if value is None:
                    continue
                if key in values and values[key] != value:
                    duplicate = True
                values[key] = value
            per_action.append({} if duplicate else values)
        if not any(all(key in values for key in keys) for values in per_action):
            no_single_action += 1
            union: dict[str, Any] = {}
            for values in per_action:
                union.update(values)
            if all(key in union for key in keys):
                union_only += 1
            reason = _unresolved_reason(keys, actions)
            reasons[reason] += 1
            if len(samples) < 8:
                samples.append((item_id, sorted(keys), reason))
        seen: dict[str, Any] = {}
        clash = False
        for values in per_action:
            for key, value in values.items():
                if key in seen and seen[key] != value:
                    clash = True
                seen[key] = value
        if clash:
            conflicting += 1
    print()
    print("## 道具效果（UseItemTable.itemUseDesc 占位符口径）")
    print(f"UseItemTable rows={len(rows)}，带占位符的条数={with_placeholders}")
    print(f"没有任何一条 action 的 blackboard 能单独填满全部 key 的条数={no_single_action}")
    print(f"其中「单 action 不行、并集可以」的条数={union_only}")
    print(f"跨 action 同 key 异值的条数={conflicting}")
    print(f"多 action 的条数={multi_action}")
    for reason, count in sorted(reasons.items()):
        print(f"    落空原因 {reason}：{count} 条")
    for item_id, keys, reason in samples:
        print(f"    样例 [{reason}] {item_id} keys={keys}")
    print(f"EquipItemTable rows={len(equip)}（冷却 / 施放 / 装填只在这张表）")


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.report:
        return report_encyclopedia(args.output)
    return asyncio.run(download(args))


if __name__ == "__main__":
    raise SystemExit(main())
