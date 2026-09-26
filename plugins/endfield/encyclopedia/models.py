"""图鉴查询的数据结构：只放 dataclass。

`tests/test_architecture.py` 对 `models.py` 的禁令照旧生效：不 import
`arclet.entari`、`otae_bot.adapters`，也不 import 路径含 `rendering` 的模块。
索引 dataclass 放在这里，`draw` / `service` 就不必为了类型去 import 带锁的 `index.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..catalog.aliases import normalize_alias_text
from ..catalog.models import TermStyleView


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """一条可检索的图鉴条目。索引里只放名字、短描述与图标，不放长正文。"""

    kind: str
    key: str
    display_name: str
    extra_names: tuple[str, ...] = ()
    listed: bool = True
    summary: str = ""
    group: str = ""
    icon_url: str = ""
    rarity: int = 0
    subtitle: str = ""


@dataclass(frozen=True, slots=True)
class EncyclopediaIndex:
    """按 (revision, kind) 分片建出来的索引；`types` 是目录分组名。"""

    kind: str
    revision: str
    entries: tuple[IndexEntry, ...] = ()
    types: tuple[tuple[str, str], ...] = ()
    redirects: frozenset[str] = frozenset()

    def exact_names(self, kind: str, query: str) -> tuple[str, ...]:
        """规范化后全等命中；返回正式名元组，别名添加据此拒绝歧义。"""
        normalized = normalize_alias_text(query)
        if not normalized:
            return ()
        names = {
            entry.display_name
            for entry in self.entries
            if entry.kind == kind
            and normalize_alias_text(entry.display_name) == normalized
        }
        return tuple(sorted(names))

    def type_name(self, key: str) -> str:
        for type_key, name in self.types:
            if type_key == str(key):
                return name
        return ""

    def group_entries(self, group: str) -> tuple[IndexEntry, ...]:
        return tuple(entry for entry in self.entries if entry.group == str(group))


@dataclass(slots=True)
class CatalogItem:
    key: str
    name: str
    icon_url: str = ""
    rarity: int = 0
    subtitle: str = ""


@dataclass(slots=True)
class CatalogGroup:
    key: str
    name: str
    count: int = 0
    items: tuple[CatalogItem, ...] = ()


@dataclass(slots=True)
class EncyclopediaCatalogView:
    kind: str
    title: str
    groups: tuple[CatalogGroup, ...] = ()
    total: int = 0
    source: str = "akedata"
    revision: str = ""
    page_number: int = 1
    page_count: int = 1


@dataclass(slots=True)
class ItemView:
    item_id: str
    name: str
    type_name: str = ""
    rarity: int = 0
    icon_url: str = ""
    description: str = ""
    deco_description: str = ""
    obtain_ways: tuple[str, ...] = ()
    no_obtain_hint: str = ""
    source: str = "akedata"
    revision: str = ""


@dataclass(slots=True)
class PropView:
    item_id: str
    name: str
    type_name: str = ""
    bucket: str = ""
    rarity: int = 0
    icon_url: str = ""
    effect_lines: tuple[str, ...] = ()
    duration: float | None = None
    is_persistent: bool = False
    cooldown: float | None = None
    cast_time: float | None = None
    charge_count: int | None = None
    recover_upper_count: int | None = None
    term_styles: dict[str, TermStyleView] = field(default_factory=dict)
    source: str = "akedata"
    revision: str = ""


@dataclass(slots=True)
class EnemyAbilityView:
    name: str = ""
    description: str = ""


@dataclass(slots=True)
class EnemyResistanceView:
    """与关卡卡同形：`percent` 是受伤比例，颜色不带 `#`。"""

    element: str
    label: str
    percent: float | None = None
    color: str = ""


@dataclass(slots=True)
class EnemyView:
    template_id: str
    name: str = ""
    nickname: str = ""
    description: str = ""
    display_type: int = 0
    display_type_name: str = ""
    icon_url: str = ""
    abilities: tuple[EnemyAbilityView, ...] = ()
    resistances: tuple[EnemyResistanceView, ...] = ()
    resistance_template_count: int = 0
    distributions: tuple[str, ...] = ()
    distribution_count: int = 0
    variant_count: int = 0
    source: str = "akedata"
    revision: str = ""


@dataclass(slots=True)
class TermSourceView:
    name: str
    skill: str = ""


@dataclass(slots=True)
class TermView:
    term_id: str
    name: str = ""
    family_id: str = ""
    family: str = ""
    color: str = ""
    icon_url: str = ""
    summary: str = ""
    related: tuple[str, ...] = ()
    sources: tuple[TermSourceView, ...] = ()
    source_total: int = 0
    source: str = "akedata"
    revision: str = ""


@dataclass(slots=True)
class ArchiveEntryView:
    item_id: str
    name: str = ""
    page_name: str = ""
    category_name: str = ""
    group_name: str = ""
    group_sub_name: str = ""
    icon_url: str = ""
    version: str = ""
    source: str = "akedata"
