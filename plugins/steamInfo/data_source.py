import json
import time
from PIL import Image
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple

from configs.path_config import IMAGE_PATH
from .models import PlayerSummariesResponse


def _unknown_avatar() -> Image.Image:
    with Image.open(Path(IMAGE_PATH) / "steamInfo/unknown_avatar.jpg") as image:
        return image.copy()


def _append_bind_record(
    result: Dict[str, List[Dict[str, Optional[str]]]],
    parent_id: Any,
    user_id: Any,
    steam_id: Any,
    nickname: Any = None,
) -> None:
    if parent_id is None or user_id is None or steam_id is None:
        return
    parent_id = str(parent_id)
    record = {
        "user_id": str(user_id),
        "steam_id": str(steam_id),
        "nickname": nickname or None,
    }
    records = result.setdefault(parent_id, [])
    if not any(
        item["user_id"] == record["user_id"]
        and item["steam_id"] == record["steam_id"]
        for item in records
    ):
        records.append(record)


def _normalize_bind_records(records: Any) -> List[Dict[str, Optional[str]]]:
    if not isinstance(records, list):
        return []
    result = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        user_id = record.get("user_id")
        steam_id = record.get("steam_id")
        if user_id is None or steam_id is None:
            continue
        normalized = {
            "user_id": str(user_id),
            "steam_id": str(steam_id),
            "nickname": record.get("nickname") or None,
        }
        key = (normalized["user_id"], normalized["steam_id"])
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def normalize_bind_map(data: Any) -> Dict[str, List[Dict[str, Optional[str]]]]:
    if not isinstance(data, dict):
        return {}

    result: Dict[str, List[Dict[str, Optional[str]]]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            records = _normalize_bind_records(value)
            if records:
                result[str(key)] = records
        elif isinstance(value, dict):
            steam_id = value.get("steam_id")
            for group in value.get("bindGroups", []):
                if isinstance(group, dict):
                    _append_bind_record(
                        result,
                        group.get("group_id"),
                        key,
                        steam_id,
                        group.get("nickname"),
                    )
    return result


def format_display_name(
    steam_name: Any, nickname: Any = None, fallback: Any = ""
) -> str:
    if nickname:
        nickname = str(nickname)
        return nickname if nickname.startswith("*") else f"*{nickname}"
    return str(steam_name or fallback)


class BindData:
    def __init__(self, save_path: Path) -> None:
        self.content: Dict[str, List[Dict[str, str]]] = {}
        self._save_path = save_path

        if save_path.exists():
            self.content = json.loads(Path(save_path).read_text("utf-8"))
            self._normalize()
        else:
            self.save()

    def _normalize(self) -> None:
        normalized = normalize_bind_map(self.content)
        if normalized != self.content:
            self.content = normalized
            self.save()

    def save(self) -> None:
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump(self.content, f, indent=4)

    def add(self, parent_id: str, content: Dict[str, str]) -> None:
        parent_id = str(parent_id)
        content["user_id"] = str(content["user_id"])
        content["steam_id"] = str(content["steam_id"])
        content.setdefault("nickname", None)
        records = [
            record
            for record in self.content.get(parent_id, [])
            if record.get("user_id") != content["user_id"]
        ]
        records.append(content)
        self.content[parent_id] = records

    def remove(self, parent_id: str, user_id: str) -> List[Dict[str, str]]:
        parent_id = str(parent_id)
        user_id = str(user_id)
        if parent_id not in self.content:
            return []
        removed = [
            record
            for record in self.content[parent_id]
            if record.get("user_id") == user_id
        ]
        remaining = [
            record
            for record in self.content[parent_id]
            if record.get("user_id") != user_id
        ]
        if remaining:
            self.content[parent_id] = remaining
        else:
            self.content.pop(parent_id, None)
        return removed

    def update(self, parent_id: str, content: Dict[str, str]) -> None:
        self.content[parent_id] = content

    def get(self, parent_id: str, user_id: str) -> Optional[Dict[str, str]]:
        if parent_id not in self.content:
            return None
        for data in self.content[parent_id]:
            if data["user_id"] == user_id:
                data.setdefault("nickname", None)
                return data
        return None

    def get_by_steam_id(self, parent_id: str, steam_id: str) -> Optional[Dict[str, str]]:
        if parent_id not in self.content:
            return None
        for data in self.content[parent_id]:
            if data["steam_id"] == steam_id:
                data.setdefault("nickname", None)
                return data
        return None

    def get_all(self, parent_id: str) -> List[str]:
        if parent_id not in self.content:
            return []

        result = []

        for data in self.content[parent_id]:
            if not data["steam_id"] in result:
                result.append(data["steam_id"])

        return result


class SteamInfoData:
    def __init__(self, save_path: Path) -> None:
        self.content: Dict[str, PlayerSummariesResponse] = {}
        self._save_path = save_path

        if save_path.exists():
            self.content = json.loads(save_path.read_text("utf-8"))
        else:
            self.save()

    def save(self) -> None:
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump(self.content, f, indent=4)

    def update(self, parent_id: str, content: PlayerSummariesResponse) -> None:
        old_content = self.content.get(parent_id, {"players": []})
        old_players = {
            player["steamid"]: player for player in old_content.get("players", [])
        }

        for player in content["players"]:
            old_player = old_players.get(player["steamid"])
            if player.get("gameextrainfo") is None:
                player["game_start_time"] = None
            elif old_player is None or old_player.get("gameextrainfo") != player.get(
                "gameextrainfo"
            ):
                player["game_start_time"] = int(time.time())
            else:
                player["game_start_time"] = old_player.get("game_start_time")

        self.content[parent_id] = content

    def get(self, parent_id: str) -> Optional[PlayerSummariesResponse]:
        return self.content.get(parent_id, None)

    def prune_players(self, parent_id: str, keep_steam_ids: List[str]) -> bool:
        if parent_id not in self.content:
            return False
        keep = {str(steam_id) for steam_id in keep_steam_ids}
        if not keep:
            self.content.pop(parent_id, None)
            return True
        content = self.content.get(parent_id, {})
        players = content.get("players", [])
        if not isinstance(players, list):
            self.content.pop(parent_id, None)
            return True
        filtered_players = [
            player
            for player in players
            if str(player.get("steamid")) in keep
        ]
        if len(filtered_players) == len(players):
            return False
        content["players"] = filtered_players
        self.content[parent_id] = content
        return True

    def compare(
        self, parent_id: str, new_content: PlayerSummariesResponse
    ) -> List[str]:
        old_content = self.get(parent_id)

        if old_content is None:
            self.update(parent_id, new_content)
            self.save()
            return []

        result = []

        for player in new_content["players"]:
            for old_player in old_content["players"]:
                if player["steamid"] == old_player["steamid"]:
                    if player.get("gameextrainfo") != old_player.get("gameextrainfo"):
                        if (
                            player.get("gameextrainfo") is not None
                            and old_player.get("gameextrainfo") is not None
                        ):
                            result.append(
                                {
                                    "type": "change",
                                    "player": player,
                                    "old_player": old_player,
                                }
                            )
                        elif player.get("gameextrainfo") is not None:
                            result.append(
                                {
                                    "type": "start",
                                    "player": player,
                                    "old_player": old_player,
                                }
                            )
                        elif old_player.get("gameextrainfo") is not None:
                            result.append(
                                {
                                    "type": "stop",
                                    "player": player,
                                    "old_player": old_player,
                                }
                            )
                        else:
                            result.append(
                                {
                                    "type": "error",
                                    "player": player,
                                    "old_player": old_player,
                                }
                            )
        return result


class ParentData:
    def __init__(self, save_path: Path) -> None:
        self.content: Dict[str, str] = {}  # parent_id: name
        self._save_path = save_path

        if not save_path.exists():
            save_path.parent.mkdir(parents=True, exist_ok=True)
            self.save()
        else:
            self.content = json.loads(save_path.read_text("utf-8"))

    def save(self) -> None:
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump(self.content, f, indent=4)

    def update(self, parent_id: str, avatar: Image.Image, name: str) -> None:
        self.content[parent_id] = name
        self.save()
        # 保存图片
        avatar_path = self._save_path.parent / f"{parent_id}.png"
        avatar.save(avatar_path)

    def has_avatar(self, parent_id: str) -> bool:
        avatar_path = self._save_path.parent / f"{parent_id}.png"
        if not avatar_path.exists():
            return False
        try:
            with Image.open(avatar_path) as image:
                image.verify()
            return True
        except Exception:
            return False

    def get(self, parent_id: str) -> Tuple[Image.Image, str]:
        fallback_avatar = _unknown_avatar()
        if parent_id not in self.content:
            return fallback_avatar, parent_id
        avatar_path = self._save_path.parent / f"{parent_id}.png"
        try:
            return Image.open(avatar_path), self.content[parent_id]
        except Exception:
            return fallback_avatar, self.content[parent_id]


class DisableParentData:
    """储存禁用 Steam 通知的 parent"""

    def __init__(self, save_path: Path) -> None:
        self.content: List[str] = []
        self._save_path = save_path

        if save_path.exists():
            self.content = json.loads(save_path.read_text("utf-8"))
        else:
            self.save()

    def save(self) -> None:
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump(self.content, f, indent=4)

    def add(self, parent_id: str) -> None:
        if parent_id not in self.content:
            self.content.append(parent_id)
            self.save()

    def remove(self, parent_id: str) -> None:
        if parent_id in self.content:
            self.content.remove(parent_id)
            self.save()

    def is_disabled(self, parent_id: str) -> bool:
        return parent_id in self.content


def _load_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def repair_from_project_data(
    project_data_dir: Path,
    bind_data: BindData,
    steam_info_data: SteamInfoData,
    parent_data: ParentData,
) -> Dict[str, int]:
    """Backfill localstore from project data without overwriting valid entries."""

    stats = {"bind": 0, "steam_info": 0, "parent": 0}

    should_backfill_bind = not bind_data._save_path.exists() or not bind_data.content
    project_bind = (
        normalize_bind_map(_load_json_object(project_data_dir / "data.json"))
        if should_backfill_bind
        else {}
    )
    bind_changed = False
    for parent_id, normalized_records in project_bind.items():
        parent_id = str(parent_id)
        current_records = bind_data.content.setdefault(parent_id, [])
        seen = {
            (str(item.get("user_id")), str(item.get("steam_id")))
            for item in current_records
            if isinstance(item, dict)
        }
        for record in normalized_records:
            key = (record["user_id"], record["steam_id"])
            if key not in seen:
                current_records.append(record)
                seen.add(key)
                stats["bind"] += 1
                bind_changed = True
        for record in current_records:
            if isinstance(record, dict) and "nickname" not in record:
                record["nickname"] = None
                bind_changed = True
    if bind_changed:
        bind_data.save()

    project_parent = _load_json_object(project_data_dir / "parent_data.json")
    parent_changed = False
    for parent_id, name in project_parent.items():
        parent_id = str(parent_id)
        name = str(name or "")
        if name and not parent_data.content.get(parent_id):
            parent_data.content[parent_id] = name
            stats["parent"] += 1
            parent_changed = True
    if parent_changed:
        parent_data.save()

    project_steam_info = _load_json_object(project_data_dir / "steam_info.json")
    steam_changed = False
    for parent_id, content in project_steam_info.items():
        parent_id = str(parent_id)
        if parent_id not in steam_info_data.content and isinstance(content, dict):
            steam_info_data.content[parent_id] = content
            stats["steam_info"] += 1
            steam_changed = True
    if steam_changed:
        steam_info_data.save()

    return stats
