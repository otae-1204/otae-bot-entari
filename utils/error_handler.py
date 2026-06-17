"""统一错误处理装饰器 — sync + async 两种."""

from __future__ import annotations

import traceback
from functools import wraps
from typing import Any, Callable, TypeVar
import logging

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def safe_call(default_return: Any = None, *, reraise: bool = False):
    """同步函数错误捕获."""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception:
                logger.error(f"[{func.__qualname__}] 执行失败:\n{traceback.format_exc()}")
                if reraise:
                    raise
                return default_return
        return wrapper  # type: ignore[return-value]
    return decorator


def async_safe_call(default_return: Any = None, *, reraise: bool = False):
    """异步函数错误捕获."""
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.error(f"[{func.__qualname__}] 执行失败:\n{traceback.format_exc()}")
                if reraise:
                    raise
                return default_return
        return wrapper  # type: ignore[return-value]
    return decorator


def suppress_error(default_return: Any = None):
    """快速装饰器：静默吞异常并返回默认值."""
    return async_safe_call(default_return)
