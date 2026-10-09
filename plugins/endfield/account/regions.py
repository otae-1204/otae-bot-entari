"""Parent-region theme colours shared by the account-family cards."""

from __future__ import annotations

REGION_FALLBACK_COLOR = "#ffd000"
_REGION_THEME_COLORS = {
    "domain_1": "#c1ff55",
    "domain_2": "#6bffff",
}
_REGION_NAME_THEME_COLORS = {
    "四号谷地": "#c1ff55",
    "武陵": "#6bffff",
}


def region_theme_color(region_id: str, name: str) -> str:
    """Match by domain ID first, then by name; unknown regions take the signature yellow."""
    return _REGION_THEME_COLORS.get(
        region_id, _REGION_NAME_THEME_COLORS.get(name, REGION_FALLBACK_COLOR)
    )
