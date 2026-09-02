"""Warfarin / AkeData 静态图 URL，以及把其它数据源的图补进主 View。"""

from __future__ import annotations

from typing import Callable, TypeVar

from .models import EffectView, OperatorView, SkillView, WeaponView

WARFARIN_STATIC_BASE = "https://static.warfarin.wiki/v4"
AKEDATA_SPRITES_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)

_T = TypeVar("_T")


def unique_urls(*urls: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))


def is_http_url(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("http://", "https://", "data:"))


def operator_icon_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.extend(
            (
                f"{WARFARIN_STATIC_BASE}/charicon/icon_{operator_id}.webp",
                f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_{operator_id}.png",
                f"{AKEDATA_SPRITES_BASE}/charicon/icon_{operator_id}.png",
            )
        )
    return unique_urls(primary, *extras)


def operator_round_icon_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.extend(
            (
                f"{WARFARIN_STATIC_BASE}/charroundicon/icon_round_{operator_id}.webp",
                f"{AKEDATA_SPRITES_BASE}/charroundicon/icon_round_{operator_id}.png",
            )
        )
    return unique_urls(primary, *extras)


def operator_portrait_urls(operator_id: str, primary: str = "") -> tuple[str, ...]:
    operator_id = str(operator_id or "").strip()
    extras: list[str] = []
    if operator_id:
        extras.append(f"{WARFARIN_STATIC_BASE}/characterportrait/{operator_id}.webp")
    return unique_urls(primary, *extras)


def skill_icon_urls(*icon_ids: str) -> tuple[str, ...]:
    urls: list[str] = []
    for item in icon_ids:
        text = str(item or "").strip()
        if not text:
            continue
        if is_http_url(text):
            urls.append(text)
            continue
        urls.append(f"{WARFARIN_STATIC_BASE}/skillicon/{text}.webp")
        urls.append(f"{AKEDATA_SPRITES_BASE}/skillicon/{text}.png")
    return unique_urls(*urls)


def item_icon_urls(item_id: str, primary: str = "") -> tuple[str, ...]:
    item_id = str(item_id or "").strip()
    extras: list[str] = []
    if item_id:
        extras.extend(
            (
                f"{WARFARIN_STATIC_BASE}/itemicon/{item_id}.webp",
                f"{AKEDATA_SPRITES_BASE}/itemiconbig/{item_id}.png",
                f"{AKEDATA_SPRITES_BASE}/itemicon/{item_id}.png",
            )
        )
    return unique_urls(primary, *extras)


def apply_operator_asset_donor(primary: OperatorView, donor: OperatorView) -> None:
    if not primary.operator_id:
        primary.operator_id = donor.operator_id
    if not primary.icon_url:
        primary.icon_url = donor.icon_url
    if not primary.round_icon_url:
        primary.round_icon_url = donor.round_icon_url
    if not primary.portrait_url:
        primary.portrait_url = donor.portrait_url
    _donate_skill_icons(primary.skills, donor.skills)
    _donate_effect_icons(primary.talents, donor.talents)
    _donate_effect_icons(primary.potentials, donor.potentials)


def apply_weapon_asset_donor(primary: WeaponView, donor: WeaponView) -> None:
    if not primary.weapon_id:
        primary.weapon_id = donor.weapon_id
    if not primary.icon_url:
        primary.icon_url = donor.icon_url


def _donate_skill_icons(primary: list[SkillView], donors: list[SkillView]) -> None:
    leftover = list(donors)
    for skill in primary:
        match = _take_match(leftover, lambda item, current=skill: item.title == current.title)
        if match is None:
            match = _take_match(leftover, lambda item, current=skill: bool(current.category) and item.category == current.category)
        if match is None:
            continue
        skill.icon_fallbacks = unique_urls(*skill.icon_fallbacks, match.icon_id, *match.icon_fallbacks)


def _donate_effect_icons(primary: list[EffectView], donors: list[EffectView]) -> None:
    leftover = list(donors)
    for effect in primary:
        match = _take_match(leftover, lambda item, current=effect: _effect_title_key(item) == _effect_title_key(current))
        if match is None:
            continue
        effect.icon_fallbacks = unique_urls(*effect.icon_fallbacks, match.icon_url, *match.icon_fallbacks)


def _effect_title_key(effect: EffectView) -> str:
    title = str(effect.title or "").strip()
    if title[:1] in {"T", "P"} and len(title) > 2 and title[1].isdigit():
        title = title.split(" ", 1)[-1]
    return title


def _take_match(items: list[_T], predicate: Callable[[_T], bool]) -> _T | None:
    for index, item in enumerate(items):
        if predicate(item):
            return items.pop(index)
    return None


def operator_needs_asset_donor(view: OperatorView) -> bool:
    if not view.operator_id or not view.icon_url or not view.portrait_url or not view.round_icon_url:
        return True
    if any(not skill.icon_id for skill in view.skills):
        return True
    if any(not effect.icon_url for effect in (*view.talents, *view.potentials)):
        return True
    return _has_fz_asset(
        view.icon_url,
        view.round_icon_url,
        view.portrait_url,
        *(skill.icon_id for skill in view.skills),
        *(effect.icon_url for effect in (*view.talents, *view.potentials)),
    )


def weapon_needs_asset_donor(view: WeaponView) -> bool:
    if not view.weapon_id or not view.icon_url:
        return True
    return _has_fz_asset(view.icon_url)


def _has_fz_asset(*urls: str) -> bool:
    return any("assets.fz.wiki" in str(url) for url in urls if url)
