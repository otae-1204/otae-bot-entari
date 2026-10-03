"""修正 Entari 的 loguru_exc_callback 与标准库调用约定不兼容。

``arclet/entari/logger.py``（0.17.4 第 186–219 行）在模块导入时把 ``traceback.print_exception``
与 ``sys.excepthook`` 都换成了 ``loguru_exc_callback(cls, val, tb, *_, **__)``，三个位置参数都是
必填。Python 3.10 起 ``print_exception`` 允许只传异常对象，3.12 与 3.13 的
``traceback.print_exc()`` 都这样调用：``print_exception(sys.exception(), limit=..., file=..., chain=...)``；
3.13 解释器自己显示异常（默认 ``sys.__excepthook__``、线程默认 excepthook）时还会经
``traceback._print_exception_bltin`` 调 ``print_exception(exc, limit=..., file=..., colorize=...)``。
于是 satori 在事件回调出错时调用 ``print_exc()``（例如引用消息为空的那类事件），会再抛
``TypeError: missing 2 required positional arguments``，原始异常反而没有记下来。

这里装一个与标准库同签名的替身：新旧两种调用方式都接受，没有异常可报时什么也不做，
最后仍交给 Entari 的回调，日志格式保持不变。Entari 的 logger 一旦（再次）导入就会把两个钩子
改回去，所以安装前先确保它已导入，启动完成后再核对一次。
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


def hooks_installed() -> bool:
    return traceback.print_exception is print_exception and sys.excepthook is excepthook


def install_exception_hooks() -> None:
    """Replace Entari's hooks; idempotent and independent of import order."""
    try:
        # 导入 Entari 的 logger 就会覆盖两个钩子，必须让它先发生。
        import arclet.entari.logger  # noqa: F401
    except ImportError:
        pass
    traceback.print_exception = print_exception
    sys.excepthook = excepthook


def _describe(hook: Any) -> str:
    return f"{getattr(hook, '__module__', '?')}.{getattr(hook, '__qualname__', type(hook).__name__)}"


async def reassert_exception_hooks() -> None:
    """Startup check: put the hooks back if something loaded later replaced them."""
    if hooks_installed():
        return
    logger.warning(
        "[core] exception hooks were replaced after install (print_exception={}, excepthook={}); reinstalling",
        _describe(traceback.print_exception), _describe(sys.excepthook),
    )
    install_exception_hooks()
