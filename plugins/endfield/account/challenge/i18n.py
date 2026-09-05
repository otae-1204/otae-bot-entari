from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..i18n import localized_text
from ...providers.akedata import _get, fetch_akedata_manifest


_TABLE_MAX_BYTES = 24 * 1024 * 1024
_I18N_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ChallengeDungeonLocale:
    name: str = ""
    desc: str = ""
    feature: str = ""
    additional_target: str = ""


@dataclass(frozen=True, slots=True)
class ChallengeEnemyLocale:
    name: str = ""
    desc: str = ""
    ability: str = ""


@dataclass(frozen=True, slots=True)
class ChallengeLocale:
    """Chinese challenge copy resolved from one AKEData snapshot."""

    text_ids: Mapping[str, Any] = field(default_factory=dict)
    source_texts: Mapping[str, str] = field(default_factory=dict)
    dungeons: Mapping[str, ChallengeDungeonLocale] = field(default_factory=dict)
    monument_groups: Mapping[str, str] = field(default_factory=dict)
    achievements: Mapping[str, str] = field(default_factory=dict)
    enemies: Mapping[str, ChallengeEnemyLocale] = field(default_factory=dict)
    version: str = ""

    def text(self, value: Any, default: str = "") -> str:
        raw = localized_text(value, translations=self.text_ids, default=default)
        if not raw:
            return default
        return self.source_texts.get(_source_key(raw), raw)

    def dungeon_text(self, dungeon_id: str, field_name: str, fallback: Any = "") -> str:
        dungeon = self.dungeons.get(str(dungeon_id or "").strip())
        if dungeon is not None:
            value = str(getattr(dungeon, field_name, "") or "").strip()
            if value:
                return value
        return self.text(fallback)

    def monument_group_text(self, group_id: str, fallback: Any = "") -> str:
        value = self.monument_groups.get(str(group_id or "").strip(), "")
        return value or self.text(fallback)

    def achievement_text(self, achievement_id: str, fallback: Any = "") -> str:
        value = self.achievements.get(str(achievement_id or "").strip(), "")
        return value or self.text(fallback)

    def enemy_text(self, enemy_id: str, field_name: str, fallback: Any = "") -> str:
        raw = localized_text(fallback, translations=self.text_ids)
        translated = self.text(fallback)
        # Prefer an exact source-text translation when it exists: named enemy
        # variants can share one base template but carry a distinct suffix.
        if translated and _source_key(translated) != _source_key(raw):
            return translated
        enemy = self.enemies.get(str(enemy_id or "").strip())
        if enemy is not None:
            value = str(getattr(enemy, field_name, "") or "").strip()
            if value:
                return value
        return translated


EMPTY_CHALLENGE_LOCALE = ChallengeLocale()
_locale_cache: ChallengeLocale | None = None
_locale_lock = asyncio.Lock()
_locale_task: asyncio.Task[ChallengeLocale] | None = None
logger = logging.getLogger(__name__)


def start_challenge_locale_warmup() -> asyncio.Task[ChallengeLocale] | None:
    """Start one shared background refresh without delaying plugin readiness."""
    global _locale_task

    if _locale_cache is not None:
        return None
    if _locale_task is None or _locale_task.done():
        _locale_task = asyncio.create_task(fetch_challenge_locale())
        _locale_task.add_done_callback(_log_warmup_result)
    return _locale_task


async def get_challenge_locale(*, max_wait_seconds: float = 0.75) -> ChallengeLocale | None:
    """Return the cached catalog while keeping cold network I/O off commands."""
    if _locale_cache is not None:
        return _locale_cache
    task = start_challenge_locale_warmup()
    if task is None:
        return _locale_cache
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max_wait_seconds)
    except TimeoutError:
        return None


def _log_warmup_result(task: asyncio.Task[ChallengeLocale]) -> None:
    try:
        locale = task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("challenge i18n warmup failed: %s", exc)
    else:
        logger.info("challenge i18n ready: version=%s", locale.version)


async def fetch_challenge_locale() -> ChallengeLocale:
    """Load and cache the current CN challenge localization catalog."""
    global _locale_cache

    manifest = await fetch_akedata_manifest()
    latest = str(manifest.get("latest") or "")
    if not latest:
        raise RuntimeError("AKEData manifest has no latest version")
    if _locale_cache is not None and _locale_cache.version == latest:
        return _locale_cache

    async with _locale_lock:
        if _locale_cache is not None and _locale_cache.version == latest:
            return _locale_cache
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

        (
            chinese,
            english,
            dungeon_table,
            dungeon_series_table,
            achievement_table,
            enemy_table,
            enemy_template_display_table,
            enemy_ability_desc_table,
        ) = await asyncio.gather(
            _get(f"/{table_cfg}/I18nTextTable_CN.json", max_bytes=_I18N_MAX_BYTES),
            _get(f"/{table_cfg}/I18nTextTable_EN.json", max_bytes=_I18N_MAX_BYTES),
            _get(f"/{table_cfg}/DungeonTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/DungeonSeriesTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/AchievementTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(f"/{table_cfg}/EnemyTable.json", max_bytes=_TABLE_MAX_BYTES),
            _get(
                f"/{table_cfg}/EnemyTemplateDisplayInfoTable.json",
                max_bytes=_TABLE_MAX_BYTES,
            ),
            _get(f"/{table_cfg}/EnemyAbilityDescTable.json", max_bytes=_TABLE_MAX_BYTES),
        )
        _locale_cache = build_challenge_locale(
            chinese,
            english,
            dungeon_table,
            dungeon_series_table=dungeon_series_table,
            achievement_table=achievement_table,
            enemy_table=enemy_table,
            enemy_template_display_table=enemy_template_display_table,
            enemy_ability_desc_table=enemy_ability_desc_table,
            version=latest,
        )
        return _locale_cache


def build_challenge_locale(
    chinese: Any,
    english: Any,
    dungeon_table: Any = None,
    *,
    dungeon_series_table: Any = None,
    achievement_table: Any = None,
    enemy_table: Any = None,
    enemy_template_display_table: Any = None,
    enemy_ability_desc_table: Any = None,
    version: str = "",
) -> ChallengeLocale:
    """Build a CN-first catalog from paired client i18n tables.

    Skland challenge endpoints sometimes ignore ``sk-language`` and return a
    bare English string.  The CN and EN tables share text ids, so an exact
    source-string reverse map restores the Chinese copy without machine
    translation.  Ambiguous English strings are deliberately excluded.
    """
    cn = chinese if isinstance(chinese, Mapping) else {}
    en = english if isinstance(english, Mapping) else {}
    source_texts = _source_translation_map(cn, en)
    dungeons: dict[str, ChallengeDungeonLocale] = {}
    monument_groups: dict[str, str] = {}
    achievements: dict[str, str] = {}
    enemies: dict[str, ChallengeEnemyLocale] = {}

    if isinstance(dungeon_table, Mapping):
        for key, value in dungeon_table.items():
            if not isinstance(value, Mapping):
                continue
            dungeon_id = _field_text(value.get("dungeonId")) or str(key)
            entry = ChallengeDungeonLocale(
                name=localized_text(value.get("dungeonName"), translations=cn),
                desc=localized_text(value.get("dungeonDesc"), translations=cn),
                feature=localized_text(value.get("featureDesc"), translations=cn),
                additional_target=localized_text(value.get("extraGoalDesc"), translations=cn),
            )
            for alias in {str(key), dungeon_id}:
                normalized = alias.strip()
                if not normalized:
                    continue
                dungeons[normalized] = entry
                dungeons.setdefault(hashlib.md5(normalized.encode("utf-8")).hexdigest(), entry)

    if isinstance(dungeon_series_table, Mapping):
        for key, value in dungeon_series_table.items():
            if (
                not isinstance(value, Mapping)
                or value.get("gameCategory") != "dungeon_highdifficulty"
            ):
                continue
            name = localized_text(value.get("name"), translations=cn)
            if name:
                _add_hashed_aliases(monument_groups, name, key, value.get("id"))

    if isinstance(achievement_table, Mapping):
        for key, value in achievement_table.items():
            if not isinstance(value, Mapping):
                continue
            name = localized_text(value.get("name"), translations=cn)
            if name:
                _add_hashed_aliases(achievements, name, key, value.get("id"))

    enemy_templates: dict[str, ChallengeEnemyLocale] = {}
    if isinstance(enemy_template_display_table, Mapping):
        abilities = (
            enemy_ability_desc_table
            if isinstance(enemy_ability_desc_table, Mapping)
            else {}
        )
        for key, value in enemy_template_display_table.items():
            if not isinstance(value, Mapping):
                continue
            template_id = _field_text(value.get("templateId")) or str(key)
            ability_lines = []
            for ability_id in value.get("abilityDescIds") or ():
                ability = abilities.get(str(ability_id))
                if not isinstance(ability, Mapping):
                    continue
                line = localized_text(ability.get("description"), translations=cn)
                if line:
                    ability_lines.append(line)
            entry = ChallengeEnemyLocale(
                name=localized_text(value.get("name"), translations=cn),
                desc=localized_text(value.get("description"), translations=cn),
                ability="\n".join(ability_lines),
            )
            for alias in {str(key), template_id}:
                normalized = alias.strip()
                if normalized:
                    enemy_templates[normalized] = entry
            _add_hashed_aliases(enemies, entry, key, template_id)

    if isinstance(enemy_table, Mapping):
        for key, value in enemy_table.items():
            if not isinstance(value, Mapping):
                continue
            template_id = _field_text(value.get("templateId"))
            entry = enemy_templates.get(template_id)
            if entry is not None:
                _add_hashed_aliases(enemies, entry, key, value.get("enemyId"))

    return ChallengeLocale(
        text_ids=cn,
        source_texts=source_texts,
        dungeons=dungeons,
        monument_groups=monument_groups,
        achievements=achievements,
        enemies=enemies,
        version=version,
    )


def _add_hashed_aliases(output: dict[str, Any], value: Any, *aliases: Any) -> None:
    for alias in aliases:
        normalized = _field_text(alias)
        if not normalized:
            continue
        output[normalized] = value
        output.setdefault(hashlib.md5(normalized.encode("utf-8")).hexdigest(), value)


def _source_translation_map(chinese: Mapping[str, Any], english: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    ambiguous: set[str] = set()
    for text_id, english_value in english.items():
        source = localized_text(english_value)
        translated = localized_text(chinese.get(str(text_id)))
        key = _source_key(source)
        if not key or not translated or source == translated or key in ambiguous:
            continue
        existing = output.get(key)
        if existing is not None and existing != translated:
            output.pop(key, None)
            ambiguous.add(key)
            continue
        output[key] = translated
    return output


def _source_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _field_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()
