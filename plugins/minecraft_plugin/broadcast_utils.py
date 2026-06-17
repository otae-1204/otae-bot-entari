"""Minecraft 广播纯逻辑工具。"""

from __future__ import annotations

from collections import defaultdict
import time


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}小时 {minutes}分钟" if hours else f"{minutes}分钟"


def build_player_change_messages(
    server_name: str,
    previous_players: dict | None,
    current_players: list[str] | None,
    timestamp: int,
) -> tuple[list[str], dict[str, int]]:
    """生成玩家变化消息和离线玩家游戏时长增量。"""
    messages: list[str] = []
    playtime_deltas: dict[str, int] = {}

    if current_players is None and previous_players is not None:
        return [f"[MC_Server] 服务器 {server_name} 已关闭"], playtime_deltas
    if current_players is not None and previous_players is None:
        if current_players:
            joined = "、".join(sorted(current_players))
            return [f"[MC_Server] 服务器 {server_name} 已启动，当前在线: {joined}"], playtime_deltas
        return [f"[MC_Server] 服务器 {server_name} 已启动"], playtime_deltas

    if current_players is None or previous_players is None:
        return messages, playtime_deltas

    current_set = set(current_players)
    previous_set = set(previous_players)
    joined = sorted(current_set - previous_set)
    left = sorted(previous_set - current_set)

    if joined:
        messages.append(f"[MC_Server] {server_name}: {'、'.join(joined)} 加入了服务器")

    if left:
        left_parts = []
        for player in left:
            duration = timestamp - int(previous_players.get(player, timestamp))
            playtime_deltas[player] = duration
            left_parts.append(f"{player}({format_duration(duration)})")
        messages.append(f"[MC_Server] {server_name}: {'、'.join(left_parts)} 离开了服务器")

    return messages, playtime_deltas


def should_send_error_digest(
    group_id: str,
    error_count: int,
    now: int,
    last_sent_at: dict[str, int],
    cooldown: int,
) -> bool:
    """同一群广播错误摘要按 cooldown 秒节流。"""
    if error_count <= 0:
        return False
    gid = str(group_id)
    last = int(last_sent_at.get(gid, 0))
    if now - last < cooldown:
        return False
    last_sent_at[gid] = now
    return True


def group_errors_by_group(errors: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for err in errors:
        group_id = "global"
        if err.startswith("群 "):
            parts = err.split(" ", 2)
            if len(parts) >= 2:
                group_id = parts[1]
        grouped[group_id].append(err)
    return dict(grouped)


def build_broadcast_snapshot(addr: str, info: dict, now: int | None = None) -> dict:
    """Build a display snapshot from a cached broadcast server record."""
    players_dict = info.get("players")
    name = info.get("name", addr)
    now = int(time.time()) if now is None else now

    if info.get("players_hidden"):
        player_list, playtimes, status = [], {}, "success"
        online_count = int(info.get("online_players", 0) or 0)
    elif isinstance(players_dict, dict):
        player_list = list(players_dict.keys()) if players_dict else []
        online_count = len(player_list)
        playtimes: dict[str, str] = {}
        for pname, join_ts in (players_dict or {}).items():
            duration = now - int(join_ts)
            hours, remainder = divmod(duration, 3600)
            minutes = remainder // 60
            playtimes[pname] = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        status = "success"
    else:
        player_list, online_count, playtimes, status = [], 0, {}, "error"

    return {
        "name": name,
        "address": addr,
        "status": status,
        "data": {
            "players": player_list,
            "playtimes": playtimes,
            "online_players": online_count,
            "players_hidden": bool(info.get("players_hidden")),
        },
    }
