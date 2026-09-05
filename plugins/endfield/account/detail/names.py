from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..i18n import localized_text
from ...providers.akedata import _get, fetch_akedata_manifest


_TABLE_MAX_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AccountDetailNameMap:
    """Chinese account-facing labels resolved from the current AKEData snapshot."""

    character_names: Mapping[str, str] = field(default_factory=dict)
    weapon_names: Mapping[str, str] = field(default_factory=dict)
    skill_names: Mapping[str, str] = field(default_factory=dict)
    item_names: Mapping[str, str] = field(default_factory=dict)
    suit_names: Mapping[str, str] = field(default_factory=dict)
    spaceship_skill_names: Mapping[str, str] = field(default_factory=dict)
    spaceship_skill_descriptions: Mapping[str, str] = field(default_factory=dict)
    domain_names: Mapping[str, str] = field(default_factory=dict)
    settlement_names: Mapping[str, str] = field(default_factory=dict)
    spaceship_room_names: Mapping[str, str] = field(default_factory=dict)
    version: str = ""


_name_map_cache: AccountDetailNameMap | None = None
_name_map_lock = asyncio.Lock()
_name_map_generation = 0


def clear_account_detail_name_map() -> int:
    global _name_map_cache, _name_map_generation, _name_map_lock
    removed = int(_name_map_cache is not None)
    _name_map_generation += 1
    _name_map_cache = None
    _name_map_lock = asyncio.Lock()
    return removed


async def fetch_account_detail_name_map() -> AccountDetailNameMap:
    """Load the small set of AKEData tables needed by the account detail card.

    AKEData keeps names as text ids, so the I18n table is loaded alongside the
    entity tables. The parsed map is retained in memory for the current AKE
    version; the shared HTTP cache also prevents duplicate downloads.
    """
    global _name_map_cache
    generation = _name_map_generation
    lock = _name_map_lock

    manifest = await fetch_akedata_manifest()
    latest = str(manifest.get("latest") or "")
    if not latest:
        raise RuntimeError("AKEData manifest has no latest version")
    if _name_map_cache is not None and _name_map_cache.version == latest:
        return _name_map_cache

    async with lock:
        if _name_map_cache is not None and _name_map_cache.version == latest:
            return _name_map_cache
        version = next(
            (
                item
                for item in manifest.get("versions") or ()
                if isinstance(item, Mapping) and str(item.get("id") or "") == latest
            ),
            None,
        )
        table_cfg = str((version or {}).get("tableCfgPath") or "").strip("/")
        if not table_cfg:
            raise RuntimeError(f"AKEData version has no TableCfg path: {latest}")

        values = await asyncio.gather(
            _get(f"/{table_cfg}/CharacterTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/CharGrowthTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/WeaponBasicTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/ItemTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/EquipSuitTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/SpaceshipSkillTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/DomainDataTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/SettlementBasicDataTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/SpaceshipRoomTypeTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/I18nTextTable_CN.json", max_bytes=64 * 1024 * 1024),
            return_exceptions=True,
        )
        (
            character_table,
            growth_table,
            weapon_table,
            item_table,
            suit_table,
            spaceship_skill_table,
            domain_table,
            settlement_table,
            spaceship_room_type_table,
            i18n,
        ) = values
        for value in (character_table, growth_table, weapon_table, item_table, suit_table, i18n):
            if isinstance(value, Exception):
                raise value
        if isinstance(spaceship_skill_table, Exception):
            # Older revisions may not publish this optional table. The other
            # account pages can still use the character/item/i18n tables.
            spaceship_skill_table = {}
        if isinstance(domain_table, Exception):
            domain_table = {}
        if isinstance(settlement_table, Exception):
            settlement_table = {}
        if isinstance(spaceship_room_type_table, Exception):
            spaceship_room_type_table = {}
        built = build_account_detail_name_map(
            character_table,
            growth_table,
            weapon_table,
            item_table,
            suit_table,
            i18n,
            spaceship_skill_table=spaceship_skill_table,
            domain_table=domain_table,
            settlement_table=settlement_table,
            spaceship_room_type_table=spaceship_room_type_table,
            version=latest,
        )
        if generation == _name_map_generation:
            _name_map_cache = built
        return built


def build_account_detail_name_map(
    character_table: Any,
    growth_table: Any,
    weapon_table: Any,
    item_table: Any,
    suit_table: Any,
    i18n: Any,
    *,
    spaceship_skill_table: Any = None,
    domain_table: Any = None,
    settlement_table: Any = None,
    spaceship_room_type_table: Any = None,
    version: str = "",
) -> AccountDetailNameMap:
    """Build an account-detail name map from AKEData table payloads."""
    translations = i18n if isinstance(i18n, Mapping) else {}
    character_names: dict[str, str] = {}
    weapon_names: dict[str, str] = {}
    skill_names: dict[str, str] = {}
    item_names: dict[str, str] = {}
    suit_names: dict[str, str] = {}
    spaceship_skill_names: dict[str, str] = {}
    spaceship_skill_descriptions: dict[str, str] = {}
    domain_names: dict[str, str] = {}
    settlement_names: dict[str, str] = {}
    spaceship_room_names: dict[str, str] = {}

    item_rows = _rows(item_table)
    for key, row in item_rows:
        item_id = _field_text(row.get("id")) or key
        _put(item_names, item_id, _i18n_text(translations, row.get("name")))

    for key, row in _rows(character_table):
        char_id = _field_text(row.get("charId")) or key
        _put(character_names, char_id, _i18n_text(translations, row.get("name")))

    for key, row in _rows(weapon_table):
        weapon_id = _field_text(row.get("weaponId")) or key
        # ItemTable carries the CN display name; WeaponBasicTable.engName is
        # intentionally the English-facing name used by the game data.
        name = item_names.get(weapon_id, "") or _i18n_text(translations, row.get("engName"))
        _put(weapon_names, weapon_id, name)

    for key, row in _rows(growth_table):
        _ = key
        groups = row.get("skillGroupMap")
        if not isinstance(groups, Mapping):
            continue
        for group_key, group in groups.items():
            if not isinstance(group, Mapping):
                continue
            name = _i18n_text(translations, group.get("name"))
            if not name:
                continue
            _put(skill_names, _field_text(group.get("skillGroupId")) or str(group_key), name)
            for skill_id in group.get("skillIdList") or ():
                _put(skill_names, _field_text(skill_id), name)

    for key, row in _rows(suit_table):
        suit_id = _field_text(row.get("suitID")) or key
        entries = row.get("list")
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = _i18n_text(translations, entry.get("suitName"))
            if name:
                _put(suit_names, suit_id, name)
                break

    for key, row in _rows(spaceship_skill_table):
        skill_id = _field_text(row.get("id")) or key
        _put(spaceship_skill_names, skill_id, _i18n_text(translations, row.get("name")))
        _put(
            spaceship_skill_descriptions,
            skill_id,
            _i18n_text(translations, row.get("desc")),
        )

    for key, row in _rows(domain_table):
        domain_id = _field_text(row.get("domainId")) or key
        _put(domain_names, domain_id, _i18n_text(translations, row.get("domainName")))

    for key, row in _rows(settlement_table):
        settlement_id = _field_text(row.get("settlementId")) or key
        _put(
            settlement_names,
            settlement_id,
            _i18n_text(translations, row.get("settlementName")),
        )

    for key, row in _rows(spaceship_room_type_table):
        room_type = _field_text(row.get("type")) or key
        _put(spaceship_room_names, room_type, _i18n_text(translations, row.get("name")))

    return AccountDetailNameMap(
        character_names=character_names,
        weapon_names=weapon_names,
        skill_names=skill_names,
        item_names=item_names,
        suit_names=suit_names,
        spaceship_skill_names=spaceship_skill_names,
        spaceship_skill_descriptions=spaceship_skill_descriptions,
        domain_names=domain_names,
        settlement_names=settlement_names,
        spaceship_room_names=spaceship_room_names,
        version=version,
    )


def _rows(value: Any) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        (str(key), row)
        for key, row in value.items()
        if isinstance(row, Mapping)
    )


def _i18n_text(i18n: Mapping[str, Any], value: Any) -> str:
    return localized_text(value, translations=i18n)


def _put(target: dict[str, str], key: str, value: str) -> None:
    normalized_key = str(key or "").strip()
    normalized_value = str(value or "").strip()
    if normalized_key and normalized_value:
        target[normalized_key] = normalized_value
        target.setdefault(
            hashlib.md5(normalized_key.encode("utf-8")).hexdigest(),
            normalized_value,
        )


def _field_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()
