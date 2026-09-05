"""Record public AKE schemas for migration checks; never accepts credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/endfield-ake-migration/public")
    )
    parser.add_argument(
        "--tables",
        default="CharacterTable,CharGrowthTable,CharacterPotentialTable,CharWpnRecommendTable,CharProfessionTable,CharTypeTable,CharBattleTagTable,TagDataTable,ItemTable,I18nTextTable_CN,SkillPatchTable,PotentialTalentEffectTable,WeaponBasicTable,WeaponUpgradeTemplateTable,WeaponBreakThroughTemplateTable,WeaponTalentTemplateTable,EquipTable,EquipSuitTable,AttributeShowConfigTable,AttributeFilterTable,SystemJumpTable,EquipFormulaTable,RichTextStyleTable,HyperlinkTextTable,GachaCharPoolTable,GachaWeaponPoolTable,ActivityTable,TimeRangeTable",
    )
    args = parser.parse_args()
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


if __name__ == "__main__":
    asyncio.run(main())
