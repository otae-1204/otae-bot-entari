from __future__ import annotations


GENERIC_FZ_METRIC_FAMILIES = {
    "攻击倍率": ("倍率",),
    "失衡值": ("失衡值",),
    "持续时间": ("持续时间", "时长"),
    "技力": ("技力", "终结技能量"),
}

SUPPLEMENTAL_FZ_METRIC_MARKERS = ("额外", "追加", "附加", "附带", "延长")


def is_generic_fz_metric(name: str) -> bool:
    return name in GENERIC_FZ_METRIC_FAMILIES


def fz_metric_replaces_generic(generic_name: str, specific_name: str) -> bool:
    family_terms = GENERIC_FZ_METRIC_FAMILIES.get(generic_name)
    if not family_terms or specific_name == generic_name:
        return False
    if not any(term in specific_name for term in family_terms):
        return False
    return not any(marker in specific_name for marker in SUPPLEMENTAL_FZ_METRIC_MARKERS)
