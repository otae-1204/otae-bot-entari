"""修正 Entari 的 loguru_exc_callback 在 Python 3.13 上的签名不兼容。

Entari 导入时把 ``traceback.print_exception`` 与 ``sys.excepthook`` 都换成了
``loguru_exc_callback(cls, val, tb, *_, **__)``，三个位置参数都是必填。Python 3.10 起
``print_exception`` 允许只传异常对象，3.13 的 ``traceback.print_exc()`` 正是这样调用：
``print_exception(sys.exception(), limit=..., file=..., chain=...)``。于是 satori 在事件
回调出错时调用 ``print_exc()``（例如引用消息为空的那类事件），会再抛
``TypeError: missing 2 required positional arguments``，原始异常反而没有记下来。

这里装一个与标准库同签名的替身：新旧两种调用方式都接受，没有异常可报时什么也不做，
最后仍交给 Entari 的回调，日志格式保持不变。
"""

from __future__ import annotations

import sys
import traceback
from types import TracebackType
from typing import Any, Callable

from loguru import logger

_UNSET: Any = object()
ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _fallback_callback(
    cls: type[BaseException], val: BaseException, tb: TracebackType | None
) -> None:
    logger.opt(exception=(cls, val, tb)).error("Exception:")


def _entari_callback() -> Callable[..., None]:
    try:
        from arclet.entari.logger import loguru_exc_callback
    except ImportError:
        return _fallback_callback
    return loguru_exc_callback


def normalize_exc_info(exc: Any, value: Any = _UNSET, tb: Any = _UNSET) -> ExcInfo | None:
    """Accept both ``(exc)`` and ``(type, value, tb)`` forms; ``None`` when nothing to report."""
    if value is _UNSET:
        if not isinstance(exc, BaseException):
            return None  # print_exc() 没有正在处理的异常：sys.exception() 为 None
        value = exc
    if not isinstance(value, BaseException):
        return None
    if tb is _UNSET:
        tb = value.__traceback__
    exc_type = exc if isinstance(exc, type) and issubclass(exc, BaseException) else type(value)
    return exc_type, value, tb


def print_exception(
    exc: Any,
    /,
    value: Any = _UNSET,
    tb: Any = _UNSET,
    limit: int | None = None,
    file: Any = None,
    chain: bool = True,
    **_ignored: Any,
) -> None:
    info = normalize_exc_info(exc, value, tb)
    if info is not None:
        _entari_callback()(*info)


def excepthook(exc_type: Any, value: Any, tb: Any) -> None:
    info = normalize_exc_info(exc_type, value, tb)
    if info is not None:
        _entari_callback()(*info)


def install_exception_hooks() -> None:
    """Call after ``arclet.entari`` is imported, which installs its own hooks on import."""
    traceback.print_exception = print_exception
    sys.excepthook = excepthook
