"""Replay captured public tables; exercise every catalog row without a bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import types
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = types.ModuleType("endfield_ake_verify")
package.__path__ = [str(ROOT / "plugins/endfield")]
sys.modules[package.__name__] = package

from endfield_ake_verify.catalog.akedata import AkeCatalog  # noqa: E402
from endfield_ake_verify.providers.repository import AkeSnapshot  # noqa: E402


def make_fixture(tables: dict, target: Path):
    """Retain real selected rows + their text/skill/effect references, not all I18n."""
    char_ids = {
        "chr_0016_laevat",
        "chr_0034_typhoea",
        "chr_0002_endminm",
        "chr_0003_endminf",
        "chr_9000_endmin",
    }
    weapons = {"wpn_sword_0006", "wpn_sword_0003", "wpn_funnel_0019"}
    gear = {
        "item_equip_t4_suit_usp02_body_03",
        "item_equip_t4_suit_usp02_hand_03",
        "item_equip_t4_suit_combo_cd01_hand_01",
    }
    gear.update(
        key
        for key, row in tables["EquipTable"].items()
        if any(v.get("modifyAttributeType") for v in row["equipAttrModifiers"])
    )
    selected = {
        name: {key: value for key, value in tables[name].items() if key in char_ids}
        for name in (
            "CharacterTable",
            "CharGrowthTable",
            "CharacterPotentialTable",
            "CharWpnRecommendTable",
        )
    }
    skills = {
        sid
        for row in selected["CharGrowthTable"].values()
        for group in row["skillGroupMap"].values()
        for sid in group["skillIdList"]
    }
    effects = {
        node.get("passiveSkillNodeInfo", {}).get("talentEffectId")
        for row in selected["CharGrowthTable"].values()
        for node in row["talentNodeMap"].values()
    }
    effects.update(
        v["potentialEffectId"]
        for row in selected["CharacterPotentialTable"].values()
        for v in row["potentialUnlockBundle"]
    )
    selected["WeaponBasicTable"] = {
        k: v for k, v in tables["WeaponBasicTable"].items() if k in weapons
    }
    skills.update(
        sid
        for row in selected["WeaponBasicTable"].values()
        for sid in row["weaponSkillList"]
    )
    selected["EquipTable"] = {
        k: v for k, v in tables["EquipTable"].items() if k in gear
    }
    selected["EquipFormulaTable"] = {
        k: v
        for k, v in tables["EquipFormulaTable"].items()
        if v.get("outcomeEquipId") in gear
    }
    suits = {v["suitID"] for v in selected["EquipTable"].values()}
    selected["EquipSuitTable"] = {
        k: v for k, v in tables["EquipSuitTable"].items() if k in suits
    }
    skills.update(
        v["skillID"] for row in selected["EquipSuitTable"].values() for v in row["list"]
    )
    selected["SkillPatchTable"] = {
        k: v for k, v in tables["SkillPatchTable"].items() if k in skills
    }
    selected["PotentialTalentEffectTable"] = {
        k: v for k, v in tables["PotentialTalentEffectTable"].items() if k in effects
    }
    selected["ItemTable"] = {
        k: v for k, v in tables["ItemTable"].items() if k in char_ids | weapons | gear
    }
    for name in (
        "CharProfessionTable",
        "CharTypeTable",
        "CharBattleTagTable",
        "TagDataTable",
        "RichTextStyleTable",
        "HyperlinkTextTable",
        "AttributeShowConfigTable",
        "AttributeFilterTable",
        "SystemJumpTable",
        "WeaponTalentTemplateTable",
    ):
        selected[name] = tables[name]
    for name, field in (
        ("WeaponUpgradeTemplateTable", "levelTemplateId"),
        ("WeaponBreakThroughTemplateTable", "breakthroughTemplateId"),
    ):
        keys = {v[field] for v in selected["WeaponBasicTable"].values()}
        selected[name] = {k: v for k, v in tables[name].items() if k in keys}
    text_ids = set()

    def collect(value):
        if isinstance(value, dict):
            if "text" in value and "id" in value:
                text_ids.add(str(value["id"]))
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(selected)
    selected["I18nTextTable_CN"] = {
        k: v for k, v in tables["I18nTextTable_CN"].items() if k in text_ids
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(selected, ensure_ascii=False, separators=(",", ":")), encoding="utf8"
    )
    print(f"fixture bytes={target.stat().st_size}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    version, build = args.directory.name.split("@", 1)
    data = AkeSnapshot(args.directory.name, f"public/{version}/{build}/TableCfg")
    data._tables = {
        path.stem: json.loads(path.read_bytes())
        for path in args.directory.glob("*.json")
    }
    if args.fixture:
        make_fixture(data._tables, args.fixture)
    catalog = AkeCatalog(data)
    errors, counts, fallbacks = [], {}, []
    for kind, table_name in (
        ("operator", "CharGrowthTable"),
        ("weapon", "WeaponBasicTable"),
        ("equipment", "EquipTable"),
    ):
        counts[kind] = 0
        for key in data._tables[table_name]:
            if key not in data._tables["ItemTable"]:
                continue
            try:
                view = await getattr(catalog, kind + "_view")(key)
                if not view.name:
                    raise ValueError("Empty name")
                if kind == "operator":
                    for skill in view.skills:
                        fields = [skill.description]
                        fields.extend(
                            value
                            for level in skill.levels
                            for value in level.values.values()
                        )
                        fields.extend(
                            level.description
                            for forms in skill.extra_levels.values()
                            for level in forms
                        )
                        if any(
                            "--" in str(value) or re.search(r"\{[^{}]+\}", str(value))
                            for value in fields
                        ):
                            raise ValueError(f"Unresolved skill field: {skill.name}")
                counts[kind] += 1
            except Exception as exc:
                target = (
                    fallbacks
                    if key in {"chr_0002_endminm", "chr_0003_endminf"}
                    and str(exc) == "AKE talent/potential parameters unavailable"
                    else errors
                )
                target.append({"kind": kind, "key": key, "error": str(exc)})
    loadouts = []
    for name in ("莱万汀", "提弗洛斯", "弭弗"):
        try:
            weapon = await catalog.recommended_weapon(name)
            view = await catalog.loadout(name, weapon, [("长息轻护甲", 3, ())])
            loadouts.append(asdict(view))
        except Exception as exc:
            errors.append({"kind": "loadout", "key": name, "error": str(exc)})
    report = {
        "version": data.version,
        "mode": "offline public replay (not live latency)",
        "counts": counts,
        "errors": errors,
        "known_incomplete_requires_whole_view_fallback": fallbacks,
        "loadouts": loadouts,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
        )
    print(
        json.dumps(
            {"counts": counts, "errors": errors, "fallbacks": fallbacks},
            ensure_ascii=False,
            indent=2,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
