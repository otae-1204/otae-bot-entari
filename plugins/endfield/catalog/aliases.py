from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from functools import lru_cache
from ..paths import ALIAS_DATA_PATH


# 别名库覆盖的种类。图鉴种类（item/prop/enemy/term/archive_entry）在
# alias_data.json 里没有条目，add_alias 靠调用方注入的 lookup 核对正式名。
FILE_KINDS = frozenset({"operator", "weapon", "equipment"})
ENCYCLOPEDIA_KINDS = frozenset({"item", "prop", "enemy", "term", "archive_entry"})
SUPPORTED_KINDS = FILE_KINDS | ENCYCLOPEDIA_KINDS


def aliases_for(kind: str, canonical_name: str) -> tuple[str, ...]:
    normalized_kind = _normalize_kind(kind)
    normalized_name = _normalize(canonical_name)
    if not normalized_kind or not normalized_name:
        return ()
    entry = _alias_data()[normalized_kind].get(normalized_name)
    return entry[1] if entry else ()


def alias_targets(kind: str, alias: str) -> tuple[str, ...]:
    normalized_kind = _normalize_kind(kind)
    normalized_alias = _normalize(alias)
    if not normalized_kind or not normalized_alias:
        return ()
    return _alias_index()[normalized_kind].get(normalized_alias, ())


def add_alias(
    kind: str,
    canonical_name: str,
    alias: str,
    *,
    lookup: Callable[[str], tuple[str, ...]] | None = None,
) -> tuple[str, bool]:
    """写入一条别名。

    `lookup` 是同步的 `Callable[[str], tuple[str, ...]]`，返回与正式名全等的名字。
    图鉴种类必须传：它们的正式名不在 `alias_data.json` 里，只能由索引核对。
    空返回与返回两个名字都拒绝，避免落一条指向歧义的别名。
    """
    normalized_kind = _normalize_kind(kind)
    canonical_name = str(canonical_name or "").strip()
    alias = str(alias or "").strip()
    if not normalized_kind:
        raise ValueError("别名类型必须是干员、武器、装备、物品、道具、敌人、词条或档案条目")
    if not canonical_name or not alias:
        raise ValueError("正式名称和新别名不能为空")

    raw = json.loads(ALIAS_DATA_PATH.read_text(encoding="utf-8"))
    entries = raw.get(normalized_kind)
    if entries is None and normalized_kind in ENCYCLOPEDIA_KINDS:
        entries = raw[normalized_kind] = {}
    if not isinstance(entries, dict):
        raise ValueError("别名库结构异常")
    normalized_canonical = _normalize(canonical_name)
    canonical = next((name for name in entries if _normalize(name) == normalized_canonical), "")
    if not canonical:
        # 图鉴的正式名不在文件里，交给调用方注入的索引核对。
        matches = (
            tuple(
                dict.fromkeys(
                    str(name).strip() for name in lookup(canonical_name) if str(name).strip()
                )
            )
            if lookup is not None
            else ()
        )
        if not matches:
            raise ValueError(f"别名库中不存在正式名称：{canonical_name}")
        if len(matches) > 1:
            raise ValueError(
                f"正式名称不唯一，请写全名：{canonical_name}（{'、'.join(matches)}）"
            )
        canonical = matches[0]
    if _normalize(alias) == _normalize(canonical):
        raise ValueError("新别名不能与正式名称相同")
    aliases = entries.get(canonical)
    if aliases is None:
        # 正式名由 lookup 核对出来的（图鉴种类不在文件里），第一条别名就地建列表。
        aliases = entries[canonical] = []
    if not isinstance(aliases, list):
        raise ValueError(f"正式名称的别名列表异常：{canonical}")
    if any(_normalize(item) == _normalize(alias) for item in aliases):
        return canonical, False

    aliases.append(alias)
    temporary_path = ALIAS_DATA_PATH.with_name(f".{ALIAS_DATA_PATH.name}.tmp")
    temporary_path.write_text(_render_alias_data(raw), encoding="utf-8", newline="\n")
    temporary_path.replace(ALIAS_DATA_PATH)
    clear_alias_caches()
    return canonical, True


def clear_alias_caches() -> None:
    _alias_data.cache_clear()
    _alias_index.cache_clear()


@lru_cache(maxsize=1)
def _alias_data() -> dict[str, dict[str, tuple[str, tuple[str, ...]]]]:
    raw = json.loads(ALIAS_DATA_PATH.read_text(encoding="utf-8"))
    result = {kind: {} for kind in SUPPORTED_KINDS}
    for kind in SUPPORTED_KINDS:
        entries = raw.get(kind, {})
        if not isinstance(entries, dict):
            continue
        for canonical_name, aliases in entries.items():
            canonical = str(canonical_name or "").strip()
            normalized_name = _normalize(canonical)
            if not canonical or not normalized_name or not isinstance(aliases, list):
                continue
            cleaned = tuple(
                dict.fromkeys(
                    str(alias or "").strip()
                    for alias in aliases
                    if str(alias or "").strip()
                )
            )
            result[kind][normalized_name] = (canonical, cleaned)
    return result


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict[str, tuple[str, ...]]]:
    result: dict[str, dict[str, list[str]]] = {kind: {} for kind in SUPPORTED_KINDS}
    for kind, entries in _alias_data().items():
        for canonical, aliases in entries.values():
            for alias in aliases:
                normalized_alias = _normalize(alias)
                if normalized_alias:
                    result[kind].setdefault(normalized_alias, []).append(canonical)
    return {
        kind: {alias: tuple(dict.fromkeys(names)) for alias, names in entries.items()}
        for kind, entries in result.items()
    }


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    return normalized if normalized in SUPPORTED_KINDS else ""


def _normalize(value: str) -> str:
    return normalize_alias_text(value)


def normalize_alias_text(value: str) -> str:
    folded = str(value or "").casefold()
    normalized = "".join(char for char in folded if char.isalnum())
    if normalized:
        return normalized
    return "".join(
        char for char in folded
        if unicodedata.category(char).startswith("S")
    )


def _render_alias_data(raw: dict[str, object]) -> str:
    lines = ["{"]
    items = list(raw.items())
    for top_index, (key, value) in enumerate(items):
        suffix = "," if top_index < len(items) - 1 else ""
        encoded_key = json.dumps(str(key), ensure_ascii=False)
        if isinstance(value, dict):
            lines.append(f"  {encoded_key}: {{")
            entries = list(value.items())
            for entry_index, (name, aliases) in enumerate(entries):
                entry_suffix = "," if entry_index < len(entries) - 1 else ""
                encoded_name = json.dumps(str(name), ensure_ascii=False)
                encoded_aliases = json.dumps(aliases, ensure_ascii=False, separators=(",", ":"))
                lines.append(f"    {encoded_name}: {encoded_aliases}{entry_suffix}")
            lines.append(f"  }}{suffix}")
        else:
            encoded_value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"  {encoded_key}: {encoded_value}{suffix}")
    lines.append("}")
    return "\n".join(lines) + "\n"
