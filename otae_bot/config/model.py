"""Typed settings models: the one model kind every config section is declared with.

A section is a ``Settings`` subclass (an Entari ``BasicConfModel``, i.e. a keyword-only
dataclass) whose fields carry a default and a short Chinese description::

    class RadarSettings(Settings):
        timeout: float = field(default=20.0, description="单次请求超时秒数")

Entari's validator does not convert scalars, and values interpolated from ``.env``
always arrive as strings, so :func:`validate` coerces ``str``/``int``/``float``/``bool``
fields (and lists of them) first. A value that cannot be converted, or a ``${{ env.X }}``
placeholder whose variable is not set, falls back to the field default with one warning
that names the key, never the value.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from typing import Any, TypeVar

from arclet.entari.config import BasicConfModel, config_model_validate, model_field
from loguru import logger

__all__ = ["Settings", "field", "validate"]

field = model_field
T = TypeVar("T", bound="Settings")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_warned: set[tuple[str, str]] = set()


class Settings(BasicConfModel):
    """Base of every settings section (plugins and core)."""


class _Invalid(Exception):
    pass


def _warn(where: str, key: str, reason: str) -> None:
    if (where, key) in _warned:
        return
    _warned.add((where, key))
    logger.warning(f"[config] {where}.{key} {reason}; using the default")


def _scalar(value: Any, tp: Any) -> Any:
    if tp is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise _Invalid
    if tp is int:
        if isinstance(value, bool):
            raise _Invalid
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        try:
            return int(str(value).strip())
        except ValueError:
            raise _Invalid from None
    if tp is float:
        if isinstance(value, bool):
            raise _Invalid
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except ValueError:
            raise _Invalid from None
    if tp is str:
        if isinstance(value, (dict, list)):
            raise _Invalid
        return value if isinstance(value, str) else str(value)
    return value


def _coerce(value: Any, tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        if value is None:
            return None
        if len(args) == 1:
            return _coerce(value, args[0])
        return value
    if origin in (list, tuple, set, frozenset):
        (item,) = typing.get_args(tp)[:1] or (Any,)
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple, set)):
            raise _Invalid
        return [_coerce(entry, item) for entry in value]
    if origin is dict:
        if not isinstance(value, Mapping):
            raise _Invalid
        return dict(value)
    if tp in (bool, int, float, str):
        if value is None:
            raise _Invalid
        return _scalar(value, tp)
    return value


def _unresolved(value: Any) -> bool:
    return isinstance(value, str) and "${{" in value


def validate(model: type[T], data: Mapping[str, Any] | None, where: str) -> T:
    """Build ``model`` from a raw section; ``where`` names it in warnings (e.g. ``plugins.radar``)."""
    data = data or {}
    hints = typing.get_type_hints(model)
    names = {item.name for item in dataclasses.fields(model)}  # type: ignore[arg-type]
    prepared: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).startswith("$"):
            continue  # Entari's own keys: $disable, $filter, $priority ...
        if key not in names:
            _warn(where, str(key), "is not a known setting and is ignored")
            continue
        if _unresolved(value):
            _warn(where, key, "references an environment variable that is not set")
            continue
        tp = hints.get(key, Any)
        nested = tp if isinstance(tp, type) and issubclass(tp, Settings) else None
        if nested is not None:
            prepared[key] = validate(nested, value if isinstance(value, Mapping) else {}, f"{where}.{key}")
            continue
        try:
            prepared[key] = _coerce(value, tp)
        except _Invalid:
            _warn(where, key, "has an invalid value")
    return config_model_validate(model, prepared)
