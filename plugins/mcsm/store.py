"""Persistent storage for the MCSM plugin.

Data is scoped per group:

{
  "<group_id>": {
    "panel_url": "http://...",
    "api_key": "...",
    "owner": "<legacy_user_id>",
    "admins": ["<user_id>", ...],
    "instances": {
      "<alias>": {
        "uuid": "...",
        "daemonId": "...",
        "hidden": false
      }
    }
  }
}
"""

from __future__ import annotations

from typing import Dict, List, Optional

from utils.json_store import JsonStore


class MCSMStore:
    """Per-group panel, group-admin, and instance binding storage."""

    def __init__(self, file_path: str = "data/mcsm/bindings.json") -> None:
        self._store = JsonStore(file_path)
        # In-memory DM wait state: {user_id: (group_id, panel_url)}
        self._pending_keys: Dict[str, tuple[str, str]] = {}

    def _normalize_group(self, group: dict) -> bool:
        changed = False
        instances = group.setdefault("instances", {})
        admins = group.setdefault("admins", [])
        if not isinstance(admins, list):
            admins = []
            group["admins"] = admins
            changed = True

        # Legacy compatibility: the old panel binder becomes a group admin.
        owner = str(group.get("owner", "") or "")
        if owner and owner not in admins:
            admins.append(owner)
            changed = True

        if isinstance(instances, dict):
            for inst in instances.values():
                if not isinstance(inst, dict):
                    continue
                if "admins" in inst:
                    inst.pop("admins", None)
                    changed = True
                if "hidden" not in inst:
                    inst["hidden"] = False
                    changed = True
        return changed

    def _group(self, group_id: str | int) -> dict:
        gid = str(group_id)
        if gid not in self._store:
            self._store[gid] = {"instances": {}, "admins": []}
        group = self._store[gid]
        if self._normalize_group(group):
            self._store._save()
        return group

    def _existing_group(self, group_id: str | int) -> dict | None:
        group = self._store.get(str(group_id))
        if group and self._normalize_group(group):
            self._store._save()
        return group

    def has_panel(self, group_id: str | int) -> bool:
        group = self._existing_group(group_id)
        return bool(group and group.get("panel_url") and group.get("api_key"))

    def get_panel(self, group_id: str | int) -> tuple[str, str] | None:
        group = self._existing_group(group_id)
        if not group or not group.get("panel_url") or not group.get("api_key"):
            return None
        return (group["panel_url"], group["api_key"])

    def set_panel_url(self, group_id: str | int, url: str) -> None:
        self._group(group_id)["panel_url"] = url.rstrip("/")
        self._store._save()

    def set_api_key(self, group_id: str | int, api_key: str) -> None:
        self._group(group_id)["api_key"] = api_key
        self._store._save()

    def set_owner(self, group_id: str | int, user_id: str) -> None:
        group = self._group(group_id)
        group["owner"] = str(user_id)
        if str(user_id) not in group.setdefault("admins", []):
            group["admins"].append(str(user_id))
        self._store._save()

    def get_owner(self, group_id: str | int) -> str:
        group = self._existing_group(group_id)
        return str(group.get("owner", "")) if group else ""

    def clear_panel(self, group_id: str | int) -> None:
        """Clear panel credentials and all instance bindings, keeping group admins."""
        group = self._existing_group(group_id)
        if not group:
            return
        group.pop("panel_url", None)
        group.pop("api_key", None)
        group["instances"] = {}
        self._normalize_group(group)
        self._store._save()

    def set_pending_key(self, user_id: str, group_id: str, panel_url: str) -> None:
        self._pending_keys[str(user_id)] = (str(group_id), panel_url)

    def get_pending_key(self, user_id: str) -> tuple[str, str] | None:
        return self._pending_keys.get(str(user_id))

    def clear_pending_key(self, user_id: str) -> None:
        self._pending_keys.pop(str(user_id), None)

    def get_group_instances(self, group_id: str | int) -> Dict[str, dict]:
        group = self._existing_group(group_id)
        if not group:
            return {}
        return group.get("instances", {})

    def bind_instance(self, group_id: str | int, alias: str, uuid: str, daemon_id: str) -> None:
        group = self._group(group_id)
        group["instances"][alias] = {
            "uuid": uuid,
            "daemonId": daemon_id,
            "hidden": False,
        }
        self._store._save()

    def unbind_instance(self, group_id: str | int, alias: str) -> bool:
        instances = self.get_group_instances(group_id)
        if alias not in instances:
            return False
        del instances[alias]
        self._store._save()
        return True

    def get_instance(self, group_id: str | int, alias: str) -> Optional[dict]:
        return self.get_group_instances(group_id).get(alias)

    def alias_exists(self, group_id: str | int, alias: str) -> bool:
        return alias in self.get_group_instances(group_id)

    def find_instance_by_uuid(self, group_id: str | int, uuid: str) -> Optional[str]:
        for alias, info in self.get_group_instances(group_id).items():
            if info.get("uuid") == uuid:
                return alias
        return None

    def find_instance_by_name(self, group_id: str | int, name: str) -> List[str]:
        name_lower = name.lower()
        return [a for a in self.get_group_instances(group_id) if name_lower in a.lower()]

    def set_hidden(self, group_id: str | int, alias: str, hidden: bool) -> bool:
        inst = self.get_instance(group_id, alias)
        if inst is None:
            return False
        inst["hidden"] = hidden
        self._store._save()
        return True

    def get_visible_instances(self, group_id: str | int) -> Dict[str, dict]:
        return {
            alias: inst
            for alias, inst in self.get_group_instances(group_id).items()
            if not inst.get("hidden")
        }

    def add_admin(self, group_id: str | int, user_id: str) -> bool:
        admins = self._group(group_id).setdefault("admins", [])
        user_id = str(user_id)
        if user_id in admins:
            return False
        admins.append(user_id)
        self._store._save()
        return True

    def remove_admin(self, group_id: str | int, user_id: str) -> bool:
        admins = self._group(group_id).setdefault("admins", [])
        user_id = str(user_id)
        if user_id not in admins:
            return False
        admins.remove(user_id)
        self._store._save()
        return True

    def get_admins(self, group_id: str | int) -> List[str]:
        group = self._existing_group(group_id)
        if not group:
            return []
        return list(group.get("admins", []))

    def is_admin(self, group_id: str | int, user_id: str) -> bool:
        return str(user_id) in self.get_admins(group_id)

    def check_instance_permission(self, group_id: str | int, alias: str, user_id: str) -> bool:
        return self.is_admin(group_id, user_id)

    def get_all_group_ids(self) -> List[str]:
        return list(self._store.keys())
