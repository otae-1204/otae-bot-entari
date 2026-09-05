from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InvestmentResourceView:
    item_id: str
    name: str
    count: int
    icon_url: str = ""
    stamina_cost: float | None = None

    @property
    def estimated_stamina(self) -> float | None:
        if self.stamina_cost is None:
            return None
        return self.count * self.stamina_cost


@dataclass(frozen=True, slots=True)
class InvestmentCategoryView:
    key: str
    label: str
    stamina: float
    note: str = ""


@dataclass(frozen=True, slots=True)
class InvestmentContributionView:
    operator_id: str
    name: str
    portrait_url: str = ""
    rarity: int = 0
    body_stamina: float = 0.0
    skill_stamina: float = 0.0
    weapon_stamina: float = 0.0
    missing: tuple[str, ...] = ()
    exact_total_stamina: float | None = None

    @property
    def total_stamina(self) -> float:
        if self.exact_total_stamina is not None:
            return self.exact_total_stamina
        return self.body_stamina + self.skill_stamina + self.weapon_stamina


@dataclass(frozen=True, slots=True)
class AccountInvestmentView:
    nickname: str
    uid: str
    server_name: str = ""
    saved_at: str = ""
    source_revision: str = ""
    operator_count: int = 0
    equipped_weapon_count: int = 0
    character_exp: int = 0
    weapon_exp: int = 0
    gold: int = 0
    stamina: float = 0.0
    categories: tuple[InvestmentCategoryView, ...] = ()
    resources: tuple[InvestmentResourceView, ...] = ()
    contributions: tuple[InvestmentContributionView, ...] = ()
    covered_components: int = 0
    expected_components: int = 0
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing and self.covered_components >= self.expected_components

    @property
    def coverage_label(self) -> str:
        if self.expected_components <= 0:
            return "暂无可统计的养成对象"
        return f"数据覆盖 {self.covered_components}/{self.expected_components} 项"

    @property
    def total_label(self) -> str:
        return "当前档案可见养成投入" if self.complete else "已知投入至少"
