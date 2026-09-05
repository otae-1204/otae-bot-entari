"""AKE TableCfg adapters to the existing card and calculation schemas.

The article-shaped objects below are internal compatibility DTOs, not FZ
responses. Keeping this boundary lets the established layout, filtering and
loadout arithmetic stay source independent without duplicating them.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from ..providers.assets import item_icon_urls, operator_icon_urls
from ..providers.repository import AkeDataIncomplete, AkeSnapshot, localize
from .models import LEVEL_COLUMNS, SkillLevelView, SkillView
from .views.common import (
    _format_fz_template,
    _format_plain_number,
    _build_fz_term_styles,
    clean_text,
)
from .views.constants import LOADOUT_ATTRIBUTE_NAMES, SKILL_CATEGORY_ORDER
from .views.operators import (
    build_operator_view,
    build_fz_operator_catalog_view,
    _effect_values,
    _skill_group_category,
)
from .views.weapons import (
    build_warfarin_weapon_view,
    build_fz_weapon_catalog_view,
    _blackboard_values,
    _warfarin_rich_text_styles,
)
from .views.equipment import (
    build_fz_equipment_view,
    build_fz_equipment_catalog_view,
    build_fz_equipment_attribute_catalog_view,
    _apply_fz_equipment_catalog_item_details,
    _apply_fz_equipment_catalog_suit_effects,
)
from .views.loadout import build_fz_loadout_view


WEAPON_TYPES = {1: "单手剑", 2: "施术单元", 3: "双手剑", 5: "长柄武器", 6: "手铳"}
ATTRIBUTES = {
    0: "Level",
    1: "MaxHp",
    2: "Atk",
    3: "Def",
    4: "PhysicalDamageTakenScalar",
    5: "FireDamageTakenScalar",
    6: "PulseDamageTakenScalar",
    7: "CrystDamageTakenScalar",
    9: "CriticalRate",
    10: "CriticalDamageIncrease",
    17: "NormalAttackDamageIncrease",
    26: "PoiseDamageOutputScalar",
    28: "UltimateSkillDamageIncrease",
    29: "HealOutputIncrease",
    30: "HealTakenIncrease",
    32: "NormalSkillDamageIncrease",
    33: "ComboSkillDamageIncrease",
    39: "Str",
    40: "Agi",
    41: "Wisd",
    42: "Will",
    44: "UltimateSpGainScalar",
    47: "ComboSkillCooldownScalar",
    48: "NaturalDamageTakenScalar",
    50: "PhysicalDamageIncrease",
    51: "FireDamageIncrease",
    52: "PulseDamageIncrease",
    53: "CrystDamageIncrease",
    54: "NaturalDamageIncrease",
    55: "EtherDamageIncrease",
    60: "EtherDamageTakenScalar",
    61: "PoiseBrokenDamageIncrease",
    87: "PhysicalAndSpellInflictionEnhance",
}
MODIFIERS = {
    5: "BaseAddition",
    6: "BaseMultiplier",
    7: "BaseFinalAddition",
    8: "BaseFinalMultiplier",
}
PARTS = {0: ("Body", "护甲"), 1: ("Hand", "护手"), 2: ("EDC", "配件")}


def article(title: str, attrs: dict, revision: str) -> dict:
    return {
        "article": {"title": title, "updatedAt": revision},
        "revision": {"contentJson": {"content": [{"attrs": attrs}]}},
    }


def term_styles(refs: dict):
    return _build_fz_term_styles(
        {
            "RICH_TEXT_STYLES": _warfarin_rich_text_styles(
                refs.get("richTextStyleTable", {})
            ),
            "HYPERLINK_TEXTS": refs.get("hyperlinkTextTable", {}),
        }
    )


def effect_values(effect: dict) -> dict:
    values = _effect_values(effect)
    # The legacy Warfarin enum calls 41 Int. AKE uses Wisd; never alias it to Will.
    values.pop("Will", None)
    for entry in effect.get("dataList", []):
        modifier = entry.get("attrModifier") or {}
        key = ATTRIBUTES.get(modifier.get("attrType"))
        if key and modifier.get("attrValue") is not None:
            values[key] = modifier["attrValue"]
    return values


def effect_description(effect: dict) -> str:
    rendered = _format_fz_template(effect.get("desc", ""), effect_values(effect))
    if "--" in rendered or re.search(r"\{[^{}]+\}", rendered):
        # Some legacy Endministrator effects reference positional values not
        # included in TableCfg (the shared SkillData defaults are zero, too).
        # A whole-view fallback is safer than publishing invented/empty values.
        raise AkeDataIncomplete("AKE talent/potential parameters unavailable")
    return rendered


def _record(bundle: dict, level: int) -> dict:
    return next(
        (
            row
            for row in bundle.get("SkillPatchDataBundle", [])
            if row.get("level") == level
        ),
        {},
    )


def operator_skills(growth: dict, patches: dict) -> list[SkillView]:
    result = []
    for group in growth.get("skillGroupMap", {}).values():
        category = _skill_group_category(group.get("skillGroupType"))
        ids = group.get("skillIdList") or []
        if category not in SKILL_CATEGORY_ORDER or not ids:
            continue
        if any(not patches.get(key, {}).get("SkillPatchDataBundle") for key in ids):
            raise AkeDataIncomplete(f"AKE skill group is incomplete: {ids[0]}")
        levels, extra = [], {}
        best_values = {}
        for key in ids:
            best_values.update(
                _blackboard_values(_record(patches[key], 12).get("blackboard", []))
            )
            extra[key] = []
        for number, label in LEVEL_COLUMNS:
            merged = {}
            first = {}
            for key in ids:
                row = _record(patches[key], number)
                if not row:
                    raise AkeDataIncomplete(
                        f"AKE skill level missing: {key} Lv{number}"
                    )
                first = first or row
                values = _blackboard_values(row.get("blackboard", []))
                metrics = {}
                for metric in row.get("subDescDataList", []):
                    name = clean_text(metric.get("name"))
                    if name:
                        metrics[name] = _format_fz_template(
                            metric.get("desc", ""), values
                        )
                        if "倍率" in name and re.fullmatch(
                            r"\d+(?:\.\d+)?", metrics[name]
                        ):
                            metrics[name] = (
                                _format_plain_number(float(metrics[name]) * 100) + "%"
                            )
                # Preserve legacy named lists when testing older table revisions.
                metrics.update(
                    zip(row.get("subDescNameList", []), row.get("subDescList", []))
                )
                for name, value in metrics.items():
                    if name in merged and merged[name] != value:
                        name = f"{clean_text(row.get('skillName')) or key.rsplit('_', 1)[-1]} · {name}"
                    merged[name] = value
                extra[key].append(
                    SkillLevelView(
                        label,
                        number,
                        metrics,
                        _format_plain_number(row.get("coolDown")),
                        _format_plain_number(row.get("costValue")),
                        _format_plain_number(row.get("maxChargeTime")),
                        _format_fz_template(row.get("description", ""), values),
                    )
                )
            primary_values = _blackboard_values(first.get("blackboard", []))
            if category == "普攻":
                finish = next(
                    (sid for sid in ids if re.search(r"attack_?5$", sid)), ids[-1]
                )
                values = _blackboard_values(
                    _record(patches[finish], number).get("blackboard", [])
                )
                poise = values.get("display_poise", values.get("poise"))
                if poise is not None:
                    merged = {"失衡值": _format_plain_number(poise), **merged}
            elif category == "战技" and first.get("costValue"):
                merged = {
                    "技力消耗": _format_plain_number(first["costValue"]),
                    **merged,
                }
            elif category == "连携技":
                scalar = primary_values.get(
                    "display_atk_scale", primary_values.get("atk_scale")
                )
                if scalar is not None:
                    merged.setdefault(
                        "攻击倍率", _format_plain_number(float(scalar) * 100) + "%"
                    )
                merged["冷却"] = _format_plain_number(first.get("coolDown")) + "s"
            levels.append(
                SkillLevelView(
                    label,
                    number,
                    merged,
                    _format_plain_number(first.get("coolDown")) + "s",
                    _format_plain_number(first.get("costValue")),
                    _format_plain_number(first.get("maxChargeTime")),
                )
            )
        forms = []
        for index in (1, 2):
            name = clean_text(group.get(f"conditionName{index}"))
            desc = group.get(f"conditionPostDesc{index}") or group.get(
                f"conditionDesc{index}"
            )
            if name and desc:
                forms.append((name, _format_fz_template(desc, best_values)))
        result.append(
            SkillView(
                ids[0],
                clean_text(group.get("name")) or category,
                str(group.get("icon") or ""),
                category,
                _format_fz_template(
                    group.get("desc")
                    or _record(patches[ids[0]], 12).get("description", ""),
                    best_values,
                ),
                forms,
                levels,
                extra,
                preserve_metric_rows=True,
            )
        )
    if len(result) != 4:
        raise AkeDataIncomplete(
            f"AKE operator has {len(result)} skill groups, expected 4"
        )
    return sorted(
        result,
        key=lambda item: {"普攻": 0, "战技": 1, "连携技": 2, "终结技": 3}[
            item.category
        ],
    )


class AkeCatalog:
    def __init__(self, data: AkeSnapshot):
        self.data = data

    async def term_styles(self):
        return term_styles(
            {
                "richTextStyleTable": await self.data.localized("RichTextStyleTable"),
                "hyperlinkTextTable": await self.data.localized("HyperlinkTextTable"),
            }
        )

    async def resolve(self, kind: str, query: str) -> str:
        table_name = {
            "operator": "CharGrowthTable",
            "weapon": "WeaponBasicTable",
            "equipment": "EquipTable",
        }[kind]
        table, items, texts = await self.data.tables(
            table_name, "ItemTable", "I18nTextTable_CN"
        )
        query = query.split("/", 1)[-1].strip()
        if query in table and query in items:
            return query
        for key, row in table.items():
            if key not in items:
                continue
            name = localize(items[key].get("name"), texts)
            if query.casefold() in {
                str(name).casefold(),
                str(row.get("engName") or "").casefold(),
            }:
                return key
        raise AkeDataIncomplete(f"AKE {kind} not found: {query}")

    async def operator_catalog(self, element="", profession=""):
        growth, items, texts, professions, types = await self.data.tables(
            "CharGrowthTable",
            "ItemTable",
            "I18nTextTable_CN",
            "CharProfessionTable",
            "CharTypeTable",
        )
        entries, names = [], set()
        for key, raw in growth.items():
            if key not in items:
                continue
            # Catalog uses lightweight basic fields, not CharacterTable or full skill localization.
            row = {
                k: localize(v, texts)
                for k, v in raw.items()
                if k
                not in {
                    "skillGroupMap",
                    "talentNodeMap",
                    "skillLevelUp",
                    "charBreakCostMap",
                }
            }
            name = row.get("name")
            if not name or name in names:
                continue
            names.add(name)
            kind = localize(types.get(row.get("charTypeId"), {}), texts)
            job = localize(professions.get(str(row.get("profession")), {}), texts)
            entries.append(
                dict(
                    name=name,
                    title=f"干员/{name}",
                    charId=key,
                    nameEn=row.get("engName"),
                    rarity=row.get("rarity"),
                    element=kind.get("name"),
                    elementColor="#" + str(kind.get("color") or "888888").lstrip("#"),
                    profession=job.get("name"),
                    weaponType=WEAPON_TYPES.get(row.get("weaponType")),
                    iconUrl=(operator_icon_urls(key) or ("",))[0],
                )
            )
        view = build_fz_operator_catalog_view(
            article("干员", {"roster": {"entries": entries}}, self.data.revision),
            element,
            profession,
        )
        view.source_version = self.data.version
        view.source_name = "AKEData"
        return view

    async def operator_raw(self, query: str) -> dict:
        key = await self.resolve("operator", query)
        names = (
            "CharacterTable",
            "CharGrowthTable",
            "ItemTable",
            "CharacterPotentialTable",
            "SkillPatchTable",
            "PotentialTalentEffectTable",
            "CharProfessionTable",
            "CharTypeTable",
            "CharBattleTagTable",
            "RichTextStyleTable",
            "HyperlinkTextTable",
            "I18nTextTable_CN",
        )
        tables = dict(zip(names, await self.data.tables(*names)))
        texts = tables["I18nTextTable_CN"]
        growth = localize(tables["CharGrowthTable"][key], texts)
        potential = localize(tables["CharacterPotentialTable"].get(key, {}), texts)
        effect_ids = {
            node.get("passiveSkillNodeInfo", {}).get("talentEffectId")
            for node in growth.get("talentNodeMap", {}).values()
        }
        effect_ids.update(
            p.get("potentialEffectId")
            for p in potential.get("potentialUnlockBundle", [])
        )
        skill_ids = {
            sid
            for group in growth.get("skillGroupMap", {}).values()
            for sid in group.get("skillIdList", [])
        }
        if not potential or any(
            eid and eid not in tables["PotentialTalentEffectTable"]
            for eid in effect_ids
        ):
            raise AkeDataIncomplete("AKE operator talents/potentials incomplete")
        return {
            "meta": {
                "id": key,
                "name": growth["name"],
                "slug": key,
                "version": self.data.version,
            },
            "data": {
                "characterTable": localize(tables["CharacterTable"][key], texts),
                "charGrowthTable": growth,
                "itemTable": localize(tables["ItemTable"][key], texts),
                "characterPotentialTable": potential,
                "skillPatchTable": localize(
                    {sid: tables["SkillPatchTable"].get(sid, {}) for sid in skill_ids},
                    texts,
                ),
                "potentialTalentEffectTable": localize(
                    {
                        eid: tables["PotentialTalentEffectTable"][eid]
                        for eid in effect_ids
                        if eid
                    },
                    texts,
                ),
            },
            "refs": {
                name[0].lower() + name[1:]: localize(tables[name], texts)
                for name in names[6:-1]
            },
        }

    async def operator_view(self, query: str):
        raw = await self.operator_raw(query)
        view = build_operator_view(raw)
        view.source_name = "AKEData"
        view.term_styles = term_styles(raw["refs"])
        data = raw["data"]
        tag_names = raw["refs"].get("charBattleTagTable", {})
        view.tags = [
            tag_names[key]
            for key in data["characterTable"].get("charBattleTagIds", [])
            if tag_names.get(key)
        ]
        view.weapon_type = WEAPON_TYPES[data["characterTable"]["weaponType"]]
        view.skills = operator_skills(data["charGrowthTable"], data["skillPatchTable"])
        effects = data["potentialTalentEffectTable"]
        for effect in (*view.talents, *view.potentials):
            native = effects[effect.effect_id]
            effect.description = effect_description(native)
        return view

    async def weapon_raw(self, query: str) -> dict:
        key = await self.resolve("weapon", query)
        basic = await self.data.localized("WeaponBasicTable", key)
        patches, texts = await self.data.tables("SkillPatchTable", "I18nTextTable_CN")
        skills = basic.get("weaponSkillList", [])
        if not skills or any(sid not in patches for sid in skills):
            raise AkeDataIncomplete("AKE weapon skills incomplete")
        return {
            "meta": {"id": key, "version": self.data.version},
            "data": {
                "weaponBasicTable": basic,
                "itemTable": await self.data.localized("ItemTable", key),
                "weaponUpgradeTemplateTable": await self.data.localized(
                    "WeaponUpgradeTemplateTable", basic["levelTemplateId"]
                ),
                "skillPatchTable": localize(
                    {sid: patches[sid] for sid in skills}, texts
                ),
            },
            "refs": {
                "weaponTypes": {str(k): v for k, v in WEAPON_TYPES.items()},
                "richTextStyleTable": await self.data.localized("RichTextStyleTable"),
                "hyperlinkTextTable": await self.data.localized("HyperlinkTextTable"),
            },
        }

    async def weapon_view(self, query: str):
        raw = await self.weapon_raw(query)
        view = build_warfarin_weapon_view(raw)
        view.source_name, view.title, view.slug = (
            "AKEData",
            f"武器/{view.name}",
            view.weapon_id,
        )
        growth, relations, texts = await self.data.tables(
            "CharGrowthTable", "CharWpnRecommendTable", "I18nTextTable_CN"
        )
        view.operator_names = list(
            dict.fromkeys(
                localize(growth[key]["name"], texts)
                for key, row in relations.items()
                if key in growth
                and view.weapon_id
                in [
                    wid
                    for group in ("weaponIds1", "weaponIds2", "weaponIds3")
                    for wid in row.get(group, [])
                ]
            )
        )
        return view

    async def weapon_catalog(self, weapon_type=""):
        basic, items, upgrades, patches, texts = await self.data.tables(
            "WeaponBasicTable",
            "ItemTable",
            "WeaponUpgradeTemplateTable",
            "SkillPatchTable",
            "I18nTextTable_CN",
        )
        entries = []
        for key, row in basic.items():
            if key not in items or not row.get("weaponSkillList"):
                continue
            name = localize(items[key]["name"], texts)
            curve = upgrades.get(row.get("levelTemplateId"), {}).get("list", [])
            terms = [
                localize(
                    (patches.get(sid, {}).get("SkillPatchDataBundle") or [{}])[0].get(
                        "skillName"
                    ),
                    texts,
                )
                for sid in row["weaponSkillList"]
            ]
            entries.append(
                dict(
                    name=name,
                    title=f"武器/{name}",
                    weaponId=key,
                    nameEn=row.get("engName"),
                    rarity=row.get("rarity"),
                    weaponType=WEAPON_TYPES.get(row.get("weaponType")),
                    maxLv=row.get("maxLv"),
                    maxAtk=next(
                        (
                            r["baseAtk"]
                            for r in curve
                            if r["weaponLv"] == row.get("maxLv")
                        ),
                        0,
                    ),
                    iconUrl=(item_icon_urls(items[key].get("iconId") or key) or ("",))[
                        0
                    ],
                    termsMain=terms[:1],
                    termsSub=terms[1:2],
                    termsSkill=terms[2:],
                )
            )
        view = build_fz_weapon_catalog_view(
            article("武器", {"roster": {"entries": entries}}, self.data.revision),
            weapon_type,
        )
        view.source_version = self.data.version
        view.source_name = "AKEData"
        return view

    async def recommended_weapon(self, query: str) -> str:
        key = await self.resolve("operator", query)
        growth, relations, weapons, items, texts = await self.data.tables(
            "CharGrowthTable",
            "CharWpnRecommendTable",
            "WeaponBasicTable",
            "ItemTable",
            "I18nTextTable_CN",
        )
        row = relations.get(key, {})
        ids = [
            wid
            for group in ("weaponIds1", "weaponIds2", "weaponIds3")
            for wid in row.get(group, [])
        ]
        for wid in ids:
            if wid in items and weapons.get(wid, {}).get("weaponType") == growth[
                key
            ].get("weaponType"):
                return "武器/" + localize(items[wid]["name"], texts)
        raise AkeDataIncomplete("AKE has no compatible recommended weapon")

    async def equipment_raw(self, query: str) -> dict:
        key = await self.resolve("equipment", query)
        names = (
            "EquipTable",
            "EquipSuitTable",
            "ItemTable",
            "SkillPatchTable",
            "AttributeShowConfigTable",
            "AttributeFilterTable",
            "SystemJumpTable",
            "EquipFormulaTable",
            "I18nTextTable_CN",
        )
        tables = dict(zip(names, await self.data.tables(*names)))
        texts = tables["I18nTextTable_CN"]
        row = tables["EquipTable"][key]
        item = localize(tables["ItemTable"][key], texts)
        part, slot = PARTS[row["partType"]]
        stats = []
        filters = [
            value
            for group in tables["AttributeFilterTable"].values()
            for value in group.get("list", [])
        ]
        displays = [(row.get("displayBaseAttrModifier", {}), False)] + [
            (value, True) for value in row.get("displayAttrModifiers", [])
        ]
        for modifier, enhances in displays:
            numeric = modifier.get("attrType", 0)
            composite = str(modifier.get("compositeAttr") or "")
            attr = composite or ATTRIBUTES.get(numeric)
            if not attr or modifier.get("modifierType") not in MODIFIERS:
                raise AkeDataIncomplete(
                    f"AKE unsupported equipment attribute: {numeric}"
                )
            shows = (
                tables["AttributeShowConfigTable"].get(str(numeric), {}).get("list", [])
            )
            show = next(
                (
                    v
                    for v in shows
                    if v["attributeModifier"] == modifier["modifierType"]
                ),
                {},
            )
            label = (
                next(
                    (
                        localize(v["name"], texts)
                        for v in filters
                        if composite
                        and v.get("compositeAttr") == composite
                        and v.get("attributeModifier") == modifier["modifierType"]
                    ),
                    "",
                )
                or localize(show.get("name"), texts)
                or LOADOUT_ATTRIBUTE_NAMES.get(attr, attr)
            )
            # One forgeable slot may expand to several combat attributes. Use
            # attrIndex to keep its exact values but do not invent extra slots.
            matching = [
                value
                for value in row.get("equipAttrModifiers", [])
                if value.get("attrIndex", 0)
                == modifier.get("enhancedAttrIndex", modifier.get("attrIndex", 0))
            ]
            values = matching[0].get("attrValues", []) if matching else []
            if not values or any(
                value.get("attrValues") != values for value in matching
            ):
                raise AkeDataIncomplete(
                    "AKE composite equipment values are inconsistent"
                )
            stats.append(
                dict(
                    label=label,
                    attrType=ATTRIBUTES.get(numeric, "Level"),
                    compositeAttr=composite,
                    modifierType=MODIFIERS[modifier["modifierType"]],
                    isPercent=bool(show.get("showPercent"))
                    or modifier["modifierType"] == 6
                    or bool(composite and composite not in {"Main", "Sub"}),
                    valueFormat=show.get("valueFormat", ""),
                    values=values,
                    enhances=enhances,
                )
            )
        suit = tables["EquipSuitTable"].get(row.get("suitID"), {})
        bonus = (suit.get("list") or [{}])[0]
        suit_name = localize(bonus.get("suitName"), texts) or "独立装备"
        skill = localize(
            _record(
                tables["SkillPatchTable"].get(bonus.get("skillID"), {}),
                bonus.get("skillLv", 1),
            ),
            texts,
        )
        if suit.get("list") and not skill:
            raise AkeDataIncomplete("AKE equipment suit skill missing")
        pieces = []
        for eid in suit.get("equipList", []):
            equip, gear_item = (
                tables["EquipTable"].get(eid),
                tables["ItemTable"].get(eid),
            )
            if equip and gear_item:
                pieces.append(
                    dict(
                        equipId=eid,
                        name=localize(gear_item["name"], texts),
                        slotType=PARTS[equip["partType"]][1],
                        iconUrl=item_icon_urls(gear_item.get("iconId") or eid)[0],
                    )
                )
        acquisition = "、".join(
            filter(
                None,
                (
                    localize(tables["SystemJumpTable"].get(jid, {}).get("desc"), texts)
                    for jid in item.get("obtainWayIds", [])
                ),
            )
        )
        formula = next(
            (
                value
                for value in tables["EquipFormulaTable"].values()
                if value.get("outcomeEquipId") == key
            ),
            None,
        )
        if formula:
            # AKE's v3-table-data.js defines 1=permission, 2=map reward,
            # 3=regional channel and 4=shop. Keep the existing card labels.
            acquisition = {
                0: "默认解锁",
                1: f"权限等级 {formula.get('unlockValue', 0)} 解锁",
                2: "EquipFormulaChest",
                3: "DomainShop",
                4: "商店兑换",
            }.get(formula.get("unlockType"), acquisition)
        return article(
            f"装备/{item['name']}",
            {
                "hero": dict(
                    id=key,
                    name=item["name"],
                    rarity=item["rarity"],
                    level=row.get("minWearLv"),
                    partType=part,
                    slotType=slot,
                    description=item.get("desc"),
                    flavor=item.get("decoDesc"),
                    iconUrl=item_icon_urls(item.get("iconId") or key)[0],
                ),
                "stats": {"rows": stats},
                "suit": dict(
                    selfEquipId=key,
                    name=suit_name,
                    groupName=suit_name,
                    equipCnt=bonus.get("equipCnt", 0),
                    pieces=pieces,
                    bonus={
                        "description": skill.get("description", ""),
                        "levels": [
                            {"values": _blackboard_values(skill.get("blackboard", []))}
                        ],
                    },
                ),
                "materials": {"unlockType": acquisition},
            },
            self.data.revision,
        )

    async def equipment_view(self, query: str):
        view = build_fz_equipment_view(await self.equipment_raw(query))
        view.term_styles = await self.term_styles()
        view.source_version = self.data.version
        view.source_name = "AKEData"
        return view

    async def equipment_catalog(
        self, group_name="", rarity_filter="gold", *, include_details=True, filters=()
    ):
        table, items, suits, texts = await self.data.tables(
            "EquipTable", "ItemTable", "EquipSuitTable", "I18nTextTable_CN"
        )
        entries = []
        for key, row in table.items():
            if key not in items:
                continue
            item = items[key]
            suit = (suits.get(row.get("suitID"), {}).get("list") or [{}])[0]
            name = localize(item["name"], texts)
            entries.append(
                dict(
                    name=name,
                    title=f"装备/{name}",
                    equipId=key,
                    rarity=item["rarity"],
                    level=row.get("minWearLv"),
                    slotType=PARTS[row["partType"]][1],
                    group=localize(suit.get("suitName"), texts) or "独立装备套组",
                    iconUrl=item_icon_urls(item.get("iconId") or key)[0],
                )
            )
        raw = article("装备", {"roster": {"entries": entries}}, self.data.revision)
        view = build_fz_equipment_catalog_view(raw, group_name, rarity_filter)
        if include_details or filters:
            keys = [item.equipment_id for group in view.groups for item in group.items]
            # Preload once before row adaptation; no N article requests.
            await self.data.tables(
                "SkillPatchTable", "AttributeShowConfigTable", "SystemJumpTable"
            )
            details = {
                entry["article"]["title"]: entry
                for entry in [await self.equipment_raw(key) for key in keys]
            }
            if filters:
                view = build_fz_equipment_attribute_catalog_view(
                    raw, details, filters, rarity_filter
                )
            else:
                _apply_fz_equipment_catalog_item_details(view, details)
            _apply_fz_equipment_catalog_suit_effects(view, list(details.values()))
        view.source_version = self.data.version
        view.source_name = "AKEData"
        return view

    async def loadout(self, operator: str, weapon: str, equipment: list, **options):
        operator_raw, weapon_raw = await asyncio.gather(
            self.operator_raw(operator), self.weapon_raw(weapon)
        )
        data = operator_raw["data"]
        char, growth = data["characterTable"], data["charGrowthTable"]
        stages = defaultdict(list)
        for row in char.get("attributes", []):
            values = {
                ATTRIBUTES.get(a["attrType"], f"attr_{a['attrType']}"): (
                    round(a["attrValue"])
                    if abs(a["attrValue"] - round(a["attrValue"])) < 1e-8
                    else a["attrValue"]
                )
                for a in row.get("Attribute", {}).get("attrs", [])
            }
            # Compatibility with the pre-migration input schema: four ability
            # values were displayed (and fed into the calculator) to 1 decimal.
            for ability in ("Str", "Agi", "Wisd", "Will"):
                if ability in values:
                    values[ability] = float(f"{values[ability]:.1f}")
            # Existing calculator base conventions for omitted sparse fields.
            # These are application defaults, not a second numeric HTTP source.
            values.setdefault("CriticalDamageIncrease", 0.5)
            values.setdefault("UltimateSpGainScalar", 1.0)
            stages[row.get("breakStage", 0)].append(values)
        keys = {key for rows in stages.values() for row in rows for key in row}
        attrs = {
            "hero": dict(
                id=char["charId"],
                name=char["name"],
                weaponType=WEAPON_TYPES[char["weaponType"]],
                meta=[
                    {
                        "label": "主 / 副属性",
                        "value": " / ".join(
                            LOADOUT_ATTRIBUTE_NAMES[ATTRIBUTES[char[k]]]
                            for k in ("mainAttrType", "subAttrType")
                        ),
                    }
                ],
            ),
            "attributes": {
                "breaks": [
                    {"breakStage": stage, "levels": [v["Level"] for v in rows]}
                    for stage, rows in stages.items()
                ],
                "rows": [
                    {
                        "key": key,
                        "cells": [
                            [v.get(key, 0) for v in rows] for rows in stages.values()
                        ],
                    }
                    for key in keys
                ],
            },
            "talents": [],
            "potentials": [],
        }
        effects = data["potentialTalentEffectTable"]
        for node in sorted(
            growth.get("talentNodeMap", {}).values(),
            key=lambda v: v.get("passiveSkillNodeInfo", {}).get("level", 0),
        ):
            info = node.get("passiveSkillNodeInfo", {})
            effect = effects.get(info.get("talentEffectId"))
            if effect:
                effect_description(effect)
                attrs["talents"].append(
                    dict(
                        name=info.get("name"),
                        description=effect["desc"],
                        values=effect_values(effect),
                    )
                )
        for potential in data["characterPotentialTable"].get(
            "potentialUnlockBundle", []
        ):
            effect = effects[potential["potentialEffectId"]]
            effect_description(effect)
            attrs["potentials"].append(
                dict(
                    name=potential["name"],
                    level=potential["level"],
                    description=effect["desc"],
                    values=effect_values(effect),
                )
            )
        weapon_data = weapon_raw["data"]
        basic = weapon_data["weaponBasicTable"]
        breaks = await self.data.localized(
            "WeaponBreakThroughTemplateTable", basic["breakthroughTemplateId"]
        )
        rows = breaks.get("list", [])
        # Preserve the existing simulator's independent weapon-level and
        # skill-level controls; its default skill bounds are max-break bounds.
        selected = rows[-1] if rows else {}
        bounds = selected.get("skillLevelBounds", [])
        skills = []
        for index, sid in enumerate(basic["weaponSkillList"]):
            bundle = weapon_data["skillPatchTable"][sid]["SkillPatchDataBundle"]
            if index >= len(bounds):
                raise AkeDataIncomplete("AKE weapon skill level bounds missing")
            skills.append(
                dict(
                    id=sid,
                    name=bundle[0]["skillName"],
                    description=bundle[0]["description"],
                    zeroPotentialMaxLevel=bounds[index]["upperBound"],
                    levels=[
                        {
                            "level": row["level"],
                            "values": _blackboard_values(row.get("blackboard", [])),
                        }
                        for row in bundle
                    ],
                )
            )
        weapon_attrs = {
            "hero": dict(
                id=basic["weaponId"],
                name=weapon_data["itemTable"]["name"],
                weaponType=WEAPON_TYPES[basic["weaponType"]],
            ),
            "stats": {
                "curve": [
                    {"lv": row["weaponLv"], "atk": row["baseAtk"]}
                    for row in weapon_data["weaponUpgradeTemplateTable"]["list"]
                ]
            },
            "skills": {"skills": skills},
        }
        gears = [
            (await self.equipment_raw(query), enhance, forges)
            for query, enhance, forges in equipment
        ]
        view = build_fz_loadout_view(
            article(operator, attrs, self.data.revision),
            article(weapon, weapon_attrs, self.data.revision),
            gears,
            operator_growth=operator_raw,
            **options,
        )
        view.term_styles = await self.term_styles()
        view.source_version = self.data.version
        view.source_name = "AKEData"
        return view
