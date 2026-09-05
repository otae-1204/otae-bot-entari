"""Runtime account registry for Entari scheduled/background tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from arclet.entari import Account

_account: Account | None = None
_status: Any = None
_updated_at: datetime | None = None


def set_account(account: Account, status: Any = None) -> None:
    global _account, _status, _updated_at
    _account = account
    _status = status
    _updated_at = datetime.now(timezone.utc)


def clear_account(account_id: str | None = None) -> None:
    global _account, _status, _updated_at
    if account_id is not None and _account is not None:
        current_id = str(getattr(_account, "self_id", "") or getattr(_account, "id", ""))
        if current_id and current_id != str(account_id):
            return
    _account = None
    _status = None
    _updated_at = datetime.now(timezone.utc)


def get_account() -> Account | None:
    return _account


def get_status() -> Any:
    return _status


def get_updated_at() -> datetime | None:
    return _updated_at


def is_online() -> bool:
    return _account is not None
