"""Persistent plugin switches, scoped to one account and one group."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from satori import ChannelType

PROTECTED_PLUGINS = {"group_manager", "request_handler"}
DEFAULT_OFF_PLUGINS = {"grok_bot"}
SUPERUSER_ENABLE_PLUGINS = {"grok_bot"}
PLUGIN_NAMES = {
    "McModQuery": ("MC 百科", "mod", "模组", "mcmod"),
    "McWikiQuery": ("MC Wiki", "wiki"),
    "bilibilibot": ("B站", "bili", "bilibili"),
    "endfield": ("终末地", "ef", "zmd"),
    "forkout": ("叉出去", "fork"),
    "grok_bot": ("Grok Bot", "grok", "grokbot"),
    "help_plugin": ("帮助", "help"),
    "hyw": ("HYW 问答", "q", "何意味"),
    "mcsm": ("MCSManager",),
    "minecraft_plugin": ("Minecraft", "mc", "我的世界"),
    "peek": ("窥视",),
    "steamInfo": ("Steam", "steam"),
    "tibo_radar": ("Tibo 雷达", "tibo", "雷达"),
}


def plugin_key(module: str) -> str:
    parts = module.split("@", 1)[0].split(".")
    return parts[1] if len(parts) >= 2 and parts[0] == "plugins" else ""


def plugin_label(plugin: str) -> str:
    return PLUGIN_NAMES.get(plugin, (plugin,))[0]


def resolve_plugin(name: str, available: list[str]) -> str | None:
    name = name.strip().casefold().removeprefix("plugins.")
    for plugin in available:
        if name in {item.casefold() for item in (plugin, *PLUGIN_NAMES.get(plugin, ()))}:
            return plugin
    return None


@dataclass(frozen=True)
class GroupScope:
    platform: str
    self_id: str
    group_id: str
    private: bool = False

    @property
    def key(self) -> str:
        if self.private:
            return json.dumps(["private", self.platform, self.self_id, self.group_id], ensure_ascii=False)
        return json.dumps([self.platform, self.self_id, self.group_id], ensure_ascii=False)


def group_scope(account: Any, group_id: str) -> GroupScope | None:
    platform = str(getattr(account, "platform", "") or "")
    self_id = str(getattr(account, "self_id", "") or "")
    if not platform or not self_id or not group_id:
        return None
    return GroupScope(platform, self_id, str(group_id))


def scope_from_event(account: Any, event: Any) -> GroupScope | None:
    channel = getattr(event, "channel", None)
    channel_type = getattr(channel, "type", None)
    if channel_type in (ChannelType.DIRECT, "direct"):
        return None
    group_id = getattr(getattr(event, "guild", None), "id", "")
    if not group_id and channel_type == ChannelType.TEXT:
        group_id = getattr(channel, "id", "")
    return group_scope(account, group_id)


class GroupFeatureStore:
    """Keep explicit switches; commit atomically before changing memory.

    A bot process is protected by the application's run lock. The local lock
    also serializes updates/reads made by worker threads. Invalid files are
    reported instead of silently losing the administrator's saved switches.
    """

    def __init__(self, path: Path = Path("data/group_manager/switches.json")):
        self.path = path
        self._disabled: dict[str, set[str]] | None = None
        self._enabled: dict[str, set[str]] = {}
        self._lock = RLock()

    def _load(self) -> dict[str, set[str]]:
        if self._disabled is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {"version": 1, "disabled": {}}
            if not isinstance(data, dict) or data.get("version") not in (1, 2):
                raise ValueError("invalid group feature store version")
            groups = data.get("disabled")
            enabled = data.get("enabled") if data["version"] == 2 else {}
            for mapping in (groups, enabled):
                if not isinstance(mapping, dict) or any(
                    not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                    for items in mapping.values()
                ):
                    raise ValueError("invalid group feature switches")
            self._enabled = {key: set(items) for key, items in enabled.items()}
            self._disabled = {key: set(items) for key, items in groups.items()}
        return self._disabled

    def is_enabled(self, scope: GroupScope | None, plugin: str) -> bool:
        if scope is None or not plugin or plugin in PROTECTED_PLUGINS:
            return True
        with self._lock:
            disabled = self._load().get(scope.key, set())
            return plugin not in disabled and (
                plugin not in DEFAULT_OFF_PLUGINS or plugin in self._enabled.get(scope.key, set())
            )

    def set_enabled(self, scope: GroupScope, plugin: str, enabled: bool) -> bool:
        if plugin in PROTECTED_PLUGINS or not plugin:
            raise ValueError("this plugin cannot be switched per group")
        with self._lock:
            current = self._load()
            disabled = set(current.get(scope.key, ()))
            if self.is_enabled(scope, plugin) == enabled:
                return False
            explicit = set(self._enabled.get(scope.key, ()))
            if enabled:
                disabled.discard(plugin)
                explicit.add(plugin)
            else:
                disabled.add(plugin)
                explicit.discard(plugin)
            updated = dict(current)
            updated_enabled = dict(self._enabled)
            if explicit:
                updated_enabled[scope.key] = explicit
            else:
                updated_enabled.pop(scope.key, None)
            if disabled:
                updated[scope.key] = disabled
            else:
                updated.pop(scope.key, None)
            payload = {"version": 2, "disabled": {key: sorted(items) for key, items in updated.items()},
                       "enabled": {key: sorted(items) for key, items in updated_enabled.items()}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False,
                ) as stream:
                    temp_path = Path(stream.name)
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            self._disabled = updated
            self._enabled = updated_enabled
            return True


feature_store = GroupFeatureStore()
