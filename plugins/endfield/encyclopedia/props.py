"""道具索引与道具卡视图；效果句只读 `UseItemTable` 的 `blackboard`。

数字口径（附录 B.5 的修订）：只读 `blackboard[].value`，`value` 为 `None`
视为缺 key；`key` 写成 `<buffId>\\<key>` 且该 buffId 就是本条 action 的 buffId
时按 `<key>` 查；不代入算式、不跨 action 合并、不读 `valueStr`、不下载 `BuffData`。
"""

from __future__ import annotations

import re

from ..providers.assets import item_icon_urls
from ..providers.repository import AkeDataIncomplete, AkeSnapshot
from . import akedata, classify, terms
from .models import EncyclopediaIndex, IndexEntry, PropView

PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
BUFF_BLACKBOARD_KEY = "buffBBData"
NAMESPACE_SEPARATOR = "\\"


class PropEffectIncomplete(ValueError):
    """没有任何一条 action 能填满效果句里的全部占位符。"""


def extract_blackboard(rows: object) -> dict[str, float | int]:
    """`blackboard` → `{key: value}`；只读 `value`，`None` 视为缺 key。"""
    values: dict[str, float | int] = {}
    if not isinstance(rows, (list, tuple)):
        return values
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        value = entry.get("value")
        if not key or value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        values[key] = value
    return values


def render_effect_lines(desc: object, actions: object) -> tuple[str, ...]:
    """逐 action 填满全部占位符；全失败抛 `PropEffectIncomplete`。"""
    template = str(desc or "")
    if not template:
        raise PropEffectIncomplete("道具效果文案为空")
    keys = [match.group(1).partition(":")[0].strip() for match in PLACEHOLDER_RE.finditer(template)]
    if not keys:
        return _lines(template)
    for action in actions if isinstance(actions, (list, tuple)) else ():
        values = _action_values(action)
        if values is None:
            continue
        rendered = _fill(template, values, keys)
        if rendered is not None:
            return _lines(rendered)
    raise PropEffectIncomplete("道具效果数值未收录")


def _action_values(action: object) -> dict[str, float | int] | None:
    if not isinstance(action, dict):
        return None
    block = action.get(BUFF_BLACKBOARD_KEY)
    block = block if isinstance(block, dict) else {}
    rows = block.get("blackboard")
    if _has_conflict(rows):
        return None
    values = extract_blackboard(rows)
    buff_id = str(block.get("buffId") or "").strip()
    if buff_id:
        for key, value in list(values.items()):
            values.setdefault(f"{buff_id}{NAMESPACE_SEPARATOR}{key}", value)
    return values


def _has_conflict(rows: object) -> bool:
    seen: dict[str, object] = {}
    if not isinstance(rows, (list, tuple)):
        return False
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        value = entry.get("value")
        if not key or value is None:
            continue
        if key in seen and seen[key] != value:
            return True
        seen[key] = value
    return False


def _fill(template: str, values: dict[str, float | int], keys: list[str]) -> str | None:
    if any(key not in values for key in keys):
        return None

    def replace(match: re.Match[str]) -> str:
        key_expr, _, fmt = match.group(1).partition(":")
        value = values.get(key_expr.strip())
        if value is None:
            return match.group(0)
        from ..catalog.views.common import _format_template_value

        return _format_template_value(value, fmt)

    rendered = PLACEHOLDER_RE.sub(replace, template)
    if "{" in rendered:
        return None
    return rendered


def _lines(text: str) -> tuple[str, ...]:
    from ..catalog.views.common import _clean_fz_rich_text

    cleaned = _clean_fz_rich_text(text)
    return tuple(line.strip() for line in cleaned.split("\n") if line.strip())


async def build_prop_index(snapshot: AkeSnapshot) -> EncyclopediaIndex:
    rows = await akedata.items(snapshot)
    texts = await akedata.texts(snapshot)
    use_rows = await akedata.use_items(snapshot)
    use_ids = set(use_rows)
    type_names = await akedata.item_type_names(snapshot)
    entries: list[IndexEntry] = []
    for item_id, use_row in use_rows.items():
        row = rows.get(str(item_id))
        if not isinstance(row, dict):
            continue
        type_id = _int(row.get("type"))
        if classify.kind_for_item(type_id, str(item_id), use_ids) != "prop":
            continue
        name = akedata.text_of(row.get("name"), texts)
        if not name:
            continue
        bucket = classify.prop_bucket(type_id)
        entries.append(
            IndexEntry(
                kind="prop",
                key=str(item_id),
                display_name=name,
                extra_names=(str(item_id),),
                summary=_summary(use_row),
                group=bucket,
                icon_url=_icon(str(item_id), row.get("iconId")),
                rarity=_int(row.get("rarity")) or 0,
                subtitle=type_names.get(type_id, "") if type_id is not None else "",
            )
        )
    entries.sort(key=lambda entry: entry.display_name)
    return EncyclopediaIndex(
        kind="prop",
        revision=snapshot.revision,
        entries=tuple(entries),
        types=_bucket_types(entries),
    )


async def build_prop_view(snapshot: AkeSnapshot, item_id: str) -> PropView:
    rows = await akedata.items(snapshot)
    use_rows = await akedata.use_items(snapshot)
    row = rows.get(str(item_id))
    use_row = use_rows.get(str(item_id))
    if not isinstance(row, dict) or not isinstance(use_row, dict):
        raise AkeDataIncomplete(f"AKE UseItemTable is missing {item_id}")
    texts = await akedata.texts(snapshot)
    type_names = await akedata.item_type_names(snapshot)
    equip = (await akedata.equip_items(snapshot)).get(str(item_id))
    equip = equip if isinstance(equip, dict) else {}
    type_id = _int(row.get("type"))
    lines = render_effect_lines(
        akedata.text_of(use_row.get("itemUseDesc"), texts), use_row.get("useActions")
    )
    styles = await terms.term_styles_for(snapshot)
    return PropView(
        item_id=str(item_id),
        name=akedata.text_of(row.get("name"), texts),
        type_name=type_names.get(type_id, "") if type_id is not None else "",
        bucket=classify.prop_bucket(type_id),
        rarity=_int(row.get("rarity")) or 0,
        icon_url=_icon(str(item_id), row.get("iconId")),
        effect_lines=lines,
        duration=_number(use_row.get("duration")),
        is_persistent=bool(use_row.get("isPersistentBuff")),
        cooldown=_number(equip.get("cooldown")),
        cast_time=_number(equip.get("castTime")),
        charge_count=_int(equip.get("chargeCount")),
        recover_upper_count=_int(equip.get("recoverUpperCount")),
        term_styles=styles,
        revision=snapshot.revision,
    )


def _bucket_types(entries: list[IndexEntry]) -> tuple[tuple[str, str], ...]:
    ordered = [*classify.PROP_BUCKETS.values(), classify.PROP_OTHER_BUCKET]
    present = {entry.group for entry in entries}
    return tuple((name, name) for name in ordered if name in present)


def _summary(row: dict) -> str:
    actions = row.get("useActions")
    if not isinstance(actions, (list, tuple)):
        return ""
    for action in actions:
        values = _action_values(action)
        if values:
            return "、".join(f"{key} {value:g}" for key, value in list(values.items())[:3])
    return ""


def _icon(item_id: str, icon_id: object) -> str:
    urls = item_icon_urls(item_id, str(icon_id or ""))
    return urls[0] if urls else ""


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    # duration == 0 存 None；其余照抄表里的值。
    return None if number == 0 else number


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
