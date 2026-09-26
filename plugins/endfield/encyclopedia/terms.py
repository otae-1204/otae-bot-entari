"""词条说明卡：族名、颜色、companion、来源反查。

来源映射按 `snapshot.revision` 做模块级缓存，单趟扫完，不在 `build_view` 里
每张卡重扫一遍技能表。映射不进 `index`：`/ef 物品` 不为词条买单。
"""

from __future__ import annotations

import re

from ..catalog.views.common import (
    _build_term_styles,
    _clean_fz_rich_text,
    _rich_text_visual,
)
from ..providers.assets import static_sprite_url
from ..providers.repository import AkeDataIncomplete, AkeSnapshot, localize
from . import akedata, classify
from .models import EncyclopediaIndex, IndexEntry, TermSourceView, TermView

TAG_RE = re.compile(r"<([@#])(ba\.[A-Za-z0-9_.-]+)>")
TERM_PREFIX = "ba."
# 每条最多画 12 个「名字 + 技能名」，其余写剩余条数。
SOURCE_LIMIT = 12

_SOURCES: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
_SOURCE_ORDER: list[str] = []


async def build_term_index(snapshot: AkeSnapshot) -> EncyclopediaIndex:
    rows = await akedata.hyperlinks(snapshot)
    texts = await akedata.texts(snapshot)
    entries: list[IndexEntry] = []
    for term_id, row in rows.items():
        if not isinstance(row, dict) or not str(term_id).startswith(TERM_PREFIX):
            continue
        name = akedata.text_of(row.get("name"), texts)
        if not name:
            continue
        family_id, family = classify.term_family(str(row.get("richTextId") or ""))
        entries.append(
            IndexEntry(
                kind="term",
                key=str(term_id),
                display_name=name,
                extra_names=(str(term_id),),
                listed=classify.term_listed(name),
                summary=_summary(row, texts),
                group=family_id,
                icon_url=static_sprite_url(row.get("iconPath")),
                subtitle=family,
            )
        )
    entries.sort(key=lambda entry: entry.display_name)
    return EncyclopediaIndex(
        kind="term",
        revision=snapshot.revision,
        entries=tuple(entries),
        types=_present_families(entries),
    )


async def build_term_view(snapshot: AkeSnapshot, term_id: str) -> TermView:
    rows = await akedata.hyperlinks(snapshot)
    row = rows.get(str(term_id))
    if not isinstance(row, dict):
        raise AkeDataIncomplete(f"AKE HyperlinkTextTable is missing {term_id}")
    texts = await akedata.texts(snapshot)
    styles = await akedata.rich_text_styles(snapshot)
    rich_text_id = str(row.get("richTextId") or "")
    color, style_icon = _rich_text_visual(
        styles.get(rich_text_id) or styles.get(str(term_id)) or {}
    )
    family_id, family = classify.term_family(rich_text_id)
    description = akedata.text_of(row.get("desc"), texts)
    sources, total = await _view_sources(snapshot, str(term_id))
    return TermView(
        term_id=str(term_id),
        name=akedata.text_of(row.get("name"), texts),
        family_id=family_id,
        family=family,
        color=color,
        icon_url=static_sprite_url(row.get("iconPath") or style_icon),
        summary=_clean_fz_rich_text(description),
        related=_related(rows, str(term_id), description),
        sources=sources,
        source_total=total,
        revision=snapshot.revision,
    )


async def term_styles_for(snapshot: AkeSnapshot) -> dict:
    """`_build_term_styles` 吃 AkeData 形状的两张表；不用 `_build_fz_term_styles`。"""
    hyperlinks, styles = await snapshot.tables(
        "HyperlinkTextTable", "RichTextStyleTable"
    )
    return _build_term_styles(
        {"hyperlinkTextTable": hyperlinks, "richTextStyleTable": styles}
    )


async def term_sources(snapshot: AkeSnapshot) -> dict[str, tuple[tuple[str, str], ...]]:
    """按 revision 缓存的反向映射 `term_id → ((名字, 技能名), …)`。"""
    revision = snapshot.revision
    cached = _SOURCES.get(revision)
    if cached is not None:
        return cached
    built = await _build_sources(snapshot)
    _SOURCES[revision] = built
    _touch(revision)
    return built


def clear_caches() -> None:
    _SOURCES.clear()
    _SOURCE_ORDER.clear()


async def _build_sources(
    snapshot: AkeSnapshot,
) -> dict[str, tuple[tuple[str, str], ...]]:
    patches, texts = await snapshot.tables("SkillPatchTable", "I18nTextTable_CN")
    growth, items, weapons = await snapshot.tables(
        "CharGrowthTable", "ItemTable", "WeaponBasicTable"
    )
    collected: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str, str]] = set()

    def record(term_id: str, name: str, skill: str) -> None:
        key = (term_id, name, skill)
        if not name or key in seen:
            return
        seen.add(key)
        collected.setdefault(term_id, []).append((name, skill))

    # 干员侧：技能卡文案取自 skillGroupMap 的 group desc；同一组的 skill id 的
    # SkillPatchDataBundle[].description 一并认（两处都可能带标记）。
    for operator_id, row in growth.items():
        if not isinstance(row, dict):
            continue
        operator_name = _text(row.get("name"), texts) or str(operator_id)
        for group in (row.get("skillGroupMap") or {}).values():
            if not isinstance(group, dict):
                continue
            skill_name = _text(group.get("name"), texts)
            tags = set(TAG_RE.findall(_text(group.get("desc"), texts)))
            for skill_id in group.get("skillIdList") or ():
                for bundle in (patches.get(str(skill_id)) or {}).get(
                    "SkillPatchDataBundle"
                ) or ():
                    if isinstance(bundle, dict):
                        tags |= set(TAG_RE.findall(_text(bundle.get("description"), texts)))
                        skill_name = skill_name or _text(bundle.get("skillName"), texts)
            for _, term_id in tags:
                record(term_id, operator_name, skill_name)

    # 武器侧：WeaponBasicTable.weaponSkillList → SkillPatchTable 的 description。
    for weapon_id, row in weapons.items():
        if not isinstance(row, dict):
            continue
        item = items.get(str(weapon_id))
        weapon_name = (
            _text(item.get("name"), texts) if isinstance(item, dict) else ""
        ) or str(weapon_id)
        for skill_id in row.get("weaponSkillList") or ():
            for bundle in (patches.get(str(skill_id)) or {}).get(
                "SkillPatchDataBundle"
            ) or ():
                if not isinstance(bundle, dict):
                    continue
                skill_name = _text(bundle.get("skillName"), texts)
                for _, term_id in set(TAG_RE.findall(_text(bundle.get("description"), texts))):
                    record(term_id, weapon_name, skill_name)
    return {term_id: tuple(rows) for term_id, rows in collected.items()}


async def _view_sources(
    snapshot: AkeSnapshot, term_id: str
) -> tuple[tuple[TermSourceView, ...], int]:
    sources = (await term_sources(snapshot)).get(term_id, ())
    limited = tuple(TermSourceView(name=name, skill=skill) for name, skill in sources[:SOURCE_LIMIT])
    return limited, len(sources)


def _related(rows: dict, term_id: str, description: str) -> tuple[str, ...]:
    related: list[str] = []
    for _, tag_id in TAG_RE.findall(description):
        if tag_id != term_id and tag_id not in related:
            related.append(tag_id)
    # companion 不在正文里也要出现。
    existing = {str(key) for key in rows}
    for candidate in existing:
        if candidate == term_id:
            continue
        if classify.term_companion(candidate, existing) == term_id and candidate not in related:
            related.append(candidate)
    return tuple(related)


def _summary(row: dict, texts: dict) -> str:
    clean = _clean_fz_rich_text(_text(row.get("desc"), texts))
    return clean[:42]


def _text(value: object, texts: dict) -> str:
    return localize(value, texts) if not isinstance(value, str) else value


def _touch(revision: str) -> None:
    if revision in _SOURCE_ORDER:
        _SOURCE_ORDER.remove(revision)
    _SOURCE_ORDER.append(revision)
    while len(_SOURCE_ORDER) > 2:
        stale = _SOURCE_ORDER.pop(0)
        _SOURCES.pop(stale, None)


def _present_families(entries: list[IndexEntry]) -> tuple[tuple[str, str], ...]:
    ordered = list(classify.TERM_FAMILY_NAMES)
    seen: list[str] = []
    for entry in entries:
        if entry.group not in seen:
            seen.append(entry.group)
    seen.sort(key=lambda key: ordered.index(key) if key in ordered else len(ordered))
    return tuple((key, classify.term_family(key)[1]) for key in seen)
