from __future__ import annotations

from dataclasses import dataclass

# UI order, independent of the upstream object's property order.
COLLECTION_COLUMNS = (
    ("trchestCount", "储藏箱"),
    ("puzzleCount", "醚质"),
    ("blackboxCount", "工业点数"),
    ("pieceCount", "维修灵感点"),
    ("equipTrchestCount", "装备模板箱"),
    ("trstarCount", "塔晶"),
)


@dataclass(frozen=True, slots=True)
class CollectionProgress:
    count: int | None = None
    total: int | None = None

    @property
    def absent(self) -> bool:
        return self.total == 0 and self.count in (None, 0)

    @property
    def inconsistent(self) -> bool:
        return (
            self.count is not None
            and self.total is not None
            and self.count > self.total
        )

    @property
    def complete(self) -> bool:
        return self.total is not None and self.total > 0 and self.count == self.total


@dataclass(frozen=True, slots=True)
class ExplorationLevel:
    level_id: str
    name: str
    collections: tuple[CollectionProgress, ...]


@dataclass(frozen=True, slots=True)
class ExplorationRegion:
    region_id: str
    name: str
    levels: tuple[ExplorationLevel, ...]


@dataclass(frozen=True, slots=True)
class ExplorationView:
    nickname: str
    uid: str
    server_name: str
    saved_at: str
    regions: tuple[ExplorationRegion, ...]
    warnings: tuple[str, ...] = ()
    version: str = ""

    @property
    def level_count(self) -> int:
        return sum(len(region.levels) for region in self.regions)
